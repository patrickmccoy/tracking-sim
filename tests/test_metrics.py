from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from stonesoup.types.state import State
from stonesoup.types.track import Track

from tracking_sim.eval import comparison_table, score_run, score_tracks, scoring_grid
from tracking_sim.eval.scoring import ScoringGrid
from tracking_sim.geo import LocalFrame
from tracking_sim.sensors import load_sensor_spec
from tracking_sim.sim import ReplayEngine
from tracking_sim.tracking import load_tracker_spec
from tracking_sim.tracking.runner import detection_batches, run_tracker
from tracking_sim.truth import build_truth
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ROOT = Path(__file__).resolve().parents[1]
FRAME = LocalFrame(39.9088, -105.1172, 1729.0)
EPOCH = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
START, END = EPOCH + timedelta(seconds=60), EPOCH + timedelta(seconds=300)


@pytest.fixture(scope="module")
def setup() -> tuple[ScoringGrid, object]:
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    truth = build_truth(traffic.records, FRAME)
    radar = load_sensor_spec(ROOT / "scenarios/sensors/radar-1.yaml")
    sim = ReplayEngine(truth, [radar], seed=1).run(START, END)
    return scoring_grid(truth, sim), sim


def as_tracks(grid: ScoringGrid, offset_m: float = 0.0) -> list[Track]:
    tracks = []
    for path in grid.truths:
        states = []
        for s in path.states:
            v = np.asarray(s.state_vector, dtype=float).copy()
            v[0] += offset_m
            states.append(State(v, timestamp=s.timestamp))
        tracks.append(Track(states, id=f"copy-{path.id}"))
    return tracks


def test_truth_as_tracks_scores_perfectly(setup: tuple[ScoringGrid, object]) -> None:
    grid, _ = setup
    score = score_tracks("perfect", as_tracks(grid), grid)
    assert score.mean_ospa_m == pytest.approx(0.0, abs=1e-6)
    assert score.mean_gospa() == pytest.approx(0.0, abs=1e-6)
    assert score.siap["completeness"] == pytest.approx(1.0)
    assert score.siap["ambiguity"] == pytest.approx(1.0)
    assert score.siap["spuriousness"] == pytest.approx(0.0)
    assert score.siap["position_accuracy_m"] == pytest.approx(0.0, abs=1e-6)
    assert score.never_tracked == []
    # Association needs 3 consecutive grid times, so latency is at most 2 scans.
    assert max(score.latency_s.values()) <= 2 * grid.period_s


def test_offsets_and_missing_tracks_show_up(setup: tuple[ScoringGrid, object]) -> None:
    grid, _ = setup
    shifted = score_tracks("shifted", as_tracks(grid, offset_m=200.0), grid)
    assert shifted.mean_ospa_m == pytest.approx(200.0, rel=0.01)
    assert shifted.siap["position_accuracy_m"] == pytest.approx(200.0, rel=0.01)
    assert shifted.mean_gospa("localisation") > 0
    half = as_tracks(grid)[: len(grid.truths) // 2]
    partial = score_tracks("half", half, grid)
    assert partial.siap["completeness"] < 1.0 and partial.mean_gospa("missed") > 0
    assert len(partial.never_tracked) == len(grid.truths) - len(half)
    none = score_tracks("none", [], grid)
    assert none.mean_ospa_m == pytest.approx(1000.0)  # every truth missed: the cut-off


def test_scores_for_a_real_tracker_and_the_table(setup: tuple[ScoringGrid, object]) -> None:
    grid, sim = setup
    spec = load_tracker_spec(ROOT / "scenarios/trackers/ekf-cv-gnn.yaml")
    run = run_tracker(spec, detection_batches(sim.scans), seed=1)  # type: ignore[attr-defined]
    score = score_run(run, grid)
    assert 0 < score.mean_ospa_m < 1000
    assert 0.5 < score.siap["completeness"] <= 1.0
    assert len(score.ospa) == len(score.times) > 10
    table = comparison_table([score, score_tracks("perfect", as_tracks(grid), grid)])
    lines = table.splitlines()
    assert lines[0].startswith("tracker") and "OSPA m" in lines[0]
    assert lines[2].startswith("ekf-cv-gnn") and lines[3].startswith("perfect")
