from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from tracking_sim.geo import LocalFrame
from tracking_sim.truth import TruthRecord
from tracking_sim.truth.builder import TruthBuilderConfig, build_truth, gate_outliers
from tracking_sim.truth.synthetic import SyntheticTraffic, generate_traffic

T0 = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def traffic() -> SyntheticTraffic:
    return generate_traffic(seed=1)


@pytest.fixture(scope="module")
def frame(traffic: SyntheticTraffic) -> LocalFrame:
    return LocalFrame(*traffic.origin)


def test_gate_rejects_isolated_jumps_only() -> None:
    t = np.arange(10, dtype=float)
    x = np.column_stack([t * 100.0, np.zeros(10)])
    x[4, 1] = 20_000.0  # bad decode in the middle
    x[0, 1] = -15_000.0  # bad decode at the start
    keep = gate_outliers(t, x, max_rate=300.0, slack=100.0)
    assert keep.tolist() == [False, True, True, True, False, True, True, True, True, True]
    smooth = gate_outliers(t[1:4], x[1:4], max_rate=300.0, slack=100.0)
    assert smooth.all()


def test_builds_every_aircraft_and_splits_on_the_gap(
    traffic: SyntheticTraffic, frame: LocalFrame
) -> None:
    truth = build_truth(traffic.records, frame)
    assert set(truth.icao24s) == {f.icao24 for f in traffic.flights}
    (gap_icao,) = traffic.gaps
    for flight in traffic.flights:
        expected = 2 if flight.icao24 == gap_icao else 1
        assert len(truth.by_icao(flight.icao24)) == expected, flight.kind
    assert truth.stats.segments_built == len(traffic.flights) + 1


def test_drops_every_duplicate_and_every_outlier(
    traffic: SyntheticTraffic, frame: LocalFrame
) -> None:
    truth = build_truth(traffic.records, frame)
    rx2 = [
        r
        for r in traffic.records
        if r.source_id == "synthetic:rx2" and (r.has_position or r.has_velocity)
    ]
    assert truth.stats.duplicates_dropped == len(rx2)
    assert truth.stats.position_outliers_dropped == len(traffic.outliers)
    for bad in traffic.outliers:
        for seg in truth.by_icao(bad.icao24):
            assert not np.any(np.isclose(seg.t, bad.ts.timestamp(), rtol=0, atol=1e-3))


def test_positions_and_altitude_are_close_to_the_true_trajectory(
    traffic: SyntheticTraffic, frame: LocalFrame
) -> None:
    truth = build_truth(traffic.records, frame)
    errors = []
    for seg in truth:
        flight = traffic.flight(seg.icao24)
        true = flight.sample(seg.t - traffic.epoch.timestamp())
        e, n, _ = frame.to_enu(true["lat"], true["lon"], true["alt_m"])
        errors.append(np.hypot(seg.enu[:, 0] - e, seg.enu[:, 1] - n))
        if not flight.on_ground:
            assert seg.alt_reference == "baro"
            # Baro differs from geometric by the weather offset (up to 300 ft).
            assert np.max(np.abs(seg.alt_m - true["alt_m"])) < 100.0
    err = np.concatenate(errors)
    assert np.median(err) < 12.0  # receiver noise is 8 m per axis
    assert err.max() < 60.0


def test_merges_velocity_callsign_and_ground_state(
    traffic: SyntheticTraffic, frame: LocalFrame
) -> None:
    truth = build_truth(traffic.records, frame)
    for seg in truth:
        flight = traffic.flight(seg.icao24)
        assert seg.callsign == flight.callsign
        assert seg.on_ground == flight.on_ground
        assert seg.source_ids == ("synthetic:rx1",)
        assert np.isfinite(seg.reported_velocity).all(axis=1).mean() > 0.95
        speed = np.hypot(seg.reported_velocity[:, 0], seg.reported_velocity[:, 1])
        true = flight.sample(seg.t - traffic.epoch.timestamp())
        assert np.nanmedian(np.abs(speed - true["gs_mps"])) < 1.0
    dropped = build_truth(traffic.records, frame, TruthBuilderConfig(drop_on_ground=True))
    assert dropped.stats.segments_on_ground_dropped == 1
    assert not any(s.on_ground for s in dropped)


def rec(seconds: float, lat: float | None = 40.0, **kw: object) -> TruthRecord:
    return TruthRecord(
        icao24="abc123",
        ts=T0 + timedelta(seconds=seconds),
        lat=lat,
        lon=None if lat is None else -105.0 + seconds * 1e-4,
        source_id="t",
        **kw,  # type: ignore[arg-type]
    )


def test_altitude_preference_and_missing_altitude() -> None:
    frame = LocalFrame(40.0, -105.0, 1600.0)
    records = [rec(s, alt_baro_ft=10_000.0) for s in range(0, 50, 5)]
    records += [rec(s + 1, lat=None, alt_geom_ft=10_200.0) for s in range(0, 50, 5)]
    (seg,) = build_truth(records, frame).segments
    assert seg.alt_reference == "geom"
    assert np.allclose(seg.alt_m, 10_200.0 * 0.3048)
    config = TruthBuilderConfig(altitude_preference=["baro", "geom"])
    (seg,) = build_truth(records, frame, config).segments
    assert seg.alt_reference == "baro"

    bare = [rec(s) for s in range(0, 50, 5)]
    (seg,) = build_truth(bare, frame).segments
    assert seg.alt_reference is None and not seg.has_altitude
    assert np.isnan(seg.alt_m).all()


def test_short_segments_and_empty_input() -> None:
    frame = LocalFrame(40.0, -105.0)
    truth = build_truth([rec(s) for s in range(0, 20, 5)], frame)
    assert len(truth) == 0 and truth.stats.segments_too_short == 1
    assert truth.span is None
    assert len(build_truth([], frame)) == 0


def test_segment_category_and_nacp_sigma() -> None:
    frame = LocalFrame(40.0, -105.0, 1600.0)
    records = [rec(s, alt_baro_ft=10_000.0, nacp=9) for s in range(0, 60, 5)]
    records += [rec(1, lat=None, category="a3"), rec(2, lat=None, category="A3")]
    records += [rec(3, lat=None, category="A5")]
    (seg,) = build_truth(records, frame).segments
    assert seg.category == "A3"
    assert seg.horizontal_sigma_m == pytest.approx(30.0 / 2.45)  # NACp 9: EPU < 30 m
    assert seg.metadata()["category"] == "A3"
    off = build_truth(records, frame, TruthBuilderConfig(sigma_from_nacp=False))
    assert off.segments[0].horizontal_sigma_m == 30.0
    # NACp 0 means unknown: the default applies.
    unknown = [rec(s, alt_baro_ft=10_000.0, nacp=0) for s in range(0, 60, 5)]
    assert build_truth(unknown, frame).segments[0].horizontal_sigma_m == 30.0
    assert build_truth(unknown, frame).segments[0].category is None
