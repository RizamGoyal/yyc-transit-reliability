"""Where processed results live.

- data/results/stop_events/YYYY-MM-DD.csv.gz: one row per trip stop per day (~450k rows,
  a few MB compressed). Kept as files, not database rows: ~2 GB/year instead of ~10 GB,
  easy to back up, and any day can be recomputed from raw GPS and replaced.
- data/results.db: small per-day and per-route summaries the website and reports read.

Writing a day replaces that day's results, so re-running is always safe.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pipeline.day import DayResult

EVENT_COLUMNS = [
    "service_date", "route_short_name", "trip_id", "direction_id", "own_timestamp", "stop_sequence",
    "stop_id", "timepoint", "scheduled_ts", "actual_ts", "scheduled_dep_ts", "departure_ts",
]
TIME_COLUMNS = ["scheduled_ts", "actual_ts", "scheduled_dep_ts", "departure_ts"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    service_date TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    info         TEXT NOT NULL          -- JSON counters for the whole day
);
CREATE TABLE IF NOT EXISTS route_days (
    service_date TEXT NOT NULL,
    route        TEXT NOT NULL,
    trips_scheduled INTEGER, trips_with_gps INTEGER,
    pings INTEGER, pings_outside_window INTEGER, pings_other_run INTEGER, trips_split INTEGER,
    pings_dropped INTEGER, stops_unplaced INTEGER, trips_no_shape INTEGER,
    arrivals_possible INTEGER, arrivals INTEGER, arr_early INTEGER, arr_on_time INTEGER, arr_late INTEGER,
    tp_possible INTEGER, tp_departures INTEGER, tp_early INTEGER, tp_on_time INTEGER, tp_late INTEGER,
    tp_delay_p50 REAL, tp_delay_p90 REAL,
    loo_n INTEGER, loo_abs_p50 REAL, loo_abs_p90 REAL,
    PRIMARY KEY (service_date, route)
);
"""


def open_results(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def write_events(result: DayResult, out_dir: Path) -> Path | None:
    if result.events.empty:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    ev = result.events[EVENT_COLUMNS].copy()
    for c in TIME_COLUMNS:
        ev[c] = ev[c].round().astype("Int64")  # whole seconds: smaller files, no false precision
    path = out_dir / f"{result.info['service_date']}.csv.gz"
    tmp = path.with_suffix(".tmp")
    ev.to_csv(tmp, index=False, compression="gzip")
    tmp.replace(path)  # never leave a half-written file in place
    return path


def write_summaries(result: DayResult, db: sqlite3.Connection, processed_at: str) -> None:
    d = result.info["service_date"]
    cols = [r[1] for r in db.execute("PRAGMA table_info(route_days)")]
    rt = result.routes.reindex(columns=cols)
    rows = rt.astype(object).where(rt.notna(), None).itertuples(index=False, name=None)
    with db:  # one transaction: a day is either fully replaced or untouched
        db.execute("DELETE FROM route_days WHERE service_date = ?", (d,))
        db.execute("DELETE FROM days WHERE service_date = ?", (d,))
        sql = f"INSERT INTO route_days ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        db.executemany(sql, rows)
        db.execute("INSERT INTO days (service_date, processed_at, info) VALUES (?, ?, ?)",
                   (d, processed_at, json.dumps(result.info)))
