"""Sanity-check GPS days and a schedule zip copied to the laptop.

Usage:
    python scripts/inspect_sample.py data/positions/2026-11-23 data/positions/2026-11-24 \
        --static data/static/<file>.zip

For each day: hourly files present, rows, vehicles, trips, routes, time range, and the
longest gap between new feed snapshots. For the schedule: row count of each file.
All times are UTC (that's how the collector stores them).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import itertools
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def inspect_day(day_dir: Path) -> None:
    if not day_dir.is_dir():
        raise SystemExit(f"folder not found: {day_dir}")
    files = sorted(day_dir.glob("*.csv.gz"))
    hours = {f.name[:2] for f in files}
    missing = sorted({f"{h:02d}" for h in range(24)} - hours)
    rows = 0
    vehicles: set[str] = set()
    trips: set[str] = set()
    routes: set[str] = set()
    snapshots: set[int] = set()
    for f in files:
        with gzip.open(f, "rt", newline="") as fh:
            for r in csv.DictReader(fh):
                if r["feed_ts"] == "feed_ts":  # header inside an appended gzip member
                    continue
                rows += 1
                snapshots.add(int(r["feed_ts"]))
                vehicles.add(r["vehicle_id"])
                if r["trip_id"]:
                    trips.add(r["trip_id"])
                if r["route_id"]:
                    routes.add(r["route_id"])
    print(f"\n== {day_dir.name} ==")
    print(f"hourly files : {len(files)}/24" + (f"  missing hours: {', '.join(missing)}" if missing else ""))
    print(f"rows         : {rows:,}")
    print(f"snapshots    : {len(snapshots):,}")
    print(f"vehicles     : {len(vehicles):,}   trips: {len(trips):,}   routes: {len(routes):,}")
    if snapshots:
        s = sorted(snapshots)
        gap, at = max(((b - a, a) for a, b in itertools.pairwise(s)), default=(0, s[0]))
        print(f"time range   : {utc(s[0])} -> {utc(s[-1])} UTC")
        print(f"longest gap  : {gap}s (starting {utc(at)} UTC)")


def inspect_static(zip_path: Path) -> None:
    print(f"\n== schedule: {zip_path.name} ({zip_path.stat().st_size / 1e6:.1f} MB) ==")
    with zipfile.ZipFile(zip_path) as z:
        for name in sorted(z.namelist()):
            with z.open(name) as fh:
                n = sum(1 for _ in io.TextIOWrapper(fh, encoding="utf-8-sig")) - 1
            print(f"{name:<22} {n:>10,} rows")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("days", nargs="+", type=Path, help="positions/YYYY-MM-DD folders")
    p.add_argument("--static", type=Path, help="schedule zip")
    a = p.parse_args()
    for d in a.days:
        inspect_day(d)
    if a.static:
        inspect_static(a.static)


if __name__ == "__main__":
    main()
