from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from stonesoup.types.detection import TrueDetection
from stonesoup.types.groundtruth import GroundTruthPath
from stonesoup.types.state import State

from tracking_sim.geo import LocalFrame, radar_horizon_visible
from tracking_sim.sensors import parse_sensor_spec
from tracking_sim.sensors.measurement import bearing_to_compass
from tracking_sim.sensors.radar import Crossing, RotatingRadar2D
from tracking_sim.sim import sensor_streams
from tracking_sim.truth import TruthSet, build_truth
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ORIGIN = (39.9088, -105.1172, 1729.0)
FRAME = LocalFrame(*ORIGIN)
EPOCH = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
START, END = EPOCH + timedelta(minutes=1), EPOCH + timedelta(minutes=9)


@pytest.fixture(scope="module")
def truth() -> TruthSet:
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    return build_truth(traffic.records, FRAME)


@pytest.fixture(scope="module")
def paths(truth: TruthSet) -> dict[str, GroundTruthPath]:
    return {p.id: p for p in truth.ground_truth_paths(step_s=5.0)}


def spec(**changes: Any) -> Any:
    data: dict[str, Any] = {
        "id": "radar-1",
        "type": "rotating_radar_2d",
        "position": {"lat": ORIGIN[0], "lon": ORIGIN[1], "alt_m": ORIGIN[2], "mast_m": 15},
        "coverage": {"range_min_m": 500, "range_max_m": 150_000, "elevation_deg": [-2, 40]},
        "scan": {"period_s": 5.0},
        "noise": {"range_sigma_m": 60, "bearing_sigma_deg": 0.2},
        "detection": {"pd": 0.9},
    }
    for key, value in changes.items():
        data[key] = {**data.get(key, {}), **value} if isinstance(value, dict) else value
    return parse_sensor_spec(data)


def radar(**changes: Any) -> RotatingRadar2D:
    return RotatingRadar2D(spec(**changes), FRAME, START)


def by_scan(crossings: list[Crossing]) -> dict[int, list[Crossing]]:
    scans: dict[int, list[Crossing]] = {}
    for c in crossings:
        scans.setdefault(c.scan, []).append(c)
    return scans


