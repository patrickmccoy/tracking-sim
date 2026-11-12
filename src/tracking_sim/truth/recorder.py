"""Pipe truth sources into recording files.

``record_stream`` copies a live source into a ``RecordingWriter``;
``export_replay`` does the same for a replay window. Both are what the
``tracking-sim record`` and ``tracking-sim export`` commands call.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from tracking_sim.truth.record import TruthRecord
from tracking_sim.truth.source import TruthSource
from tracking_sim.truth.sources.recording import RecordingWriter

__all__ = ["RecordingSummary", "export_replay", "record_stream", "summarize"]


async def record_stream(
    source: TruthSource,
    writer: RecordingWriter,
    *,
    duration_s: float | None = None,
    max_records: int | None = None,
    flush_every_s: float = 10.0,
) -> int:
    """Write records from ``source.stream()`` until a limit is reached.

    Stops after ``duration_s`` seconds or ``max_records`` records, whichever
    comes first, or runs until cancelled if neither is given. The writer is
    flushed every ``flush_every_s`` seconds; the caller owns closing it.
    Returns the number of records written by this call.
    """
    written = 0
    last_flush = time.monotonic()

    async def consume() -> None:
        nonlocal written, last_flush
        async for record in source.stream():
            writer.write(record)
            written += 1
            if max_records is not None and written >= max_records:
                return
            now = time.monotonic()
            if now - last_flush >= flush_every_s:
                writer.flush()
                last_flush = now

    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(duration_s):
            await consume()
    writer.flush()
    return written


def export_replay(
    source: TruthSource,
    writer: RecordingWriter,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Write every record ``source.replay(start, end)`` yields. Returns the count."""
    before = writer.count
    writer.write_many(source.replay(start, end))
    writer.flush()
    return writer.count - before


@dataclass
class RecordingSummary:
    records: int = 0
    first: datetime | None = None
    last: datetime | None = None
    aircraft: set[str] = field(default_factory=set)
    by_source: Counter[str] = field(default_factory=Counter)
    fields: Counter[str] = field(default_factory=Counter)

    def lines(self) -> list[str]:
        span = (self.last - self.first) if self.first and self.last else None
        out = [
            f"records:   {self.records}",
            f"aircraft:  {len(self.aircraft)}",
            f"first:     {self.first.isoformat() if self.first else '-'}",
            f"last:      {self.last.isoformat() if self.last else '-'}",
            f"span:      {span if span is not None else '-'}",
            "sources:",
        ]
        out += [f"  {name}: {count}" for name, count in sorted(self.by_source.items())]
        out.append("fields present (records with a value):")
        out += [f"  {name}: {count}" for name, count in sorted(self.fields.items())]
        return out


_SUMMARY_FIELDS = (
    "lat",
    "alt_baro_ft",
    "alt_geom_ft",
    "alt_unknown_ft",
    "gs_kt",
    "track_deg",
    "vrate_fpm",
    "callsign",
    "on_ground",
    "nic",
    "nacp",
)


def summarize(records: Iterable[TruthRecord]) -> RecordingSummary:
    summary = RecordingSummary()
    for record in records:
        summary.records += 1
        if summary.first is None or record.ts < summary.first:
            summary.first = record.ts
        if summary.last is None or record.ts > summary.last:
            summary.last = record.ts
        summary.aircraft.add(record.icao24)
        summary.by_source[record.source_id] += 1
        for name in _SUMMARY_FIELDS:
            if getattr(record, name) is not None:
                summary.fields["position" if name == "lat" else name] += 1
    return summary
