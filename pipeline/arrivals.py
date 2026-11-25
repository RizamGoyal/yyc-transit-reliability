"""Turn GPS pings into stop arrival times, and measure how accurate that is.

Per trip:
1. Pings are sorted by time. A ping's time is the vehicle's own GPS timestamp when the
   feed provides one, otherwise the feed snapshot time (which can trail the real fix).
   Repeats of the exact same position keep only the FIRST occurrence: without a vehicle
   timestamp we can't tell a stale repeat from a bus standing still, and the first
   occurrence is the right one for *arrival*. (So we measure arrivals, not departures.)
2. Each ping is snapped onto the route shape ("metres along the route"), always at or
   ahead of the previous ping. Pings far off the line, or implying an impossible speed,
   are dropped.
3. Distance is made non-decreasing (running max), so GPS jitter backwards at a stop
   can't create a second "arrival".
4. Arrival at a stop = the moment the bus first reaches the stop's distance, linearly
   interpolated between the ping before and the ping after. If those two pings are more
   than MAX_GAP_S apart, or the trip's data starts after the stop, there's no estimate.

Departures: a second track keeps repeated positions (a bus standing at a stop reports
the same coordinates), and departure = first time the bus is DEPART_PAST_M past the stop.
For buses that send their own timestamp, stale repeats are removed first; for buses that
don't, a stale repeat looks like standing still, so their departures can read slightly
late. The report shows the two groups separately so that bias is visible.

Validation (leave-one-out): for pings that sit right at a stop, hide the ping, predict
its time from its neighbours, compare. Hiding a ping doubles the gap the interpolation
has to bridge, so this error is pessimistic compared with normal use.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np
import pandas as pd

from pipeline.geometry import Polyline

MAX_OFF_M = 50.0         # ping farther than this from the route line is ignored
STOP_MAX_OFF_M = 100.0   # stops can sit a bit off the drawn line
BACK_TOL_M = 30.0        # GPS jitter allowed backwards before a ping is discarded
MAX_SPEED_MS = 35.0      # 126 km/h; anything faster between pings is a bad fix
MAX_GAP_S = 120.0        # longest gap we'll interpolate across
LOO_NEAR_STOP_M = 15.0   # a ping this close to a stop counts as "at the stop"
DEPART_PAST_M = 30.0     # "departed" = first time the bus is this far past the stop
WINDOW_BEFORE_S = 1800   # pings more than 30 min before the trip's scheduled start are ignored
WINDOW_AFTER_S = 5400    # ...or more than 90 min after its scheduled end (stale trip IDs reused later)
RUN_GAP_S = 900          # silence longer than this splits a trip's pings into separate runs
DEFAULT_LAG_S = 15.0     # feed time minus GPS fix time, used if a day has too few fixes to measure


@dataclass
class Track:
    t: np.ndarray       # unix seconds
    d: np.ndarray       # metres along route, non-decreasing
    raw_d: np.ndarray   # metres along route before the running max
    dropped: int        # pings discarded (off route, backwards, too fast)


def build_track(pings: pd.DataFrame, line: Polyline, *, keep_repeats: bool = False) -> Track:
    """pings: one trip's rows with columns t, lat, lon (any order).

    keep_repeats=False (arrivals): a run of identical positions keeps only its first ping.
    keep_repeats=True (departures): identical positions are kept, so a bus standing at a
    stop stays visible until it moves. Callers should already have removed stale repeats
    they can identify (same vehicle timestamp).
    """
    p = pings.sort_values("t", kind="stable")
    if not keep_repeats:
        same = (p["lat"] == p["lat"].shift()) & (p["lon"] == p["lon"].shift())
        p = p[~same]
    ts: list[float] = []
    ds: list[float] = []
    dropped = 0  # repeats of the same position are merged, not counted as dropped
    prev_d, prev_t = -np.inf, None
    for t, lat, lon in zip(p["t"].to_numpy(float), p["lat"].to_numpy(float), p["lon"].to_numpy(float),
                           strict=True):
        if np.isnan(lat) or np.isnan(lon):
            dropped += 1
            continue
        hit = line.locate(lat, lon, after=max(prev_d, 0.0) if ts else 0.0, max_off=MAX_OFF_M,
                          back_tol=BACK_TOL_M if ts else 0.0)
        if hit is None:
            dropped += 1
            continue
        d = hit[0]
        if prev_t is not None and t > prev_t and (d - prev_d) / (t - prev_t) > MAX_SPEED_MS:
            dropped += 1
            continue
        if prev_t is not None and t == prev_t:
            continue
        ts.append(t)
        ds.append(d)
        prev_d, prev_t = max(prev_d, d), t
    raw = np.array(ds, dtype=float)
    return Track(np.array(ts, dtype=float), np.maximum.accumulate(raw) if len(raw) else raw, raw, dropped)


def stop_distances(stop_lat, stop_lon, line: Polyline) -> np.ndarray:
    """Distance along the line for each stop (trip order); NaN if a stop can't be placed.

    Stops are placed together, not one at a time: each stop has one candidate per pass of
    the line near it, and we choose one candidate per stop so that stops never go
    backwards (beyond BACK_TOL_M) and the total distance from the line is smallest.
    Placing greedily breaks on out-and-back routes, where a return-leg stop on the other
    side of the street is also close to the outbound leg.
    """
    cands = [line.passes(lat, lon, STOP_MAX_OFF_M) for lat, lon in zip(stop_lat, stop_lon, strict=True)]
    n = len(cands)
    out = np.full(n, np.nan)
    placed = [k for k in range(n) if len(cands[k][0])]
    if not placed:
        return out
    # cost[c] = best total offset for paths ending at candidate c of the last placed stop.
    cost = cands[placed[0]][1].copy()
    back: list[np.ndarray] = [np.full(len(cost), -1)]
    kept = [placed[0]]
    for k in placed[1:]:
        prev_along = cands[kept[-1]][0]
        along, off = cands[k]
        valid = along[:, None] >= prev_along[None, :] - BACK_TOL_M      # [cand, prev]
        total = np.where(valid, cost[None, :], np.inf)
        best_prev = np.argmin(total, axis=1)
        new_cost = total[np.arange(len(along)), best_prev] + off
        if np.isinf(new_cost).all():
            continue  # this stop can't sit in order with the others: leave it unplaced
        cost, kept = new_cost, [*kept, k]
        back.append(best_prev)
    c = int(np.argmin(cost))
    for j in range(len(kept) - 1, -1, -1):
        out[kept[j]] = cands[kept[j]][0][c]
        c = int(back[j][c])
    return out


def crossing_times(track: Track, targets: np.ndarray, max_gap: float = MAX_GAP_S) -> np.ndarray:
    """Time the track first reaches each target distance (NaN if not observable)."""
    t, d = track.t, track.d
    out = np.full(len(targets), np.nan)
    if len(t) < 2:
        return out
    for k, target in enumerate(targets):
        if np.isnan(target):
            continue
        j = int(np.searchsorted(d, target, side="left"))  # first index with d >= target
        if j == 0 or j >= len(d):
            continue  # data starts after the stop, or never reaches it
        i = j - 1
        if t[j] - t[i] > max_gap:
            continue
        out[k] = t[i] + (target - d[i]) / (d[j] - d[i]) * (t[j] - t[i])
    return out


def leave_one_out(track: Track, stop_ds: np.ndarray, max_gap: float = 2 * MAX_GAP_S) -> np.ndarray:
    """Prediction errors (seconds, predicted - actual) for pings within LOO_NEAR_STOP_M of a stop."""
    t, d = track.t, track.d
    stops = np.sort(stop_ds[~np.isnan(stop_ds)])
    errors = []
    if len(stops) == 0:
        return np.array(errors)
    for i in range(1, len(t) - 1):
        nearest = stops[np.clip(np.searchsorted(stops, d[i]), 1, len(stops)) - 1 :][:2]
        if np.min(np.abs(nearest - d[i])) > LOO_NEAR_STOP_M:
            continue
        if d[i + 1] <= d[i - 1] or t[i + 1] - t[i - 1] > max_gap:
            continue
        pred = t[i - 1] + (d[i] - d[i - 1]) / (d[i + 1] - d[i - 1]) * (t[i + 1] - t[i - 1])
        errors.append(pred - t[i])
    return np.array(errors, dtype=float)


# ---------------------------------------------------------------------------
# One route, one service day
# ---------------------------------------------------------------------------

def _shape(db: sqlite3.Connection, version: str, shape_id: str) -> Polyline:
    rows = db.execute(
        "SELECT shape_pt_lat, shape_pt_lon FROM shapes WHERE version=? AND shape_id=? "
        "ORDER BY shape_pt_sequence",
        (version, shape_id),
    ).fetchall()
    return Polyline.from_latlon([r[0] for r in rows], [r[1] for r in rows])


def _stop_times(db: sqlite3.Connection, version: str, trip_id: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT st.stop_sequence, st.stop_id, st.arrival_s, st.departure_s, st.timepoint, "
        "s.stop_lat, s.stop_lon "
        "FROM stop_times st JOIN stops s ON s.version = st.version AND s.stop_id = st.stop_id "
        "WHERE st.version=? AND st.trip_id=? ORDER BY st.stop_sequence",
        db, params=(version, trip_id),
    )


def best_run(track: Track, sched_mid_ts: float) -> tuple[float, float, int]:
    """Split an ON-ROUTE track wherever it has no on-route ping for more than RUN_GAP_S, and
    return the time range (lo, hi) of the part whose middle is closest to the trip's
    scheduled middle, plus how many track points fell outside it.

    Why: a bus sometimes reports an old trip ID again on a later run, or keeps the trip ID
    while off the route for a long time (garage, detour) and then comes back. Without
    this, the later pings get stitched onto the real trip and look like stops served
    hours late. Splitting on the on-route track (not raw pings) catches both cases; the
    track must keep repeated positions so a long dwell at a stop isn't mistaken for a gap.
    """
    t = track.t
    if len(t) == 0:
        return -np.inf, np.inf, 0
    run = np.concatenate([[0], np.cumsum(np.diff(t) > RUN_GAP_S)])
    if run[-1] == 0:
        return -np.inf, np.inf, 0
    best = min(range(run[-1] + 1), key=lambda r: abs(np.median(t[run == r]) - sched_mid_ts))
    keep = run == best
    return float(t[keep].min()), float(t[keep].max()), int((~keep).sum())


def measure_lag(rows: pd.DataFrame, min_fixes: int = 200) -> tuple[float, int]:
    """Median of (feed time - GPS fix time) over the first appearance of each fix.
    Returns (lag seconds, number of fixes); DEFAULT_LAG_S if there are too few."""
    has = rows[rows["vehicle_ts"].notna()]
    first = has.groupby(["vehicle_id", "vehicle_ts"])["feed_ts"].min().reset_index()
    lag = (first["feed_ts"] - first["vehicle_ts"]).astype(float)
    if len(lag) < min_fixes:
        return DEFAULT_LAG_S, len(lag)
    return float(lag.median()), len(lag)


def route_day_arrivals(
    matched: pd.DataFrame, db: sqlite3.Connection, lag_s: float = 0.0
) -> tuple[pd.DataFrame, np.ndarray, dict]:
    """matched: output of match_trips filtered to the trips to process (service_active rows).
    lag_s: subtracted from feed time for pings without their own GPS timestamp.
    Returns (one row per trip stop with arrival and departure, leave-one-out errors, counters)."""
    m = matched.copy()
    feed_t = m["feed_ts"].astype(float) - lag_s
    m["t"] = m["vehicle_ts"].astype("Float64").fillna(feed_t.astype("Float64")).astype(float)
    in_window = m["service_s"].between(m["trip_start_s"] - WINDOW_BEFORE_S,
                                       m["trip_end_s"] + WINDOW_AFTER_S)
    shapes: dict[tuple[str, str], Polyline] = {}
    stop_cache: dict[tuple, np.ndarray] = {}  # trips sharing a shape + stop pattern share placements
    stats = {"trips": 0, "trips_no_shape": 0, "pings": len(m),
             "pings_outside_window": int((~in_window).sum()), "pings_other_run": 0, "trips_split": 0,
             "pings_dropped": 0, "stops_unplaced": 0}
    m = m[in_window]
    rows, loo = [], []
    for (version, trip_id, service_date), g in m.groupby(["version", "trip_id", "service_date"], sort=False):
        stats["trips"] += 1
        shape_id = g["shape_id"].iloc[0]
        if pd.isna(shape_id):
            stats["trips_no_shape"] += 1
            continue
        key = (version, shape_id)
        if key not in shapes:
            shapes[key] = _shape(db, version, shape_id)
        line = shapes[key]

        midnight = pd.Timestamp(service_date, tz="America/Edmonton").timestamp()
        stale = g["vehicle_ts"].notna() & g.duplicated(["vehicle_id", "vehicle_ts"])
        fresh = g.loc[~stale, ["t", "lat", "lon"]]
        sched_mid = midnight + (g["trip_start_s"].iloc[0] + g["trip_end_s"].iloc[0]) / 2
        lo, hi, n_other = best_run(build_track(fresh, line, keep_repeats=True), sched_mid)
        stats["pings_other_run"] += n_other
        stats["trips_split"] += int(n_other > 0)
        g = g[g["t"].between(lo, hi)]
        fresh = fresh[fresh["t"].between(lo, hi)]

        track = build_track(g[["t", "lat", "lon"]], line)
        dep_track = build_track(fresh, line, keep_repeats=True)
        stats["pings_dropped"] += track.dropped

        st = _stop_times(db, version, trip_id)
        pattern = (version, shape_id, tuple(st["stop_id"]))
        if pattern not in stop_cache:
            stop_cache[pattern] = stop_distances(st["stop_lat"], st["stop_lon"], line)
        sd = stop_cache[pattern]
        stats["stops_unplaced"] += int(np.isnan(sd).sum())
        loo.append(leave_one_out(track, sd))

        arr_s = st["arrival_s"].astype("Float64").fillna(st["departure_s"].astype("Float64")).astype(float)
        dep_s = st["departure_s"].astype("Float64").fillna(st["arrival_s"].astype("Float64")).astype(float)
        rows.append(pd.DataFrame({
            "service_date": service_date, "trip_id": trip_id,
            "route_short_name": g["route_short_name"].iloc[0], "direction_id": g["direction_id"].iloc[0],
            "own_timestamp": bool(g["vehicle_ts"].notna().mean() > 0.5),
            "stop_sequence": st["stop_sequence"], "stop_id": st["stop_id"], "timepoint": st["timepoint"],
            "stop_dist_m": sd,
            "scheduled_ts": midnight + arr_s, "actual_ts": crossing_times(track, sd),
            "scheduled_dep_ts": midnight + dep_s,
            "departure_ts": crossing_times(dep_track, sd + DEPART_PAST_M),
        }))
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if len(out):
        out["delay_s"] = out["actual_ts"] - out["scheduled_ts"]
        out["dep_delay_s"] = out["departure_ts"] - out["scheduled_dep_ts"]
    return out, (np.concatenate(loo) if loo else np.array([])), stats