def test_targets_are_sampled_when_the_beam_crosses_them(truth: TruthSet) -> None:
    r = radar()
    crossings, skipped = r.crossings(truth, START, END)
    assert crossings
    assert all(not truth.segment(s).has_altitude for s in skipped)  # a taxiing aircraft
    times = np.array([c.t for c in crossings])
    beam = r.beam.azimuth_at(times)
    target = np.array([c.azimuth for c in crossings])
    assert np.all(np.abs((beam - target + np.pi) % (2 * np.pi) - np.pi) < 1e-5)
    assert np.all(np.diff(times) >= 0)
    assert all(START.timestamp() <= c.t < END.timestamp() for c in crossings)
    # The truth state is the one at the crossing time, not at the scan start.
    c = crossings[len(crossings) // 2]
    pos, _, _ = truth.segment(c.segment_id).evaluate(c.t)
    assert c.state_vector[[0, 2, 4]] == pytest.approx(pos[0])


def test_each_aircraft_is_crossed_about_once_per_rotation(truth: TruthSet) -> None:
    # Not exactly once per scan window: an aircraft near the start azimuth
    # moving against the beam is lapped slightly faster than once a period,
    # so one window can see it at its start and again at its end, as a real
    # radar would.
    r = radar()
    crossings, _ = r.crossings(truth, START, END)
    for segment_id in {c.segment_id for c in crossings}:
        t = np.array([c.t for c in crossings if c.segment_id == segment_id])
        assert np.all(np.diff(t) > 0.9 * r.period_s), segment_id
    for scan, items in by_scan(crossings).items():
        start, end = r.scan_window(scan)
        assert all(start.timestamp() <= c.t < end.timestamp() for c in items)


def test_geometry_gates(truth: TruthSet) -> None:
    near = radar(coverage={"range_max_m": 40_000})
    crossings, _ = near.crossings(truth, START, END)
    gated = [c for c in crossings if c.gate == "range"]
    assert gated and all(c.slant_range > 40_000 or c.slant_range < 500 for c in gated)
    assert all(500 <= c.slant_range <= 40_000 for c in crossings if c.in_coverage)

    east = radar(coverage={"azimuth_deg": [45, 135]})
    for c in east.crossings(truth, START, END)[0]:
        inside = 45 <= np.degrees(c.azimuth) <= 135
        assert (c.gate != "azimuth") == inside

    low = radar(coverage={"elevation_deg": [-1, 2]})
    assert any(c.gate == "elevation" for c in low.crossings(truth, START, END)[0])

    for c in radar().crossings(truth, START, END)[0]:
        visible = bool(radar_horizon_visible(c.ground_range, 15.0, c.height_m))
        assert (c.gate == "horizon") == (not visible and c.gate in (None, "horizon"))


def test_noise_free_detections_match_the_measurement_model(
    truth: TruthSet, paths: dict[str, GroundTruthPath]
) -> None:
    r = radar(noise={"range_sigma_m": 0, "bearing_sigma_deg": 0}, detection={"pd": 1.0})
    crossings, _ = r.crossings(truth, START, END)
    scan, items = next(iter(by_scan(crossings).items()))
    result = r.observe(scan, items, sensor_streams(1, r.sensor_id), paths)
    assert len(result.true_detections) == len(result.opportunities) > 0
    assert result.detected == [c.in_coverage for c in items]
    for det, c in zip(result.true_detections, result.opportunities, strict=True):
        assert isinstance(det, TrueDetection)
        assert det.timestamp == c.time
        assert det.groundtruth_path is paths[c.segment_id]
        expected = r.measurement_model.function(State(c.state_vector.reshape(6, 1)))
        assert float(det.state_vector[0, 0]) == pytest.approx(float(expected[0, 0]), abs=1e-9)
        assert float(det.state_vector[1, 0]) == pytest.approx(c.slant_range)
        assert float(bearing_to_compass(float(det.state_vector[0, 0]))) == pytest.approx(c.azimuth)
        assert det.metadata["report_time"] == c.time  # latency 0
        assert det.metadata["sensor_id"] == "radar-1" and det.metadata["kind"] == "target"


def test_noise_has_the_configured_spread(
    truth: TruthSet, paths: dict[str, GroundTruthPath]
) -> None:
    r = radar(detection={"pd": 1.0}, latency_s=0.5)
    crossings, _ = r.crossings(truth, START, END)
    streams = sensor_streams(7, r.sensor_id)
    dr, db = [], []
    for scan, items in sorted(by_scan(crossings).items()):
        result = r.observe(scan, items, streams, paths)
        for det, c in zip(result.true_detections, result.opportunities, strict=True):
            dr.append(float(det.state_vector[1, 0]) - c.slant_range)
            meas = float(bearing_to_compass(float(det.state_vector[0, 0])))
            db.append((meas - c.azimuth + np.pi) % (2 * np.pi) - np.pi)
            assert det.metadata["report_time"] == c.time + timedelta(seconds=0.5)
    assert len(dr) > 500
    assert np.std(dr) == pytest.approx(60, rel=0.1)
    assert np.degrees(np.std(db)) == pytest.approx(0.2, rel=0.1)
    assert abs(np.mean(dr)) < 60 * 4 / np.sqrt(len(dr))


def test_segments_without_altitude_are_skipped() -> None:
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", altitude_field="alt_unknown_ft"),)
    )
    from tracking_sim.truth import TruthBuilderConfig

    no_alt = build_truth(traffic.records, FRAME, TruthBuilderConfig(altitude_preference=["baro"]))
    crossings, skipped = radar().crossings(no_alt, START, END)
    assert not crossings and len(skipped) == len(no_alt)
