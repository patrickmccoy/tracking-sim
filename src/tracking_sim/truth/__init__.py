"""Truth: canonical ADS-B records, their sources, and the truth builder."""

from tracking_sim.truth.builder import (
    BuildStats,
    FlightSegment,
    TruthBuilderConfig,
    TruthSet,
    build_truth,
)
from tracking_sim.truth.record import TruthRecord, ensure_utc
from tracking_sim.truth.source import TruthSource, UnsupportedModeError
from tracking_sim.truth.trajectory import Trajectory, TruthState

__all__ = [
    "BuildStats",
    "FlightSegment",
    "Trajectory",
    "TruthBuilderConfig",
    "TruthRecord",
    "TruthSet",
    "TruthSource",
    "TruthState",
    "UnsupportedModeError",
    "build_truth",
    "ensure_utc",
]
