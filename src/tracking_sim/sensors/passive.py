"""A passive bearing-only sensor.

A staring receiver (an ESM system, or a direction finder listening to
aircraft transponders) hears every aircraft in its coverage at once, so it
reports on a fixed clock: at every update (``update_period_s``, 1 s by
default) each aircraft that passes the geometry gates is an opportunity,
sampled at the update time. It measures only bearing; range and height are
not observable from one passive sensor, which is why bearing-only tracking
and fusion with a radar wait for milestone 6.

Pd is constant, or from a one-way link (SNR falls with range squared, not
to the fourth power as for a radar echo), for a steady emitter by default.
False bearings are a Poisson number per update, uniform over the sector.
Randomness uses the same per-sensor streams as the radar: one uniform per
opportunity for detection, one normal per opportunity for noise, and the
clutter stream for false bearings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

import numpy as np
from stonesoup.types.angle import Bearing
from stonesoup.types.array import StateVector
from stonesoup.types.detection import Clutter, Detection, TrueDetection
from stonesoup.types.groundtruth import GroundTruthPath

from tracking_sim.geo import LocalFrame
from tracking_sim.geo.terrain import TerrainHorizon
from tracking_sim.sensors.base import Crossing, FloatArray, GroundSensor, IntArray, ScanResult
from tracking_sim.sensors.detection import AircraftClasses, pd_swerling
from tracking_sim.sensors.measurement import CartesianToSensorBearing, compass_to_bearing
from tracking_sim.sensors.spec import PassiveBearingSpec
from tracking_sim.truth.builder import FlightSegment

if TYPE_CHECKING:
    from tracking_sim.sim.rng import SensorStreams

__all__ = ["PassiveBearingSensor"]


class PassiveBearingSensor(GroundSensor):
    """A staring bearing-only sensor built from a ``PassiveBearingSpec``.

    Update ``k`` happens at ``t_ref + k * update_period_s`` and its window
    runs to the next update.
    """

    spec: PassiveBearingSpec

    def __init__(
        self,
        spec: PassiveBearingSpec,
        frame: LocalFrame,
        t_ref: datetime,
        *,
        terrain: TerrainHorizon | None = None,
        classes: AircraftClasses | None = None,
    ) -> None:
        super().__init__(spec, frame, t_ref, spec.update_period_s, terrain=terrain, classes=classes)
        self.bearing_sigma = float(np.radians(spec.noise.bearing_sigma_deg))
        self.measurement_model = CartesianToSensorBearing(
            ndim_state=6,
            mapping=(0, 2, 4),
            noise_covar=np.array([[self.bearing_sigma**2]]),
            translation_offset=StateVector(self.site.position),
            frame_rotation=self.site.rotation,
        )

    # -- a look at every update ---------------------------------------------

    def _observation_times(self, seg: FlightSegment, lo: float, hi: float) -> FloatArray:
        t_ref, period = self.t_ref.timestamp(), self.period_s
        first = int(np.ceil((lo - t_ref) / period - 1e-9))
        last = int(np.floor((hi - t_ref) / period + 1e-9))
        times: FloatArray = t_ref + period * np.arange(first, last + 1, dtype=float)
        return times

    def _scan_index(self, times: FloatArray) -> IntArray:
        index: IntArray = np.round((times - self.t_ref.timestamp()) / self.period_s).astype(
            np.int64
        )
        return index

    def pd(self, slant_range_m: Any, rcs_class: str) -> FloatArray:
        det = self.spec.detection
        r = np.asarray(slant_range_m, dtype=float)
        if det.model == "constant":
            assert det.pd is not None
            return np.full(r.shape, det.pd)
        assert det.reference_snr_db is not None and det.reference_range_m is not None
        snr = 10 ** (det.reference_snr_db / 10) * (det.reference_range_m / np.maximum(r, 1.0)) ** 2
        return pd_swerling(snr, det.pfa, det.swerling)

    @property
    def expected_false_alarms(self) -> float:
        return self.spec.detection.false_alarm_rate

    # -- observation (random) ----------------------------------------------

    def observe(
        self,
        scan: int,
        crossings: Sequence[Crossing],
        streams: SensorStreams,
        paths: Mapping[str, GroundTruthPath],
    ) -> ScanResult:
        """Turn one update's looks into bearing detections, plus false bearings."""
        start, end = self.scan_window(scan)
        opportunities = [c for c in crossings if c.in_coverage]
        n = len(opportunities)
        pd = np.array([c.pd for c in opportunities])
        hits = streams.detection.random(n) < pd
        noise = streams.noise.standard_normal(n) * self.bearing_sigma
        detections: list[Detection] = [
            self._true_detection(c, float(nb), paths)
            for c, hit, nb in zip(opportunities, hits, noise, strict=True)
            if hit
        ]
        detections.extend(self._false_bearings(scan, streams.clutter))
        detections.sort(key=lambda d: d.timestamp)
        hit_ids = {(c.segment_id, c.t) for c, hit in zip(opportunities, hits, strict=True) if hit}
        return ScanResult(
            sensor_id=self.sensor_id,
            scan=scan,
            start=start,
            end=end,
            crossings=list(crossings),
            detected=[(c.segment_id, c.t) in hit_ids for c in crossings],
            detections=detections,
        )

    def _true_detection(
        self, c: Crossing, noise: float, paths: Mapping[str, GroundTruthPath]
    ) -> TrueDetection:
        metadata = self._metadata(c.scan, c.t, "target")
        metadata.update(
            {
                "segment_id": c.segment_id,
                "icao24": c.icao24,
                "callsign": c.callsign,
                "true_azimuth_deg": float(np.degrees(c.azimuth)),
                "true_elevation_deg": float(np.degrees(c.elevation)),
                "true_slant_range_m": c.slant_range,
                "true_ground_range_m": c.ground_range,
                "merged_segment_ids": [c.segment_id],
            }
        )
        return TrueDetection(
            state_vector=StateVector([Bearing(float(compass_to_bearing(c.azimuth)) + noise)]),
            timestamp=c.time,
            measurement_model=self.measurement_model,
            groundtruth_path=paths[c.segment_id],
            metadata=metadata,
        )

    def _false_bearings(self, scan: int, rng: np.random.Generator) -> list[Detection]:
        cov = self.spec.coverage
        count = int(rng.poisson(self.expected_false_alarms))
        if count == 0:
            return []
        az = np.mod(
            np.radians(cov.azimuth_deg[0] + cov.sector_width_deg * rng.random(count)), 2 * np.pi
        )
        t, _ = self.scan_window(scan)
        return [
            Clutter(
                state_vector=StateVector([Bearing(float(compass_to_bearing(a)))]),
                timestamp=t,
                measurement_model=self.measurement_model,
                metadata=self._metadata(scan, t.timestamp(), "clutter"),
            )
            for a in az
        ]
