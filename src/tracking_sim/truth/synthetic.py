"""Seeded synthetic ADS-B traffic for fixtures, tests and demos.

This is *not* truth from the sky. It produces records with the shape and
defects of real decoder output (sparse position and velocity messages,
duplicates from a second receiver, bad-decode position jumps, coverage gaps
and a taxiing aircraft) together with the noise-free trajectories that
produced them, so the truth builder can be tested against a known answer.

Kinematics are integrated on a flat local tangent plane and converted to
WGS84, which is plenty for test traffic.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pymap3d
from numpy.typing import NDArray

from tracking_sim.truth.record import TruthRecord

__all__ = [
    "ReceiverConfig",
    "SyntheticFlight",
    "SyntheticTraffic",
    "generate_traffic",
]

FT = 0.3048
_EARTH_RADIUS_M = 6_371_000.0
KT = 1852.0 / 3600.0
FPM = FT / 60.0

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class ReceiverConfig:
    """How one simulated receiver hears the traffic.

    ``hear_probability`` is per message. ``altitude_field`` chooses which
    TruthRecord altitude field the receiver fills, so a receiver can imitate a
    source whose altitude reference is unknown.

    ``altitude_kind`` (``"baro"`` or ``"geom"``) says what the value in
    ``altitude_field`` really is; by default it is geometric only when the
    field is ``alt_geom_ft``. ``report_geom`` also fills ``alt_geom_ft`` with
    geometric height, as ``aircraft.json`` does.

    With ``duplicate_of`` set to another receiver's ``source_id``, this
    receiver re-hears that receiver's messages: same decoded values, a later
    timestamp, and only a ``hear_probability`` fraction of them. That is what
    two receivers in range of the same aircraft produce.
    """

    source_id: str
    position_sigma_m: float = 8.0
    hear_probability: float = 1.0
    latency_s: tuple[float, float] = (0.0, 0.05)
    altitude_field: str = "alt_baro_ft"
    outlier_count: int = 0
    duplicate_of: str | None = None
    altitude_kind: str | None = None
    report_geom: bool = False


@dataclass
class SyntheticFlight:
    """The noise-free trajectory of one aircraft, sampled at 1 Hz."""

    icao24: str
    callsign: str | None
    kind: str
    t: FloatArray  # seconds since the traffic epoch
    lat: FloatArray
    lon: FloatArray
    alt_m: FloatArray  # geometric altitude
    gs_mps: FloatArray
    track_deg: FloatArray
    vrate_mps: FloatArray
    on_ground: bool = False

    def sample(self, t: FloatArray | float) -> dict[str, FloatArray]:
        """Linearly interpolate the trajectory at ``t`` (seconds since epoch)."""
        tt = np.atleast_1d(np.asarray(t, dtype=float))
        track = np.unwrap(np.radians(self.track_deg))
        return {
            "lat": np.interp(tt, self.t, self.lat),
            "lon": np.interp(tt, self.t, self.lon),
            "alt_m": np.interp(tt, self.t, self.alt_m),
            "gs_mps": np.interp(tt, self.t, self.gs_mps),
            "track_deg": np.degrees(np.interp(tt, self.t, track)) % 360.0,
            "vrate_mps": np.interp(tt, self.t, self.vrate_mps),
        }


@dataclass
class SyntheticTraffic:
    epoch: datetime
    duration_s: float
    origin: tuple[float, float, float]
    flights: list[SyntheticFlight]
    records: list[TruthRecord] = field(default_factory=list)
    gaps: dict[str, tuple[float, float]] = field(default_factory=dict)
    outliers: list[TruthRecord] = field(default_factory=list)

    def flight(self, icao24: str) -> SyntheticFlight:
        for flight in self.flights:
            if flight.icao24 == icao24:
                return flight
        raise KeyError(icao24)


@dataclass
class _Leg:
    duration_s: float
    turn_dps: float = 0.0
    accel_mps2: float = 0.0
    vrate_mps: float = 0.0


def _integrate(
    e0: float,
    n0: float,
    h0: float,
    heading_deg: float,
    speed_mps: float,
    legs: list[_Leg],
    *,
    h_floor: float,
    speed_limits: tuple[float, float],
) -> tuple[FloatArray, ...]:
    steps = sum(int(leg.duration_s) for leg in legs)
    e = np.empty(steps + 1)
    n = np.empty(steps + 1)
    h = np.empty(steps + 1)
    hdg = np.empty(steps + 1)
    gs = np.empty(steps + 1)
    vr = np.empty(steps + 1)
    e[0], n[0], h[0], hdg[0], gs[0], vr[0] = e0, n0, h0, heading_deg, speed_mps, 0.0
    i = 0
    for leg in legs:
        for _ in range(int(leg.duration_s)):
            speed = float(np.clip(gs[i] + leg.accel_mps2, *speed_limits))
            heading = (hdg[i] + leg.turn_dps) % 360.0
            vrate = leg.vrate_mps if h[i] + leg.vrate_mps > h_floor else 0.0
            rad = np.radians(heading)
            e[i + 1] = e[i] + speed * np.sin(rad)
            n[i + 1] = n[i] + speed * np.cos(rad)
            h[i + 1] = h[i] + vrate
            hdg[i + 1], gs[i + 1], vr[i + 1] = heading, speed, vrate
            i += 1
    vr[0] = vr[1] if steps else 0.0
    return e, n, h, hdg, gs, vr


def _bearing_point(rng: np.random.Generator, radius_m: float) -> tuple[float, float, float]:
    bearing = rng.uniform(0.0, 360.0)
    rad = np.radians(bearing)
    return radius_m * np.sin(rad), radius_m * np.cos(rad), bearing


def _heading_to(e0: float, n0: float, e1: float, n1: float) -> float:
    return float(np.degrees(np.arctan2(e1 - e0, n1 - n0)) % 360.0)


def _make_flights(
    rng: np.random.Generator, duration_s: float, ground_m: float
) -> list[tuple[str, float, tuple[FloatArray, ...], bool]]:
    """Return (kind, start offset, trajectory arrays, on_ground) per flight."""
    airport = (-8000.0, 5000.0)
    flights: list[tuple[str, float, tuple[FloatArray, ...], bool]] = []

    def start_in_window(length: float) -> float:
        return float(rng.uniform(-0.3 * length, max(duration_s - 0.6 * length, 1.0)))

    for _ in range(5):  # high overflights crossing the area
        e0, n0, _ = _bearing_point(rng, 175_000.0)
        aim = rng.normal(0.0, 40_000.0, size=2)
        heading = _heading_to(e0, n0, aim[0], aim[1])
        speed = rng.uniform(430, 480) * KT
        legs = [_Leg(1500), _Leg(30, turn_dps=float(rng.choice([-0.5, 0.5]))), _Leg(1500)]
        alt = float(rng.choice([31000, 33000, 35000, 37000, 39000])) * FT
        traj = _integrate(e0, n0, alt, heading, speed, legs, h_floor=0, speed_limits=(50, 300))
        flights.append(("overflight", start_in_window(3030), traj, False))

    for _ in range(3):  # arrivals descending towards the airport
        e0, n0, _ = _bearing_point(rng, 140_000.0)
        heading = _heading_to(e0, n0, *airport)
        speed = rng.uniform(280, 320) * KT
        cruise = (np.hypot(e0 - airport[0], n0 - airport[1]) - 18_000.0) / (230 * KT)
        legs = [
            _Leg(cruise, accel_mps2=-0.03, vrate_mps=-1800 * FPM),
            _Leg(30, turn_dps=3.0),
            _Leg(240, accel_mps2=-0.05, vrate_mps=-900 * FPM),
        ]
        traj = _integrate(
            e0,
            n0,
            rng.uniform(18000, 24000) * FT,
            heading,
            speed,
            legs,
            h_floor=ground_m + 300.0,
            speed_limits=(140 * KT, 340 * KT),
        )
        flights.append(("arrival", start_in_window(cruise + 270), traj, False))

    for _ in range(3):  # departures climbing out of the airport
        legs = [
            _Leg(120, accel_mps2=0.4, vrate_mps=2500 * FPM),
            _Leg(
                rng.uniform(20, 40), turn_dps=float(rng.choice([-3.0, 3.0])), vrate_mps=2000 * FPM
            ),
            _Leg(1200, accel_mps2=0.1, vrate_mps=1800 * FPM),
        ]
        traj = _integrate(
            airport[0],
            airport[1],
            ground_m + 150.0,
            rng.uniform(0, 360),
            150 * KT,
            legs,
            h_floor=0,
            speed_limits=(140 * KT, 320 * KT),
        )
        h = traj[2]
        cap = 33000 * FT
        h_capped = np.minimum(h, cap)
        vr = np.where(h >= cap, 0.0, traj[5])
        traj = (traj[0], traj[1], h_capped, traj[3], traj[4], vr)
        flights.append(("departure", start_in_window(1350), traj, False))

    for _ in range(2):  # light aircraft manoeuvring at low level
        e0, n0, _ = _bearing_point(rng, rng.uniform(10_000, 40_000))
        legs = []
        for _ in range(8):
            legs.append(_Leg(rng.uniform(90, 240)))
            legs.append(_Leg(rng.uniform(20, 60), turn_dps=float(rng.choice([-3.0, 3.0]))))
        traj = _integrate(
            e0,
            n0,
            rng.uniform(7500, 9500) * FT,
            rng.uniform(0, 360),
            rng.uniform(100, 120) * KT,
            legs,
            h_floor=0,
            speed_limits=(60 * KT, 130 * KT),
        )
        flights.append(("light", start_in_window(len(traj[0])), traj, False))

    legs = [_Leg(120), _Leg(30, turn_dps=3.0), _Leg(200), _Leg(30, turn_dps=-3.0), _Leg(120)]
    traj = _integrate(
        airport[0],
        airport[1],
        ground_m,
        90.0,
        15 * KT,
        legs,
        h_floor=0,
        speed_limits=(0, 30 * KT),
    )
    flights.append(("taxi", float(rng.uniform(0, max(duration_s - 600, 1.0))), traj, True))
    return flights


def _icao(rng: np.random.Generator, used: set[str]) -> str:
    while True:
        icao = f"{int(rng.integers(0xA00000, 0xADF7C8)):06x}"
        if icao not in used:
            used.add(icao)
            return icao


def generate_traffic(
    *,
    seed: int = 1,
    epoch: datetime = datetime(2026, 11, 1, 23, 0, tzinfo=UTC),
    duration_s: float = 3600.0,
    origin: tuple[float, float, float] = (39.98, -105.13, 1650.0),
    receivers: tuple[ReceiverConfig, ...] = (
        ReceiverConfig("synthetic:rx1", outlier_count=3),
        ReceiverConfig("synthetic:rx2", hear_probability=0.4, duplicate_of="synthetic:rx1"),
    ),
    position_period_s: float = 5.0,
    velocity_period_s: float = 5.0,
    ident_period_s: float = 30.0,
    coverage_radius_m: float = 180_000.0,
    gap_s: float | None = 120.0,
) -> SyntheticTraffic:
    """Generate an hour (by default) of seeded synthetic traffic.

    ``origin`` is (lat, lon, ground altitude in metres). The longest
    overflight gets a coverage gap of ``gap_s`` seconds in the middle of its
    track, during which no receiver hears it. The first receiver with
    ``outlier_count > 0`` injects that many bad-decode position jumps.
    """
    rng = np.random.default_rng(seed)
    lat0, lon0, ground_m = origin
    used: set[str] = set()
    traffic = SyntheticTraffic(epoch=epoch, duration_s=duration_s, origin=origin, flights=[])

    for kind, start, traj, on_ground in _make_flights(rng, duration_s, ground_m):
        e, n, h, hdg, gs, vr = traj
        t = start + np.arange(len(e), dtype=float)
        in_window = (t >= 0.0) & (t <= duration_s) & (np.hypot(e, n) <= coverage_radius_m)
        if in_window.sum() < 60:
            continue
        lat, lon, _ = pymap3d.enu2geodetic(e, n, np.zeros_like(e), lat0, lon0, 0.0)
        icao = _icao(rng, used)
        letters = "".join(rng.choice(list("ABCDEFGHJKLMNPRSTUVWXYZ"), size=3))
        callsign = None if kind == "taxi" else f"{letters}{int(rng.integers(10, 9999))}"
        traffic.flights.append(
            SyntheticFlight(
                icao24=icao,
                callsign=callsign,
                kind=kind,
                t=t[in_window],
                lat=np.asarray(lat)[in_window],
                lon=np.asarray(lon)[in_window],
                alt_m=h[in_window],
                gs_mps=gs[in_window],
                track_deg=hdg[in_window],
                vrate_mps=vr[in_window],
                on_ground=on_ground,
            )
        )

    if gap_s is not None:
        candidates = [f for f in traffic.flights if f.kind == "overflight"]
        longest = max(candidates or traffic.flights, key=lambda f: f.t[-1] - f.t[0])
        middle = float(longest.t[0] + longest.t[-1]) / 2.0
        traffic.gaps[longest.icao24] = (middle - gap_s / 2.0, middle + gap_s / 2.0)

    baro_offset_ft = float(rng.uniform(-300.0, 300.0))  # weather: QNH away from standard
    for receiver in receivers:
        if receiver.duplicate_of is not None:
            continue
        traffic.records.extend(
            _hear(
                traffic,
                receiver,
                _receiver_rng(seed, receiver),
                position_period_s,
                velocity_period_s,
                ident_period_s,
                baro_offset_ft,
            )
        )
    outliers = {id(r) for r in traffic.outliers}
    for receiver in receivers:
        if receiver.duplicate_of is None:
            continue
        originals = [
            r
            for r in traffic.records
            if r.source_id == receiver.duplicate_of and id(r) not in outliers
        ]
        dup_rng = _receiver_rng(seed, receiver)
        keep = dup_rng.random(len(originals)) < receiver.hear_probability
        delays = dup_rng.uniform(*receiver.latency_s, size=len(originals))
        traffic.records.extend(
            replace(r, source_id=receiver.source_id, ts=r.ts + timedelta(seconds=float(d)))
            for r, k, d in zip(originals, keep, delays, strict=True)
            if k
        )
    traffic.records.sort(key=lambda r: (r.ts, r.source_id, r.icao24))
    return traffic


def _hear(
    traffic: SyntheticTraffic,
    receiver: ReceiverConfig,
    rng: np.random.Generator,
    position_period_s: float,
    velocity_period_s: float,
    ident_period_s: float,
    baro_offset_ft: float,
) -> list[TruthRecord]:
    records: list[TruthRecord] = []
    for flight in traffic.flights:
        t0, t1 = float(flight.t[0]), float(flight.t[-1])
        phase = float(rng.uniform(0.0, position_period_s))
        gap = traffic.gaps.get(flight.icao24)

        def heard(times: FloatArray, gap: tuple[float, float] | None = gap) -> FloatArray:
            keep = rng.random(times.shape) < receiver.hear_probability
            if gap is not None:
                keep &= (times < gap[0]) | (times >= gap[1])
            return times[keep]

        pos_t = heard(np.arange(t0 + phase, t1, position_period_s))
        vel_t = heard(np.arange(t0 + phase + velocity_period_s / 2, t1, velocity_period_s))
        id_t = heard(np.arange(t0 + phase + 1.0, t1, ident_period_s))

        pos = flight.sample(pos_t)
        noise = rng.normal(0.0, receiver.position_sigma_m, size=(2, len(pos_t)))
        # Offset each point by metres of noise around itself; a few metres is
        # far too small for the spherical approximation to matter.
        lat = pos["lat"] + np.degrees(noise[1] / _EARTH_RADIUS_M)
        lon = pos["lon"] + np.degrees(noise[0] / (_EARTH_RADIUS_M * np.cos(np.radians(pos["lat"]))))
        geom_ft = pos["alt_m"] / FT
        kind = receiver.altitude_kind or (
            "geom" if receiver.altitude_field == "alt_geom_ft" else "baro"
        )
        alt_ft = geom_ft if kind == "geom" else geom_ft - baro_offset_ft
        alt_ft = np.round(alt_ft / 25.0) * 25.0

        for k, ts in enumerate(pos_t):
            record = TruthRecord(
                icao24=flight.icao24,
                ts=_stamp(traffic.epoch, ts, receiver, rng),
                lat=round(float(lat[k]), 6),
                lon=round(float(lon[k]), 6),
                source_id=receiver.source_id,
                on_ground=flight.on_ground,
            )
            if not flight.on_ground:
                altitude: dict[str, Any] = {receiver.altitude_field: float(alt_ft[k])}
                if receiver.report_geom:
                    altitude["alt_geom_ft"] = float(np.round(geom_ft[k] / 25.0) * 25.0)
                record = replace(record, **altitude)
            records.append(record)
        vel = flight.sample(vel_t)
        for k, ts in enumerate(vel_t):
            records.append(
                TruthRecord(
                    icao24=flight.icao24,
                    ts=_stamp(traffic.epoch, ts, receiver, rng),
                    lat=None,
                    lon=None,
                    source_id=receiver.source_id,
                    gs_kt=float(np.round(vel["gs_mps"][k] / KT)),
                    track_deg=round(float(vel["track_deg"][k]), 1),
                    vrate_fpm=float(np.round(vel["vrate_mps"][k] / FPM / 64.0) * 64.0),
                    on_ground=flight.on_ground,
                )
            )
        for ts in id_t:
            records.append(
                TruthRecord(
                    icao24=flight.icao24,
                    ts=_stamp(traffic.epoch, ts, receiver, rng),
                    lat=None,
                    lon=None,
                    source_id=receiver.source_id,
                    callsign=flight.callsign,
                )
            )

    if receiver.outlier_count:
        positions = [r for r in records if r.has_position and not r.on_ground]
        picks = rng.choice(len(positions), size=receiver.outlier_count, replace=False)
        for idx in sorted(picks):
            good = positions[int(idx)]
            assert good.lat is not None and good.lon is not None
            jump_e, jump_n = rng.uniform(5_000, 30_000, size=2) * rng.choice([-1, 1], size=2)
            lat, lon, _ = pymap3d.enu2geodetic(jump_e, jump_n, 0.0, good.lat, good.lon, 0.0)
            bad = TruthRecord(
                icao24=good.icao24,
                ts=good.ts + timedelta(milliseconds=500),
                lat=round(float(lat), 6),
                lon=round(float(lon), 6),
                source_id=receiver.source_id,
                alt_baro_ft=good.alt_baro_ft,
                alt_geom_ft=good.alt_geom_ft,
                alt_unknown_ft=good.alt_unknown_ft,
                on_ground=False,
            )
            records.append(bad)
            traffic.outliers.append(bad)
    return records


def _receiver_rng(seed: int, receiver: ReceiverConfig) -> np.random.Generator:
    """An RNG stream of its own per receiver.

    The flights come from the main stream, so the same seed always gives the
    same traffic; each receiver's noise is independent of every other
    receiver's, and adding or removing a receiver leaves the others unchanged.
    """
    return np.random.default_rng([seed, zlib.crc32(receiver.source_id.encode())])


def _stamp(
    epoch: datetime, t: float, receiver: ReceiverConfig, rng: np.random.Generator
) -> datetime:
    latency = float(rng.uniform(*receiver.latency_s))
    micros = round((float(t) + latency) * 1e6)
    return epoch + timedelta(microseconds=micros)
