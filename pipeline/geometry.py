"""Route-line geometry: snap GPS points onto a route shape.

Coordinates are flattened to metres with an equirectangular projection around the
shape's centre. Over a city (~50 km) the distance error is well under 0.1%, far below
GPS noise (~5-15 m), and it keeps the maths to plain NumPy.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_M = 6_371_000.0
_EMPTY = np.array([])


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

    def __post_init__(self) -> None:
        # Segment geometry, computed once per shape (not once per GPS point).
        self._x0, self._y0 = self.x[:-1], self.y[:-1]
        self._vx, self._vy = np.diff(self.x), np.diff(self.y)
        self._len2 = self._vx * self._vx + self._vy * self._vy
        self._len = np.sqrt(self._len2)
        # 1/len^2, with 0 for zero-length segments (projection then lands on the segment start).
        self._inv_len2 = np.divide(1.0, self._len2, out=np.zeros_like(self._len2), where=self._len2 > 0)
        self._cum0 = self.cum[:-1]
        self._kx = np.pi / 180 * EARTH_RADIUS_M * np.cos(np.radians(self.lat0))
        self._ky = np.pi / 180 * EARTH_RADIUS_M

    @property
    def length(self) -> float:
        return float(self.cum[-1])

    def _segment_range(self, lo: float | None, hi: float | None) -> slice:
        """Segments overlapping [lo, hi] metres along the line (all segments if unbounded)."""
        n = len(self._len)
        i0 = 0 if lo is None else max(int(self.cum.searchsorted(lo, side="right")) - 1, 0)
        i1 = n if hi is None else min(int(self.cum.searchsorted(hi, side="left")) + 1, n)
        return slice(i0, max(i1, i0))

    def candidates(self, lat: float, lon: float, lo: float | None = None,
                   hi: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """For one point: (distance along the line, perpendicular distance) to each segment
        overlapping [lo, hi] metres along the line (every segment if no bounds)."""
        px = (lon - self.lon0) * self._kx
        py = (lat - self.lat0) * self._ky
        r = self._segment_range(lo, hi)
        x0, y0, vx, vy = self._x0[r], self._y0[r], self._vx[r], self._vy[r]
        # (np.minimum/np.maximum instead of np.clip: same result, far less per-call overhead;
        # this runs once per GPS ping, ~1M times a day.)
        t = np.minimum(np.maximum(((px - x0) * vx + (py - y0) * vy) * self._inv_len2[r], 0.0), 1.0)
        off = np.hypot(px - (x0 + t * vx), py - (y0 + t * vy))
        along = self._cum0[r] + t * self._len[r]
        return along, off

    def passes(self, lat: float, lon: float, max_off: float, lo: float | None = None,
               hi: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Every separate time the line passes within max_off of the point.

        Consecutive segments within reach form one "pass"; each pass is reduced to its
        closest point. On an out-and-back street this gives two candidates (outbound and
        return) instead of a smear of nearby segments.
        lo/hi limit the search to part of the line (a 2*max_off margin is added so a pass
        isn't cut in half at the edge).
        Returns (along, off) arrays, one entry per pass, in order along the line.
        """
        margin = 2 * max_off
        along, off = self.candidates(lat, lon, None if lo is None else lo - margin,
                                     None if hi is None else hi + margin)
        idx = (off <= max_off).nonzero()[0]
        if idx.size == 0:
            return _EMPTY, _EMPTY
        gaps = (idx[1:] - idx[:-1]) > 1
        if not gaps.any():  # the common case: one pass
            i = idx[off[idx].argmin()]
            return along[i : i + 1], off[i : i + 1]
        best = [run[off[run].argmin()] for run in np.split(idx, gaps.nonzero()[0] + 1)]
        return along[best], off[best]

    def locate(self, lat: float, lon: float, *, after: float, max_off: float,
               back_tol: float = 0.0, until: float | None = None) -> tuple[float, float] | None:
        """Distance along the line for a point: the EARLIEST pass at or after
        `after - back_tol`.

        "Earliest ahead" is what keeps a bus on the right leg of an out-and-back route:
        it can't jump onto the return leg or skip forward past part of the route.
        `until` (optional) is the furthest the vehicle could have got; passes beyond it are
        ignored, which also makes the search much faster on long shapes.
        Returns (along, off) or None if the point isn't near the line ahead.
        """
        lo = after - back_tol if np.isfinite(after) else None
        along, off = self.passes(lat, lon, max_off, lo, until)
        ok = along >= after - back_tol
        if until is not None:
            ok &= along <= until
        if not ok.any():
            return None
        i = int(ok.argmax())  # first True
        return float(along[i]), float(off[i])
