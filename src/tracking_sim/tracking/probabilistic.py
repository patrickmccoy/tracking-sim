"""Probabilistic data association (PDA, JPDA) fitted to this project's radars.

Stone Soup's ``PDAHypothesiser`` weighs each detection in a track's gate
against the chance that none is the aircraft's, using one probability of
detection and one clutter density for the whole picture. Neither is
constant here:

* false alarms are uniform over ground area (``sensors.radar``), so in the
  radar's own (bearing, slant range) space their density grows with range:
  ``false_alarm_density * range`` per radian per metre per scan;
* radar-equation Pd falls with range.

``RadarPDAHypothesiser`` sets both per track from the radar that made the
detections, at the track's predicted range. It also predicts every
hypothesis for a track to one time. Stone Soup predicts the missed-detection
hypothesis to the batch time (the scan's end) and each detection's to its
own time, and the mixture tracker then blends them; with a rotating radar
those are up to a scan apart, about a kilometre at airliner speed. The
detections in one gate come from one pass of the beam, milliseconds apart,
so predicting the missed hypothesis to their time is exact enough.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from stonesoup.base import Property
from stonesoup.hypothesiser.probability import PDAHypothesiser
from stonesoup.types.numeric import Probability

from tracking_sim.sensors.detection import pd_swerling, snr_radar_equation
from tracking_sim.sensors.spec import RotatingRadar2DSpec

__all__ = ["RadarModel", "RadarPDAHypothesiser", "gate_probability"]

# Keep Pd off 0 and 1: at 1 a missed scan would be impossible.
_PD_RANGE = (0.01, 0.999)


def gate_probability(gate_sigmas: float, ndim: int = 2) -> float:
    """The chance a measurement falls within ``gate_sigmas`` (Mahalanobis) of its mean.

    For two dimensions this is ``1 - exp(-gate**2 / 2)``: 98.9% at 3, 99.97%
    at 4. Stone Soup's PDA hypothesiser gates on the inverse, so its gate is
    exactly ``gate_sigmas`` standard deviations.
    """
    from scipy.stats import chi2

    return float(chi2.cdf(gate_sigmas**2, ndim))


class RadarModel:
    """What a tracker may know of one radar: Pd and clutter density by range."""

    def __init__(self, spec: RotatingRadar2DSpec) -> None:
        self.spec = spec

    def clutter_density(self, slant_range_m: float) -> float:
        """Expected false alarms per radian of bearing per metre of range, per scan."""
        return self.spec.detection.false_alarm_density * max(slant_range_m, 1.0)

    def prob_detect(self, slant_range_m: float) -> float:
        """Single-scan Pd at this range, for an aircraft of the default class."""
        det = self.spec.detection
        if det.model == "constant":
            assert det.pd is not None
            pd = det.pd
        else:
            assert det.reference_snr_db is not None and det.reference_range_m is not None
            rcs = getattr(det.rcs_m2, det.default_class)
            snr = snr_radar_equation(
                slant_range_m, rcs, det.reference_snr_db, det.reference_range_m
            )
            pd = float(pd_swerling(snr, det.pfa, det.swerling))
        return min(max(pd, _PD_RANGE[0]), _PD_RANGE[1])


class RadarPDAHypothesiser(PDAHypothesiser):  # type: ignore[misc]
    """PDA hypotheses with Pd and clutter density from the detections' radar.

    ``radars`` maps sensor ids (``detection.metadata["sensor_id"]``) to their
    models. ``fixed_prob_detect`` and ``fixed_clutter_density`` override the
    radar's values when set. ``position_mapping`` picks x, y, z out of the
    track state for its range from the radar.
    """

    radars: Mapping[str, RadarModel] = Property(doc="Radar models by sensor id")
    position_mapping: tuple[int, int, int] = Property(doc="x, y, z indices in the state")
    fixed_prob_detect: float | None = Property(default=None, doc="Pd instead of the radar's")
    fixed_clutter_density: float | None = Property(
        default=None, doc="Clutter density instead of the radar's"
    )

    def hypothesise(self, track: Any, detections: Any, timestamp: Any, **kwargs: Any) -> Any:
        detections = list(detections)
        if detections:
            # One pass of the beam: predict "no detection" to the same moment.
            timestamp = max(d.timestamp for d in detections)
            pd, clutter = self._parameters(track, detections)
        else:
            # Only the missed-detection hypothesis, whose weight normalises to 1.
            pd, clutter = self.fixed_prob_detect or 0.9, self.fixed_clutter_density or 1e-9
        self.prob_detect = Probability(pd)
        self.clutter_spatial_density = clutter
        return super().hypothesise(track, detections, timestamp, **kwargs)

    def _parameters(self, track: Any, detections: list[Any]) -> tuple[float, float]:
        if self.fixed_prob_detect is not None and self.fixed_clutter_density is not None:
            return self.fixed_prob_detect, self.fixed_clutter_density
        first = detections[0]
        radar = self.radars.get(first.metadata.get("sensor_id", ""))
        if radar is None:
            raise ValueError(
                f"no radar {first.metadata.get('sensor_id')!r} for probabilistic association"
            )
        state = getattr(track, "state", track)
        position = np.asarray(state.mean, dtype=float).ravel()[list(self.position_mapping)]
        site = np.asarray(first.measurement_model.translation_offset, dtype=float).ravel()
        slant = float(math.dist(position, site))
        pd = self.fixed_prob_detect if self.fixed_prob_detect is not None else None
        clutter = self.fixed_clutter_density
        return (
            pd if pd is not None else radar.prob_detect(slant),
            clutter if clutter is not None else radar.clutter_density(slant),
        )
