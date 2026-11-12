from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from stonesoup.dataassociator.neighbour import GNNWith2DAssignment
from stonesoup.predictor.kalman import ExtendedKalmanPredictor, UnscentedKalmanPredictor
from stonesoup.predictor.particle import MultiModelPredictor
from stonesoup.types.detection import Detection
from stonesoup.types.state import MultiModelParticleState, State
from stonesoup.types.update import Update

from tracking_sim.geo import LocalFrame, SensorSite
from tracking_sim.sensors import load_sensor_spec
from tracking_sim.sensors.measurement import CartesianToBearingSlantRange
from tracking_sim.sim import ReplayEngine
from tracking_sim.tracking import load_tracker_spec
from tracking_sim.tracking.factory import TrackerPipeline, build_tracker
from tracking_sim.tracking.initiators import SlantRangeInitiator, initial_position
from tracking_sim.truth import TruthSet, build_truth
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ROOT = Path(__file__).resolve().parents[1]
FRAME = LocalFrame(39.9088, -105.1172, 1729.0)
EPOCH = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)


def tracker_spec(name: str) -> Any:
    return load_tracker_spec(ROOT / f"scenarios/trackers/{name}.yaml")


@pytest.fixture(scope="module")
def truth() -> TruthSet:
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    return build_truth(traffic.records, FRAME)


@pytest.fixture(scope="module")
def batches(truth: TruthSet) -> list[tuple[datetime, set[Detection]]]:
    spec = load_sensor_spec(ROOT / "scenarios/sensors/radar-1.yaml")
    start = EPOCH + timedelta(seconds=60)
    result = ReplayEngine(truth, [spec], seed=1).run(start, start + timedelta(minutes=3))
    return [(s.end, set(s.detections)) for s in result.scans]


def model(site: SensorSite | None = None) -> CartesianToBearingSlantRange:
    site = site or SensorSite(FRAME, FRAME.lat, FRAME.lon, FRAME.alt_m, 15.0)
    return CartesianToBearingSlantRange(
        ndim_state=6,
        mapping=(0, 2, 4),
        noise_covar=np.diag([np.radians(0.2) ** 2, 60.0**2]),
        translation_offset=site.position.reshape(3, 1),
        frame_rotation=site.rotation,
    )


def run(pipe: TrackerPipeline, batches: list[tuple[datetime, set[Detection]]]) -> set[Any]:
    np.random.seed(0)  # noqa: NPY002 - Stone Soup particle components use the global state
    tracker = pipe.make_tracker([(t, pipe.adapt(d)) for t, d in batches])
    seen: set[Any] = set()
    for _, tracks in tracker:
        seen |= tracks
    return seen


def test_each_example_builds_the_right_components() -> None:
    ekf = build_tracker(tracker_spec("ekf-cv-gnn"))
    assert isinstance(ekf.predictor, ExtendedKalmanPredictor) and ekf.ndim_state == 6
    assert ekf.position_mapping == (0, 2, 4)
    ukf = build_tracker(tracker_spec("ukf-cv-gnn"))
    assert isinstance(ukf.predictor, UnscentedKalmanPredictor)
    pf = build_tracker(tracker_spec("pf-mm-gnn"))
    assert isinstance(pf.predictor, MultiModelPredictor) and pf.ndim_state == 7
    assert pf.position_mapping == (0, 2, 5) and pf.velocity_mapping == (1, 3, 6)
    assert pf.predictor.transition_matrix[0, 1] == pytest.approx(0.05)
    for pipe in (ekf, ukf, pf):
        assert isinstance(pipe.data_associator, GNNWith2DAssignment)


def test_initial_position_uses_the_assumed_height() -> None:
    m = model()
    site_local = np.array([30_000.0, 40_000.0, 5_000.0])
    target = site_local + np.asarray(m.translation_offset, dtype=float).ravel()
    z = m.function(State(np.array([[target[0]], [0], [target[1]], [0], [target[2]], [0]])))
    pos, cov = initial_position(m, float(z[0, 0]), float(z[1, 0]), 5_000.0, 3_000.0)
    assert pos == pytest.approx(target, abs=1e-3)
    assert np.all(np.linalg.eigvalsh(cov) > 0)
    # Far out, height uncertainty is almost all vertical...
    assert np.sqrt(cov[2, 2]) == pytest.approx(3_000.0, rel=0.01)
    # ...close in, a wrong height guess moves the ground range a lot.
    near = m.function(State(np.array([[3_000.0], [0], [0.0], [0], [4_000.0], [0]])))
    _, cov_near = initial_position(m, float(near[0, 0]), float(near[1, 0]), 4_000.0, 3_000.0)
    assert np.sqrt(np.trace(cov_near[:2, :2])) > 1_000.0
    # An impossible height (above the slant range) is capped.
    pos_low, _ = initial_position(m, 0.0, 2_000.0, 5_000.0, 3_000.0)
    origin = np.asarray(m.translation_offset, dtype=float).ravel()
    assert 0 < pos_low[2] - origin[2] < 2_000.0


