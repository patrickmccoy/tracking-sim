"""Tracking metrics and the tracker comparison table.

All metrics use horizontal position (x, y) on the scoring grid
(``eval.scoring``): a 2D radar does not measure height, so scoring it would
measure the trackers' height prior, not their tracking.

* **OSPA** and **GOSPA** (Stone Soup) at every grid time, with cut-off
  ``cutoff_m`` and order ``p``. GOSPA splits into localisation, missed-target
  and false-track parts, which is the more useful view when comparing
  trackers.
* **SIAP** (Stone Soup): completeness (share of visible aircraft held by a
  track), ambiguity (tracks per held aircraft), spuriousness (share of
  tracks on no aircraft), position and velocity accuracy, the rate of track
  number changes (lower is better), and the longest track segment (share of
  each aircraft's visible time spent on its single longest track).
  Tracks are paired with truth by Stone Soup's ``TrackToTruth`` within
  ``association_threshold_m``.
* **Track latency**: for each aircraft, from the first grid time it was
  visible to the first time a track was associated with it. Aircraft never
  tracked are counted separately rather than given an infinite latency.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from stonesoup.dataassociator.tracktotrack import TrackToTruth
from stonesoup.measures import Euclidean
from stonesoup.metricgenerator.manager import MultiManager
from stonesoup.metricgenerator.ospametric import GOSPAMetric, OSPAMetric
from stonesoup.metricgenerator.tracktotruthmetrics import SIAPMetrics
from stonesoup.types.track import Track

from tracking_sim.eval.scoring import ScoringGrid
from tracking_sim.tracking.runner import TrackerRun

__all__ = ["MetricSettings", "TrackerScore", "comparison_table", "score_run", "score_tracks"]

SIAP_KEYS = {
    "SIAP Completeness": "completeness",
    "SIAP Ambiguity": "ambiguity",
    "SIAP Spuriousness": "spuriousness",
    "SIAP Position Accuracy": "position_accuracy_m",
    "SIAP Velocity Accuracy": "velocity_accuracy_mps",
    "SIAP Rate of Track Number Change": "track_number_changes_per_s",
    "SIAP Longest Track Segment": "longest_track_segment",
}


@dataclass(frozen=True)
class MetricSettings:
    cutoff_m: float = 1000.0
    p: float = 1.0
    association_threshold_m: float = 1000.0
    consecutive_to_associate: int = 3
    consecutive_misses_to_end: int = 2


@dataclass
class TrackerScore:
    """Every metric for one tracker on one grid."""

    tracker_id: str
    tracks: int
    elapsed_s: float
    times: list[datetime]
    ospa: list[float]
    gospa: list[dict[str, float]]
    siap: dict[str, float]
    latency_s: dict[str, float] = field(default_factory=dict)
    never_tracked: list[str] = field(default_factory=list)

    @property
    def mean_ospa_m(self) -> float:
        return float(np.mean(self.ospa)) if self.ospa else float("nan")

    def mean_gospa(self, part: str = "distance") -> float:
        return float(np.mean([g[part] for g in self.gospa])) if self.gospa else float("nan")

    @property
    def median_latency_s(self) -> float:
        return statistics.median(self.latency_s.values()) if self.latency_s else float("nan")

    def as_row(self) -> dict[str, Any]:
        return {
            "tracker": self.tracker_id,
            "run_s": self.elapsed_s,
            "tracks": self.tracks,
            "ospa_m": self.mean_ospa_m,
            "gospa": self.mean_gospa(),
            "gospa_localisation": self.mean_gospa("localisation"),
            "gospa_missed": self.mean_gospa("missed"),
            "gospa_false": self.mean_gospa("false"),
            **self.siap,
            "median_latency_s": self.median_latency_s,
            "never_tracked": len(self.never_tracked),
        }


def _manager(settings: MetricSettings) -> MultiManager:
    position = Euclidean(mapping=[0, 2])
    return MultiManager(
        [
            OSPAMetric(c=settings.cutoff_m, p=settings.p, measure=position, generator_name="ospa"),
            GOSPAMetric(
                c=settings.cutoff_m, p=settings.p, measure=position, generator_name="gospa"
            ),
            SIAPMetrics(
                position_measure=position,
                velocity_measure=Euclidean(mapping=[1, 3]),
                generator_name="siap",
            ),
        ],
        associator=TrackToTruth(
            association_threshold=settings.association_threshold_m,
            consec_pairs_confirm=settings.consecutive_to_associate,
            consec_misses_end=settings.consecutive_misses_to_end,
            measure=position,
        ),
    )


def _start_of(time_range: Any) -> datetime:
    """Start of a ``TimeRange`` or of the earliest interval of a ``CompoundTimeRange``."""
    if hasattr(time_range, "start"):
        start: datetime = time_range.start
        return start
    earliest: datetime = min(r.start for r in time_range.time_ranges)
    return earliest


def score_tracks(
    tracker_id: str,
    tracks: Sequence[Track],
    grid: ScoringGrid,
    *,
    elapsed_s: float = float("nan"),
    settings: MetricSettings | None = None,
) -> TrackerScore:
    """Score grid-sampled ``tracks`` against ``grid``'s truth."""
    settings = settings or MetricSettings()
    manager = _manager(settings)
    manager.add_data({"tracks": list(tracks), "groundtruth_paths": grid.truths})
    metrics = manager.generate_metrics()

    def by_time(metric: Any) -> list[Any]:
        values = metric.value if isinstance(metric.value, list) else [metric]
        return sorted(values, key=lambda m: m.timestamp)

    ospa = by_time(metrics["ospa"]["OSPA distances"])
    gospa = by_time(metrics["gospa"]["GOSPA Metrics"])
    siap = {
        name: float(metrics["siap"][title].value)
        for title, name in SIAP_KEYS.items()
        if title in metrics["siap"]
    }

    # Latency: first visible grid time to first associated time, per aircraft.
    first_associated: dict[str, datetime] = {}
    for association in manager.association_set.associations:
        truth = next(o for o in association.objects if o.id in {p.id for p in grid.truths})
        start = _start_of(association.time_range)
        if truth.id not in first_associated or start < first_associated[truth.id]:
            first_associated[truth.id] = start
    latency: dict[str, float] = {}
    never: list[str] = []
    for path in grid.truths:
        if path.id in first_associated:
            seen = path.states[0].timestamp
            latency[path.id] = max((first_associated[path.id] - seen).total_seconds(), 0.0)
        else:
            never.append(path.id)

    return TrackerScore(
        tracker_id=tracker_id,
        tracks=len(tracks),
        elapsed_s=elapsed_s,
        times=[m.timestamp for m in ospa],
        ospa=[float(m.value) for m in ospa],
        gospa=[{k: float(v) for k, v in m.value.items()} for m in gospa],
        siap=siap,
        latency_s=latency,
        never_tracked=sorted(never),
    )


