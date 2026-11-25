"""What was the bus doing? A compressed timeline of one trip's GPS pings.

Usage:
    python scripts/trip_timeline.py data/positions/2026-11-23 data/positions/2026-11-24 --trip 75679730

Consecutive pings are grouped: ON ROUTE (with the distance range along the route),
OFF ROUTE (with how far from the line), and silent gaps. The schedule for the trip is
printed underneath so you can see where the bus should have been.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.arrivals import MAX_OFF_M, _shape, _stop_times, stop_distances
from pipeline.match import TZ, match_trips
from pipeline.positions import load_positions

GAP_S = 120


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=TZ).strftime("%H:%M:%S")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("days", nargs="+", type=Path)
    p.add_argument("--trip", required=True)
    p.add_argument("--db", type=Path, default=Path("data/schedule.db"))
    a = p.parse_args()

    db = sqlite3.connect(a.db)
    pos = load_positions(a.days)
    pos = pos[pos["trip_id"] == a.trip]
    if pos.empty:
        sys.exit(f"no GPS rows with trip_id {a.trip}")
    m = match_trips(pos, db).sort_values("feed_ts")
    r0 = m.iloc[0]
    if not r0["matched"]:
        sys.exit(f"trip {a.trip} is not in the schedule")
    line = _shape(db, r0["version"], r0["shape_id"])
    print(f"trip {a.trip}  route {r0['route_short_name']}  service day {r0['service_date']}  "
          f"vehicles {', '.join(m['vehicle_id'].dropna().unique())}  shape length {line.length:,.0f} m")
    print(f"own GPS timestamp on {100 * m['vehicle_ts'].notna().mean():.0f}% of pings\n")

    # Classify each ping.
    rows = []
    prev = None
    for r in m.itertuples():
        t = float(r.vehicle_ts) if pd.notna(r.vehicle_ts) else float(r.feed_ts)
        along, off = line.passes(r.lat, r.lon, MAX_OFF_M)
        if len(along):
            status, val = "ON ROUTE", float(along[np.argmin(off)])
        else:
            _, all_off = line.candidates(r.lat, r.lon)
            status, val = "OFF ROUTE", float(all_off.min())
        standing = prev is not None and (r.lat, r.lon) == prev
        prev = (r.lat, r.lon)
        rows.append((t, status, val, standing))

    # Group consecutive pings with the same status; break groups at silent gaps.
    groups: list[list] = []
    for t, status, val, standing in rows:
        g = groups[-1] if groups else None
        if g and g[0] == status and t - g[2] <= GAP_S:
            g[2] = t
            g[3].append(val)
            g[4] += standing
        else:
            if g and t - g[2] > GAP_S:
                groups.append(["GAP", g[2], t, [], 0])
            groups.append([status, t, t, [val], int(standing)])
    for status, t0, t1, vals, standing in groups:
        span = f"{clock(t0)}-{clock(t1)}"
        if status == "GAP":
            print(f"  {span}  -- no pings for {(t1 - t0) / 60:.0f} min --")
        elif status == "ON ROUTE":
            extra = f"  ({standing} repeated positions)" if standing else ""
            print(f"  {span}  {len(vals):>4} pings  ON ROUTE   "
                  f"{min(vals):>7,.0f} m -> {max(vals):>7,.0f} m{extra}")
        else:
            print(f"  {span}  {len(vals):>4} pings  OFF ROUTE  "
                  f"{min(vals) / 1000:.1f}-{max(vals) / 1000:.1f} km from the line")

    st = _stop_times(db, r0["version"], a.trip)
    sd = stop_distances(st["stop_lat"], st["stop_lon"], line)
    midnight = pd.Timestamp(r0["service_date"]).tz_localize(TZ).timestamp()
    sched = st["arrival_s"].astype("Float64").fillna(st["departure_s"].astype("Float64")).astype(float)
    print("\n  schedule (every 5th stop, plus last):")
    for i in sorted({*range(0, len(st), 5), len(st) - 1}):
        tp = "  timepoint" if st["timepoint"].iloc[i] == 1 else ""
        print(f"    #{int(st['stop_sequence'].iloc[i]):<4} {clock(midnight + sched.iloc[i])}  "
              f"at {sd[i]:>7,.0f} m{tp}")
    db.close()


if __name__ == "__main__":
    main()
