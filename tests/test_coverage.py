import numpy as np
import pytest

from tracking_sim.geo.site import EARTH_RADIUS_M
from tracking_sim.geo.terrain import SyntheticRidge
from tracking_sim.sensors.spec import parse_sensor_spec
from tracking_sim.sim.coverage import coverage, visible_ranges

RADAR = {
    "id": "r",
    "type": "rotating_radar_2d",
    "position": {"lat": 39.9, "lon": -105.1, "alt_m": 1700, "mast_m": 15},
    "coverage": {"range_min_m": 500, "range_max_m": 110_000, "elevation_deg": [-1, 30]},
    "scan": {"period_s": 5, "beamwidth_deg": 1.5},
    "noise": {"range_sigma_m": 60, "bearing_sigma_deg": 0.2},
    "detection": {"pd": 0.9},
}


def radar(**coverage_changes: object) -> object:
    return parse_sensor_spec({**RADAR, "coverage": {**RADAR["coverage"], **coverage_changes}})


def test_low_coverage_stops_at_the_radar_horizon() -> None:
    k = 4 / 3 * EARTH_RADIUS_M
    horizon = np.sqrt(2 * k * 15) + np.sqrt(2 * k * 300)
    (low,) = coverage(radar(), [300])["features"]
    assert low["properties"]["area_km2"] == pytest.approx(np.pi * horizon**2 / 1e6, rel=0.02)
    (ring,) = low["geometry"]["coordinates"]  # all round: one polygon
    _, lat = np.array(ring[0]).T
    assert lat.max() - 39.9 == pytest.approx(horizon / 111_000, rel=0.03)


def test_high_coverage_is_range_limited_with_a_cone_of_silence() -> None:
    _, ranges, visible = visible_ranges(radar(), 5000)
    row = ranges[visible[0]]
    # Elevation at most 30 degrees: nothing nearer than about 5000 / tan(30).
    assert row.min() == pytest.approx((5000 - 15) / np.tan(np.radians(30)), abs=300)
    assert row.max() == pytest.approx(110_000, abs=300)
    assert visible.all(axis=1).sum() == 0 and visible.any(axis=1).all()


def test_a_sector_radar_sees_only_its_sector() -> None:
    azimuths, _, visible = visible_ranges(radar(azimuth_deg=[300, 60]), 3000)
    seen = azimuths[visible.any(axis=1)]
    assert ((seen > 300) | (seen < 60)).all() and len(seen) == 120


def test_a_ridge_casts_a_shadow_on_its_bearing_only() -> None:
    # A 900 m ridge running north-south 6 km west of the radar.
    ridge = SyntheticRidge(lat=39.9, lon=-105.17, azimuth_deg=0, height_m=900, base_m=1700)
    _, ranges, visible = visible_ranges(radar(), 1500, terrain=ridge)
    west, east = visible[270], visible[90]  # 1-degree bins
    assert not west[ranges > 10_000].any()  # hidden beyond the ridge
    assert west[(ranges > 3_000) & (ranges < 5_000)].all()  # in front of it
    assert east[(ranges > 5_000) & (ranges < 100_000)].all()
    flat = coverage(radar(), [1500])["features"][0]["properties"]["area_km2"]
    shadowed = coverage(radar(), [1500], terrain=ridge)["features"][0]
    assert shadowed["properties"]["area_km2"] < 0.8 * flat
    assert len(shadowed["geometry"]["coordinates"]) > 1