def score_run(
    run: TrackerRun, grid: ScoringGrid, *, settings: MetricSettings | None = None
) -> TrackerScore:
    """Score one tracker run against the grid's truth."""
    return score_tracks(
        run.tracker_id, grid.tracks(run), grid, elapsed_s=run.elapsed_s, settings=settings
    )


_COLUMNS = [
    ("tracker", "tracker", "{}"),
    ("run_s", "run s", "{:.1f}"),
    ("tracks", "tracks", "{}"),
    ("ospa_m", "OSPA m", "{:.0f}"),
    ("gospa", "GOSPA", "{:.0f}"),
    ("gospa_localisation", "loc", "{:.0f}"),
    ("gospa_missed", "missed", "{:.0f}"),
    ("gospa_false", "false", "{:.0f}"),
    ("completeness", "complete", "{:.2f}"),
    ("ambiguity", "ambig", "{:.2f}"),
    ("spuriousness", "spurious", "{:.2f}"),
    ("position_accuracy_m", "pos acc m", "{:.0f}"),
    ("longest_track_segment", "longest", "{:.2f}"),
    ("median_latency_s", "latency s", "{:.0f}"),
    ("never_tracked", "never", "{}"),
]


def comparison_table(scores: Sequence[TrackerScore]) -> str:
    """A fixed-width table, one row per tracker, lower-is-better and higher-is-better mixed.

    OSPA, GOSPA (and its parts), ambiguity, spuriousness, position accuracy,
    latency and never-tracked: lower is better. Completeness and longest
    segment: higher is better.
    """
    rows = [s.as_row() for s in scores]
    cells = [[header for _, header, _ in _COLUMNS]]
    for row in rows:
        cells.append(
            [fmt.format(row[key]) if row.get(key) is not None else "-" for key, _, fmt in _COLUMNS]
        )
    widths = [max(len(r[i]) for r in cells) for i in range(len(_COLUMNS))]

    def line(r: list[str]) -> str:
        first = r[0].ljust(widths[0])
        rest = [c.rjust(w) for c, w in zip(r[1:], widths[1:], strict=True)]
        return "  ".join([first, *rest])

    out = [line(cells[0]), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in cells[1:]]
    return "\n".join(out)
