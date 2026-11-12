"""Truth source adapters. Each module implements ``TruthSource``."""

from __future__ import annotations

from datetime import tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracking_sim.truth.sources.recording import RecordingSource
    from tracking_sim.truth.sources.sbs import SbsSource

__all__ = ["is_sbs_capture", "open_recording"]


def is_sbs_capture(path: str | Path) -> bool:
    """True for captured SBS text (``.sbs``, ``.sbs.gz``), false for a recording."""
    return ".sbs" in Path(path).suffixes


def open_recording(
    path: str | Path, *, receiver: str = "recording", tz: str | tzinfo = "UTC"
) -> RecordingSource | SbsSource:
    """Replay a file of truth: a tracking-sim recording or a captured SBS feed.

    Recordings are what ``tracking-sim record`` and ``export`` write
    (``.parquet``, ``.jsonl``, or a directory of them). SBS captures are
    Dump1090's BaseStation text; ``tz`` is the zone of their timestamps
    (Dump1090 writes the host's local time) and ``receiver`` names them.
    """
    if is_sbs_capture(path):
        from tracking_sim.truth.sources.sbs import SbsSource

        return SbsSource(path=path, receiver=receiver, tz=tz)
    from tracking_sim.truth.sources.recording import RecordingSource

    return RecordingSource(path)
