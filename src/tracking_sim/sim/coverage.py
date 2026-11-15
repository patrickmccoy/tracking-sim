"""Where a sensor can see an aircraft at a given height: coverage for maps.

``coverage`` evaluates the sensor's own detection checks (range, azimuth
sector, elevation limits, radar horizon and terrain; ``GroundSensor.
gate_checks``) on a polar grid around it, at each height asked for, and
returns the visible area as GeoJSON. Heights are above the sensor site's
ground, the same height the line-of-sight checks use.

The grid is coarser than the simulator's terrain horizon (by default 1
degree and about 1/400 of the range, against 0.1 degree and 100 m), so the
outline can be redrawn while a sensor is dragged on a map; the edges of
terrain shadows are good to the grid step. Detection probability is not
part of coverage: inside it, a radar still misses some looks.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray

from tracking_sim.geo import LocalFrame
from tracking_sim.geo.site import EARTH_RADIUS_M, SensorSite
from tracking_sim.geo.terrain import ElevationModel, TerrainHorizon
from tracking_sim.sensors.base import GroundSensor
from tracking_sim.sensors.passive import PassiveBearingSensor
from tracking_sim.sensors.radar import RotatingRadar2D
from tracking_sim.sensors.spec import AnySensorSpec, RotatingRadar2DSpec

__all__ = ["coverage", "visible_ranges"]

FloatArray = NDArray[np.float64]

_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)  # any time: coverage has no clock


def _sensor(
    spec: AnySensorSpec,
    terrain: ElevationModel | None,
    azimuth_step_deg: float,
    range_step_m: float,
) -> GroundSensor:
    pos = spec.position
    frame = LocalFrame(pos.lat, pos.lon, pos.alt_m)
    horizon = None
    if terrain is not None and spec.coverage.terrain_masking:
        site = SensorSite(frame, pos.lat, pos.lon, pos.alt_m, pos.mast_m)
        horizon = TerrainHorizon(
            site,
            terrain,
            spec.coverage.range_max_m,
            azimuth_step_deg=azimuth_step_deg / 2,
            range_step_m=range_step_m,
            k=spec.coverage.refraction_k,
        )
    if isinstance(spec, RotatingRadar2DSpec):
        return RotatingRadar2D(spec, frame, _EPOCH, terrain=horizon)
    return PassiveBearingSensor(spec, frame, _EPOCH, terrain=horizon)


def visible_ranges(
    spec: AnySensorSpec,
    height_m: float,
    *,
    terrain: ElevationModel | None = None,
    azimuth_step_deg: float = 1.0,
    range_step_m: float | None = None,
) -> tuple[FloatArray, FloatArray, NDArray[np.bool_]]:
    """Bin-centre azimuths (degrees), ground ranges (m) and a visibility grid.

    ``visible[i, j]`` is True where an aircraft ``height_m`` above the site's
    ground, at azimuth ``i`` and ground range ``j``, passes every check.
    """
    step = range_step_m or max(spec.coverage.range_max_m / 400.0, 50.0)
    sensor = _sensor(spec, terrain, azimuth_step_deg, step)
    azimuths = (np.arange(round(360.0 / azimuth_step_deg)) + 0.5) * azimuth_step_deg
    ranges = np.arange(step / 2, spec.coverage.range_max_m + step, step)
    az = np.radians(np.repeat(azimuths, len(ranges)))
    ground = np.tile(ranges, len(azimuths))
    height = np.full_like(ground, height_m)
    # Inverse of SensorSite.height_above_site: the antenna's local "up".
    up = height - spec.position.mast_m - ground**2 / (2 * EARTH_RADIUS_M)
    el = np.arctan2(up, ground)
    rng = np.hypot(ground, up)
    checks = sensor.gate_checks(az, el, rng, ground, height)
    visible: NDArray[np.bool_] = np.all(np.stack(checks), axis=0).reshape(
        len(azimuths), len(ranges)
    )
    return np.asarray(azimuths, dtype=float), np.asarray(ranges, dtype=float), visible


def _intervals(
    row: NDArray[np.bool_], ranges: FloatArray, step: float
) -> tuple[tuple[float, float], ...]:
    edges = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
    starts, stops = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return tuple(
        (max(float(ranges[int(a)]) - step / 2, 0.0), float(ranges[int(b) - 1]) + step / 2)
        for a, b in zip(starts, stops, strict=True)
    )


def _coords(frame: LocalFrame, ring: FloatArray) -> list[list[float]]:
    """Local east/north points to GeoJSON [lon, lat], anticlockwise.

    The rings are built clockwise in compass azimuth, which is clockwise on
    the map, so they are reversed for GeoJSON's anticlockwise exteriors.
    """
    lat, lon, _ = frame.to_geodetic(ring[:, 0], ring[:, 1], np.zeros(len(ring)))
    coords: list[list[float]] = np.round(np.column_stack([lon, lat]), 6)[::-1].tolist()
    return coords


def _ring(frame: LocalFrame, arc: FloatArray, radius: float) -> list[list[float]]:
    points = np.column_stack([np.sin(arc) * radius, np.cos(arc) * radius])
    return _coords(frame, np.vstack([points[:-1], points[:1]]))


def coverage(
    spec: AnySensorSpec,
    heights_m: Sequence[float],
    *,
    terrain: ElevationModel | None = None,
    azimuth_step_deg: float = 1.0,
    range_step_m: float | None = None,
) -> dict[str, Any]:
    """A GeoJSON FeatureCollection: one MultiPolygon of visible area per height.

    Each feature's properties carry ``height_m`` and ``area_km2``.
    Neighbouring azimuth bins with the same visible ranges are merged into
    one sector, so open country gives a handful of polygons and terrain
    shadows give more.
    """
    pos = spec.position
    frame = LocalFrame(pos.lat, pos.lon, pos.alt_m)
    features = []
    for height_m in heights_m:
        azimuths, ranges, visible = visible_ranges(
            spec,
            height_m,
            terrain=terrain,
            azimuth_step_deg=azimuth_step_deg,
            range_step_m=range_step_m,
        )
        step = float(ranges[1] - ranges[0]) if len(ranges) > 1 else float(ranges[0] * 2)
        rows = [_intervals(visible[i], ranges, step) for i in range(len(azimuths))]
        # Runs of azimuth bins with identical intervals become one sector.
        runs: list[tuple[int, int, tuple[tuple[float, float], ...]]] = []
        for i, row in enumerate(rows):
            if runs and runs[-1][2] == row:
                runs[-1] = (runs[-1][0], i, row)
            else:
                runs.append((i, i, row))
        if len(runs) > 1 and runs[0][2] == runs[-1][2]:  # join across north
            first, last = runs.pop(0), runs.pop()
            runs.append((last[0], first[1] + len(rows), first[2]))
        polygons = []
        area = 0.0
        for a, b, row in runs:
            lo = (a * azimuth_step_deg) % 360.0
            width = (b - a + 1) * azimuth_step_deg
            arc = np.radians(lo + np.linspace(0, width, max(int(np.ceil(width)), 1) + 1))
            for r0, r1 in row:
                area += float(np.radians(width)) / 2 * (r1**2 - r0**2)
                if width >= 360.0:
                    # A whole disc or annulus: rings with no radial seam.
                    rings = [_ring(frame, arc, r1)]
                    if r0 > 0:
                        rings.append(_ring(frame, arc, r0)[::-1])
                    polygons.append(rings)
                    continue
                outer = np.column_stack([np.sin(arc) * r1, np.cos(arc) * r1])
                if r0 > 0:
                    inner = np.column_stack([np.sin(arc) * r0, np.cos(arc) * r0])[::-1]
                else:
                    inner = np.zeros((1, 2))
                polygons.append([_coords(frame, np.vstack([outer, inner, outer[:1]]))])
        features.append(
            {
                "type": "Feature",
                "properties": {"height_m": float(height_m), "area_km2": round(area / 1e6, 1)},
                "geometry": {"type": "MultiPolygon", "coordinates": polygons},
            }
        )
    return {"type": "FeatureCollection", "features": features}
