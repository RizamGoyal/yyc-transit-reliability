"""
YYC Transit Reliability - data collector.

Polls Calgary Transit's open GTFS-Realtime feeds and the static GTFS schedule,
and stores what it gets:

  data/positions/YYYY-MM-DD/HH.csv.gz   every vehicle GPS ping (raw, kept forever)
  data/collector.db (SQLite)            latest trip predictions, alerts,
                                        schedule versions, and a log of every poll
  data/static/<date>_<hash>.zip         each new version of the schedule

All timestamps are stored as UTC epoch seconds.

Run:  python -m collector.collector
Stop: Ctrl+C (or `systemctl stop` on the server). Buffered data is flushed on exit.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import logging
import os
import signal
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

# ---------------------------------------------------------------------------
# Configuration (override any of these with environment variables / .env)
# ---------------------------------------------------------------------------

def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)

DATA_DIR = Path(_env("DATA_DIR", "data"))
APP_TOKEN = _env("CALGARY_APP_TOKEN", "")  # optional free token from data.calgary.ca

VEHICLE_URL = _env("VEHICLE_URL", "https://data.calgary.ca/download/am7c-qe3u/application%2Foctet-stream")
TRIP_URL = _env("TRIP_URL", "https://data.calgary.ca/download/gs4m-mdc2/application%2Foctet-stream")
ALERT_URL = _env("ALERT_URL", "https://data.calgary.ca/download/jhgn-ynqj/application%2Foctet-stream")
STATIC_URL = _env("STATIC_URL", "https://data.calgary.ca/download/npk7-z3bj/application%2Fzip")

VEHICLE_EVERY = int(_env("VEHICLE_EVERY", "15"))      # seconds
TRIP_EVERY = int(_env("TRIP_EVERY", "60"))
ALERT_EVERY = int(_env("ALERT_EVERY", "300"))
STATIC_EVERY = int(_env("STATIC_EVERY", "86400"))     # once a day
FLUSH_EVERY = int(_env("FLUSH_EVERY", "300"))         # write buffered GPS rows every 5 min

MAX_BACKOFF = 300  # never wait more than 5 minutes between retries

log = logging.getLogger("collector")

POSITION_COLUMNS = [
    "feed_ts", "vehicle_ts", "vehicle_id", "trip_id", "route_id",
    "start_date", "lat", "lon", "bearing", "speed",
]

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    polled_at   INTEGER NOT NULL,   -- when we asked
    feed        TEXT    NOT NULL,   -- vehicles / trips / alerts / static
    ok          INTEGER NOT NULL,   -- 1 = success, 0 = failure
    http_status INTEGER,
    bytes       INTEGER,
    entities    INTEGER,
    feed_ts     INTEGER,            -- timestamp inside the feed header
    duplicate   INTEGER DEFAULT 0,  -- 1 = same feed_ts as last time, skipped
    error       TEXT
);
CREATE INDEX IF NOT EXISTS polls_feed_time ON polls(feed, polled_at);

-- Latest prediction per trip + stop. -1 placeholders are stored as NULL.
CREATE TABLE IF NOT EXISTS trip_predictions (
    start_date   TEXT    NOT NULL,
    trip_id      TEXT    NOT NULL,
    stop_sequence INTEGER NOT NULL,
    stop_id      TEXT,
    route_id     TEXT,
    arrival      INTEGER,
    departure    INTEGER,
    schedule_relationship INTEGER,
    feed_ts      INTEGER NOT NULL,
    PRIMARY KEY (start_date, trip_id, stop_sequence)
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_key    TEXT PRIMARY KEY,  -- hash of id + text, so edited alerts are kept too
    alert_id     TEXT,
    header       TEXT,
    description  TEXT,
    cause        INTEGER,
    effect       INTEGER,
    routes       TEXT,              -- comma-separated route_ids
    stops        TEXT,              -- comma-separated stop_ids
    active_start INTEGER,
    active_end   INTEGER,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS static_versions (
    sha256       TEXT PRIMARY KEY,
    downloaded_at INTEGER NOT NULL,
    path         TEXT NOT NULL,
    bytes        INTEGER NOT NULL
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")   # safe to read while the collector writes
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    return db


def record_poll(db: sqlite3.Connection, feed: str, *, ok: bool, http_status=None,
                nbytes=None, entities=None, feed_ts=None, duplicate=False, error=None) -> None:
    db.execute(
        "INSERT INTO polls (polled_at, feed, ok, http_status, bytes, entities, feed_ts, duplicate, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(time.time()), feed, int(ok), http_status, nbytes, entities, feed_ts, int(duplicate),
         (error or "")[:500] or None),
    )
    db.commit()


def write_positions(rows: list[list], data_dir: Path) -> int:
    """Append rows to hourly gzip CSV files, grouped by the UTC hour of feed_ts."""
    by_file: dict[Path, list[list]] = {}
    for row in rows:
        ts = datetime.fromtimestamp(row[0], tz=timezone.utc)
        path = data_dir / "positions" / ts.strftime("%Y-%m-%d") / f"{ts:%H}.csv.gz"
        by_file.setdefault(path, []).append(row)

    for path, file_rows in by_file.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        # Appending to a gzip file adds a new gzip "member"; readers (gzip, pandas) handle this.
        with gzip.open(path, "at", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(POSITION_COLUMNS)
            writer.writerows(file_rows)
    return len(rows)

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _time_or_none(event) -> int | None:
    """Calgary uses -1 (and sometimes 0) as 'no time'. Store those as NULL."""
    t = int(event.time)
    return t if t > 0 else None


def parse_positions(feed) -> list[list]:
    feed_ts = int(feed.header.timestamp)
    rows = []
    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        if not v.HasField("position"):
            continue
        p = v.position
        if p.latitude == 0 and p.longitude == 0:  # no GPS fix
            continue
        rows.append([
            feed_ts,
            int(v.timestamp) or None,
            v.vehicle.id,
            v.trip.trip_id or None,
            v.trip.route_id or None,
            v.trip.start_date or None,
            round(p.latitude, 6),
            round(p.longitude, 6),
            round(p.bearing, 1) if p.HasField("bearing") else None,
            round(p.speed, 2) if p.HasField("speed") else None,
        ])
    return rows


def parse_trip_updates(feed) -> list[tuple]:
    feed_ts = int(feed.header.timestamp)
    today = datetime.fromtimestamp(feed_ts, tz=timezone.utc).strftime("%Y%m%d")
    rows = []
    for e in feed.entity:
        if not e.HasField("trip_update"):
            continue
        tu = e.trip_update
        start_date = tu.trip.start_date or today  # start_date is optional in Calgary's feed
        for stu in tu.stop_time_update:
            rows.append((
                start_date,
                tu.trip.trip_id,
                int(stu.stop_sequence),
                stu.stop_id or None,
                tu.trip.route_id or None,
                _time_or_none(stu.arrival) if stu.HasField("arrival") else None,
                _time_or_none(stu.departure) if stu.HasField("departure") else None,
                int(stu.schedule_relationship),
                feed_ts,
            ))
    return rows


def _text(translated) -> str:
    return " ".join(t.text for t in translated.translation).strip()


def parse_alerts(feed) -> list[dict]:
    out = []
    for e in feed.entity:
        if not e.HasField("alert"):
            continue
        a = e.alert
        header, desc = _text(a.header_text), _text(a.description_text)
        periods = list(a.active_period)
        out.append({
            "alert_key": hashlib.sha256(f"{e.id}|{header}|{desc}".encode()).hexdigest()[:32],
            "alert_id": e.id,
            "header": header,
            "description": desc,
            "cause": int(a.cause),
            "effect": int(a.effect),
            "routes": ",".join(sorted({i.route_id for i in a.informed_entity if i.route_id})),
            "stops": ",".join(sorted({i.stop_id for i in a.informed_entity if i.stop_id})),
            "active_start": int(periods[0].start) if periods and periods[0].start else None,
            "active_end": int(periods[-1].end) if periods and periods[-1].end else None,
        })
    return out

# ---------------------------------------------------------------------------
# Fetching with back-off
# ---------------------------------------------------------------------------

@dataclass
class FeedState:
    name: str
    url: str
    every: int
    next_at: float = 0.0
    failures: int = 0
    last_feed_ts: int | None = None

    def schedule_next(self, ok: bool) -> None:
        if ok:
            self.failures = 0
            self.next_at = time.time() + self.every
        else:
            self.failures += 1
            wait = min(self.every * (2 ** self.failures), MAX_BACKOFF)
            self.next_at = time.time() + wait
            log.warning("%s: failure #%d, retrying in %ds", self.name, self.failures, wait)


@dataclass
class Collector:
    data_dir: Path = DATA_DIR
    session: requests.Session = field(default_factory=requests.Session)
    position_buffer: list = field(default_factory=list)
    last_flush: float = field(default_factory=time.time)
    running: bool = True

    def __post_init__(self):
        self.db = open_db(self.data_dir / "collector.db")
        self.session.headers["User-Agent"] = "yyc-transit-reliability (student project)"
        if APP_TOKEN:
            self.session.headers["X-App-Token"] = APP_TOKEN
        self.feeds = [
            FeedState("vehicles", VEHICLE_URL, VEHICLE_EVERY),
            FeedState("trips", TRIP_URL, TRIP_EVERY),
            FeedState("alerts", ALERT_URL, ALERT_EVERY),
            FeedState("static", STATIC_URL, STATIC_EVERY),
        ]

    # -- one poll ----------------------------------------------------------

    def fetch(self, url: str, timeout: int) -> requests.Response:
        resp = self.session.get(url, timeout=timeout)
        resp.raise_for_status()  # 429 / 5xx become exceptions -> back-off
        return resp

    def poll_realtime(self, fs: FeedState) -> None:
        status = None
        try:
            resp = self.fetch(fs.url, timeout=30)
            status = resp.status_code
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(resp.content)
            feed_ts = int(feed.header.timestamp)

            if feed_ts == fs.last_feed_ts:
                record_poll(self.db, fs.name, ok=True, http_status=status, nbytes=len(resp.content),
                            entities=len(feed.entity), feed_ts=feed_ts, duplicate=True)
                fs.schedule_next(ok=True)
                return
            fs.last_feed_ts = feed_ts

            if fs.name == "vehicles":
                self.position_buffer.extend(parse_positions(feed))
            elif fs.name == "trips":
                rows = parse_trip_updates(feed)
                self.db.executemany(
                    "INSERT INTO trip_predictions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(start_date, trip_id, stop_sequence) DO UPDATE SET"
                    " stop_id=excluded.stop_id, route_id=excluded.route_id,"
                    " arrival=COALESCE(excluded.arrival, arrival),"
                    " departure=COALESCE(excluded.departure, departure),"
                    " schedule_relationship=excluded.schedule_relationship, feed_ts=excluded.feed_ts",
                    rows,
                )
            elif fs.name == "alerts":
                now = int(time.time())
                for a in parse_alerts(feed):
                    self.db.execute(
                        "INSERT INTO alerts (alert_key, alert_id, header, description, cause, effect,"
                        " routes, stops, active_start, active_end, first_seen, last_seen)"
                        " VALUES (:alert_key, :alert_id, :header, :description, :cause, :effect,"
                        " :routes, :stops, :active_start, :active_end, :now, :now)"
                        " ON CONFLICT(alert_key) DO UPDATE SET last_seen=:now",
                        {**a, "now": now},
                    )

            record_poll(self.db, fs.name, ok=True, http_status=status, nbytes=len(resp.content),
                        entities=len(feed.entity), feed_ts=feed_ts)
            fs.schedule_next(ok=True)

        # Catch everything on purpose: one bad poll must never stop the collector.
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                status = exc.response.status_code
            log.warning("%s: %s", fs.name, exc)
            record_poll(self.db, fs.name, ok=False, http_status=status, error=repr(exc))
            fs.schedule_next(ok=False)

    def poll_static(self, fs: FeedState) -> None:
        status = None
        try:
            resp = self.fetch(fs.url, timeout=120)
            status = resp.status_code
            digest = hashlib.sha256(resp.content).hexdigest()
            exists = self.db.execute("SELECT 1 FROM static_versions WHERE sha256=?", (digest,)).fetchone()
            if not exists:
                out = self.data_dir / "static" / f"{datetime.now(timezone.utc):%Y-%m-%d}_{digest[:8]}.zip"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(resp.content)
                self.db.execute("INSERT INTO static_versions VALUES (?, ?, ?, ?)",
                                (digest, int(time.time()), str(out), len(resp.content)))
                log.info("static: NEW schedule version saved to %s", out)
            record_poll(self.db, fs.name, ok=True, http_status=status, nbytes=len(resp.content),
                        duplicate=bool(exists))
            fs.schedule_next(ok=True)
        except Exception as exc:  # noqa: BLE001 - same reason as above
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                status = exc.response.status_code
            log.warning("static: %s", exc)
            record_poll(self.db, fs.name, ok=False, http_status=status, error=repr(exc))
            fs.schedule_next(ok=False)

    # -- main loop ---------------------------------------------------------

    def flush(self) -> None:
        if self.position_buffer:
            n = write_positions(self.position_buffer, self.data_dir)
            log.info("flushed %d GPS rows", n)
            self.position_buffer.clear()
        self.last_flush = time.time()

    def stop(self, *_):
        log.info("stopping...")
        self.running = False

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        log.info("collector started, writing to %s", self.data_dir.resolve())
        try:
            while self.running:
                now = time.time()
                for fs in self.feeds:
                    if now >= fs.next_at:
                        if fs.name == "static":
                            self.poll_static(fs)
                        else:
                            self.poll_realtime(fs)
                if time.time() - self.last_flush >= FLUSH_EVERY:
                    self.flush()
                time.sleep(1)
        finally:
            self.flush()
            self.db.close()
            log.info("collector stopped cleanly")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Collector().run()


if __name__ == "__main__":
    main()
