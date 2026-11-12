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
    terrain: {source: copernicus, resolution_m: 90}           # optional
    sensors: [sensors/radar-1.yaml]
    trackers: [trackers/ekf-cv-gnn.yaml, trackers/ukf-cv-gnn.yaml]
    seed: 42

Sensors and trackers are spec files or specs written inline (a mapping in
place of the path). Paths are relative to the scenario file. Without ``origin`` the scenario
frame is centred on the first sensor's site.

``run_scenario`` builds truth, simulates the sensors once, runs every
tracker on the same detections, and scores them all on one grid.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracking_sim.config import load_yaml
from tracking_sim.eval.metrics import MetricSettings, TrackerScore, comparison_table, score_run
from tracking_sim.eval.scoring import ScoringGrid, scoring_grid, segment_of
from tracking_sim.geo import LocalFrame
from tracking_sim.geo.terrain import ElevationModel, FlatTerrain, SyntheticRidge
from tracking_sim.sensors.detection import RcsClass, load_aircraft_classes
from tracking_sim.sensors.spec import (
    AnySensorSpec,
    RotatingRadar2DSpec,
    load_sensor_spec,
    parse_sensor_spec,
)
from tracking_sim.sim.engine import ReplayEngine, SimulationResult
from tracking_sim.tracking.runner import TrackerRun, detection_batches, run_tracker
from tracking_sim.tracking.spec import TrackerSpec, load_tracker_spec, parse_tracker_spec
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
    aircraft_classes: str | None = Field(
        None, description="optional icao24,class CSV of RCS classes (radar-equation Pd)"
    )

    @model_validator(mode="after")
    def _check(self) -> TruthSpec:
        if self.window is not None and ensure_utc(self.window[1]) <= ensure_utc(self.window[0]):
            raise ValueError("truth window must end after it starts")
        return self


class RidgeSpec(_Spec):
    """A synthetic ridge (see ``geo.terrain.SyntheticRidge``)."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    azimuth_deg: float = 0.0
    height_m: float = Field(gt=0)
    width_m: float = Field(1500.0, gt=0)
    length_m: float = Field(60_000.0, gt=0)
    base_m: float = 0.0


class TerrainSpec(_Spec):
    """Terrain for line-of-sight masking.

    ``copernicus`` downloads (once) and caches Copernicus DEM tiles covering
    every sensor's range; it needs the ``terrain`` extra. ``synthetic`` is
    flat ground with one ``ridge``; ``flat`` is level ground at ``height_m``.
    """

    source: Literal["copernicus", "synthetic", "flat"]
    resolution_m: Literal[30, 90] = 90
    ridge: RidgeSpec | None = None
    height_m: float = 0.0

    @model_validator(mode="after")
    def _check(self) -> TerrainSpec:
        if self.source == "synthetic" and self.ridge is None:
            raise ValueError("terrain source 'synthetic' needs a 'ridge'")
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
    terrain: TerrainSpec | None = None
    sensors: list[str | dict[str, Any]] = Field(
        min_length=1, description="spec files, or specs written inline"
    )
    trackers: list[str | dict[str, Any]] = Field(
        min_length=1, description="spec files, or specs written inline"
    )
    seed: int = Field(0, ge=0)
    metrics: MetricsSpec = Field(default_factory=MetricsSpec)


@dataclass
class Scenario:
    """A scenario with its referenced files loaded."""

    spec: ScenarioSpec
    base_dir: Path
    sensors: list[AnySensorSpec]
    trackers: list[TrackerSpec]
    _terrain: Any = field(default=None, init=False, repr=False)

    def terrain_model(self) -> ElevationModel | None:
        """The scenario's terrain (loaded or downloaded once), or None."""
        t = self.spec.terrain
        if t is None:
            return None
        if self._terrain is None:
            if t.source == "flat":
                self._terrain = FlatTerrain(t.height_m)
            elif t.source == "synthetic":
                assert t.ridge is not None
                self._terrain = SyntheticRidge(**t.ridge.model_dump())
            else:
                from tracking_sim.geo.copernicus import area_around, load_area

                box = area_around(
                    [(s.position.lat, s.position.lon, s.coverage.range_max_m) for s in self.sensors]
                )
                self._terrain = load_area(*box, resolution_m=t.resolution_m)
        model: ElevationModel = self._terrain
        return model

    def input_digests(self) -> dict[str, str]:
        """Digests of the data a run reads, for the run store."""
        from tracking_sim.eval.runstore import input_digest

        digests = {"truth": input_digest(self.truth_path)}
        if self.aircraft_classes_path is not None:
            digests["aircraft_classes"] = input_digest(self.aircraft_classes_path)
        terrain = self.terrain_model()
        if terrain is not None:
            digests["terrain"] = terrain.digest()
        return digests

    @property
    def truth_path(self) -> Path:
        return (self.base_dir / self.spec.truth.path).resolve()

    @property
    def aircraft_classes_path(self) -> Path | None:
        path = self.spec.truth.aircraft_classes
        return None if path is None else (self.base_dir / path).resolve()

    def aircraft_classes(self) -> dict[str, RcsClass]:
        path = self.aircraft_classes_path
        return {} if path is None else load_aircraft_classes(path)

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
        other = Scenario(
            spec=self.spec.model_copy(update={"seed": seed}),
            base_dir=self.base_dir,
            sensors=self.sensors,
            trackers=self.trackers,
        )
        other._terrain = self._terrain
        return other

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
    sensors = [
        load_sensor_spec(base / item) if isinstance(item, str) else parse_sensor_spec(item)
        for item in spec.sensors
    ]
    trackers = [
        load_tracker_spec(base / item) if isinstance(item, str) else parse_tracker_spec(item)
        for item in spec.trackers
    ]
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
    engine = ReplayEngine(
        truth,
        scenario.sensors,
        seed=scenario.spec.seed,
        terrain=scenario.terrain_model(),
        aircraft_classes=scenario.aircraft_classes(),
    )
    simulation = engine.run(start, end)
    # Trackers take radar detections only: bearing-only sensors need fusion
    # (milestone 6). They are scored on what the radars could see.
    radars = [s.id for s in scenario.sensors if isinstance(s, RotatingRadar2DSpec)]
    if scenario.trackers and not radars:
        raise ValueError("trackers need at least one rotating_radar_2d sensor")
    grid = scoring_grid(truth, simulation, sensor_ids=radars)
    batches = detection_batches([s for s in simulation.scans if s.sensor_id in radars])
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
