import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tracking_sim.cli import main, parse_time
from tracking_sim.truth.sources.recording import read_records

LINES = [
    "MSG,3,1,1,A1B2C3,1,2026/11/01,23:00:00.000,2026/11/01,23:00:00.000,,36000,,,40.1,-105.5,,,0,0,0,0",
    "MSG,4,1,1,A1B2C3,1,2026/11/01,23:00:01.000,2026/11/01,23:00:01.000,,,451,270.5,,,-64,,0,0,0,0",
    "MSG,1,1,1,A1B2C3,1,2026/11/01,23:00:02.000,2026/11/01,23:00:02.000,UAL123,,,,,,,,0,0,0,0",
]


@pytest.fixture
def sbs_port() -> Iterator[int]:
    """A fake Dump1090 that sends three lines and then keeps the socket open."""
    done = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.sendall(("\n".join(LINES) + "\n").encode())
            done.wait(10)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    done.set()
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("suffix", [".parquet", ".jsonl"])
def test_record_sbs_stops_at_max_records(tmp_path: Path, sbs_port: int, suffix: str) -> None:
    out = tmp_path / f"live{suffix}"
    host = ["--host", "127.0.0.1", "--port", str(sbs_port), "--receiver", "roof"]
    code = main(["record", "sbs", *host, "--out", str(out), "--max-records", "3"])
    assert code == 0
    records = read_records(out)
    assert [r.source_id for r in records] == ["sbs:roof"] * 3
    assert records[2].callsign == "UAL123"


def test_record_sbs_stops_at_duration(tmp_path: Path, sbs_port: int) -> None:
    out = tmp_path / "live.parquet"
    host = ["--host", "127.0.0.1", "--port", str(sbs_port)]
    main(["record", "sbs", *host, "--out", str(out), "--duration", "0.5"])
    assert len(read_records(out)) == 3


def test_record_sbs_reads_host_from_the_environment(
    tmp_path: Path, sbs_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACKING_SIM_SBS_HOST", "127.0.0.1")
    monkeypatch.setenv("TRACKING_SIM_SBS_PORT", str(sbs_port))
    out = tmp_path / "live.jsonl"
    main(["record", "sbs", "--out", str(out), "--max-records", "1"])
    assert len(read_records(out)) == 1


def test_record_sbs_without_a_host_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TRACKING_SIM_SBS_HOST", raising=False)
    with pytest.raises(SystemExit):
        main(["record", "sbs", "--out", str(tmp_path / "x.parquet")])


def test_info_summarizes_a_recording(
    fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["info", str(fixture_path), "--end", "2026-11-01T23:30:00Z"])
    out = capsys.readouterr().out
    assert "aircraft:" in out and "synthetic:rx2" in out


def test_parse_time_defaults_to_utc() -> None:
    assert parse_time("2026-11-01T23:00:00") == parse_time("2026-11-01T23:00:00Z")
    assert parse_time("2026-11-01T16:00:00-07:00") == parse_time("2026-11-01T23:00:00Z")
