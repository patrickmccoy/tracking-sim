from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from tracking_sim.geo import LocalFrame
from tracking_sim.sensors import load_sensor_spec
from tracking_sim.sim import ReplayEngine, SimulationResult
from tracking_sim.tracking import load_tracker_spec
from tracking_sim.tracking.runner import detection_batches, run_tracker
from tracking_sim.truth import build_truth
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ROOT = Path(__file__).resolve().parents[1]
FRAME = LocalFrame(39.9088, -105.1172, 1729.0)
EPOCH = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
START = EPOCH + timedelta(seconds=60)


@pytest.fixture(scope="module")
def simulation() -> SimulationResult:
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    truth = build_truth(traffic.records, FRAME)
    spec = load_sensor_spec(ROOT / "scenarios/sensors/radar-1.yaml")
    return ReplayEngine(truth, [spec], seed=1).run(START, START + timedelta(minutes=2))


def tracker(name: str) -> Any:
    return load_tracker_spec(ROOT / f"scenarios/trackers/{name}.yaml")


def test_batches_are_one_per_scan_in_report_order(simulation: SimulationResult) -> None:
    batches = detection_batches(simulation.scans)
    assert len(batches) == len(simulation.scans)
    times = [t for t, _ in batches]
    assert times == sorted(times)
    for (t, dets), scan in zip(batches, simulation.scans, strict=True):
        assert t == scan.end and dets == set(scan.detections)
        assert all(d.timestamp <= t for d in dets)


@pytest.mark.parametrize("name", ["ekf-cv-gnn", "pf-mm-gnn"])
def test_same_seed_same_tracks(name: str, simulation: SimulationResult) -> None:
    batches = detection_batches(simulation.scans)
    a = run_tracker(tracker(name), batches, seed=3)
    b = run_tracker(tracker(name), batches, seed=3)
    assert a.tracks_table().num_rows > 0
    assert a.tracks_table().equals(b.tracks_table())
    if name == "pf-mm-gnn":
        c = run_tracker(tracker(name), batches, seed=4)
        assert not a.tracks_table().equals(c.tracks_table())


def test_reported_tracks_start_at_confirmation(simulation: SimulationResult) -> None:
    run = run_tracker(tracker("ekf-cv-gnn"), detection_batches(simulation.scans), seed=1)
    reported = run.reported_tracks()
    assert [t.id for t in reported] == run.labels
    assert run.labels[0] == "ekf-cv-gnn:0001"
    for track, full, when in zip(reported, run.tracks, run.confirmed_at, strict=True):
        assert track.states[0].timestamp == when
        updates = sum(1 for s in full.states if s.timestamp <= when and hasattr(s, "hypothesis"))
        assert updates >= 3
    table = run.tracks_table()
    confirmed = table.filter(table["confirmed"])
    assert confirmed.num_rows == sum(len(t) for t in reported)
    hits = table.filter(pc.equal(table["detection_kind"], "target"))
    assert pc.count_distinct(hits["segment_id"]).as_py() >= 3


def test_tracks_parquet_round_trip(simulation: SimulationResult, tmp_path: Path) -> None:
    run = run_tracker(tracker("ukf-cv-gnn"), detection_batches(simulation.scans), seed=1)
    path = run.write(tmp_path / "tracks" / "ukf.parquet")
    assert pq.read_table(path).equals(run.tracks_table())
    assert run.elapsed_s > 0
