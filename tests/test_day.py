import csv
import gzip
import json
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest
from test_schedule import GTFS, make_zip

from pipeline.day import hour_files, process_day
from pipeline.match import TZ, check_match_rate, match_trips
from pipeline.positions import COLUMNS, load_files
from pipeline.results import open_results, write_events, write_summaries
from pipeline.schedule import load_schedule, open_db


def write_positions(root, rows):
    """rows: (feed_ts, vehicle_id, trip_id, lat, lon) -> hourly files like the collector's."""
    by_file = {}
    for r in rows:
        t = datetime.fromtimestamp(r[0], tz=timezone.utc)
        by_file.setdefault(root / t.strftime("%Y-%m-%d") / f"{t:%H}.csv.gz", []).append(r)
    for path, rs in by_file.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for ts, veh, trip, lat, lon in rs:
                w.writerow([ts, "", veh, trip, "", "", f"{lat:.6f}", f"{lon:.6f}", "", ""])


def test_hour_files_cover_0200_to_0500_next_day(tmp_path):
    # Monday Nov 23 2026. Alberta is on permanent UTC-6 from Nov 1 2026: 02:00 local = 08:00 UTC.
    for d, hours in [("2026-11-23", range(8, 24)), ("2026-11-24", range(0, 11))]:
        (tmp_path / d).mkdir(parents=True)
        for h in hours:
            (tmp_path / d / f"{h:02d}.csv.gz").touch()
    files, missing = hour_files(tmp_path, date(2026, 11, 23))
    assert missing == 0 and len(files) == 27
    assert files[0].parts[-2:] == ("2026-11-23", "08.csv.gz")
    assert files[-1].parts[-2:] == ("2026-11-24", "10.csv.gz")
    (tmp_path / "2026-11-23" / "15.csv.gz").unlink()
    assert hour_files(tmp_path, date(2026, 11, 23))[1] == 1


# Calgary's real trip IDs are all digits ("75679730"). Libraries can treat those as numbers
# and stop matching the schedule's text IDs, so the end-to-end test runs with both styles.
@pytest.mark.parametrize("trip", ["T1", "75679730"])
def test_process_day_end_to_end_and_rerun_replaces(tmp_path, trip):
    sched = open_db(tmp_path / "schedule.db")
    gtfs = {k: v.replace("T1", trip) for k, v in GTFS.items()}
    load_schedule(make_zip(tmp_path / "2026-11-19_abcd1234.zip", gtfs), sched)
    # Route 3, Monday: leaves A 23:58, reaches B 01:33 Tuesday (scheduled 01:30): 3 min late.
    start = int(datetime(2026, 11, 23, 23, 58, tzinfo=TZ).timestamp())
    end = int(datetime(2026, 11, 24, 1, 33, tzinfo=TZ).timestamp())
    t = np.arange(start, end + 1, 30)
    lat = 51.0 + 0.01 * (t - start) / (end - start)
    rows = [(ts, "7927", trip, la, -114.0) for ts, la in zip(t, lat, strict=True)]
    write_positions(tmp_path / "positions", rows)

    result = process_day(tmp_path / "positions", sched, date(2026, 11, 23))
    assert result.info["rows_used"] == len(t)
    r = result.routes.set_index("route").loc["3"]
    assert r["trips_scheduled"] == 1 and r["trips_with_gps"] == 1
    assert r["arrivals"] == 1 and r["arr_on_time"] == 1      # +3 min is on time

    res = open_results(tmp_path / "results.db")
    for _ in range(2):  # running the same day twice must not duplicate anything
        path = write_events(result, tmp_path / "events")
        write_summaries(result, res, "2026-11-24T03:00:00")
    assert res.execute("SELECT COUNT(*) FROM route_days").fetchone()[0] == 1
    assert res.execute("SELECT COUNT(*) FROM days").fetchone()[0] == 1
    assert json.loads(res.execute("SELECT info FROM days").fetchone()[0])["rows_used"] == len(t)
    ev = pd.read_csv(path)
    assert len(ev) == 2 and ev.loc[ev["stop_sequence"] == 2, "actual_ts"].iloc[0] - \
        ev.loc[ev["stop_sequence"] == 2, "scheduled_ts"].iloc[0] == 180 - 15  # default lag correction


def test_day_without_gps_files(tmp_path):
    sched = open_db(tmp_path / "schedule.db")
    load_schedule(make_zip(tmp_path / "2026-11-19_abcd1234.zip"), sched)
    result = process_day(tmp_path / "positions", sched, date(2026, 11, 23))
    assert result.routes.empty and result.info["hours_missing"] == 27


def test_match_rate_collapse_stops_with_diagnostics(tmp_path):
    sched = open_db(tmp_path / "schedule.db")
    load_schedule(make_zip(tmp_path / "2026-11-19_abcd1234.zip"), sched)
    ts = int(datetime(2026, 11, 23, 9, 0, tzinfo=TZ).timestamp())
    write_positions(tmp_path / "positions", [(ts + 30 * i, "7927", "NOT_IN_SCHEDULE", 51.0, -114.0)
                                             for i in range(10)])
    m = match_trips(load_files(sorted((tmp_path / "positions").rglob("*.csv.gz"))), sched)
    with pytest.raises(RuntimeError, match="matched the schedule"):
        check_match_rate(m, sched)