def test_slant_range_initiator_starts_tentative_tracks() -> None:
    m = model()
    pipe = build_tracker(tracker_spec("ekf-cv-gnn"))
    inner = pipe.initiator.initiator
    assert isinstance(inner, SlantRangeInitiator)
    det = Detection(np.array([[0.3], [50_000.0]]), timestamp=EPOCH, measurement_model=m)
    (track,) = inner.initiate({det}, EPOCH)
    state = track.state
    assert isinstance(state, Update) and state.timestamp == EPOCH
    mean = np.asarray(state.state_vector, dtype=float).ravel()
    assert mean[[1, 3, 5]] == pytest.approx(0.0)
    assert np.sqrt(state.covar[1, 1]) == pytest.approx(150.0)
    assert np.all(np.linalg.eigvalsh(np.asarray(state.covar, dtype=float)) > 0)


def test_adapt_maps_detections_to_a_7d_state() -> None:
    m = model()
    det = Detection(np.array([[0.3], [50_000.0]]), timestamp=EPOCH, measurement_model=m)
    ekf = build_tracker(tracker_spec("ekf-cv-gnn"))
    assert ekf.adapt([det]) == {det}
    pf = build_tracker(tracker_spec("pf-mm-gnn"))
    (copy,) = pf.adapt([det])
    assert copy is not det and pf.adapt([det]) == {copy}  # cached
    m7 = copy.measurement_model
    assert m7.ndim_state == 7 and tuple(m7.mapping) == (0, 2, 5)
    x6 = State(np.array([[1e4], [0], [2e4], [0], [3e3], [0]]))
    x7 = State(np.array([[1e4], [0], [2e4], [0], [0.01], [3e3], [0]]))
    assert np.asarray(m7.function(x7), dtype=float) == pytest.approx(
        np.asarray(m.function(x6), dtype=float)
    )


def test_pre_gate_changes_speed_not_results(
    batches: list[tuple[datetime, set[Detection]]],
) -> None:
    gated = build_tracker(tracker_spec("ekf-cv-gnn"))
    plain = build_tracker(tracker_spec("ekf-cv-gnn"))
    plain.data_associator = GNNWith2DAssignment(plain.data_associator.hypothesiser.hypothesiser)
    confirm = plain.initiator
    confirm.data_associator = GNNWith2DAssignment(confirm.data_associator.hypothesiser.hypothesiser)

    def summary(tracks: set[Any]) -> list[tuple[Any, ...]]:
        return sorted(
            (
                t.states[0].timestamp,
                len(t),
                tuple(np.round(np.asarray(t.state_vector, dtype=float).ravel(), 6)),
            )
            for t in tracks
        )

    # The first minute: without the pre-gate this is ~30x slower.
    minute = batches[:12]
    assert summary(run(gated, minute)) == summary(run(plain, minute))


@pytest.mark.parametrize("name", ["ekf-cv-gnn", "ukf-cv-gnn", "pf-mm-gnn"])
def test_every_tracker_follows_aircraft(
    name: str, truth: TruthSet, batches: list[tuple[datetime, set[Detection]]]
) -> None:
    pipe = build_tracker(tracker_spec(name))
    tracks = run(pipe, batches)
    # Every long track, not "the longest": several tie, and set order would pick one.
    long_tracks = [t for t in tracks if len(t) > 20]
    assert len(long_tracks) >= 3
    px, py, _ = pipe.position_mapping
    medians = []
    for track in long_tracks:
        errors = []
        for state in track.states[5:]:
            mean = np.asarray(state.mean, dtype=float).ravel()
            errors.append(
                min(
                    np.hypot(mean[px] - s.position[0], mean[py] - s.position[1])
                    for s in truth.states_at(state.timestamp).values()
                )
            )
        medians.append(np.median(errors))
    # A few hundred metres: noise plus the assumed-height bias at short range.
    assert np.median(medians) < 500.0
    longest = max(long_tracks, key=len)
    if name == "pf-mm-gnn":
        assert isinstance(longest.state, MultiModelParticleState)
