"""Attach each GPS row to its scheduled trip, route and service day.

Service day: GTFS trips belong to a service day that can run past midnight (a 24:40 stop
time is 12:40 a.m. the next calendar day). If the feed gives start_date we use it. If not,
we try "today" and "yesterday" (local time) and keep whichever puts the observed time
inside, or closest to, the trip's scheduled span.

Limitation: the schedule *version* is picked from the local calendar date of the GPS row.
That's only wrong for a trip that crosses midnight on the exact day a new schedule
takes over, which is rare and documented.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd

from pipeline.schedule import active_services, version_for

TZ = ZoneInfo("America/Edmonton")


def _trip_table(db: sqlite3.Connection, versions: list[str]) -> pd.DataFrame:
    """trip_id -> route, service, shape, scheduled start/end (service-day seconds)."""
    q = f"""
        SELECT t.version, t.trip_id, t.route_id, r.route_short_name, t.service_id, t.direction_id,
               t.shape_id, MIN(COALESCE(st.departure_s, st.arrival_s)) AS trip_start_s,
               MAX(COALESCE(st.arrival_s, st.departure_s)) AS trip_end_s
        FROM trips t
        LEFT JOIN routes r ON r.version = t.version AND r.route_id = t.route_id
        LEFT JOIN stop_times st ON st.version = t.version AND st.trip_id = t.trip_id
        WHERE t.version IN ({",".join("?" * len(versions))})
        GROUP BY t.version, t.trip_id
    """
    return pd.read_sql_query(q, db, params=versions, dtype={"trip_id": "string", "version": "string"})


def _distance_to_span(x: pd.Series, start: pd.Series, end: pd.Series) -> pd.Series:
    """0 inside [start, end], otherwise seconds outside it."""
    return (start - x).clip(lower=0) + (x - end).clip(lower=0)


def match_trips(pos: pd.DataFrame, db: sqlite3.Connection) -> pd.DataFrame:
    """Returns pos plus: version, route_id (from schedule), route_short_name, service_id,
    direction_id, shape_id, trip_start_s, trip_end_s, service_date, service_s (seconds
    after service-day midnight), matched, service_active."""
    out = pos.reset_index(drop=True).copy()
    local = pd.to_datetime(out["feed_ts"], unit="s", utc=True).dt.tz_convert(TZ)
    local_date = local.dt.date
    secs = local.dt.hour * 3600 + local.dt.minute * 60 + local.dt.second

    # Schedule version per local date.
    vmap = {d: version_for(db, d) for d in local_date.unique()}
    out["version"] = local_date.map(vmap).astype("string")
    versions = sorted({v for v in vmap.values() if v})
    if not versions:
        raise ValueError("no schedule loaded; run python -m pipeline.schedule first")

    trips = _trip_table(db, versions)
    out = out.drop(columns=["route_id"]).merge(trips, on=["version", "trip_id"], how="left")
    out["matched"] = out["service_id"].notna()

    # Service date: feed's start_date if valid, else whichever of today/yesterday fits.
    # Kept as datetime64 (midnight) while computing, converted to datetime.date at the end.
    today = pd.to_datetime(local.dt.strftime("%Y-%m-%d")).reset_index(drop=True)
    yesterday = today - pd.Timedelta(days=1)
    given = pd.to_datetime(out["start_date"], format="%Y%m%d", errors="coerce")
    secs = secs.reset_index(drop=True)
    dist_today = _distance_to_span(secs, out["trip_start_s"], out["trip_end_s"])
    dist_yest = _distance_to_span(secs + 86400, out["trip_start_s"], out["trip_end_s"])
    inferred = today.where(~(dist_yest < dist_today), yesterday)
    inferred = inferred.where(out["matched"], today)  # no trip to fit against: calendar day
    service_day = given.where(given.notna(), inferred)

    # Seconds after service-day midnight, comparable to schedule times (can exceed 86400).
    out["service_s"] = secs + (today - service_day).dt.days * 86400
    out["service_date"] = service_day.dt.date

    # Is the trip's service actually scheduled to run that day?
    active: dict[tuple[str, date], set[str]] = {}
    pairs = out.loc[out["matched"], ["version", "service_date"]].drop_duplicates()
    for v, d in pairs.itertuples(index=False):
        active[(v, d)] = active_services(db, v, d)
    out["service_active"] = [
        bool(m) and s in active.get((v, d), set())
        for m, v, d, s in zip(out["matched"], out["version"], out["service_date"], out["service_id"],
                              strict=True)
    ]
    return out
