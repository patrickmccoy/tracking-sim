"""The ``aircraft_json`` adapter: readsb / dump1090-fa ``aircraft.json``.

readsb and dump1090-fa publish the aircraft they currently hear as a JSON
document, usually at ``http://<receiver>/data/aircraft.json`` (dump1090-fa
via its web server, readsb via tar1090), refreshed about once a second::

    {"now": 1793574000.1, "aircraft": [
        {"hex": "a1b2c3", "flight": "UAL123  ", "category": "A3",
         "alt_baro": 35000, "alt_geom": 35450, "gs": 452.1, "track": 271.3,
         "baro_rate": -64, "lat": 39.91, "lon": -105.12, "nic": 8, "nac_p": 9,
         "seen_pos": 0.4, "seen": 0.1, ...}, ...]}

It carries what SBS doesn't: separate barometric and geometric altitude,
position accuracy (NIC, NACp) and the emitter category. Each poll is a
snapshot, so:

* a position is timestamped ``now - seen_pos`` (when it was received);
  velocity and identity ``now - seen`` (the latest message of any kind);
* an aircraft whose position (or velocity) hasn't changed since the last
  poll is the same report seen again, and is dropped;
* ``alt_baro`` may be the string ``"ground"``: the aircraft is on the
  ground and reports no altitude;
* addresses starting with ``~`` are not ICAO addresses (TIS-B, anonymous)
  and are skipped unless ``include_non_icao`` is set;
* older dump1090-fa field names (``altitude``, ``speed``, ``vert_rate``) are
  accepted.

Replay reads saved snapshots: ``.json`` files (one snapshot each), or
``.jsonl``/``.ndjson`` with one snapshot per line, optionally gzipped, or a
directory of them. Live mode polls a URL. Record ``source_id`` is
``aircraft_json:<receiver>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import urllib.request
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracking_sim.truth.record import TruthRecord, ensure_utc
from tracking_sim.truth.source import UnsupportedModeError

__all__ = ["AircraftJsonSource", "SnapshotParser"]

log = logging.getLogger(__name__)
_USER_AGENT = "tracking-sim (aircraft.json reader)"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


@dataclass
class SnapshotParser:
    """Turns successive ``aircraft.json`` snapshots into new TruthRecords.

    Keeps the last position and velocity reported per aircraft, so a report
    repeated in the next poll is not emitted twice. Use one parser per feed,
    fed in time order.
    """

    source_id: str = "aircraft_json:default"
    include_non_icao: bool = False
    _last_position: dict[str, tuple[float, float]] = field(default_factory=dict)
    _last_velocity: dict[str, tuple[Any, ...]] = field(default_factory=dict)

    def parse(self, snapshot: dict[str, Any]) -> list[TruthRecord]:
        now = _number(snapshot.get("now"))
        if now is None:
            return []
        out: list[TruthRecord] = []
        for aircraft in snapshot.get("aircraft") or []:
            out.extend(self._aircraft(aircraft, now))
        out.sort(key=lambda r: r.ts)
        return out

    def _aircraft(self, a: dict[str, Any], now: float) -> list[TruthRecord]:
        hexid = str(a.get("hex") or "").strip().lower()
        if hexid.startswith("~"):
            if not self.include_non_icao:
                return []
            hexid = hexid[1:]
        if not hexid:
            return []
        callsign = (a.get("flight") or "").strip() or None
        category = (a.get("category") or "").strip() or None
        seen = _number(a.get("seen")) or 0.0
        out: list[TruthRecord] = []

        lat, lon, seen_pos = (
            _number(a.get("lat")),
            _number(a.get("lon")),
            _number(a.get("seen_pos")),
        )
        if lat is not None and lon is not None and seen_pos is not None:
            ts = now - seen_pos
            previous = self._last_position.get(hexid)
            if previous is None or previous != (lat, lon):
                self._last_position[hexid] = (lat, lon)
                baro = a.get("alt_baro", a.get("altitude"))
                on_ground = baro == "ground"
                fields: dict[str, Any] = {
                    "alt_baro_ft": None if on_ground else _number(baro),
                    "alt_geom_ft": None if on_ground else _number(a.get("alt_geom")),
                    "on_ground": True
                    if on_ground
                    else (False if _number(baro) is not None else None),
                    "nic": _integer(a.get("nic")),
                    "nacp": _integer(a.get("nac_p")),
                    "callsign": callsign,
                    "category": category,
                }
                with contextlib.suppress(ValueError):  # out-of-range position
                    out.append(
                        TruthRecord(
                            icao24=hexid,
                            ts=datetime.fromtimestamp(ts, UTC),
                            lat=lat,
                            lon=lon,
                            source_id=self.source_id,
                            **fields,
                        )
                    )

        gs = _number(a.get("gs", a.get("speed")))
        track = _number(a.get("track"))
        rate = _number(a.get("baro_rate", a.get("geom_rate", a.get("vert_rate"))))
        if gs is not None or track is not None or rate is not None:
            key = (gs, track, rate)
            if self._last_velocity.get(hexid) != key:
                self._last_velocity[hexid] = key
                out.append(
                    TruthRecord(
                        icao24=hexid,
                        ts=datetime.fromtimestamp(now - seen, UTC),
                        lat=None,
                        lon=None,
                        source_id=self.source_id,
                        gs_kt=gs,
                        track_deg=track,
                        vrate_fpm=rate,
                        callsign=callsign,
                        category=category,
                    )
                )
        return out


def _snapshots_in(path: Path) -> Iterator[dict[str, Any]]:
    name = path.name.lower()
    opener: Any = gzip.open if name.endswith(".gz") else open
    stem = name[:-3] if name.endswith(".gz") else name
    with opener(path, "rt", encoding="utf-8") as fh:
        if stem.endswith((".jsonl", ".ndjson")):
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
        else:
            yield json.load(fh)


def _expand(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(p for p in path.iterdir() if ".json" in p.name.lower()))
        else:
            out.append(path)
    return out


class AircraftJsonSource:
    """Truth from ``aircraft.json``, polled live or replayed from saved snapshots."""

    def __init__(
        self,
        *,
        url: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        receiver: str = "default",
        poll_interval_s: float = 1.0,
        include_non_icao: bool = False,
        timeout_s: float = 5.0,
        max_failures: int | None = None,
    ) -> None:
        if url is None and path is None:
            raise ValueError("AircraftJsonSource needs a url to poll or a path to replay")
        self.url = url
        if path is None:
            self.paths: list[Path] = []
        elif isinstance(path, str | Path):
            self.paths = [Path(path)]
        else:
            self.paths = [Path(p) for p in path]
        self.source_id = f"aircraft_json:{receiver}"
        self.poll_interval_s = poll_interval_s
        self.include_non_icao = include_non_icao
        self.timeout_s = timeout_s
        self.max_failures = max_failures
        self.supports_replay = bool(self.paths)
        self.supports_stream = url is not None

    def _parser(self) -> SnapshotParser:
        return SnapshotParser(source_id=self.source_id, include_non_icao=self.include_non_icao)

    def replay(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[TruthRecord]:
        if not self.paths:
            raise UnsupportedModeError("replay needs saved aircraft.json snapshots")
        start = ensure_utc(start) if start is not None else None
        end = ensure_utc(end) if end is not None else None
        snapshots = [s for p in _expand(self.paths) for s in _snapshots_in(p)]
        snapshots.sort(key=lambda s: _number(s.get("now")) or 0.0)
        parser = self._parser()
        records = [
            r
            for s in snapshots
            for r in parser.parse(s)
            if (start is None or r.ts >= start) and (end is None or r.ts < end)
        ]
        records.sort(key=lambda r: r.ts)
        yield from records

    def stream(self) -> AsyncIterator[TruthRecord]:
        """Poll the URL every ``poll_interval_s`` and yield new records."""
        if self.url is None:
            raise UnsupportedModeError("streaming needs the url of aircraft.json")
        return self._poll(self.url)

    def _fetch(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return data

    async def _poll(self, url: str) -> AsyncIterator[TruthRecord]:
        parser = self._parser()
        failures = 0
        while True:
            try:
                snapshot = await asyncio.to_thread(self._fetch, url)
            except (OSError, ValueError) as exc:
                failures += 1
                log.warning("polling %s failed: %s", url, exc)
                if self.max_failures is not None and failures > self.max_failures:
                    raise
            else:
                failures = 0
                for record in parser.parse(snapshot):
                    yield record
            await asyncio.sleep(self.poll_interval_s)
