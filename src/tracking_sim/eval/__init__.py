"""Evaluation: scoring truth and tracks on a common grid, and metrics."""

from tracking_sim.eval.metrics import (
    MetricSettings,
    TrackerScore,
    comparison_table,
    score_run,
    score_tracks,
)
from tracking_sim.eval.scoring import ScoringGrid, scoring_grid

__all__ = [
    "MetricSettings",
    "ScoringGrid",
    "TrackerScore",
    "comparison_table",
    "score_run",
    "score_tracks",
    "scoring_grid",
]
