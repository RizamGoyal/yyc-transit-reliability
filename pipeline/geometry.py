"""Route-line geometry: snap GPS points onto a route shape.

Coordinates are flattened to metres with an equirectangular projection around the
shape's centre. Over a city (~50 km) the distance error is well under 0.1%, far below
GPS noise (~5-15 m), and it keeps the maths to plain NumPy.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_M = 6_371_000.0


def to_xy(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    k = np.pi / 180 * EARTH_RADIUS_M
    x = (np.asarray(lon, dtype=float) - lon0) * k * np.cos(np.radians(lat0))
    y = (np.asarray(lat, dtype=float) - lat0) * k
    return x, y


@dataclass
class Polyline:
    """A route shape as segments, with cumulative distance (m) at each vertex."""

    lat0: float
    lon0: float
    x: np.ndarray
    y: np.ndarray
    cum: np.ndarray  # metres along the line at each vertex

    @classmethod
    def from_latlon(cls, lat, lon) -> Polyline:
        lat = np.asarray(lat, dtype=float)
        lon = np.asarray(lon, dtype=float)
        if len(lat) < 2:
            raise ValueError("a shape needs at least 2 points")
        lat0, lon0 = float(lat.mean()), float(lon.mean())
        x, y = to_xy(lat, lon, lat0, lon0)
        seg = np.hypot(np.diff(x), np.diff(y))
        return cls(lat0, lon0, x, y, np.concatenate([[0.0], np.cumsum(seg)]))

    @property
    def length(self) -> float:
        return float(self.cum[-1])

    def candidates(self, lat: float, lon: float) -> tuple[np.ndarray, np.ndarray]:
        """For one point: (distance along the line, perpendicular distance) to every segment."""
        px, py = to_xy(np.array([lat]), np.array([lon]), self.lat0, self.lon0)
        x0, y0 = self.x[:-1], self.y[:-1]
        vx, vy = np.diff(self.x), np.diff(self.y)
        seg_len2 = vx * vx + vy * vy
        with np.errstate(invalid="ignore", divide="ignore"):
            t = ((px - x0) * vx + (py - y0) * vy) / seg_len2
        t = np.clip(np.nan_to_num(t), 0.0, 1.0)  # zero-length segments -> t = 0
        off = np.hypot(px - (x0 + t * vx), py - (y0 + t * vy))
        along = self.cum[:-1] + t * np.sqrt(seg_len2)
        return along, off

    def passes(self, lat: float, lon: float, max_off: float) -> tuple[np.ndarray, np.ndarray]:
        """Every separate time the line passes within max_off of the point.

        Consecutive segments within reach form one "pass"; each pass is reduced to its
        closest point. On an out-and-back street this gives two candidates (outbound and
        return) instead of a smear of nearby segments.
        Returns (along, off) arrays, one entry per pass, in order along the line.
        """
        along, off = self.candidates(lat, lon)
        near = off <= max_off
        if not near.any():
            return np.array([]), np.array([])
        idx = np.flatnonzero(near)
        breaks = np.flatnonzero(np.diff(idx) > 1) + 1
        best = [run[np.argmin(off[run])] for run in np.split(idx, breaks)]
        return along[best], off[best]

    def locate(self, lat: float, lon: float, *, after: float, max_off: float,
               back_tol: float = 0.0) -> tuple[float, float] | None:
        """Distance along the line for a point: the EARLIEST pass at or after
        `after - back_tol`.

        "Earliest ahead" is what keeps a bus on the right leg of an out-and-back route:
        it can't jump onto the return leg or skip forward past part of the route.
        Returns (along, off) or None if the point isn't near the line ahead.
        """
        along, off = self.passes(lat, lon, max_off)
        ok = along >= after - back_tol
        if not ok.any():
            return None
        i = int(np.flatnonzero(ok)[0])
        return float(along[i]), float(off[i])
