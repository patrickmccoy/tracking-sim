"""What every ground-based sensor shares: a site, coverage gates and opportunities.

A sensor observes each aircraft at some set of times: when a rotating beam
crosses it (``RotatingRadar2D``), or on a fixed update clock for a staring
sensor (``PassiveBearingSensor``). At each of those times this module
samples truth and applies the same geometry gates, in order:

1. ``range``: slant range within the sensor's limits;
2. ``azimuth``: inside the compass sector;
3. ``elevation``: geometric elevation within its limits;
4. ``horizon``: line of sight over a smooth 4/3 earth from the mast;
5. ``terrain``: line of sight over the terrain, when there is a model.

Each observation becomes a ``Crossing``, which is a detection opportunity
if no gate hid the aircraft, and carries its probability of detection.
Subclasses decide the times, the Pd and how opportunities turn into
detections.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray
from stonesoup.types.detection import Clutter, Detection, TrueDetection
from stonesoup.types.groundtruth import GroundTruthPath

from tracking_sim.geo import LocalFrame, SensorSite, radar_horizon_visible
from tracking_sim.geo.terrain import TerrainHorizon
from tracking_sim.sensors.detection import AircraftClasses
from tracking_sim.truth.builder import FlightSegment, TruthSet
from tracking_sim.truth.record import ensure_utc

if TYPE_CHECKING:
    from tracking_sim.sim.rng import SensorStreams

__all__ = ["GATES", "Crossing", "GroundSensor", "ScanResult"]

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]

# Gate names, in the order they are checked.
GATES = ("range", "azimuth", "elevation", "horizon", "terrain")


@dataclass(frozen=True)
class Crossing:
    """One look at one aircraft: a detection opportunity if ``gate`` is None.

    (For a rotating radar a look is a beam crossing, hence the name.)
    Angles are radians (compass azimuth, geometric elevation), distances
    metres, ``t`` POSIX seconds. ``state_vector`` is the true scenario-frame
    state ``[x, vx, y, vy, z, vz]`` at ``t``. ``gate`` names the first
    geometry gate that hid the aircraft, if any.
    """

    sensor_id: str
    scan: int
    t: float
    segment_id: str
    icao24: str
    callsign: str | None
    azimuth: float
    elevation: float
    slant_range: float
    ground_range: float
    height_m: float
    state_vector: FloatArray = field(repr=False, compare=False)
    gate: str | None
    rcs_class: str = "unknown"
    pd: float = 1.0  # this crossing's probability of detection, if in coverage

    @property
    def in_coverage(self) -> bool:
        return self.gate is None

    @property
    def time(self) -> datetime:
        return datetime.fromtimestamp(self.t, UTC)


@dataclass
class ScanResult:
    """Everything one sensor produced in one scan (or update)."""

    sensor_id: str
    scan: int
    start: datetime
    end: datetime
    crossings: list[Crossing]
    detected: list[bool]
    detections: list[Detection]

    @property
    def opportunities(self) -> list[Crossing]:
        """Crossings that passed every geometry gate."""
        return [c for c in self.crossings if c.in_coverage]

    @property
    def true_detections(self) -> list[TrueDetection]:
        return [d for d in self.detections if isinstance(d, TrueDetection)]

    @property
    def clutter(self) -> list[Clutter]:
        return [d for d in self.detections if isinstance(d, Clutter)]


class GroundSensor:
    """A sensor at a fixed site, observing on a clock of ``period_s`` from ``t_ref``.

    Scan (or update) ``k`` covers ``[t_ref + k * period, t_ref + (k + 1) *
    period)``. Subclasses set ``measurement_model`` and implement
    ``_observation_times``, ``_scan_index``, ``pd`` and ``observe``.
    """

    measurement_model: Any

    def __init__(
        self,
        spec: Any,
        frame: LocalFrame,
        t_ref: datetime,
        period_s: float,
        *,
        terrain: TerrainHorizon | None = None,
        classes: AircraftClasses | None = None,
    ) -> None:
        self.spec = spec
        self.frame = frame
        self.t_ref = ensure_utc(t_ref)
        self._period_s = period_s
        pos = spec.position
        self.site = SensorSite(frame, pos.lat, pos.lon, pos.alt_m, pos.mast_m)
        if terrain is not None and terrain.site != self.site:
            raise ValueError("the terrain horizon was built for a different site")
        self.terrain = terrain if spec.coverage.terrain_masking else None
        self.classes = classes or AircraftClasses()

    @property
    def sensor_id(self) -> str:
        sensor: str = self.spec.id
        return sensor

    @property
    def period_s(self) -> float:
        return self._period_s

    def scan_window(self, scan: int) -> tuple[datetime, datetime]:
        start = self.t_ref + timedelta(seconds=scan * self.period_s)
        return start, start + timedelta(seconds=self.period_s)

    # -- what subclasses provide -------------------------------------------

    def _observation_times(self, seg: FlightSegment, lo: float, hi: float) -> FloatArray:
        raise NotImplementedError

    def _scan_index(self, times: FloatArray) -> IntArray:
        raise NotImplementedError

    def pd(self, slant_range_m: Any, rcs_class: str) -> FloatArray:
        raise NotImplementedError

    @property
    def expected_false_alarms(self) -> float:
        """Mean number of false alarms per scan."""
        raise NotImplementedError

    def observe(
        self,
        scan: int,
        crossings: Sequence[Crossing],
        streams: SensorStreams,
        paths: Mapping[str, GroundTruthPath],
    ) -> ScanResult:
        """Turn one scan's opportunities into detections (subclasses)."""
        raise NotImplementedError

    # -- geometry (deterministic) ------------------------------------------

    def crossings(
        self, truth: TruthSet, start: datetime, end: datetime
    ) -> tuple[list[Crossing], list[str]]:
        """Every look at every segment in ``[start, end)``, in time order.

        Returns the looks and the ids of segments skipped because they have
        no altitude: slant range, elevation and line of sight all depend on
        height, and placing such an aircraft on the ground would be wrong.
        """
        if truth.frame != self.frame:
            raise ValueError("the truth set and the sensor must share a LocalFrame")
        t0, t1 = ensure_utc(start).timestamp(), ensure_utc(end).timestamp()
        out: list[Crossing] = []
        skipped: list[str] = []
        for seg in truth:
            if not seg.has_altitude:
                skipped.append(seg.segment_id)
                continue
            out.extend(self._segment_crossings(seg, t0, t1))
        out.sort(key=lambda c: (c.t, c.segment_id))
        return out, skipped

    def _segment_crossings(self, seg: FlightSegment, t0: float, t1: float) -> list[Crossing]:
        lo, hi = max(float(seg.t[0]), t0), min(float(seg.t[-1]), t1)
        if hi <= lo:
            return []
        times = self._observation_times(seg, lo, hi)
        times = times[(times >= t0) & (times < t1)]
        if times.size == 0:
            return []
        pos, vel, _ = seg.evaluate(times)
        az, el, rng = self.site.polar(pos)
        local = self.site.to_local(pos)
        ground = np.hypot(local[:, 0], local[:, 1])
        height = self.site.height_above_site(pos)
        gates = self._gates(az, el, rng, ground, height)
        rcs_class = self.classes.classify(seg.icao24, seg.category)
        pd = self.pd(rng, rcs_class)
        scans = self._scan_index(times)
        states = np.column_stack([pos[:, 0], vel[:, 0], pos[:, 1], vel[:, 1], pos[:, 2], vel[:, 2]])
        return [
            Crossing(
                sensor_id=self.sensor_id,
                scan=int(scans[i]),
                t=float(times[i]),
                segment_id=seg.segment_id,
                icao24=seg.icao24,
                callsign=seg.callsign,
                azimuth=float(az[i]),
                elevation=float(el[i]),
                slant_range=float(rng[i]),
                ground_range=float(ground[i]),
                height_m=float(height[i]),
                state_vector=states[i],
                gate=gates[i],
                rcs_class=rcs_class,
                pd=float(pd[i]),
            )
            for i in range(len(times))
        ]

    def _gates(
        self,
        az: FloatArray,
        el: FloatArray,
        rng: FloatArray,
        ground: FloatArray,
        height: FloatArray,
    ) -> list[str | None]:
        cov = self.spec.coverage
        ok_range = (rng >= cov.range_min_m) & (rng <= cov.range_max_m)
        if cov.full_circle:
            ok_az = np.ones_like(az, dtype=bool)
        else:
            from_deg = cov.azimuth_deg[0]
            ok_az = np.mod(np.degrees(az) - from_deg, 360.0) <= cov.sector_width_deg
        el_deg = np.degrees(el)
        ok_el = (el_deg >= cov.elevation_deg[0]) & (el_deg <= cov.elevation_deg[1])
        if cov.radar_horizon:
            ok_horizon = radar_horizon_visible(
                ground, self.spec.position.mast_m, height, cov.refraction_k
            )
        else:
            ok_horizon = np.ones_like(az, dtype=bool)
        if self.terrain is not None:
            ok_terrain = ~self.terrain.masked(az, ground, height)
        else:
            ok_terrain = np.ones_like(az, dtype=bool)
        checks = (ok_range, ok_az, ok_el, ok_horizon, ok_terrain)
        result: list[str | None] = []
        for i in range(len(az)):
            result.append(
                next((name for name, ok in zip(GATES, checks, strict=True) if not ok[i]), None)
            )
        return result

    def _metadata(self, scan: int, t: float, kind: str) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "scan": scan,
            "kind": kind,
            "report_time": datetime.fromtimestamp(t, UTC) + timedelta(seconds=self.spec.latency_s),
        }
