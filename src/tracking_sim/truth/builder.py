"""The truth builder: sparse TruthRecords to clean per-flight segments.

Steps, in order:

1. Deduplicate records that more than one receiver stream delivered.
2. Group by ``icao24`` and split into flight segments on a time gap.
3. Reject position (and altitude) outliers, such as bad decodes, with a
   speed gate.
4. Merge the sparse position, altitude and velocity messages per segment.
5. Convert to the local ENU frame of the scenario origin (SI units from here).

6. Smooth each segment (``tracking_sim.truth.trajectory``) and expose
   ``state_at(t)``, interpolating only, never extrapolating.
7. Export Stone Soup ``GroundTruthPath`` objects for trackers and metrics.
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from tracking_sim.geo import LocalFrame, enu_velocity
from tracking_sim.truth.record import TruthRecord
from tracking_sim.truth.trajectory import Trajectory, TruthState, to_posix
from tracking_sim.units import DEG_TO_RAD, FPM_TO_MPS, FT_TO_M, KT_TO_MPS

if TYPE_CHECKING:
    from stonesoup.types.groundtruth import GroundTruthPath

__all__ = [
    "AltitudeReference",
    "BuildStats",
    "FlightSegment",
    "TruthBuilderConfig",
    "TruthSet",
    "build_truth",
    "gate_outliers",
]

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]
AltitudeReference = Literal["geom", "baro", "unknown"]

_DEFAULT_ALTITUDE_PREFERENCE: tuple[AltitudeReference, ...] = ("geom", "baro", "unknown")
_ALT_FIELDS: dict[AltitudeReference, str] = {
    "geom": "alt_geom_ft",
    "baro": "alt_baro_ft",
    "unknown": "alt_unknown_ft",
}


class TruthBuilderConfig(BaseModel):
    """Tuning for the truth builder. Defaults follow the design document."""

    model_config = ConfigDict(extra="forbid")

    dedupe_window_s: float = Field(
        1.0, description="identical messages closer than this are one message"
    )
    segment_gap_s: float = Field(60.0, description="split a flight on a position gap this long")
    max_speed_mps: float = Field(350.0, description="speed gate for position outliers")
    max_vrate_mps: float = Field(100.0, description="vertical rate gate for altitude outliers")
    gate_slack_m: float | None = Field(
        None, description="distance allowance in the gate; default 4x the horizontal sigma"
    )
    altitude_gate_slack_m: float = 100.0
    min_segment_points: int = Field(5, ge=2)
    default_horizontal_sigma_m: float = Field(
        30.0, description="truth sigma for sources without NACp"
    )
    altitude_preference: list[AltitudeReference] = Field(
        default_factory=lambda: list(_DEFAULT_ALTITUDE_PREFERENCE)
    )
    velocity_merge_window_s: float = Field(
        10.0, description="attach a velocity report to a position at most this far away"
    )
    drop_on_ground: bool = False
    smoothing: Literal["spline", "linear"] = "spline"
    smoothing_lam: float | None = Field(
        None, description="spline smoothing parameter; None picks it by cross-validation"
    )


@dataclass
class BuildStats:
    records_in: int = 0
    duplicates_dropped: int = 0
    position_outliers_dropped: int = 0
    altitude_outliers_dropped: int = 0
    segments_built: int = 0
    segments_too_short: int = 0
    segments_on_ground_dropped: int = 0
    aircraft: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class FlightSegment:
    """One continuous stretch of one aircraft's flight, cleaned and in ENU.

    ``t`` is POSIX seconds (strictly increasing). ``enu`` is (n, 3) metres.
    ``alt_m`` is the altitude used for the conversion, in the segment's
    ``alt_reference``, or NaN if the segment has no altitude at all.
    ``reported_velocity`` is the ADS-B velocity report nearest each position
    (within the merge window), as ENU m/s, NaN where there was none.
    """

    segment_id: str
    icao24: str
    callsign: str | None
    source_ids: tuple[str, ...]
    t: FloatArray
    lat: FloatArray
    lon: FloatArray
    alt_m: FloatArray
    alt_reference: AltitudeReference | None
    enu: FloatArray
    reported_velocity: FloatArray
    on_ground: bool
    horizontal_sigma_m: float
    smoothing: Literal["spline", "linear"] = "spline"
    smoothing_lam: float | None = None
    _trajectory: Trajectory | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def trajectory(self) -> Trajectory:
        """The smoothed fit, built on first use."""
        if self._trajectory is None:
            self._trajectory = Trajectory(
                self.t, self.enu, method=self.smoothing, lam=self.smoothing_lam
            )
        return self._trajectory

    def state_at(self, t: datetime | float) -> TruthState | None:
        """Smoothed ENU state at ``t``, or ``None`` outside the segment."""
        return self.trajectory.state_at(t)

    def evaluate(self, t: Any) -> tuple[FloatArray, FloatArray, BoolArray]:
        """Vectorised ``state_at``: positions, velocities and a validity mask."""
        return self.trajectory.evaluate(t)

    def metadata(self) -> dict[str, Any]:
        return {
            "icao24": self.icao24,
            "callsign": self.callsign,
            "segment_id": self.segment_id,
            "alt_reference": self.alt_reference,
            "horizontal_sigma_m": self.horizontal_sigma_m,
            "on_ground": self.on_ground,
            "source_ids": self.source_ids,
        }

    def to_ground_truth_path(
        self, times: Any = None, *, step_s: float | None = None
    ) -> GroundTruthPath:
        """Export as a Stone Soup ``GroundTruthPath``.

        States are taken at ``times`` if given (points outside the segment are
        skipped), every ``step_s`` seconds from the segment start if given,
        and at the observation times otherwise. State vectors are
        ``[x, vx, y, vy, z, vz]`` in the local ENU frame, and every state
        carries the segment metadata.
        """
        from stonesoup.types.array import StateVector
        from stonesoup.types.groundtruth import GroundTruthPath, GroundTruthState

        if times is not None:
            tt = to_posix(times)
        elif step_s is not None:
            tt = np.arange(self.t[0], self.t[-1] + 1e-9, step_s)
        else:
            tt = self.t
        pos, vel, valid = self.evaluate(tt)
        meta = self.metadata()
        states = [
            GroundTruthState(
                StateVector([p[0], v[0], p[1], v[1], p[2], v[2]]),
                timestamp=datetime.fromtimestamp(float(ts), UTC),
                metadata=dict(meta),
            )
            for ts, p, v, ok in zip(tt, pos, vel, valid, strict=True)
            if ok
        ]
        return GroundTruthPath(states, id=self.segment_id)

    @property
    def start(self) -> datetime:
        return datetime.fromtimestamp(float(self.t[0]), UTC)

    @property
    def end(self) -> datetime:
        return datetime.fromtimestamp(float(self.t[-1]), UTC)

    @property
    def duration_s(self) -> float:
        return float(self.t[-1] - self.t[0])

    @property
    def has_altitude(self) -> bool:
        return self.alt_reference is not None

    def __len__(self) -> int:
        return len(self.t)


@dataclass
class TruthSet:
    """Every flight segment built from one batch of records."""

    frame: LocalFrame
    segments: list[FlightSegment]
    stats: BuildStats
    config: TruthBuilderConfig = field(default_factory=TruthBuilderConfig)

    def by_icao(self, icao24: str) -> list[FlightSegment]:
        icao24 = icao24.lower()
        return [s for s in self.segments if s.icao24 == icao24]

    def segment(self, segment_id: str) -> FlightSegment:
        for seg in self.segments:
            if seg.segment_id == segment_id:
                return seg
        raise KeyError(segment_id)

    @property
    def icao24s(self) -> list[str]:
        return sorted({s.icao24 for s in self.segments})

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        if not self.segments:
            return None
        return min(s.start for s in self.segments), max(s.end for s in self.segments)

    def __len__(self) -> int:
        return len(self.segments)

    def __iter__(self) -> Iterator[FlightSegment]:
        return iter(self.segments)

    def active(self, t: datetime | float) -> list[FlightSegment]:
        """Segments whose time span contains ``t``."""
        ts = float(to_posix(t)[0])
        return [s for s in self.segments if s.t[0] <= ts <= s.t[-1]]

    def states_at(self, t: datetime | float) -> dict[str, TruthState]:
        """Smoothed state of every segment active at ``t``, keyed by segment id."""
        out: dict[str, TruthState] = {}
        for seg in self.active(t):
            state = seg.state_at(t)
            if state is not None:
                out[seg.segment_id] = state
        return out

    def ground_truth_paths(self, *, step_s: float | None = None) -> list[GroundTruthPath]:
        """Every segment as a Stone Soup ``GroundTruthPath``."""
        return [s.to_ground_truth_path(step_s=step_s) for s in self.segments]


def gate_outliers(
    t: FloatArray, points: FloatArray, max_rate: float, slack: float, max_passes: int = 10
) -> BoolArray:
    """Return a keep-mask that rejects isolated jumps.

    A step between two points "fails" when the distance is more than
    ``max_rate * dt + slack``. A point is rejected when the steps on both
    sides of it fail but its two neighbours agree with each other, so one bad
    decode is removed without throwing away the good points around it. End
    points are rejected when their only step fails and the next step is fine.
    Repeats until nothing changes. ``points`` is (n, k).
    """
    n = len(t)
    keep = np.ones(n, dtype=bool)
    if n < 3:
        return keep
    for _ in range(max_passes):
        idx = np.flatnonzero(keep)
        if len(idx) < 3:
            break
        tt, pp = t[idx], points[idx]

        def fails(a: slice, b: slice, tt: FloatArray = tt, pp: FloatArray = pp) -> BoolArray:
            dist = np.linalg.norm(pp[b] - pp[a], axis=1)
            limit: BoolArray = dist > max_rate * np.abs(tt[b] - tt[a]) + slack
            return limit

        step = fails(slice(0, -1), slice(1, None))  # i -> i+1
        skip = fails(slice(0, -2), slice(2, None))  # i-1 -> i+1
        bad = np.zeros(len(idx), dtype=bool)
        bad[1:-1] = step[:-1] & step[1:] & ~skip
        bad[0] = step[0] & ~step[1]
        bad[-1] = step[-1] & ~step[-2]
        if not bad.any():
            break
        keep[idx[bad]] = False
    return keep


@dataclass
class _Track:
    """All observations of one aircraft, before segmentation."""

    positions: list[tuple[float, float, float, bool | None, str]] = field(default_factory=list)
    altitudes: dict[AltitudeReference, list[tuple[float, float]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    velocities: list[tuple[float, float, float, float | None]] = field(default_factory=list)
    callsigns: list[tuple[float, str]] = field(default_factory=list)


def _collect(
    records: Iterable[TruthRecord], window_s: float, stats: BuildStats
) -> dict[str, _Track]:
    """Group records per aircraft and drop duplicate messages."""
    tracks: dict[str, _Track] = defaultdict(_Track)
    last_seen: dict[tuple[object, ...], float] = {}

    def duplicate(key: tuple[object, ...], ts: float) -> bool:
        previous = last_seen.get(key)
        last_seen[key] = ts if previous is None or ts - previous > window_s else previous
        return previous is not None and 0.0 <= ts - previous <= window_s

    for record in sorted(records, key=lambda r: r.ts):
        stats.records_in += 1
        ts = record.ts.timestamp()
        track = tracks[record.icao24]
        dropped = False
        if record.lat is not None and record.lon is not None:
            key = ("p", record.icao24, record.lat, record.lon, record.altitude_ft)
            if duplicate(key, ts):
                dropped = True
            else:
                track.positions.append(
                    (ts, record.lat, record.lon, record.on_ground, record.source_id)
                )
        if record.gs_kt is not None and record.track_deg is not None:
            key = ("v", record.icao24, record.gs_kt, record.track_deg, record.vrate_fpm)
            if duplicate(key, ts):
                dropped = True
            else:
                track.velocities.append((ts, record.gs_kt, record.track_deg, record.vrate_fpm))
        if not dropped:
            for ref, name in _ALT_FIELDS.items():
                value = getattr(record, name)
                if value is not None:
                    track.altitudes[ref].append((ts, value * FT_TO_M))
        if record.callsign:
            track.callsigns.append((ts, record.callsign))
        if dropped:
            stats.duplicates_dropped += 1
    return tracks


def _split(t: FloatArray, gap_s: float) -> list[slice]:
    if len(t) == 0:
        return []
    breaks = np.flatnonzero(np.diff(t) > gap_s) + 1
    edges = [0, *breaks.tolist(), len(t)]
    return [slice(a, b) for a, b in itertools.pairwise(edges)]


def _segment_altitude(
    track: _Track,
    t: FloatArray,
    config: TruthBuilderConfig,
    stats: BuildStats,
) -> tuple[FloatArray, AltitudeReference | None]:
    """Altitude at each position time from the most preferred reference available."""
    margin = config.velocity_merge_window_s
    lo, hi = t[0] - margin, t[-1] + margin
    for ref in config.altitude_preference:
        samples = [(ts, h) for ts, h in track.altitudes.get(ref, []) if lo <= ts <= hi]
        if not samples:
            continue
        at = np.array([s[0] for s in samples])
        ah = np.array([s[1] for s in samples])
        order = np.argsort(at, kind="stable")
        at, ah = at[order], ah[order]
        keep = gate_outliers(at, ah[:, None], config.max_vrate_mps, config.altitude_gate_slack_m)
        stats.altitude_outliers_dropped += int((~keep).sum())
        at, ah = at[keep], ah[keep]
        return np.interp(t, at, ah), ref
    return np.full(len(t), np.nan), None


def _segment_velocity(track: _Track, t: FloatArray, window_s: float) -> FloatArray:
    out = np.full((len(t), 3), np.nan)
    if not track.velocities:
        return out
    vel = np.array(
        [(ts, gs, trk, np.nan if vr is None else vr) for ts, gs, trk, vr in track.velocities]
    )
    vel = vel[np.argsort(vel[:, 0], kind="stable")]
    vt = vel[:, 0]
    right = np.clip(np.searchsorted(vt, t), 0, len(vt) - 1)
    left = np.clip(right - 1, 0, len(vt) - 1)
    nearest = np.where(np.abs(vt[left] - t) <= np.abs(vt[right] - t), left, right)
    close = np.abs(vt[nearest] - t) <= window_s
    ve, vn, vu = enu_velocity(
        vel[nearest, 1] * KT_TO_MPS, vel[nearest, 2] * DEG_TO_RAD, vel[nearest, 3] * FPM_TO_MPS
    )
    out[close] = np.column_stack([ve, vn, vu])[close]
    return out


def _callsign(track: _Track, t0: float, t1: float) -> str | None:
    within = [cs for ts, cs in track.callsigns if t0 - 60.0 <= ts <= t1 + 60.0]
    if not within:
        return None
    return Counter(within).most_common(1)[0][0]


def build_segments(
    icao24: str,
    track: _Track,
    frame: LocalFrame,
    config: TruthBuilderConfig,
    stats: BuildStats,
) -> list[FlightSegment]:
    """Segment, gate, merge and convert one aircraft's observations."""
    if not track.positions:
        return []
    positions = sorted(track.positions, key=lambda p: p[0])
    t_all = np.array([p[0] for p in positions])
    lat_all = np.array([p[1] for p in positions])
    lon_all = np.array([p[2] for p in positions])
    ground_all = np.array([bool(p[3]) for p in positions])
    sources_all = [p[4] for p in positions]
    slack = (
        config.gate_slack_m
        if config.gate_slack_m is not None
        else 4.0 * config.default_horizontal_sigma_m
    )

    segments: list[FlightSegment] = []
    for part in _split(t_all, config.segment_gap_s):
        t, lat, lon = t_all[part], lat_all[part], lon_all[part]
        ground, sources = ground_all[part], sources_all[part]
        if len(t) < config.min_segment_points:
            stats.segments_too_short += 1
            continue

        alt_m, alt_ref = _segment_altitude(track, t, config, stats)
        h = np.where(np.isnan(alt_m), frame.alt_m, alt_m)
        e, n, u = frame.to_enu(lat, lon, h)
        enu = np.column_stack([e, n, u])

        keep = gate_outliers(t, enu[:, :2], config.max_speed_mps, slack)
        stats.position_outliers_dropped += int((~keep).sum())
        # Two positions at one instant can't both be right; keep the first.
        unique = np.ones(len(t), dtype=bool)
        unique[1:] = np.diff(t) > 0
        keep &= unique
        t, lat, lon, alt_m, enu, ground = (
            t[keep],
            lat[keep],
            lon[keep],
            alt_m[keep],
            enu[keep],
            ground[keep],
        )
        sources = [s for s, k in zip(sources, keep, strict=True) if k]
        if len(t) < config.min_segment_points:
            stats.segments_too_short += 1
            continue

        on_ground = bool(ground.mean() > 0.5)
        if on_ground and config.drop_on_ground:
            stats.segments_on_ground_dropped += 1
            continue

        segments.append(
            FlightSegment(
                segment_id=f"{icao24}-{len(segments)}",
                icao24=icao24,
                callsign=_callsign(track, float(t[0]), float(t[-1])),
                source_ids=tuple(sorted(set(sources))),
                t=t,
                lat=lat,
                lon=lon,
                alt_m=alt_m,
                alt_reference=alt_ref,
                enu=enu,
                reported_velocity=_segment_velocity(track, t, config.velocity_merge_window_s),
                on_ground=on_ground,
                horizontal_sigma_m=config.default_horizontal_sigma_m,
                smoothing=config.smoothing,
                smoothing_lam=config.smoothing_lam,
            )
        )
    return segments


def build_truth(
    records: Iterable[TruthRecord],
    frame: LocalFrame,
    config: TruthBuilderConfig | None = None,
) -> TruthSet:
    """Build clean flight segments in ``frame`` from any iterable of records."""
    config = config or TruthBuilderConfig()
    stats = BuildStats()
    tracks = _collect(records, config.dedupe_window_s, stats)
    segments: list[FlightSegment] = []
    for icao24 in sorted(tracks):
        segments.extend(build_segments(icao24, tracks[icao24], frame, config, stats))
    stats.segments_built = len(segments)
    stats.aircraft = len({s.icao24 for s in segments})
    return TruthSet(frame=frame, segments=segments, stats=stats, config=config)
