"""Sensor specifications: what a sensor is, loaded from YAML.

A sensor is a spec plus a model class. The spec holds everything a user
sets, in the units people think in (degrees, metres, seconds); the model
converts to SI radians once, when it is built. Specs are Pydantic models, so
a YAML file with a typo or a missing field fails loudly at load time rather
than halfway through a run.

``type`` picks the spec class. Milestone 2 implements ``rotating_radar_2d``;
the other sensor types in the design (``radar_3d``, ``passive_bearing``,
``eo_ir``) and the ``radar_equation`` detection model are rejected with a
pointer to the milestone that adds them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from tracking_sim.config import load_yaml

__all__ = [
    "CoverageSpec",
    "DetectionSpec",
    "NoiseSpec",
    "PositionSpec",
    "ResolutionSpec",
    "RotatingRadar2DSpec",
    "ScanSpec",
    "SensorSpec",
    "load_sensor_spec",
    "parse_sensor_spec",
]

# Sensor types from the design that a later milestone adds.
_PLANNED_TYPES = {
    "radar_3d": "milestone 4 or later",
    "passive_bearing": "milestone 4",
    "eo_ir": "milestone 4 or later",
}


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PositionSpec(_Spec):
    """Where the sensor stands.

    ``alt_m`` is the ground height at the site, in the same reference as the
    truth altitudes (the truth builder treats reported altitude as height
    above the ellipsoid; the few tens of metres between that and mean sea
    level cancel out as long as both use the same reference). ``mast_m``
    is the antenna height above that ground.
    """

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    alt_m: float = 0.0
    mast_m: float = Field(10.0, ge=0)

    @property
    def antenna_alt_m(self) -> float:
        return self.alt_m + self.mast_m


class CoverageSpec(_Spec):
    """Where the sensor can see.

    ``azimuth_deg`` is a compass sector ``[from, to]`` swept clockwise, so
    ``[300, 60]`` covers north. ``[0, 360]`` is all round. ``elevation_deg``
    limits the geometric elevation angle from the antenna. ``radar_horizon``
    applies a line-of-sight check on an earth of ``refraction_k`` times its
    real radius (4/3 is the standard-atmosphere value).
    """

    range_min_m: float = Field(0.0, ge=0)
    range_max_m: float = Field(gt=0)
    azimuth_deg: tuple[float, float] = (0.0, 360.0)
    elevation_deg: tuple[float, float] = (-90.0, 90.0)
    radar_horizon: bool = True
    refraction_k: float = Field(4.0 / 3.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> CoverageSpec:
        if self.range_min_m >= self.range_max_m:
            raise ValueError("range_min_m must be less than range_max_m")
        lo, hi = self.elevation_deg
        if not (-90 <= lo < hi <= 90):
            raise ValueError("elevation_deg must be [low, high] within [-90, 90]")
        a, b = self.azimuth_deg
        if not (0 <= a <= 360 and 0 <= b <= 360) or a == b:
            raise ValueError("azimuth_deg must be two different angles in [0, 360]")
        return self

    @property
    def full_circle(self) -> bool:
        a, b = self.azimuth_deg
        return (b - a) % 360 == 0

    @property
    def sector_width_deg(self) -> float:
        if self.full_circle:
            return 360.0
        a, b = self.azimuth_deg
        return (b - a) % 360


class ScanSpec(_Spec):
    """How the antenna turns.

    The beam points at ``start_azimuth_deg`` (compass) at the start of the
    run and turns through 360 degrees every ``period_s``. Real surveillance
    radars turn clockwise seen from above, which is the default.
    """

    period_s: float = Field(gt=0)
    beamwidth_deg: float = Field(1.5, gt=0, le=360)
    start_azimuth_deg: float = Field(0.0, ge=0, lt=360)
    direction: Literal["clockwise", "counterclockwise"] = "clockwise"


class NoiseSpec(_Spec):
    """Measurement noise, one standard deviation, in the sensor's own coordinates."""

    range_sigma_m: float = Field(ge=0)
    bearing_sigma_deg: float = Field(ge=0)


class DetectionSpec(_Spec):
    """Whether a target in coverage is detected, and how much clutter appears.

    ``false_alarm_density`` is the expected number of false alarms per
    square metre of coverage per scan.
    """

    model: Literal["constant"] = "constant"
    pd: float = Field(ge=0, le=1)
    false_alarm_density: float = Field(0.0, ge=0)

    @field_validator("model", mode="before")
    @classmethod
    def _planned(cls, value: Any) -> Any:
        if value == "radar_equation":
            raise ValueError("detection model 'radar_equation' arrives in milestone 4")
        return value


class ResolutionSpec(_Spec):
    """Targets closer than this in both range and bearing merge into one detection."""

    range_m: float = Field(0.0, ge=0)
    bearing_deg: float = Field(0.0, ge=0)


class RotatingRadar2DSpec(_Spec):
    """A rotating surveillance radar measuring slant range and bearing."""

    id: str = Field(min_length=1)
    type: Literal["rotating_radar_2d"]
    position: PositionSpec
    coverage: CoverageSpec
    scan: ScanSpec
    noise: NoiseSpec
    detection: DetectionSpec
    resolution: ResolutionSpec = Field(default_factory=ResolutionSpec)
    latency_s: float = Field(0.0, ge=0)


# One entry per implemented sensor type; Pydantic picks by ``type``.
SensorSpec = Annotated[RotatingRadar2DSpec, Field(discriminator="type")]

_ADAPTER: TypeAdapter[RotatingRadar2DSpec] = TypeAdapter(SensorSpec)


def parse_sensor_spec(data: Any) -> RotatingRadar2DSpec:
    """Validate a mapping (for example parsed YAML) as a sensor spec."""
    kind = data.get("type") if isinstance(data, dict) else None
    if kind in _PLANNED_TYPES:
        raise ValueError(f"sensor type {kind!r} is planned for {_PLANNED_TYPES[kind]}")
    return _ADAPTER.validate_python(data)


def load_sensor_spec(path: str | Path) -> RotatingRadar2DSpec:
    """Load a sensor spec from a YAML file (``${VAR}`` references are expanded)."""
    return parse_sensor_spec(load_yaml(path))
