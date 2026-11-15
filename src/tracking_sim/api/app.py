"""The FastAPI app: scenarios, stored runs and run jobs."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

import tracking_sim
from tracking_sim.api.jobs import JobRunner
from tracking_sim.api.replay import RunData, RunDataCache
from tracking_sim.config import load_yaml
from tracking_sim.eval.runstore import RunRecord, RunStore
from tracking_sim.geo.terrain import ElevationModel
from tracking_sim.sensors.spec import parse_sensor_spec
from tracking_sim.sim.coverage import coverage
from tracking_sim.sim.scenario import (
    Scenario,
    TerrainSpec,
    build_terrain,
    load_scenario,
    scenario_from_dict,
)

__all__ = ["Settings", "create_app"]


# OpenFreeMap's vector tiles: free, no key, OpenStreetMap data.
DEFAULT_MAP_STYLE = "https://tiles.openfreemap.org/styles/positron"


@dataclass(frozen=True)
class Settings:
    """Where the app finds scenarios, runs and the built UI.

    ``map_style`` is the MapLibre style URL the UI loads its base map from;
    the browser fetches the tiles itself.
    """

    scenarios_dir: Path
    runs_dir: Path
    web_dir: Path | None = None
    map_style: str = DEFAULT_MAP_STYLE


def _relative(path: Path, root: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def scenario_files(root: Path) -> dict[str, Path]:
    """Scenario files directly in ``root``, by file stem.

    Sensor and tracker specs live in subdirectories, so only the top level
    holds scenarios.
    """
    return {p.stem: p for p in sorted(root.glob("*.yaml")) if p.is_file()}


def scenario_document(scenario: Scenario, root: Path) -> dict[str, Any]:
    """A scenario as the UI edits it: specs inline, paths relative to ``root``.

    Posting it back (with paths still relative to ``root``) gives the same
    scenario, so the UI never needs to know where the file was.
    """
    spec = scenario.spec.model_dump(mode="json", exclude={"sensors", "trackers"})
    truth = spec["truth"]
    truth["path"] = _relative(scenario.truth_path, root)
    if scenario.aircraft_classes_path is not None:
        truth["aircraft_classes"] = _relative(scenario.aircraft_classes_path, root)
    return {
        **spec,
        "sensors": [s.model_dump(mode="json") for s in scenario.sensors],
        "trackers": [t.model_dump(mode="json") for t in scenario.trackers],
    }


class RunRequest(BaseModel):
    """A scenario to run, as ``GET /api/scenarios/{key}`` returns it.

    Paths in it are relative to the scenarios directory.
    """

    scenario: dict[str, Any]


class CoverageRequest(BaseModel):
    """A sensor spec, the scenario's terrain (or none), and heights above the site's ground."""

    sensor: dict[str, Any]
    terrain: dict[str, Any] | None = None
    heights_m: list[float] = [300.0, 1500.0, 3000.0]


def _validation_detail(exc: ValidationError) -> Any:
    return json.loads(exc.json(include_url=False, include_context=False))


def _run_row(record: RunRecord) -> dict[str, Any]:
    m = record.manifest
    trackers = [
        {k: v for k, v in t.items() if k not in ("over_time", "never_tracked")}
        for t in record.metrics["trackers"]
    ]
    return {
        "id": record.id,
        "created": m["created"],
        "scenario": m["scenario"]["name"],
        "seed": m["scenario"]["seed"],
        "sensors": [s["id"] for s in m["scenario"]["sensors"]],
        "trackers": trackers,
    }


def create_app(settings: Settings) -> FastAPI:
    """The API over ``settings``' scenarios and run store, plus the UI if built."""
    root = settings.scenarios_dir.resolve()
    store = RunStore(settings.runs_dir)
    jobs = JobRunner(store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        jobs.shutdown()

    replays = RunDataCache()
    terrains: dict[str, ElevationModel] = {}

    app = FastAPI(title="tracking-sim", version=tracking_sim.__version__, lifespan=lifespan)
    # Replay data is a few megabytes of numbers; it compresses about 5x.
    app.add_middleware(GZipMiddleware, minimum_size=4096)
    app.state.settings = settings
    app.state.store = store
    app.state.jobs = jobs

    def find_scenario(key: str) -> Path:
        path = scenario_files(root).get(key)
        if path is None:
            raise HTTPException(404, f"no scenario {key!r}")
        return path

    def find_run(run_id: str) -> RunRecord:
        try:
            return store.get(run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from None

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": tracking_sim.__version__}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {"map_style": settings.map_style, "version": tracking_sim.__version__}

    @app.get("/api/scenarios")
    def list_scenarios() -> list[dict[str, Any]]:
        rows = []
        for key, path in scenario_files(root).items():
            data = load_yaml(path) or {}
            rows.append({"key": key, "name": data.get("name", key), "file": path.name})
        return rows

    @app.get("/api/scenarios/{key}")
    def get_scenario(key: str) -> dict[str, Any]:
        path = find_scenario(key)
        try:
            scenario = load_scenario(path)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, f"{path.name}: {exc}") from None
        frame = scenario.frame
        return {
            "key": key,
            "scenario": scenario_document(scenario, root),
            "frame": {"lat": frame.lat, "lon": frame.lon, "alt_m": frame.alt_m},
        }

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        return [_run_row(r) for r in store.list()]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        record = find_run(run_id)
        return {**_run_row(record), "manifest": record.manifest, "metrics": record.metrics}

    def run_data(run_id: str) -> RunData:
        return replays.get(find_run(run_id))

    @app.get("/api/runs/{run_id}/truth")
    def run_truth(run_id: str, step_s: float = 5.0) -> dict[str, Any]:
        if not 0.5 <= step_s <= 60:
            raise HTTPException(422, "step_s must be between 0.5 and 60")
        try:
            return run_data(run_id).truth(step_s)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/runs/{run_id}/detections")
    def run_detections(run_id: str) -> dict[str, Any]:
        return run_data(run_id).detections()

    @app.get("/api/runs/{run_id}/tracks/{tracker_id}")
    def run_tracks(run_id: str, tracker_id: str, tentative: bool = False) -> dict[str, Any]:
        try:
            return run_data(run_id).tracks(tracker_id, tentative=tentative)
        except KeyError:
            raise HTTPException(404, f"run {run_id} has no tracker {tracker_id!r}") from None

    def terrain_for(spec: TerrainSpec | None, lat: float, lon: float, range_m: float) -> Any:
        if spec is None:
            return None
        if spec.source != "copernicus":
            return build_terrain(spec, [])
        from tracking_sim.geo.copernicus import area_around

        # Tiles are large: keep only the latest area (a dragged sensor stays in it).
        key = json.dumps([spec.resolution_m, area_around([(lat, lon, range_m)])])
        if key not in terrains:
            terrains.clear()
            terrains[key] = build_terrain(spec, [(lat, lon, range_m)])
        return terrains[key]

    @app.post("/api/coverage")
    def sensor_coverage(request: CoverageRequest) -> dict[str, Any]:
        try:
            sensor = parse_sensor_spec(request.sensor)
            terrain_spec = (
                None if request.terrain is None else TerrainSpec.model_validate(request.terrain)
            )
        except ValidationError as exc:
            raise HTTPException(422, _validation_detail(exc)) from None
        if not request.heights_m or len(request.heights_m) > 8:
            raise HTTPException(422, "give between one and eight heights")
        pos = sensor.position
        try:
            terrain = terrain_for(terrain_spec, pos.lat, pos.lon, sensor.coverage.range_max_m)
        except ImportError:
            raise HTTPException(422, "copernicus terrain needs the terrain extra") from None
        except OSError as exc:
            raise HTTPException(503, f"terrain: {exc}") from None
        return coverage(sensor, request.heights_m, terrain=terrain)

    @app.post("/api/runs", status_code=202)
    def start_run(request: RunRequest) -> dict[str, Any]:
        try:
            scenario = scenario_from_dict(request.scenario, root)
            job = jobs.submit(scenario)
        except ValidationError as exc:
            raise HTTPException(422, _validation_detail(exc)) from None
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        return job.as_dict()

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [j.as_dict() for j in jobs.list()]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"no job {job_id!r}")
        return job.as_dict()

    if settings.web_dir is not None and (settings.web_dir / "index.html").exists():
        web = settings.web_dir
        app.mount("/assets", StaticFiles(directory=web / "assets", check_dir=False), "assets")

        @app.get("/{path:path}", include_in_schema=False)
        def ui(path: str) -> FileResponse:
            # A single-page app: every non-API path gets index.html.
            if path.startswith("api/"):
                raise HTTPException(404)
            candidate = (web / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(web.resolve()):
                return FileResponse(candidate)
            return FileResponse(web / "index.html")

    return app
