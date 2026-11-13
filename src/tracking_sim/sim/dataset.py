"""Truth and one radar's detections, ready to feed Stone Soup by hand.

``run_scenario`` builds and runs trackers from specs. Notebooks that build
Stone Soup components themselves need the pieces instead: the truth, the
radar and its measurement model, its detections scan by scan, and a way to
score whatever tracks they produce. ``TrackingData`` holds those, from
either source:

* ``simulate``: replay truth past a radar spec now;
* ``load_run``: read a stored run's detections back (``eval.runstore``),
  rebuilding each as the Stone Soup detection the run's trackers saw.

Both give the same object, so everything downstream is the same. Only
rotating 2D radars are kept: trackers take radar detections until fusion
(milestone 6).

``subset`` cuts the data down in time and to the neighbourhood of one
aircraft, for the association methods whose cost grows quickly with the
number of tracks and detections (JPDA, multi-frame assignment).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import yaml
from numpy.typing import NDArray
from stonesoup.types.angle import Bearing
from stonesoup.types.array import StateVector
from stonesoup.types.detection import Clutter, Detection, TrueDetection
from stonesoup.types.groundtruth import GroundTruthPath
from stonesoup.types.hypothesis import SingleHypothesis
from stonesoup.types.multihypothesis import MultipleHypothesis
from stonesoup.types.state import State
from stonesoup.types.track import Track
from stonesoup.types.update import Update

from tracking_sim.eval.metrics import MetricSettings, TrackerScore, score_tracks
from tracking_sim.eval.scoring import ScoringGrid, grid_from_looks
from tracking_sim.sensors import RotatingRadar2DSpec, parse_sensor_spec
from tracking_sim.sensors.base import GroundSensor
from tracking_sim.sensors.measurement import bearing_to_compass
from tracking_sim.sim.engine import ReplayEngine, SimulationResult
from tracking_sim.truth.builder import TruthSet

__all__ = ["Scan", "TrackingData", "load_run", "simulate", "tracks_following"]

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class Scan:
    """One radar scan: its window and the detections made in it."""

    scan: int
    start: datetime
    end: datetime
    detections: tuple[Detection, ...]


@dataclass(frozen=True)
class Looks:
    """When, and where, one aircraft was in the radar's coverage."""

    t: FloatArray  # POSIX seconds
    xy: FloatArray  # (n, 2) east, north in the scenario frame, metres

    def take(self, keep: NDArray[np.bool_]) -> Looks:
        return Looks(self.t[keep], self.xy[keep])


