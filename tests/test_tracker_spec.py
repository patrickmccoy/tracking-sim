from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tracking_sim.tracking import TrackerSpec, load_tracker_spec, parse_tracker_spec

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "scenarios/trackers").glob("*.yaml"))


@pytest.mark.parametrize("path", EXAMPLES, ids=[p.stem for p in EXAMPLES])
def test_example_specs_load(path: Path) -> None:
    spec = load_tracker_spec(path)
    assert isinstance(spec, TrackerSpec)
    assert spec.id == path.stem


def test_the_three_examples_are_the_milestone_3_trackers() -> None:
    specs = {p.stem: load_tracker_spec(p) for p in EXAMPLES}
    assert set(specs) == {"ekf-cv-gnn", "ukf-cv-gnn", "pf-mm-gnn"}
    assert specs["ekf-cv-gnn"].filter == "ekf" and specs["ekf-cv-gnn"].ndim_state == 6
    assert specs["ukf-cv-gnn"].filter == "ukf"
    pf = specs["pf-mm-gnn"]
    assert pf.filter == "particle_mm" and pf.motion.model == "cv_ct"
    assert pf.ndim_state == 7 and pf.particle_count == 1000
    # The baseline and UKF differ only in the filter.
    a = specs["ekf-cv-gnn"].model_dump(exclude={"id", "filter"})
    b = specs["ukf-cv-gnn"].model_dump(exclude={"id", "filter"})
    assert a == b


def test_defaults() -> None:
    spec = parse_tracker_spec({"id": "t", "filter": "ekf"})
    assert spec.motion.model == "constant_velocity"
    assert spec.association.gate == 3.0
    assert spec.initiation.min_points == 3 and spec.initiation.assumed_height_m == 5000
    assert spec.deletion.missed_scans == 3
    assert parse_tracker_spec({"id": "p", "filter": "particle_mm", "motion": {"model": "cv_ct"}})


@pytest.mark.parametrize(
    "data",
    [
        {"id": "t", "filter": "kalman"},
        {"id": "t", "filter": "particle_mm"},  # needs the multiple-model motion
        {"id": "t", "filter": "ekf", "motion": {"model": "cv_ct"}},
        {"id": "t", "filter": "ekf", "particles": {"count": 500}},
        {"id": "t", "filter": "ekf", "initiation": {"min_points": 1}},
        {"id": "t", "filter": "ekf", "association": {"method": "jpda"}},
        {"id": "t", "filter": "ekf", "deletion": {"missed": 3}},  # unknown field
        {"id": "", "filter": "ekf"},
    ],
)
def test_invalid_specs_fail(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_tracker_spec(data)
