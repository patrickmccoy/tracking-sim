"""The seeded replay engine: truth plus sensors in, detections out.

``ReplayEngine`` replays a ``TruthSet`` past one or more sensors over a time
window. Each sensor's beam starts at its spec's start azimuth at the window
start, and the window is trimmed to whole scans. Scans come out in time
order across sensors (by scan end, then sensor id), each holding every
beam crossing (the detection opportunities, including gated ones), which
were detected, and the detections themselves: Stone Soup ``TrueDetection``
and ``Clutter`` objects ready for a tracker.

Everything random comes from the run ``seed`` through per-sensor streams
(``tracking_sim.sim.rng``), so the same truth, specs, window and seed give
identical detections, and adding a sensor does not change another's.

``SimulationResult`` collects a run and flattens it to Arrow tables for
plotting, statistics and Parquet. Its ``east_m``/``north_m`` columns for a
detection are where a PPI display would draw it: measured slant range laid
along the measured azimuth on the radar's horizontal plane. That is how a
2D radar shows the world, and it puts high aircraft visibly farther out
than they are.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from stonesoup.types.detection import Detection, TrueDetection
from stonesoup.types.groundtruth import GroundTruthPath

from tracking_sim.geo import SensorSite
from tracking_sim.geo.terrain import ElevationModel, TerrainHorizon
from tracking_sim.sensors.base import GroundSensor, ScanResult
from tracking_sim.sensors.detection import AircraftClasses, RcsClass
from tracking_sim.sensors.measurement import bearing_to_compass
from tracking_sim.sensors.passive import PassiveBearingSensor
from tracking_sim.sensors.radar import RotatingRadar2D
from tracking_sim.sensors.spec import AnySensorSpec, RotatingRadar2DSpec
from tracking_sim.sim.rng import sensor_streams
from tracking_sim.truth.builder import TruthSet
from tracking_sim.truth.record import ensure_utc

__all__ = ["DetectionStats", "ReplayEngine", "SimulationResult", "wilson_interval"]


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (95% for z = 1.96)."""
    if trials == 0:
        return 0.0, 1.0
    p = successes / trials
    denom = 1 + z**2 / trials
    centre = (p + z**2 / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z**2 / (4 * trials**2)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class DetectionStats:
    """Per-sensor detection statistics for a run."""

    sensor_id: str
    scans: int
    crossings: int
    opportunities: int
    detected: int
    configured_pd: float  # the mean Pd over the opportunities (constant: the spec's pd)
    false_alarms: int
    expected_false_alarms_per_scan: float
    gated: dict[str, int]
    pd_model: str = "constant"

    @property
    def achieved_pd(self) -> float:
        return self.detected / self.opportunities if self.opportunities else float("nan")

    @property
    def pd_interval(self) -> tuple[float, float]:
        """95% Wilson interval on the achieved Pd."""
        return wilson_interval(self.detected, self.opportunities)

    @property
    def pd_consistent(self) -> bool:
        """True when the configured (or expected) Pd lies inside the 95% interval.

        With the radar equation each opportunity has its own Pd, and the
        count is Poisson-binomial, whose variance is at most the binomial's at
        the same mean, so the interval is, if anything, conservative.
        """
        lo, hi = self.pd_interval
        return lo <= self.configured_pd <= hi

    @property
    def false_alarms_per_scan(self) -> float:
        return self.false_alarms / self.scans if self.scans else float("nan")

    def summary(self) -> str:
        lo, hi = self.pd_interval
        gated = ", ".join(f"{k} {v}" for k, v in self.gated.items() if v) or "none"
        verdict = "consistent" if self.pd_consistent else "NOT consistent"
        label = "configured" if self.pd_model == "constant" else "expected (radar equation)"
        return (
            f"{self.sensor_id}: {self.scans} scans, {self.crossings} beam crossings, "
            f"gated: {gated}\n"
            f"  achieved Pd {self.achieved_pd:.3f} ({self.detected}/{self.opportunities}), "
            f"95% interval [{lo:.3f}, {hi:.3f}], {label} {self.configured_pd:.3f}: {verdict}\n"
            f"  false alarms {self.false_alarms_per_scan:.1f} per scan "
            f"(expected {self.expected_false_alarms_per_scan:.1f})"
        )


@dataclass
class SimulationResult:
    """The output of one run: every scan of every sensor, in time order."""

    seed: int
    start: datetime
    end: datetime
    radars: dict[str, GroundSensor]  # every sensor, radar or not
    scans: list[ScanResult]
    skipped_segments: dict[str, list[str]] = field(default_factory=dict)

    def detections(self, sensor_id: str | None = None) -> list[Detection]:
        """All detections (targets and clutter) in time order."""
        out = [
            d
            for s in self.scans
            if sensor_id is None or s.sensor_id == sensor_id
            for d in s.detections
        ]
        out.sort(key=lambda d: (d.timestamp, d.metadata["sensor_id"]))
        return out

    def stats(self, sensor_id: str) -> DetectionStats:
        radar = self.radars[sensor_id]
        scans = [s for s in self.scans if s.sensor_id == sensor_id]
        gated: dict[str, int] = {}
        crossings = opportunities = detected = false_alarms = 0
        pd_sum = 0.0
        for s in scans:
            crossings += len(s.crossings)
            false_alarms += len(s.clutter)
            for c, hit in zip(s.crossings, s.detected, strict=True):
                if c.gate is None:
                    opportunities += 1
                    detected += hit
                    pd_sum += c.pd
                else:
                    gated[c.gate] = gated.get(c.gate, 0) + 1
        return DetectionStats(
            sensor_id=sensor_id,
            scans=len(scans),
            crossings=crossings,
            opportunities=opportunities,
            detected=detected,
            configured_pd=pd_sum / opportunities if opportunities else float("nan"),
            false_alarms=false_alarms,
            expected_false_alarms_per_scan=radar.expected_false_alarms,
            gated=gated,
            pd_model=radar.spec.detection.model,
        )

    def summary(self) -> str:
        lines = [f"seed {self.seed}, {self.start:%Y-%m-%d %H:%M:%S} to {self.end:%H:%M:%S} UTC"]
        lines += [self.stats(sid).summary() for sid in self.radars]
        for sid, skipped in self.skipped_segments.items():
            if skipped:
                lines.append(f"{sid}: skipped {len(skipped)} segments without altitude")
        return "\n".join(lines)

    # -- tables ------------------------------------------------------------

    def detections_table(self) -> pa.Table:
        """One row per detection, targets and clutter."""
        rows: dict[str, list[Any]] = {k: [] for k in _DETECTION_SCHEMA.names}
        for d in self.detections():
            meta = d.metadata
            radar = self.radars[meta["sensor_id"]]
            bearing = float(d.state_vector[0, 0])
            # Bearing-only sensors measure no range: no PPI position either.
            slant = float(d.state_vector[1, 0]) if d.state_vector.shape[0] > 1 else None
            az = float(bearing_to_compass(bearing))
            ppi = (
                radar.site.to_scenario([[slant * math.sin(az), slant * math.cos(az), 0.0]])[0]
                if slant is not None
                else (None, None)
            )
            is_target = isinstance(d, TrueDetection)
            rows["sensor_id"].append(meta["sensor_id"])
            rows["scan"].append(meta["scan"])
            rows["kind"].append(meta["kind"])
            rows["timestamp"].append(d.timestamp)
            rows["report_time"].append(meta["report_time"])
            rows["bearing_rad"].append(bearing)
            rows["azimuth_deg"].append(math.degrees(az))
            rows["slant_range_m"].append(slant)
            rows["east_m"].append(None if ppi[0] is None else float(ppi[0]))
            rows["north_m"].append(None if ppi[1] is None else float(ppi[1]))
            rows["segment_id"].append(meta.get("segment_id") if is_target else None)
            rows["icao24"].append(meta.get("icao24") if is_target else None)
            rows["merged"].append(len(meta.get("merged_segment_ids", [])) if is_target else 0)
            for key in ("true_azimuth_deg", "true_elevation_deg", "true_slant_range_m"):
                rows[key].append(meta.get(key) if is_target else None)
        return pa.table(rows, schema=_DETECTION_SCHEMA)

    def opportunities_table(self) -> pa.Table:
        """One row per beam crossing: geometry, gate and whether it was detected."""
        rows: dict[str, list[Any]] = {k: [] for k in _OPPORTUNITY_SCHEMA.names}
        for s in self.scans:
            for c, hit in zip(s.crossings, s.detected, strict=True):
                rows["sensor_id"].append(c.sensor_id)
                rows["scan"].append(c.scan)
                rows["timestamp"].append(c.time)
                rows["segment_id"].append(c.segment_id)
                rows["icao24"].append(c.icao24)
                rows["azimuth_deg"].append(math.degrees(c.azimuth))
                rows["elevation_deg"].append(math.degrees(c.elevation))
                rows["slant_range_m"].append(c.slant_range)
                rows["ground_range_m"].append(c.ground_range)
                rows["height_m"].append(c.height_m)
                rows["east_m"].append(float(c.state_vector[0]))
                rows["north_m"].append(float(c.state_vector[2]))
                rows["up_m"].append(float(c.state_vector[4]))
                rows["gate"].append(c.gate)
                rows["in_coverage"].append(c.gate is None)
                rows["detected"].append(bool(hit))
                rows["rcs_class"].append(c.rcs_class)
                rows["pd"].append(c.pd)
        return pa.table(rows, schema=_OPPORTUNITY_SCHEMA)

    def write(self, directory: str | Path) -> dict[str, Path]:
        """Write ``detections.parquet`` and ``opportunities.parquet`` into ``directory``."""
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        paths = {
            "detections": out / "detections.parquet",
            "opportunities": out / "opportunities.parquet",
        }
        pq.write_table(self.detections_table(), paths["detections"])
        pq.write_table(self.opportunities_table(), paths["opportunities"])
        return paths


_TS = pa.timestamp("us", tz="UTC")
_DETECTION_SCHEMA = pa.schema(
    [
        ("sensor_id", pa.string()),
        ("scan", pa.int64()),
        ("kind", pa.string()),
        ("timestamp", _TS),
        ("report_time", _TS),
        ("bearing_rad", pa.float64()),
        ("azimuth_deg", pa.float64()),
        ("slant_range_m", pa.float64()),
        ("east_m", pa.float64()),
        ("north_m", pa.float64()),
        ("segment_id", pa.string()),
        ("icao24", pa.string()),
        ("merged", pa.int64()),
        ("true_azimuth_deg", pa.float64()),
        ("true_elevation_deg", pa.float64()),
        ("true_slant_range_m", pa.float64()),
    ]
)
_OPPORTUNITY_SCHEMA = pa.schema(
    [
        ("sensor_id", pa.string()),
        ("scan", pa.int64()),
        ("timestamp", _TS),
        ("segment_id", pa.string()),
        ("icao24", pa.string()),
        ("azimuth_deg", pa.float64()),
        ("elevation_deg", pa.float64()),
        ("slant_range_m", pa.float64()),
        ("ground_range_m", pa.float64()),
        ("height_m", pa.float64()),
        ("east_m", pa.float64()),
        ("north_m", pa.float64()),
        ("up_m", pa.float64()),
        ("gate", pa.string()),
        ("in_coverage", pa.bool_()),
        ("detected", pa.bool_()),
        ("rcs_class", pa.string()),
        ("pd", pa.float64()),
    ]
)


class ReplayEngine:
    """Replay a truth set past sensors, deterministically for a given seed.

    ``truth_step_s`` is the spacing of the Stone Soup ``GroundTruthPath``
    states that detections link to (built once per engine). With a
    ``terrain`` model, each sensor also masks targets behind terrain (unless
    its spec turns ``terrain_masking`` off). ``aircraft_classes`` maps ICAO
    addresses to RCS classes for the radar-equation Pd, ahead of the ADS-B
    emitter category.
    """

    def __init__(
        self,
        truth: TruthSet,
        sensors: Sequence[AnySensorSpec],
        *,
        seed: int,
        truth_step_s: float = 1.0,
        terrain: ElevationModel | None = None,
        aircraft_classes: Mapping[str, RcsClass] | None = None,
    ) -> None:
        ids = [s.id for s in sensors]
        if len(set(ids)) != len(ids):
            raise ValueError(f"sensor ids must be unique: {ids}")
        if not sensors:
            raise ValueError("need at least one sensor")
        self.truth = truth
        self.specs = list(sensors)
        self.seed = seed
        self.truth_step_s = truth_step_s
        self.terrain = terrain
        self.aircraft_classes = dict(aircraft_classes or {})
        self._paths: dict[str, GroundTruthPath] | None = None
        self._horizons: dict[str, TerrainHorizon] = {}

    @property
    def paths(self) -> dict[str, GroundTruthPath]:
        if self._paths is None:
            self._paths = {p.id: p for p in self.truth.ground_truth_paths(step_s=self.truth_step_s)}
        return self._paths

    def _window(self, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
        span = self.truth.span
        if start is None or end is None:
            if span is None:
                raise ValueError("the truth set is empty; give start and end")
            start = span[0] if start is None else start
            end = span[1] if end is None else end
        lo, hi = ensure_utc(start), ensure_utc(end)
        if hi <= lo:
            raise ValueError("end must be after start")
        return lo, hi

    def scans(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[ScanResult]:
        """Yield every sensor's scans in time order (by scan end, then sensor id)."""
        lo, hi = self._window(start, end)
        streams = [self._sensor_scans(spec, lo, hi) for spec in self.specs]
        yield from heapq.merge(*streams, key=lambda s: (s.end, s.sensor_id))

    def horizon(self, spec: AnySensorSpec) -> TerrainHorizon | None:
        """The sensor's terrain horizon (built once), if there is terrain and it masks."""
        if self.terrain is None or not spec.coverage.terrain_masking:
            return None
        if spec.id not in self._horizons:
            pos = spec.position
            site = SensorSite(self.truth.frame, pos.lat, pos.lon, pos.alt_m, pos.mast_m)
            self._horizons[spec.id] = TerrainHorizon(
                site, self.terrain, spec.coverage.range_max_m, k=spec.coverage.refraction_k
            )
        return self._horizons[spec.id]

    def _radar(self, spec: AnySensorSpec, start: datetime) -> GroundSensor:
        """The sensor model for ``spec`` (a radar or a passive sensor)."""
        if isinstance(spec, RotatingRadar2DSpec):
            classes = AircraftClasses(self.aircraft_classes, default=spec.detection.default_class)
            return RotatingRadar2D(
                spec, self.truth.frame, start, terrain=self.horizon(spec), classes=classes
            )
        return PassiveBearingSensor(
            spec,
            self.truth.frame,
            start,
            terrain=self.horizon(spec),
            classes=AircraftClasses(self.aircraft_classes),
        )

    def _whole_scans(self, sensor: GroundSensor, start: datetime, end: datetime) -> int:
        return int((end - start).total_seconds() // sensor.period_s + 1e-9)

    def _sensor_scans(
        self, spec: AnySensorSpec, start: datetime, end: datetime
    ) -> Iterator[ScanResult]:
        radar = self._radar(spec, start)
        n_scans = self._whole_scans(radar, start, end)
        stop = start + timedelta(seconds=n_scans * radar.period_s)
        crossings, _ = radar.crossings(self.truth, start, stop)
        by_scan: dict[int, list[Any]] = {}
        for c in crossings:
            by_scan.setdefault(c.scan, []).append(c)
        streams = sensor_streams(self.seed, spec.id)
        for k in range(n_scans):
            yield radar.observe(k, by_scan.get(k, []), streams, self.paths)

    def run(self, start: datetime | None = None, end: datetime | None = None) -> SimulationResult:
        """Run the whole window and collect the result."""
        lo, hi = self._window(start, end)
        radars = {spec.id: self._radar(spec, lo) for spec in self.specs}
        skipped = {
            spec.id: [s.segment_id for s in self.truth if not s.has_altitude] for spec in self.specs
        }
        scans = list(self.scans(lo, hi))
        ends = [s.end for s in scans]
        return SimulationResult(
            seed=self.seed,
            start=lo,
            end=max(ends) if ends else lo,
            radars=radars,
            scans=scans,
            skipped_segments=skipped,
        )
