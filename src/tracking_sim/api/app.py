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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

import tracking_sim
from tracking_sim.api.jobs import JobRunner
from tracking_sim.config import load_yaml
from tracking_sim.eval.runstore import RunRecord, RunStore
from tracking_sim.sim.scenario import Scenario, load_scenario, scenario_from_dict

__all__ = ["Settings", "create_app"]


@dataclass(frozen=True)
class Settings:
    """Where the app finds scenarios, runs and the built UI."""

    scenarios_dir: Path
    runs_dir: Path
    web_dir: Path | None = None


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

    app = FastAPI(title="tracking-sim", version=tracking_sim.__version__, lifespan=lifespan)
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

    @app.post("/api/runs", status_code=202)
    def start_run(request: RunRequest) -> dict[str, Any]:
        try:
            scenario = scenario_from_dict(request.scenario, root)
            job = jobs.submit(scenario)
        except ValidationError as exc:
            detail = json.loads(exc.json(include_url=False, include_context=False))
            raise HTTPException(422, detail) from None
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
