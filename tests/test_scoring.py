from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from tracking_sim.eval import scoring_grid
from tracking_sim.eval.scoring import segment_of
from tracking_sim.geo import LocalFrame
from tracking_sim.sensors import load_sensor_spec, parse_sensor_spec
from tracking_sim.sim import ReplayEngine
from tracking_sim.tracking import load_tracker_spec
from tracking_sim.tracking.runner import detection_batches, run_tracker
from tracking_sim.truth import TruthSet, build_truth
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ROOT = Path(__file__).resolve().parents[1]
FRAME = LocalFrame(39.9088, -105.1172, 1729.0)
EPOCH = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
START, END = EPOCH + timedelta(seconds=60), EPOCH + timedelta(seconds=240)


@pytest.fixture(scope="module")
def truth() -> TruthSet:
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    return build_truth(traffic.records, FRAME)


def test_truth_counts_only_while_in_coverage(truth: TruthSet) -> None:
    data = load_sensor_spec(ROOT / "scenarios/sensors/radar-1.yaml").model_dump()
    data["coverage"]["range_max_m"] = 40_000
    radar = parse_sensor_spec(data)
    sim = ReplayEngine(truth, [radar], seed=1).run(START, END)
    grid = scoring_grid(truth, sim)
    assert grid.times == [s.end for s in sim.scans]
    assert grid.period_s == 5.0
    in_coverage = {c.segment_id for s in sim.scans for c in s.opportunities}
    assert {segment_of(p.id) for p in grid.truths} == in_coverage
    # Each path is one continuous stretch of visibility.
    for path in grid.truths:
        gaps = np.diff([s.timestamp.timestamp() for s in path.states])
        assert np.all(gaps <= 5.0 + 1e-6)
    site = sim.radars[radar.id].site
    for path in grid.truths:
        for state in path.states:
            assert state.timestamp in grid.times
            sv = np.asarray(state.state_vector, dtype=float).ravel()
            _, _, rng = site.polar(sv[[0, 2, 4]])
            assert rng[0] < 40_000 + 5 * 300  # in range, give or take a scan of flight
            expected = truth.segment(segment_of(path.id)).state_at(state.timestamp)
            assert sv[[0, 2, 4]] == pytest.approx(expected.position)


def test_tracks_on_the_grid_extrapolate_the_latest_state(truth: TruthSet) -> None:
    radar = load_sensor_spec(ROOT / "scenarios/sensors/radar-1.yaml")
    sim = ReplayEngine(truth, [radar], seed=1).run(START, END)
    grid = scoring_grid(truth, sim)
    spec = load_tracker_spec(ROOT / "scenarios/trackers/pf-mm-gnn.yaml")
    run = run_tracker(spec, detection_batches(sim.scans), seed=1)
    tracks = grid.tracks(run)
    assert tracks and {t.id for t in tracks} <= set(run.labels)
    reported = {t.id: t for t in run.reported_tracks()}
    for track in tracks:
        source = reported[track.id]
        assert track.states[0].timestamp >= source.states[0].timestamp
        assert track.states[-1].timestamp <= source.states[-1].timestamp
        state = track.states[len(track.states) // 2]
        assert state.timestamp in grid.times
        latest = [s for s in source.states if s.timestamp <= state.timestamp][-1]
        mean = np.asarray(latest.mean, dtype=float).ravel()
        dt = (state.timestamp - latest.timestamp).total_seconds()
        sv = np.asarray(state.state_vector, dtype=float).ravel()
        # 7D layout (x, vx, y, vy, w, z, vz) mapped to 6D (x, vx, y, vy, z, vz).
        assert sv[0] == pytest.approx(mean[0] + dt * mean[1])
        assert sv[2] == pytest.approx(mean[2] + dt * mean[3])
        assert sv[4] == pytest.approx(mean[5] + dt * mean[6])


def test_grid_from_looks_matches_the_simulation(truth: TruthSet) -> None:
    from tracking_sim.eval.scoring import grid_from_looks

    radar = load_sensor_spec(ROOT / "scenarios/sensors/radar-1.yaml")
    sim = ReplayEngine(truth, [radar], seed=1).run(START, END)
    grid = scoring_grid(truth, sim)
    looks: dict[str, list[datetime]] = {}
    for scan in sim.scans:
        for c in scan.opportunities:
            looks.setdefault(c.segment_id, []).append(c.time)
    again = grid_from_looks(truth, [s.end for s in sim.scans], 5.0, looks)
    assert again.times == grid.times
    assert [p.id for p in again.truths] == [p.id for p in grid.truths]
    assert [len(p.states) for p in again.truths] == [len(p.states) for p in grid.truths]
    # Plain Stone Soup tracks sample the same way as a tracker run's.
    path = truth.segment(segment_of(grid.truths[0].id))
    t = [grid.times[0] - timedelta(seconds=7), grid.times[0] + timedelta(seconds=3)]
    from stonesoup.types.state import State
    from stonesoup.types.track import Track

    pos, vel, _ = path.evaluate(t)
    states = [
        State(np.array([p[0], v[0], p[1], v[1], p[2], v[2]]).reshape(6, 1), timestamp=ti)
        for ti, p, v in zip(t, pos, vel, strict=True)
    ]
    (sampled,) = grid.sample([Track(states, id="x"), Track([], id="empty")])
    assert sampled.states[0].timestamp == grid.times[0]
    expected = pos[0] + 7 * vel[0]
    assert np.asarray(sampled.states[0].state_vector, dtype=float).ravel()[[0, 2, 4]] == (
        pytest.approx(expected)
    )
