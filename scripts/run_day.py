"""Process one service day for every route and save the results.

Usage:
    python scripts/run_day.py                    # yesterday (Calgary time)
    python scripts/run_day.py --date 2026-11-23  # a specific service day

Steps: load any new schedule zips from data/static into data/schedule.db, process the
day, write data/results/stop_events/<date>.csv.gz and the summaries in data/results.db.
Re-running a day replaces its results.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.day import process_day
from pipeline.match import TZ
from pipeline.results import open_results, write_events, write_summaries
from pipeline.schedule import load_schedule
from pipeline.schedule import open_db as open_schedule


def peak_mb() -> str:
    try:
        import resource  # not available on Windows
    except ImportError:
        return "n/a"
    return f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def pct(a, b) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def main() -> None:
    data = Path(os.environ.get("DATA_DIR", "data"))
    p = argparse.ArgumentParser()
    yesterday = datetime.now(TZ).date() - timedelta(days=1)
    p.add_argument("--date", type=date.fromisoformat, default=yesterday,
                   help="service day (default: yesterday)")
    a = p.parse_args()
    t0 = time.time()

    sched = open_schedule(data / "schedule.db")
    for zp in sorted((data / "static").glob("*.zip")):
        version, loaded = load_schedule(zp, sched)
        if loaded:
            print(f"loaded new schedule version {version} from {zp.name}")

    result = process_day(data / "positions", sched, a.date)
    sched.close()
    info = result.info
    if result.routes.empty:
        sys.exit(f"{a.date}: no GPS files for this service day ({info['hours_missing']} hours missing)")

    ev_path = write_events(result, data / "results" / "stop_events")
    res = open_results(data / "results.db")
    write_summaries(result, res, datetime.now(TZ).isoformat(timespec="seconds"))
    res.close()

    r = result.routes.fillna(0)
    with_gps = r[r["trips_with_gps"] > 0]
    print(f"service day {a.date} ({a.date:%a}) processed in {time.time() - t0:.0f}s, peak memory {peak_mb()}")
    print(f"  GPS hour files      : {info['hour_files']} ({info['hours_missing']} missing)")
    print(f"  GPS rows            : {info['rows']:,}, matched to schedule "
          f"{pct(info['rows_matched'], info['rows'])}, used for this day {info['rows_used']:,}")
    print(f"  timestamp lag       : {info['lag_s']:.0f}s (from {info['lag_fixes']:,} fixes)")
    n_sched = int((r["trips_scheduled"] > 0).sum())
    print(f"  routes              : {len(with_gps)} with GPS / {n_sched} scheduled")
    print(f"  trips               : {int(r['trips_with_gps'].sum()):,} with GPS / "
          f"{int(r['trips_scheduled'].sum()):,} scheduled")
    print(f"  pings dropped       : {pct(r['pings_dropped'].sum(), r['pings'].sum())} off route/impossible, "
          f"{int(r['trips_split'].sum())} trips split")
    print(f"  arrivals estimated  : {pct(r['arrivals'].sum(), r['arrivals_possible'].sum())}")
    n = r["tp_departures"].sum()
    print(f"  timepoint departures: {int(n):,} measured of {int(r['tp_possible'].sum()):,}")
    print(f"  ON TIME (departures from timepoints, -1/+5 min): {pct(r['tp_on_time'].sum(), n)}   "
          f"early {pct(r['tp_early'].sum(), n)}   late {pct(r['tp_late'].sum(), n)}")
    loo = with_gps[with_gps["loo_n"] > 0]
    if len(loo):
        print(f"  accuracy check      : median |error| {loo['loo_abs_p50'].median():.0f}s across routes "
              f"(p90 {loo['loo_abs_p90'].median():.0f}s)")
    big = with_gps[with_gps["tp_departures"] >= 30].assign(otp=lambda x: x["tp_on_time"] / x["tp_departures"])
    if len(big):
        print("  lowest on-time routes (30+ timepoint departures):")
        for row in big.nsmallest(5, "otp").itertuples():
            print(f"    {row.route:<5} {100 * row.otp:5.1f}%  ({int(row.tp_departures)} departures)")
    print(f"wrote {ev_path} and data/results.db")


if __name__ == "__main__":
    main()
