"""The rotating 2D surveillance radar.

Per scan, for each aircraft, the radar:

1. finds when its beam crosses the aircraft and samples truth at that moment
   (``BeamSchedule``), not at the scan start;
2. applies the geometry gates: slant range limits, azimuth sector,
   elevation limits and the 4/3-earth radar horizon;
3. draws a detection with probability Pd;
4. adds Gaussian noise to slant range and bearing;
5. merges detected targets closer together than the resolution cell;
6. adds false alarms across the coverage area;

and emits Stone Soup ``TrueDetection`` objects (and ``Clutter`` for false
alarms) carrying the slant-range measurement model and, for targets, a link
to the truth path. Every crossing, detected or
not, is kept as an *opportunity* so achieved Pd can be measured against the
configured value.

Steps 1-2 are pure geometry, computed for the whole run up front by
``crossings``; steps 3-4 draw from seeded streams in ``observe``, one scan
at a time, in a fixed order so a seed always gives the same detections.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray
from stonesoup.types.angle import Bearing
from stonesoup.types.array import StateVector
from stonesoup.types.detection import Clutter, Detection, TrueDetection
from stonesoup.types.groundtruth import GroundTruthPath

from tracking_sim.geo import LocalFrame, SensorSite, radar_horizon_visible
from tracking_sim.sensors.measurement import CartesianToBearingSlantRange, compass_to_bearing
from tracking_sim.sensors.scan import BeamSchedule
from tracking_sim.sensors.spec import RotatingRadar2DSpec
from tracking_sim.sim.rng import SensorStreams
from tracking_sim.truth.builder import FlightSegment, TruthSet
from tracking_sim.truth.record import ensure_utc

__all__ = ["Crossing", "RotatingRadar2D", "ScanResult"]

FloatArray = NDArray[np.float64]

# Gate names, in the order they are checked.
GATES = ("range", "azimuth", "elevation", "horizon")


@dataclass(frozen=True)
class Crossing:
    """One beam crossing of one aircraft: a detection opportunity if ``gate`` is None.

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

    @property
    def in_coverage(self) -> bool:
        return self.gate is None

    @property
    def time(self) -> datetime:
        return datetime.fromtimestamp(self.t, UTC)


@dataclass
class ScanResult:
    """Everything one sensor produced in one scan."""

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


