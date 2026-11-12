from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tracking_sim.truth import TruthRecord, TruthSource, UnsupportedModeError
from tracking_sim.truth.sources.recording import (
    RecordingSource,
    RecordingWriter,
    read_records,
    write_records,
)

T0 = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)


def sample_records() -> list[TruthRecord]:
    return [
        TruthRecord(
            icao24="abc123",
            ts=T0 + timedelta(seconds=10),
            lat=40.0,
            lon=-105.0,
            source_id="sbs:rx1",
            alt_baro_ft=12000.0,
            on_ground=False,
            nacp=9,
        ),
        TruthRecord(
            icao24="abc123",
            ts=T0 + timedelta(seconds=11, microseconds=250_000),
            lat=None,
            lon=None,
            source_id="sbs:rx1",
            gs_kt=250.5,
            track_deg=91.0,
            vrate_fpm=-640.0,
        ),
        TruthRecord(
            icao24="def456",
            ts=T0,
            lat=39.5,
            lon=-104.5,
            source_id="sbs:rx2",
            callsign="UAL1",
        ),
    ]


@pytest.mark.parametrize("suffix", [".parquet", ".jsonl"])
def test_round_trip_preserves_records_and_sorts(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"rec{suffix}"
    assert write_records(path, sample_records()) == 3
    records = read_records(path)
    assert records == sorted(sample_records(), key=lambda r: r.ts)
    assert records[1].ts.microsecond == 0
    assert records[2].ts.microsecond == 250_000


@pytest.mark.parametrize("suffix", [".parquet", ".jsonl"])
def test_window_is_half_open(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"rec{suffix}"
    write_records(path, sample_records())
    window = read_records(path, T0, T0 + timedelta(seconds=10))
    assert [r.icao24 for r in window] == ["def456"]


def test_writer_flushes_in_batches(tmp_path: Path) -> None:
    path = tmp_path / "rec.parquet"
    with RecordingWriter(path, batch_size=2) as writer:
        writer.write_many(sample_records() * 3)
    assert len(read_records(path)) == 9


def test_source_merges_a_directory_and_conforms(tmp_path: Path) -> None:
    write_records(tmp_path / "a.parquet", sample_records()[:2])
    write_records(tmp_path / "b.jsonl", sample_records()[2:])
    source = RecordingSource(tmp_path)
    assert isinstance(source, TruthSource)
    assert [r.icao24 for r in source.replay()] == ["def456", "abc123", "abc123"]
    with pytest.raises(UnsupportedModeError):
        source.stream()


def test_unknown_suffix_and_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RecordingWriter(tmp_path / "rec.csv")
    with pytest.raises(FileNotFoundError):
        read_records(tmp_path / "missing.parquet")


def test_category_round_trips_and_old_files_still_read(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    from tracking_sim.truth import TruthRecord

    ts = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
    records = [TruthRecord("abc123", ts, 39.5, -105.0, "t", category="A1", nacp=9)]
    for name in ("r.parquet", "r.jsonl"):
        write_records(tmp_path / name, records)
        (back,) = read_records(tmp_path / name)
        assert back.category == "A1" and back.nacp == 9
    # A recording written before the category column existed.
    table = pq.read_table(tmp_path / "r.parquet").drop(["category"])
    pq.write_table(table, tmp_path / "old.parquet")
    (old,) = read_records(tmp_path / "old.parquet")
    assert old.category is None and old.nacp == 9
