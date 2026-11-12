"""Track initiation for a 2D radar that measures no height.

``SlantRangeInitiator`` starts a tentative track from one detection. A 2D
radar gives bearing and slant range only, so the new track is placed at an
assumed height above the antenna, and the starting covariance is the
linearisation of position in (bearing, slant range, height) with the
height's own uncertainty included, rather than Stone Soup's default, which
would derive everything from the measurement and so give height almost no
variance. Velocity starts at zero with a wide spread.

``ParticleConvertingInitiator`` wraps a multi-measurement initiator so that
tentative tracks run on a cheap Kalman filter, and a track is only turned
into a multiple-model particle state once it is confirmed. Particles are
sampled from a generator seeded by the run seed and the track's state, so
the result does not depend on the order tracks are confirmed in.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from stonesoup.base import Property
from stonesoup.initiator.base import GaussianInitiator, Initiator
from stonesoup.types.array import CovarianceMatrix, StateVector, StateVectors
from stonesoup.types.hypothesis import SingleHypothesis
from stonesoup.types.state import GaussianState, MultiModelParticleState
from stonesoup.types.track import Track
from stonesoup.types.update import GaussianStateUpdate

from tracking_sim.sensors.measurement import CartesianToBearingSlantRange
from tracking_sim.tracking.particle import content_rng

__all__ = ["ParticleConvertingInitiator", "SlantRangeInitiator", "initial_position"]

# A target closer than this fraction of the assumed height can't be at that
# height; cap the height so the ground range stays positive.
_MAX_HEIGHT_FRACTION = 0.9


def initial_position(
    model: CartesianToBearingSlantRange,
    bearing: float,
    slant_range: float,
    height_m: float,
    height_sigma_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Scenario-frame position (3,) and covariance (3, 3) of a 2D-radar detection.

    The target is put at ``height_m`` above the antenna (capped below the
    slant range). The covariance propagates bearing, slant range and height
    uncertainty through the polar-to-Cartesian conversion, so a wrong height
    guess shows up as ground-range uncertainty close to the radar, where it
    matters.
    """
    sigma_b, sigma_r = np.sqrt(np.diag(np.asarray(model.covar(), dtype=float)))
    h = min(height_m, _MAX_HEIGHT_FRACTION * slant_range)

    def local(b: float, r: float, z: float) -> np.ndarray:
        g = np.sqrt(max(r**2 - z**2, 0.0))
        return np.array([g * np.cos(b), g * np.sin(b), z])

    x0 = np.array([bearing, slant_range, h])
    p0 = local(*x0)
    steps = np.array([1e-6, 1e-3, 1e-3])
    jac = np.column_stack(
        [(local(*(x0 + np.eye(3)[k] * steps[k])) - p0) / steps[k] for k in range(3)]
    )
    cov_local = jac @ np.diag([sigma_b**2, sigma_r**2, height_sigma_m**2]) @ jac.T
    rot = np.asarray(model.rotation_matrix, dtype=float)
    position = rot.T @ p0 + np.asarray(model.translation_offset, dtype=float).ravel()
    return position, rot.T @ cov_local @ rot


class SlantRangeInitiator(GaussianInitiator):  # type: ignore[misc]
    """One tentative track per detection, at an assumed height.

    ``prior_state`` supplies the state size and the velocity (and turn-rate)
    variances; its position entries are replaced from each detection.
    """

    prior_state: GaussianState = Property(doc="Prior: zero mean, velocity variances set")
    measurement_model: CartesianToBearingSlantRange = Property(
        default=None, doc="Used for detections that carry no model of their own"
    )
    height_m: float = Property(default=5000.0, doc="Assumed height above the antenna")
    height_sigma_m: float = Property(default=3000.0, doc="Uncertainty of that height")

    def initiate(self, detections: Any, timestamp: Any, **kwargs: Any) -> set[Track]:
        tracks = set()
        for detection in detections:
            model = detection.measurement_model or self.measurement_model
            bearing = float(detection.state_vector[0, 0])
            slant = float(detection.state_vector[1, 0])
            pos, pos_cov = initial_position(
                model, bearing, slant, self.height_m, self.height_sigma_m
            )
            mapping = list(model.mapping)
            mean = np.array(self.prior_state.state_vector, dtype=float).ravel()
            covar = np.array(self.prior_state.covar, dtype=float)
            mean[mapping] = pos
            covar[np.ix_(mapping, mapping)] = pos_cov
            tracks.add(
                Track(
                    [
                        GaussianStateUpdate(
                            StateVector(mean),
                            CovarianceMatrix(covar),
                            SingleHypothesis(None, detection),
                            timestamp=detection.timestamp,
                        )
                    ]
                )
            )
        return tracks


class ParticleConvertingInitiator(Initiator):  # type: ignore[misc]
    """Confirm tracks with a Gaussian initiator, then hand them over as particles.

    Every particle starts in model 0 (constant velocity); the predictor's
    switching probability populates the others.
    """

    initiator: Initiator = Property(doc="Gaussian multi-measurement initiator")
    number_particles: int = Property(default=1000, doc="Particles per confirmed track")
    seed: int = Property(default=0, doc="Run seed mixed into the particle draws")

    def initiate(self, detections: Any, timestamp: Any, **kwargs: Any) -> set[Track]:
        confirmed = self.initiator.initiate(detections, timestamp, **kwargs)
        return {self._to_particles(track) for track in confirmed}

    def _to_particles(self, track: Track) -> Track:
        state = track.state
        mean = np.asarray(state.state_vector, dtype=float).ravel()
        covar = np.asarray(state.covar, dtype=float)
        covar = (covar + covar.T) / 2
        n = self.number_particles
        rng = content_rng(self.seed, mean, state.timestamp)
        samples = rng.multivariate_normal(mean, covar, size=n).T
        particles = MultiModelParticleState(
            StateVectors(samples),
            log_weight=np.full(n, -np.log(n)),
            dynamic_model=np.zeros(n, dtype=int),
            timestamp=state.timestamp,
        )
        return Track([*track.states[:-1], particles], id=track.id)
