"""Sensor specifications: what a sensor is, loaded from YAML.

A sensor is a spec plus a model class. The spec holds everything a user
sets, in the units people think in (degrees, metres, seconds); the model
converts to SI radians once, when it is built. Specs are Pydantic models, so
a YAML file with a typo or a missing field fails loudly at load time rather
than halfway through a run.

``type`` picks the spec class: ``rotating_radar_2d`` and
``passive_bearing`` are implemented; the design's other types (``radar_3d``,
``eo_ir``) are rejected with a pointer to when they arrive.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from tracking_sim.config import load_yaml

__all__ = [
    "AnySensorSpec",
    "CoverageSpec",
    "DetectionSpec",
    "NoiseSpec",
    "PassiveBearingSpec",
    "PassiveDetectionSpec",
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
    "radar_3d": "a later milestone",
    "eo_ir": "a later milestone",
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
    real radius (4/3 is the standard-atmosphere value). When the scenario
    has terrain, ``terrain_masking`` also hides targets behind it.
    """

    range_min_m: float = Field(0.0, ge=0)
    range_max_m: float = Field(gt=0)
    azimuth_deg: tuple[float, float] = (0.0, 360.0)
    elevation_deg: tuple[float, float] = (-90.0, 90.0)
    radar_horizon: bool = True
    refraction_k: float = Field(4.0 / 3.0, gt=0)
    terrain_masking: bool = Field(
        True, description="mask behind terrain when the scenario has a terrain model"
    )

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


class RcsSpec(_Spec):
    """Mean radar cross-section per aircraft class, m^2 (see ``sensors.detection``).

    Rough values for an L/S-band surveillance radar; RCS depends on
    frequency and aspect, so a sensor can override them.
    """

    light: float = Field(1.0, gt=0)
    small: float = Field(2.0, gt=0)
    large: float = Field(20.0, gt=0)
    heavy: float = Field(60.0, gt=0)
    rotorcraft: float = Field(3.0, gt=0)
    unknown: float = Field(5.0, gt=0)


class DetectionSpec(_Spec):
    """Whether a target in coverage is detected, and how much clutter appears.

    ``constant``: every opportunity is detected with probability ``pd``.

    ``radar_equation``: Pd from SNR, where a 1 m^2 target at
    ``reference_range_m`` (slant) has ``reference_snr_db``, scaled by the
    aircraft's RCS class (``rcs_m2``) and range to the fourth power, with
    false-alarm probability ``pfa`` and a Swerling 1 (fluctuating, the
    default) or 0 (steady) target. Aircraft with no known class use
    ``default_class``.

    ``false_alarm_density`` is the expected number of false alarms per
    square metre of coverage per scan, for either model.
    """

    model: Literal["constant", "radar_equation"] = "constant"
    pd: float | None = Field(None, ge=0, le=1)
    reference_snr_db: float | None = None
    reference_range_m: float | None = Field(None, gt=0)
    pfa: float = Field(1e-6, gt=0, lt=1)
    swerling: Literal[0, 1] = 1
    rcs_m2: RcsSpec = Field(default_factory=RcsSpec)
    default_class: Literal["light", "small", "large", "heavy", "rotorcraft", "unknown"] = "unknown"
    false_alarm_density: float = Field(0.0, ge=0)

    @model_validator(mode="after")
    def _check(self) -> DetectionSpec:
        if self.model == "constant" and self.pd is None:
            raise ValueError("detection model 'constant' needs 'pd'")
        if self.model == "radar_equation" and (
            self.reference_snr_db is None or self.reference_range_m is None
        ):
            raise ValueError(
                "detection model 'radar_equation' needs 'reference_snr_db' and 'reference_range_m'"
            )
        return self


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


class PassiveNoiseSpec(_Spec):
    bearing_sigma_deg: float = Field(ge=0)


class PassiveDetectionSpec(_Spec):
    """Detection for a passive receiver listening to aircraft emissions.

    ``constant``: probability ``pd``. ``one_way``: Pd from SNR, which falls
    with range squared (a one-way link, unlike a radar's echo), with
    ``reference_snr_db`` at ``reference_range_m``; the emitter is steady by
    default (Swerling 0). ``false_alarm_rate`` is the mean number of false
    bearings per update, spread uniformly over the sector.
    """

    model: Literal["constant", "one_way"] = "constant"
    pd: float | None = Field(None, ge=0, le=1)
    reference_snr_db: float | None = None
    reference_range_m: float | None = Field(None, gt=0)
    pfa: float = Field(1e-6, gt=0, lt=1)
    swerling: Literal[0, 1] = 0
    false_alarm_rate: float = Field(0.0, ge=0)

    @model_validator(mode="after")
    def _check(self) -> PassiveDetectionSpec:
        if self.model == "constant" and self.pd is None:
            raise ValueError("detection model 'constant' needs 'pd'")
        if self.model == "one_way" and (
            self.reference_snr_db is None or self.reference_range_m is None
        ):
            raise ValueError(
                "detection model 'one_way' needs 'reference_snr_db' and 'reference_range_m'"
            )
        return self


class PassiveBearingSpec(_Spec):
    """A staring passive sensor measuring bearing only (for example an ESM or a
    direction-finding receiver), updating every ``update_period_s``."""

    id: str = Field(min_length=1)
    type: Literal["passive_bearing"]
    position: PositionSpec
    coverage: CoverageSpec
    update_period_s: float = Field(1.0, gt=0)
    noise: PassiveNoiseSpec
    detection: PassiveDetectionSpec
    latency_s: float = Field(0.0, ge=0)


AnySensorSpec = RotatingRadar2DSpec | PassiveBearingSpec

# One entry per implemented sensor type; Pydantic picks by ``type``.
SensorSpec = Annotated[AnySensorSpec, Field(discriminator="type")]

_ADAPTER: TypeAdapter[AnySensorSpec] = TypeAdapter(SensorSpec)


def parse_sensor_spec(data: Any) -> AnySensorSpec:
    """Validate a mapping (for example parsed YAML) as a sensor spec."""
    kind = data.get("type") if isinstance(data, dict) else None
    if kind in _PLANNED_TYPES:
        raise ValueError(f"sensor type {kind!r} is planned for {_PLANNED_TYPES[kind]}")
    return _ADAPTER.validate_python(data)


def load_sensor_spec(path: str | Path) -> AnySensorSpec:
    """Load a sensor spec from a YAML file (``${VAR}`` references are expanded)."""
    return parse_sensor_spec(load_yaml(path))
