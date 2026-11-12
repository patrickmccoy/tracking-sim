from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from tracking_sim.geo import LocalFrame
from tracking_sim.sensors import parse_sensor_spec
from tracking_sim.sim import ReplayEngine, wilson_interval
from tracking_sim.truth import TruthSet, build_truth
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

ORIGIN = (39.9088, -105.1172, 1729.0)
FRAME = LocalFrame(*ORIGIN)
EPOCH = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
START, END = EPOCH + timedelta(minutes=1), EPOCH + timedelta(minutes=9, seconds=2)


@pytest.fixture(scope="module")
def truth() -> TruthSet:
    traffic = generate_traffic(
        seed=5, duration_s=600, receivers=(ReceiverConfig("rx", outlier_count=0),)
    )
    return build_truth(traffic.records, FRAME)


def spec(sensor_id: str = "radar-1", **changes: Any) -> Any:
    data: dict[str, Any] = {
        "id": sensor_id,
        "type": "rotating_radar_2d",
        "position": {"lat": ORIGIN[0], "lon": ORIGIN[1], "alt_m": ORIGIN[2], "mast_m": 15},
        "coverage": {"range_min_m": 500, "range_max_m": 150_000, "elevation_deg": [-2, 40]},
        "scan": {"period_s": 5.0},
        "noise": {"range_sigma_m": 60, "bearing_sigma_deg": 0.2},
        "detection": {"pd": 0.7, "false_alarm_density": 2e-10},
        "resolution": {"range_m": 150, "bearing_deg": 1.5},
    }
    for key, value in changes.items():
        data[key] = {**data.get(key, {}), **value} if isinstance(value, dict) else value
    return parse_sensor_spec(data)


def test_same_seed_same_detections(truth: TruthSet) -> None:
    a = ReplayEngine(truth, [spec()], seed=42).run(START, END)
    b = ReplayEngine(truth, [spec()], seed=42).run(START, END)
    assert a.detections_table().num_rows > 0
    assert a.detections_table().equals(b.detections_table())
    assert a.opportunities_table().equals(b.opportunities_table())
    c = ReplayEngine(truth, [spec()], seed=43).run(START, END)
    assert not a.detections_table().equals(c.detections_table())
    # Geometry does not depend on the seed, only the draws do.
    assert (
        a.opportunities_table()
        .drop(["detected"])
        .equals(c.opportunities_table().drop(["detected"]))
    )


def test_adding_a_sensor_leaves_the_others_unchanged(truth: TruthSet) -> None:
    alone = ReplayEngine(truth, [spec()], seed=42).run(START, END).detections_table()
    second = spec("radar-2", position={"lat": 40.2, "lon": -104.8}, scan={"period_s": 4.0})
    both = ReplayEngine(truth, [second, spec()], seed=42).run(START, END)
    table = both.detections_table()
    mine = table.filter(pc.equal(table["sensor_id"], "radar-1"))
    assert mine.equals(alone)
    assert pc.sum(pc.equal(table["sensor_id"], "radar-2")).as_py() > 0


def test_clutter_density_does_not_change_target_detections(truth: TruthSet) -> None:
    quiet = ReplayEngine(truth, [spec(detection={"false_alarm_density": 0})], seed=42)
    noisy = ReplayEngine(truth, [spec(detection={"false_alarm_density": 1e-9})], seed=42)

    def targets(engine: ReplayEngine) -> Any:
        table = engine.run(START, END).detections_table()
        return table.filter(pc.equal(table["kind"], "target"))

    assert targets(quiet).equals(targets(noisy))


