from datetime import date, datetime

import pandas as pd
import pytest
from test_schedule import make_zip

from pipeline.match import TZ, match_trips
from pipeline.schedule import load_schedule, open_db


def ts(y, mo, d, h, mi):
    """Calgary local time -> unix seconds."""
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


@pytest.fixture
def db(tmp_path):
    db = open_db(tmp_path / "schedule.db")
    load_schedule(make_zip(tmp_path / "2026-11-19_abcd1234.zip"), db)
    return db


def gps(rows):
    df = pd.DataFrame(rows, columns=["feed_ts", "trip_id", "start_date"])
    df["route_id"] = pd.NA
    df["trip_id"] = df["trip_id"].astype("string")
    df["start_date"] = df["start_date"].astype("string")
    return df


def test_after_midnight_belongs_to_previous_service_day(db):
    # T1 runs 23:58 -> 25:30 on Monday's schedule; seen 00:40 Tuesday, no start_date.
    out = match_trips(gps([(ts(2026, 11, 24, 0, 40), "T1", None)]), db).iloc[0]
    assert out["matched"] and out["service_date"] == date(2026, 11, 23)
    assert out["service_s"] == 24 * 3600 + 40 * 60
    assert out["service_active"]  # weekday service on a Monday
    assert out["route_short_name"] == "3"


def test_before_midnight_stays_same_day(db):
    out = match_trips(gps([(ts(2026, 11, 23, 23, 59), "T1", None)]), db).iloc[0]
    assert out["service_date"] == date(2026, 11, 23) and out["service_s"] == 23 * 3600 + 59 * 60


def test_feed_start_date_wins(db):
    out = match_trips(gps([(ts(2026, 11, 24, 0, 40), "T1", "20261123")]), db).iloc[0]
    assert out["service_date"] == date(2026, 11, 23)


def test_trip_not_scheduled_that_day_is_flagged(db):
    # T2 is a Saturday trip, seen on a Monday.
    out = match_trips(gps([(ts(2026, 11, 23, 8, 0), "T2", None)]), db).iloc[0]
    assert out["matched"] and not out["service_active"]


def test_unknown_and_missing_trips_are_unmatched(db):
    out = match_trips(gps([(ts(2026, 11, 23, 9, 0), "NOPE", None), (ts(2026, 11, 23, 9, 0), None, None)]), db)
    assert not out["matched"].any() and not out["service_active"].any()
    assert (out["service_date"] == date(2026, 11, 23)).all()


def test_keeps_row_order_and_count(db):
    rows = [(ts(2026, 11, 23, 23, 59), "T1", None), (ts(2026, 11, 23, 9, 0), "NOPE", None),
            (ts(2026, 11, 24, 0, 40), "T1", None)]
    out = match_trips(gps(rows), db)
    assert out["feed_ts"].tolist() == [r[0] for r in rows]
