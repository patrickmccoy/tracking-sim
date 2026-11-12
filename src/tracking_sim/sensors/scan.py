"""When a rotating beam crosses each target.

A rotating radar does not see everything at the scan start. Its beam sweeps
round once per period, and an aircraft is observed at the moment the beam
crosses its bearing; an aircraft at 90 degrees is seen a quarter of a period
later than one at north. Getting this right matters: at 250 m/s a quarter
of a 5 s scan is 300 m of travel, several times a radar's range noise.

The beam's phase relative to a target,

    phi(t) = (t - t_ref) / T - s * (target_azimuth(t) - start_azimuth) / 2 pi

(``s`` is +1 for clockwise rotation, -1 for counterclockwise), grows by
about one per rotation, and the beam crosses the target wherever ``phi`` is
an integer. ``BeamSchedule.crossings`` finds those integers on a coarse time
grid of the target's azimuth (unwrapped, so passing north is not a jump)
and refines each crossing with a few fixed-point iterations against the
target's real azimuth, which converge because aircraft turn round a radar
far slower than its antenna does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

__all__ = ["BeamSchedule"]

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]

TWO_PI = 2 * np.pi


@dataclass(frozen=True)
class BeamSchedule:
    """A beam pointing at ``start_azimuth`` (compass radians) at ``t_ref``,
    turning once every ``period_s``."""

    t_ref: float
    period_s: float
    start_azimuth: float = 0.0
    clockwise: bool = True

    @property
    def _sign(self) -> float:
        return 1.0 if self.clockwise else -1.0

    def azimuth_at(self, t: ArrayLike) -> FloatArray:
        """Compass azimuth of the beam at ``t`` (POSIX seconds), in ``[0, 2*pi)``."""
        turns = (np.asarray(t, dtype=float) - self.t_ref) / self.period_s
        azimuth: FloatArray = np.mod(self.start_azimuth + self._sign * TWO_PI * turns, TWO_PI)
        return azimuth

    def scan_index(self, t: ArrayLike) -> IntArray:
        """Which rotation ``t`` falls in; scan 0 starts at ``t_ref``."""
        index: IntArray = np.floor(
            (np.asarray(t, dtype=float) - self.t_ref) / self.period_s
        ).astype(np.int64)
        return index

    def scan_start(self, k: ArrayLike) -> FloatArray:
        start: FloatArray = self.t_ref + np.asarray(k, dtype=float) * self.period_s
        return start

    def time_at_azimuth(self, k: ArrayLike, azimuth: ArrayLike) -> FloatArray:
        """When the beam points at compass ``azimuth`` during scan ``k``."""
        relative = np.asarray(azimuth, dtype=float) - self.start_azimuth
        offset = np.mod(self._sign * relative, TWO_PI)
        t: FloatArray = self.scan_start(k) + self.period_s * offset / TWO_PI
        return t

    def crossings(
        self,
        t: ArrayLike,
        azimuth: ArrayLike,
        azimuth_fn: Callable[[FloatArray], FloatArray],
        *,
        max_iterations: int = 20,
        tolerance_s: float = 1e-6,
    ) -> FloatArray:
        """Times the beam crosses a target, within ``[t[0], t[-1]]``.

        ``t`` and ``azimuth`` sample the target's compass azimuth (radians)
        on a grid fine enough that it moves less than half a turn between
        samples; a step of half the scan period is plenty for aircraft.
        ``azimuth_fn`` evaluates the target's azimuth at any times inside
        the grid and refines each crossing.
        """
        tt = np.asarray(t, dtype=float)
        if len(tt) < 2:
            return np.array([])
        az = np.unwrap(np.asarray(azimuth, dtype=float))
        phase = self._phase(tt, az)
        # Integers n with phase[i] < n <= phase[i+1] cross in that interval.
        lo, hi = np.floor(phase[:-1]), np.floor(phase[1:])
        counts = (hi - lo).astype(np.int64)
        if counts.sum() <= 0:
            return np.array([])
        idx = np.repeat(np.arange(len(counts)), np.maximum(counts, 0))
        n = np.concatenate([lo[i] + np.arange(1, c + 1) for i, c in enumerate(counts) if c > 0])
        # Linear interpolation of the crossing inside each grid interval...
        frac = (n - phase[idx]) / (phase[idx + 1] - phase[idx])
        crossing = tt[idx] + frac * (tt[idx + 1] - tt[idx])
        guide = az[idx] + frac * (az[idx + 1] - az[idx])
        # ...then fixed-point refinement against the real azimuth. Each step
        # shrinks the error by (target turn rate / antenna turn rate).
        for _ in range(max_iterations):
            inside = np.clip(crossing, tt[0], tt[-1])
            real = np.asarray(azimuth_fn(inside), dtype=float)
            # Put each real azimuth on the same branch as the unwrapped guide.
            real = real + TWO_PI * np.round((guide - real) / TWO_PI)
            previous = crossing
            crossing = self.t_ref + self.period_s * (
                n + self._sign * (real - self.start_azimuth) / TWO_PI
            )
            guide = real
            if np.max(np.abs(crossing - previous)) < tolerance_s:
                break
        keep = (crossing >= tt[0]) & (crossing <= tt[-1])
        result: FloatArray = crossing[keep]
        return result

    def _phase(self, t: FloatArray, unwrapped_azimuth: FloatArray) -> FloatArray:
        phase: FloatArray = (t - self.t_ref) / self.period_s - self._sign * (
            unwrapped_azimuth - self.start_azimuth
        ) / TWO_PI
        return phase
