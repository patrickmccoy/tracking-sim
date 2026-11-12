"""The ``sbs`` adapter: Dump1090 SBS-1 BaseStation text.

Dump1090 and its forks (dump1090-fa, readsb) serve BaseStation CSV on TCP
port 30003 by default. Each ``MSG`` line carries a subset of fields depending
on its transmission type:

====  ========================  ====================================
Type  Meaning                   Fields used here
====  ========================  ====================================
1     Identification            callsign
2     Surface position          altitude, ground speed, track, lat, lon
3     Airborne position         altitude, lat, lon
4     Airborne velocity         ground speed, track, vertical rate
5     Surveillance altitude     altitude
6     Surveillance identity     altitude
7     Air-to-air                altitude
8     All-call reply            on-ground flag only
====  ========================  ====================================

Things that differ between forks and must come from config, not code:

* BaseStation timestamps carry no time zone. Dump1090 writes the receiver
  host's local time, so set ``tz`` to that zone, or use arrival time for live
  feeds with ``use_arrival_time=True``.
* The altitude is pressure altitude in standard Dump1090 output, so it goes to
  ``alt_baro_ft`` by default. Set ``altitude_field`` if your decoder differs.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
from collections.abc import AsyncIterator, Iterable, Iterator
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from tracking_sim.truth.record import TruthRecord, ensure_utc
from tracking_sim.truth.source import UnsupportedModeError

__all__ = ["AltitudeField", "SbsSource", "format_sbs_line", "parse_sbs_line"]

log = logging.getLogger(__name__)

AltitudeField = Literal["alt_baro_ft", "alt_geom_ft", "alt_unknown_ft"]

# Column positions in a BaseStation MSG line.
_TYPE, _HEX, _DATE_GEN, _TIME_GEN, _DATE_LOG, _TIME_LOG = 1, 4, 6, 7, 8, 9
_CALLSIGN, _ALT, _GS, _TRACK, _LAT, _LON, _VRATE, _GROUND = 10, 11, 12, 13, 14, 15, 16, 21


def _float(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _ground(value: str) -> bool | None:
    value = value.strip()
    if value in ("-1", "1"):
        return True
    if value == "0":
        return False
    return None


def _parse_time(date: str, time: str, tz: tzinfo) -> datetime | None:
    date, time = date.strip(), time.strip()
    if not date or not time:
        return None
    try:
        naive = datetime.strptime(f"{date} {time}", "%Y/%m/%d %H:%M:%S.%f")
    except ValueError:
        try:
            naive = datetime.strptime(f"{date} {time}", "%Y/%m/%d %H:%M:%S")
        except ValueError:
            return None
    return naive.replace(tzinfo=tz).astimezone(UTC)


def parse_sbs_line(
    line: str,
    *,
    source_id: str = "sbs",
    tz: tzinfo = UTC,
    time_field: Literal["generated", "logged"] = "generated",
    altitude_field: AltitudeField = "alt_baro_ft",
    arrival_time: datetime | None = None,
) -> TruthRecord | None:
    """Parse one BaseStation line into a TruthRecord.

    Returns ``None`` for non-``MSG`` lines, malformed lines, and messages that
    carry nothing a TruthRecord can hold (for example a squawk-only message).
    If ``arrival_time`` is given it is used instead of the line's timestamp.
    """
    parts = line.rstrip("\r\n").split(",")
    if len(parts) < 11 or parts[0] != "MSG":
        return None
    hex_ident = parts[_HEX].strip()
    if not hex_ident:
        return None
    parts += [""] * (22 - len(parts))

    if arrival_time is not None:
        ts: datetime | None = ensure_utc(arrival_time)
    elif time_field == "generated":
        ts = _parse_time(parts[_DATE_GEN], parts[_TIME_GEN], tz)
    else:
        ts = _parse_time(parts[_DATE_LOG], parts[_TIME_LOG], tz)
    if ts is None:
        return None

    lat, lon = _float(parts[_LAT]), _float(parts[_LON])
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    fields: dict[str, Any] = {
        altitude_field: _float(parts[_ALT]),
        "gs_kt": _float(parts[_GS]),
        "track_deg": _float(parts[_TRACK]),
        "vrate_fpm": _float(parts[_VRATE]),
        "callsign": parts[_CALLSIGN].strip() or None,
        "on_ground": _ground(parts[_GROUND]),
    }
    empty = lat is None and all(fields[k] is None for k in fields if k != "on_ground")
    ground_only = parts[_TYPE].strip() == "8" and fields["on_ground"] is not None
    if empty and not ground_only:
        return None
    try:
        return TruthRecord(icao24=hex_ident, ts=ts, lat=lat, lon=lon, source_id=source_id, **fields)
    except ValueError:
        return None


def _fmt(value: float | None, digits: int) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:.0f}"


def format_sbs_line(
    record: TruthRecord,
    *,
    tz: tzinfo = UTC,
    altitude_field: AltitudeField = "alt_baro_ft",
) -> str:
    """Format a record as a BaseStation MSG line (the inverse of parsing).

    The transmission type is chosen from the fields present. Timestamps are
    written to the millisecond, as Dump1090 does.
    """
    alt = getattr(record, altitude_field)
    if record.has_position:
        msg_type = 2 if record.on_ground else 3
    elif record.gs_kt is not None or record.track_deg is not None:
        msg_type = 4
    elif record.callsign:
        msg_type = 1
    elif alt is not None:
        msg_type = 5
    else:
        msg_type = 8
    local = record.ts.astimezone(tz)
    date = local.strftime("%Y/%m/%d")
    time = local.strftime("%H:%M:%S.") + f"{local.microsecond // 1000:03d}"
    ground = "" if record.on_ground is None else ("-1" if record.on_ground else "0")
    cols = [
        "MSG",
        str(msg_type),
        "1",
        "1",
        record.icao24.upper(),
        "1",
        date,
        time,
        date,
        time,
        record.callsign or "",
        _fmt(alt, 0),
        _fmt(record.gs_kt, 1),
        _fmt(record.track_deg, 1),
        _fmt(record.lat, 6),
        _fmt(record.lon, 6),
        _fmt(record.vrate_fpm, 0),
        "",
        "",
        "",
        "",
        ground,
    ]
    return ",".join(cols)


def _open_text(path: Path) -> Iterable[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="ascii", errors="replace") as fh:
            yield from fh
    else:
        with path.open(encoding="ascii", errors="replace") as fh:
            yield from fh


class SbsSource:
    """Truth from BaseStation text, live over TCP or replayed from files.

    Give ``host`` (and optionally ``port``) for live streaming, or ``path``
    for replaying a captured feed (plain or ``.gz``, one or more files). The
    record ``source_id`` is ``sbs:<receiver>``.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int = 30003,
        path: str | Path | Iterable[str | Path] | None = None,
        receiver: str = "default",
        tz: str | tzinfo = "UTC",
        time_field: Literal["generated", "logged"] = "generated",
        use_arrival_time: bool = False,
        altitude_field: AltitudeField = "alt_baro_ft",
        reconnect_delay_s: float = 5.0,
        max_reconnects: int | None = None,
    ) -> None:
        if host is None and path is None:
            raise ValueError("SbsSource needs a host to stream from or a path to replay")
        self.host = host
        self.port = port
        if path is None:
            self.paths: list[Path] = []
        elif isinstance(path, str | Path):
            self.paths = [Path(path)]
        else:
            self.paths = [Path(p) for p in path]
        self.source_id = f"sbs:{receiver}"
        self.tz: tzinfo = ZoneInfo(tz) if isinstance(tz, str) else tz
        self.time_field = time_field
        self.use_arrival_time = use_arrival_time
        self.altitude_field: AltitudeField = altitude_field
        self.reconnect_delay_s = reconnect_delay_s
        self.max_reconnects = max_reconnects
        self.supports_replay = bool(self.paths)
        self.supports_stream = host is not None

    def parse(self, line: str, arrival_time: datetime | None = None) -> TruthRecord | None:
        return parse_sbs_line(
            line,
            source_id=self.source_id,
            tz=self.tz,
            time_field=self.time_field,
            altitude_field=self.altitude_field,
            arrival_time=arrival_time,
        )

    def replay(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[TruthRecord]:
        if not self.paths:
            raise UnsupportedModeError("replay needs a path to a captured SBS feed")
        start = ensure_utc(start) if start is not None else None
        end = ensure_utc(end) if end is not None else None
        records = []
        for path in self.paths:
            for line in _open_text(path):
                record = self.parse(line)
                if record is None:
                    continue
                if (start is None or record.ts >= start) and (end is None or record.ts < end):
                    records.append(record)
        records.sort(key=lambda r: r.ts)
        yield from records

    def stream(self) -> AsyncIterator[TruthRecord]:
        """Yield records from the TCP feed, reconnecting when it drops."""
        if self.host is None:
            raise UnsupportedModeError("streaming needs a host running Dump1090")
        return self._stream(self.host)

    async def _stream(self, host: str) -> AsyncIterator[TruthRecord]:
        reconnects = 0
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, self.port)
            except OSError as exc:
                log.warning("cannot connect to %s:%s: %s", host, self.port, exc)
            else:
                log.info("connected to %s:%s", host, self.port)
                try:
                    while line := await reader.readline():
                        arrival = datetime.now(UTC) if self.use_arrival_time else None
                        record = self.parse(line.decode("ascii", "replace"), arrival)
                        if record is not None:
                            yield record
                finally:
                    writer.close()
                log.warning("connection to %s:%s closed", host, self.port)
            reconnects += 1
            if self.max_reconnects is not None and reconnects > self.max_reconnects:
                return
            await asyncio.sleep(self.reconnect_delay_s)
