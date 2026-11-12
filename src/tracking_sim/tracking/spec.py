"""Tracker specifications: which Stone Soup components a tracker uses, from YAML.

A tracker is a Stone Soup pipeline assembled from config, so comparing two
filters is a config diff on the same scenario and seed. Each component is
picked from a fixed list rather than by class path: a typo fails at load
time, and every combination the factory accepts is one it knows how to wire
up.

Units are the ones people think in (metres, seconds, degrees); the factory
converts. Motion noise values are Stone Soup's diffusion coefficients:
``acceleration_noise`` is the ``noise_diff_coeff`` of a constant-velocity
model in m^2/s^3, and ``turn_rate_noise_deg`` the coordinated-turn model's
turn-rate diffusion in deg^2/s^3.

The radar is 2D, so the tracker cannot observe height. Tracks carry it
anyway (the state is ``[x, vx, y, vy, z, vz]``, plus turn rate for the
multiple-model filter), starting from ``initiation.assumed_height_m`` above
the antenna with ``height_sigma_m`` of uncertainty; slant range informs it
only weakly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracking_sim.config import load_yaml

__all__ = [
    "AssociationSpec",
    "DeletionSpec",
    "InitiationSpec",
    "MotionSpec",
    "ParticleSpec",
    "TrackerSpec",
    "load_tracker_spec",
    "parse_tracker_spec",
]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MotionSpec(_Spec):
    """How targets are assumed to move between measurements.

    ``constant_velocity`` is one model for every axis. ``cv_ct`` is a
    two-model set for the multiple-model filter: constant velocity, and a
    coordinated turn in the horizontal plane (with constant velocity
    vertically); particles switch between them with ``switch_probability``
    per update.
    """

    model: Literal["constant_velocity", "cv_ct"] = "constant_velocity"
    acceleration_noise: float = Field(1.0, gt=0, description="horizontal, m^2/s^3")
    vertical_acceleration_noise: float = Field(0.1, gt=0, description="m^2/s^3")
    turn_rate_noise_deg: float = Field(1.0, gt=0, description="cv_ct only, deg^2/s^3")
    switch_probability: float = Field(0.05, gt=0, lt=1, description="cv_ct only")


class ParticleSpec(_Spec):
    """Particle filter settings (``filter: particle_mm`` only)."""

    count: int = Field(1000, ge=50)


class AssociationSpec(_Spec):
    """Global nearest neighbour association with a Mahalanobis gate."""

    method: Literal["gnn"] = "gnn"
    gate: float = Field(3.0, gt=0, description="Mahalanobis distance beyond which no association")


class InitiationSpec(_Spec):
    """How tentative tracks start and when they are confirmed.

    A new track is placed at ``assumed_height_m`` above the antenna (a 2D
    radar measures no height) with zero mean velocity, and becomes a
    confirmed track after ``min_points`` associated detections.
    """

    min_points: int = Field(3, ge=2)
    assumed_height_m: float = 5000.0
    height_sigma_m: float = Field(3000.0, gt=0)
    speed_sigma_mps: float = Field(150.0, gt=0, description="prior horizontal speed spread")
    vertical_speed_sigma_mps: float = Field(10.0, gt=0)


class DeletionSpec(_Spec):
    """Drop a track after this many consecutive scans without an update."""

    missed_scans: int = Field(3, ge=1)


class TrackerSpec(_Spec):
    """One tracker: filter, motion model, association, initiation and deletion."""

    id: str = Field(min_length=1)
    filter: Literal["ekf", "ukf", "particle_mm"]
    motion: MotionSpec = Field(default_factory=MotionSpec)
    particles: ParticleSpec | None = None
    association: AssociationSpec = Field(default_factory=AssociationSpec)
    initiation: InitiationSpec = Field(default_factory=InitiationSpec)
    deletion: DeletionSpec = Field(default_factory=DeletionSpec)

    @model_validator(mode="after")
    def _check(self) -> TrackerSpec:
        multi = self.filter == "particle_mm"
        if multi and self.motion.model != "cv_ct":
            raise ValueError("filter 'particle_mm' needs the multiple-model motion 'cv_ct'")
        if not multi and self.motion.model == "cv_ct":
            raise ValueError("motion 'cv_ct' is a multiple-model set: use filter 'particle_mm'")
        if not multi and self.particles is not None:
            raise ValueError("'particles' only applies to filter 'particle_mm'")
        return self

    @property
    def particle_count(self) -> int:
        return (self.particles or ParticleSpec()).count

    @property
    def ndim_state(self) -> int:
        """6 for ``[x, vx, y, vy, z, vz]``, 7 when turn rate is carried too."""
        return 7 if self.motion.model == "cv_ct" else 6


def parse_tracker_spec(data: Any) -> TrackerSpec:
    """Validate a mapping (for example parsed YAML) as a tracker spec."""
    return TrackerSpec.model_validate(data)


def load_tracker_spec(path: str | Path) -> TrackerSpec:
    """Load a tracker spec from a YAML file (``${VAR}`` references are expanded)."""
    return parse_tracker_spec(load_yaml(path))
