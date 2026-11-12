"""Reproducible multiple-model particle filtering.

Stone Soup's particle components draw from numpy's global random state, in
whatever order they are called. In a multi-target tracker that order
follows set iteration over tracks and detections, which depends on memory
addresses, so even a seeded global generator gives different tracks from
run to run.

The components here make every random draw from its own generator, seeded
from the run seed plus the content of what is being drawn for (the particle
state and the time), so results depend on the seed and the data, never on
call order:

* ``SeededMultiModelPredictor``: model switching and process noise.
* ``SeededSystematicResampler``: the systematic resampler's offset.
* ``SeededMultiModelParticleUpdater``: measurement prediction without
  random noise; the measurement noise is added to the prediction's
  covariance instead, which is what gating needs.
* ``content_rng``: the seeding rule, also used to sample a confirmed
  track's particles.
"""

from __future__ import annotations

import copy
import zlib
from datetime import datetime
from typing import Any

import numpy as np
from stonesoup.base import Property
from stonesoup.predictor._utils import predict_lru_cache
from stonesoup.predictor.particle import MultiModelPredictor
from stonesoup.resampler.base import Resampler
from stonesoup.types.array import CovarianceMatrix, StateVectors
from stonesoup.types.prediction import MeasurementPrediction, Prediction
from stonesoup.types.state import ParticleState
from stonesoup.updater.particle import MultiModelParticleUpdater

__all__ = [
    "SeededMultiModelParticleUpdater",
    "SeededMultiModelPredictor",
    "SeededSystematicResampler",
    "content_rng",
]


def content_rng(seed: int, *parts: Any) -> np.random.Generator:
    """A generator seeded from ``seed`` and a digest of ``parts``.

    Arrays contribute their bytes, datetimes their ISO form, anything else
    its ``repr``. The same seed and content always give the same stream.
    """
    digest = 0
    for part in parts:
        if isinstance(part, np.ndarray):
            data = np.ascontiguousarray(part, dtype=float).tobytes()
        elif isinstance(part, datetime):
            data = part.isoformat().encode()
        else:
            data = repr(part).encode()
        digest = zlib.crc32(data, digest)
    return np.random.default_rng(np.random.SeedSequence([seed, digest]))


def _fingerprint(state: Any) -> np.ndarray:
    """A small, content-derived sample of a particle state, for seeding."""
    vectors = np.asarray(state.state_vector, dtype=float)
    return vectors[:, : min(8, vectors.shape[1])]


class SeededMultiModelPredictor(MultiModelPredictor):  # type: ignore[misc]
    """``MultiModelPredictor`` drawing model switches and noise from a content-seeded generator."""

    seed: int = Property(default=0, doc="Run seed mixed into every draw")

    @predict_lru_cache()  # type: ignore[untyped-decorator]
    def predict(self, prior: Any, timestamp: Any = None, **kwargs: Any) -> Any:
        rng = content_rng(self.seed, _fingerprint(prior), prior.timestamp, timestamp)
        dt = timestamp - prior.timestamp
        new = Prediction.from_state(
            prior,
            state_vector=copy.copy(prior.state_vector),
            parent=prior,
            dynamic_model=copy.copy(prior.dynamic_model),
            timestamp=timestamp,
        )
        cumulative = self.probabilities
        for index, model in enumerate(self.transition_models):
            members = prior.dynamic_model == index
            count = int(np.count_nonzero(members))
            if count == 0:
                continue
            new.dynamic_model[members] = np.searchsorted(cumulative[index], rng.random(count))
            mapping = list(self.model_mappings[index])
            subset = copy.copy(prior[members])
            subset.state_vector = subset.state_vector[mapping, :]
            covar = np.asarray(model.covar(time_interval=dt), dtype=float)
            noise = rng.multivariate_normal(np.zeros(len(mapping)), covar, size=count).T
            moved = np.asarray(
                model.function(subset, noise=StateVectors(noise), time_interval=dt), dtype=float
            )
            full = np.zeros((prior.ndim, count))  # unmapped dimensions are zero
            full[mapping, :] = moved
            new.state_vector[:, members] = full
        return new


class SeededSystematicResampler(Resampler):  # type: ignore[misc]
    """Systematic resampling with a content-seeded starting offset."""

    seed: int = Property(default=0, doc="Run seed mixed into every draw")

    def resample(self, particles: Any, nparts: int | None = None) -> Any:
        if not isinstance(particles, ParticleState):
            particles = ParticleState(None, particle_list=particles)
        n = nparts or len(particles)
        log_w = np.asarray(particles.log_weight, dtype=float)
        order = np.argsort(log_w, kind="stable")
        top = log_w[order[-1]]
        with np.errstate(divide="ignore"):
            cdf = np.log(np.cumsum(np.exp(log_w[order] - top))) + top
        rng = content_rng(self.seed, _fingerprint(particles), log_w[:8], particles.timestamp)
        u = rng.uniform(0, 1 / n) + np.arange(n) / n
        index = order[np.minimum(np.searchsorted(cdf, np.log(u)), len(order) - 1)]
        new = particles[index]
        new.log_weight = np.full(n, -np.log(n))
        return new


class SeededMultiModelParticleUpdater(MultiModelParticleUpdater):  # type: ignore[misc]
    """``MultiModelParticleUpdater`` with a noise-free measurement prediction.

    Stone Soup's particle updater predicts measurements by adding a random
    noise draw to every particle. Here the particles are mapped without
    noise and the measurement noise covariance is added to the prediction's
    weighted covariance, which is deterministic and is what Mahalanobis
    gating uses. Bearing spreads are wrapped about the mean bearing.
    """

    def predict_measurement(
        self,
        predicted_state: Any,
        measurement_model: Any = None,
        measurement_noise: bool = True,
        **kwargs: Any,
    ) -> Any:
        model = measurement_model or self.measurement_model
        z = model.function(predicted_state, noise=False, **kwargs)
        values = np.asarray(z, dtype=float)
        weights = np.exp(np.asarray(predicted_state.log_weight, dtype=float))
        weights = weights / weights.sum()
        mean_bearing = np.arctan2(weights @ np.sin(values[0]), weights @ np.cos(values[0]))
        residual = values.copy()
        residual[0] = (values[0] - mean_bearing + np.pi) % (2 * np.pi) - np.pi
        residual[1] = values[1] - weights @ values[1]
        covar = (residual * weights) @ residual.T
        if measurement_noise:
            covar = covar + np.asarray(model.covar(), dtype=float)
        return MeasurementPrediction.from_state(
            predicted_state,
            state_vector=z,
            timestamp=predicted_state.timestamp,
            fixed_covar=CovarianceMatrix(covar),
        )
