import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import yaml

from tracking_sim.cli import main
from tracking_sim.sim.scenario import load_scenario
from tracking_sim.truth.sources.recording import write_records
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ROOT = Path(__file__).resolve().parents[1]


def test_example_scenario_loads() -> None:
    scenario = load_scenario(ROOT / "scenarios/kbjc-synthetic.yaml")
    assert scenario.spec.name == "kbjc-synthetic" and scenario.spec.seed == 42
    assert [t.id for t in scenario.trackers] == ["ekf-cv-gnn", "ukf-cv-gnn", "pf-mm-gnn"]
    assert [s.id for s in scenario.sensors] == ["radar-1"]
    assert scenario.truth_path == (ROOT / "tests/data/synthetic_1h.parquet").resolve()
    assert scenario.truth_path.exists()
    assert scenario.frame.lat == pytest.approx(39.9088)
    resolved = scenario.resolved()
    assert resolved["sensors"][0]["id"] == "radar-1" and len(resolved["trackers"]) == 3


@pytest.fixture
def small_scenario(tmp_path: Path) -> Path:
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    write_records(tmp_path / "truth.parquet", traffic.records)
    scenario = {
        "name": "small",
        "truth": {
            "path": "truth.parquet",
            "window": ["2026-11-01T23:01:00Z", "2026-11-01T23:04:00Z"],
        },
        "sensors": [str(ROOT / "scenarios/sensors/radar-1.yaml")],
        "trackers": [str(p) for p in sorted((ROOT / "scenarios/trackers").glob("*.yaml"))],
        "seed": 7,
    }
    path = tmp_path / "small.yaml"
    path.write_text(yaml.safe_dump(scenario))
    return path


def test_run_command_prints_the_comparison_and_writes_outputs(
    small_scenario: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "run"
    assert main(["run", str(small_scenario), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "scenario small, seed 7" in printed
    assert "achieved Pd" in printed
    rows = [line.split()[0] for line in printed.splitlines() if line.endswith(tuple("0123456789"))]
    assert {"ekf-cv-gnn", "ukf-cv-gnn", "pf-mm-gnn"} <= set(rows)
    for name in ("detections.parquet", "opportunities.parquet", "comparison.txt"):
        assert (out / name).exists()
    for tracker in ("ekf-cv-gnn", "ukf-cv-gnn", "pf-mm-gnn"):
        assert pq.read_table(out / "tracks" / f"{tracker}.parquet").num_rows > 0
    metrics = json.loads((out / "metrics.json").read_text())
    assert [t["tracker"] for t in metrics["trackers"]] == ["ekf-cv-gnn", "pf-mm-gnn", "ukf-cv-gnn"]
    assert metrics["trackers"][0]["over_time"][0]["ospa_m"] >= 0
    resolved = yaml.safe_load((out / "scenario.resolved.yaml").read_text())
    assert resolved["seed"] == 7 and resolved["sensors"][0]["id"] == "radar-1"


def test_seed_override_and_reproducibility(small_scenario: Path, tmp_path: Path) -> None:
    for name, seed in (("a", "11"), ("b", "11"), ("c", "12")):
        main(["run", str(small_scenario), "--seed", seed, "--out", str(tmp_path / name)])

    def table(name: str, tracker: str) -> object:
        return pq.read_table(tmp_path / name / "tracks" / f"{tracker}.parquet")

    for tracker in ("ekf-cv-gnn", "pf-mm-gnn"):
        assert table("a", tracker).equals(table("b", tracker))
        assert not table("a", tracker).equals(table("c", tracker))
    assert (tmp_path / "a" / "comparison.txt").read_text().splitlines()[2:] != []


def test_bad_scenarios_fail(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"name": "x", "truth": {"path": "t.parquet"}, "sensors": []}))
    with pytest.raises(ValueError):
        load_scenario(bad)
    dup = tmp_path / "dup.yaml"
    tracker = str(ROOT / "scenarios/trackers/ekf-cv-gnn.yaml")
    dup.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "truth": {"path": "t.parquet"},
                "sensors": [str(ROOT / "scenarios/sensors/radar-1.yaml")],
                "trackers": [tracker, tracker],
            }
        )
    )
    with pytest.raises(ValueError, match="unique"):
        load_scenario(dup)


def test_terrain_example_scenario_loads() -> None:
    scenario = load_scenario(ROOT / "scenarios/kbjc-terrain.yaml")
    assert [s.type for s in scenario.sensors] == ["rotating_radar_2d", "passive_bearing"]
    assert scenario.spec.terrain is not None and scenario.spec.terrain.source == "copernicus"
    assert scenario.sensors[0].detection.model == "radar_equation"
    assert len(scenario.trackers) == 3


def test_with_truth_swaps_in_a_capture(small_scenario: Path, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from tracking_sim.sim.scenario import _load_truth
    from tracking_sim.truth.sources import is_sbs_capture, open_recording
    from tracking_sim.truth.sources.recording import RecordingSource
    from tracking_sim.truth.sources.sbs import SbsSource

    sbs = ROOT / "tests/data/synthetic_5min.sbs"
    assert is_sbs_capture(sbs) and is_sbs_capture("feed.sbs.gz")
    assert not is_sbs_capture(ROOT / "tests/data/synthetic_1h.parquet")
    assert isinstance(open_recording(sbs), SbsSource)
    assert isinstance(open_recording(ROOT / "tests/data/synthetic_1h.parquet"), RecordingSource)

    scenario = load_scenario(small_scenario)
    capture = scenario.with_truth(sbs)
    # Same sensors and trackers; the capture's own time span, not the scenario's window.
    assert capture.spec.truth.source == "sbs" and capture.spec.truth.window is None
    assert capture.truth_path == sbs.resolve() and capture.sensors == scenario.sensors
    assert capture.spec.seed == scenario.spec.seed
    assert capture.input_digests()["truth"] != scenario.input_digests()["truth"]
    assert len(_load_truth(capture)) > 0

    window = (datetime(2026, 11, 1, 23, 1, tzinfo=UTC), datetime(2026, 11, 1, 23, 2, tzinfo=UTC))
    assert scenario.with_truth(sbs, window=window).spec.truth.window == window
    with pytest.raises(ValueError, match="end after"):
        scenario.with_truth(sbs, window=(window[1], window[0]))
