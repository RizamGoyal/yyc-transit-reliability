import numpy as np
import pandas as pd
import pytest

from pipeline.arrivals import (
    MAX_GAP_S,
    Track,
    build_track,
    clean_vehicle_ts,
    crossing_times,
    leave_one_out,
    stop_distances,
)
from pipeline.geometry import EARTH_RADIUS_M, Polyline

LAT0, LON0 = 51.05, -114.07
K = np.pi / 180 * EARTH_RADIUS_M


def latlon(x_m, y_m):
    """Metres east/north of a Calgary origin -> (lat, lon)."""
    x_m, y_m = np.asarray(x_m, float), np.asarray(y_m, float)
    return LAT0 + y_m / K, LON0 + x_m / (K * np.cos(np.radians(LAT0)))


def line_north(length=2000.0, step=100.0):
    ys = np.arange(0, length + step, step)
    lat, lon = latlon(np.zeros_like(ys), ys)
    return Polyline.from_latlon(lat, lon)


def out_and_back():
    """2 km north, then 2 km back south on a parallel street 10 m east (a shared corridor)."""
    ys = np.arange(0, 2001, 100.0)
    x = np.concatenate([np.zeros_like(ys), np.full_like(ys, 10.0)])
    y = np.concatenate([ys, ys[::-1]])
    lat, lon = latlon(x, y)
    return Polyline.from_latlon(lat, lon)


def pings(ts, xs, ys):
    lat, lon = latlon(xs, ys)
    return pd.DataFrame({"t": np.asarray(ts, float), "lat": lat, "lon": lon})


def test_polyline_length_is_metres():
    assert line_north(2000).length == pytest.approx(2000, rel=1e-3)


def test_snaps_point_mid_segment_not_previous_vertex():
    line = line_north()
    lat, lon = latlon([5.0], [105.0])
    along, off = line.locate(lat[0], lon[0], after=0, max_off=50)
    assert along == pytest.approx(105, abs=0.5) and off == pytest.approx(5, abs=0.5)


def test_constant_speed_arrival_is_exact():
    line = line_north()
    # 10 m/s, a ping every 30 s.
    ts = np.arange(0, 210, 30)
    tr = build_track(pings(ts, np.zeros(len(ts)), ts * 10.0), line)
    arr = crossing_times(tr, np.array([450.0, 1000.0]))
    assert arr == pytest.approx([45.0, 100.0], abs=0.5)


def test_out_and_back_does_not_jump_to_return_leg():
    line = out_and_back()
    ts = np.arange(0, 400, 30)
    pos = ts * 10.0  # metres along the route
    x = np.where(pos <= 2000, 0.0, 10.0)
    y = np.where(pos <= 2000, pos, 4000 - pos)
    tr = build_track(pings(ts, x, y), line)
    assert tr.dropped == 0
    assert np.all(np.diff(tr.d) > 0)
    assert tr.d[-1] == pytest.approx(pos[-1], abs=15)
    # Same physical spot (y=500) is passed outbound at ~500 m and inbound at ~3500 m.
    stops = stop_distances(*latlon([0.0, 10.0], [500.0, 500.0]), line)
    assert stops == pytest.approx([500, 3500], abs=15)
    assert crossing_times(tr, stops) == pytest.approx([50, 350], abs=2)


def test_off_route_and_impossible_jumps_are_dropped():
    line = line_north()
    df = pings([0, 30, 60, 90], [0, 500, 0, 0], [0, 300, 600, 900])  # 2nd ping 500 m off the street
    tr = build_track(df, line)
    assert tr.dropped == 1 and len(tr.t) == 3


def test_backward_jitter_does_not_create_second_crossing():
    line = line_north()
    tr = build_track(pings([0, 30, 60, 90, 120], [0] * 5, [0, 300, 290, 300, 600]), line)
    assert np.all(np.diff(tr.d) >= 0)
    assert crossing_times(tr, np.array([295.0])) == pytest.approx([29.5], abs=1)


def test_repeated_position_keeps_first_occurrence():
    line = line_north()
    # Bus reaches 300 m at t=30 and the same fix repeats in later snapshots.
    tr = build_track(pings([0, 30, 60, 90, 120], [0] * 5, [0, 300, 300, 300, 600]), line)
    assert tr.t.tolist() == [0, 30, 120]


def test_gap_too_long_gives_no_estimate():
    tr = Track(t=np.array([0.0, MAX_GAP_S + 60]), d=np.array([0.0, 1000.0]),
               raw_d=np.array([0.0, 1000.0]), dropped=0)
    assert np.isnan(crossing_times(tr, np.array([500.0]))[0])


def test_stop_before_first_ping_or_after_last_is_nan():
    tr = Track(t=np.array([0.0, 30.0]), d=np.array([200.0, 500.0]), raw_d=np.array([200.0, 500.0]), dropped=0)
    out = crossing_times(tr, np.array([100.0, 300.0, 900.0, np.nan]))
    assert np.isnan(out[[0, 2, 3]]).all() and out[1] == pytest.approx(10.0)


