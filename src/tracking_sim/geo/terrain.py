"""Terrain: elevation models and line-of-sight masking for sensors.

An ``ElevationModel`` gives ground height (metres above mean sea level) at
any latitude and longitude. Three kinds:

* ``GridDEM``: a regular latitude/longitude grid, bilinearly interpolated;
  the Copernicus tiles (``geo.copernicus``) load into one.
* ``SyntheticRidge``: flat ground with one straight ridge, for tests and
  for demonstrating masking without downloading anything.
* ``FlatTerrain``: level ground, which masks nothing above it.

``TerrainHorizon`` precomputes, for one sensor, the highest elevation angle
the terrain reaches in each azimuth bin out to each range, on an earth of
``k`` times its radius (4/3 for standard refraction). Checking whether a
target is hidden is then one lookup: it is masked if its own elevation
angle is below the terrain's highest angle anywhere between the antenna and
the target. Heights are taken relative to the sensor site's ground, so the
geoid-versus-ellipsoid offset of the truth altitudes cancels (as for the
radar horizon, D13).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np
import pymap3d
from numpy.typing import ArrayLike, NDArray

from tracking_sim.geo.site import EARTH_RADIUS_M, SensorSite

__all__ = [
    "ElevationModel",
    "FlatTerrain",
    "GridDEM",
    "SyntheticRidge",
    "TerrainHorizon",
]

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]


@runtime_checkable
class ElevationModel(Protocol):
    """Ground height in metres above mean sea level."""

    def height(self, lat: ArrayLike, lon: ArrayLike) -> FloatArray: ...

    def digest(self) -> str:
        """A stable identifier of the terrain data, for the run store."""
        ...


@dataclass(frozen=True)
class FlatTerrain:
    """Level ground at ``height_m``."""

    height_m: float = 0.0

    def height(self, lat: ArrayLike, lon: ArrayLike) -> FloatArray:
        shape = np.broadcast(np.asarray(lat), np.asarray(lon)).shape
        return np.full(shape, self.height_m, dtype=float)

    def digest(self) -> str:
        return f"flat:{self.height_m!r}"


@dataclass(frozen=True)
class SyntheticRidge:
    """Flat ground at ``base_m`` with one straight ridge.

    The crest passes through (``lat``, ``lon``) and runs along compass
    ``azimuth_deg``. Across the ridge the height is a Gaussian, ``height_m``
    above the base at the crest with a one-sigma half-width of ``width_m``;
    along it the ridge is ``length_m`` long, tapering smoothly at the ends.
    Distances use a flat local frame at the crest point, which is accurate
    to well under a metre of height over a radar's range.
    """

    lat: float
    lon: float
    azimuth_deg: float
    height_m: float
    width_m: float = 1500.0
    length_m: float = 60_000.0
    base_m: float = 1600.0

    def height(self, lat: ArrayLike, lon: ArrayLike) -> FloatArray:
        la, lo = np.broadcast_arrays(np.asarray(lat, dtype=float), np.asarray(lon, dtype=float))
        e, n, _ = pymap3d.geodetic2enu(la, lo, np.zeros_like(la), self.lat, self.lon, 0.0)
        az = np.radians(self.azimuth_deg)
        along = np.asarray(e) * np.sin(az) + np.asarray(n) * np.cos(az)
        across = np.asarray(e) * np.cos(az) - np.asarray(n) * np.sin(az)
        half = self.length_m / 2
        overhang = np.maximum(np.abs(along) - half, 0.0)
        profile = np.exp(-0.5 * (across / self.width_m) ** 2 - 0.5 * (overhang / self.width_m) ** 2)
        heights: FloatArray = self.base_m + self.height_m * profile
        return heights

    def digest(self) -> str:
        return f"ridge:{self!r}"


@dataclass(frozen=True)
class GridDEM:
    """Heights on a regular grid of pixel centres, rows north to south.

    ``north`` and ``west`` are the latitude and longitude of the centre of
    pixel (0, 0); ``dlat`` and ``dlon`` the (positive) spacing. Outside the
    grid the height is ``fill_m`` (sea level by default).
    """

    heights: FloatArray = field(repr=False)
    north: float
    west: float
    dlat: float
    dlon: float
    fill_m: float = 0.0
    source: str = "grid"

    @property
    def shape(self) -> tuple[int, int]:
        rows, cols = self.heights.shape
        return rows, cols

    def height(self, lat: ArrayLike, lon: ArrayLike) -> FloatArray:
        la, lo = np.broadcast_arrays(np.asarray(lat, dtype=float), np.asarray(lon, dtype=float))
        rows, cols = self.shape
        r = (self.north - la) / self.dlat
        c = (lo - self.west) / self.dlon
        inside = (r >= 0) & (r <= rows - 1) & (c >= 0) & (c <= cols - 1)
        r0 = np.clip(np.floor(r).astype(int), 0, max(rows - 2, 0))
        c0 = np.clip(np.floor(c).astype(int), 0, max(cols - 2, 0))
        fr = np.clip(r - r0, 0.0, 1.0)
        fc = np.clip(c - c0, 0.0, 1.0)
        h = self.heights
        r1 = np.minimum(r0 + 1, rows - 1)
        c1 = np.minimum(c0 + 1, cols - 1)
        value = (
            h[r0, c0] * (1 - fr) * (1 - fc)
            + h[r0, c1] * (1 - fr) * fc
            + h[r1, c0] * fr * (1 - fc)
            + h[r1, c1] * fr * fc
        )
        out: FloatArray = np.where(inside, value, self.fill_m)
        return out

    def digest(self) -> str:
        h = hashlib.sha256(np.ascontiguousarray(self.heights, dtype=np.float32).tobytes())
        h.update(repr((self.north, self.west, self.dlat, self.dlon, self.fill_m)).encode())
        return f"{self.source}:{h.hexdigest()}"


class TerrainHorizon:
    """The terrain's highest elevation angle around one sensor, by azimuth and range.

    Built once per sensor: terrain heights are sampled every
    ``range_step_m`` out to ``max_range_m`` in ``azimuth_step_deg`` bins, and
    the running maximum of their elevation angle from the antenna (on a
    ``k`` x earth-radius sphere) is kept. ``masked`` then answers in one
    lookup per target.
    """

    def __init__(
        self,
        site: SensorSite,
        terrain: ElevationModel,
        max_range_m: float,
        *,
        azimuth_step_deg: float = 0.1,
        range_step_m: float = 100.0,
        k: float = 4.0 / 3.0,
    ) -> None:
        self.site = site
        self.k = k
        self.azimuth_step = np.radians(azimuth_step_deg)
        n_az = round(360.0 / azimuth_step_deg)
        self.ranges = np.arange(range_step_m, max_range_m + range_step_m, range_step_m)
        az = (np.arange(n_az) + 0.5) * self.azimuth_step
        east = np.sin(az)[:, None] * self.ranges[None, :]
        north = np.cos(az)[:, None] * self.ranges[None, :]
        lat, lon, _ = pymap3d.enu2geodetic(
            east, north, np.zeros_like(east), site.lat, site.lon, site.antenna_alt_m
        )
        ground = terrain.height(lat, lon) - site.ground_alt_m
        drop = self.ranges**2 / (2 * k * EARTH_RADIUS_M)
        angle = np.arctan2(ground - site.mast_m - drop[None, :], self.ranges[None, :])
        self.max_angle: FloatArray = np.maximum.accumulate(angle, axis=1)

    def target_angle(self, ground_range: ArrayLike, height_above_site: ArrayLike) -> FloatArray:
        """Elevation angle of targets on the same ``k`` earth."""
        d = np.asarray(ground_range, dtype=float)
        h = np.asarray(height_above_site, dtype=float)
        angle: FloatArray = np.arctan2(
            h - self.site.mast_m - d**2 / (2 * self.k * EARTH_RADIUS_M), d
        )
        return angle

    def blocking_angle(self, azimuth: ArrayLike, ground_range: ArrayLike) -> FloatArray:
        """The terrain's highest angle in each target's azimuth bin, short of its range."""
        az = np.mod(np.asarray(azimuth, dtype=float), 2 * np.pi)
        d = np.asarray(ground_range, dtype=float)
        bins = np.minimum((az / self.azimuth_step).astype(int), self.max_angle.shape[0] - 1)
        # Terrain samples strictly nearer than the target.
        j = np.searchsorted(self.ranges, d, side="left") - 1
        j = np.minimum(j, len(self.ranges) - 1)
        angle = np.where(j >= 0, self.max_angle[bins, np.maximum(j, 0)], -np.pi / 2)
        result: FloatArray = np.asarray(angle, dtype=float)
        return result

    def masked(
        self, azimuth: ArrayLike, ground_range: ArrayLike, height_above_site: ArrayLike
    ) -> BoolArray:
        """True where terrain hides a target (compass azimuth in radians)."""
        target = self.target_angle(ground_range, height_above_site)
        hidden: BoolArray = target < self.blocking_angle(azimuth, ground_range)
        return hidden

    def lowest_visible_height(self, azimuth: ArrayLike, ground_range: ArrayLike) -> FloatArray:
        """Height above the site's ground an aircraft needs to be seen there (for maps)."""
        d = np.asarray(ground_range, dtype=float)
        angle = self.blocking_angle(azimuth, d)
        height: FloatArray = (
            d * np.tan(angle) + self.site.mast_m + d**2 / (2 * self.k * EARTH_RADIUS_M)
        )
        return height
