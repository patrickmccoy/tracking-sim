from datetime import UTC, datetime, timedelta, timezone

import pytest

from tracking_sim.truth import TruthRecord


def make(**kw: object) -> TruthRecord:
    base: dict[str, object] = {
        "icao24": "A1B2C3",
        "ts": datetime(2026, 11, 1, 23, 0, tzinfo=UTC),
        "lat": 40.0,
        "lon": -105.0,
        "source_id": "test",
    }
    base.update(kw)
    return TruthRecord(**base)  # type: ignore[arg-type]


def test_icao_is_lower_cased_and_stripped() -> None:
    assert make(icao24=" A1B2C3 ").icao24 == "a1b2c3"


def test_naive_timestamp_is_utc_and_aware_is_converted() -> None:
    naive = make(ts=datetime(2026, 11, 1, 23, 0))
    assert naive.ts.tzinfo is UTC
    mountain = timezone(timedelta(hours=-7))
    local = make(ts=datetime(2026, 11, 1, 16, 0, tzinfo=mountain))
    assert local.ts == naive.ts
    assert local.ts.tzinfo is UTC


def test_position_must_be_paired_and_in_range() -> None:
    with pytest.raises(ValueError):
        make(lat=None)
    with pytest.raises(ValueError):
        make(lat=91.0)
    velocity_only = make(lat=None, lon=None, gs_kt=250.0, track_deg=90.0)
    assert not velocity_only.has_position
    assert velocity_only.has_velocity


def test_empty_callsign_becomes_none_and_nan_becomes_none() -> None:
    record = make(callsign="   ", alt_baro_ft=float("nan"))
    assert record.callsign is None
    assert record.alt_baro_ft is None


def test_altitude_preference() -> None:
    assert make(alt_unknown_ft=1.0).altitude_ft == 1.0
    assert make(alt_unknown_ft=1.0, alt_baro_ft=2.0).altitude_ft == 2.0
    assert make(alt_unknown_ft=1.0, alt_baro_ft=2.0, alt_geom_ft=3.0).altitude_ft == 3.0
    assert make().altitude_ft is None


def test_dict_round_trip() -> None:
    record = make(alt_baro_ft=35000.0, callsign="UAL123", on_ground=False, nacp=9)
    data = record.to_dict()
    assert record == TruthRecord.from_dict(data)
    data["ts"] = data["ts"].isoformat()
    data["unexpected"] = "ignored"
    assert record == TruthRecord.from_dict(data)
