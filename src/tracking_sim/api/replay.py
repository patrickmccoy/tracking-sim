"""A stored run's truth, detections and tracks, shaped for a map.

Everything is in latitude and longitude, converted from the run's local
frame here so the browser needs no geodesy. Each response is columnar
(one array per field) to keep it small: an hour of busy traffic is tens of
thousands of points. Times are POSIX seconds.

Truth is not stored with a run; it is rebuilt from the run's truth file,
which must be unchanged since the run (its digest is checked), exactly as
``sim.dataset.load_run`` does.
"""

from __future__ import annotations

import threading
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from tracking_sim.eval.runstore import RunRecord, input_digest
from tracking_sim.geo import LocalFrame
from tracking_sim.sim.scenario import load_truth, scenario_from_resolved
from tracking_sim.truth.builder import TruthSet
from tracking_sim.truth.record import ensure_utc

__all__ = ["RunData", "RunDataCache"]


def _round(values: Any, digits: int) -> list[float | None]:
    a = np.round(np.asarray(values, dtype=float), digits)
    return [None if np.isnan(v) else float(v) for v in a]


def _posix(column: Any) -> Any:
    return np.asarray(column.cast("int64"), dtype=float) / 1e6  # microsecond timestamps


class RunData:
    """One stored run, read lazily."""

    def __init__(self, record: RunRecord) -> None:
        self.record = record
        resolved = record.manifest["scenario"]
        f = resolved["frame"]
        self.frame = LocalFrame(f["lat"], f["lon"], f["alt_m"])
        self._resolved = resolved
        self._truth: TruthSet | None = None
        self._lock = threading.Lock()

    def truth_set(self) -> TruthSet:
        with self._lock:
            if self._truth is None:
                scenario = scenario_from_resolved(self._resolved)
                digest = input_digest(scenario.truth_path)
                if digest != self.record.manifest["inputs"]["truth"]:
                    raise ValueError(
                        f"{scenario.truth_path} has changed since run {self.record.id}"
                    )
                self._truth = load_truth(scenario)
            return self._truth

    def window(self) -> tuple[float, float]:
        """The run's time span: its truth window, or the span of its detections."""
        window = self._resolved["truth"].get("window")
        if window:
            lo, hi = (ensure_utc(datetime.fromisoformat(w)) for w in window)
            return lo.timestamp(), hi.timestamp()
        t = _posix(pq.read_table(self.record.path / "detections.parquet")["timestamp"])
        return (float(t.min()), float(t.max())) if len(t) else (0.0, 0.0)

    def truth(self, step_s: float = 5.0) -> dict[str, Any]:
        """Every truth segment, sampled every ``step_s`` within the run's window."""
        lo, hi = self.window()
        segments = []
        for seg in self.truth_set():
            t0, t1 = max(float(seg.t[0]), lo), min(float(seg.t[-1]), hi)
            if t1 <= t0:
                continue
            times = np.arange(np.ceil(t0 / step_s) * step_s, t1, step_s)
            times = np.unique(np.concatenate([[t0], times, [t1]]))
            pos, vel, _ = seg.evaluate(times)
            lat, lon, alt = self.frame.to_geodetic(pos[:, 0], pos[:, 1], pos[:, 2])
            segments.append(
                {
                    "id": seg.segment_id,
                    "icao24": seg.icao24,
                    "callsign": seg.callsign,
                    "t": _round(times, 1),
                    "lat": _round(lat, 6),
                    "lon": _round(lon, 6),
                    "alt_m": _round(alt, 0) if seg.has_altitude else [None] * len(times),
                    "speed_mps": _round(np.hypot(vel[:, 0], vel[:, 1]), 1),
                    "course_deg": _round(np.degrees(np.arctan2(vel[:, 0], vel[:, 1])) % 360, 1),
                }
            )
        return {"window": list(self.window()), "step_s": step_s, "segments": segments}

    def detections(self) -> dict[str, Any]:
        """Every detection, columnar, ordered by time."""
        table = pq.read_table(self.record.path / "detections.parquet").sort_by("timestamp")
        lat, lon, _ = self.frame.to_geodetic(
            table["east_m"].to_numpy(), table["north_m"].to_numpy(), np.zeros(table.num_rows)
        )
        return {
            "t": _round(_posix(table["timestamp"]), 2),
            "sensor_id": table["sensor_id"].to_pylist(),
            "scan": table["scan"].to_pylist(),
            "kind": table["kind"].to_pylist(),
            "segment_id": table["segment_id"].to_pylist(),
            "lat": _round(lat, 6),
            "lon": _round(lon, 6),
        }

    def trackers(self) -> list[str]:
        return [t["id"] for t in self._resolved["trackers"]]

    def tracks(self, tracker_id: str, *, tentative: bool = False) -> dict[str, Any]:
        """One tracker's tracks: confirmed states, or every state with ``tentative``."""
        path = self.record.path / "tracks" / f"{tracker_id}.parquet"
        if tracker_id not in self.trackers() or not path.exists():
            raise KeyError(tracker_id)
        rows = pq.read_table(path).sort_by([("track_id", "ascending"), ("timestamp", "ascending")])
        data = rows.to_pydict()
        t = _posix(rows["timestamp"])
        by_track: dict[str, list[int]] = {}
        for i, (track, confirmed) in enumerate(
            zip(data["track_id"], data["confirmed"], strict=True)
        ):
            if confirmed or tentative:
                by_track.setdefault(track, []).append(i)
        e, n, u = (np.asarray(data[k], dtype=float) for k in ("east_m", "north_m", "up_m"))
        lat, lon, alt = self.frame.to_geodetic(e, n, u)
        ve, vn = np.asarray(data["v_east_mps"]), np.asarray(data["v_north_mps"])
        tracks = []
        for track, idx in by_track.items():
            ix = np.asarray(idx)
            segments = Counter(data["segment_id"][i] for i in idx if data["segment_id"][i])
            tracks.append(
                {
                    "id": track,
                    "segment_id": segments.most_common(1)[0][0] if segments else None,
                    "t": _round(t[ix], 2),
                    "lat": _round(lat[ix], 6),
                    "lon": _round(lon[ix], 6),
                    "alt_m": _round(alt[ix], 0),
                    "speed_mps": _round(np.hypot(ve[ix], vn[ix]), 1),
                    "course_deg": _round(np.degrees(np.arctan2(ve[ix], vn[ix])) % 360, 1),
                    "sigma_h_m": _round(np.asarray(data["sigma_h_m"])[ix], 0),
                    "confirmed": [bool(data["confirmed"][i]) for i in idx],
                    "updated": [bool(data["updated"][i]) for i in idx],
                }
            )
        return {"tracker": tracker_id, "tracks": tracks}


class RunDataCache:
    """The last few runs opened, so truth is rebuilt once per run, not per request."""

    def __init__(self, size: int = 4) -> None:
        self.size = size
        self._items: OrderedDict[str, RunData] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, record: RunRecord) -> RunData:
        with self._lock:
            data = self._items.pop(record.id, None) or RunData(record)
            self._items[record.id] = data
            while len(self._items) > self.size:
                self._items.popitem(last=False)
            return data
