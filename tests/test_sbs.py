import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tracking_sim.truth import TruthSource, UnsupportedModeError
from tracking_sim.truth.sources.recording import read_records
from tracking_sim.truth.sources.sbs import SbsSource, format_sbs_line, parse_sbs_line

# Lines in the shape dump1090-fa writes on port 30003.
POSITION = "MSG,3,1,1,A1B2C3,1,2026/11/01,16:00:00.250,2026/11/01,16:00:00.300,,36000,,,40.12345,-105.54321,,,0,0,0,0"  # noqa: E501
VELOCITY = (
    "MSG,4,1,1,A1B2C3,1,2026/11/01,16:00:01.000,2026/11/01,16:00:01.000,,,451,270.5,,,-64,,0,0,0,0"
)
IDENT = "MSG,1,1,1,A1B2C3,1,2026/11/01,16:00:02.000,2026/11/01,16:00:02.000,UAL123  ,,,,,,,,0,0,0,0"
SURFACE = (
    "MSG,2,1,1,A1B2C3,1,2026/11/01,16:00:03.000,2026/11/01,16:00:03.000,,,12,90,40.0,-105.0,,,,,,-1"
)
SQUAWK = "MSG,6,1,1,A1B2C3,1,2026/11/01,16:00:04.000,2026/11/01,16:00:04.000,,,,,,,,1200,0,0,0,0"
DENVER = ZoneInfo("America/Denver")


def test_airborne_position() -> None:
    record = parse_sbs_line(POSITION, source_id="sbs:rx", tz=DENVER)
    assert record is not None
    assert record.icao24 == "a1b2c3"
    assert record.ts == datetime(2026, 11, 1, 23, 0, 0, 250_000, tzinfo=UTC)
    assert (record.lat, record.lon, record.alt_baro_ft) == (40.12345, -105.54321, 36000.0)
    assert record.on_ground is False
    assert record.gs_kt is None


def test_logged_time_and_altitude_field_are_configurable() -> None:
    record = parse_sbs_line(POSITION, time_field="logged", altitude_field="alt_unknown_ft")
    assert record is not None
    assert record.ts == datetime(2026, 11, 1, 16, 0, 0, 300_000, tzinfo=UTC)
    assert record.alt_unknown_ft == 36000.0 and record.alt_baro_ft is None


def test_velocity_ident_and_surface() -> None:
    velocity = parse_sbs_line(VELOCITY)
    assert velocity is not None and not velocity.has_position
    assert (velocity.gs_kt, velocity.track_deg, velocity.vrate_fpm) == (451.0, 270.5, -64.0)
    ident = parse_sbs_line(IDENT)
    assert ident is not None and ident.callsign == "UAL123"
    surface = parse_sbs_line(SURFACE)
    assert surface is not None and surface.on_ground is True and surface.has_position


@pytest.mark.parametrize(
    "line",
    [
        SQUAWK,
        "",
        "STA,,5,179,400AE7,10103,2008/11/28,14:58:51.153,2008/11/28,14:58:51.153,RM",
        "MSG,3,1,1,,1,2026/11/01,16:00:00.250,,,,36000,,,40.1,-105.5,,,0,0,0,0",
        "MSG,3,1,1,A1B2C3,1,garbage,16:00:00.250,,,,36000,,,40.1,-105.5,,,0,0,0,0",
        "MSG,3,1,1",
    ],
)
def test_lines_without_usable_content_are_skipped(line: str) -> None:
    assert parse_sbs_line(line) is None


def test_arrival_time_overrides_message_time() -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    record = parse_sbs_line(POSITION, arrival_time=now)
    assert record is not None and record.ts == now


def test_format_round_trips_to_the_millisecond() -> None:
    for line in (POSITION, VELOCITY, IDENT, SURFACE):
        record = parse_sbs_line(line, tz=DENVER)
        assert record is not None
        again = parse_sbs_line(format_sbs_line(record, tz=DENVER), tz=DENVER)
        assert again is not None
        assert again.to_dict() == record.to_dict()


def test_replay_of_the_fixture_matches_the_recording(tmp_path: Path, fixture_path: Path) -> None:
    source = SbsSource(path=fixture_path.with_name("synthetic_5min.sbs"), tz="America/Denver")
    assert isinstance(source, TruthSource)
    assert source.supports_replay and not source.supports_stream
    records = list(source.replay())
    start = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
    expected = [
        r
        for r in read_records(fixture_path, start, start + timedelta(minutes=5))
        if r.source_id == "synthetic:rx1"
    ]
    assert len(records) == len(expected)
    for got, want in zip(records, expected, strict=True):
        assert got.icao24 == want.icao24
        assert abs((got.ts - want.ts).total_seconds()) < 0.001
        assert got.lat == want.lat and got.alt_baro_ft == want.alt_baro_ft
    with pytest.raises(UnsupportedModeError):
        source.stream()


def test_stream_reads_lines_from_a_tcp_feed() -> None:
    async def scenario() -> list[str]:
        async def serve(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(("\r\n".join([POSITION, SQUAWK, VELOCITY, IDENT]) + "\r\n").encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        source = SbsSource(host="127.0.0.1", port=port, max_reconnects=0, reconnect_delay_s=0)
        async with server:
            return [r.icao24 + str(r.has_position) async for r in source.stream()]

    assert asyncio.run(scenario()) == ["a1b2c3True", "a1b2c3False", "a1b2c3False"]


def test_needs_a_host_or_a_path() -> None:
    with pytest.raises(ValueError):
        SbsSource()
