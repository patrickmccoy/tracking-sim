"""Truth and tracks on a common grid, ready for Stone Soup's metrics.

Stone Soup's OSPA, GOSPA and SIAP generators compare tracks and truth at
timestamps they share. Here they never would: truth is sampled at beam
crossings and tracks at detection times and scan ends. So both are put on
one evaluation grid, the end of every scan of the first sensor:

* **Truth** counts at a grid time only while the radar could see the
  aircraft: if it had a detection opportunity (a beam crossing that passed
  every geometry gate) during the scan ending then. Aircraft out of range,
  outside the sector, too high in elevation or below the horizon are left
  out, so no tracker is penalised for what the radar could never see. Its
  position is the true state at the grid time. Each continuous stretch of
  visibility becomes its own truth path, ``<segment id>#<n>``: Stone Soup's
  SIAP assumes a truth exists throughout any track association with it, and
  an aircraft re-entering coverage is, to the radar, a new object anyway.
* **Tracks** count from their confirmation until their last state. At a
  grid time a track reports its latest state extrapolated at constant
  velocity to that time, which is what a tracker would put on a display.

Both come out as 6D ``[x, vx, y, vy, z, vz]`` states, whatever the tracker's
internal layout; metrics then measure horizontal position (x, y).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from stonesoup.types.groundtruth import GroundTruthPath, GroundTruthState
from stonesoup.types.state import State
from stonesoup.types.track import Track

from tracking_sim.sim.engine import SimulationResult
from tracking_sim.tracking.runner import TrackerRun
from tracking_sim.truth.builder import TruthSet

__all__ = ["ScoringGrid", "grid_from_looks", "scoring_grid", "segment_of"]


def segment_of(truth_id: str) -> str:
    """The truth segment a scoring path came from (``"a1b2c3-0#2"`` -> ``"a1b2c3-0"``)."""
    return truth_id.split("#", 1)[0]


@dataclass
class ScoringGrid:
    """Evaluation times and the truth visible at each of them."""

    times: list[datetime]
    truths: list[GroundTruthPath]
    period_s: float

    def visible(self, time: datetime) -> list[str]:
        """Ids of the truth paths present at ``time``."""
        return [p.id for p in self.truths if any(s.timestamp == time for s in p.states)]

    def tracks(self, run: TrackerRun) -> list[Track]:
        """``run``'s reported tracks sampled on the grid, as 6D states."""
        return self.sample(run.reported_tracks(), run.position_mapping, run.velocity_mapping)

    def sample(
        self,
        tracks: Sequence[Track],
        position_mapping: tuple[int, int, int] = (0, 2, 4),
        velocity_mapping: tuple[int, int, int] = (1, 3, 5),
    ) -> list[Track]:
        """Any Stone Soup tracks sampled on the grid, as 6D states.

        The mappings say where position and velocity sit in the tracks'
        states (default: the 6D ``[x, vx, y, vy, z, vz]`` layout).
        """
        px, py, pz = position_mapping
        vx, vy, vz = velocity_mapping
        out = []
        for track in tracks:
            states = track.states
            if not states:
                continue
            stamps = [s.timestamp for s in states]
            grid_states = []
            for t in self.times:
                if t < stamps[0] or t > stamps[-1]:
                    continue
                state = states[bisect_right(stamps, t) - 1]
                # Particle states hold every particle; their mean is the estimate.
                mean = np.asarray(getattr(state, "mean", state.state_vector), dtype=float).ravel()
                dt = (t - state.timestamp).total_seconds()
                pos = mean[[px, py, pz]] + dt * mean[[vx, vy, vz]]
                vel = mean[[vx, vy, vz]]
                vector = np.array([pos[0], vel[0], pos[1], vel[1], pos[2], vel[2]])
                grid_states.append(State(vector.reshape(6, 1), timestamp=t))
            if grid_states:
                out.append(Track(grid_states, id=track.id))
        return out


def scoring_grid(
    truth: TruthSet, simulation: SimulationResult, *, sensor_ids: list[str] | None = None
) -> ScoringGrid:
    """The evaluation grid for ``simulation`` and the truth visible on it.

    ``sensor_ids`` limits both to the sensors whose detections the trackers
    used (default: all); the grid follows the first of them.
    """
    scans = [s for s in simulation.scans if sensor_ids is None or s.sensor_id in sensor_ids]
    if not scans:
        raise ValueError("the simulation has no scans from those sensors")
    first = sensor_ids[0] if sensor_ids else scans[0].sensor_id
    period = simulation.radars[first].period_s
    times = sorted(s.end for s in scans if s.sensor_id == first)

    # When each aircraft could be seen, by any of those sensors.
    seen: dict[str, list[datetime]] = {}
    for scan in scans:
        for crossing in scan.opportunities:
            seen.setdefault(crossing.segment_id, []).append(crossing.time)
    return grid_from_looks(truth, times, period, seen)


def grid_from_looks(
    truth: TruthSet,
    times: Sequence[datetime],
    period_s: float,
    looks: Mapping[str, Sequence[datetime]],
) -> ScoringGrid:
    """The grid at ``times`` (scan ends), from when each aircraft could be seen.

    ``looks`` maps segment ids to the times of their detection
    opportunities; an aircraft counts at a grid time if it had one in the
    scan ending then (``period_s`` long).
    """
    times = sorted(times)
    window = timedelta(seconds=period_s)
    truths = []
    for segment_id in sorted(looks):
        crossings = sorted(looks[segment_id])
        visible = [
            t
            for t in times
            if (k := bisect_right(crossings, t)) > 0 and t - crossings[k - 1] < window
        ]
        if not visible:
            continue
        segment = truth.segment(segment_id)
        pos, vel, ok = segment.evaluate(visible)
        states = [
            GroundTruthState(
                np.array([p[0], v[0], p[1], v[1], p[2], v[2]]).reshape(6, 1), timestamp=t
            )
            for t, p, v, good in zip(visible, pos, vel, ok, strict=True)
            if good
        ]
        # One path per continuous stretch of visibility.
        stretch: list[GroundTruthState] = []
        number = 0
        for state in states:
            if stretch and state.timestamp - stretch[-1].timestamp > window + timedelta(seconds=1):
                number += 1
                truths.append(GroundTruthPath(stretch, id=f"{segment_id}#{number}"))
                stretch = []
            stretch.append(state)
        if stretch:
            number += 1
            truths.append(GroundTruthPath(stretch, id=f"{segment_id}#{number}"))
    return ScoringGrid(times=list(times), truths=truths, period_s=period_s)
