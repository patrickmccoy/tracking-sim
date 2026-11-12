import numpy as np
import pytest

from tracking_sim.sensors.scan import BeamSchedule

T0 = 1_700_000_000.0


def angle_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.abs((a - b + np.pi) % (2 * np.pi) - np.pi)


def test_beam_azimuth_and_scan_index() -> None:
    beam = BeamSchedule(T0, 4.0, start_azimuth=np.radians(30))
    assert np.degrees(beam.azimuth_at(T0 + 1.0)) == pytest.approx(120.0)
    assert np.degrees(beam.azimuth_at(T0 + 4.0)) == pytest.approx(30.0)
    assert list(beam.scan_index([T0, T0 + 3.99, T0 + 4.0, T0 - 0.1])) == [0, 0, 1, -1]
    assert beam.time_at_azimuth(2, np.radians(30 + 90)) == pytest.approx(T0 + 9.0)
    ccw = BeamSchedule(T0, 4.0, clockwise=False)
    assert np.degrees(ccw.azimuth_at(T0 + 1.0)) == pytest.approx(270.0)
    assert ccw.time_at_azimuth(0, np.radians(270)) == pytest.approx(T0 + 1.0)


@pytest.mark.parametrize("clockwise", [True, False])
def test_stationary_target_is_crossed_once_per_scan(clockwise: bool) -> None:
    beam = BeamSchedule(T0, 5.0, clockwise=clockwise)
    az = np.radians(90.0)
    t = T0 + np.arange(0.0, 30.001, 2.5)
    times = beam.crossings(t, np.full_like(t, az), lambda tt: np.full_like(tt, az))
    quarter = 1.25 if clockwise else 3.75
    assert times == pytest.approx(T0 + quarter + 5.0 * np.arange(6))


def test_moving_target_crossings_are_exact_and_survive_north() -> None:
    # A target circling the radar fast (one turn a minute), passing north
    # several times, against a 4 s clockwise antenna.
    beam = BeamSchedule(T0, 4.0, start_azimuth=np.radians(200))
    rate = 2 * np.pi / 60.0

    def target_az(tt: np.ndarray) -> np.ndarray:
        return np.mod(np.radians(350) + rate * (tt - T0), 2 * np.pi)

    t = T0 + np.arange(0.0, 200.0, 2.0)
    times = beam.crossings(t, target_az(t), target_az)
    # Exact to the beam's travel in a microsecond (POSIX float resolution is ~0.2 us).
    assert np.all(angle_diff(beam.azimuth_at(times), target_az(times)) < 2e-6)
    # Relative to the target the beam laps once every 1 / (1/4 - 1/60) s.
    assert len(times) == pytest.approx((t[-1] - t[0]) * (1 / 4 - 1 / 60), abs=1)
    # The target runs ahead of the beam, so the beam laps it every 4.29 s:
    # some scans miss it, and no scan sees it twice.
    steps = np.diff(beam.scan_index(times))
    assert set(steps) == {1, 2}


def test_crossings_stay_inside_the_sampled_span() -> None:
    beam = BeamSchedule(T0, 5.0)
    t = np.array([T0 + 0.5, T0 + 1.0])  # the beam is at 36-72 deg here
    az = np.radians(10.0)
    assert beam.crossings(t, np.full(2, az), lambda tt: np.full_like(tt, az)).size == 0
    assert beam.crossings(t[:1], np.full(1, az), lambda tt: tt).size == 0
