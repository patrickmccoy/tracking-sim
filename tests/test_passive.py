from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from stonesoup.types.detection import TrueDetection
from stonesoup.types.state import State

from tracking_sim.geo import LocalFrame
from tracking_sim.sensors import PassiveBearingSpec, parse_sensor_spec
from tracking_sim.sensors.measurement import bearing_to_compass
from tracking_sim.sensors.passive import PassiveBearingSensor
from tracking_sim.sim import ReplayEngine, sensor_streams, wilson_interval
from tracking_sim.truth import TruthSet, build_truth
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = (39.9088, -105.1172, 1729.0)
FRAME = LocalFrame(*ORIGIN)
EPOCH = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
START, END = EPOCH + timedelta(minutes=1), EPOCH + timedelta(minutes=4)


@pytest.fixture(scope="module")
def truth() -> TruthSet:
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    return build_truth(traffic.records, FRAME)


def spec(**changes: Any) -> Any:
    data: dict[str, Any] = {
        "id": "esm-1",
        "type": "passive_bearing",
        "position": {"lat": 40.05, "lon": -105.0, "alt_m": 1600, "mast_m": 10},
        "coverage": {"range_max_m": 150_000, "elevation_deg": [-2, 60]},
        "update_period_s": 2.0,
        "noise": {"bearing_sigma_deg": 1.0},
        "detection": {"pd": 1.0},
    }
    for key, value in changes.items():
        data[key] = {**data.get(key, {}), **value} if isinstance(value, dict) else value
    return parse_sensor_spec(data)


def test_spec_loads_with_defaults() -> None:
    s = spec()
    assert isinstance(s, PassiveBearingSpec)
    assert s.detection.model == "constant" and s.detection.swerling == 0
    one_way = spec(
        detection={
            "model": "one_way",
            "reference_snr_db": 15,
            "reference_range_m": 50_000,
            "pd": None,
        }
    )
    assert one_way.detection.reference_snr_db == 15
    with pytest.raises(ValueError, match="reference_snr_db"):
        spec(detection={"model": "one_way", "pd": None})


def test_looks_are_on_the_update_clock(truth: TruthSet) -> None:
    sensor = PassiveBearingSensor(spec(), FRAME, START)
    crossings, _ = sensor.crossings(truth, START, END)
    assert crossings
    offsets = np.array([c.t - START.timestamp() for c in crossings])
    assert np.allclose(offsets / 2.0, np.round(offsets / 2.0))
    for c in crossings:
        start, end = sensor.scan_window(c.scan)
        assert start.timestamp() == pytest.approx(c.t) and end > start
    # A staring sensor sees each covered aircraft at every update.
    per_scan: dict[str, set[int]] = {}
    for c in crossings:
        per_scan.setdefault(c.segment_id, set()).add(c.scan)
    seg = truth.segment(max(per_scan, key=lambda s: len(per_scan[s])))
    lo = max(seg.t[0], START.timestamp())
    hi = min(seg.t[-1], END.timestamp())
    assert len(per_scan[seg.segment_id]) == pytest.approx((hi - lo) / 2.0, abs=1.5)


def test_bearings_match_the_model_and_noise_spread(truth: TruthSet) -> None:
    exact = PassiveBearingSensor(spec(noise={"bearing_sigma_deg": 0.0}), FRAME, START)
    crossings, _ = exact.crossings(truth, START, END)
    paths = {p.id: p for p in truth.ground_truth_paths(step_s=5.0)}
    scans: dict[int, list[Any]] = {}
    for c in crossings:
        scans.setdefault(c.scan, []).append(c)
    k, items = next(iter(sorted(scans.items())))
    result = exact.observe(k, items, sensor_streams(1, "esm-1"), paths)
    for det, c in zip(result.true_detections, result.opportunities, strict=True):
        assert isinstance(det, TrueDetection) and det.state_vector.shape == (1, 1)
        model_bearing = exact.measurement_model.function(State(c.state_vector.reshape(6, 1)))
        assert float(det.state_vector[0, 0]) == pytest.approx(float(model_bearing[0, 0]))
        assert float(bearing_to_compass(float(det.state_vector[0, 0]))) == pytest.approx(c.azimuth)

    noisy = PassiveBearingSensor(spec(), FRAME, START)
    streams = sensor_streams(2, "esm-1")
    errors = []
    for k, items in sorted(scans.items()):
        result = noisy.observe(k, items, streams, paths)
        for det, c in zip(result.true_detections, result.opportunities, strict=True):
            meas = float(bearing_to_compass(float(det.state_vector[0, 0])))
            errors.append((meas - c.azimuth + np.pi) % (2 * np.pi) - np.pi)
    assert np.degrees(np.std(errors)) == pytest.approx(1.0, rel=0.1)


def test_one_way_pd_and_false_bearings(truth: TruthSet) -> None:
    s = spec(
        detection={
            "model": "one_way",
            "reference_snr_db": 13.0,
            "reference_range_m": 40_000,
            "pd": None,
            "false_alarm_rate": 3.0,
        }
    )
    sensor = PassiveBearingSensor(s, FRAME, START)
    pd = sensor.pd(np.array([20_000.0, 40_000.0, 80_000.0]), "unknown")
    assert pd[0] > pd[1] > pd[2]
    # One way: doubling range costs 6 dB, not 12.
    from tracking_sim.sensors.detection import pd_swerling

    assert pd[2] == pytest.approx(pd_swerling(10**1.3 / 4, 1e-6, 0))
    result = ReplayEngine(truth, [s], seed=4).run(START, END)
    stats = result.stats("esm-1")
    lo, hi = wilson_interval(stats.detected, stats.opportunities, z=3.29)
    assert lo <= stats.configured_pd <= hi
    assert stats.false_alarms_per_scan == pytest.approx(3.0, rel=0.25)
    table = result.detections_table()
    assert table.num_rows > 0
    assert table.column("slant_range_m").null_count == table.num_rows  # bearing only
    assert table.column("east_m").null_count == table.num_rows


def test_passive_sensor_in_a_scenario_with_trackers(tmp_path: Path) -> None:
    from tracking_sim.sim.scenario import load_scenario, run_scenario
    from tracking_sim.truth.sources.recording import write_records

    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    write_records(tmp_path / "truth.parquet", traffic.records)
    passive = spec().model_dump(mode="json")
    scenario = {
        "name": "mixed",
        "truth": {
            "path": "truth.parquet",
            "window": ["2026-11-01T23:01:00Z", "2026-11-01T23:03:00Z"],
        },
        "sensors": [passive, str(ROOT / "scenarios/sensors/radar-1.yaml")],
        "trackers": [{"id": "ekf", "filter": "ekf"}],
        "seed": 3,
    }
    (tmp_path / "s.yaml").write_text(yaml.safe_dump(scenario))
    result = run_scenario(load_scenario(tmp_path / "s.yaml"))
    assert {s.sensor_id for s in result.simulation.scans} == {"esm-1", "radar-1"}
    # Trackers saw only radar detections, and are scored on the radar's grid.
    used = {
        st.hypothesis.measurement.metadata["sensor_id"]
        for track in result.runs[0].tracks
        for st in track.states
        if hasattr(st, "hypothesis") and st.hypothesis.measurement is not None
    }
    assert used == {"radar-1"}
    assert result.grid.period_s == 5.0
    assert "esm-1" in result.summary()

    scenario["sensors"] = [passive]
    (tmp_path / "s.yaml").write_text(yaml.safe_dump(scenario))
    with pytest.raises(ValueError, match="rotating_radar_2d"):
        run_scenario(load_scenario(tmp_path / "s.yaml"))
