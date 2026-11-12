import numpy as np
import pytest

from tracking_sim.geo import LocalFrame, enu_velocity


def test_origin_maps_to_zero_and_round_trips() -> None:
    frame = LocalFrame(39.98, -105.13, 1650.0)
    e, n, u = frame.to_enu(39.98, -105.13, 1650.0)
    assert np.allclose([e, n, u], 0.0, atol=1e-6)
    lat = np.array([39.0, 40.5, 41.0])
    lon = np.array([-106.0, -105.0, -103.5])
    alt = np.array([0.0, 3000.0, 11000.0])
    back = frame.to_geodetic(*frame.to_enu(lat, lon, alt))
    assert np.allclose(back[0], lat, atol=1e-9)
    assert np.allclose(back[1], lon, atol=1e-9)
    assert np.allclose(back[2], alt, atol=1e-4)


def test_axes_point_east_north_up() -> None:
    frame = LocalFrame(40.0, -105.0)
    e, n, _ = frame.to_enu(40.0, -104.99, 0.0)
    assert e > 800 and abs(n) < 1
    e, n, _ = frame.to_enu(40.01, -105.0, 0.0)
    assert n > 1100 and abs(e) < 1


def test_earth_curvature_drops_a_distant_target() -> None:
    # About 100 km away at the same height, a target sits roughly d^2 / 2R
    # (several hundred metres) below the tangent plane.
    frame = LocalFrame(0.0, 0.0)
    _, n, u = frame.to_enu(0.9, 0.0, 0.0)
    assert n == pytest.approx(99_500, rel=1e-2)
    assert u == pytest.approx(-(n**2) / (2 * 6_357_000), rel=0.02)


def test_enu_velocity() -> None:
    ve, vn, vu = enu_velocity([100.0, 100.0], np.radians([90.0, 180.0]), [5.0, -5.0])
    assert np.allclose(ve, [100.0, 0.0], atol=1e-9)
    assert np.allclose(vn, [0.0, -100.0], atol=1e-9)
    assert np.allclose(vu, [5.0, -5.0])


def test_bad_origin() -> None:
    with pytest.raises(ValueError):
        LocalFrame(95.0, 0.0)
