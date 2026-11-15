"""The map playground's HTTP API (the ``api`` extra).

``create_app`` builds a FastAPI app over a directory of scenario files and
a run store; ``tracking-sim serve`` runs it with uvicorn and serves the
built map UI from ``web/dist``. Everything is under ``/api``:

* ``GET /config``: the base map style and the package version.
* ``GET /scenarios``: scenario files in the scenarios directory.
* ``GET /scenarios/{key}``: one scenario, every sensor and tracker inline.
* ``GET /runs``: stored runs, oldest first, with headline scores.
* ``GET /runs/{id}``: one run's manifest and metrics.
* ``GET /runs/{id}/truth``: truth over the run, sampled every ``step_s``.
* ``GET /runs/{id}/detections``: every detection, by time.
* ``GET /runs/{id}/tracks/{tracker}``: one tracker's tracks.
* ``POST /runs``: run a scenario in the background; returns a job.
* ``GET /jobs`` and ``GET /jobs/{id}``: queued, running (with the stage),
  done or failed.
* ``POST /coverage``: a sensor's coverage at given heights, as GeoJSON.
* ``POST /elevation``: terrain heights at points (for a dropped sensor).
* ``POST /scenarios/export``: a scenario as a YAML file.

Replay data is in latitude and longitude, one array per field
(``api.replay``).

The server is for one person on their own machine: it has no accounts and
``serve`` listens on 127.0.0.1 unless told otherwise.
"""

from tracking_sim.api.app import create_app

__all__ = ["create_app"]
