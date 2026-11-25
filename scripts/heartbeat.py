"""Heartbeat: tell healthchecks.io the collector is alive, but only if data is fresh.

Run every 5 minutes by deploy/heartbeat.timer.

- Fresh data (a new, non-duplicate vehicle feed saved in the last MAX_AGE seconds)
  -> ping HEALTHCHECK_URL            (check stays green)
- Stale data (collector stuck, crashed, or the City feed frozen)
  -> ping HEALTHCHECK_URL/fail       (healthchecks.io emails right away)
- VM down / no network / this script broken
  -> no ping at all                  (healthchecks.io emails after the grace period)

That last case is why the alert lives outside the VM: a machine that is off
can't send an email saying it's off.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

MAX_AGE = int(os.environ.get("HEARTBEAT_MAX_AGE", "900"))  # 15 min


def last_fresh_vehicle_poll(db_path: Path) -> int | None:
    """Unix time of the newest successful, non-duplicate vehicle poll (read-only)."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        row = db.execute(
            "SELECT MAX(polled_at) FROM polls WHERE feed = 'vehicles' AND ok = 1 AND duplicate = 0"
        ).fetchone()
    finally:
        db.close()
    return row[0] if row else None


def check(db_path: Path, now: float, max_age: int = MAX_AGE) -> tuple[bool, str]:
    """Return (healthy, message)."""
    if not db_path.exists():
        return False, f"database not found: {db_path}"
    try:
        last = last_fresh_vehicle_poll(db_path)
    except sqlite3.Error as e:
        return False, f"database error: {e}"
    if last is None:
        return False, "no successful vehicle polls yet"
    age = int(now - last)
    if age > max_age:
        return False, f"stale: last new vehicle data {age}s ago (limit {max_age}s)"
    return True, f"ok: last new vehicle data {age}s ago"


def main() -> int:
    url = os.environ.get("HEALTHCHECK_URL", "").rstrip("/")
    if not url:
        print("HEALTHCHECK_URL not set; nothing to do", file=sys.stderr)
        return 1
    db_path = Path(os.environ.get("DATA_DIR", "data")) / "collector.db"
    healthy, msg = check(db_path, time.time())
    target = url if healthy else f"{url}/fail"
    try:
        # The message body shows up in the healthchecks.io event log.
        resp = requests.post(target, data=msg.encode(), timeout=10)
        resp.raise_for_status()  # a 400 (bad URL) must not look like success
    except requests.RequestException as e:
        print(f"ping failed: {e}", file=sys.stderr)
        return 1
    print(msg)
    return 0 if healthy else 2


if __name__ == "__main__":
    sys.exit(main())
