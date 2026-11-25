"""How much of the GPS data can be tied to the schedule?

Usage:
    python scripts/match_report.py data/positions/2026-11-23 data/positions/2026-11-24 [--db data/schedule.db]

Load the schedule first:  python -m pipeline.schedule data/static/<file>.zip
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.match import match_trips
from pipeline.positions import load_positions
from pipeline.schedule import active_services


def pct(a: int, b: int) -> str:
    return f"{a:,} / {b:,} ({100 * a / b:.1f}%)" if b else f"{a:,} / 0"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("days", nargs="+", type=Path)
    p.add_argument("--db", type=Path, default=Path("data/schedule.db"))
    a = p.parse_args()
    if not a.db.exists():
        sys.exit(f"{a.db} not found. Load the schedule first: "
                 "python -m pipeline.schedule data/static/<file>.zip")

    t0 = time.time()
    pos = load_positions(a.days)
    db = sqlite3.connect(a.db)
    m = match_trips(pos, db)
    print(f"loaded and matched {len(m):,} rows in {time.time() - t0:.1f}s")

    has_trip = m["trip_id"].notna()
    matched = m["matched"]
    print("\n== GPS rows ==")
    print(f"with a trip_id       : {pct(int(has_trip.sum()), len(m))}")
    print(f"trip in schedule     : {pct(int(matched.sum()), int(has_trip.sum()))}   (of rows with a trip_id)")
    print(f"service runs that day: {pct(int(m['service_active'].sum()), int(matched.sum()))}   (of matched)")
    print(f"feed gave start_date : {pct(int(m['start_date'].notna().sum()), len(m))}")

    trips_seen = m.loc[has_trip, "trip_id"].nunique()
    trips_matched = m.loc[matched, "trip_id"].nunique()
    print(f"\ndistinct trip IDs    : {pct(trips_matched, trips_seen)} matched")

    unmatched = m.loc[has_trip & ~matched, "trip_id"].value_counts().head(10)
    if len(unmatched):
        print("\n== most common unmatched trip IDs ==")
        for trip_id, n in unmatched.items():
            print(f"  {trip_id:<30} {n:>7,} rows")

    inactive = m.loc[matched & ~m["service_active"]]
    if len(inactive):
        print("\n== matched, but service not scheduled that day (service_id: rows) ==")
        for sid, n in inactive["service_id"].value_counts().head(5).items():
            print(f"  {sid:<30} {n:>7,}")

    print("\n== per service day: scheduled trips that appeared in GPS ==")
    print("  (only days fully inside the files are meaningful; edge days are partial)")
    ok = m[m["service_active"]]
    for (version, day), g in ok.groupby(["version", "service_date"]):
        services = active_services(db, version, day)
        sched = db.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT route_id) FROM trips WHERE version = ? "
            f"AND service_id IN ({','.join('?' * len(services))})",
            (version, *services),
        ).fetchone()
        print(f"  {day} ({day:%a}): trips {pct(g['trip_id'].nunique(), sched[0])}"
              f"   routes {pct(g['route_id'].nunique(), sched[1])}")
    db.close()


if __name__ == "__main__":
    main()
