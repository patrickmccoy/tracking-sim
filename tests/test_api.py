"""The map playground's HTTP API (needs the api extra)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient

from tracking_sim.api.app import Settings, create_app
from tracking_sim.eval.runstore import RunStore
from tracking_sim.sim.scenario import load_scenario, scenario_from_dict
from tracking_sim.truth.sources.recording import write_records
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scenarios directory with one small scenario, and a run store holding its run."""
    root = tmp_path_factory.mktemp("api")
    (root / "data").mkdir()
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    write_records(root / "data/truth.parquet", traffic.records)
    scenarios = root / "scenarios"
    (scenarios / "sensors").mkdir(parents=True)
    (scenarios / "sensors/radar-1.yaml").write_text(
        (ROOT / "scenarios/sensors/radar-1.yaml").read_text()
    )
    scenario: dict[str, Any] = {
        "name": "small",
        "truth": {
            "path": "../data/truth.parquet",
            "window": ["2026-11-01T23:01:00Z", "2026-11-01T23:03:00Z"],
        },
        "sensors": ["sensors/radar-1.yaml"],
        "trackers": [{"id": "ekf", "filter": "ekf"}],
        "seed": 3,
    }
    (scenarios / "small.yaml").write_text(yaml.safe_dump(scenario))
    RunStore(root / "runs").run(load_scenario(scenarios / "small.yaml"))
    return root


@pytest.fixture
def client(workspace: Path) -> TestClient:
    app = create_app(Settings(scenarios_dir=workspace / "scenarios", runs_dir=workspace / "runs"))
    return TestClient(app)


def test_health_and_config(client: TestClient) -> None:
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/config").json()["map_style"].startswith("https://")


def test_scenarios_are_the_top_level_files(client: TestClient) -> None:
    assert client.get("/api/scenarios").json() == [
        {"key": "small", "name": "small", "file": "small.yaml"}
    ]
    assert client.get("/api/scenarios/nope").status_code == 404


def test_a_scenario_comes_back_inline_and_round_trips(client: TestClient, workspace: Path) -> None:
    body = client.get("/api/scenarios/small").json()
    doc = body["scenario"]
    assert doc["sensors"][0]["id"] == "radar-1" and doc["sensors"][0]["scan"]["period_s"] == 5.0
    assert doc["trackers"][0]["filter"] == "ekf"
    assert doc["truth"]["path"] == "../data/truth.parquet"
    assert body["frame"]["lat"] == pytest.approx(39.9088)
    # Relative to the scenarios directory, the document is the same scenario.
    again = scenario_from_dict(doc, workspace / "scenarios")
    original = load_scenario(workspace / "scenarios/small.yaml")
    assert again.resolved() == original.resolved()


def test_runs_are_listed_with_headline_scores(client: TestClient) -> None:
    (row,) = client.get("/api/runs").json()
    assert row["scenario"] == "small" and row["seed"] == 3 and row["sensors"] == ["radar-1"]
    (tracker,) = row["trackers"]
    assert tracker["tracker"] == "ekf" and "ospa_m" in tracker and "over_time" not in tracker
    one = client.get(f"/api/runs/{row['id'][:10]}").json()
    assert one["id"] == row["id"]
    assert one["metrics"]["trackers"][0]["over_time"]
    assert one["manifest"]["scenario"]["name"] == "small"
    assert client.get("/api/runs/missing").status_code == 404


