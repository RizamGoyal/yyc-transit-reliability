"""
Export the small, irreplaceable tables from collector.db into a separate file for backup.

    python scripts/export_meta.py data/collector.db data/backup/meta.db

Copies: polls (feed health history), alerts, static_versions.
Skips: trip_predictions (large, secondary cross-check data).
Safe to run while the collector is writing (reads in read-only mode).
"""

import sqlite3
import sys
from pathlib import Path

TABLES = ("polls", "alerts", "static_versions")


def export(src_path: Path, out_path: Path) -> dict[str, int]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    out = sqlite3.connect(tmp)
    counts = {}
    for table in TABLES:
        (schema,) = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        out.execute(schema)
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        placeholders = ",".join("?" * len(cols))
        rows = src.execute(f"SELECT * FROM {table}")
        out.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
        counts[table] = out.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    out.commit()
    out.close()
    src.close()
    tmp.replace(out_path)  # swap in only once the export is complete
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: export_meta.py <collector.db> <meta.db>")
    result = export(Path(sys.argv[1]), Path(sys.argv[2]))
    print("exported", ", ".join(f"{t}={n:,}" for t, n in result.items()))
