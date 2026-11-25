"""Read the collector's hourly GPS files into a DataFrame."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd

COLUMNS = ["feed_ts", "vehicle_ts", "vehicle_id", "trip_id", "route_id", "start_date", "lat", "lon",
           "bearing", "speed"]


def load_positions(day_dirs: Iterable[Path]) -> pd.DataFrame:
    """All rows from positions/YYYY-MM-DD/HH.csv.gz for the given day folders.
    IDs stay text; empty strings become <NA>."""
    files = []
    for d in day_dirs:
        d = Path(d)
        if not d.is_dir():
            raise FileNotFoundError(f"folder not found: {d}")
        files += sorted(d.glob("*.csv.gz"))
    if not files:
        return pd.DataFrame({c: pd.Series(dtype="string") for c in COLUMNS})
    df = pd.concat((pd.read_csv(f, dtype=str, keep_default_na=False) for f in files), ignore_index=True)
    df = df[df["feed_ts"] != "feed_ts"]  # defensive: a stray header row
    for c in ["vehicle_id", "trip_id", "route_id", "start_date"]:
        df[c] = df[c].astype("string").str.strip().replace("", pd.NA)
    df["feed_ts"] = pd.to_numeric(df["feed_ts"]).astype("int64")
    df["vehicle_ts"] = pd.to_numeric(df["vehicle_ts"].replace({"": None})).astype("Int64")
    for c in ["lat", "lon", "bearing", "speed"]:
        df[c] = pd.to_numeric(df[c].replace({"": None})).astype("float64")
    return df.reset_index(drop=True)
