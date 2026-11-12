from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tracking_sim.sensors import RotatingRadar2DSpec, load_sensor_spec, parse_sensor_spec

ROOT = Path(__file__).resolve().parents[1]


def minimal(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "id": "r",
        "type": "rotating_radar_2d",
        "position": {"lat": 39.9, "lon": -105.1},
        "coverage": {"range_max_m": 50_000},
        "scan": {"period_s": 4.0},
        "noise": {"range_sigma_m": 50, "bearing_sigma_deg": 0.1},
        "detection": {"pd": 0.8},
    }
    spec.update(overrides)
    return spec


def test_example_spec_loads() -> None:
    spec = load_sensor_spec(ROOT / "scenarios/sensors/radar-1.yaml")
    assert isinstance(spec, RotatingRadar2DSpec)
    assert spec.id == "radar-1"
    assert spec.position.antenna_alt_m == pytest.approx(1744.0)
    assert spec.coverage.full_circle and spec.coverage.sector_width_deg == 360.0
    assert spec.detection.pd == 0.9 and spec.detection.false_alarm_density == 1e-9
    assert spec.resolution.range_m == 150 and spec.latency_s == 0.5


def test_defaults_fill_optional_sections() -> None:
    spec = parse_sensor_spec(minimal())
    assert spec.resolution.range_m == 0 and spec.latency_s == 0
    assert spec.scan.direction == "clockwise" and spec.coverage.radar_horizon
    assert spec.coverage.refraction_k == pytest.approx(4 / 3)


def test_sector_wraps_through_north() -> None:
    spec = parse_sensor_spec(minimal(coverage={"range_max_m": 1e4, "azimuth_deg": [300, 60]}))
    assert not spec.coverage.full_circle
    assert spec.coverage.sector_width_deg == 120


@pytest.mark.parametrize(
    "change",
    [
        {"coverage": {"range_min_m": 5e4, "range_max_m": 1e4}},
        {"coverage": {"range_max_m": 1e4, "elevation_deg": [10, 5]}},
        {"coverage": {"range_max_m": 1e4, "azimuth_deg": [90, 90]}},
        {"detection": {"pd": 1.5}},
        {"scan": {"period_s": 0}},
        {"noise": {"range_sigma_m": -1, "bearing_sigma_deg": 0.1}},
        {"position": {"lat": 39.9, "lon": -105.1, "height": 3}},  # unknown field
    ],
)
def test_invalid_specs_fail(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_sensor_spec(minimal(**change))


def test_planned_types_and_models_point_at_their_milestone() -> None:
    with pytest.raises(ValueError, match="milestone 4"):
        parse_sensor_spec(minimal(type="passive_bearing"))
    with pytest.raises(ValueError, match="planned"):
        parse_sensor_spec(minimal(type="radar_3d"))


def test_radar_equation_detection() -> None:
    spec = parse_sensor_spec(
        minimal(
            detection={
                "model": "radar_equation",
                "reference_snr_db": 13,
                "reference_range_m": 50_000,
                "rcs_m2": {"heavy": 100},
            }
        )
    )
    det = spec.detection
    assert det.pd is None and det.pfa == 1e-6 and det.swerling == 1
    assert det.rcs_m2.heavy == 100 and det.rcs_m2.large == 20  # others keep defaults
    assert det.default_class == "unknown"
    with pytest.raises(ValidationError, match="reference_snr_db"):
        parse_sensor_spec(minimal(detection={"model": "radar_equation", "pd": 0.9}))
    with pytest.raises(ValidationError, match="needs 'pd'"):
        parse_sensor_spec(minimal(detection={"model": "constant"}))
    with pytest.raises(ValidationError):
        parse_sensor_spec(minimal(type="sonar"))


def test_specs_are_immutable() -> None:
    spec = parse_sensor_spec(minimal())
    with pytest.raises(ValidationError):
        spec.detection.pd = 0.5  # type: ignore[misc]
