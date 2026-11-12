"""A Stone Soup measurement model for a 2D radar that measures slant range.

A 2D surveillance radar reports bearing and the straight-line (slant)
range to the target, not the range along the ground. For an aircraft 10 km
up at 100 km the two differ by about 500 m, and a tracker that assumes the
radar measures ground range sees that as a range bias that grows with
altitude. Stone Soup's ``CartesianToBearingRange`` measures range in the
horizontal plane, so this model keeps its measurement layout
``[bearing, range]`` but makes range the 3D distance from the antenna.

Conventions follow Stone Soup so its filters, initiators and metrics work
unchanged:

* The state is the scenario ENU state ``[x, vx, y, vy, z, vz]``
  (``mapping=(0, 2, 4)``).
* ``translation_offset`` is the antenna position in the scenario frame and
  ``frame_rotation`` the exact rotation from the scenario frame to the
  sensor's own ENU frame (``SensorSite.rotation``).
* Bearing is Stone Soup's: counter-clockwise from the sensor's east axis,
  as a ``Bearing`` wrapped to ``[-pi, pi)``. Compass azimuth is ``pi/2 - bearing``.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from stonesoup.base import Property
from stonesoup.models.base import ReversibleModel
from stonesoup.models.measurement.nonlinear import _AngleNonLinearGaussianMeasurement
from stonesoup.types.angle import Bearing
from stonesoup.types.array import StateVector, StateVectors

__all__ = [
    "CartesianToBearingSlantRange",
    "CartesianToSensorBearing",
    "bearing_to_compass",
    "compass_to_bearing",
]


def compass_to_bearing(azimuth: Any) -> Any:
    """Compass azimuth (clockwise from north, radians) to Stone Soup bearing."""
    return np.mod(np.pi / 2 - np.asarray(azimuth) + np.pi, 2 * np.pi) - np.pi


def bearing_to_compass(bearing: Any) -> Any:
    """Stone Soup bearing (counter-clockwise from east) to compass azimuth in [0, 2*pi)."""
    return np.mod(np.pi / 2 - np.asarray(bearing, dtype=float), 2 * np.pi)


class CartesianToBearingSlantRange(
    _AngleNonLinearGaussianMeasurement,  # type: ignore[misc]
    ReversibleModel,  # type: ignore[misc]
):
    r"""Bearing and slant range from an antenna, with Gaussian noise.

    .. math::

        \begin{bmatrix} \phi \\ r \end{bmatrix} =
        \begin{bmatrix} \operatorname{atan2}(y', x') \\
        \sqrt{x'^2 + y'^2 + z'^2} \end{bmatrix} + v,
        \qquad [x', y', z']^T = R\,(p - p_s)

    where :math:`p` is the target position, :math:`p_s` the antenna and
    :math:`R` the scenario-to-sensor rotation.
    """

    translation_offset: StateVector | None = Property(
        default=None, doc="Antenna position in the scenario frame (3x1). Defaults to the origin."
    )
    frame_rotation: np.ndarray | None = Property(
        default=None,
        doc="3x3 rotation from the scenario ENU frame to the sensor's ENU frame. "
        "Defaults to the rotation built from ``rotation_offset``.",
    )
    inverse_height_m: float = Property(
        default=0.0,
        doc="Height above the antenna assumed by ``inverse_function``: a 2D radar "
        "does not measure it, so turning a measurement into a position needs a guess.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.translation_offset is None:
            self.translation_offset = StateVector([0.0, 0.0, 0.0])
        if len(self.mapping) != 3:
            raise ValueError("mapping must give the x, y and z position indices")

    @property
    def ndim_meas(self) -> int:
        return 2

    @property
    def rotation_matrix(self) -> np.ndarray:
        if self.frame_rotation is not None:
            return np.asarray(self.frame_rotation, dtype=float)
        matrix: np.ndarray = super().rotation_matrix
        return matrix

    def _function(self, state: Any, noise: Any = False, **kwargs: Any) -> StateVectors:
        if isinstance(noise, bool) or noise is None:
            noise = self.rvs(num_samples=state.state_vector.shape[1], **kwargs) if noise else 0
        xyz = state.state_vector[self.mapping, :] - self.translation_offset
        local = self.rotation_matrix @ xyz
        x, y, z = (np.asarray(local[i, :], dtype=float) for i in range(3))
        bearing = np.arctan2(y, x)
        slant = np.sqrt(x**2 + y**2 + z**2)
        return StateVectors([bearing, slant]) + noise

    def inverse_function(self, detection: Any, **kwargs: Any) -> StateVector:
        bearing = np.asarray(detection.state_vector[0, :], dtype=float)
        slant = np.asarray(detection.state_vector[1, :], dtype=float)
        h = self.inverse_height_m
        ground = np.sqrt(np.maximum(slant**2 - h**2, 0.0))
        local = np.array(
            [ground * np.cos(bearing), ground * np.sin(bearing), np.full_like(ground, h)]
        )
        xyz = self.rotation_matrix.T @ local + np.asarray(self.translation_offset, dtype=float)
        res = np.zeros((self.ndim_state, local.shape[1])).view(StateVector)
        res[self.mapping, :] = xyz
        return res

    @staticmethod
    def _typed_vector() -> np.ndarray:
        return np.array([[Bearing(0.0)], [0.0]])


class CartesianToSensorBearing(  # bearing only, for passive sensors
    _AngleNonLinearGaussianMeasurement,  # type: ignore[misc]
):
    r"""Bearing from a sensor, in its own frame, with Gaussian noise.

    Like ``CartesianToBearingSlantRange`` without the range: the target is
    moved into the sensor's ENU frame (``translation_offset``,
    ``frame_rotation``) and the measurement is ``atan2(y', x')``, Stone Soup's
    bearing convention. Not reversible: one bearing doesn't place a target.
    """

    translation_offset: StateVector | None = Property(
        default=None, doc="Sensor position in the scenario frame (3x1). Defaults to the origin."
    )
    frame_rotation: np.ndarray | None = Property(
        default=None, doc="3x3 rotation from the scenario ENU frame to the sensor's."
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.translation_offset is None:
            self.translation_offset = StateVector([0.0, 0.0, 0.0])
        if len(self.mapping) != 3:
            raise ValueError("mapping must give the x, y and z position indices")

    @property
    def ndim_meas(self) -> int:
        return 1

    @property
    def rotation_matrix(self) -> np.ndarray:
        if self.frame_rotation is not None:
            return np.asarray(self.frame_rotation, dtype=float)
        matrix: np.ndarray = super().rotation_matrix
        return matrix

    def _function(self, state: Any, noise: Any = False, **kwargs: Any) -> StateVectors:
        if isinstance(noise, bool) or noise is None:
            noise = self.rvs(num_samples=state.state_vector.shape[1], **kwargs) if noise else 0
        xyz = state.state_vector[self.mapping, :] - self.translation_offset
        local = self.rotation_matrix @ xyz
        bearing = np.arctan2(
            np.asarray(local[1, :], dtype=float), np.asarray(local[0, :], dtype=float)
        )
        return StateVectors([bearing]) + noise

    @staticmethod
    def _typed_vector() -> np.ndarray:
        return np.array([[Bearing(0.0)]])
