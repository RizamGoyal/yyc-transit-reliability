from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from test_schedule import make_zip

from pipeline.arrivals import route_day_arrivals
from pipeline.match import TZ, match_trips
from pipeline.schedule import load_schedule, open_db


def fake_trip(t, frac):
    return pd.DataFrame({
        "feed_ts": t, "vehicle_ts": pd.array([pd.NA] * len(t), dtype="Int64"),
        "vehicle_id": "v1", "trip_id": pd.array(["T1"] * len(t), dtype="string"),
        "route_id": pd.NA, "start_date": pd.array([pd.NA] * len(t), dtype="string"),
        "lat": 51.0 + 0.01 * frac, "lon": -114.0,
    })


def late_bus(tmp_path, extra=None):
    db = open_db(tmp_path / "schedule.db")
    load_schedule(make_zip(tmp_path / "2026-11-19_abcd1234.zip"), db)
    # Fake schedule: T1 (Monday service) leaves stop A (51.00) at 23:58, reaches stop B (51.01)
    # at 25:30 = 01:30 Tuesday. Our bus leaves at 23:58 and reaches B at 01:33 (3 min late).
    start = int(datetime(2026, 11, 23, 23, 58, tzinfo=TZ).timestamp())
    end = int(datetime(2026, 11, 24, 1, 33, tzinfo=TZ).timestamp())
    t = np.arange(start, end + 1, 30)
    pos = fake_trip(t, (t - start) / (end - start))
    if extra is not None:
        pos = pd.concat([pos, extra], ignore_index=True)
    return match_trips(pos, db), db


def test_late_bus_shows_positive_delay(tmp_path):
    m, db = late_bus(tmp_path)
    assert m["service_active"].all()
    arrivals, loo, stats = route_day_arrivals(m[m["service_active"]], db)
    assert stats["trips"] == 1 and stats["pings_dropped"] == 0 and stats["stops_unplaced"] == 0
    a = arrivals.set_index("stop_sequence")
    assert np.isnan(a.loc[1, "actual_ts"])            # data starts at the first stop: no arrival
    assert a.loc[2, "delay_s"] == pytest.approx(180, abs=1)
    assert a.loc[2, "route_short_name"] == "3"
    assert len(loo) > 0 and np.allclose(loo, 0, atol=0.5)  # constant speed: interpolation is exact


def test_lag_correction_shifts_feed_times(tmp_path):
    m, db = late_bus(tmp_path)
    arrivals, _, _ = route_day_arrivals(m, db, lag_s=20)
    assert arrivals.set_index("stop_sequence").loc[2, "delay_s"] == pytest.approx(160, abs=1)


def test_same_trip_id_hours_later_is_ignored(tmp_path):
    # The bus reports T1 again at 03:45-04:00 on a later run, part-way along the route.
    s = int(datetime(2026, 11, 24, 3, 45, tzinfo=TZ).timestamp())
    t = np.arange(s, s + 900, 30)
    m, db = late_bus(tmp_path, extra=fake_trip(t, np.linspace(0.3, 0.9, len(t))))
    arrivals, _, stats = route_day_arrivals(m, db)
    assert stats["pings_outside_window"] == len(t)
    assert arrivals.set_index("stop_sequence").loc[2, "delay_s"] == pytest.approx(180, abs=1)


def test_departure_from_first_stop(tmp_path):
    m, db = late_bus(tmp_path)
    arrivals, _, _ = route_day_arrivals(m, db)
    first = arrivals.set_index("stop_sequence").loc[1]
    # Left stop A at 23:58:00 (scheduled 23:58:30); "departed" = 30 m out, ~0.2 m/s in this slow fake.
    assert first["dep_delay_s"] == pytest.approx(-30 + 30 / (1112 / (95 * 60)), abs=5)


def partial_then_resumed(tmp_path, gap_lon):
    """Real run: leaves A at 23:58 and gets halfway by 00:45. Then the bus stops reporting
    on the route (silent if gap_lon is None, else 7 km away at gap_lon) until 01:50, and
    resumes from 55% to the end (stop B) by 02:05, still tagged T1. Stop B is scheduled
    at 01:30, so stitching the runs together would invent a ~35 min late arrival."""
    s0 = int(datetime(2026, 11, 23, 23, 58, tzinfo=TZ).timestamp())
    s1 = int(datetime(2026, 11, 24, 0, 45, tzinfo=TZ).timestamp())
    s2 = int(datetime(2026, 11, 24, 1, 50, tzinfo=TZ).timestamp())
    s3 = int(datetime(2026, 11, 24, 2, 5, tzinfo=TZ).timestamp())
    t1 = np.arange(s0, s1 + 1, 30)
    parts = [fake_trip(t1, 0.5 * (t1 - s0) / (s1 - s0))]
    if gap_lon is not None:
        t_off = np.arange(s1 + 30, s2, 30)
        off = fake_trip(t_off, np.full(len(t_off), 0.5))
        off["lon"] = gap_lon
        parts.append(off)
    t3 = np.arange(s2, s3 + 1, 30)
    parts.append(fake_trip(t3, 0.55 + 0.45 * (t3 - s2) / (s3 - s2)))
    db = open_db(tmp_path / "schedule.db")
    load_schedule(make_zip(tmp_path / "2026-11-19_abcd1234.zip"), db)
    m = match_trips(pd.concat(parts, ignore_index=True), db)
    return route_day_arrivals(m, db)


@pytest.mark.parametrize("gap_lon", [None, -113.9], ids=["silent", "off-route"])
def test_resumed_run_is_not_stitched_onto_trip(tmp_path, gap_lon):
    arrivals, _, stats = partial_then_resumed(tmp_path, gap_lon)
    assert stats["trips_split"] == 1
    assert np.isnan(arrivals.set_index("stop_sequence").loc[2, "actual_ts"])  # no fake late arrival


def test_long_dwell_at_a_stop_does_not_split_the_trip(tmp_path):
    # Bus stands still mid-route for 20 min (same coordinates every ping): one run, not two.
    s0 = int(datetime(2026, 11, 23, 23, 58, tzinfo=TZ).timestamp())
    t = np.arange(s0, s0 + 95 * 60 + 1, 30)
    frac = np.clip((t - s0) / (75 * 60), 0, 1)
    frac = np.where((t - s0) < 30 * 60, (t - s0) / (75 * 60), np.where((t - s0) < 50 * 60, 30 / 75,
                                                                       (t - s0 - 20 * 60) / (75 * 60)))
    db = open_db(tmp_path / "schedule.db")
    load_schedule(make_zip(tmp_path / "2026-11-19_abcd1234.zip"), db)
    arrivals, _, stats = route_day_arrivals(match_trips(fake_trip(t, frac), db), db)
    assert stats["trips_split"] == 0
    assert arrivals["actual_ts"].notna().sum() == 1
