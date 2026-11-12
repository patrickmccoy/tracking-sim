"""A cheap pre-gate in front of Stone Soup's hypothesiser.

Stone Soup's ``DistanceHypothesiser`` predicts every track to every
detection's time and computes the Mahalanobis distance, pair by pair in
Python. With a few dozen false alarms a scan, and a tentative track started
on each of them, that is millions of pairs an hour. Nearly all are
hopeless: a track 80 km away cannot take a detection here.

``PreGatedHypothesiser`` first extrapolates each track's mean to each
detection's time, converts it to the detection's (bearing, slant range),
and keeps only detections within a generous box: a fixed margin, plus three
sigma of the track's horizontal position, plus what the fastest aircraft
could cover in the time since the track's last state. The exact gate is
still Stone Soup's Mahalanobis distance on the survivors, so as long as the
box is wider than that gate the pre-gate changes speed, not results.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from stonesoup.base import Property
from stonesoup.hypothesiser.base import Hypothesiser

from tracking_sim.sensors.measurement import CartesianToBearingSlantRange

__all__ = ["PreGatedHypothesiser"]


class PreGatedHypothesiser(Hypothesiser):  # type: ignore[misc]
    """Pass only plausible detections on to ``hypothesiser``."""

    hypothesiser: Hypothesiser = Property(doc="The exact hypothesiser to run on survivors")
    position_mapping: tuple[int, int, int] = Property(doc="x, y, z indices in the state")
    velocity_mapping: tuple[int, int, int] = Property(doc="vx, vy, vz indices in the state")
    margin_m: float = Property(default=2000.0, doc="Fixed part of the box half-width")
    max_speed_mps: float = Property(default=400.0, doc="Fastest target speed to allow for")

    def hypothesise(self, track: Any, detections: Any, timestamp: Any, **kwargs: Any) -> Any:
        return self.hypothesiser.hypothesise(
            track, self.candidates(track, detections), timestamp, **kwargs
        )

    def candidates(self, track: Any, detections: Any) -> set[Any]:
        detections = list(detections)
        if not detections:
            return set()
        state = track.state
        mean = np.asarray(state.mean, dtype=float).ravel()
        pos = mean[list(self.position_mapping)]
        vel = mean[list(self.velocity_mapping)]
        cov = np.asarray(state.covar, dtype=float)
        ix, iy = self.position_mapping[0], self.position_mapping[1]
        spread = 3.0 * np.sqrt(max(cov[ix, ix], cov[iy, iy], 0.0))
        t0 = state.timestamp
        dt = np.array([(d.timestamp - t0).total_seconds() for d in detections])
        predicted = pos[None, :] + dt[:, None] * vel[None, :]
        half_width = self.margin_m + spread + self.max_speed_mps * np.abs(dt)

        keep = np.zeros(len(detections), dtype=bool)
        by_model: dict[int, list[int]] = {}
        for i, d in enumerate(detections):
            by_model.setdefault(id(d.measurement_model), []).append(i)
        for indices in by_model.values():
            model = detections[indices[0]].measurement_model
            if not isinstance(model, CartesianToBearingSlantRange):
                keep[indices] = True  # unknown geometry: let the exact gate decide
                continue
            idx = np.array(indices)
            offset = np.asarray(model.translation_offset, dtype=float).ravel()
            local = (predicted[idx] - offset) @ np.asarray(model.rotation_matrix, dtype=float).T
            p_bearing = np.arctan2(local[:, 1], local[:, 0])
            p_range = np.linalg.norm(local, axis=1)
            meas = np.array(
                [
                    [
                        float(detections[i].state_vector[0, 0]),
                        float(detections[i].state_vector[1, 0]),
                    ]
                    for i in indices
                ]
            )
            d_bearing = np.abs((meas[:, 0] - p_bearing + np.pi) % (2 * np.pi) - np.pi)
            d_range = np.abs(meas[:, 1] - p_range)
            cross = d_bearing * np.maximum(meas[:, 1], p_range)
            keep[idx] = (d_range < half_width[idx]) & (cross < half_width[idx])
        return {d for d, k in zip(detections, keep, strict=True) if k}
