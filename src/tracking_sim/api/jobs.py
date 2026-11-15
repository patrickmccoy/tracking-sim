"""Running scenarios in the background for the API.

A run of an hour of traffic takes a minute or more, far too long for one
HTTP request, so ``POST /api/runs`` queues a job and the UI polls it.
Jobs run one at a time on a single worker thread: a run is CPU-bound, and
two at once would only make both slower.

A job for a run that is already stored finishes at once, and submitting a
scenario whose run is already queued or running returns that job rather
than queuing it twice. Jobs live in memory and are forgotten when the
server stops; the runs they made are in the run store.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from tracking_sim.eval.runstore import RunStore
from tracking_sim.sim.scenario import Scenario

__all__ = ["Job", "JobRunner"]

log = logging.getLogger(__name__)

JobState = Literal["queued", "running", "done", "failed"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    run_id: str
    scenario: str
    state: JobState = "queued"
    stage: str | None = None
    reused: bool = False
    error: str | None = None
    created: str = field(default_factory=_now)
    started: str | None = None
    finished: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "scenario": self.scenario,
            "state": self.state,
            "stage": self.stage,
            "reused": self.reused,
            "error": self.error,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
        }


class JobRunner:
    """Queues scenario runs into ``store`` and runs them one at a time."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tracking-sim-run")

    def submit(self, scenario: Scenario) -> Job:
        """A job for ``scenario``'s run: done if stored, else queued or already active."""
        run_id, _ = self.store.plan(scenario)
        with self._lock:
            for job in self._jobs.values():
                if job.run_id == run_id and job.state in ("queued", "running"):
                    return job
            job = Job(id=uuid.uuid4().hex[:12], run_id=run_id, scenario=scenario.spec.name)
            self._jobs[job.id] = job
            if (self.store.root / run_id / "manifest.json").exists():
                job.state, job.reused = "done", True
                job.started = job.finished = job.created
                return job
        self._pool.submit(self._run, job, scenario)
        return job

    def _run(self, job: Job, scenario: Scenario) -> None:
        job.state, job.started = "running", _now()

        def progress(stage: str) -> None:
            job.stage = stage

        try:
            record = self.store.run(scenario, progress=progress)
        except Exception as exc:
            log.exception("run %s failed", job.run_id)
            job.state, job.error = "failed", f"{type(exc).__name__}: {exc}"
        else:
            job.reused = record.reused
            job.state = "done"
        job.stage = None
        job.finished = _now()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        """Every job since the server started, newest first."""
        with self._lock:
            return list(reversed(self._jobs.values()))

    def wait(self) -> None:
        """Block until every queued job has finished (for tests and shutdown)."""
        self._pool.submit(lambda: None).result()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
