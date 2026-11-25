import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from heartbeat import check

from collector.collector import open_db, record_poll

NOW = 1_800_000_000


def make_db(tmp_path, polls):
    path = tmp_path / "collector.db"
    db = open_db(path)
    for t, ok, dup in polls:
        record_poll(db, "vehicles", ok=ok, duplicate=dup)
        db.execute("UPDATE polls SET polled_at = ? WHERE rowid = last_insert_rowid()", (t,))
    db.commit()
    db.close()
    return path


def test_missing_db_is_unhealthy(tmp_path):
    healthy, msg = check(tmp_path / "nope.db", NOW)
    assert not healthy and "not found" in msg


def test_fresh_data_is_healthy(tmp_path):
    path = make_db(tmp_path, [(NOW - 60, True, False)])
    healthy, _ = check(path, NOW)
    assert healthy


def test_old_data_is_stale(tmp_path):
    path = make_db(tmp_path, [(NOW - 2000, True, False)])
    healthy, msg = check(path, NOW)
    assert not healthy and "stale" in msg


def test_only_duplicates_recently_is_stale(tmp_path):
    # Feed frozen: we keep polling OK, but the City keeps serving the same file.
    path = make_db(tmp_path, [(NOW - 2000, True, False), (NOW - 30, True, True)])
    healthy, _ = check(path, NOW)
    assert not healthy


def test_only_failures_recently_is_stale(tmp_path):
    path = make_db(tmp_path, [(NOW - 2000, True, False), (NOW - 30, False, False)])
    healthy, _ = check(path, NOW)
    assert not healthy


def test_no_polls_is_unhealthy(tmp_path):
    path = make_db(tmp_path, [])
    healthy, msg = check(path, NOW)
    assert not healthy and "no successful" in msg


class FakeResp:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} error")


def run_main(monkeypatch, tmp_path, status):
    import heartbeat

    make_db(tmp_path, [(heartbeat.time.time() - 10, True, False)])
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.example/abc")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sent = []
    monkeypatch.setattr(heartbeat.requests, "post", lambda url, **kw: sent.append(url) or FakeResp(status))
    return heartbeat.main(), sent


def test_main_pings_success_url(monkeypatch, tmp_path):
    code, sent = run_main(monkeypatch, tmp_path, 200)
    assert code == 0 and sent == ["https://hc-ping.example/abc"]


def test_main_rejected_ping_is_an_error(monkeypatch, tmp_path):
    code, _ = run_main(monkeypatch, tmp_path, 400)
    assert code == 1
