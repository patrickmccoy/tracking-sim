import numpy as np
import pymap3d
import pytest

from tracking_sim.geo import LocalFrame, SensorSite
from tracking_sim.geo.terrain import (
    ElevationModel,
    FlatTerrain,
    GridDEM,
    SyntheticRidge,
    TerrainHorizon,
)

SITE_LL = (39.9088, -105.1172)
GROUND = 1700.0
FRAME = LocalFrame(*SITE_LL, GROUND)


def site(mast: float = 15.0) -> SensorSite:
    return SensorSite(FRAME, SITE_LL[0], SITE_LL[1], GROUND, mast)


def point(az_deg: float, dist_m: float) -> tuple[float, float]:
    az = np.radians(az_deg)
    lat, lon, _ = pymap3d.enu2geodetic(
        dist_m * np.sin(az), dist_m * np.cos(az), 0.0, *SITE_LL, GROUND
    )
    return float(lat), float(lon)


# A 1,000 m ridge running north-south, 20 km west of the site.
RIDGE = SyntheticRidge(
    *point(270.0, 20_000.0),
    azimuth_deg=0.0,
    height_m=1000.0,
    width_m=1500.0,
    length_m=40_000.0,
    base_m=GROUND,
)


def test_models_satisfy_the_protocol() -> None:
    for model in (FlatTerrain(), RIDGE, GridDEM(np.zeros((2, 2)), 40.0, -106.0, 0.5, 0.5)):
        assert isinstance(model, ElevationModel)
        assert isinstance(model.digest(), str)


def test_synthetic_ridge_shape() -> None:
    crest = RIDGE.height(*point(270.0, 20_000.0))
    assert crest == pytest.approx(GROUND + 1000.0)
    assert RIDGE.height(*point(270.0, 20_000.0 + 1500.0)) == pytest.approx(
        GROUND + 1000.0 * np.exp(-0.5), rel=1e-3
    )
    assert RIDGE.height(*point(90.0, 20_000.0)) == pytest.approx(GROUND)  # far side of the site
    # Tapered beyond its 40 km length.
    north_end = RIDGE.height(*point(-45.0, 20_000.0 * np.sqrt(2) * 1.6))
    assert north_end < GROUND + 50


def test_grid_dem_interpolates_bilinearly() -> None:
    dem = GridDEM(
        np.array([[0.0, 10.0], [20.0, 30.0]]), north=40.0, west=-106.0, dlat=1.0, dlon=1.0
    )
    assert dem.height(40.0, -106.0) == pytest.approx(0.0)
    assert dem.height(39.0, -105.0) == pytest.approx(30.0)
    assert dem.height(39.5, -105.5) == pytest.approx(15.0)
    assert dem.height(41.0, -105.5) == pytest.approx(0.0)  # outside: fill
    assert dem.digest() != GridDEM(np.ones((2, 2)), 40.0, -106.0, 1.0, 1.0).digest()


def test_flat_terrain_masks_nothing_above_the_ground() -> None:
    horizon = TerrainHorizon(site(), FlatTerrain(GROUND), 100_000.0)
    az = np.radians([10.0, 200.0])
    assert not horizon.masked(az, [50_000.0, 80_000.0], [3000.0, 3000.0]).any()


def test_ridge_hides_low_aircraft_behind_it_only() -> None:
    horizon = TerrainHorizon(site(), RIDGE, 100_000.0)
    west, east = np.radians(270.0), np.radians(90.0)
    # Behind the ridge at 40 km: low is hidden, high is not.
    assert horizon.masked(west, 40_000.0, 1000.0)
    assert not horizon.masked(west, 40_000.0, 4000.0)
    # In front of the ridge, and on the open side, a low aircraft is visible.
    assert not horizon.masked(west, 10_000.0, 500.0)
    assert not horizon.masked(east, 40_000.0, 1000.0)
    # The lowest visible height behind the crest is about twice the crest at twice the range.
    need = float(horizon.lowest_visible_height(west, 40_000.0))
    assert 1900.0 < need < 2200.0


def test_horizon_matches_brute_force_line_of_sight() -> None:
    horizon = TerrainHorizon(site(), RIDGE, 60_000.0, range_step_m=50.0)
    rng = np.random.default_rng(3)
    k_r = 4 / 3 * 6_371_000.0
    checked = 0
    for _ in range(100):
        az_deg = rng.uniform(240.0, 300.0)
        d = rng.uniform(5_000.0, 60_000.0)
        h = rng.uniform(0.0, 4_000.0)
        # Brute force: terrain every 25 m along the bearing, angles on a k-earth.
        r = np.arange(25.0, d, 25.0)
        az = np.radians(az_deg)
        lat, lon, _ = pymap3d.enu2geodetic(r * np.sin(az), r * np.cos(az), 0.0, *SITE_LL, GROUND)
        ground = RIDGE.height(lat, lon) - GROUND
        terrain_angle = np.arctan2(ground - 15.0 - r**2 / (2 * k_r), r)
        target_angle = np.arctan2(h - 15.0 - d**2 / (2 * k_r), d)
        if abs(np.max(terrain_angle) - target_angle) < 2e-4:
            continue  # grazing: sampling differences decide it
        blocked = bool(np.max(terrain_angle) > target_angle)
        assert bool(horizon.masked(az, d, h)) == blocked, (az_deg, d, h)
        checked += 1
    assert checked > 90
