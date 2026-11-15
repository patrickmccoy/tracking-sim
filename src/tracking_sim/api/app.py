"""The FastAPI app: scenarios, stored runs and run jobs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

import tracking_sim
from tracking_sim.api.captures import CaptureError, CaptureStore, check_upload
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

# Ground heights for sensors in scenarios without terrain: Copernicus GLO-90,
# downloaded once per 1-degree tile into the terrain cache.
DEFAULT_GROUND_TERRAIN: dict[str, Any] = {"source": "copernicus", "resolution_m": 90}

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    """Where the app finds scenarios, runs and the built UI.

    ``map_style`` is the MapLibre style URL the UI loads its base map from;
    the browser fetches the tiles itself. ``ground_terrain`` (a terrain
    spec) gives a placed sensor its ground height when the scenario has no
    terrain of its own; ``None`` leaves the height to the user. Uploaded
    captures are kept in ``captures_dir``, up to ``max_upload_bytes`` each.
    """

    scenarios_dir: Path
    runs_dir: Path
    web_dir: Path | None = None
    map_style: str = DEFAULT_MAP_STYLE
    ground_terrain: dict[str, Any] | None = field(
        default_factory=lambda: dict(DEFAULT_GROUND_TERRAIN)
    )
    captures_dir: Path = Path("data/captures")
    max_upload_bytes: int = 2 * 1024**3


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


class ElevationRequest(BaseModel):
    """Ground heights at points, from a scenario's terrain."""

    terrain: dict[str, Any] | None = None
    points: list[tuple[float, float]]  # (lat, lon)


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
    captures = CaptureStore(settings.captures_dir)
    terrains: dict[str, ElevationModel] = {}

    app = FastAPI(title="tracking-sim", version=tracking_sim.__version__, lifespan=lifespan)
    # Replay data is a few megabytes of numbers; it compresses about 5x.
    app.add_middleware(GZipMiddleware, minimum_size=4096)
    app.state.settings = settings
    app.state.store = store
    app.state.jobs = jobs
    app.state.captures = captures

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
            # Two areas: a scenario's coverage area and the ground lookups.
            while len(terrains) >= 2:
                terrains.pop(next(iter(terrains)))
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

    ground_spec = (
        None
        if settings.ground_terrain is None
        else TerrainSpec.model_validate(settings.ground_terrain)
    )

    @app.post("/api/elevation")
    def elevation(request: ElevationRequest) -> dict[str, Any]:
        """Ground height at each point, and where it came from.

        The scenario's terrain when it has some (what the simulator uses);
        otherwise the server's ground terrain (Copernicus GLO-90 by
        default), or nulls when that is unavailable (no ``terrain`` extra,
        no network) so the UI keeps the height it had.
        """
        if not 0 < len(request.points) <= 1000:
            raise HTTPException(422, "give between 1 and 1000 points")
        try:
            spec = None if request.terrain is None else TerrainSpec.model_validate(request.terrain)
        except ValidationError as exc:
            raise HTTPException(422, _validation_detail(exc)) from None
        source = "scenario"
        if spec is None:
            spec, source = ground_spec, "ground"
        nothing = {"heights_m": [None] * len(request.points), "source": None}
        if spec is None:
            return nothing
        lat, lon = (np.array(v, dtype=float) for v in zip(*request.points, strict=True))
        try:
            # A small reach: copernicus then loads only the tiles under the points.
            terrain = terrain_for(spec, float(lat.mean()), float(lon.mean()), 1000.0)
        except ImportError:
            if source == "ground":
                return nothing
            raise HTTPException(422, "copernicus terrain needs the terrain extra") from None
        except OSError as exc:
            if source == "ground":
                log.warning("ground terrain unavailable: %s", exc)
                return nothing
            raise HTTPException(503, f"terrain: {exc}") from None
        heights = np.asarray(terrain.height(lat, lon), dtype=float)
        return {
            "heights_m": [None if np.isnan(h) else round(float(h), 1) for h in heights],
            "source": f"{source}:{spec.source}",
        }

    @app.post("/api/scenarios/export")
    def export_scenario(request: RunRequest) -> Response:
        """The scenario as a YAML file, after the same checks a run makes."""
        try:
            scenario_from_dict(request.scenario, root)
        except ValidationError as exc:
            raise HTTPException(422, _validation_detail(exc)) from None
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        name = str(request.scenario.get("name", "scenario"))
        header = (
            f"# {name}, saved from the tracking-sim map playground.\n"
            "# Paths are relative to the scenarios directory: save it there to run it.\n"
        )
        body = yaml.safe_dump(request.scenario, sort_keys=False)
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "scenario"
        return Response(
            header + body,
            media_type="application/yaml",
            headers={"Content-Disposition": f'attachment; filename="{filename}.yaml"'},
        )

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

    def capture_row(capture: Any) -> dict[str, Any]:
        # A scenario names its truth relative to the scenarios directory.
        return {**asdict(capture), "truth_path": _relative(Path(capture.path), root)}

    @app.get("/api/captures")
    def list_captures() -> list[dict[str, Any]]:
        return [capture_row(c) for c in captures.list()]

    @app.post("/api/captures", status_code=201)
    async def upload_capture(request: Request, filename: str, tz: str = "UTC") -> dict[str, Any]:
        """Store the request body, a recording or SBS capture, as a capture.

        The body is the file itself (not a form), streamed to disk; ``tz``
        is the zone of an SBS capture's timestamps.
        """
        try:
            check_upload(filename, tz)
        except CaptureError as exc:
            raise HTTPException(422, str(exc)) from None
        upload = captures.incoming() / uuid.uuid4().hex
        digest = hashlib.sha256()
        size = 0
        try:
            with upload.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(
                            413, f"uploads are limited to {settings.max_upload_bytes // 2**20} MB"
                        )
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise HTTPException(422, "the upload is empty")
            capture = await run_in_threadpool(
                captures.add, upload, filename, digest.hexdigest(), tz=tz
            )
        except CaptureError as exc:
            raise HTTPException(422, str(exc)) from None
        finally:
            upload.unlink(missing_ok=True)
        return capture_row(capture)

    @app.get("/api/captures/{capture_id}/preview")
    def capture_preview(capture_id: str) -> dict[str, Any]:
        """A sample of the capture's positions, for the map."""
        preview = captures.preview(capture_id)
        if preview is None:
            raise HTTPException(404, f"no capture {capture_id!r}")
        return preview

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
