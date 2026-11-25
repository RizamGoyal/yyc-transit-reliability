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
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from pipeline.schedule import active_services, version_for


def _calgary_tz() -> ZoneInfo:
    """Calgary's time zone, from the pinned `tzdata` package rather than the OS copy.

    Alberta moved to permanent UTC-6 ("Alberta Time") on Nov 1, 2026. Python normally
    reads time-zone rules from the operating system first; a server whose copy predates
    that change would think Calgary is UTC-7 in winter and silently shift every result by
    an hour. Reading the pinned package keeps laptop, VM and CI identical. Falls back to
    the OS copy only if the package isn't installed (tests check the rules are current).
    """
    try:
        from importlib.resources import files

        with files("tzdata.zoneinfo").joinpath("America", "Edmonton").open("rb") as f:
            return ZoneInfo.from_file(f, key="America/Edmonton")
    except ModuleNotFoundError:
        return ZoneInfo("America/Edmonton")


TZ = _calgary_tz()


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


def _distance_to_span(x, start, end):
    """0 inside [start, end], otherwise seconds outside it (NaN if there's no span)."""
    return np.clip(start - x, 0, None) + np.clip(x - end, 0, None)


def match_trips(pos: pd.DataFrame, db: sqlite3.Connection) -> pd.DataFrame:
    """Returns pos plus: version, route_id (from schedule), route_short_name, service_id,
    direction_id, shape_id, trip_start_s, trip_end_s, service_date, service_s (seconds
    after service-day midnight), matched, service_active.

    Memory: a day is ~1M rows, so text columns are categorical (each distinct value stored
    once) and schedule attributes are looked up once per distinct trip, then spread to
    rows by integer code, instead of a million-row merge.
    """
    out = pos.reset_index(drop=True)
    out = out.drop(columns=[c for c in ("route_id", "bearing", "speed") if c in out.columns])
    out["trip_id"] = out["trip_id"].astype("category")
    if "vehicle_id" in out.columns:
        out["vehicle_id"] = out["vehicle_id"].astype("category")
    n = len(out)

    local = pd.to_datetime(out["feed_ts"], unit="s", utc=True).dt.tz_convert(TZ).dt.tz_localize(None)
    today = local.dt.normalize()  # local calendar day, as midnight
    secs = ((local - today) // pd.Timedelta(seconds=1)).to_numpy(dtype="int64")

    # Schedule version per local calendar day. Everything below uses plain Python strings
    # and integer codes rather than pandas' implicit label alignment, whose behaviour for
    # categorical/string dtypes has changed between pandas versions.
    day_codes_local, day_uniques_local = pd.factorize(today)
    day_versions = [version_for(db, pd.Timestamp(u).date()) for u in day_uniques_local]
    versions = sorted({v for v in day_versions if v})
    if not versions:
        raise ValueError("no schedule loaded; run python -m pipeline.schedule first")
    version_of_day = np.array([versions.index(v) if v else -1 for v in day_versions], dtype="int64")
    version_codes = version_of_day[day_codes_local] if n else np.zeros(0, dtype="int64")
    out["version"] = pd.Categorical.from_codes(version_codes, categories=versions)

    # Schedule attributes: looked up per distinct trip ID, spread to rows by code.
    trips = _trip_table(db, versions)
    trip_names = [str(x) for x in out["trip_id"].cat.categories]
    trip_codes = out["trip_id"].cat.codes.to_numpy()
    str_cols = ["route_id", "route_short_name", "service_id", "shape_id"]
    num_cols = ["direction_id", "trip_start_s", "trip_end_s"]
    str_index = {c: pd.Index(sorted({str(x) for x in trips[c] if pd.notna(x)}), dtype=object)
                 for c in str_cols}
    str_codes = {c: np.full(n, -1, dtype="int32") for c in str_cols}
    nums = {c: np.full(n, np.nan) for c in num_cols}
    trip_version = [str(x) for x in trips["version"]]
    for vi, v in enumerate(versions):
        tv = trips[[tvv == v for tvv in trip_version]]
        row_of = {str(t): i for i, t in enumerate(tv["trip_id"])}
        tv_row = np.array([row_of.get(t, -1) for t in trip_names], dtype="int64")  # per trip category
        rows = np.flatnonzero((version_codes == vi) & (trip_codes >= 0))
        sel = tv_row[trip_codes[rows]]
        rows, sel = rows[sel >= 0], sel[sel >= 0]
        for c in str_cols:
            per_tv_row = str_index[c].get_indexer([str(x) if pd.notna(x) else None for x in tv[c]])
            str_codes[c][rows] = per_tv_row[sel]
        for c in num_cols:
            nums[c][rows] = tv[c].to_numpy(dtype=float)[sel]
    for c in str_cols:
        out[c] = pd.Categorical.from_codes(str_codes[c], categories=str_index[c])
    for c in num_cols:
        out[c] = nums[c]
    out["direction_id"] = out["direction_id"].astype("Int64")
    matched = str_codes["service_id"] >= 0
    out["matched"] = matched

    # Service date: feed's start_date if valid, else whichever of today/yesterday fits.
    start_s, end_s = nums["trip_start_s"], nums["trip_end_s"]
    dist_today = _distance_to_span(secs, start_s, end_s)
    dist_yest = _distance_to_span(secs + 86400, start_s, end_s)
    today_np = today.to_numpy()
    use_yest = matched & (dist_yest < dist_today)
    inferred = np.where(use_yest, today_np - np.timedelta64(1, "D"), today_np)
    given = pd.to_datetime(out["start_date"].astype("string"), format="%Y%m%d", errors="coerce").to_numpy()
    service_day = np.where(pd.isna(given), inferred, given).astype("datetime64[ns]")

    # Seconds after service-day midnight, comparable to schedule times (can exceed 86400).
    out["service_s"] = secs + ((today_np - service_day) // np.timedelta64(1, "D")).astype("int64") * 86400
    day_codes, day_uniques = pd.factorize(service_day)
    days = [pd.Timestamp(u).date() for u in day_uniques]
    out["service_date"] = pd.Categorical.from_codes(day_codes, categories=pd.Index(days, dtype=object))

    # Is the trip's service actually scheduled to run that day?
    active = np.zeros(n, dtype=bool)
    svc_codes = str_codes["service_id"]
    for vi, v in enumerate(versions):
        for di, d in enumerate(days):
            rows = matched & (version_codes == vi) & (day_codes == di)
            if rows.any():
                ok = str_index["service_id"].get_indexer(sorted(active_services(db, v, d)))
                active[rows] = np.isin(svc_codes[rows], ok[ok >= 0])
    out["service_active"] = active
    return out


MIN_MATCH_RATE = 0.5


def check_match_rate(m: pd.DataFrame, db: sqlite3.Connection) -> None:
    """Stop with diagnostics if most GPS trip IDs don't match the schedule.

    Normally ~97% match. A collapse means something is broken (a library change in how
    IDs are compared, the wrong schedule, a feed format change), and processing on would
    silently write garbage results, so fail loudly instead.
    """
    with_trip = m["trip_id"].notna()
    if not with_trip.any():
        return
    rate = float(m.loc[with_trip, "matched"].mean())
    if rate >= MIN_MATCH_RATE:
        return
    gps_ids = [str(x) for x in m.loc[with_trip, "trip_id"].drop_duplicates().head(5)]
    sched_ids = [r[0] for r in db.execute("SELECT trip_id FROM trips LIMIT 5")]
    raise RuntimeError(
        f"only {100 * rate:.1f}% of GPS rows with a trip ID matched the schedule (expected ~97%).\n"
        f"  GPS trip IDs (sample)     : {gps_ids}  dtype {m['trip_id'].dtype}\n"
        f"  schedule trip IDs (sample): {sched_ids}\n"
        f"  schedule versions used    : {list(m['version'].cat.categories)}\n"
        f"  pandas {pd.__version__}, numpy {np.__version__}"
    )