def test_scans_are_whole_and_in_time_order(truth: TruthSet) -> None:
    engine = ReplayEngine(
        truth, [spec("a", scan={"period_s": 4.0}), spec("b", scan={"period_s": 5.0})], seed=1
    )
    scans = list(engine.scans(START, END))
    keys = [(s.end, s.sensor_id) for s in scans]
    assert keys == sorted(keys)
    window = (END - START).total_seconds()  # 482 s: 120 whole 4 s scans, 96 whole 5 s scans
    assert sum(s.sensor_id == "a" for s in scans) == int(window // 4)
    assert sum(s.sensor_id == "b" for s in scans) == int(window // 5)
    assert all(s.end <= END for s in scans)
    for s in scans:
        assert all(s.start <= d.timestamp < s.end for d in s.detections)


def test_achieved_pd_matches_the_configured_pd(truth: TruthSet) -> None:
    result = ReplayEngine(truth, [spec()], seed=7).run(START, END)
    stats = result.stats("radar-1")
    assert stats.opportunities > 1000
    lo, hi = wilson_interval(stats.detected, stats.opportunities, z=3.29)  # 99.9%
    assert lo <= 0.7 <= hi
    assert stats.false_alarms_per_scan == pytest.approx(
        stats.expected_false_alarms_per_scan, rel=0.2
    )
    assert "achieved Pd" in result.summary()
    # Merged detections count once in the detections, but each target in Pd.
    table = result.detections_table()
    targets = table.filter(pc.equal(table["kind"], "target"))
    assert pc.sum(targets["merged"]).as_py() == stats.detected


def test_tables_and_parquet(truth: TruthSet, tmp_path: Path) -> None:
    result = ReplayEngine(truth, [spec()], seed=3).run(START, END)
    paths = result.write(tmp_path / "run")
    detections = pq.read_table(paths["detections"])
    opportunities = pq.read_table(paths["opportunities"])
    assert detections.equals(result.detections_table())
    assert opportunities.equals(result.opportunities_table())
    clutter = detections.filter(pc.equal(detections["kind"], "clutter"))
    assert clutter.num_rows > 0 and pc.all(pc.is_null(clutter["segment_id"])).as_py()
    # PPI position: slant range laid on the ground, so at least the true ground range.
    targets = detections.filter(pc.equal(detections["kind"], "target"))
    assert targets.num_rows == len(result.detections()) - clutter.num_rows
    assert set(opportunities["gate"].drop_null().to_pylist()) <= {
        "range",
        "azimuth",
        "elevation",
        "horizon",
        "terrain",
    }


def test_wilson_interval() -> None:
    lo, hi = wilson_interval(70, 100)
    assert lo < 0.7 < hi and hi - lo == pytest.approx(0.18, abs=0.01)
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_engine_rejects_bad_input(truth: TruthSet) -> None:
    with pytest.raises(ValueError, match="unique"):
        ReplayEngine(truth, [spec(), spec()], seed=1)
    with pytest.raises(ValueError, match="at least one"):
        ReplayEngine(truth, [], seed=1)
    with pytest.raises(ValueError, match="after start"):
        ReplayEngine(truth, [spec()], seed=1).run(END, START)


def test_terrain_removes_low_detections_on_the_ridge_bearing(truth: TruthSet) -> None:
    import numpy as np
    import pymap3d

    from tracking_sim.geo.terrain import SyntheticRidge

    lat, lon, _ = pymap3d.enu2geodetic(-15_000.0, 0.0, 0.0, *ORIGIN)
    ridge = SyntheticRidge(
        float(lat),
        float(lon),
        azimuth_deg=0.0,
        height_m=1500.0,
        width_m=1500.0,
        length_m=40_000.0,
        base_m=ORIGIN[2],
    )
    radar = spec(detection={"pd": 1.0, "false_alarm_density": 0.0})
    flat = ReplayEngine(truth, [radar], seed=1).run(START, END)
    hilly_engine = ReplayEngine(truth, [radar], seed=1, terrain=ridge)
    hilly = hilly_engine.run(START, END)
    gated = hilly.stats("radar-1").gated
    assert gated.get("terrain", 0) > 0 and "terrain" not in flat.stats("radar-1").gated

    def target_times(result: Any) -> set[Any]:
        t = result.detections_table()
        return set(t.filter(pc.equal(t["kind"], "target"))["timestamp"].to_pylist())

    lost = target_times(flat) - target_times(hilly)
    opp = hilly.opportunities_table()
    removed = opp.filter(pc.equal(opp["gate"], "terrain"))
    az = removed["azimuth_deg"].to_numpy()
    assert len(lost) == removed.num_rows > 0
    assert np.all((az >= 180) & (az <= 360))  # only on the ridge side
    # Only aircraft below the ridge's shadow: the height needed grows with range.
    horizon = hilly_engine.horizon(radar)
    assert horizon is not None
    need = horizon.lowest_visible_height(np.radians(az), removed["ground_range_m"].to_numpy())
    assert (removed["height_m"].to_numpy() < need).all()


def test_radar_equation_pd(tmp_path: Path) -> None:
    import numpy as np

    traffic = generate_traffic(
        seed=5,
        duration_s=600,
        receivers=(ReceiverConfig("rx", outlier_count=0, report_category=True),),
    )
    truth = build_truth(traffic.records, FRAME)
    assert {s.category for s in truth} >= {"A3"}
    radar = spec(
        detection={
            "model": "radar_equation",
            "reference_snr_db": 10.0,
            "reference_range_m": 60_000.0,
            "false_alarm_density": 0.0,
        }
    )
    result = ReplayEngine(truth, [radar], seed=11).run(START, END)
    stats = result.stats("radar-1")
    assert stats.pd_model == "radar_equation"
    lo, hi = wilson_interval(stats.detected, stats.opportunities, z=3.29)
    assert lo <= stats.configured_pd <= hi
    assert "expected (radar_equation)" in stats.summary()

    opp = result.opportunities_table()
    seen = opp.filter(opp["in_coverage"])
    cls = np.array(seen["rcs_class"].to_pylist())
    rng = seen["slant_range_m"].to_numpy()
    pd = seen["pd"].to_numpy()
    # Pd falls with range within a class, and bigger classes are easier to see.
    for name in set(cls):
        mine = cls == name
        if mine.sum() > 10:
            order = np.argsort(rng[mine])
            assert np.all(np.diff(pd[mine][order]) <= 1e-12)
    band = (rng >= 60_000) & (rng <= 80_000)
    if ((cls == "light") & band).any() and ((cls == "large") & band).any():
        assert pd[(cls == "light") & band].mean() < pd[(cls == "large") & band].mean()
    by_segment = dict(zip(seen["segment_id"].to_pylist(), cls, strict=True))
    for seg in truth:
        if seg.segment_id in by_segment:
            expected = {"A1": "light", "A2": "small", "A3": "large", "A5": "heavy"}
            assert by_segment[seg.segment_id] == expected.get(seg.category or "", "unknown")

    # A table of classes overrides the ADS-B category.
    table = {seg.icao24: "rotorcraft" for seg in truth}
    forced = ReplayEngine(truth, [radar], seed=11, aircraft_classes=table).run(START, END)
    classes = forced.opportunities_table()["rcs_class"].to_pylist()
    assert set(classes) == {"rotorcraft"}
