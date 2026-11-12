"""Run trackers on simulated detections.

``detection_batches`` turns the replay engine's scans into what a Stone
Soup tracker reads: ``(time, detections)`` batches, one per scan, ordered by
when the scan's detections would reach a tracker (scan end plus the
sensor's latency). Within a batch each detection keeps its own measurement
time, and the hypothesiser predicts every track to it.

``run_tracker`` builds a tracker from its spec, feeds it the batches and
collects every track it ever held (Stone Soup drops deleted tracks from its
set; scoring needs them all). Each track records when it was confirmed:
the states before that belong to the tentative track the initiator grew
it from, which a real tracker would not report, so scoring uses only the
confirmed part (``TrackerRun.reported_tracks``).

Stone Soup gives tracks random UUIDs; here they are labelled
``<tracker id>:0001`` and so on in order of confirmation, so two runs with
one seed produce identical tables.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from stonesoup.types.detection import Clutter, Detection, TrueDetection
from stonesoup.types.track import Track
from stonesoup.types.update import Update

from tracking_sim.sensors.radar import ScanResult
from tracking_sim.tracking.factory import build_tracker
from tracking_sim.tracking.spec import TrackerSpec

__all__ = ["DetectionBatch", "TrackerRun", "detection_batches", "run_tracker"]

DetectionBatch = tuple[datetime, set[Detection]]


def detection_batches(scans: Iterable[ScanResult]) -> list[DetectionBatch]:
    """One batch per scan, in the order the scans' detections would be reported."""

    def report(scan: ScanResult) -> tuple[datetime, str, int]:
        latency = 0.0
        if scan.detections:
            first = scan.detections[0]
            latency = (first.metadata["report_time"] - first.timestamp).total_seconds()
        return scan.end + timedelta(seconds=latency), scan.sensor_id, scan.scan

    ordered = sorted(scans, key=report)
    return [(s.end, set(s.detections)) for s in ordered]


@dataclass
class TrackerRun:
    """Every track one tracker produced, with when each was confirmed."""

    spec: TrackerSpec
    seed: int
    tracks: list[Track]
    labels: list[str]
    confirmed_at: list[datetime]
    elapsed_s: float
    position_mapping: tuple[int, int, int]
    velocity_mapping: tuple[int, int, int]

    @property
    def tracker_id(self) -> str:
        return self.spec.id

    def reported_tracks(self) -> list[Track]:
        """Each track from its confirmation on: what the tracker would report."""
        out = []
        for track, label, when in zip(self.tracks, self.labels, self.confirmed_at, strict=True):
            states = [s for s in track.states if s.timestamp >= when]
            out.append(Track(states, id=label))
        return out

    def tracks_table(self) -> pa.Table:
        """One row per track state, tentative ones flagged."""
        rows: dict[str, list[Any]] = {k: [] for k in _TRACK_SCHEMA.names}
        px, py, pz = self.position_mapping
        vx, vy, vz = self.velocity_mapping
        for track, label, when in zip(self.tracks, self.labels, self.confirmed_at, strict=True):
            for state in track.states:
                mean = np.asarray(state.mean, dtype=float).ravel()
                covar = np.asarray(state.covar, dtype=float)
                detection = state.hypothesis.measurement if isinstance(state, Update) else None
                rows["tracker_id"].append(self.tracker_id)
                rows["track_id"].append(label)
                rows["timestamp"].append(state.timestamp)
                rows["confirmed"].append(state.timestamp >= when)
                rows["updated"].append(isinstance(state, Update))
                for key, i in (("east_m", px), ("north_m", py), ("up_m", pz)):
                    rows[key].append(float(mean[i]))
                for key, i in (("v_east_mps", vx), ("v_north_mps", vy), ("v_up_mps", vz)):
                    rows[key].append(float(mean[i]))
                rows["sigma_h_m"].append(float(np.sqrt(max(covar[px, px] + covar[py, py], 0.0))))
                kind = None
                segment = None
                if isinstance(detection, TrueDetection):
                    kind, segment = "target", detection.metadata.get("segment_id")
                elif isinstance(detection, Clutter):
                    kind = "clutter"
                rows["detection_kind"].append(kind)
                rows["segment_id"].append(segment)
        return pa.table(rows, schema=_TRACK_SCHEMA)

    def write(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(self.tracks_table(), out)
        return out


_TRACK_SCHEMA = pa.schema(
    [
        ("tracker_id", pa.string()),
        ("track_id", pa.string()),
        ("timestamp", pa.timestamp("us", tz="UTC")),
        ("confirmed", pa.bool_()),
        ("updated", pa.bool_()),
        ("east_m", pa.float64()),
        ("north_m", pa.float64()),
        ("up_m", pa.float64()),
        ("v_east_mps", pa.float64()),
        ("v_north_mps", pa.float64()),
        ("v_up_mps", pa.float64()),
        ("sigma_h_m", pa.float64()),
        ("detection_kind", pa.string()),
        ("segment_id", pa.string()),
    ]
)


def _confirmation_time(track: Track, min_points: int) -> datetime:
    """When the track got its ``min_points``-th update (it was confirmed then)."""
    count = 0
    for state in track.states:
        if isinstance(state, Update):
            count += 1
            if count >= min_points:
                timestamp: datetime = state.timestamp
                return timestamp
    last: datetime = track.states[-1].timestamp
    return last


def run_tracker(spec: TrackerSpec, batches: Sequence[DetectionBatch], *, seed: int) -> TrackerRun:
    """Run one tracker over ``batches`` and collect all its tracks."""
    pipeline = build_tracker(spec, seed=seed)
    adapted = [(t, pipeline.adapt(dets)) for t, dets in batches]
    tracker = pipeline.make_tracker(adapted)
    seen: dict[int, Track] = {}
    started = time.perf_counter()
    for _, tracks in tracker:
        for track in tracks:
            seen.setdefault(id(track), track)
    elapsed = time.perf_counter() - started

    confirmed = {key: _confirmation_time(t, spec.initiation.min_points) for key, t in seen.items()}

    def order(key: int) -> tuple[Any, ...]:
        track = seen[key]
        first = np.asarray(track.states[0].mean, dtype=float).ravel()
        return (confirmed[key], track.states[0].timestamp, tuple(np.round(first, 6)))

    keys = sorted(seen, key=order)
    return TrackerRun(
        spec=spec,
        seed=seed,
        tracks=[seen[k] for k in keys],
        labels=[f"{spec.id}:{i + 1:04d}" for i in range(len(keys))],
        confirmed_at=[confirmed[k] for k in keys],
        elapsed_s=elapsed,
        position_mapping=pipeline.position_mapping,
        velocity_mapping=pipeline.velocity_mapping,
    )
