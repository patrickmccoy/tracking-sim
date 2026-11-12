from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from tracking_sim.truth.sources.recording import read_records
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic


def test_same_seed_same_traffic() -> None:
    a = generate_traffic(seed=7, duration_s=600)
    b = generate_traffic(seed=7, duration_s=600)
    assert a.records == b.records
    assert a.records != generate_traffic(seed=8, duration_s=600).records


def test_records_have_the_defects_the_builder_must_handle() -> None:
    traffic = generate_traffic(seed=1)
    records = traffic.records
    assert records == sorted(records, key=lambda r: r.ts)
    assert {r.source_id for r in records} == {"synthetic:rx1", "synthetic:rx2"}
    assert any(r.has_position for r in records)
    assert any(r.has_velocity and not r.has_position for r in records)
    assert any(r.callsign and not r.has_position for r in records)
    assert any(r.on_ground for r in records)
    assert len(traffic.outliers) == 3
    ((icao, (g0, g1)),) = traffic.gaps.items()
    epoch = traffic.epoch
    in_gap = [
        r for r in records if r.icao24 == icao and g0 + 1 < (r.ts - epoch).total_seconds() < g1 - 1
    ]
    assert in_gap == []


def test_duplicates_repeat_values_with_a_later_timestamp() -> None:
    traffic = generate_traffic(seed=1, duration_s=600)
    sent: dict[tuple[object, ...], list[datetime]] = defaultdict(list)
    for r in traffic.records:
        if r.source_id == "synthetic:rx1":
            sent[(r.icao24, r.lat, r.lon, r.gs_kt, r.callsign)].append(r.ts)
    dupes = [r for r in traffic.records if r.source_id == "synthetic:rx2"]
    assert dupes
    for dupe in dupes:
        times = sent[(dupe.icao24, dupe.lat, dupe.lon, dupe.gs_kt, dupe.callsign)]
        assert any(0 <= (dupe.ts - ts).total_seconds() <= 0.05 for ts in times)


def test_independent_receiver_can_fill_another_altitude_field() -> None:
    traffic = generate_traffic(
        seed=1,
        duration_s=300,
        receivers=(ReceiverConfig("x", altitude_field="alt_unknown_ft"),),
    )
    positions = [r for r in traffic.records if r.has_position and not r.on_ground]
    assert positions
    assert all(r.alt_unknown_ft is not None and r.alt_baro_ft is None for r in positions)


def test_positions_are_near_the_true_trajectory() -> None:
    traffic = generate_traffic(seed=1, duration_s=900, receivers=(ReceiverConfig("rx"),))
    for flight in traffic.flights:
        positions = [r for r in traffic.records if r.icao24 == flight.icao24 and r.has_position]
        t = np.array([(r.ts - traffic.epoch).total_seconds() for r in positions])
        truth = flight.sample(t)
        dlat = np.array([r.lat for r in positions]) - truth["lat"]
        assert np.max(np.abs(dlat)) * 111_000 < 100


def test_committed_fixture_matches_the_generator(fixture_path: Path) -> None:
    # Compare with a tolerance: libm differences between platforms can move
    # the last digit of a converted coordinate.
    committed = read_records(fixture_path)
    generated = generate_traffic(seed=1).records
    assert len(committed) == len(generated)
    for c, g in zip(committed, generated, strict=True):
        assert (c.icao24, c.ts, c.source_id, c.gs_kt, c.callsign) == (
            g.icao24,
            g.ts,
            g.source_id,
            g.gs_kt,
            g.callsign,
        )
        if c.lat is not None:
            assert g.lat is not None and g.lon is not None and c.lon is not None
            assert abs(c.lat - g.lat) < 2e-6 and abs(c.lon - g.lon) < 2e-6
