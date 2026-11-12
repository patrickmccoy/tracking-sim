"""Coordinate frames: WGS84 geodetic and a local East-North-Up frame.

Everything downstream of the truth builder works in one ``LocalFrame``
centred on the scenario origin. Conversions are exact ellipsoidal ones from
``pymap3d``, so they stay correct at radar ranges where a flat-earth
approximation would be off by hundreds of metres.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pymap3d
from numpy.typing import ArrayLike, NDArray

__all__ = ["LocalFrame", "enu_velocity"]

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class LocalFrame:
    """A local ENU frame. ``alt_m`` is the origin's height above the ellipsoid."""

    lat: float
    lon: float
    alt_m: float = 0.0

    def __post_init__(self) -> None:
        if not (-90.0 <= self.lat <= 90.0 and -180.0 <= self.lon <= 180.0):
            raise ValueError(f"origin out of range: {self.lat}, {self.lon}")

    def to_enu(
        self, lat: ArrayLike, lon: ArrayLike, alt_m: ArrayLike
    ) -> tuple[FloatArray, FloatArray, FloatArray]:
        """Geodetic degrees and metres to east, north, up metres."""
        e, n, u = pymap3d.geodetic2enu(
            np.asarray(lat, dtype=float),
            np.asarray(lon, dtype=float),
            np.asarray(alt_m, dtype=float),
            self.lat,
            self.lon,
            self.alt_m,
        )
        return np.asarray(e, dtype=float), np.asarray(n, dtype=float), np.asarray(u, dtype=float)

    def to_geodetic(
        self, east: ArrayLike, north: ArrayLike, up: ArrayLike
    ) -> tuple[FloatArray, FloatArray, FloatArray]:
        """East, north, up metres to geodetic degrees and metres."""
        lat, lon, alt = pymap3d.enu2geodetic(
            np.asarray(east, dtype=float),
            np.asarray(north, dtype=float),
            np.asarray(up, dtype=float),
            self.lat,
            self.lon,
            self.alt_m,
        )
        return (
            np.asarray(lat, dtype=float),
            np.asarray(lon, dtype=float),
            np.asarray(alt, dtype=float),
        )


def enu_velocity(
    gs_mps: ArrayLike, track_rad: ArrayLike, vrate_mps: ArrayLike
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Ground speed, true track and vertical rate to (east, north, up) velocity.

    Track is measured from true north at the aircraft. Within a few hundred
    kilometres of the origin the rotation between that north and the local
    frame's north is well under a degree, which is ignored here.
    """
    gs = np.asarray(gs_mps, dtype=float)
    track = np.asarray(track_rad, dtype=float)
    return gs * np.sin(track), gs * np.cos(track), np.asarray(vrate_mps, dtype=float)
