"""Execute the notebooks end to end on the synthetic fixture."""

from pathlib import Path

import pytest

nbformat = pytest.importorskip("nbformat")
nbclient = pytest.importorskip("nbclient")
pytest.importorskip("matplotlib")

NOTEBOOKS = sorted((Path(__file__).resolve().parents[1] / "notebooks").glob("*.ipynb"))


@pytest.mark.notebook
@pytest.mark.parametrize("path", NOTEBOOKS, ids=[p.name for p in NOTEBOOKS])
def test_notebook_runs(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TRACKING_SIM_RECORDING",
        "TRACKING_SIM_ORIGIN",
        "TRACKING_SIM_WINDOW",
        "TRACKING_SIM_SECOND",
        "TRACKING_SIM_SBS_TZ",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MPLBACKEND", "Agg")
    nb = nbformat.read(path, as_version=4)
    client = nbclient.NotebookClient(
        nb, timeout=600, kernel_name="python3", resources={"metadata": {"path": str(path.parent)}}
    )
    client.execute()
    outputs = [o for cell in nb.cells for o in cell.get("outputs", [])]
    assert not [o for o in outputs if o.get("output_type") == "error"]
    text = "".join(o.get("text", "") for o in outputs)
    assert "AGREE" in text and "DISAGREE" not in text
