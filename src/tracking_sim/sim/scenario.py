"""Scenario files: one run of truth, sensors and trackers, from YAML.

A minimal subset of the design's scenario format, enough for milestone 3's
"one command runs a scenario with three trackers". Milestone 4 extends it
(and adds the immutable run store) rather than replacing it::

    name: kbjc-synthetic
    origin: {lat: 39.9088, lon: -105.1172, alt_m: 1729}   # optional
    truth:
      source: recording          # or sbs
      path: ../tests/data/synthetic_1h.parquet
      window: ["2026-11-01T23:00:00Z", "2026-11-02T00:00:00Z"]   # optional
    sensors: [sensors/radar-1.yaml]
    trackers: [trackers/ekf-cv-gnn.yaml, trackers/ukf-cv-gnn.yaml]
    seed: 42

Paths are relative to the scenario file. Without ``origin`` the scenario
frame is centred on the first sensor's site.

``run_scenario`` builds truth, simulates the sensors once, runs every
tracker on the same detections, and scores them all on one grid.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracking_sim.config import load_yaml
from tracking_sim.eval.metrics import MetricSettings, TrackerScore, comparison_table, score_run
from tracking_sim.eval.scoring import ScoringGrid, scoring_grid, segment_of
from tracking_sim.geo import LocalFrame
from tracking_sim.sensors.spec import RotatingRadar2DSpec, load_sensor_spec
from tracking_sim.sim.engine import ReplayEngine, SimulationResult
from tracking_sim.tracking.runner import TrackerRun, detection_batches, run_tracker
from tracking_sim.tracking.spec import TrackerSpec, load_tracker_spec
from tracking_sim.truth.builder import TruthBuilderConfig, TruthSet, build_truth
from tracking_sim.truth.record import ensure_utc

__all__ = ["Scenario", "ScenarioResult", "ScenarioSpec", "load_scenario", "run_scenario"]

log = logging.getLogger(__name__)


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OriginSpec(_Spec):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    alt_m: float = 0.0


class TruthSpec(_Spec):
    source: Literal["recording", "sbs"] = "recording"
    path: str
    window: tuple[datetime, datetime] | None = None
    tz: str = Field("UTC", description="time zone of SBS timestamps (sbs only)")

    @model_validator(mode="after")
    def _check(self) -> TruthSpec:
        if self.window is not None and ensure_utc(self.window[1]) <= ensure_utc(self.window[0]):
            raise ValueError("truth window must end after it starts")
        return self


class MetricsSpec(_Spec):
    cutoff_m: float = Field(1000.0, gt=0)
    p: float = Field(1.0, ge=1)
    association_threshold_m: float = Field(1000.0, gt=0)


class ScenarioSpec(_Spec):
    """The scenario file as written: paths not yet resolved."""

    name: str = Field(min_length=1)
    origin: OriginSpec | None = None
    truth: TruthSpec
    sensors: list[str] = Field(min_length=1)
    trackers: list[str] = Field(min_length=1)
    seed: int = Field(0, ge=0)
    metrics: MetricsSpec = Field(default_factory=MetricsSpec)


@dataclass
class Scenario:
    """A scenario with its referenced files loaded."""

    spec: ScenarioSpec
    base_dir: Path
    sensors: list[RotatingRadar2DSpec]
    trackers: list[TrackerSpec]

    @property
    def truth_path(self) -> Path:
        return (self.base_dir / self.spec.truth.path).resolve()

    @property
    def frame(self) -> LocalFrame:
        origin = self.spec.origin
        if origin is not None:
            return LocalFrame(origin.lat, origin.lon, origin.alt_m)
        site = self.sensors[0].position
        return LocalFrame(site.lat, site.lon, site.alt_m)

    @property
    def metric_settings(self) -> MetricSettings:
        m = self.spec.metrics
        return MetricSettings(
            cutoff_m=m.cutoff_m, p=m.p, association_threshold_m=m.association_threshold_m
        )

    def with_seed(self, seed: int) -> Scenario:
        return Scenario(
            spec=self.spec.model_copy(update={"seed": seed}),
            base_dir=self.base_dir,
            sensors=self.sensors,
            trackers=self.trackers,
        )

    def resolved(self) -> dict[str, Any]:
        """The full configuration, every referenced file inlined."""
        return {
            **self.spec.model_dump(mode="json", exclude={"sensors", "trackers"}),
            "truth_path": str(self.truth_path),
            "frame": {"lat": self.frame.lat, "lon": self.frame.lon, "alt_m": self.frame.alt_m},
            "sensors": [s.model_dump(mode="json") for s in self.sensors],
            "trackers": [t.model_dump(mode="json") for t in self.trackers],
        }


def load_scenario(path: str | Path) -> Scenario:
    """Load a scenario file and the sensor and tracker specs it names."""
    path = Path(path)
    spec = ScenarioSpec.model_validate(load_yaml(path) or {})
    base = path.parent
    sensors = [load_sensor_spec(base / p) for p in spec.sensors]
    trackers = [load_tracker_spec(base / p) for p in spec.trackers]
    for kind, ids in (("sensor", [s.id for s in sensors]), ("tracker", [t.id for t in trackers])):
        if len(set(ids)) != len(ids):
            raise ValueError(f"{kind} ids must be unique: {ids}")
    return Scenario(spec=spec, base_dir=base, sensors=sensors, trackers=trackers)


@dataclass
class ScenarioResult:
    scenario: Scenario
    truth: TruthSet
    simulation: SimulationResult
    grid: ScoringGrid
    runs: list[TrackerRun]
    scores: list[TrackerScore]
    timings_s: dict[str, float]

    def table(self) -> str:
        return comparison_table(self.scores)

    def summary(self) -> str:
        s = self.scenario.spec
        head = [
            f"scenario {s.name}, seed {s.seed}",
            f"truth: {len(self.truth)} segments, {self.truth.stats.aircraft} aircraft; "
            f"{len({segment_of(p.id) for p in self.grid.truths})} segments seen by the sensors "
            f"in {len(self.grid.truths)} stretches of coverage",
            self.simulation.summary(),
            "",
            self.table(),
        ]
        return "\n".join(head)

    def write(self, directory: str | Path) -> Path:
        """Write detections, tracks, metrics and the resolved config to ``directory``."""
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        (out / "scenario.resolved.yaml").write_text(
            yaml.safe_dump(self.scenario.resolved(), sort_keys=False)
        )
        self.simulation.write(out)
        for run in self.runs:
            run.write(out / "tracks" / f"{run.tracker_id}.parquet")
        metrics = {
            "timings_s": self.timings_s,
            "trackers": [
                {
                    **score.as_row(),
                    "latency_s": score.latency_s,
                    "never_tracked": score.never_tracked,
                    "over_time": [
                        {
                            "time": t.isoformat(),
                            "ospa_m": o,
                            **{f"gospa_{k}": v for k, v in g.items()},
                        }
                        for t, o, g in zip(score.times, score.ospa, score.gospa, strict=True)
                    ],
                }
                for score in self.scores
            ],
        }
        (out / "metrics.json").write_text(json.dumps(metrics, indent=2))
        (out / "comparison.txt").write_text(self.table() + "\n")
        return out


def _load_truth(scenario: Scenario) -> TruthSet:
    from tracking_sim.truth.sources.recording import RecordingSource
    from tracking_sim.truth.sources.sbs import SbsSource

    t = scenario.spec.truth
    source: Any
    if t.source == "sbs":
        source = SbsSource(path=scenario.truth_path, receiver="scenario", tz=t.tz)
    else:
        source = RecordingSource(scenario.truth_path)
    start, end = (ensure_utc(t.window[0]), ensure_utc(t.window[1])) if t.window else (None, None)
    records = list(source.replay(start, end))
    return build_truth(records, scenario.frame, TruthBuilderConfig())


def run_scenario(scenario: Scenario) -> ScenarioResult:
    """Build truth, simulate the sensors once, run and score every tracker."""
    timings: dict[str, float] = {}
    started = time.perf_counter()
    truth = _load_truth(scenario)
    timings["truth"] = time.perf_counter() - started

    started = time.perf_counter()
    window = scenario.spec.truth.window
    start, end = (window[0], window[1]) if window else (None, None)
    simulation = ReplayEngine(truth, scenario.sensors, seed=scenario.spec.seed).run(start, end)
    grid = scoring_grid(truth, simulation)
    batches = detection_batches(simulation.scans)
    timings["sensors"] = time.perf_counter() - started
    log.info("sensors: %d scans, %.1f s", len(simulation.scans), timings["sensors"])

    runs: list[TrackerRun] = []
    scores: list[TrackerScore] = []
    for spec in scenario.trackers:
        run = run_tracker(spec, batches, seed=scenario.spec.seed)
        log.info("%s: %d tracks, %.1f s", spec.id, len(run.tracks), run.elapsed_s)
        runs.append(run)
        scores.append(score_run(run, grid, settings=scenario.metric_settings))
        timings[spec.id] = run.elapsed_s
    return ScenarioResult(scenario, truth, simulation, grid, runs, scores, timings)