def test_the_built_ui_is_served_for_any_other_path(workspace: Path, tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>map</title>")
    (tmp_path / "assets/app.js").write_text("console.log(1)")
    app = create_app(
        Settings(
            scenarios_dir=workspace / "scenarios", runs_dir=workspace / "runs", web_dir=tmp_path
        )
    )
    client = TestClient(app)
    assert "map" in client.get("/").text
    assert "map" in client.get("/runs/abc").text  # client-side route
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/api/nothing").status_code == 404
    assert client.get("/api/health").json()["ok"]


def test_a_posted_scenario_runs_in_the_background(workspace: Path, tmp_path: Path) -> None:
    app = create_app(Settings(scenarios_dir=workspace / "scenarios", runs_dir=tmp_path / "runs"))
    client = TestClient(app)
    doc = client.get("/api/scenarios/small").json()["scenario"]
    # Move the radar, as the map would.
    doc["sensors"][0]["position"]["lat"] += 0.05
    job = client.post("/api/runs", json={"scenario": doc}).json()
    assert job["state"] in ("queued", "running") and job["run_id"].startswith("small-")
    # The same scenario again while it runs is the same job.
    assert client.post("/api/runs", json={"scenario": doc}).json()["id"] == job["id"]
    app.state.jobs.wait()
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["state"] == "done" and not done["reused"] and done["error"] is None
    run = client.get(f"/api/runs/{job['run_id']}").json()
    assert run["manifest"]["scenario"]["sensors"][0]["position"]["lat"] == pytest.approx(39.9588)
    # Once stored, it is reused at once.
    again = client.post("/api/runs", json={"scenario": doc}).json()
    assert again["state"] == "done" and again["reused"] and again["run_id"] == job["run_id"]
    assert [j["id"] for j in client.get("/api/jobs").json()] == [again["id"], job["id"]]
    assert client.get("/api/jobs/nope").status_code == 404


def test_a_bad_scenario_is_rejected_with_reasons(client: TestClient) -> None:
    doc = client.get("/api/scenarios/small").json()["scenario"]
    bad = {**doc, "sensors": [{**doc["sensors"][0], "scan": {"period_s": -1}}]}
    response = client.post("/api/runs", json={"scenario": bad})
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert "period_s" in error["loc"]
    missing = {**doc, "truth": {**doc["truth"], "path": "nowhere.parquet"}}
    response = client.post("/api/runs", json={"scenario": missing})
    assert response.status_code == 422 and "nowhere.parquet" in response.json()["detail"]


def test_replay_data_is_in_latitude_and_longitude(client: TestClient) -> None:
    (row,) = client.get("/api/runs").json()
    run = row["id"]
    truth = client.get(f"/api/runs/{run}/truth", params={"step_s": 10}).json()
    lo, hi = truth["window"]
    assert hi - lo == 120 and truth["segments"]
    for seg in truth["segments"]:
        assert lo <= seg["t"][0] < seg["t"][-1] <= hi
        assert len(seg["t"]) == len(seg["lat"]) == len(seg["lon"]) == len(seg["alt_m"])
        assert all(
            abs(lat - 39.9) < 2 and abs(lon + 105.1) < 2
            for lat, lon in zip(seg["lat"], seg["lon"], strict=True)
        )
    assert client.get(f"/api/runs/{run}/truth", params={"step_s": 0}).status_code == 422

    det = client.get(f"/api/runs/{run}/detections").json()
    n = len(det["t"])
    assert n > 100 and all(len(v) == n for v in det.values())
    assert det["t"] == sorted(det["t"])
    assert {"target", "clutter"} == set(det["kind"])
    ids = {s["id"] for s in truth["segments"]}
    assert {s for s, k in zip(det["segment_id"], det["kind"], strict=True) if k == "target"} <= ids

    tracks = client.get(f"/api/runs/{run}/tracks/ekf").json()["tracks"]
    assert tracks and all(len(t["t"]) == len(t["lat"]) for t in tracks)
    assert all(all(t["confirmed"]) for t in tracks)
    every = client.get(f"/api/runs/{run}/tracks/ekf", params={"tentative": True}).json()
    assert sum(len(t["t"]) for t in every["tracks"]) > sum(len(t["t"]) for t in tracks)
    assert any(t["segment_id"] in ids for t in tracks)
    assert client.get(f"/api/runs/{run}/tracks/nope").status_code == 404


def test_coverage_for_a_dragged_sensor(client: TestClient) -> None:
    sensor = client.get("/api/scenarios/small").json()["scenario"]["sensors"][0]
    ridge = {
        "source": "synthetic",
        "ridge": {"lat": 39.9088, "lon": -105.19, "height_m": 900, "base_m": 1729},
    }
    flat = client.post("/api/coverage", json={"sensor": sensor, "heights_m": [300, 3000]}).json()
    assert [f["properties"]["height_m"] for f in flat["features"]] == [300, 3000]
    hilly = client.post(
        "/api/coverage", json={"sensor": sensor, "terrain": ridge, "heights_m": [300, 3000]}
    ).json()
    for a, b in zip(flat["features"], hilly["features"], strict=True):
        assert b["properties"]["area_km2"] < a["properties"]["area_km2"]
    bad = {**sensor, "coverage": {**sensor["coverage"], "range_max_m": -1}}
    assert client.post("/api/coverage", json={"sensor": bad}).status_code == 422
    assert client.post("/api/coverage", json={"sensor": sensor, "heights_m": []}).status_code == 422


def test_elevation_comes_from_the_scenario_terrain(client: TestClient) -> None:
    ridge = {
        "source": "synthetic",
        "ridge": {"lat": 39.9, "lon": -105.2, "height_m": 900, "base_m": 1700},
    }
    points = [[39.9, -105.2], [39.9, -104.9]]
    body = client.post("/api/elevation", json={"terrain": ridge, "points": points}).json()
    top, plain = body["heights_m"]
    assert top == pytest.approx(2600, abs=1) and plain == pytest.approx(1700, abs=1)
    none = client.post("/api/elevation", json={"terrain": None, "points": points}).json()
    assert none == {"heights_m": [None, None]}
    assert client.post("/api/elevation", json={"points": []}).status_code == 422


def test_an_edited_scenario_exports_as_yaml(client: TestClient, workspace: Path) -> None:
    doc = client.get("/api/scenarios/small").json()["scenario"]
    doc["name"] = 'moved "radar"'
    doc["sensors"][0]["position"]["lon"] = -105.0
    response = client.post("/api/scenarios/export", json={"scenario": doc})
    assert response.status_code == 200
    assert 'filename="moved-radar.yaml"' in response.headers["content-disposition"]
    saved = workspace / "scenarios" / "moved.yaml"
    saved.write_text(response.text)
    try:
        assert load_scenario(saved).sensors[0].position.lon == -105.0
    finally:
        saved.unlink()
    doc["seed"] = -1
    assert client.post("/api/scenarios/export", json={"scenario": doc}).status_code == 422
