"""Tests for the backup export of small tables."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from export_meta import export

from collector.collector import open_db, record_poll


def test_export_copies_small_tables_and_skips_predictions(tmp_path):
    db_path = tmp_path / "collector.db"
    db = open_db(db_path)
    record_poll(db, "vehicles", ok=True, http_status=200, entities=350)
    db.execute(
        "INSERT INTO trip_predictions VALUES ('20261119', 't1', 1, 's1', 'r1', NULL, 100, 0, 1)"
    )
    db.execute(
        "INSERT INTO alerts (alert_key, alert_id, header, first_seen, last_seen)"
        " VALUES ('k', 'a1', 'Route 20 detour', 1, 1)"
    )
    db.commit()
    db.close()

    out = tmp_path / "backup" / "meta.db"
    counts = export(db_path, out)

    assert counts == {"polls": 1, "alerts": 1, "static_versions": 0}
    meta = sqlite3.connect(out)
    tables = {r[0] for r in meta.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "trip_predictions" not in tables
    assert meta.execute("SELECT header FROM alerts").fetchone()[0] == "Route 20 detour"


def test_export_overwrites_previous_backup(tmp_path):
    db_path = tmp_path / "collector.db"
    open_db(db_path).close()
    out = tmp_path / "meta.db"
    export(db_path, out)
    export(db_path, out)  # second run replaces, doesn't fail on existing tables
    assert out.exists() and not out.with_suffix(".tmp").exists()
