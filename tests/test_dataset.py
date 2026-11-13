import math
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import yaml
from stonesoup.types.detection import Clutter, TrueDetection
from stonesoup.types.state import State
from stonesoup.types.track import Track

from tracking_sim.eval import scoring_grid
from tracking_sim.eval.runstore import RunStore
from tracking_sim.geo import LocalFrame
from tracking_sim.sensors import load_sensor_spec
from tracking_sim.sim import ReplayEngine
from tracking_sim.sim.dataset import TrackingData, load_run, simulate, tracks_following
from tracking_sim.sim.scenario import load_scenario
from tracking_sim.tracking import load_tracker_spec
from tracking_sim.tracking.runner import run_tracker
from tracking_sim.truth import TruthSet, build_truth
from tracking_sim.truth.sources.recording import write_records
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ROOT = Path(__file__).resolve().parents[1]
FRAME = LocalFrame(39.9088, -105.1172, 1729.0)
EPOCH = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
START, END = EPOCH + timedelta(minutes=1), EPOCH + timedelta(minutes=5)
RADAR = load_sensor_spec(ROOT / "scenarios/sensors/radar-1.yaml")


@pytest.fixture(scope="module")
def traffic():  # type: ignore[no-untyped-def]
    return generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )


@pytest.fixture(scope="module")
def truth(traffic) -> TruthSet:  # type: ignore[no-untyped-def]
    return build_truth(traffic.records, FRAME)


@pytest.fixture(scope="module")
def data(truth: TruthSet) -> TrackingData:
    return simulate(truth, RADAR, seed=3, start=START, end=END)


def test_simulated_data_holds_the_radars_scans(truth: TruthSet, data: TrackingData) -> None:
    assert data.sensor_id == "radar-1" and data.period_s == 5.0
    assert len(data.scans) == 48 and data.start == START
    batches = data.batches()
    assert len(batches) == 48 and [t for t, _ in batches] == [s.end for s in data.scans]
    detections = data.detections()
    assert sum(len(b) for _, b in batches) == len(detections)
    assert any(isinstance(d, Clutter) for d in detections)
    assert data.pd == 0.9
    cov = RADAR.coverage
    assert data.clutter_density() == pytest.approx(
        data.false_alarms_per_scan / (2 * math.pi * (cov.range_max_m - cov.range_min_m))
    )
    # The scoring grid is the one the scenario runner builds.
    sim = ReplayEngine(truth, [RADAR], seed=3).run(START, END)
    grid, again = scoring_grid(truth, sim), data.grid()
    assert again.times == grid.times
    assert [p.id for p in again.truths] == [p.id for p in grid.truths]


def test_aircraft_table_and_one_aircrafts_detections(data: TrackingData) -> None:
    table = data.aircraft()
    counts = table["detections"].to_pylist()
    assert counts == sorted(counts, reverse=True) and counts[0] > 10
    busiest = table["segment_id"][0].as_py()
    mine = data.detections_of(busiest)
    assert len(mine) == counts[0]
    assert all(isinstance(d, TrueDetection) for d in mine)
    assert [d.timestamp for d in mine] == sorted(d.timestamp for d in mine)
    assert data.truth_path(busiest).id == busiest
    # Plot positions: slant range on the ground, so near the truth but further out.
    xy = data.plot_positions(mine)
    pos, _, _ = data.truth.segment(busiest).evaluate([d.timestamp for d in mine])
    assert np.median(np.hypot(*(xy - pos[:, :2]).T)) < 1500
    assert all(0 <= t < 30 for t in table["turn_deg_s"].to_pylist())


def test_subset_keeps_whole_scans_near_one_aircraft(data: TrackingData) -> None:
    busiest = data.aircraft()["segment_id"][0].as_py()
    lo, hi = START + timedelta(seconds=62), START + timedelta(minutes=3)
    part = data.subset(lo, hi, around=busiest, radius_m=20_000)
    assert part.start >= lo and part.end <= hi and part.start - lo < timedelta(seconds=5)
    centre = data.truth.segment(busiest)
    kept = part.detections()
    assert kept and len(kept) < len(data.subset(lo, hi).detections())
    pos, _, _ = centre.evaluate([d.timestamp for d in kept])
    assert np.all(np.hypot(*(part.plot_positions(kept) - pos[:, :2]).T) <= 20_000)
    assert busiest in part.looks and len(part.looks) < len(data.looks)
    assert "within 20 km" in part.label
    with pytest.raises(ValueError, match="radius_m"):
        data.subset(around=busiest)
    with pytest.raises(ValueError, match="no whole scans"):
        data.subset(END, END + timedelta(minutes=1))


