"""Arrival times for one route on one service day, plus the accuracy check.

Usage:
    python scripts/arrivals_report.py data/positions/2026-11-23 data/positions/2026-11-24 \\
        --route 3 --date 2026-11-23

Pass every UTC day folder the service day touches (a Calgary service day runs from about
4 a.m. to 1:30 a.m. the next calendar day, which spans two UTC dates).
Writes data/out/arrivals_route<route>_<date>.csv.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.arrivals import MAX_GAP_S, clean_vehicle_ts, measure_lag, route_day_arrivals
from pipeline.match import check_match_rate, match_trips
from pipeline.positions import load_positions

EARLY_S, LATE_S = -60, 300  # on time = up to 1 min early, up to 5 min late


def q(a: np.ndarray, p: float) -> str:
    return f"{np.percentile(a, p):+.0f}s" if len(a) else "n/a"


def otp(label: str, d: np.ndarray) -> str:
    """One line of on-time stats for delays in seconds."""
    if not len(d):
        return f"  {label:<26} no data"
    early, late = (d < EARLY_S).mean(), (d > LATE_S).mean()
    return (f"  {label:<26} n={len(d):>6,}  on time {100 * (1 - early - late):5.1f}%"
            f"  early {100 * early:5.1f}%  late {100 * late:5.1f}%"
            f"  | p10 {q(d, 10)} median {q(d, 50)} p90 {q(d, 90)}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("days", nargs="+", type=Path)
    p.add_argument("--route", required=True, help="route number as riders know it, e.g. 3")
    p.add_argument("--date", required=True, type=date.fromisoformat, help="service day, YYYY-MM-DD")
    p.add_argument("--db", type=Path, default=Path("data/schedule.db"))
    p.add_argument("--out", type=Path, default=Path("data/out"))
    a = p.parse_args()

    t0 = time.time()
    db = sqlite3.connect(a.db)
    m = match_trips(load_positions(a.days), db)
    check_match_rate(m, db)
    m, _ = clean_vehicle_ts(m)
    sel = m[m["service_active"] & (m["route_short_name"] == a.route) & (m["service_date"] == a.date)]
    if sel.empty:
        day = m[m["service_active"] & (m["service_date"] == a.date)]
        have = sorted(day["route_short_name"].dropna().unique(), key=lambda r: (len(r), r))
        sched = db.execute(
            "SELECT DISTINCT route_short_name, route_long_name, route_type FROM routes "
            "WHERE route_short_name = ?",
            (a.route,)).fetchall()
        msg = f"no matched GPS rows for route {a.route} on {a.date}.\n"
        msg += (f"In the schedule as: {sched}  (route_type 0 = tram/light rail, 3 = bus)\n" if sched
                else "That route number isn't in the schedule at all.\n")
        msg += f"Routes with GPS that day ({len(have)}): {', '.join(have)}"
        sys.exit(msg)
    lag_s, lag_n = measure_lag(m)
    arr, loo, st = route_day_arrivals(sel, db, lag_s=lag_s)
    db.close()
    a.out.mkdir(parents=True, exist_ok=True)
    out_csv = a.out / f"arrivals_route{a.route}_{a.date}.csv"
    arr.to_csv(out_csv, index=False)

    print(f"route {a.route}, service day {a.date} ({a.date:%a}) -- {time.time() - t0:.1f}s")
    print("\n== input ==")
    print(f"trips with GPS     : {st['trips']}   (no shape: {st['trips_no_shape']})")
    print(f"pings              : {st['pings']:,}")
    print(f"  outside trip time window (stale trip ID): {st['pings_outside_window']:,}")
    print(f"  other run of the same trip ID           : {st['pings_other_run']:,} "
          f"({st['trips_split']} trips had a 15+ min silence then reappeared)")
    print(f"  dropped (off route / impossible jump)   : {st['pings_dropped']:,} "
          f"({100 * st['pings_dropped'] / max(st['pings'], 1):.1f}%)")
    print(f"own GPS timestamp  : {100 * sel['vehicle_ts'].notna().mean():.0f}% of pings; others corrected by "
          f"{lag_s:.0f}s (median lag over {lag_n:,} fixes today)")
    print(f"stops not placed on shape: {st['stops_unplaced']}")

    tp = arr["timepoint"] == 1
    dep = arr[tp & arr["departure_ts"].notna()]
    last = arr.groupby("trip_id", observed=True)["stop_sequence"].transform("max") == arr["stop_sequence"]
    print("\n== HEADLINE: departures from timepoints (on time = 1 min early to 5 min late) ==")
    print(f"  timepoint departures measured: {len(dep):,} / {int((tp & ~last).sum()):,}")
    print(otp("all buses", dep["dep_delay_s"].to_numpy()))
    print(otp("buses with own timestamp", dep.loc[dep["own_timestamp"], "dep_delay_s"].to_numpy()))
    print(otp("buses without", dep.loc[~dep["own_timestamp"], "dep_delay_s"].to_numpy()))
    print("  (without own timestamp, a stale repeat looks like standing still: departures may read late;")
    print("   a big gap between those two lines means that bias matters)")
    is_first = arr.groupby("trip_id", observed=True)["stop_sequence"].transform("min") == arr["stop_sequence"]
    first_dep = arr[is_first & arr["departure_ts"].notna()]
    print(otp("first stop of trip", first_dep["dep_delay_s"].to_numpy()))

    # First stop of each trip: the bus waits there, so its ARRIVAL means nothing.
    first = arr.groupby("trip_id", observed=True)["stop_sequence"].transform("min") == arr["stop_sequence"]
    visits = arr[~first]
    est = visits["actual_ts"].notna()
    print("\n== arrivals at every stop (what a rider at any stop sees) ==")
    print(f"  estimated: {est.sum():,} / {len(visits):,} ({100 * est.mean():.1f}%)"
          f"   (no estimate = ping gap > {MAX_GAP_S:.0f}s, or GPS didn't cover that stop)")
    d = visits.loc[est, "delay_s"].to_numpy()
    print(otp("arrivals, all stops", d))

    print("\n== accuracy check (leave-one-out, pings within 15 m of a stop) ==")
    if len(loo):
        ae = np.abs(loo)
        print(f"samples {len(loo):,}   median |error| {np.median(ae):.0f}s   p90 |error| "
              f"{np.percentile(ae, 90):.0f}s   bias {np.mean(loo):+.1f}s")
        print("  (pessimistic: hiding a ping doubles the gap being interpolated)")
    else:
        print("no samples")

    if len(d):
        print("\n== most extreme delays (eyeball these) ==")
        worst = visits.loc[est].reindex(visits.loc[est, "delay_s"].abs().sort_values(ascending=False).index)
        for r in worst.head(5).itertuples():
            print(f"  trip {r.trip_id} stop #{r.stop_sequence} ({r.stop_id}): {r.delay_s / 60:+.1f} min")
    print(f"\nwrote {out_csv}")


if __name__ == "__main__":
    main()
