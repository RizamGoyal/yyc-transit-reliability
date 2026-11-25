"""Read the collector's hourly GPS files into a DataFrame.

Memory matters (a service day is ~1M rows and the VM has 1 GB): only the columns the
pipeline uses are read, IDs are categorical (each distinct value stored once), and
categories are unified across files before concatenating, because concatenating
categoricals with different categories silently falls back to plain strings.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd
from pandas.api.types import union_categoricals

COLUMNS = ["feed_ts", "vehicle_ts", "vehicle_id", "trip_id", "route_id", "start_date", "lat", "lon",
           "bearing", "speed"]
DTYPES = {"feed_ts": "int64", "vehicle_ts": "Int64", "vehicle_id": "category", "trip_id": "category",
          "start_date": "category", "lat": "float64", "lon": "float64"}
CATEGORICAL = [c for c, t in DTYPES.items() if t == "category"]


def load_files(files: Iterable[Path]) -> pd.DataFrame:
    """Rows from the given hourly .csv.gz files (only the columns the pipeline uses)."""
    frames = [pd.read_csv(f, usecols=list(DTYPES), dtype=DTYPES) for f in files]
    if not frames:
        return pd.DataFrame({c: pd.Series(dtype=t) for c, t in DTYPES.items()})
    for c in CATEGORICAL:
        cats = union_categoricals([f[c] for f in frames]).categories
        for f in frames:
            f[c] = f[c].cat.set_categories(cats)
    return pd.concat(frames, ignore_index=True)


def load_positions(day_dirs: Iterable[Path]) -> pd.DataFrame:
    """All rows from positions/YYYY-MM-DD/HH.csv.gz for the given day folders."""
    files = []
    for d in day_dirs:
        d = Path(d)
        if not d.is_dir():
            raise FileNotFoundError(f"folder not found: {d}")
        files += sorted(d.glob("*.csv.gz"))
    return load_files(files)
