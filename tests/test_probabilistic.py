"""PDA hypotheses with Pd and clutter from the radar, at one time per track."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from scipy import integrate
from stonesoup.predictor.kalman import ExtendedKalmanPredictor
from stonesoup.types.detection import Detection
from stonesoup.types.state import GaussianState
from stonesoup.types.track import Track
from stonesoup.updater.kalman import ExtendedKalmanUpdater

from tracking_sim.geo import LocalFrame, SensorSite
from tracking_sim.sensors import load_sensor_spec
from tracking_sim.sensors.measurement import CartesianToBearingSlantRange
from tracking_sim.sensors.radar import RotatingRadar2D
from tracking_sim.sensors.spec import RotatingRadar2DSpec
from tracking_sim.tracking import load_tracker_spec
from tracking_sim.tracking.factory import _cv
from tracking_sim.tracking.probabilistic import (
    RadarModel,
    RadarPDAHypothesiser,
    gate_probability,
)

ROOT = Path(__file__).resolve().parents[1]
FRAME = LocalFrame(39.9088, -105.1172, 1729.0)
EPOCH = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)


def radar_spec(**detection: object) -> RotatingRadar2DSpec:
    spec = load_sensor_spec(ROOT / "scenarios/sensors/radar-1.yaml")
    assert isinstance(spec, RotatingRadar2DSpec)
    if detection:
        merged = {**spec.detection.model_dump(), **detection}
        spec = spec.model_copy(update={"detection": type(spec.detection)(**merged)})
    return spec


def test_gate_probability_is_the_mahalanobis_gate() -> None:
    assert gate_probability(3.0) == pytest.approx(1 - np.exp(-4.5))
    assert gate_probability(4.0) == pytest.approx(0.99966, abs=1e-5)
    # Stone Soup gates on the inverse: the same number of standard deviations.
    assert RadarPDAHypothesiser._gate_threshold(gate_probability(4.0), 2) == pytest.approx(16.0)


def test_clutter_density_adds_up_to_the_radars_false_alarms() -> None:
    spec = radar_spec()
    model = RadarModel(spec)
    cov = spec.coverage
    per_scan, _ = integrate.quad(model.clutter_density, cov.range_min_m, cov.range_max_m)
    per_scan *= np.radians(cov.sector_width_deg)
    radar = RotatingRadar2D(spec, FRAME, EPOCH)
    assert per_scan == pytest.approx(radar.expected_false_alarms, rel=1e-6)
    assert model.clutter_density(50_000) == pytest.approx(5 * model.clutter_density(10_000))


def test_prob_detect_follows_the_detection_model() -> None:
    assert RadarModel(radar_spec()).prob_detect(80_000) == 0.9
    spec = radar_spec(model="radar_equation", reference_snr_db=13, reference_range_m=50_000)
    model = RadarModel(spec)
    radar = RotatingRadar2D(spec, FRAME, EPOCH)
    for r in (20_000.0, 60_000.0, 100_000.0):
        expected = float(radar.pd(r, spec.detection.default_class))
        assert model.prob_detect(r) == pytest.approx(min(max(expected, 0.01), 0.999))
    assert model.prob_detect(20_000) > model.prob_detect(100_000)


def measurement_model() -> CartesianToBearingSlantRange:
    site = SensorSite(FRAME, FRAME.lat, FRAME.lon, FRAME.alt_m, 15.0)
    return CartesianToBearingSlantRange(
        ndim_state=6,
        mapping=(0, 2, 4),
        noise_covar=np.diag([np.radians(0.2) ** 2, 60.0**2]),
        translation_offset=site.position.reshape(3, 1),
        frame_rotation=site.rotation,
    )


def hypothesiser(**fixed: float) -> RadarPDAHypothesiser:
    spec = load_tracker_spec(ROOT / "scenarios/trackers/ekf-cv-gnn.yaml")
    return RadarPDAHypothesiser(
        predictor=ExtendedKalmanPredictor(_cv(spec)),
        updater=ExtendedKalmanUpdater(measurement_model=None),
        clutter_spatial_density=None,
        prob_detect=0.9,
        prob_gate=gate_probability(4.0),
        radars={"radar-1": RadarModel(radar_spec())},
        position_mapping=(0, 2, 4),
        **{f"fixed_{k}": v for k, v in fixed.items()},
    )


def track_and_detections() -> tuple[Track, list[Detection]]:
    # 40 km north of the radar heading east at 200 m/s.
    state = GaussianState(
        np.array([[0.0], [200.0], [40_000.0], [0.0], [5000.0], [0.0]]),
        np.diag([200.0**2, 20.0**2, 200.0**2, 20.0**2, 1000.0**2, 5.0**2]),
        timestamp=EPOCH,
    )
    m = measurement_model()
    seen = EPOCH + timedelta(seconds=3)
    detections = [
        Detection(
            np.array([[np.pi / 2 - b], [r]]),
            timestamp=seen + timedelta(milliseconds=i),
            measurement_model=m,
            metadata={"sensor_id": "radar-1"},
        )
        for i, (b, r) in enumerate(((0.015, 40_300.0), (0.02, 40_600.0)))
    ]
    return Track([state]), detections


def test_every_hypothesis_is_predicted_to_the_detections_time() -> None:
    track, detections = track_and_detections()
    scan_end = EPOCH + timedelta(seconds=5)
    hypotheses = hypothesiser().hypothesise(track, set(detections), scan_end)
    assert len(hypotheses) == 3  # missed, and both detections
    times = {h.prediction.timestamp for h in hypotheses}
    latest = max(d.timestamp for d in detections)
    assert max(times) == latest and max(times) - min(times) < timedelta(milliseconds=5)
    # With nothing in the gate, the track is predicted to the scan's end.
    (missed,) = hypothesiser().hypothesise(track, set(), scan_end)
    assert missed.prediction.timestamp == scan_end


def test_pd_and_clutter_come_from_the_radar_at_the_tracks_range() -> None:
    track, detections = track_and_detections()
    h = hypothesiser()
    h.hypothesise(track, set(detections), EPOCH + timedelta(seconds=5))
    slant = np.hypot(40_000.0, 5000.0 - 15.0)
    assert float(h.prob_detect) == pytest.approx(0.9)
    assert h.clutter_spatial_density == pytest.approx(1e-9 * slant, rel=0.01)
    fixed = hypothesiser(prob_detect=0.7, clutter_density=2e-6)
    fixed.hypothesise(track, set(detections), EPOCH + timedelta(seconds=5))
    assert float(fixed.prob_detect) == pytest.approx(0.7)
    assert fixed.clutter_spatial_density == 2e-6
    # More expected clutter makes a detection less likely to be the aircraft.
    weights = [
        float(
            hypothesiser(clutter_density=c)
            .hypothesise(track, set(detections), EPOCH)[0]
            .probability
        )
        for c in (1e-8, 1e-4)
    ]
    assert weights[0] != weights[1]


def test_an_unknown_radar_is_an_error() -> None:
    track, detections = track_and_detections()
    for d in detections:
        d.metadata["sensor_id"] = "radar-9"
    with pytest.raises(ValueError, match="radar-9"):
        hypothesiser().hypothesise(track, set(detections), EPOCH)
