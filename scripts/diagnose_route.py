"""Diagnostics behind the route report: wrong-trip logins, timepoints, dwell visibility, timestamp lag.

Usage:
    python scripts/diagnose_route.py data/positions/2026-11-23 data/positions/2026-11-24 \\
        --route 3 --date 2026-11-23
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.arrivals import clean_vehicle_ts, measure_lag, route_day_arrivals
from pipeline.geometry import EARTH_RADIUS_M
from pipeline.match import TZ, check_match_rate, match_trips
from pipeline.positions import load_positions

EARLY_S, LATE_S = -60, 300


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=TZ).strftime("%H:%M") if pd.notna(ts) else "--:--"


def on_time_line(label: str, d: np.ndarray) -> str:
    if not len(d):
        return f"  {label:<28} no data"
    early, late = (d < EARLY_S).mean(), (d > LATE_S).mean()
    return (f"  {label:<28} n={len(d):>6,}  on time {100 * (1 - early - late):5.1f}%"
            f"  early {100 * early:5.1f}%  late {100 * late:5.1f}%  median {np.median(d):+.0f}s")


def step_m(g: pd.DataFrame) -> np.ndarray:
    """Metres moved between consecutive rows of one vehicle."""
    k = np.pi / 180 * EARTH_RADIUS_M
    dy = np.diff(g["lat"].to_numpy()) * k
    dx = np.diff(g["lon"].to_numpy()) * k * np.cos(np.radians(g["lat"].iloc[0]))
    return np.hypot(dx, dy)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("days", nargs="+", type=Path)
    p.add_argument("--route", required=True)
    p.add_argument("--date", required=True, type=date.fromisoformat)
    p.add_argument("--db", type=Path, default=Path("data/schedule.db"))
    a = p.parse_args()

    db = sqlite3.connect(a.db)
    m = match_trips(load_positions(a.days), db)
    check_match_rate(m, db)
    m, _ = clean_vehicle_ts(m)
    sel = m[m["service_active"] & (m["route_short_name"] == a.route) & (m["service_date"] == a.date)]
    arr, _, _ = route_day_arrivals(sel, db, lag_s=measure_lag(m)[0])
    first = arr.groupby("trip_id", observed=True)["stop_sequence"].transform("min") == arr["stop_sequence"]
    arr = arr[~first & arr["actual_ts"].notna()]

    # ---- A. trips whose whole run is far off schedule ------------------------------------
    per_trip = arr.groupby("trip_id", observed=True)["delay_s"].agg(["median", "size"])
    absmed = per_trip["median"].abs() / 60
    print("== A. per-trip median delay ==")
    for lo, hi in [(0, 10), (10, 30), (30, 60), (60, 1e9)]:
        print(f"  {lo:>3}-{'' if hi > 1e8 else hi:<3} min: {((absmed >= lo) & (absmed < hi)).sum():>4} trips")
    odd = per_trip[absmed >= 30].sort_values("median")
    if len(odd):
        print("\n  trips 30+ min off (vehicle, scheduled span, observed span):")
        trips = sel.groupby("trip_id", observed=True).agg(
            vehicle=("vehicle_id", "first"), t0=("feed_ts", "min"), t1=("feed_ts", "max"))
        for trip_id, r in odd.iterrows():
            a_t = arr[arr["trip_id"] == trip_id]
            tr = trips.loc[trip_id]
            print(f"  {trip_id}  median {r['median'] / 60:+6.1f} min  vehicle {tr['vehicle']}  "
                  f"sched {hhmm(a_t['scheduled_ts'].min())}-{hhmm(a_t['scheduled_ts'].max())}  "
                  f"seen {hhmm(tr['t0'])}-{hhmm(tr['t1'])}")
            # What else did that vehicle report around then?
            near = m["feed_ts"].between(tr["t0"] - 3 * 3600, tr["t1"] + 3600)
            v = m[(m["vehicle_id"] == tr["vehicle"]) & near]
            other = v.groupby("trip_id", dropna=False, observed=True).agg(
                t0=("feed_ts", "min"), t1=("feed_ts", "max"), start=("trip_start_s", "first"),
            ).sort_values("t0")
            for oid, o in other.iterrows():
                st = ""
                if pd.notna(o["start"]):
                    s0 = int(o["start"])
                    st = f"sched start {s0 // 3600:02d}:{s0 % 3600 // 60:02d}"
                print(f"      {oid!s:<12} seen {hhmm(o['t0'])}-{hhmm(o['t1'])}  {st}")
    normal = arr[arr["trip_id"].isin(per_trip.index[absmed < 30])]

    # ---- B. timepoints -------------------------------------------------------------------
    print("\n== B. timepoints (trips 30+ min off excluded) ==")
    tp = normal["timepoint"]
    print(f"  timepoint column: 1 = {int((tp == 1).sum()):,}   0 = {int((tp == 0).sum()):,}   "
          f"blank = {int(tp.isna().sum()):,}")
    print(on_time_line("all stops", normal["delay_s"].to_numpy()))
    if (tp == 1).any():
        print(on_time_line("timepoints only", normal.loc[tp == 1, "delay_s"].to_numpy()))
        print(on_time_line("non-timepoints", normal.loc[tp != 1, "delay_s"].to_numpy()))
    # Scheduled times at round minutes usually mark timepoints even when the column is blank.
    round_min = (normal["scheduled_ts"] % 60 == 0)
    print(f"  scheduled times on a whole minute: {100 * round_min.mean():.0f}%")
    # Where along the trip are the early arrivals?
    pos = normal.groupby("trip_id", observed=True)["stop_sequence"].rank(pct=True)
    for lo, hi in [(0, 0.33), (0.33, 0.67), (0.67, 1.01)]:
        sel_part = (pos > lo) & (pos <= hi)
        label = f"trip part {lo:.0%}-{min(hi, 1):.0%}"
        print(on_time_line(label, normal.loc[sel_part, "delay_s"].to_numpy()))

    # ---- C. is a stopped bus visible? ----------------------------------------------------
    print("\n== C. consecutive pings of the same bus ==")
    s = sel.sort_values(["vehicle_id", "feed_ts"])
    with_ts, no_ts = s[s["vehicle_ts"].notna()], s[s["vehicle_ts"].isna()]
    rows = []
    for _, g in with_ts.groupby("vehicle_id", observed=True):
        if len(g) < 2:
            continue
        mv = step_m(g)
        same_ts = (np.diff(g["vehicle_ts"].to_numpy(dtype="float64")) == 0)
        rows.append(pd.DataFrame({"moved": mv, "same_ts": same_ts}))
    if rows:
        c = pd.concat(rows)
        new = c[~c["same_ts"]]
        print(f"  buses WITH own timestamp ({len(c):,} pairs):")
        print(f"    same timestamp repeated (stale)       : {100 * c['same_ts'].mean():.1f}%")
        print(f"    new timestamp, identical position     : "
              f"{100 * (new['moved'] == 0).mean():.1f}% of new fixes")
        print(f"    new timestamp, moved 0-5 m (jitter)   : "
              f"{100 * ((new['moved'] > 0) & (new['moved'] < 5)).mean():.1f}% of new fixes")
    rows = [step_m(g) for _, g in no_ts.groupby("vehicle_id", observed=True) if len(g) >= 2]
    if rows:
        mv = np.concatenate(rows)
        print(f"  buses WITHOUT own timestamp ({len(mv):,} pairs):")
        print(f"    identical position                     : {100 * (mv == 0).mean():.1f}%")
        print(f"    moved 0-5 m                            : {100 * ((mv > 0) & (mv < 5)).mean():.1f}%")

    # ---- D. timestamp lag, first occurrence of each fix only -----------------------------
    print("\n== D. feed time minus vehicle GPS time (first time each fix appears) ==")
    f = with_ts.groupby(["vehicle_id", "vehicle_ts"], observed=True)["feed_ts"].min().reset_index()
    lag = (f["feed_ts"] - f["vehicle_ts"]).astype(float).to_numpy()
    if len(lag):
        print(f"  fixes {len(lag):,}   median {np.median(lag):.0f}s   p10 {np.percentile(lag, 10):.0f}s   "
              f"p90 {np.percentile(lag, 90):.0f}s   negative (clock skew) {100 * (lag < 0).mean():.1f}%")
    db.close()


if __name__ == "__main__":
    main()
