"""Notebooks are committed clean: no outputs, no execution counts.

Running a notebook in Jupyter writes outputs (which can include local data,
such as positions near your receiver), execution counts and kernel
metadata into the file. None of that belongs in git. This test needs only
the standard library, so it runs in every CI job.

To clean a notebook you have run: in Jupyter, Edit > Clear Outputs of All
Cells and save, or restore the committed version with
``git checkout -- notebooks/<name>.ipynb``.
"""

import json
from pathlib import Path

import pytest

NOTEBOOKS = sorted((Path(__file__).resolve().parents[1] / "notebooks").glob("*.ipynb"))


@pytest.mark.parametrize("path", NOTEBOOKS, ids=[p.name for p in NOTEBOOKS])
def test_notebook_is_committed_without_outputs(path: Path) -> None:
    nb = json.loads(path.read_text())
    code = [c for c in nb["cells"] if c["cell_type"] == "code"]
    with_outputs = [i for i, c in enumerate(code) if c.get("outputs")]
    counted = [i for i, c in enumerate(code) if c.get("execution_count") is not None]
    empty = [i for i, c in enumerate(nb["cells"]) if not "".join(c["source"]).strip()]
    assert not with_outputs, f"code cells {with_outputs} have outputs: clear them before committing"
    assert not counted, f"code cells {counted} have execution counts: clear them"
    assert not empty, f"cells {empty} are empty (Jupyter adds one when you run to the end)"
    assert "version" not in nb["metadata"].get("language_info", {}), (
        "language_info carries the local Python version: restore the committed metadata"
    )
