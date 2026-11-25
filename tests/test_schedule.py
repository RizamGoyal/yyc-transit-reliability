import zipfile
from datetime import date

import pandas as pd
import pytest

from pipeline.schedule import (
    active_services,
    load_schedule,
    open_db,
    parse_gtfs_time,
    version_for,
)

GTFS = {
    # BOM on purpose: Calgary's files (like many) start with one.
    "routes.txt": "﻿route_id,route_short_name,route_long_name,route_type\n"
                  "3-20761,3,Sandstone/Elbow Drive,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id,shape_id\n"
                 "3-20761,WKDY,T1,0,S1\n"
                 "3-20761,SAT,T2,1,S1\n",
    "stops.txt": "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
                 "0001,1001,Stop A,51.0,-114.0\n"
                 "0002,1002,Stop B,51.01,-114.0\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                      "T1,23:58:00,23:58:30,0001,1\n"
                      "T1,25:30:00,25:30:00,0002,2\n"
                      "T2, 8:00:00,08:00:00,0001,1\n"
                      "T2,,,0002,2\n",
    "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                  "S1,51.0,-114.0,1\n"
                  "S1,51.01,-114.0,2\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                    "start_date,end_date\n"
                    "WKDY,1,1,1,1,1,0,0,20261101,20261231\n"
                    "SAT,0,0,0,0,0,1,0,20261101,20261231\n",
    "calendar_dates.txt": "service_id,date,exception_type\n"
                          "WKDY,20261111,2\n"    # Remembrance Day: weekday service removed
                          "SAT,20261111,1\n",    # ...and Saturday service runs instead
}


def make_zip(path, files=GTFS):
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return path


@pytest.fixture
def loaded(tmp_path):
    db = open_db(tmp_path / "schedule.db")
    version, _ = load_schedule(make_zip(tmp_path / "2026-11-19_abcd1234.zip"), db)
    return db, version


def test_parse_gtfs_time_keeps_hours_past_midnight():
    out = parse_gtfs_time(pd.Series(["08:00:00", "25:30:00", " 8:05:00", "", None]))
    assert out.tolist()[:3] == [28800, 91800, 29100]
    assert out.isna().tolist()[3:] == [True, True]


def test_loads_all_tables(loaded):
    db, v = loaded
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE version=?", (v,)).fetchone()[0]
              for t in ["routes", "trips", "stops", "stop_times", "shapes", "calendar", "calendar_dates"]}
    assert counts == {"routes": 1, "trips": 2, "stops": 2, "stop_times": 4, "shapes": 2,
                      "calendar": 2, "calendar_dates": 2}


def test_ids_stay_text_with_leading_zeros(loaded):
    db, v = loaded
    row = db.execute("SELECT stop_id FROM stops WHERE version=? ORDER BY stop_id", (v,)).fetchone()
    assert row[0] == "0001"


def test_stop_times_in_service_seconds(loaded):
    db, v = loaded
    rows = db.execute(
        "SELECT trip_id, stop_sequence, arrival_s, departure_s FROM stop_times WHERE version=? "
        "ORDER BY trip_id, stop_sequence", (v,)).fetchall()
    assert rows == [("T1", 1, 86280, 86310), ("T1", 2, 91800, 91800),
                    ("T2", 1, 28800, 28800), ("T2", 2, None, None)]


def test_optional_columns_are_null(loaded):
    db, v = loaded
    row = db.execute("SELECT block_id, trip_headsign FROM trips WHERE version=? LIMIT 1", (v,)).fetchone()
    assert row == (None, None)


def test_loading_twice_is_skipped(tmp_path, loaded):
    db, v = loaded
    v2, loaded_now = load_schedule(tmp_path / "2026-11-19_abcd1234.zip", db)
    assert v2 == v and not loaded_now
    assert db.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 2


def test_active_services_weekday_weekend_and_exceptions(loaded):
    db, v = loaded
    assert active_services(db, v, date(2026, 11, 23)) == {"WKDY"}   # Monday
    assert active_services(db, v, date(2026, 11, 28)) == {"SAT"}    # Saturday
    assert active_services(db, v, date(2026, 11, 29)) == set()      # Sunday: none in this fake feed
    assert active_services(db, v, date(2026, 11, 11)) == {"SAT"}    # holiday swap
    assert active_services(db, v, date(2027, 1, 5)) == set()        # after end_date


def test_version_for_picks_schedule_in_effect(tmp_path, loaded):
    db, v1 = loaded
    newer = dict(GTFS, **{"routes.txt": GTFS["routes.txt"].replace("Elbow Drive", "Elbow Dr")})
    v2, _ = load_schedule(make_zip(tmp_path / "2026-12-20_ffff0000.zip", newer), db)
    assert v1 != v2
    assert version_for(db, date(2026, 11, 23)) == v1
    assert version_for(db, date(2026, 12, 20)) == v2
    assert version_for(db, date(2026, 1, 1)) == v1   # before everything: oldest


def test_missing_required_file_fails_and_loads_nothing(tmp_path):
    db = open_db(tmp_path / "s.db")
    bad = {k: v for k, v in GTFS.items() if k != "trips.txt"}
    with pytest.raises(ValueError, match=r"trips\.txt"):
        load_schedule(make_zip(tmp_path / "bad.zip", bad), db)
    assert db.execute("SELECT COUNT(*) FROM schedule_versions").fetchone()[0] == 0
