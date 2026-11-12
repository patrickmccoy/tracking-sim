"""The ``recording`` adapter: TruthRecords stored as Parquet or JSONL files.

Recordings are the repeatable test fixture and the format the ``record`` CLI
writes, so anyone with a receiver can build their own history without a
database. A recording keeps each record's original ``source_id``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from tracking_sim.truth.record import TRUTH_RECORD_FIELDS, TruthRecord, ensure_utc
from tracking_sim.truth.source import UnsupportedModeError

__all__ = [
    "RECORDING_SCHEMA",
    "RecordingFormat",
    "RecordingSource",
    "RecordingWriter",
    "read_records",
    "records_to_table",
    "table_to_records",
    "write_records",
]

RecordingFormat = Literal["parquet", "jsonl"]

RECORDING_SCHEMA = pa.schema(
    [
        pa.field("icao24", pa.string(), nullable=False),
        pa.field("ts", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("lat", pa.float64()),
        pa.field("lon", pa.float64()),
        pa.field("source_id", pa.string(), nullable=False),
        pa.field("alt_baro_ft", pa.float64()),
        pa.field("alt_geom_ft", pa.float64()),
        pa.field("alt_unknown_ft", pa.float64()),
        pa.field("gs_kt", pa.float64()),
        pa.field("track_deg", pa.float64()),
        pa.field("vrate_fpm", pa.float64()),
        pa.field("callsign", pa.string()),
        pa.field("on_ground", pa.bool_()),
        pa.field("nic", pa.int16()),
        pa.field("nacp", pa.int16()),
        pa.field("category", pa.string()),
    ]
)
assert tuple(RECORDING_SCHEMA.names) == TRUTH_RECORD_FIELDS

_FLOAT_FIELDS = ("alt_baro_ft", "alt_geom_ft", "alt_unknown_ft", "gs_kt", "track_deg", "vrate_fpm")

_SUFFIXES: dict[str, RecordingFormat] = {
    ".parquet": "parquet",
    ".pq": "parquet",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
}


def _format_for(path: Path) -> RecordingFormat:
    try:
        return _SUFFIXES[path.suffix.lower()]
    except KeyError:
        raise ValueError(
            f"cannot tell the recording format of {path}; use one of {sorted(_SUFFIXES)}"
        ) from None


def records_to_table(records: Iterable[TruthRecord]) -> pa.Table:
    """Convert records to an Arrow table with ``RECORDING_SCHEMA``."""
    columns: dict[str, list[Any]] = {name: [] for name in TRUTH_RECORD_FIELDS}
    for record in records:
        for name in TRUTH_RECORD_FIELDS:
            columns[name].append(getattr(record, name))
    return pa.Table.from_pydict(columns, schema=RECORDING_SCHEMA)


def table_to_records(table: pa.Table) -> Iterator[TruthRecord]:
    """Yield records from an Arrow table whose columns are canonical fields."""
    names = [n for n in table.column_names if n in TRUTH_RECORD_FIELDS]
    for batch in table.select(names).to_batches():
        for row in batch.to_pylist():
            yield TruthRecord(**row)


def _record_to_json(record: TruthRecord) -> str:
    data = record.to_dict()
    data["ts"] = record.ts.isoformat().replace("+00:00", "Z")
    return json.dumps(data, separators=(",", ":"))


def _record_from_json(line: str) -> TruthRecord:
    data = json.loads(line)
    ts = data.get("ts")
    if isinstance(ts, str):
        data["ts"] = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    for name in _FLOAT_FIELDS:
        if isinstance(data.get(name), int):
            data[name] = float(data[name])
    return TruthRecord.from_dict(data)


class RecordingWriter:
    """Write TruthRecords to one Parquet or JSONL file.

    Records are buffered and flushed every ``batch_size`` records, on
    ``flush()`` and on ``close()``. Use it as a context manager so the Parquet
    footer is always written.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        format: RecordingFormat | None = None,
        batch_size: int = 5000,
    ) -> None:
        self.path = Path(path)
        self.format: RecordingFormat = format or _format_for(self.path)
        self.batch_size = batch_size
        self.count = 0
        self._buffer: list[TruthRecord] = []
        self._parquet: pq.ParquetWriter | None = None
        self._jsonl: Any = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.format == "parquet":
            self._parquet = pq.ParquetWriter(self.path, RECORDING_SCHEMA, compression="zstd")
        else:
            self._jsonl = self.path.open("w", encoding="utf-8")

    def write(self, record: TruthRecord) -> None:
        self._buffer.append(record)
        self.count += 1
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def write_many(self, records: Iterable[TruthRecord]) -> None:
        for record in records:
            self.write(record)

    def flush(self) -> None:
        if not self._buffer:
            return
        if self._parquet is not None:
            self._parquet.write_table(records_to_table(self._buffer))
        elif self._jsonl is not None:
            self._jsonl.write("".join(_record_to_json(r) + "\n" for r in self._buffer))
            self._jsonl.flush()
        self._buffer.clear()

    def close(self) -> None:
        self.flush()
        if self._parquet is not None:
            self._parquet.close()
            self._parquet = None
        if self._jsonl is not None:
            self._jsonl.close()
            self._jsonl = None

    def __enter__(self) -> RecordingWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def write_records(
    path: str | Path, records: Iterable[TruthRecord], *, format: RecordingFormat | None = None
) -> int:
    """Write records to ``path`` and return how many were written."""
    with RecordingWriter(path, format=format) as writer:
        writer.write_many(records)
        return writer.count


