"""Smoothed, interpolable trajectories for flight segments.

A ``Trajectory`` fits each ENU axis of one segment with a cubic smoothing
spline (``scipy.interpolate.make_smoothing_spline``). By default the
smoothing parameter is chosen by generalised cross-validation, so it adapts
to each segment's noise and manoeuvres. Velocity is the spline's derivative.

A trajectory is only defined between its first and last observation: there
is no extrapolation. Asking for a time outside the segment returns ``None``
(or a False entry in the validity mask).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.interpolate import make_smoothing_spline

__all__ = ["MIN_SPLINE_POINTS", "Trajectory", "TruthState", "to_posix"]

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]

MIN_SPLINE_POINTS = 5


def to_posix(t: datetime | float | ArrayLike) -> FloatArray:
    """Datetimes (or POSIX seconds) to a float array of POSIX seconds."""
    if isinstance(t, datetime):
        dt = t if t.tzinfo is not None else t.replace(tzinfo=UTC)
        return np.array([dt.timestamp()])
    arr = np.atleast_1d(np.asarray(t))
    if arr.dtype == object:
        return np.array(
            [
                (v if v.tzinfo is not None else v.replace(tzinfo=UTC)).timestamp()
                if isinstance(v, datetime)
                else float(v)
                for v in arr
            ]
        )
    if np.issubdtype(arr.dtype, np.datetime64):
        return arr.astype("datetime64[ns]").astype(np.int64) / 1e9
    return arr.astype(float)


@dataclass(frozen=True)
class TruthState:
    """Position and velocity in the local ENU frame at one time."""

    ts: datetime
    position: FloatArray  # (3,) metres
    velocity: FloatArray  # (3,) m/s

    @property
    def state_vector(self) -> FloatArray:
        """Stone Soup ordering: [x, vx, y, vy, z, vz]."""
        p, v = self.position, self.velocity
        return np.array([p[0], v[0], p[1], v[1], p[2], v[2]])

    @property
    def speed_mps(self) -> float:
        return float(np.hypot(self.velocity[0], self.velocity[1]))


class Trajectory:
    """A smoothed fit through one segment's ENU observations.

    ``method="spline"`` fits a cubic smoothing spline per axis (with ``lam``
    fixed, or ``None`` for cross-validation). Segments with fewer than
    ``MIN_SPLINE_POINTS`` points, and ``method="linear"``, interpolate
    linearly with piecewise-constant velocity.
    """

    def __init__(
        self,
        t: ArrayLike,
        enu: ArrayLike,
        *,
        method: Literal["spline", "linear"] = "spline",
        lam: float | None = None,
    ) -> None:
        self.t = np.asarray(t, dtype=float)
        self.enu = np.asarray(enu, dtype=float)
        if self.t.ndim != 1 or self.enu.shape != (len(self.t), 3):
            raise ValueError("t must be (n,) and enu (n, 3)")
        if len(self.t) < 2 or np.any(np.diff(self.t) <= 0):
            raise ValueError("need at least two strictly increasing times")
        self.t0 = float(self.t[0])
        self.t1 = float(self.t[-1])
        self.method: Literal["spline", "linear"] = (
            "spline" if method == "spline" and len(self.t) >= MIN_SPLINE_POINTS else "linear"
        )
        self._splines: list[Any] = []
        self._derivatives: list[Any] = []
        if self.method == "spline":
            x = self.t - self.t0
            self._splines = [make_smoothing_spline(x, self.enu[:, k], lam=lam) for k in range(3)]
            self._derivatives = [s.derivative() for s in self._splines]

    @property
    def start(self) -> datetime:
        return datetime.fromtimestamp(self.t0, UTC)

    @property
    def end(self) -> datetime:
        return datetime.fromtimestamp(self.t1, UTC)

    def covers(self, t: datetime | float | ArrayLike) -> BoolArray:
        tt = to_posix(t)
        covered: BoolArray = (tt >= self.t0) & (tt <= self.t1)
        return covered

    def evaluate(self, t: datetime | float | ArrayLike) -> tuple[FloatArray, FloatArray, BoolArray]:
        """Positions (n, 3), velocities (n, 3) and a validity mask at ``t``.

        Entries outside the segment are NaN and their mask entry is False.
        """
        tt = to_posix(t)
        valid = self.covers(tt)
        pos = np.full((len(tt), 3), np.nan)
        vel = np.full((len(tt), 3), np.nan)
        if not valid.any():
            return pos, vel, valid
        inside = tt[valid]
        if self.method == "spline":
            x = inside - self.t0
            pos[valid] = np.column_stack([s(x) for s in self._splines])
            vel[valid] = np.column_stack([d(x) for d in self._derivatives])
        else:
            pos[valid] = np.column_stack(
                [np.interp(inside, self.t, self.enu[:, k]) for k in range(3)]
            )
            seg_vel = np.diff(self.enu, axis=0) / np.diff(self.t)[:, None]
            idx = np.clip(np.searchsorted(self.t, inside, side="right") - 1, 0, len(seg_vel) - 1)
            vel[valid] = seg_vel[idx]
        return pos, vel, valid

    def state_at(self, t: datetime | float) -> TruthState | None:
        """The state at one time, or ``None`` outside the segment."""
        pos, vel, valid = self.evaluate(t)
        if not valid[0]:
            return None
        ts = t if isinstance(t, datetime) else datetime.fromtimestamp(float(t), UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return TruthState(ts=ts, position=pos[0], velocity=vel[0])
