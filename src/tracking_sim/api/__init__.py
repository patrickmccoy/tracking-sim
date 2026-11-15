"""The map playground's HTTP API (the ``api`` extra).

``create_app`` builds a FastAPI app over a directory of scenario files and
a run store; ``tracking-sim serve`` runs it with uvicorn and serves the
built map UI from ``web/dist``. Everything is under ``/api``:

==============================  =============================================
``GET /api/scenarios``          scenario files in the scenarios directory
``GET /api/scenarios/{key}``    one scenario, every sensor and tracker inline
``GET /api/runs``               stored runs, oldest first, with headline scores
``GET /api/runs/{id}``          one run's manifest and metrics
``POST /api/runs``              run a scenario in the background: a job
``GET /api/jobs``               jobs since the server started, newest first
``GET /api/jobs/{id}``          one job: queued, running (and its stage),
                                done or failed
==============================  =============================================

The server is for one person on their own machine: it has no accounts and
``serve`` listens on 127.0.0.1 unless told otherwise.
"""

from tracking_sim.api.app import create_app

__all__ = ["create_app"]