@dataclass
class TrackingData:
    """Truth, one radar and its detections, scan by scan.

    ``looks`` holds each aircraft's detection opportunities (when the radar
    could have seen it), which is what scoring counts as visible truth.
    ``stored_tracks`` holds, for a loaded run, the tracks its own trackers
    produced, by tracker id.
    """

    label: str
    truth: TruthSet
    sensor: GroundSensor
    scans: list[Scan]
    looks: dict[str, Looks]
    paths: dict[str, GroundTruthPath]
    stored_tracks: dict[str, pa.Table] = field(default_factory=dict)
    area: tuple[str, float] | None = None  # a subset's (aircraft, radius_m)

    # -- what the data is ----------------------------------------------------

    @property
    def frame(self) -> Any:
        return self.truth.frame

    @property
    def sensor_id(self) -> str:
        return self.sensor.sensor_id

    @property
    def measurement_model(self) -> Any:
        """The radar's Stone Soup measurement model (bearing, slant range)."""
        return self.sensor.measurement_model

    @property
    def period_s(self) -> float:
        return self.sensor.period_s

    @property
    def start(self) -> datetime:
        return self.scans[0].start

    @property
    def end(self) -> datetime:
        return self.scans[-1].end

    @property
    def pd(self) -> float | None:
        """The radar's configured Pd, if it is constant."""
        det = self.sensor.spec.detection
        return float(det.pd) if det.model == "constant" else None

    @property
    def false_alarms_per_scan(self) -> float:
        return float(self.sensor.expected_false_alarms)

    def clutter_density(self) -> float:
        """Mean false alarms per unit of measurement space, per scan.

        Stone Soup's probabilistic hypothesisers (PDA, JPDA, MFA) want the
        clutter density in the measurement's own units: here per radian of
        bearing per metre of slant range. False alarms are uniform over
        ground area, so in (bearing, range) they thin out towards the radar;
        this is the average over the coverage.
        """
        cov = self.sensor.spec.coverage
        span = math.radians(cov.sector_width_deg) * (cov.range_max_m - cov.range_min_m)
        return float(self.false_alarms_per_scan / span)

    def __repr__(self) -> str:
        n = sum(len(s.detections) for s in self.scans)
        return (
            f"TrackingData({self.label!r}: {self.sensor_id}, {len(self.scans)} scans, "
            f"{n} detections, {len(self.looks)} aircraft in coverage)"
        )

    # -- detections ----------------------------------------------------------

    def batches(self) -> list[tuple[datetime, set[Detection]]]:
        """``(scan end, detections)`` per scan: what a Stone Soup tracker reads."""
        latency = timedelta(seconds=self.sensor.spec.latency_s)
        ordered = sorted(self.scans, key=lambda s: s.end + latency)
        return [(s.end, set(s.detections)) for s in ordered]

    def detections(self) -> list[Detection]:
        """Every detection, targets and clutter, in time order."""
        out = [d for s in self.scans for d in s.detections]
        out.sort(key=lambda d: d.timestamp)
        return out

    def detections_of(self, segment_id: str) -> list[TrueDetection]:
        """One aircraft's detections, in time order.

        A detection of two unresolved aircraft belongs to the one whose
        echo it is centred on (its ``segment_id``).
        """
        return [
            d
            for d in self.detections()
            if isinstance(d, TrueDetection) and d.metadata.get("segment_id") == segment_id
        ]

    def plot_positions(self, detections: Sequence[Detection]) -> FloatArray:
        """(n, 2) east, north of detections as a radar display shows them.

        A 2D radar knows bearing and slant range but not height, so its
        display lays slant range along the ground: high aircraft appear a
        little further out than they are.
        """
        if not detections:
            return np.zeros((0, 2))
        bearing = np.array([float(d.state_vector[0, 0]) for d in detections])
        slant = np.array([float(d.state_vector[1, 0]) for d in detections])
        az = bearing_to_compass(bearing)
        local = np.column_stack([slant * np.sin(az), slant * np.cos(az), np.zeros_like(slant)])
        positions: FloatArray = self.sensor.site.to_scenario(local)[:, :2]
        return positions

    # -- truth ---------------------------------------------------------------

    def truth_path(self, segment_id: str) -> GroundTruthPath:
        """One aircraft's truth as a Stone Soup path (1 s states)."""
        return self.paths[segment_id]

    def aircraft(self) -> pa.Table:
        """Every aircraft the radar detected, most detections first.

        ``turn_deg_s`` is how hard it manoeuvred while detected: the 95th
        percentile of its turn rate over 10 s steps (a single noisy
        heading can't dominate it).
        """
        counts: dict[str, int] = {}
        times: dict[str, list[datetime]] = {}
        for d in self.detections():
            if isinstance(d, TrueDetection):
                seg = d.metadata["segment_id"]
                counts[seg] = counts.get(seg, 0) + 1
                times.setdefault(seg, []).append(d.timestamp)
        rows: dict[str, list[Any]] = {
            k: []
            for k in (
                "segment_id",
                "icao24",
                "callsign",
                "detections",
                "looks",
                "first",
                "last",
                "minutes",
                "speed_mps",
                "height_m",
                "turn_deg_s",
            )
        }
        for seg_id in sorted(counts, key=lambda s: (-counts[s], s)):
            seg = self.truth.segment(seg_id)
            first, last = min(times[seg_id]), max(times[seg_id])
            # Offsets from the first detection: 1e-9 added to POSIX seconds (~1.7e9)
            # rounds away, so an aircraft detected once would get no samples at all.
            span = (last - first).total_seconds()
            t = first.timestamp() + np.arange(0.0, span + 1e-6, 10.0)
            pos, vel, ok = seg.evaluate(t)
            pos, vel = pos[ok], vel[ok]
            heading = np.unwrap(np.arctan2(vel[:, 0], vel[:, 1]))
            turn = np.abs(np.diff(heading)) / 10.0 if len(heading) > 1 else np.zeros(1)
            rows["segment_id"].append(seg_id)
            rows["icao24"].append(seg.icao24)
            rows["callsign"].append(seg.callsign)
            rows["detections"].append(counts[seg_id])
            rows["looks"].append(
                len(self.looks.get(seg_id, Looks(np.zeros(0), np.zeros((0, 2)))).t)
            )
            rows["first"].append(first)
            rows["last"].append(last)
            rows["minutes"].append((last - first).total_seconds() / 60)
            rows["speed_mps"].append(float(np.median(np.hypot(vel[:, 0], vel[:, 1]))))
            rows["height_m"].append(float(np.median(pos[:, 2])))
            rows["turn_deg_s"].append(float(np.degrees(np.percentile(turn, 95))))
        return pa.table(rows)

    # -- cutting it down -----------------------------------------------------

    def subset(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        around: str | None = None,
        radius_m: float | None = None,
    ) -> TrackingData:
        """Whole scans in ``[start, end)``, optionally near one aircraft.

        With ``around`` (a segment id) and ``radius_m``, detections and looks
        are kept only within ``radius_m`` of that aircraft's true position at
        their own time: a window that moves with it, so neighbours come and
        go as they would in a tracker's local picture. Clutter counts the
        same way.
        """
        lo = start or self.start
        hi = end or self.end
        scans = [s for s in self.scans if s.start >= lo and s.end <= hi]
        if not scans:
            raise ValueError("no whole scans in that window")
        lo, hi = scans[0].start, scans[-1].end
        looks = {}
        for seg, look in self.looks.items():
            keep = (look.t >= lo.timestamp()) & (look.t < hi.timestamp())
            if keep.any():
                looks[seg] = look.take(keep)
        label = f"{self.label}, {lo:%H:%M:%S}-{hi:%H:%M:%S}"
        if around is not None:
            if radius_m is None:
                raise ValueError("give radius_m with around")
            centre = self.truth.segment(around)

            def near(t: FloatArray, xy: FloatArray) -> NDArray[np.bool_]:
                pos, _, ok = centre.evaluate(t)
                dist = np.hypot(xy[:, 0] - pos[:, 0], xy[:, 1] - pos[:, 1])
                within: NDArray[np.bool_] = ok & (dist <= radius_m)
                return within

            trimmed = []
            for s in scans:
                dets = list(s.detections)
                if dets:
                    t = np.array([d.timestamp.timestamp() for d in dets])
                    keep = near(t, self.plot_positions(dets))
                    dets = [d for d, k in zip(dets, keep, strict=True) if k]
                trimmed.append(replace(s, detections=tuple(dets)))
            scans = trimmed
            looks = {seg: look.take(near(look.t, look.xy)) for seg, look in looks.items()}
            looks = {seg: look for seg, look in looks.items() if len(look.t)}
            label += f", within {radius_m / 1000:g} km of {around}"
            return replace(
                self, label=label, scans=scans, looks=looks, area=(around, float(radius_m))
            )
        return replace(self, label=label, scans=scans, looks=looks)

    def clip(
        self, tracks: Iterable[Track], position_mapping: tuple[int, int, int] = (0, 2, 4)
    ) -> list[Track]:
        """Tracks cut to this data's window (and area, for a subset near one aircraft).

        Tracks made from other detections (a stored run's, over its whole
        coverage) can then be scored on the same footing as tracks made from
        this subset. A track that leaves the area and comes back is split,
        so scoring never bridges the gap.
        """
        lo, hi = self.start.timestamp(), self.end.timestamp()
        ix, iy = position_mapping[0], position_mapping[1]
        centre = self.truth.segment(self.area[0]) if self.area else None
        out = []
        for track in tracks:
            states = list(track.states)
            if not states:
                continue
            t = np.array([s.timestamp.timestamp() for s in states])
            keep = (t >= lo) & (t < hi)
            if centre is not None and self.area is not None:
                means = np.array(
                    [
                        np.asarray(getattr(s, "mean", s.state_vector), dtype=float).ravel()[
                            [ix, iy]
                        ]
                        for s in states
                    ]
                )
                pos, _, ok = centre.evaluate(t)
                dist = np.hypot(means[:, 0] - pos[:, 0], means[:, 1] - pos[:, 1])
                keep &= ok & (dist <= self.area[1])
            run: list[Any] = []
            pieces = 0
            for state, k in zip(states, keep, strict=True):
                if k:
                    run.append(state)
                elif run:
                    pieces += 1
                    out.append(Track(run, id=f"{track.id}/{pieces}"))
                    run = []
            if run:
                pieces += 1
                out.append(Track(run, id=f"{track.id}/{pieces}"))
        return out

    # -- scoring -------------------------------------------------------------

    def grid(self) -> ScoringGrid:
        """The evaluation grid (scan ends) and the truth visible on it."""
        looks = {
            seg: [datetime.fromtimestamp(t, UTC) for t in look.t]
            for seg, look in self.looks.items()
        }
        return grid_from_looks(self.truth, [s.end for s in self.scans], self.period_s, looks)

    def score(
        self,
        name: str,
        tracks: Iterable[Track],
        *,
        position_mapping: tuple[int, int, int] = (0, 2, 4),
        velocity_mapping: tuple[int, int, int] = (1, 3, 5),
        elapsed_s: float = float("nan"),
        settings: MetricSettings | None = None,
        grid: ScoringGrid | None = None,
    ) -> TrackerScore:
        """OSPA, GOSPA and SIAP for Stone Soup tracks of any state layout."""
        grid = grid or self.grid()
        sampled = grid.sample(list(tracks), position_mapping, velocity_mapping)
        return score_tracks(name, sampled, grid, elapsed_s=elapsed_s, settings=settings)

    def stored(self, tracker_id: str) -> list[Track]:
        """A loaded run's tracks from one of its trackers, as 6D states.

        Only confirmed states: what that tracker reported.
        """
        table = self.stored_tracks[tracker_id].filter(pc.field("confirmed"))
        out: dict[str, list[State]] = {}
        cols = {k: table[k].to_pylist() for k in table.column_names}
        for i, track_id in enumerate(cols["track_id"]):
            vector = [
                cols[k][i]
                for k in ("east_m", "v_east_mps", "north_m", "v_north_mps", "up_m", "v_up_mps")
            ]
            state = State(StateVector(vector), timestamp=cols["timestamp"][i])
            out.setdefault(track_id, []).append(state)
        return [Track(states, id=track_id) for track_id, states in out.items()]

    # -- building one ----------------------------------------------------------

    @classmethod
    def from_simulation(
        cls,
        truth: TruthSet,
        simulation: SimulationResult,
        paths: Mapping[str, GroundTruthPath],
        *,
        sensor_id: str | None = None,
        label: str = "simulated",
    ) -> TrackingData:
        """Take one radar's scans out of a simulation (default: its first radar)."""
        sensor = _pick_radar(simulation.radars, sensor_id)
        scans = [
            Scan(s.scan, s.start, s.end, tuple(s.detections))
            for s in simulation.scans
            if s.sensor_id == sensor.sensor_id
        ]
        looks: dict[str, tuple[list[float], list[tuple[float, float]]]] = {}
        for s in simulation.scans:
            if s.sensor_id != sensor.sensor_id:
                continue
            for c in s.opportunities:
                t, xy = looks.setdefault(c.segment_id, ([], []))
                t.append(c.t)
                xy.append((float(c.state_vector[0]), float(c.state_vector[2])))
        return cls(
            label=label,
            truth=truth,
            sensor=sensor,
            scans=scans,
            looks={k: Looks(np.array(t), np.array(xy)) for k, (t, xy) in looks.items()},
            paths=dict(paths),
        )


