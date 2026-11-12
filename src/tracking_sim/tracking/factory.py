"""Build Stone Soup trackers from ``TrackerSpec``s.

``build_tracker(spec)`` returns a ``TrackerPipeline``: the Stone Soup
components for that spec, plus what a runner needs to feed it.

State layouts:

* ``ekf`` and ``ukf``: ``[x, vx, y, vy, z, vz]`` (positions at 0, 2, 4).
* ``particle_mm``: ``[x, vx, y, vy, w, z, vz]`` (positions at 0, 2, 5),
  with turn rate ``w`` placed where Stone Soup's ``ConstantTurn`` expects
  it. The constant-velocity model maps every dimension but ``w``, which
  Stone Soup's multiple-model predictor then holds at zero, so a particle
  that switches to the turn model starts from straight flight.

Every tracker confirms tracks the same way: tentative tracks start from
one detection at an assumed height (``SlantRangeInitiator``), run on an
extended Kalman filter with the same motion model, and are confirmed after
``min_points`` updates. The particle tracker samples particles only then.

Detections carry the 6D slant-range model the sensor built. A 7D tracker
needs the same model mapped to its own state, which ``adapt`` provides.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from stonesoup.dataassociator.neighbour import GNNWith2DAssignment
from stonesoup.deleter.time import UpdateTimeStepsDeleter
from stonesoup.hypothesiser.distance import DistanceHypothesiser
from stonesoup.initiator.simple import MultiMeasurementInitiator
from stonesoup.measures import Mahalanobis
from stonesoup.models.transition.base import CombinedGaussianTransitionModel
from stonesoup.models.transition.linear import (
    CombinedLinearGaussianTransitionModel,
    ConstantVelocity,
    RandomWalk,
)
from stonesoup.models.transition.nonlinear import ConstantTurn
from stonesoup.predictor.kalman import ExtendedKalmanPredictor, UnscentedKalmanPredictor
from stonesoup.predictor.particle import MultiModelPredictor
from stonesoup.resampler.particle import ESSResampler
from stonesoup.tracker.simple import MultiTargetTracker
from stonesoup.types.array import CovarianceMatrix, StateVector
from stonesoup.types.detection import Clutter, Detection, TrueDetection
from stonesoup.types.state import GaussianState
from stonesoup.updater.kalman import ExtendedKalmanUpdater, UnscentedKalmanUpdater
from stonesoup.updater.particle import MultiModelParticleUpdater

from tracking_sim.sensors.measurement import CartesianToBearingSlantRange
from tracking_sim.tracking.gating import PreGatedHypothesiser
from tracking_sim.tracking.initiators import ParticleConvertingInitiator, SlantRangeInitiator
from tracking_sim.tracking.spec import TrackerSpec

__all__ = ["TrackerPipeline", "build_tracker"]


@dataclass(frozen=True)
class StateLayout:
    """Where position, velocity and turn rate sit in a tracker's state vector."""

    position: tuple[int, int, int]
    velocity: tuple[int, int, int]
    turn_rate: int | None = None


LAYOUT_6D = StateLayout(position=(0, 2, 4), velocity=(1, 3, 5))
LAYOUT_7D = StateLayout(position=(0, 2, 5), velocity=(1, 3, 6), turn_rate=4)


@dataclass
class TrackerPipeline:
    """A tracker's Stone Soup components and the wiring to run them."""

    spec: TrackerSpec
    predictor: Any
    updater: Any
    data_associator: Any
    initiator: Any
    deleter: Any
    position_mapping: tuple[int, int, int]
    velocity_mapping: tuple[int, int, int]
    _models: dict[int, Any] = field(default_factory=dict, repr=False)
    # id(original) -> (original, copy); holding the original keeps its id unique.
    _detections: dict[int, tuple[Detection, Detection]] = field(default_factory=dict, repr=False)

    @property
    def ndim_state(self) -> int:
        return self.spec.ndim_state

    def make_tracker(self, detector: Iterable[tuple[Any, set[Detection]]]) -> MultiTargetTracker:
        """A Stone Soup ``MultiTargetTracker`` reading ``(time, detections)`` batches."""
        return MultiTargetTracker(
            initiator=self.initiator,
            deleter=self.deleter,
            detector=detector,
            data_associator=self.data_associator,
            updater=self.updater,
        )

    def adapt(self, detections: Iterable[Detection]) -> set[Detection]:
        """Detections with measurement models mapped to this tracker's state.

        For a 6D tracker the sensor's own models fit and detections pass
        through unchanged. A 7D tracker gets copies whose models map position
        to (0, 2, 5); copies are cached, so the same input gives the same
        object every time.
        """
        if self.position_mapping == LAYOUT_6D.position:
            return set(detections)
        return {self._adapt_one(d) for d in detections}

    def _adapt_one(self, detection: Detection) -> Detection:
        cached = self._detections.get(id(detection))
        if cached is not None:
            return cached[1]
        model = self._model_for(detection.measurement_model)
        if isinstance(detection, TrueDetection):
            copy: Detection = TrueDetection(
                state_vector=detection.state_vector,
                timestamp=detection.timestamp,
                measurement_model=model,
                groundtruth_path=detection.groundtruth_path,
                metadata=detection.metadata,
            )
        elif isinstance(detection, Clutter):
            copy = Clutter(
                state_vector=detection.state_vector,
                timestamp=detection.timestamp,
                measurement_model=model,
                metadata=detection.metadata,
            )
        else:
            copy = Detection(
                state_vector=detection.state_vector,
                timestamp=detection.timestamp,
                measurement_model=model,
                metadata=detection.metadata,
            )
        self._detections[id(detection)] = (detection, copy)
        return copy

    def _model_for(self, model: Any) -> Any:
        cached = self._models.get(id(model))
        if cached is None:
            if not isinstance(model, CartesianToBearingSlantRange):
                raise TypeError(f"can't map a {type(model).__name__} to a 7D state")
            cached = CartesianToBearingSlantRange(
                ndim_state=self.ndim_state,
                mapping=self.position_mapping,
                noise_covar=model.covar(),
                translation_offset=model.translation_offset,
                frame_rotation=model.rotation_matrix,
                inverse_height_m=model.inverse_height_m,
            )
            self._models[id(model)] = cached
        return cached


