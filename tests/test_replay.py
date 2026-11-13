from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("matplotlib")

import matplotlib

matplotlib.use("Agg")

from stonesoup.models.transition.linear import (
    CombinedLinearGaussianTransitionModel,
    ConstantVelocity,
)
from stonesoup.predictor.kalman import ExtendedKalmanPredictor
from stonesoup.types.hypothesis import SingleHypothesis
from stonesoup.types.state import GaussianState
from stonesoup.types.track import Track
from stonesoup.updater.kalman import ExtendedKalmanUpdater

from tracking_sim.geo import LocalFrame
from tracking_sim.sensors import load_sensor_spec
from tracking_sim.sim.dataset import TrackingData, simulate
from tracking_sim.tracking.initiators import initial_position
from tracking_sim.truth import build_truth
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic
from tracking_sim.viz.replay import Estimate, Replay, horizontal_error

ROOT = Path(__file__).resolve().parents[1]
EPOCH = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def data() -> TrackingData:
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    truth = build_truth(traffic.records, LocalFrame(39.9088, -105.1172, 1729.0))
    radar = load_sensor_spec(ROOT / "scenarios/sensors/radar-1.yaml")
    return simulate(
        truth, radar, seed=3, start=EPOCH + timedelta(minutes=1), end=EPOCH + timedelta(minutes=6)
    )


def ekf_track(data: TrackingData, segment: str) -> Track:
    dets = data.detections_of(segment)
    d0 = dets[0]
    pos, cov = initial_position(
        d0.measurement_model, float(d0.state_vector[0, 0]), float(d0.state_vector[1, 0]), 5000, 3000
    )
    mean, covar = np.zeros(6), np.diag([0, 150.0**2, 0, 150.0**2, 0, 1.0])
    mean[[0, 2, 4]] = pos
    covar[np.ix_([0, 2, 4], [0, 2, 4])] = cov
    motion = CombinedLinearGaussianTransitionModel(
        [ConstantVelocity(5.0), ConstantVelocity(5.0), ConstantVelocity(0.01)]
    )
    predictor, updater = ExtendedKalmanPredictor(motion), ExtendedKalmanUpdater(None)
    track = Track([GaussianState(mean.reshape(-1, 1), covar, timestamp=d0.timestamp)])
    for d in dets[1:]:
        track.append(
            updater.update(SingleHypothesis(predictor.predict(track[-1], timestamp=d.timestamp), d))
        )
    return track


def test_replay_draws_every_layer(data: TrackingData) -> None:
    segment = data.aircraft()["segment_id"][0].as_py()
    estimate = Estimate("EKF", ekf_track(data, segment))
    t, err = horizontal_error(data, segment, estimate)
    assert len(t) == len(data.detections_of(segment)) and np.nanmedian(err) < 2000
    replay = Replay(data, segment, [estimate])
    assert replay.frames and replay.frames == sorted(replay.frames)
    fig = replay.draw(len(replay.frames) // 2)
    ax_map, ax_err = fig.axes
    labels = {line.get_label() for line in ax_map.get_lines()}
    assert {"truth", "EKF"} <= labels
    assert len(ax_err.get_lines()) == 2  # the error curve and the cursor
    text = replay.summary(len(replay.frames) // 2)
    assert "EKF" in text and text.endswith(" m")
    with pytest.raises(ValueError, match="never detected"):
        Replay(data, "nobody-0")
    with pytest.raises(ValueError, match="at most"):
        Replay(data, segment, [estimate] * 9)


def test_replay_widget(data: TrackingData) -> None:
    widgets = pytest.importorskip("ipywidgets")
    segment, other = data.aircraft()["segment_id"].to_pylist()[:2]
    calls: list[str] = []

    def choose(seg: str) -> list[Estimate]:
        calls.append(seg)
        return [Estimate("EKF", ekf_track(data, seg))]

    replay = Replay(data, segment, choose(segment))
    player = replay.widget(choose, [segment, other])
    assert isinstance(player, widgets.VBox)
    controls = player.children[0].children
    picker, _play, slider = controls
    assert slider.max == len(replay.frames) - 1
    picker.value = other
    assert calls == [segment, other]
    assert "EKF" in player.children[1].value
