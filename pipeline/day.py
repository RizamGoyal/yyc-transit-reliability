"""Process one service day for every route: GPS -> stop arrivals/departures -> summaries.

A Calgary service day runs from about 4 a.m. to 1:30 a.m. the next calendar day, and
trips can be judged up to 90 minutes past their scheduled end. So we load GPS from
02:00 on the service day to 05:00 the next day (local time), which is 27 hourly files.
Files are named by UTC hour, so the list is built in UTC (this also handles the
daylight-saving change correctly).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.arrivals import measure_lag, route_day_arrivals
from pipeline.match import TZ, check_match_rate, match_trips
from pipeline.positions import load_files
from pipeline.schedule import active_services, version_for

LOAD_FROM_H = 2    # local hour on the service day
LOAD_HOURS = 27    # ... through 05:00 the next day
EARLY_S, LATE_S = -60, 300  # on time = 1 min early to 5 min late


def hour_files(positions_dir: Path, service_date: date) -> tuple[list[Path], int]:
    """Existing hourly GPS files covering the service day, and how many hours are missing."""
    start = datetime(service_date.year, service_date.month, service_date.day, LOAD_FROM_H, tzinfo=TZ)
    start_utc = start.astimezone(timezone.utc)
    files, missing = [], 0
    for h in range(LOAD_HOURS):
        t = start_utc + timedelta(hours=h)
        f = positions_dir / t.strftime("%Y-%m-%d") / f"{t:%H}.csv.gz"
        if f.exists():
            files.append(f)
        else:
            missing += 1
    return files, missing


def otp_counts(delays: np.ndarray) -> tuple[int, int, int, int]:
    """(n, early, on_time, late) for delays in seconds; NaN delays are ignored."""
    d = delays[~np.isnan(delays)]
    early = int((d < EARLY_S).sum())
    late = int((d > LATE_S).sum())
    return len(d), early, len(d) - early - late, late


def scheduled_trips_by_route(db: sqlite3.Connection, service_date: date) -> dict[str, int]:
    version = version_for(db, service_date)
    if version is None:
        return {}
    services = sorted(active_services(db, version, service_date))
    if not services:
        return {}
    rows = db.execute(
        "SELECT r.route_short_name, COUNT(*) FROM trips t JOIN routes r "
        "ON r.version = t.version AND r.route_id = t.route_id "
        f"WHERE t.version = ? AND t.service_id IN ({','.join('?' * len(services))}) "
        "GROUP BY r.route_short_name",
        (version, *services),
    ).fetchall()
    return dict(rows)


@dataclass
class DayResult:
    events: pd.DataFrame                      # one row per trip stop
    routes: pd.DataFrame                      # one row per route
    info: dict = field(default_factory=dict)  # whole-day counters


def _route_summary(route: str, arr: pd.DataFrame, loo: np.ndarray, st: dict) -> dict:
    out = {"route": route, **st}
    if arr.empty:
        return out
    first = arr["stop_sequence"] == arr.groupby("trip_id", observed=True)["stop_sequence"].transform("min")
    last = arr["stop_sequence"] == arr.groupby("trip_id", observed=True)["stop_sequence"].transform("max")
    visits = arr[~first]
    n, e, o, late = otp_counts(visits["delay_s"].to_numpy(dtype=float))
    out.update(arrivals_possible=len(visits), arrivals=n, arr_early=e, arr_on_time=o, arr_late=late)
    tp = arr[(arr["timepoint"] == 1) & ~last]
    dd = tp["dep_delay_s"].to_numpy(dtype=float)
    n, e, o, late = otp_counts(dd)
    out.update(tp_possible=len(tp), tp_departures=n, tp_early=e, tp_on_time=o, tp_late=late)
    dd = dd[~np.isnan(dd)]
    out.update(tp_delay_p50=float(np.median(dd)) if len(dd) else None,
               tp_delay_p90=float(np.percentile(dd, 90)) if len(dd) else None)
    a = np.abs(loo)
    out.update(loo_n=len(a), loo_abs_p50=float(np.median(a)) if len(a) else None,
               loo_abs_p90=float(np.percentile(a, 90)) if len(a) else None)
    return out


def process_day(positions_dir: Path, db: sqlite3.Connection, service_date: date) -> DayResult:
    files, missing = hour_files(positions_dir, service_date)
    info = {"service_date": service_date.isoformat(), "hour_files": len(files), "hours_missing": missing}
    if not files:
        return DayResult(pd.DataFrame(), pd.DataFrame(), {**info, "rows": 0})

    m = match_trips(load_files(files), db)
    check_match_rate(m, db)
    info.update(rows=len(m), rows_with_trip=int(m["trip_id"].notna().sum()),
                rows_matched=int(m["matched"].sum()))
    day = m[m["service_active"] & (m["service_date"] == service_date)]
    del m
    lag_s, lag_n = measure_lag(day)
    info.update(rows_used=len(day), lag_s=lag_s, lag_fixes=lag_n)

    scheduled = scheduled_trips_by_route(db, service_date)
    events, routes = [], []
    for route, g in day.groupby("route_short_name", observed=True, sort=True):
        arr, loo, st = route_day_arrivals(g, db, lag_s=lag_s)
        st = {"trips_scheduled": scheduled.get(route, 0), "trips_with_gps": st.pop("trips"), **st}
        routes.append(_route_summary(str(route), arr, loo, st))
        if not arr.empty:
            events.append(arr)
    # Routes scheduled that day with no GPS at all (e.g. CTrain) still get a row.
    for route in sorted(set(scheduled) - {r["route"] for r in routes}):
        routes.append({"route": route, "trips_scheduled": scheduled[route], "trips_with_gps": 0})
    ev = pd.concat(events, ignore_index=True) if events else pd.DataFrame()
    rt = pd.DataFrame(routes)
    rt.insert(0, "service_date", service_date.isoformat())
    return DayResult(ev, rt, info)