def _expand(paths: str | Path | Sequence[str | Path]) -> list[Path]:
    items = [paths] if isinstance(paths, str | Path) else list(paths)
    files: list[Path] = []
    for item in items:
        path = Path(item)
        if path.is_dir():
            files.extend(
                sorted(p for p in path.iterdir() if p.suffix.lower() in _SUFFIXES and p.is_file())
            )
        elif any(ch in str(item) for ch in "*?["):
            files.extend(sorted(Path().glob(str(item))))
        elif path.exists():
            files.append(path)
        else:
            raise FileNotFoundError(path)
    if not files:
        raise FileNotFoundError(f"no recording files found in {paths}")
    return files


def _read_file(path: Path, start: datetime | None, end: datetime | None) -> list[TruthRecord]:
    if _format_for(path) == "parquet":
        dataset = ds.dataset(path, format="parquet")
        expr = None
        if start is not None:
            expr = ds.field("ts") >= pa.scalar(start, pa.timestamp("us", tz="UTC"))
        if end is not None:
            upper = ds.field("ts") < pa.scalar(end, pa.timestamp("us", tz="UTC"))
            expr = upper if expr is None else expr & upper
        return list(table_to_records(dataset.to_table(filter=expr)))
    records = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            record = _record_from_json(line)
            if (start is None or record.ts >= start) and (end is None or record.ts < end):
                records.append(record)
    return records


def read_records(
    paths: str | Path | Sequence[str | Path],
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[TruthRecord]:
    """Read records from files, a directory or a glob, sorted by time."""
    start = ensure_utc(start) if start is not None else None
    end = ensure_utc(end) if end is not None else None
    records: list[TruthRecord] = []
    for path in _expand(paths):
        records.extend(_read_file(path, start, end))
    records.sort(key=lambda r: r.ts)
    return records


class RecordingSource:
    """Replay TruthRecords from recording files.

    ``paths`` may be a file, a directory of recordings, a glob pattern, or a
    list of any of those. Records from several files are merged in time order.
    """

    supports_replay = True
    supports_stream = False

    def __init__(
        self, paths: str | Path | Sequence[str | Path], *, source_id: str | None = None
    ) -> None:
        self.paths = paths
        if source_id is None:
            first = paths if isinstance(paths, str | Path) else (paths[0] if paths else "")
            source_id = f"recording:{Path(first).name}"
        self.source_id = source_id

    def replay(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[TruthRecord]:
        yield from read_records(self.paths, start, end)

    def stream(self) -> AsyncIterator[TruthRecord]:
        raise UnsupportedModeError("recordings support replay only")
