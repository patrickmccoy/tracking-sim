"""Uploaded captures: truth files added from the browser.

An upload is a tracking-sim recording (``.parquet``, ``.jsonl``) or a
captured SBS feed (``.sbs``, ``.sbs.gz``, with the zone of its
timestamps). Either way it is read once, checked, and stored as a Parquet
recording named by its content, so a scenario's ``truth.path`` can point at
it like any recording and the same upload twice is one capture. Beside each
recording is a JSON summary (records, aircraft, time span, extent) and a
sample of positions for the map.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np

from tracking_sim.truth.record import TruthRecord
from tracking_sim.truth.sources import is_sbs_capture, open_recording
from tracking_sim.truth.sources.recording import write_records

__all__ = ["CAPTURE_SUFFIXES", "Capture", "CaptureError", "CaptureStore"]

# What an upload's file name may end in (``.sbs.gz`` counts as ``.sbs``).
CAPTURE_SUFFIXES = (".parquet", ".pq", ".jsonl", ".ndjson", ".sbs", ".sbs.gz")

# Positions kept for the map's picture of where the traffic is.
PREVIEW_POINTS = 5000


class CaptureError(ValueError):
    """An upload that can't be used, with a message for the user."""


@dataclass(frozen=True)
class Capture:
    id: str
    name: str  # the uploaded file's name
    path: str  # the stored recording, absolute
    uploaded: str
    kind: str  # "sbs" or "recording": what was uploaded
    tz: str | None  # SBS only
    records: int
    aircraft: int
    positions: int
    first: str
    last: str
    bbox: tuple[float, float, float, float]  # west, south, east, north


def _slug(name: str) -> str:
    stem = name
    for suffix in sorted(CAPTURE_SUFFIXES, key=len, reverse=True):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:40] or "capture"


def check_upload(filename: str, tz: str) -> None:
    """Raise ``CaptureError`` for a name or time zone an upload can't have."""
    if not filename.lower().endswith(CAPTURE_SUFFIXES):
        raise CaptureError(f"upload a recording or SBS capture: {', '.join(CAPTURE_SUFFIXES)}")
    if is_sbs_capture(filename):
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise CaptureError(f"unknown time zone {tz!r}") from None


class CaptureStore:
    """Captures in one directory: ``<id>.parquet``, ``<id>.json``, ``<id>.preview.json``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def incoming(self) -> Path:
        """Where uploads are written while they arrive."""
        path = self.root / ".incoming"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def capture_id(self, filename: str, digest: str, tz: str) -> str:
        """The id an upload gets: its name, and a hash of its bytes (and zone, for SBS)."""
        key = f"{digest}:{tz}" if is_sbs_capture(filename) else digest
        return f"{_slug(filename)}-{hashlib.sha256(key.encode()).hexdigest()[:10]}"

    def add(self, upload: Path, filename: str, digest: str, *, tz: str = "UTC") -> Capture:
        """Read ``upload`` (named ``filename`` by the user) and store it.

        ``digest`` is the SHA-256 of its bytes. Returns the stored capture,
        which may be one uploaded before. ``upload`` is removed either way.
        """
        try:
            check_upload(filename, tz)
            capture_id = self.capture_id(filename, digest, tz)
            existing = self.get(capture_id)
            if existing is not None:
                return existing
            # The readers go by suffix, so give the upload its real one.
            suffix = ".sbs.gz" if filename.lower().endswith(".sbs.gz") else Path(filename).suffix
            named = upload.with_name(upload.name + suffix.lower())
            upload.rename(named)
            upload = named
            records = self._read(upload, filename, tz)
            recording = self.root / f"{capture_id}.parquet"
            partial = recording.with_suffix(".parquet.partial")
            write_records(partial, records, format="parquet")
            partial.replace(recording)
            capture, preview = self._summarize(capture_id, filename, recording, records, tz)
            self._write_json(self.root / f"{capture_id}.preview.json", preview)
            self._write_json(self.root / f"{capture_id}.json", asdict(capture))
            return capture
        finally:
            upload.unlink(missing_ok=True)

    def _read(self, upload: Path, filename: str, tz: str) -> list[TruthRecord]:
        try:
            source = open_recording(upload, receiver="upload", tz=tz)
            records = sorted(source.replay(None, None), key=lambda r: r.ts)
        except Exception as exc:  # any reader failure is a bad upload
            raise CaptureError(f"can't read {filename}: {exc}") from None
        if not records:
            raise CaptureError(f"{filename} has no records")
        if not any(r.lat is not None and r.lon is not None for r in records):
            raise CaptureError(f"{filename} has no aircraft positions")
        return records

    def _summarize(
        self, capture_id: str, filename: str, recording: Path, records: list[TruthRecord], tz: str
    ) -> tuple[Capture, dict[str, Any]]:
        placed = [r for r in records if r.lat is not None and r.lon is not None]
        lat = np.array([r.lat for r in placed], dtype=float)
        lon = np.array([r.lon for r in placed], dtype=float)
        sbs = is_sbs_capture(filename)
        capture = Capture(
            id=capture_id,
            name=filename,
            path=str(recording.resolve()),
            uploaded=datetime.now(UTC).isoformat(timespec="seconds"),
            kind="sbs" if sbs else "recording",
            tz=tz if sbs else None,
            records=len(records),
            aircraft=len({r.icao24 for r in records}),
            positions=len(placed),
            first=records[0].ts.isoformat(),
            last=records[-1].ts.isoformat(),
            # The 1st and 99th percentiles: one bad position shouldn't stretch it.
            bbox=(
                round(float(np.percentile(lon, 1)), 4),
                round(float(np.percentile(lat, 1)), 4),
                round(float(np.percentile(lon, 99)), 4),
                round(float(np.percentile(lat, 99)), 4),
            ),
        )
        step = max(1, len(placed) // PREVIEW_POINTS)
        preview = {
            "lat": [round(float(v), 4) for v in lat[::step]],
            "lon": [round(float(v), 4) for v in lon[::step]],
        }
        return capture, preview

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        partial = path.with_suffix(".partial")
        partial.write_text(json.dumps(data))
        partial.replace(path)

    def get(self, capture_id: str) -> Capture | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", capture_id):
            return None
        meta = self.root / f"{capture_id}.json"
        if not meta.is_file() or not (self.root / f"{capture_id}.parquet").is_file():
            return None
        data = json.loads(meta.read_text())
        return Capture(**{**data, "bbox": tuple(data["bbox"])})

    def preview(self, capture_id: str) -> dict[str, Any] | None:
        if self.get(capture_id) is None:
            return None
        data: dict[str, Any] = json.loads((self.root / f"{capture_id}.preview.json").read_text())
        return data

    def list(self) -> list[Capture]:
        """Every capture, newest upload first."""
        if not self.root.is_dir():
            return []
        found = (self.get(p.stem) for p in self.root.glob("*.json") if "." not in p.stem)
        return sorted((c for c in found if c), key=lambda c: c.uploaded, reverse=True)