def _pick_radar(sensors: Mapping[str, GroundSensor], sensor_id: str | None) -> GroundSensor:
    radars = {k: s for k, s in sensors.items() if isinstance(s.spec, RotatingRadar2DSpec)}
    if sensor_id is not None:
        if sensor_id not in radars:
            raise KeyError(f"no rotating 2D radar {sensor_id!r}; radars: {sorted(radars)}")
        return radars[sensor_id]
    if not radars:
        raise ValueError("no rotating 2D radar: trackers need one")
    return next(iter(radars.values()))


def simulate(
    truth: TruthSet,
    sensor: RotatingRadar2DSpec,
    *,
    seed: int,
    start: datetime | None = None,
    end: datetime | None = None,
    terrain: Any = None,
    aircraft_classes: Mapping[str, Any] | None = None,
) -> TrackingData:
    """Replay ``truth`` past one radar and keep what a tracker needs."""
    engine = ReplayEngine(
        truth, [sensor], seed=seed, terrain=terrain, aircraft_classes=aircraft_classes
    )
    simulation = engine.run(start, end)
    return TrackingData.from_simulation(
        truth, simulation, engine.paths, label=f"simulated, seed {seed}"
    )


def load_run(
    run: str | Path, *, store: str | Path = "runs", sensor_id: str | None = None
) -> TrackingData:
    """A stored run's truth and radar detections, as Stone Soup objects.

    ``run`` is a run directory, or a run id (or unique prefix) in ``store``.
    Truth is rebuilt from the run's truth file, which must be unchanged
    (its digest is checked). Detections are read from the run's table and
    rebuilt with the radar's measurement model, so they are exactly what
    the run's trackers saw; the radar is not simulated again.
    """
    from tracking_sim.eval.runstore import RunStore, input_digest
    from tracking_sim.sim.scenario import Scenario, ScenarioSpec, load_truth
    from tracking_sim.tracking import parse_tracker_spec

    path = Path(run)
    if not (path / "manifest.json").exists():
        path = RunStore(store).get(str(run)).path
    manifest = json.loads((path / "manifest.json").read_text())
    resolved = yaml.safe_load((path / "scenario.resolved.yaml").read_text())
    spec_data = {k: v for k, v in resolved.items() if k not in ("truth_path", "frame")}
    # The run's paths were relative to its scenario file; the resolved one is absolute.
    # (Aircraft classes only shaped Pd, which the stored detections already reflect.)
    spec_data["truth"] = {
        **spec_data["truth"],
        "path": resolved["truth_path"],
        "aircraft_classes": None,
    }
    scenario = Scenario(
        spec=ScenarioSpec.model_validate(spec_data),
        base_dir=path,
        sensors=[parse_sensor_spec(s) for s in resolved["sensors"]],
        trackers=[parse_tracker_spec(t) for t in resolved["trackers"]],
    )
    digest = input_digest(scenario.truth_path)
    if digest != manifest["inputs"]["truth"]:
        raise ValueError(f"{scenario.truth_path} has changed since run {manifest['id']}")
    truth = load_truth(scenario)

    engine = ReplayEngine(truth, scenario.sensors, seed=scenario.spec.seed)
    window = scenario.spec.truth.window
    lo, hi = engine.window(*(window or (None, None)))
    sensors = {s.id: engine.sensor(s, lo) for s in scenario.sensors}
    sensor = _pick_radar(sensors, sensor_id)
    paths = engine.paths

    table = pq.read_table(path / "detections.parquet")
    by_scan: dict[int, list[Detection]] = {}
    model = sensor.measurement_model
    for row in table.to_pylist():
        if row["sensor_id"] != sensor.sensor_id:
            continue
        vector = StateVector([Bearing(row["bearing_rad"]), row["slant_range_m"]])
        metadata = {
            "sensor_id": row["sensor_id"],
            "scan": row["scan"],
            "kind": row["kind"],
            "report_time": row["report_time"],
        }
        detection: Detection
        if row["kind"] == "target":
            metadata.update(
                {
                    "segment_id": row["segment_id"],
                    "icao24": row["icao24"],
                    "true_azimuth_deg": row["true_azimuth_deg"],
                    "true_elevation_deg": row["true_elevation_deg"],
                    "true_slant_range_m": row["true_slant_range_m"],
                }
            )
            detection = TrueDetection(
                vector,
                timestamp=row["timestamp"],
                measurement_model=model,
                groundtruth_path=paths[row["segment_id"]],
                metadata=metadata,
            )
        else:
            detection = Clutter(
                vector, timestamp=row["timestamp"], measurement_model=model, metadata=metadata
            )
        by_scan.setdefault(row["scan"], []).append(detection)

    n_scans = int((hi - lo).total_seconds() // sensor.period_s + 1e-9)
    scans = []
    for k in range(n_scans):
        start, end = sensor.scan_window(k)
        dets = sorted(by_scan.get(k, []), key=lambda d: d.timestamp)
        scans.append(Scan(k, start, end, tuple(dets)))

    opp = pq.read_table(path / "opportunities.parquet").to_pydict()
    looks: dict[str, tuple[list[float], list[tuple[float, float]]]] = {}
    for i, sid in enumerate(opp["sensor_id"]):
        if sid == sensor.sensor_id and opp["in_coverage"][i]:
            t, xy = looks.setdefault(opp["segment_id"][i], ([], []))
            t.append(opp["timestamp"][i].timestamp())
            xy.append((opp["east_m"][i], opp["north_m"][i]))

    stored = {p.stem: pq.read_table(p) for p in sorted((path / "tracks").glob("*.parquet"))}
    return TrackingData(
        label=f"run {manifest['id']}",
        truth=truth,
        sensor=sensor,
        scans=scans,
        looks={k: Looks(np.array(t), np.array(xy)) for k, (t, xy) in looks.items()},
        paths=dict(paths),
        stored_tracks=stored,
    )


def tracks_following(
    tracks: Iterable[Track], segment_id: str, *, min_share: float = 0.5
) -> list[Track]:
    """The tracks that mostly followed one aircraft, longest first.

    A track follows the aircraft if at least ``min_share`` of its updates
    used that aircraft's detections. For a probabilistic tracker (PDA,
    JPDA, MFA) an update weighs several detections; its most likely one
    counts.
    """
    out = []
    for track in tracks:
        mine = total = 0
        for state in track.states:
            if not isinstance(state, Update):
                continue
            detection = _main_detection(state.hypothesis)
            if detection is None:
                continue
            total += 1
            if isinstance(detection, TrueDetection) and (
                detection.metadata.get("segment_id") == segment_id
            ):
                mine += 1
        if total and mine / total >= min_share:
            out.append(track)
    return sorted(out, key=lambda t: -len(t.states))


def _main_detection(hypothesis: Any) -> Detection | None:
    if isinstance(hypothesis, SingleHypothesis):
        return hypothesis.measurement if hypothesis else None
    if isinstance(hypothesis, MultipleHypothesis):
        real = [h for h in hypothesis if h]
        if not real:
            return None
        best = max(real, key=lambda h: float(getattr(h, "weight", getattr(h, "probability", 0))))
        found: Detection = best.measurement
        return found
    return None
