"""Load a GTFS schedule zip into SQLite, one version per zip.

Usage:
    python -m pipeline.schedule data/static/2026-11-19_fc11f078.zip [--db data/schedule.db]

Design notes
- Every table has a `version` column (first 8 hex chars of the zip's SHA-256, the same
  hash the collector puts in the file name). The City replaces the schedule a few times a
  year; old GPS data must be judged against the schedule that was in effect *then*.
- Loading is idempotent: a version that's already loaded is skipped.
- Times like "25:30:00" are normal in GTFS: a trip that runs past midnight still belongs to
  the previous service day. They're stored as seconds after midnight of the service day
  (25:30:00 -> 91800), never wrapped.
- IDs are kept as text (leading zeros matter).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import sqlite3
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# GTFS file -> (columns we keep, required columns). Optional columns are filled with NULL.
FILES: dict[str, tuple[list[str], set[str]]] = {
    "routes.txt": (["route_id", "route_short_name", "route_long_name", "route_type"], {"route_id"}),
    "trips.txt": (
        ["trip_id", "route_id", "service_id", "direction_id", "shape_id", "trip_headsign", "block_id"],
        {"trip_id", "route_id", "service_id"},
    ),
    "stops.txt": (
        ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"],
        {"stop_id", "stop_lat", "stop_lon"},
    ),
    "stop_times.txt": (
        ["trip_id", "stop_sequence", "stop_id", "arrival_time", "departure_time", "shape_dist_traveled",
         "timepoint"],
        {"trip_id", "stop_sequence", "stop_id"},
    ),
    "shapes.txt": (
        ["shape_id", "shape_pt_sequence", "shape_pt_lat", "shape_pt_lon", "shape_dist_traveled"],
        {"shape_id", "shape_pt_sequence", "shape_pt_lat", "shape_pt_lon"},
    ),
    "calendar.txt": (
        ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
         "start_date", "end_date"],
        set(),
    ),
    "calendar_dates.txt": (["service_id", "date", "exception_type"], set()),
}

NUMERIC = {
    "route_type": "Int64", "direction_id": "Int64", "stop_sequence": "Int64", "shape_pt_sequence": "Int64",
    "timepoint": "Int64", "exception_type": "Int64",
    "monday": "Int64", "tuesday": "Int64", "wednesday": "Int64", "thursday": "Int64", "friday": "Int64",
    "saturday": "Int64", "sunday": "Int64",
    "stop_lat": "float64", "stop_lon": "float64", "shape_pt_lat": "float64", "shape_pt_lon": "float64",
    "shape_dist_traveled": "float64",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_versions (
    version     TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    first_seen  TEXT NOT NULL,      -- YYYY-MM-DD the collector first downloaded it
    loaded_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routes (
    version TEXT, route_id TEXT, route_short_name TEXT, route_long_name TEXT, route_type INTEGER,
    PRIMARY KEY (version, route_id));
CREATE TABLE IF NOT EXISTS trips (
    version TEXT, trip_id TEXT, route_id TEXT, service_id TEXT, direction_id INTEGER, shape_id TEXT,
    trip_headsign TEXT, block_id TEXT,
    PRIMARY KEY (version, trip_id));
CREATE TABLE IF NOT EXISTS stops (
    version TEXT, stop_id TEXT, stop_code TEXT, stop_name TEXT, stop_lat REAL, stop_lon REAL,
    PRIMARY KEY (version, stop_id));
CREATE TABLE IF NOT EXISTS stop_times (
    version TEXT, trip_id TEXT, stop_sequence INTEGER, stop_id TEXT,
    arrival_s INTEGER, departure_s INTEGER,   -- seconds after service-day midnight; may exceed 86400
    shape_dist_traveled REAL, timepoint INTEGER,
    PRIMARY KEY (version, trip_id, stop_sequence));
CREATE TABLE IF NOT EXISTS shapes (
    version TEXT, shape_id TEXT, shape_pt_sequence INTEGER, shape_pt_lat REAL, shape_pt_lon REAL,
    shape_dist_traveled REAL,
    PRIMARY KEY (version, shape_id, shape_pt_sequence));
CREATE TABLE IF NOT EXISTS calendar (
    version TEXT, service_id TEXT, monday INTEGER, tuesday INTEGER, wednesday INTEGER, thursday INTEGER,
    friday INTEGER, saturday INTEGER, sunday INTEGER, start_date TEXT, end_date TEXT,
    PRIMARY KEY (version, service_id));
CREATE TABLE IF NOT EXISTS calendar_dates (
    version TEXT, service_id TEXT, date TEXT, exception_type INTEGER,
    PRIMARY KEY (version, service_id, date));
"""

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_gtfs_time(s: pd.Series) -> pd.Series:
    """'25:30:00' -> 91800. Blank -> <NA>. Hours above 23 are kept (service-day time)."""
    parts = s.astype("string").str.strip().str.extract(r"^(\d+):(\d{2}):(\d{2})$")
    h, m, sec = (pd.to_numeric(parts[i]) for i in range(3))
    return (h * 3600 + m * 60 + sec).astype("Int64")


