"""A sensor site: its own local frame inside the scenario frame.

Truth lives in one scenario ``LocalFrame``. A sensor measures in its own
local frame, centred on its antenna with north and up taken at the antenna,
because that is where its bearings and elevations are measured from. A few
tens of kilometres from the scenario origin the two norths differ by a
fraction of a degree (meridians converge), which is the size of a radar's
bearing noise, so the conversion is done exactly: both frames are rotations
of ECEF, and ``SensorSite`` holds the rotation between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import ArrayLike, NDArray

from tracking_sim.geo.frames import LocalFrame

__all__ = ["EARTH_RADIUS_M", "SensorSite", "ecef_to_enu_rotation", "radar_horizon_visible"]

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]

EARTH_RADIUS_M = 6_371_000.0


def ecef_to_enu_rotation(lat_deg: float, lon_deg: float) -> FloatArray:
    """The rotation taking ECEF vectors to ENU vectors at (lat, lon)."""
    phi, lam = np.radians(lat_deg), np.radians(lon_deg)
    sp, cp, sl, cl = np.sin(phi), np.cos(phi), np.sin(lam), np.cos(lam)
    return np.array(
        [
            [-sl, cl, 0.0],
            [-sp * cl, -sp * sl, cp],
            [cp * cl, cp * sl, sp],
        ]
    )


@dataclass(frozen=True)
class SensorSite:
    """A sensor antenna at (lat, lon), ``ground_alt_m + mast_m`` up, in ``frame``."""

    frame: LocalFrame
    lat: float
    lon: float
    ground_alt_m: float
    mast_m: float
    position: FloatArray = field(init=False, repr=False, compare=False)
    rotation: FloatArray = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        e, n, u = self.frame.to_enu(self.lat, self.lon, self.antenna_alt_m)
        object.__setattr__(self, "position", np.array([float(e), float(n), float(u)]))
        # scenario ENU -> ECEF -> sensor ENU; both are pure rotations of ECEF vectors.
        r_scenario = ecef_to_enu_rotation(self.frame.lat, self.frame.lon)
        r_sensor = ecef_to_enu_rotation(self.lat, self.lon)
        object.__setattr__(self, "rotation", r_sensor @ r_scenario.T)

    @property
    def antenna_alt_m(self) -> float:
        return self.ground_alt_m + self.mast_m

    def to_local(self, points: ArrayLike) -> FloatArray:
        """Scenario ENU points (n, 3) to the sensor's local ENU (n, 3)."""
        p = np.atleast_2d(np.asarray(points, dtype=float))
        local: FloatArray = (p - self.position) @ self.rotation.T
        return local

    def to_scenario(self, local: ArrayLike) -> FloatArray:
        """Sensor-local ENU points (n, 3) back to scenario ENU (n, 3)."""
        q = np.atleast_2d(np.asarray(local, dtype=float))
        scenario: FloatArray = q @ self.rotation + self.position
        return scenario

    def polar(self, points: ArrayLike) -> tuple[FloatArray, FloatArray, FloatArray]:
        """Compass azimuth and elevation (radians) and slant range (metres) of points.

        Azimuth is clockwise from the sensor's north in ``[0, 2*pi)``.
        Elevation is the geometric angle above the antenna's horizontal plane.
        """
        local = self.to_local(points)
        e, n, u = local[:, 0], local[:, 1], local[:, 2]
        ground = np.hypot(e, n)
        azimuth = np.mod(np.arctan2(e, n), 2 * np.pi)
        return azimuth, np.arctan2(u, ground), np.sqrt(ground**2 + u**2)

    def height_above_site(self, points: ArrayLike) -> FloatArray:
        """Height of points above a sphere through the site's ground level.

        Local "up" falls away from the earth's surface with distance, by about
        ``d**2 / (2 R)``; adding it back gives the height that line-of-sight
        calculations need. Good to a metre or so within radar ranges.
        """
        local = self.to_local(points)
        ground = np.hypot(local[:, 0], local[:, 1])
        height: FloatArray = local[:, 2] + self.mast_m + ground**2 / (2 * EARTH_RADIUS_M)
        return height


def radar_horizon_visible(
    ground_range_m: ArrayLike,
    antenna_height_m: float,
    target_height_m: ArrayLike,
    k: float = 4.0 / 3.0,
) -> BoolArray:
    """Line of sight over a smooth earth of radius ``k * R``.

    Heights are above the site's ground level. The standard-atmosphere
    ``k = 4/3`` models refraction bending radar waves around the earth.
    A target below that level is never visible beyond the antenna's own
    horizon.
    """
    effective = k * EARTH_RADIUS_M
    h_a = max(antenna_height_m, 0.0)
    h_t = np.maximum(np.asarray(target_height_m, dtype=float), 0.0)
    horizon = np.sqrt(2 * effective * h_a) + np.sqrt(2 * effective * h_t)
    visible: BoolArray = np.asarray(ground_range_m, dtype=float) <= horizon
    return visible