def _cv(spec: TrackerSpec) -> CombinedLinearGaussianTransitionModel:
    m = spec.motion
    return CombinedLinearGaussianTransitionModel(
        [
            ConstantVelocity(m.acceleration_noise),
            ConstantVelocity(m.acceleration_noise),
            ConstantVelocity(m.vertical_acceleration_noise),
        ]
    )


def _turn_noise(spec: TrackerSpec) -> float:
    return float(np.radians(np.sqrt(spec.motion.turn_rate_noise_deg)) ** 2)


def _prior(spec: TrackerSpec) -> GaussianState:
    init = spec.initiation
    n = spec.ndim_state
    layout = LAYOUT_7D if n == 7 else LAYOUT_6D
    variances = np.zeros(n)
    vx, vy, vz = layout.velocity
    variances[[vx, vy]] = init.speed_sigma_mps**2
    variances[vz] = init.vertical_speed_sigma_mps**2
    if n == 7:
        variances[4] = np.radians(init.turn_rate_sigma_deg) ** 2  # LAYOUT_7D.turn_rate
    return GaussianState(StateVector(np.zeros(n)), CovarianceMatrix(np.diag(variances)))


def _gated(spec: TrackerSpec, predictor: Any, updater: Any) -> PreGatedHypothesiser:
    layout = LAYOUT_7D if spec.ndim_state == 7 else LAYOUT_6D
    return PreGatedHypothesiser(
        hypothesiser=DistanceHypothesiser(
            predictor, updater, Mahalanobis(), missed_distance=spec.association.gate
        ),
        position_mapping=layout.position,
        velocity_mapping=layout.velocity,
    )


def build_tracker(spec: TrackerSpec) -> TrackerPipeline:
    """Assemble the Stone Soup components ``spec`` describes."""
    layout = LAYOUT_7D if spec.ndim_state == 7 else LAYOUT_6D

    # Tentative tracks: an EKF with the tracker's own state layout.
    if spec.ndim_state == 7:
        m = spec.motion
        tentative_motion: Any = CombinedLinearGaussianTransitionModel(
            [
                ConstantVelocity(m.acceleration_noise),
                ConstantVelocity(m.acceleration_noise),
                RandomWalk(_turn_noise(spec)),
                ConstantVelocity(m.vertical_acceleration_noise),
            ]
        )
    else:
        tentative_motion = _cv(spec)
    tentative_predictor = ExtendedKalmanPredictor(tentative_motion)
    tentative_updater = ExtendedKalmanUpdater(measurement_model=None)
    confirm = MultiMeasurementInitiator(
        prior_state=_prior(spec),
        deleter=UpdateTimeStepsDeleter(spec.initiation.tentative_missed_scans),
        data_associator=GNNWith2DAssignment(_gated(spec, tentative_predictor, tentative_updater)),
        updater=tentative_updater,
        min_points=spec.initiation.min_points,
        initiator=SlantRangeInitiator(
            prior_state=_prior(spec),
            height_m=spec.initiation.assumed_height_m,
            height_sigma_m=spec.initiation.height_sigma_m,
        ),
    )

    predictor: Any
    updater: Any
    initiator: Any = confirm
    if spec.filter == "ekf":
        predictor = ExtendedKalmanPredictor(_cv(spec))
        updater = ExtendedKalmanUpdater(measurement_model=None)
    elif spec.filter == "ukf":
        predictor = UnscentedKalmanPredictor(_cv(spec))
        updater = UnscentedKalmanUpdater(measurement_model=None)
    else:
        m = spec.motion
        straight = _cv(spec)
        turning = CombinedGaussianTransitionModel(
            [
                ConstantTurn([m.acceleration_noise, m.acceleration_noise], _turn_noise(spec)),
                ConstantVelocity(m.vertical_acceleration_noise),
            ]
        )
        p = m.switch_probability
        predictor = MultiModelPredictor(
            transition_models=[straight, turning],
            transition_matrix=np.array([[1 - p, p], [p, 1 - p]]),
            model_mappings=[(0, 1, 2, 3, 5, 6), (0, 1, 2, 3, 4, 5, 6)],
        )
        updater = MultiModelParticleUpdater(
            measurement_model=None, predictor=predictor, resampler=ESSResampler()
        )
        initiator = ParticleConvertingInitiator(
            initiator=confirm, number_particles=spec.particle_count
        )

    return TrackerPipeline(
        spec=spec,
        predictor=predictor,
        updater=updater,
        data_associator=GNNWith2DAssignment(_gated(spec, predictor, updater)),
        initiator=initiator,
        deleter=UpdateTimeStepsDeleter(spec.deletion.missed_scans),
        position_mapping=layout.position,
        velocity_mapping=layout.velocity,
    )
