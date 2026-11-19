"""Unit tests for the collector. Run: python -m pytest -q"""

import csv
import gzip
import time

import requests
from google.transit import gtfs_realtime_pb2

from collector.collector import (
    Collector,
    FeedState,
    open_db,
    parse_alerts,
    parse_positions,
    parse_trip_updates,
    write_positions,
)

FEED_TS = 1795044194  # 2026-11-18 23:23:14 UTC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vehicle_feed(feed_ts=FEED_TS):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = feed_ts
    e = feed.entity.add(id="1")
    e.vehicle.vehicle.id = "7995"
    e.vehicle.trip.trip_id = "75670261"
    e.vehicle.trip.route_id = "93"
    e.vehicle.position.latitude = 51.0376511
    e.vehicle.position.longitude = -114.1863556
    e.vehicle.timestamp = feed_ts - 5
    # no position -> skipped
    feed.entity.add(id="2").vehicle.vehicle.id = "1234"
    # 0,0 position = no GPS fix -> skipped
    bad = feed.entity.add(id="3").vehicle
    bad.vehicle.id = "5555"
    bad.position.latitude = 0
    bad.position.longitude = 0
    return feed


def _trip_feed(feed_ts=FEED_TS, arrival=-1):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = feed_ts
    e = feed.entity.add(id="t1")
    e.trip_update.trip.trip_id = "75670261"
    e.trip_update.trip.route_id = "93"
    s = e.trip_update.stop_time_update.add()
    s.stop_sequence = 13
    s.stop_id = "3732"
    s.arrival.time = arrival
    s.departure.time = 1795044480
    return feed


def _response(content=b"", status=200, url="http://test/feed"):
    r = requests.Response()
    r.status_code = status
    r._content = content
    r.url = url
    r.reason = "Too Many Requests" if status == 429 else "OK"
    return r


class FakeSession(requests.Session):
    """Returns queued responses instead of making network calls."""

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    def get(self, url, **kwargs):
        return self.responses.pop(0)


def _collector(tmp_path, responses):
    return Collector(data_dir=tmp_path, session=FakeSession(responses))


def _feed(c, name):
    return next(f for f in c.feeds if f.name == name)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_positions_skips_missing_and_zero_fixes():
    rows = parse_positions(_vehicle_feed())
    assert len(rows) == 1
    feed_ts, vehicle_ts, vid, trip, route, _start, lat, lon, bearing, speed = rows[0]
    assert (feed_ts, vid, trip, route) == (FEED_TS, "7995", "75670261", "93")
    assert vehicle_ts == FEED_TS - 5
    assert abs(lat - 51.037651) < 1e-5 and abs(lon + 114.186356) < 1e-5
    assert bearing is None and speed is None


def test_minus_one_becomes_null():
    rows = parse_trip_updates(_trip_feed())
    assert len(rows) == 1
    start_date, trip, seq, stop, _route, arrival, departure, _rel, _ts = rows[0]
    assert arrival is None                 # -1 placeholder dropped
    assert departure == 1795044480
    assert start_date == "20261118"        # filled from feed time when missing
    assert (trip, seq, stop) == ("75670261", 13, "3732")


def test_alerts_parse():
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = FEED_TS
    e = feed.entity.add(id="a1")
    e.alert.header_text.translation.add(text="Route 20 detour")
    e.alert.informed_entity.add(route_id="20")
    alerts = parse_alerts(feed)
    assert alerts[0]["header"] == "Route 20 detour"
    assert alerts[0]["routes"] == "20"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_write_positions_appends_hourly(tmp_path):
    rows = parse_positions(_vehicle_feed())
    write_positions(rows, tmp_path)
    write_positions(rows, tmp_path)        # second flush appends, no second header
    path = tmp_path / "positions" / "2026-11-18" / "23.csv.gz"
    with gzip.open(path, "rt") as f:
        lines = list(csv.reader(f))
    assert lines[0][0] == "feed_ts"
    assert len(lines) == 3


def test_db_schema_creates(tmp_path):
    db = open_db(tmp_path / "c.db")
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"polls", "trip_predictions", "alerts", "static_versions"} <= tables


# ---------------------------------------------------------------------------
# Polling behaviour
# ---------------------------------------------------------------------------

def test_duplicate_snapshot_is_skipped(tmp_path):
    body = _vehicle_feed().SerializeToString()
    c = _collector(tmp_path, [_response(body), _response(body)])
    fs = _feed(c, "vehicles")
    c.poll_realtime(fs)
    c.poll_realtime(fs)
    assert len(c.position_buffer) == 1     # stored once, not twice
    dupes = c.db.execute("SELECT duplicate FROM polls WHERE feed='vehicles' ORDER BY rowid").fetchall()
    assert [d[0] for d in dupes] == [0, 1]


def test_rate_limit_backs_off_and_logs(tmp_path):
    c = _collector(tmp_path, [_response(status=429)])
    fs = _feed(c, "vehicles")
    before = time.time()
    c.poll_realtime(fs)
    ok, status = c.db.execute("SELECT ok, http_status FROM polls").fetchone()
    assert (ok, status) == (0, 429)
    assert fs.failures == 1
    assert fs.next_at >= before + fs.every * 2   # waits longer than normal


def test_backoff_is_capped():
    fs = FeedState("vehicles", "http://x", every=15, failures=20)
    fs.schedule_next(ok=False)
    assert fs.next_at <= time.time() + 300 + 1


def test_known_arrival_not_overwritten_by_placeholder(tmp_path):
    first = _trip_feed(FEED_TS, arrival=1795044470).SerializeToString()
    second = _trip_feed(FEED_TS + 30, arrival=-1).SerializeToString()
    c = _collector(tmp_path, [_response(first), _response(second)])
    fs = _feed(c, "trips")
    c.poll_realtime(fs)
    c.poll_realtime(fs)
    arrival, feed_ts = c.db.execute("SELECT arrival, feed_ts FROM trip_predictions").fetchone()
    assert arrival == 1795044470           # kept, not replaced by NULL
    assert feed_ts == FEED_TS + 30         # but the row was refreshed


def test_bad_protobuf_is_logged_not_fatal(tmp_path):
    c = _collector(tmp_path, [_response(b"this is not protobuf")])
    fs = _feed(c, "vehicles")
    c.poll_realtime(fs)                    # must not raise
    assert c.db.execute("SELECT ok FROM polls").fetchone()[0] == 0


def test_static_saved_once_per_version(tmp_path):
    z = b"PK fake zip bytes"
    c = _collector(tmp_path, [_response(z), _response(z)])
    fs = _feed(c, "static")
    c.poll_static(fs)
    c.poll_static(fs)
    assert c.db.execute("SELECT COUNT(*) FROM static_versions").fetchone()[0] == 1
    assert len(list((tmp_path / "static").glob("*.zip"))) == 1
