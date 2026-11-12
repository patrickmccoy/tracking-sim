import numpy as np
import pytest
from stonesoup.models.measurement.nonlinear import CartesianToBearingRange
from stonesoup.types.angle import Bearing
from stonesoup.types.detection import Detection
from stonesoup.types.state import State

from tracking_sim.geo import LocalFrame, SensorSite
from tracking_sim.sensors.measurement import (
    CartesianToBearingSlantRange,
    bearing_to_compass,
    compass_to_bearing,
)

FRAME = LocalFrame(39.9088, -105.1172, 1729.0)


def state(x: float, y: float, z: float) -> State:
    return State(np.array([[x], [100.0], [y], [0.0], [z], [0.0]]))


def model(site: SensorSite | None = None, **kwargs: object) -> CartesianToBearingSlantRange:
    extra: dict[str, object] = {}
    if site is not None:
        extra = {"translation_offset": site.position.reshape(3, 1), "frame_rotation": site.rotation}
    return CartesianToBearingSlantRange(
        ndim_state=6, mapping=(0, 2, 4), noise_covar=np.diag([1e-6, 100.0]), **extra, **kwargs
    )


def test_range_is_slant_range_and_bearing_is_stone_soup_convention() -> None:
    m = model()
    z = m.function(state(30_000.0, 40_000.0, 10_000.0))
    assert isinstance(z[0, 0], Bearing)
    assert float(z[0, 0]) == pytest.approx(np.arctan2(40_000, 30_000))
    assert float(z[1, 0]) == pytest.approx(np.sqrt(30_000**2 + 40_000**2 + 10_000**2))
    # Stone Soup's own model measures ground range; the difference is the 2D radar's bias.
    flat = CartesianToBearingRange(ndim_state=6, mapping=(0, 2), noise_covar=np.eye(2))
    assert float(flat.function(state(30_000.0, 40_000.0, 10_000.0))[1, 0]) == pytest.approx(
        50_000.0
    )


def test_offset_site_matches_the_site_polar_coordinates() -> None:
    site = SensorSite(FRAME, 40.3, -104.6, 1500.0, 20.0)
    m = model(site)
    target = np.array([12_000.0, 55_000.0, 9_000.0])
    az, _, rng = site.polar(target)
    z = m.function(state(*target))
    assert float(bearing_to_compass(float(z[0, 0]))) == pytest.approx(az[0])
    assert float(z[1, 0]) == pytest.approx(rng[0])


def test_inverse_recovers_position_at_the_assumed_height() -> None:
    site = SensorSite(FRAME, 40.1, -105.3, 1700.0, 15.0)
    target = np.array([20_000.0, -35_000.0, 0.0])
    local_up = site.to_local(target)[0, 2]
    m = model(site, inverse_height_m=local_up)
    det = Detection(m.function(state(*target)), measurement_model=m)
    back = m.inverse_function(det)
    assert np.asarray(back[[0, 2, 4], 0], dtype=float) == pytest.approx(target, abs=1e-3)


def test_jacobian_and_noise_work_with_stone_soup() -> None:
    m = model()
    jac = m.jacobian(state(30_000.0, 40_000.0, 10_000.0))
    assert jac.shape == (2, 6)
    r = np.sqrt(30_000**2 + 40_000**2 + 10_000**2)
    assert jac[1, 4] == pytest.approx(10_000 / r, rel=1e-4)  # d(range)/dz
    samples = m.function(state(30_000.0, 40_000.0, 10_000.0), noise=True)
    assert samples.shape == (2, 1)


def test_compass_conversions() -> None:
    assert compass_to_bearing(0.0) == pytest.approx(np.pi / 2)
    assert compass_to_bearing(np.pi / 2) == pytest.approx(0.0)
    assert abs(compass_to_bearing(np.radians(270))) == pytest.approx(np.pi)
    assert bearing_to_compass(-np.pi / 2) == pytest.approx(np.pi)
    az = np.radians([0.0, 45.0, 181.0, 359.0])
    assert bearing_to_compass(compass_to_bearing(az)) == pytest.approx(az)