def test_scoring_plain_tracks(data: TrackingData) -> None:
    # Truth itself, as tracks, scores almost perfectly.
    grid = data.grid()
    tracks = []
    for path in grid.truths:
        states = [State(s.state_vector, timestamp=s.timestamp) for s in path.states]
        tracks.append(Track(states, id=path.id))
    score = data.score("truth", tracks, grid=grid)
    assert score.mean_ospa_m < 1.0
    assert score.siap["completeness"] == pytest.approx(1.0)


def test_tracks_following(truth: TruthSet, data: TrackingData) -> None:
    spec = load_tracker_spec(ROOT / "scenarios/trackers/ekf-cv-gnn.yaml")
    run = run_tracker(spec, data.batches(), seed=1)
    busiest = data.aircraft()["segment_id"][0].as_py()
    mine = tracks_following(run.tracks, busiest)
    assert mine
    lengths = [len(t.states) for t in mine]
    assert lengths == sorted(lengths, reverse=True)
    assert not tracks_following(run.tracks, "nobody")


def test_load_run_gives_back_the_runs_detections(traffic, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    write_records(tmp_path / "truth.parquet", traffic.records)
    scenario = {
        "name": "small",
        "truth": {"path": "truth.parquet", "window": [START.isoformat(), END.isoformat()]},
        "sensors": [str(ROOT / "scenarios/sensors/radar-1.yaml")],
        "trackers": [str(ROOT / "scenarios/trackers/ekf-cv-gnn.yaml")],
        "seed": 3,
    }
    (tmp_path / "small.yaml").write_text(yaml.safe_dump(scenario))
    store = RunStore(tmp_path / "runs")
    record = store.run(load_scenario(tmp_path / "small.yaml"))

    loaded = load_run(record.id[:12], store=tmp_path / "runs")
    assert loaded.label == f"run {record.id}"
    fresh = simulate(loaded.truth, RADAR, seed=3, start=START, end=END)
    assert [s.end for s in loaded.scans] == [s.end for s in fresh.scans]

    def rows(d: TrackingData) -> list[tuple[object, ...]]:
        return [
            (
                x.timestamp,
                type(x).__name__,
                x.metadata.get("segment_id"),
                round(float(x.state_vector[0, 0]), 9),
                round(float(x.state_vector[1, 0]), 6),
            )
            for x in d.detections()
        ]

    assert rows(loaded) == rows(fresh)
    det = loaded.detections()[0]
    assert det.measurement_model.function(State(np.zeros((6, 1)))) is not None
    assert [p.id for p in loaded.grid().truths] == [p.id for p in fresh.grid().truths]
    # The run's own tracks come back too, as 6D tracks that score.
    stored = loaded.stored("ekf-cv-gnn")
    assert stored and all(len(t.states) for t in stored)
    assert loaded.score("stored", stored).mean_ospa_m < 1000

    # A changed truth file is refused rather than silently used.
    run_dir = tmp_path / "copy"
    shutil.copytree(record.path, run_dir)
    for p in run_dir.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)
    write_records(tmp_path / "truth.parquet", traffic.records[:-10])
    with pytest.raises(ValueError, match="has changed"):
        load_run(run_dir)


def test_clip_cuts_tracks_to_the_subset(data: TrackingData) -> None:
    busiest = data.aircraft()["segment_id"][0].as_py()
    lo = START + timedelta(minutes=1)
    part = data.subset(lo, lo + timedelta(minutes=2), around=busiest, radius_m=10_000)
    assert part.area == (busiest, 10_000.0)
    centre = data.truth.segment(busiest)
    # A straight track that passes through the area, leaves, and comes back.
    times = [START + timedelta(seconds=10 * k) for k in range(30)]
    pos, _, _ = centre.evaluate(times)
    offsets = [0 if k % 6 < 3 else 50_000 for k in range(30)]  # in 30 s, out 30 s, ...
    states = [
        State(np.array([[p[0] + off], [0], [p[1]], [0], [p[2]], [0]]), timestamp=t)
        for t, p, off in zip(times, pos, offsets, strict=True)
    ]
    pieces = part.clip([Track(states, id="x"), Track([], id="empty")])
    assert [p.id for p in pieces] == ["x/1", "x/2"]
    for piece in pieces:
        for s in piece.states:
            assert part.start <= s.timestamp < part.end
    # Without an area, only time clips: states 40-90 s in keep those from 60 s.
    (whole,) = data.subset(lo, lo + timedelta(minutes=2)).clip([Track(states[4:10], id="y")])
    assert [s.timestamp for s in whole.states] == times[6:10]