def test_leave_one_out_zero_error_at_constant_speed():
    t = np.arange(0, 300, 30.0)
    d = t * 10
    tr = Track(t=t, d=d, raw_d=d, dropped=0)
    err = leave_one_out(tr, np.array([300.0, 600.0, 2500.0]))
    assert len(err) == 2 and np.allclose(err, 0)


def test_leave_one_out_sees_dwell_error():
    # Bus reaches the stop (300 m) at t=30, waits, leaves; neighbours straddle the dwell.
    t = np.array([0.0, 30.0, 90.0])
    d = np.array([0.0, 300.0, 600.0])
    err = leave_one_out(Track(t=t, d=d, raw_d=d, dropped=0), np.array([300.0]))
    assert err == pytest.approx([15.0])  # interpolation says 45 s, truth 30 s


def dense_out_and_back(step=5.0):
    ys = np.arange(0, 2000 + step, step)
    x = np.concatenate([np.zeros_like(ys), np.full_like(ys, 10.0)])
    y = np.concatenate([ys, ys[::-1]])
    return Polyline.from_latlon(*latlon(x, y))


def test_passes_one_candidate_per_leg():
    lat, lon = latlon([10.0], [500.0])
    along, off = dense_out_and_back().passes(lat[0], lon[0], max_off=100)
    assert len(along) == 2
    # Return leg: 2000 out + 10 m turnaround + 1500 back = 3510.
    assert along == pytest.approx([500, 3510], abs=1) and off == pytest.approx([10, 0], abs=0.5)


def test_dense_shape_stops_on_both_legs():
    line = dense_out_and_back()
    # Outbound stops at 500 and 1500; return-leg stops across the street at the same spots.
    stops = stop_distances(*latlon([0, 0, 10, 10], [500, 1500, 1500, 500]), line)
    assert stops == pytest.approx([500, 1500, 2510, 3510], abs=2)


def test_stop_slightly_behind_previous_stays_on_same_leg():
    line = dense_out_and_back()
    # Second stop's pole sits 20 m "behind" the first (noise): must stay outbound, not jump to return.
    stops = stop_distances(*latlon([0, 0, 0], [500, 480, 900]), line)
    assert stops[1] == pytest.approx(480, abs=2) and stops[2] == pytest.approx(900, abs=2)


def test_unplaceable_stop_is_nan_and_others_still_placed():
    line = line_north()
    stops = stop_distances(*latlon([0, 800, 0], [300, 1000, 1500]), line)  # middle stop 800 m off the line
    assert np.isnan(stops[1]) and stops[[0, 2]] == pytest.approx([300, 1500], abs=1)


def test_dense_out_and_back_track_recovers_after_turnaround():
    """Known limitation: right after a turnaround, the return side is within GPS noise of the
    outbound side, so one ping can be placed on the outbound leg. The next ping corrects it,
    and only distances within ~150 m of the turnaround are affected."""
    line = dense_out_and_back()
    ts = np.arange(0, 400, 30)
    pos = ts * 10.0                                  # true metres travelled (turnaround ignored)
    x = np.where(pos <= 2000, 0.0, 10.0)
    y = np.where(pos <= 2000, pos, 4000 - pos)
    true_along = np.where(pos <= 2000, pos, pos + 10)  # + the 10 m turnaround segment
    tr = build_track(pings(ts, x, y), line)
    assert tr.dropped == 0 and np.all(np.diff(tr.d) >= 0)
    far_from_turn = np.abs(true_along - 2005) > 150
    assert tr.d[far_from_turn] == pytest.approx(true_along[far_from_turn], abs=2)


def test_departure_sees_dwell_arrival_does_not():
    line = line_north()
    # Reaches the stop (300 m) at t=30, stands there (same coordinates) until t=90, then moves on.
    df = pings([0, 30, 60, 90, 120], [0] * 5, [0, 300, 300, 300, 600])
    arr = crossing_times(build_track(df, line), np.array([300.0]))
    dep = crossing_times(build_track(df, line, keep_repeats=True), np.array([330.0]))
    assert arr == pytest.approx([30.0], abs=0.5)
    assert dep == pytest.approx([93.0], abs=0.5)   # left between t=90 and t=120, 30 m past at ~93 s


def test_clean_vehicle_ts_three_cases():
    feed = 1_800_000_000
    rows = pd.DataFrame({
        "feed_ts": [feed] * 6,
        "vehicle_ts": pd.array([feed - 15, feed + 10, None, feed - 900, feed - 39 * 365 * 86400, feed + 3600],
                               dtype="Int64"),
        "lat": [51.0] * 6,
    })
    out, counts = clean_vehicle_ts(rows)
    # normal (15 s old), small skew (10 s ahead), no timestamp: kept as they are
    assert out["vehicle_ts"].iloc[:3].tolist()[:2] == [feed - 15, feed + 10]
    # 15 min old: stale position, dropped
    assert counts["vehicle_ts_stale_dropped"] == 1 and len(out) == 5
    # 1987, and 1 h in the future: broken clocks, timestamp removed but row kept
    assert counts["vehicle_ts_broken"] == 2
    assert out["vehicle_ts"].isna().sum() == 3
