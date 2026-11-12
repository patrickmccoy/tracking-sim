"""The rotating 2D surveillance radar.

Per scan, for each aircraft, the radar:

1. finds when its beam crosses the aircraft and samples truth at that moment
   (``BeamSchedule``), not at the scan start;
2. applies the geometry gates: slant range limits, azimuth sector,
   elevation limits, the 4/3-earth radar horizon and, with a terrain
   model, line of sight over the terrain;
3. draws a detection with probability Pd, constant or from the radar
   equation for the aircraft's RCS class and slant range
   (``sensors.detection``);
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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray
from stonesoup.types.angle import Bearing
from stonesoup.types.array import StateVector
from stonesoup.types.detection import Clutter, Detection, TrueDetection
from stonesoup.types.groundtruth import GroundTruthPath

from tracking_sim.geo import LocalFrame
from tracking_sim.geo.terrain import TerrainHorizon
from tracking_sim.sensors.base import GATES, Crossing, GroundSensor, IntArray, ScanResult
from tracking_sim.sensors.detection import AircraftClasses, pd_swerling, snr_radar_equation
from tracking_sim.sensors.measurement import CartesianToBearingSlantRange, compass_to_bearing
from tracking_sim.sensors.scan import BeamSchedule
from tracking_sim.sensors.spec import RotatingRadar2DSpec
from tracking_sim.truth.builder import FlightSegment

if TYPE_CHECKING:
    # Only a type here; importing tracking_sim.sim at runtime would be circular.
    from tracking_sim.sim.rng import SensorStreams

__all__ = ["GATES", "Crossing", "RotatingRadar2D", "ScanResult"]

FloatArray = NDArray[np.float64]


class RotatingRadar2D(GroundSensor):
    """A rotating 2D radar built from a ``RotatingRadar2DSpec`` in a scenario frame.

    ``t_ref`` is when the beam points at the spec's start azimuth; scan
    ``k`` covers ``[t_ref + k * period, t_ref + (k + 1) * period)``.
    """

    spec: RotatingRadar2DSpec

    def __init__(
        self,
        spec: RotatingRadar2DSpec,
        frame: LocalFrame,
        t_ref: datetime,
        *,
        terrain: TerrainHorizon | None = None,
        classes: AircraftClasses | None = None,
    ) -> None:
        super().__init__(
            spec,
            frame,
            t_ref,
            spec.scan.period_s,
            terrain=terrain,
            classes=classes or AircraftClasses(default=spec.detection.default_class),
        )
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

    # -- when the beam sees each aircraft ------------------------------------

    def _observation_times(self, seg: FlightSegment, lo: float, hi: float) -> FloatArray:
        grid = np.append(np.arange(lo, hi, self.period_s / 2), hi)

        def azimuth(t: FloatArray) -> FloatArray:
            pos, _, _ = seg.evaluate(t)
            az, _, _ = self.site.polar(pos)
            return az

        return self.beam.crossings(grid, azimuth(grid), azimuth)

    def _scan_index(self, times: FloatArray) -> IntArray:
        return self.beam.scan_index(times)

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
        pd = np.array([c.pd for c in opportunities])
        hits = streams.detection.random(n) < pd
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

    def pd(self, slant_range_m: Any, rcs_class: str) -> FloatArray:
        """Probability of detection at these slant ranges for one RCS class."""
        det = self.spec.detection
        r = np.asarray(slant_range_m, dtype=float)
        if det.model == "constant":
            assert det.pd is not None
            return np.full(r.shape, det.pd)
        assert det.reference_snr_db is not None and det.reference_range_m is not None
        rcs = getattr(det.rcs_m2, rcs_class)
        snr = snr_radar_equation(r, rcs, det.reference_snr_db, det.reference_range_m)
        return pd_swerling(snr, det.pfa, det.swerling)

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
                "rcs_class": first.rcs_class,
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
