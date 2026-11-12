import asyncio
import gzip
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tracking_sim.cli import main
from tracking_sim.truth import TruthSource, UnsupportedModeError
from tracking_sim.truth.sources.aircraft_json import AircraftJsonSource, SnapshotParser
from tracking_sim.truth.sources.recording import read_records

NOW = datetime(2026, 11, 1, 23, 0, tzinfo=UTC).timestamp()


def snapshot(now: float, **override: Any) -> dict[str, Any]:
    airliner = {
        "hex": "A1B2C3",
        "flight": "UAL123  ",
        "category": "A3",
        "alt_baro": 35000,
        "alt_geom": 35450,
        "gs": 452.1,
        "track": 271.3,
        "baro_rate": -64,
        "lat": 39.91,
        "lon": -105.12,
        "nic": 8,
        "nac_p": 9,
        "seen_pos": 0.4,
        "seen": 0.1,
    }
    airliner.update(override)
    taxiing = {
        "hex": "abcdef",
        "alt_baro": "ground",
        "lat": 39.909,
        "lon": -105.117,
        "seen_pos": 1.0,
        "seen": 1.0,
        "category": "A1",
    }
    old_format = {
        "hex": "c0ffee",
        "altitude": 12000,
        "speed": 210,
        "track": 90,
        "vert_rate": 512,
        "lat": 40.0,
        "lon": -105.0,
        "seen_pos": 2.0,
        "seen": 0.5,
    }
    tisb = {"hex": "~123456", "alt_baro": 5000, "lat": 40.1, "lon": -105.2, "seen_pos": 0.1}
    return {"now": now, "aircraft": [airliner, taxiing, old_format, tisb]}


def test_parse_a_snapshot() -> None:
    records = SnapshotParser(source_id="aircraft_json:t").parse(snapshot(NOW))
    by = {(r.icao24, r.has_position): r for r in records}
    pos = by[("a1b2c3", True)]
    assert pos.ts.timestamp() == pytest.approx(NOW - 0.4)
    assert (pos.alt_baro_ft, pos.alt_geom_ft, pos.nic, pos.nacp) == (35000, 35450, 8, 9)
    assert pos.category == "A3" and pos.callsign == "UAL123" and pos.on_ground is False
    vel = by[("a1b2c3", False)]
    assert vel.ts.timestamp() == pytest.approx(NOW - 0.1)
    assert (vel.gs_kt, vel.track_deg, vel.vrate_fpm) == (452.1, 271.3, -64)
    ground = by[("abcdef", True)]
    assert ground.on_ground is True and ground.altitude_ft is None
    old = by[("c0ffee", True)]
    assert old.alt_baro_ft == 12000 and by[("c0ffee", False)].gs_kt == 210
    assert by[("c0ffee", False)].vrate_fpm == 512
    assert not any(r.icao24 == "123456" for r in records)  # non-ICAO skipped
    with_tisb = SnapshotParser(include_non_icao=True).parse(snapshot(NOW))
    assert any(r.icao24 == "123456" for r in with_tisb)
    assert [r.ts for r in records] == sorted(r.ts for r in records)


def test_repeated_reports_are_dropped() -> None:
    parser = SnapshotParser()
    first = parser.parse(snapshot(NOW))
    # One second later: the airliner has a new position, everyone else is unchanged.
    again = parser.parse(snapshot(NOW + 1.0, lat=39.912, seen_pos=0.2))
    assert len(first) > len(again)
    assert {(r.icao24, r.has_position) for r in again} == {("a1b2c3", True)}
    assert again[0].ts.timestamp() == pytest.approx(NOW + 0.8)


def test_replay_from_saved_snapshots(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    for i in range(3):
        (tmp_path / "dir" / f"aircraft-{i}.json").write_text(
            json.dumps(snapshot(NOW + i, lat=39.91 + i * 0.002))
        )
    lines = "\n".join(json.dumps(snapshot(NOW + i, lat=39.91 + i * 0.002)) for i in range(3))
    (tmp_path / "polls.jsonl").write_text(lines)
    with gzip.open(tmp_path / "polls.jsonl.gz", "wt") as fh:
        fh.write(lines)
    results = []
    for path in (tmp_path / "dir", tmp_path / "polls.jsonl", tmp_path / "polls.jsonl.gz"):
        source = AircraftJsonSource(path=path, receiver="roof")
        assert isinstance(source, TruthSource)
        records = list(source.replay())
        assert {r.source_id for r in records} == {"aircraft_json:roof"}
        results.append([(r.icao24, r.ts, r.lat) for r in records])
    assert results[0] == results[1] == results[2]
    positions = [
        r
        for r in AircraftJsonSource(path=tmp_path / "dir").replay()
        if r.icao24 == "a1b2c3" and r.has_position
    ]
    assert len(positions) == 3
    window = list(
        AircraftJsonSource(path=tmp_path / "dir").replay(
            datetime.fromtimestamp(NOW + 0.5, UTC), datetime.fromtimestamp(NOW + 1.7, UTC)
        )
    )
    assert window and all(NOW + 0.5 <= r.ts.timestamp() < NOW + 1.7 for r in window)
    with pytest.raises(UnsupportedModeError):
        AircraftJsonSource(url="http://x/aircraft.json").replay().__next__()


def test_live_polling(tmp_path: Path) -> None:
    polls = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            polls["n"] += 1
            body = json.dumps(snapshot(NOW + polls["n"], lat=39.91 + polls["n"] * 0.001)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/data/aircraft.json"
    try:
        source = AircraftJsonSource(url=url, receiver="live", poll_interval_s=0.01)

        async def take(n: int) -> list[Any]:
            out = []
            async for record in source.stream():
                if record.icao24 == "a1b2c3" and record.has_position:
                    out.append(record)
                if len(out) == n:
                    return out
            return out

        records = asyncio.run(asyncio.wait_for(take(3), timeout=10))
        assert len({r.lat for r in records}) == 3

        out = tmp_path / "live.parquet"
        assert (
            main(
                [
                    "record",
                    "aircraft-json",
                    "--url",
                    url,
                    "--interval",
                    "0.01",
                    "--max-records",
                    "10",
                    "-o",
                    str(out),
                ]
            )
            == 0
        )
        assert len(read_records(out)) == 10
    finally:
        server.shutdown()


def test_export_command(tmp_path: Path) -> None:
    lines = "\n".join(json.dumps(snapshot(NOW + i, lat=39.91 + i * 0.002)) for i in range(3))
    (tmp_path / "polls.jsonl").write_text(lines)
    out = tmp_path / "out.parquet"
    assert (
        main(
            [
                "export",
                "aircraft-json",
                str(tmp_path / "polls.jsonl"),
                "--receiver",
                "roof",
                "-o",
                str(out),
            ]
        )
        == 0
    )
    records = read_records(out)
    assert any(r.category == "A3" and r.nacp == 9 for r in records)
