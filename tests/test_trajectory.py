import itertools
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from stonesoup.types.groundtruth import GroundTruthPath, GroundTruthState

from tracking_sim.geo import LocalFrame
from tracking_sim.truth.builder import TruthBuilderConfig, TruthSet, build_truth
from tracking_sim.truth.synthetic import SyntheticTraffic, generate_traffic
from tracking_sim.truth.trajectory import Trajectory, to_posix

T0 = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def traffic() -> SyntheticTraffic:
    return generate_traffic(seed=1)


@pytest.fixture(scope="module")
def truth(traffic: SyntheticTraffic) -> TruthSet:
    return build_truth(traffic.records, LocalFrame(*traffic.origin))


def straight_line(n: int = 20) -> tuple[np.ndarray, np.ndarray]:
    t = T0.timestamp() + np.arange(n) * 5.0
    enu = np.column_stack([np.arange(n) * 1000.0, np.arange(n) * -500.0, np.full(n, 3000.0)])
    return t, enu


@pytest.mark.parametrize("method", ["spline", "linear"])
def test_recovers_a_straight_line_and_never_extrapolates(method: str) -> None:
    t, enu = straight_line()
    traj = Trajectory(t, enu, method=method)  # type: ignore[arg-type]
    state = traj.state_at(T0 + timedelta(seconds=12.5))
    assert state is not None
    assert np.allclose(state.position, [2500.0, -1250.0, 3000.0], atol=1e-3)
    assert np.allclose(state.velocity, [200.0, -100.0, 0.0], atol=1e-3)
    assert np.allclose(state.state_vector, [2500.0, 200.0, -1250.0, -100.0, 3000.0, 0.0], atol=1e-3)
    assert traj.state_at(T0 - timedelta(seconds=1)) is None
    assert traj.state_at(T0 + timedelta(seconds=96)) is None
    _, _, valid = traj.evaluate([t[0] - 1, t[0], t[-1], t[-1] + 1])
    assert valid.tolist() == [False, True, True, False]


def test_short_segments_fall_back_to_linear() -> None:
    t, enu = straight_line(3)
    assert Trajectory(t, enu).method == "linear"
    with pytest.raises(ValueError):
        Trajectory(t[::-1], enu)


def test_to_posix_accepts_common_time_types() -> None:
    naive = datetime(2026, 11, 1, 23, 0)
    expected = T0.timestamp()
    assert to_posix(naive)[0] == expected
    assert to_posix([T0, naive]).tolist() == [expected, expected]
    assert to_posix(np.array(["2026-11-01T23:00:00"], dtype="datetime64[s]"))[0] == expected
    assert to_posix(expected)[0] == expected


def test_smoothing_beats_the_raw_positions(traffic: SyntheticTraffic, truth: TruthSet) -> None:
    frame = truth.frame
    raw_err, smooth_err, speed_err = [], [], []
    for seg in truth:
        flight = traffic.flight(seg.icao24)
        true = flight.sample(seg.t - traffic.epoch.timestamp())
        e, n, _ = frame.to_enu(true["lat"], true["lon"], true["alt_m"])
        pos, vel, valid = seg.evaluate(seg.t)
        assert valid.all()
        raw_err.append(np.hypot(seg.enu[:, 0] - e, seg.enu[:, 1] - n))
        smooth_err.append(np.hypot(pos[:, 0] - e, pos[:, 1] - n))
        speed_err.append(np.abs(np.hypot(vel[:, 0], vel[:, 1]) - true["gs_mps"]))
    raw, smooth = np.concatenate(raw_err), np.concatenate(smooth_err)
    assert np.median(smooth) < 0.8 * np.median(raw)
    assert np.median(np.concatenate(speed_err)) < 1.0


def test_states_at_and_active_segments(truth: TruthSet) -> None:
    t = T0 + timedelta(minutes=30)
    states = truth.states_at(t)
    assert set(states) == {s.segment_id for s in truth.active(t)}
    assert 0 < len(states) < len(truth)
    for state in states.values():
        assert state.ts == t and np.isfinite(state.position).all()


def test_exports_stone_soup_ground_truth_paths(truth: TruthSet) -> None:
    paths = truth.ground_truth_paths()
    assert len(paths) == len(truth)
    seg = truth.segments[0]
    path = paths[0]
    assert isinstance(path, GroundTruthPath) and path.id == seg.segment_id
    assert len(path.states) == len(seg)
    state = path.states[0]
    assert isinstance(state, GroundTruthState)
    assert state.state_vector.shape == (6, 1)
    assert state.timestamp == seg.start
    assert state.metadata["icao24"] == seg.icao24
    assert state.metadata["alt_reference"] == seg.alt_reference
    expected = seg.state_at(seg.start)
    assert expected is not None
    assert np.allclose(np.asarray(state.state_vector).ravel(), expected.state_vector)

    gridded = seg.to_ground_truth_path(step_s=10.0)
    stamps = [s.timestamp for s in gridded.states]
    steps = [(b - a).total_seconds() for a, b in itertools.pairwise(stamps)]
    assert steps == pytest.approx([10.0] * len(steps))
    outside = seg.to_ground_truth_path([seg.start - timedelta(seconds=5), seg.start])
    assert len(outside.states) == 1


def test_linear_smoothing_option(traffic: SyntheticTraffic) -> None:
    config = TruthBuilderConfig(smoothing="linear")
    truth = build_truth(traffic.records, LocalFrame(*traffic.origin), config)
    seg = truth.segments[0]
    assert seg.trajectory.method == "linear"
    pos, _, _ = seg.evaluate(seg.t)
    assert np.allclose(pos, seg.enu)
