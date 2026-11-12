import numpy as np
import pymap3d
import pytest

from tracking_sim.geo import LocalFrame, SensorSite, radar_horizon_visible

FRAME = LocalFrame(39.9088, -105.1172, 1729.0)


def test_local_frame_matches_pymap3d_for_an_offset_site() -> None:
    # A site 60 km north-east of the scenario origin, where the two norths differ.
    site = SensorSite(FRAME, 40.3, -104.6, 1500.0, 20.0)
    lat = np.array([40.0, 40.8, 39.5])
    lon = np.array([-105.5, -104.0, -103.9])
    alt = np.array([3000.0, 11000.0, 6000.0])
    scenario = np.column_stack(FRAME.to_enu(lat, lon, alt))
    expected = np.column_stack(pymap3d.geodetic2enu(lat, lon, alt, 40.3, -104.6, 1520.0))
    assert np.allclose(site.to_local(scenario), expected, atol=1e-3)
    assert np.allclose(site.to_scenario(site.to_local(scenario)), scenario, atol=1e-6)


def test_polar_is_compass_azimuth_elevation_and_slant_range() -> None:
    site = SensorSite(FRAME, FRAME.lat, FRAME.lon, FRAME.alt_m, 0.0)
    pts = np.array([[0.0, 1000.0, 0.0], [1000.0, 0.0, 1000.0], [-1000.0, -1000.0, 0.0]])
    az, el, rng = site.polar(pts)
    assert np.degrees(az) == pytest.approx([0.0, 90.0, 225.0])
    assert np.degrees(el) == pytest.approx([0.0, 45.0, 0.0])
    assert rng == pytest.approx([1000.0, np.sqrt(2e6), np.sqrt(2e6)])


def test_height_above_site_adds_back_earth_curvature() -> None:
    site = SensorSite(FRAME, FRAME.lat, FRAME.lon, FRAME.alt_m, 15.0)
    # A point 100 km due east at the site's ground altitude sits ~785 m below local horizontal.
    lat, lon, _ = pymap3d.enu2geodetic(100_000.0, 0.0, 0.0, FRAME.lat, FRAME.lon, FRAME.alt_m)
    point = np.column_stack(FRAME.to_enu(lat, lon, FRAME.alt_m))
    assert site.to_local(point)[0, 2] < -700
    assert site.height_above_site(point)[0] == pytest.approx(0.0, abs=5.0)


def test_radar_horizon_on_a_four_thirds_earth() -> None:
    # 15 m mast: horizon to a 3000 m target is about 16 + 226 = 242 km.
    ranges = np.array([200_000.0, 250_000.0])
    assert list(radar_horizon_visible(ranges, 15.0, 3000.0)) == [True, False]
    # Without refraction the horizon is closer.
    assert not radar_horizon_visible(220_000.0, 15.0, 3000.0, k=1.0)
    # A target below the site never shows beyond the antenna's own horizon (~16 km).
    assert list(radar_horizon_visible([10_000.0, 20_000.0], 15.0, -50.0)) == [True, False]