def file_version(zip_path: Path) -> str:
    return hashlib.sha256(zip_path.read_bytes()).hexdigest()[:8]


def first_seen_from_name(zip_path: Path) -> str:
    """The collector names zips '<YYYY-MM-DD>_<hash>.zip'. Fall back to the file's mtime."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})_", zip_path.name)
    if m:
        return m.group(1)
    return datetime.fromtimestamp(zip_path.stat().st_mtime).strftime("%Y-%m-%d")


def read_gtfs_file(z: zipfile.ZipFile, name: str) -> pd.DataFrame | None:
    keep, required = FILES[name]
    if name not in z.namelist():
        if required:
            raise ValueError(f"{name} missing from schedule zip")
        return None
    with z.open(name) as fh:
        df = pd.read_csv(io.TextIOWrapper(fh, encoding="utf-8-sig"), dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{name} is missing required columns: {sorted(missing)}")
    df = df.reindex(columns=keep)  # optional columns that don't exist become NaN
    for col in keep:
        df[col] = df[col].astype("string").str.strip().replace("", pd.NA)
        if col in NUMERIC:
            df[col] = pd.to_numeric(df[col]).astype(NUMERIC[col])
    return df


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def _insert(db: sqlite3.Connection, table: str, df: pd.DataFrame) -> None:
    cols = list(df.columns)
    rows = df.astype(object).where(df.notna(), None).itertuples(index=False, name=None)
    db.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)


def load_schedule(zip_path: Path, db: sqlite3.Connection) -> tuple[str, bool]:
    """Load one zip. Returns (version, loaded_now). Already-loaded versions are skipped."""
    version = file_version(zip_path)
    if db.execute("SELECT 1 FROM schedule_versions WHERE version = ?", (version,)).fetchone():
        return version, False

    with zipfile.ZipFile(zip_path) as z:
        frames = {name: read_gtfs_file(z, name) for name in FILES}
    if frames["calendar.txt"] is None and frames["calendar_dates.txt"] is None:
        raise ValueError("schedule has neither calendar.txt nor calendar_dates.txt")

    st = frames["stop_times.txt"]
    st["arrival_s"] = parse_gtfs_time(st["arrival_time"])
    st["departure_s"] = parse_gtfs_time(st["departure_time"])
    frames["stop_times.txt"] = st.drop(columns=["arrival_time", "departure_time"])

    with db:  # one transaction: a half-loaded version can never exist
        for name, df in frames.items():
            if df is None:
                continue
            df.insert(0, "version", version)
            _insert(db, name.removesuffix(".txt"), df)
        db.execute(
            "INSERT INTO schedule_versions (version, source, first_seen, loaded_at) VALUES (?, ?, ?, ?)",
            (version, zip_path.name, first_seen_from_name(zip_path),
             datetime.now().isoformat(timespec="seconds")),
        )
    return version, True


def version_for(db: sqlite3.Connection, service_date: date) -> str | None:
    """Schedule version in effect on a date: the newest one first seen on or before it.
    If the date is before every version we have, use the oldest (best available)."""
    d = service_date.isoformat()
    row = db.execute(
        "SELECT version FROM schedule_versions WHERE first_seen <= ? ORDER BY first_seen DESC LIMIT 1", (d,)
    ).fetchone() or db.execute("SELECT version FROM schedule_versions ORDER BY first_seen LIMIT 1").fetchone()
    return row[0] if row else None


def active_services(db: sqlite3.Connection, version: str, service_date: date) -> set[str]:
    """service_ids running on a date: calendar.txt weekday rules, then calendar_dates.txt
    exceptions (1 = added that day, 2 = removed that day)."""
    ymd = service_date.strftime("%Y%m%d")
    weekday = WEEKDAYS[service_date.weekday()]
    active = {
        r[0] for r in db.execute(
            f"SELECT service_id FROM calendar WHERE version = ? AND {weekday} = 1 "
            "AND start_date <= ? AND end_date >= ?",
            (version, ymd, ymd),
        )
    }
    for service_id, exc in db.execute(
        "SELECT service_id, exception_type FROM calendar_dates WHERE version = ? AND date = ?", (version, ymd)
    ):
        if exc == 1:
            active.add(service_id)
        elif exc == 2:
            active.discard(service_id)
    return active


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("zips", nargs="+", type=Path)
    p.add_argument("--db", type=Path, default=Path("data/schedule.db"))
    a = p.parse_args()
    for zp in a.zips:
        if not zp.is_file():
            raise SystemExit(f"schedule zip not found: {zp}")
    db = open_db(a.db)
    for zp in a.zips:
        t0 = time.time()
        version, loaded = load_schedule(zp, db)
        if not loaded:
            print(f"{zp.name}: version {version} already loaded, skipped")
            continue
        print(f"{zp.name}: loaded version {version} in {time.time() - t0:.1f}s")
        for table in ["routes", "trips", "stops", "stop_times", "shapes", "calendar", "calendar_dates"]:
            n = db.execute(f"SELECT COUNT(*) FROM {table} WHERE version = ?", (version,)).fetchone()[0]
            print(f"  {table:<15} {n:>10,}")
    db.close()


if __name__ == "__main__":
    main()
