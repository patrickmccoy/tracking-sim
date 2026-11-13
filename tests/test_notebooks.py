"""Execute the notebooks end to end on the synthetic fixture."""

from pathlib import Path

import pytest

nbformat = pytest.importorskip("nbformat")
nbclient = pytest.importorskip("nbclient")
pytest.importorskip("matplotlib")
pytest.importorskip("pandas")

NOTEBOOKS = sorted((Path(__file__).resolve().parents[1] / "notebooks").glob("*.ipynb"))

# What each notebook must print when it works.
EXPECTED = {
    "01_truth_playground.ipynb": (["AGREE"], ["DISAGREE"]),
    "02_one_sensor.ipynb": (
        ["configured 0.9: consistent", "identical = True", "same beam crossings: True"],
        ["NOT consistent"],
    ),
    "04_scenarios_and_terrain.ipynb": (
        [
            "terrain: synthetic ridge",
            "loses most low-altitude traffic on those bearings: True",
            "expected (radar_equation)",
            "expected (one_way)",
            "reused stored run: True",
        ],
        ["NOT consistent"],
    ),
    "03_tracking.ipynb": (
        ["trackers: ekf-cv-gnn, ukf-cv-gnn, pf-mm-gnn", "lowest mean OSPA:", "configured 0.900"],
        [],
    ),
    "05_stone_soup_walkthrough.ipynb": (
        [
            "EKF: median error",
            "UKF: median error",
            "PF: median error",
            "EKF turn: median error",
            "PF multiple model: median error",
            "EKF smoothed: median error",
            "final scan:",
            "GNN:",
            "JPDA:",
            "MFA: ",
            "lowest mean OSPA:",
        ],
        ["MFA skipped"],
    ),
}


RELATIVE_PATHS = {
    "01_truth_playground.ipynb": {"TRACKING_SIM_RECORDING": "tests/data/synthetic_1h.parquet"},
    "02_one_sensor.ipynb": {
        "TRACKING_SIM_RECORDING": "tests/data/synthetic_1h.parquet",
        "TRACKING_SIM_SENSOR": "scenarios/sensors/radar-1.yaml",
    },
    "03_tracking.ipynb": {"TRACKING_SIM_SCENARIO": "scenarios/kbjc-synthetic.yaml"},
    "05_stone_soup_walkthrough.ipynb": {"TRACKING_SIM_SENSOR": "scenarios/sensors/radar-1.yaml"},
}


def test_every_notebook_has_expectations() -> None:
    assert {p.name for p in NOTEBOOKS} == set(EXPECTED)


@pytest.mark.notebook
@pytest.mark.parametrize("path", NOTEBOOKS, ids=[p.name for p in NOTEBOOKS])
def test_notebook_runs(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TRACKING_SIM_RECORDING",
        "TRACKING_SIM_ORIGIN",
        "TRACKING_SIM_WINDOW",
        "TRACKING_SIM_SECOND",
        "TRACKING_SIM_SBS_TZ",
        "TRACKING_SIM_SENSOR",
        "TRACKING_SIM_SEED",
        "TRACKING_SIM_SCENARIO",
        "TRACKING_SIM_RUN",
        "TRACKING_SIM_RUNS",
        "TRACKING_SIM_AIRCRAFT",
    ):
        monkeypatch.delenv(name, raising=False)
    # Relative paths are from the repository root, not the notebooks directory
    # Jupyter runs in: these name the defaults, so the expectations still hold.
    for name, value in RELATIVE_PATHS.get(path.name, {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MPLBACKEND", "Agg")
    # Notebook 04 downloads real terrain by default; tests stay offline.
    monkeypatch.setenv("TRACKING_SIM_TERRAIN", "synthetic")
    nb = nbformat.read(path, as_version=4)
    client = nbclient.NotebookClient(
        nb, timeout=600, kernel_name="python3", resources={"metadata": {"path": str(path.parent)}}
    )
    client.execute()
    outputs = [o for cell in nb.cells for o in cell.get("outputs", [])]
    assert not [o for o in outputs if o.get("output_type") == "error"]
    text = "".join(o.get("text", "") for o in outputs)
    present, absent = EXPECTED[path.name]
    assert all(s in text for s in present), [s for s in present if s not in text]
    assert not any(s in text for s in absent)
