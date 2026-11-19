"""
Collector health report. Safe to run while the collector is running.

    python scripts/health.py            # last 1 hour
    python scripts/health.py --hours 24 # last 24 hours

Paste the output into the chat when asked.
"""

import argparse
import itertools
import os
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1)
    args = ap.parse_args()
    since = int(time.time() - args.hours * 3600)

    db_path = DATA_DIR / "collector.db"
    if not db_path.exists():
        print(f"No database at {db_path.resolve()} - has the collector run yet?")
        return
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    print(f"=== Collector health, last {args.hours:g} h ===")
    print(f"{'feed':<9} {'polls':>6} {'ok%':>6} {'dupes':>6} {'avg ents':>9} {'last ok':>10} {'max gap':>9}")
    for feed in ("vehicles", "trips", "alerts", "static"):
        rows = db.execute(
            "SELECT polled_at, ok, duplicate, entities FROM polls"
            " WHERE feed=? AND polled_at>=? ORDER BY polled_at",
            (feed, since),
        ).fetchall()
        if not rows:
            print(f"{feed:<9} {'no polls':>6}")
            continue
        ok_rows = [r for r in rows if r[1]]
        ok_pct = 100 * len(ok_rows) / len(rows)
        dupes = sum(r[2] for r in rows)
        ents = [r[3] for r in ok_rows if r[3] is not None]
        avg_ents = sum(ents) / len(ents) if ents else 0
        last_ok = f"{int(time.time() - ok_rows[-1][0])}s ago" if ok_rows else "never"
        gaps = [b[0] - a[0] for a, b in itertools.pairwise(ok_rows)]
        max_gap = f"{max(gaps)}s" if gaps else "-"
        print(f"{feed:<9} {len(rows):>6} {ok_pct:>5.0f}% {dupes:>6} {avg_ents:>9.0f}"
              f" {last_ok:>10} {max_gap:>9}")

    errors = db.execute(
        "SELECT feed, http_status, error, COUNT(*) FROM polls WHERE ok=0 AND polled_at>=?"
        " GROUP BY feed, http_status, error ORDER BY COUNT(*) DESC LIMIT 5",
        (since,),
    ).fetchall()
    if errors:
        print("\nTop errors:")
        for feed, status, err, n in errors:
            print(f"  {n}x {feed} HTTP {status}: {(err or '')[:120]}")

    print("\n=== Stored data ===")
    pos_files = list((DATA_DIR / "positions").rglob("*.csv.gz"))
    print(f"GPS files: {len(pos_files)}, total {human(sum(f.stat().st_size for f in pos_files))}")
    if pos_files:
        newest = max(pos_files, key=lambda f: f.stat().st_mtime)
        print(f"Newest GPS file: {newest} ({human(newest.stat().st_size)})")
    print(f"Trip predictions stored: {db.execute('SELECT COUNT(*) FROM trip_predictions').fetchone()[0]:,}")
    print(f"Unique alerts stored: {db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]}")
    for sha, _at, path, nbytes in db.execute("SELECT * FROM static_versions ORDER BY downloaded_at"):
        print(f"Schedule version {sha[:8]}: {path} ({human(nbytes)})")
    print(f"Database size: {human(db_path.stat().st_size)}")

    total = sum(f.stat().st_size for f in DATA_DIR.rglob("*") if f.is_file())
    print(f"Total data folder: {human(total)}")


if __name__ == "__main__":
    main()
