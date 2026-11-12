import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from tracking_sim.cli import main
from tracking_sim.eval import runstore
from tracking_sim.eval.runstore import RunStore
from tracking_sim.sim.scenario import load_scenario
from tracking_sim.truth.sources.recording import write_records
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ROOT = Path(__file__).resolve().parents[1]


def scenario_file(tmp_path: Path, *, seed: int = 7, name: str = "small") -> Path:
    truth = tmp_path / "truth.parquet"
    if not truth.exists():
        traffic = generate_traffic(
            seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
        )
        write_records(truth, traffic.records)
    scenario: dict[str, Any] = {
        "name": name,
        "truth": {
            "path": "truth.parquet",
            "window": ["2026-11-01T23:01:00Z", "2026-11-01T23:04:00Z"],
        },
        "sensors": [str(ROOT / "scenarios/sensors/radar-1.yaml")],
        # An inline tracker spec, in place of a path.
        "trackers": [{"id": "ekf", "filter": "ekf"}],
        "seed": seed,
    }
    path = tmp_path / f"{name}-{seed}.yaml"
    path.write_text(yaml.safe_dump(scenario))
    return path


def test_inline_specs_load(tmp_path: Path) -> None:
    scenario = load_scenario(scenario_file(tmp_path))
    assert [t.id for t in scenario.trackers] == ["ekf"]
    assert scenario.trackers[0].filter == "ekf"


def test_a_run_is_stored_once_and_reused(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    scenario = load_scenario(scenario_file(tmp_path))
    first = store.run(scenario)
    assert not first.reused and first.path.parent == store.root
    assert first.id.startswith("small-") and len(first.id) == len("small-") + 12
    for name in (
        "manifest.json",
        "metrics.json",
        "comparison.txt",
        "summary.txt",
        "detections.parquet",
        "opportunities.parquet",
        "tracks/ekf.parquet",
        "scenario.resolved.yaml",
    ):
        path = first.path / name
        assert path.exists(), name
        assert not os.access(path, os.W_OK), f"{name} should be read-only"
    manifest = first.manifest
    assert manifest["scenario"]["seed"] == 7 and len(manifest["inputs"]["truth"]) == 64
    assert manifest["code"]["hashed"]["libraries"]["stonesoup"].startswith("1.9")
    assert "ekf" in first.table and "achieved Pd" in first.summary

    again = store.run(load_scenario(scenario_file(tmp_path)))
    assert again.reused and again.id == first.id
    assert again.manifest["created"] == first.manifest["created"]
    assert [r.id for r in store.list()] == [first.id]
    assert not any(p.name.startswith(".") for p in store.root.iterdir())  # no staging left


def test_the_id_changes_with_seed_data_and_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path / "runs")
    base, _ = store.plan(load_scenario(scenario_file(tmp_path)))
    other_seed, _ = store.plan(load_scenario(scenario_file(tmp_path, seed=8)))
    assert other_seed != base

    real = runstore.code_identity()
    changed = {**real, "hashed": {**real["hashed"], "source_sha256": "0" * 64}}
    monkeypatch.setattr(runstore, "code_identity", lambda: changed)
    other_code, _ = store.plan(load_scenario(scenario_file(tmp_path)))
    assert other_code != base
    monkeypatch.undo()

    # Notes for people (git state) don't change the id.
    noted = {**real, "git_commit": "something else", "git_uncommitted_changes": True}
    monkeypatch.setattr(runstore, "code_identity", lambda: noted)
    assert store.plan(load_scenario(scenario_file(tmp_path)))[0] == base
    monkeypatch.undo()

    truth = tmp_path / "truth.parquet"
    truth.write_bytes(truth.read_bytes() + b"\0")
    assert store.plan(load_scenario(scenario_file(tmp_path)))[0] != base


def test_a_failed_run_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tracking_sim.sim.scenario as scenario_module

    def broken(_: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(scenario_module, "run_scenario", broken)
    store = RunStore(tmp_path / "runs")
    with pytest.raises(RuntimeError, match="boom"):
        store.run(load_scenario(scenario_file(tmp_path)))
    assert not store.root.exists() or not any(store.root.iterdir())


def test_runs_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs = str(tmp_path / "runs")
    assert main(["run", str(scenario_file(tmp_path)), "--runs", runs]) == 0
    assert main(["run", str(scenario_file(tmp_path, seed=8)), "--runs", runs]) == 0
    out = capsys.readouterr()
    assert "stored run small-" in out.err
    assert main(["run", str(scenario_file(tmp_path)), "--runs", runs]) == 0
    assert "reused stored run" in capsys.readouterr().err

    store = RunStore(runs)
    a, b = (r.id for r in store.list())
    assert main(["runs", "list", "--runs", runs]) == 0
    listing = capsys.readouterr().out
    assert a in listing and b in listing and "best OSPA" in listing
    assert main(["runs", "show", a[:10] if a[:10] != b[:10] else a, "--runs", runs]) == 0
    assert f"run {a}" in capsys.readouterr().out
    assert main(["runs", "compare", a, b, "--runs", runs]) == 0
    compared = capsys.readouterr().out
    assert "seed: 7 -> 8" in compared and "ekf: ospa_m" in compared
    with pytest.raises(KeyError):
        store.get("nothing-like-this")
    metrics = json.loads((store.root / a / "metrics.json").read_text())
    assert metrics["trackers"][0]["tracker"] == "ekf"