class RotatingRadar2D:
    """A rotating 2D radar built from a ``RotatingRadar2DSpec`` in a scenario frame.

    ``t_ref`` is when the beam points at the spec's start azimuth; scan
    ``k`` covers ``[t_ref + k * period, t_ref + (k + 1) * period)``.
    """

    def __init__(self, spec: RotatingRadar2DSpec, frame: LocalFrame, t_ref: datetime) -> None:
        self.spec = spec
        self.frame = frame
        self.t_ref = ensure_utc(t_ref)
        pos = spec.position
        self.site = SensorSite(frame, pos.lat, pos.lon, pos.alt_m, pos.mast_m)
        self.beam = BeamSchedule(
            t_ref=self.t_ref.timestamp(),
            period_s=spec.scan.period_s,
            start_azimuth=float(np.radians(spec.scan.start_azimuth_deg)),
            clockwise=spec.scan.direction == "clockwise",
        )
        self.range_sigma_m = spec.noise.range_sigma_m
        self.bearing_sigma = float(np.radians(spec.noise.bearing_sigma_deg))
        self.measurement_model = CartesianToBearingSlantRange(
            ndim_state=6,
            mapping=(0, 2, 4),
            noise_covar=np.diag([self.bearing_sigma**2, self.range_sigma_m**2]),
            translation_offset=StateVector(self.site.position),
            frame_rotation=self.site.rotation,
        )

    @property
    def sensor_id(self) -> str:
        return self.spec.id

    @property
    def period_s(self) -> float:
        return self.spec.scan.period_s

    def scan_window(self, scan: int) -> tuple[datetime, datetime]:
        start = self.t_ref + timedelta(seconds=scan * self.period_s)
        return start, start + timedelta(seconds=self.period_s)

    # -- geometry (deterministic) ------------------------------------------

    def crossings(
        self, truth: TruthSet, start: datetime, end: datetime
    ) -> tuple[list[Crossing], list[str]]:
        """Every beam crossing of every segment in ``[start, end)``, in time order.

        Returns the crossings and the ids of segments skipped because they
        have no altitude: a radar's slant range and horizon depend on height,
        and placing such an aircraft on the ground would be wrong.
        """
        if truth.frame != self.frame:
            raise ValueError("the truth set and the radar must share a LocalFrame")
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

    def _azimuth_of(self, seg: FlightSegment) -> Any:
        def azimuth(t: FloatArray) -> FloatArray:
            pos, _, _ = seg.evaluate(t)
            az, _, _ = self.site.polar(pos)
            return az

        return azimuth

    def _segment_crossings(self, seg: FlightSegment, t0: float, t1: float) -> list[Crossing]:
        lo, hi = max(float(seg.t[0]), t0), min(float(seg.t[-1]), t1)
        if hi <= lo:
            return []
        grid = np.append(np.arange(lo, hi, self.period_s / 2), hi)
        azimuth = self._azimuth_of(seg)
        times = self.beam.crossings(grid, azimuth(grid), azimuth)
        times = times[(times >= t0) & (times < t1)]
        if times.size == 0:
            return []
        pos, vel, _ = seg.evaluate(times)
        az, el, rng = self.site.polar(pos)
        local = self.site.to_local(pos)
        ground = np.hypot(local[:, 0], local[:, 1])
        height = self.site.height_above_site(pos)
        gates = self._gates(az, el, rng, ground, height)
        scans = self.beam.scan_index(times)
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
        checks = (ok_range, ok_az, ok_el, ok_horizon)
        result: list[str | None] = []
        for i in range(len(az)):
            result.append(
                next((name for name, ok in zip(GATES, checks, strict=True) if not ok[i]), None)
            )
        return result

    # -- observation (random) ----------------------------------------------

    def observe(
        self,
        scan: int,
        crossings: Sequence[Crossing],
        streams: SensorStreams,
        paths: Mapping[str, GroundTruthPath],
    ) -> ScanResult:
        """Turn one scan's crossings into detections.

        ``crossings`` must be this scan's, in time order. Pd and noise are
        drawn for every opportunity, detected or not, so the noise on one
        target does not depend on whether another was detected. Detected
        targets the radar cannot resolve then merge into one detection, and
        false alarms are added. Detections come back in time order.
        """
        start, end = self.scan_window(scan)
        opportunities = [c for c in crossings if c.in_coverage]
        n = len(opportunities)
        hits = streams.detection.random(n) < self.spec.detection.pd
        noise = streams.noise.standard_normal((n, 2)) * [self.bearing_sigma, self.range_sigma_m]
        hit_index = [i for i in range(n) if hits[i]]
        detections: list[Detection] = []
        for cluster in self._resolve([opportunities[i] for i in hit_index]):
            members = [hit_index[k] for k in cluster]
            detections.append(
                self._true_detection([opportunities[i] for i in members], noise[members], paths)
            )
        detections.extend(self._false_alarms(scan, streams.clutter))
        detections.sort(key=lambda d: d.timestamp)
        detected_ids = {(opportunities[i].segment_id, opportunities[i].t) for i in hit_index}
        detected = [(c.segment_id, c.t) in detected_ids for c in crossings]
        return ScanResult(
            sensor_id=self.sensor_id,
            scan=scan,
            start=start,
            end=end,
            crossings=list(crossings),
            detected=detected,
            detections=detections,
        )

    @property
    def coverage_area_m2(self) -> float:
        """Area of the coverage annulus (or sector of it), in square metres."""
        cov = self.spec.coverage
        sector = np.radians(cov.sector_width_deg)
        return float(0.5 * sector * (cov.range_max_m**2 - cov.range_min_m**2))

    @property
    def expected_false_alarms(self) -> float:
        """Mean number of false alarms per scan."""
        return self.spec.detection.false_alarm_density * self.coverage_area_m2

    def _false_alarms(self, scan: int, rng: np.random.Generator) -> list[Detection]:
        """Poisson-many false alarms, uniform over the coverage area.

        Uniform over area means uniform in azimuth and in range squared, so
        false alarms thin out towards the centre the way a PPI shows them.
        Each is timestamped when the beam points at its azimuth.
        """
        cov = self.spec.coverage
        count = int(rng.poisson(self.expected_false_alarms))
        if count == 0:
            return []
        az = np.radians(cov.azimuth_deg[0] + cov.sector_width_deg * rng.random(count))
        az = np.mod(az, 2 * np.pi)
        slant = np.sqrt(rng.uniform(cov.range_min_m**2, cov.range_max_m**2, count))
        times = self.beam.time_at_azimuth(scan, az)
        bearings = compass_to_bearing(az)
        return [
            Clutter(
                state_vector=StateVector([Bearing(float(b)), float(r)]),
                timestamp=datetime.fromtimestamp(float(t), UTC),
                measurement_model=self.measurement_model,
                metadata=self._metadata(scan, float(t), "clutter"),
            )
            for b, r, t in zip(bearings, slant, times, strict=True)
        ]

    def _resolve(self, targets: Sequence[Crossing]) -> list[list[int]]:
        """Group targets closer than the resolution cell in both range and bearing.

        Clusters are single-linkage on the true slant range and azimuth, so
        a chain of close targets merges into one. Returns lists of indices
        into ``targets``, each in time order, clusters ordered by their
        first member.
        """
        res = self.spec.resolution
        n = len(targets)
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        if res.range_m > 0 and res.bearing_deg > 0 and n > 1:
            rng = np.array([c.slant_range for c in targets])
            az = np.array([c.azimuth for c in targets])
            cell_az = np.radians(res.bearing_deg)
            for i in range(n):
                d_az = np.abs((az[i + 1 :] - az[i] + np.pi) % (2 * np.pi) - np.pi)
                close = (np.abs(rng[i + 1 :] - rng[i]) < res.range_m) & (d_az < cell_az)
                for j in np.nonzero(close)[0] + i + 1:
                    parent[find(int(j))] = find(i)
        clusters: dict[int, list[int]] = {}
        for i in range(n):
            clusters.setdefault(find(i), []).append(i)
        return sorted(clusters.values(), key=lambda members: members[0])

    def _metadata(self, scan: int, t: float, kind: str) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "scan": scan,
            "kind": kind,
            "report_time": datetime.fromtimestamp(t, UTC) + timedelta(seconds=self.spec.latency_s),
        }

    def _true_detection(
        self,
        members: Sequence[Crossing],
        noise: FloatArray,
        paths: Mapping[str, GroundTruthPath],
    ) -> TrueDetection:
        """One detection from one target, or from several unresolved ones.

        Unresolved targets give a single plot at the mean of their noisy
        measurements, timed and linked to the first of them; all of them are
        listed in ``merged_segment_ids``.
        """
        first = members[0]
        bearings = np.array([float(compass_to_bearing(c.azimuth)) for c in members]) + noise[:, 0]
        ranges = np.array([c.slant_range for c in members]) + noise[:, 1]
        bearing = Bearing(float(np.arctan2(np.sin(bearings).mean(), np.cos(bearings).mean())))
        slant = max(float(ranges.mean()), 0.0)
        metadata = self._metadata(first.scan, first.t, "target")
        metadata.update(
            {
                "segment_id": first.segment_id,
                "icao24": first.icao24,
                "callsign": first.callsign,
                "true_azimuth_deg": float(np.degrees(first.azimuth)),
                "true_elevation_deg": float(np.degrees(first.elevation)),
                "true_slant_range_m": first.slant_range,
                "true_ground_range_m": first.ground_range,
                "merged_segment_ids": [c.segment_id for c in members],
            }
        )
        return TrueDetection(
            state_vector=StateVector([bearing, slant]),
            timestamp=first.time,
            measurement_model=self.measurement_model,
            groundtruth_path=paths[first.segment_id],
            metadata=metadata,
        )
