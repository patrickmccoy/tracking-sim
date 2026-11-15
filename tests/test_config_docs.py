"""docs/configuration.md documents every key the config files accept."""

from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel

from tracking_sim.sensors import spec as sensor_spec
from tracking_sim.sim import scenario
from tracking_sim.tracking import spec as tracker_spec

DOC = (Path(__file__).resolve().parents[1] / "docs/configuration.md").read_text()

MODELS: list[type[BaseModel]] = [
    scenario.ScenarioSpec,
    scenario.OriginSpec,
    scenario.TruthSpec,
    scenario.TerrainSpec,
    scenario.RidgeSpec,
    scenario.MetricsSpec,
    sensor_spec.PositionSpec,
    sensor_spec.CoverageSpec,
    sensor_spec.ScanSpec,
    sensor_spec.NoiseSpec,
    sensor_spec.RcsSpec,
    sensor_spec.DetectionSpec,
    sensor_spec.ResolutionSpec,
    sensor_spec.RotatingRadar2DSpec,
    sensor_spec.PassiveNoiseSpec,
    sensor_spec.PassiveDetectionSpec,
    sensor_spec.PassiveBearingSpec,
    tracker_spec.MotionSpec,
    tracker_spec.ParticleSpec,
    tracker_spec.AssociationSpec,
    tracker_spec.InitiationSpec,
    tracker_spec.DeletionSpec,
    tracker_spec.TrackerSpec,
]


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_every_key_is_documented(model: type[BaseModel]) -> None:
    missing = [name for name in model.model_fields if f"`{name}`" not in DOC]
    assert not missing, f"{model.__name__}: document {missing} in docs/configuration.md"
    assert model.__name__ in DOC


CHOICES = [
    (scenario.TruthSpec, "source"),
    (scenario.TerrainSpec, "source"),
    (sensor_spec.ScanSpec, "direction"),
    (sensor_spec.DetectionSpec, "model"),
    (sensor_spec.DetectionSpec, "default_class"),
    (sensor_spec.PassiveDetectionSpec, "model"),
    (tracker_spec.TrackerSpec, "filter"),
    (tracker_spec.MotionSpec, "model"),
    (tracker_spec.AssociationSpec, "method"),
]


@pytest.mark.parametrize(("model", "field"), CHOICES, ids=lambda x: getattr(x, "__name__", x))
def test_every_choice_is_documented(model: type[BaseModel], field: str) -> None:
    values = get_args(model.model_fields[field].annotation)
    assert values
    missing = [v for v in values if f"`{v}`" not in DOC]
    assert not missing, f"{model.__name__}.{field}: document {missing} in docs/configuration.md"
