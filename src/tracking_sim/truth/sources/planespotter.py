"""The ``planespotter`` adapter: read-only access to a planespotter database.

Planespotter is one optional truth source. This adapter only ever issues
``SELECT`` statements, sets MySQL sessions to read-only, and should connect as
a dedicated read-only database user. It needs the ``planespotter`` extra::

    uv sync --extra planespotter

The table and column names, units and timestamp semantics are not hard-coded:
they come from ``PlanespotterConfig`` (usually a YAML file), because the
schema is site-specific.

Altitude: planespotter's stored altitude is most likely barometric but has not
been verified, so by default it goes to ``alt_unknown_ft``. Change
``altitude_reference`` once it is verified; see ``tracking_sim.truth.compare``.

Live mode polls the database for rows older than ``live_delay_s``. The
``/flights/stream`` HTTP endpoint is not used yet because its format has not
been confirmed.
"""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from tracking_sim.truth.record import TruthRecord, ensure_utc

if TYPE_CHECKING:
    from sqlalchemy import Engine, Select

__all__ = [
    "PlanespotterConfig",
    "PlanespotterSource",
    "PositionColumns",
    "PositionTable",
    "VelocityColumns",
    "VelocityTable",
    "set_session_read_only",
]

_FT_PER_M = 1 / 0.3048
_SPEED_TO_KT = {"kt": 1.0, "mps": 3600 / 1852, "kmh": 1000 / 1852, "mph": 1609.344 / 1852}
_ALT_TO_FT = {"ft": 1.0, "m": _FT_PER_M}
_VRATE_TO_FPM = {"fpm": 1.0, "mps": _FT_PER_M * 60}


def set_session_read_only(dbapi_conn: Any, _: Any = None) -> None:
    """Make every new MySQL session read-only, as a second line of defence."""
    cursor = dbapi_conn.cursor()
    cursor.execute("SET SESSION TRANSACTION READ ONLY")
    cursor.close()


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PositionColumns(_Model):
    icao24: str = "icao24"
    ts: str = "ts"
    lat: str = "lat"
    lon: str = "lon"
    altitude: str | None = "altitude"
    callsign: str | None = None
    on_ground: str | None = None


class VelocityColumns(_Model):
    icao24: str = "icao24"
    ts: str = "ts"
    gs: str | None = "gs"
    track: str | None = "track"
    vrate: str | None = "vrate"


class PositionTable(_Model):
    name: str = "positions"
    columns: PositionColumns = Field(default_factory=PositionColumns)


class VelocityTable(_Model):
    name: str = "velocities"
    columns: VelocityColumns = Field(default_factory=VelocityColumns)


class TimeConfig(_Model):
    kind: Literal["datetime", "epoch_s", "epoch_ms"] = "datetime"
    timezone: str = Field("UTC", description="zone of naive datetime columns")


class UnitsConfig(_Model):
    altitude: Literal["ft", "m"] = "ft"
    speed: Literal["kt", "mps", "kmh", "mph"] = "kt"
    vrate: Literal["fpm", "mps"] = "fpm"


class PlanespotterConfig(_Model):
    """Where planespotter's data lives and how to read it.

    ``url`` is a SQLAlchemy URL such as
    ``mysql+pymysql://reader:${PLANESPOTTER_PASSWORD}@db-host/planespotter``.
    """

    url: str
    receiver: str = "planespotter"
    positions: PositionTable = Field(default_factory=PositionTable)
    velocities: VelocityTable | None = Field(default_factory=VelocityTable)
    time: TimeConfig = Field(default_factory=TimeConfig)
    units: UnitsConfig = Field(default_factory=UnitsConfig)
    altitude_reference: Literal["unknown", "baro", "geom"] = "unknown"
    fetch_size: int = 10_000
    poll_interval_s: float = 5.0
    live_delay_s: float = 10.0


class PlanespotterSource:
    """Replay (and poll live) planespotter positions and velocities."""

    supports_replay = True
    supports_stream = True

    def __init__(self, config: PlanespotterConfig, *, engine: Engine | None = None) -> None:
        self.config = config
        self.source_id = f"planespotter:{config.receiver}"
        self._tz: tzinfo = ZoneInfo(config.time.timezone)
        self._engine = engine
        self._alt_field = {
            "unknown": "alt_unknown_ft",
            "baro": "alt_baro_ft",
            "geom": "alt_geom_ft",
        }[config.altitude_reference]

    @classmethod
    def from_yaml(cls, path: str) -> PlanespotterSource:
        from tracking_sim.config import load_yaml_model

        return cls(load_yaml_model(PlanespotterConfig, path))

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            try:
                from sqlalchemy import create_engine, event
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise ImportError(
                    "the planespotter adapter needs the 'planespotter' extra: "
                    "uv sync --extra planespotter"
                ) from exc
            engine = create_engine(self.config.url, pool_pre_ping=True)
            if engine.dialect.name in ("mysql", "mariadb"):
                event.listen(engine, "connect", set_session_read_only)
            self._engine = engine
        return self._engine

    # -- time conversion -------------------------------------------------

    def _to_db_time(self, ts: datetime) -> Any:
        ts = ensure_utc(ts)
        kind = self.config.time.kind
        if kind == "epoch_s":
            return ts.timestamp()
        if kind == "epoch_ms":
            return ts.timestamp() * 1000.0
        return ts.astimezone(self._tz).replace(tzinfo=None)

    def _from_db_time(self, value: Any) -> datetime:
        kind = self.config.time.kind
        if kind == "epoch_s":
            return datetime.fromtimestamp(float(value), UTC)
        if kind == "epoch_ms":
            return datetime.fromtimestamp(float(value) / 1000.0, UTC)
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=self._tz)
        return ensure_utc(value)

    # -- queries ---------------------------------------------------------

    def position_query(self, start: datetime | None, end: datetime | None) -> Select[Any]:
        cols = self.config.positions.columns
        names: dict[str, str | None] = {
            "icao24": cols.icao24,
            "ts": cols.ts,
            "lat": cols.lat,
            "lon": cols.lon,
            "altitude": cols.altitude,
            "callsign": cols.callsign,
            "on_ground": cols.on_ground,
        }
        return self._query(self.config.positions.name, names, cols.ts, start, end)

    def velocity_query(self, start: datetime | None, end: datetime | None) -> Select[Any]:
        assert self.config.velocities is not None
        cols = self.config.velocities.columns
        names: dict[str, str | None] = {
            "icao24": cols.icao24,
            "ts": cols.ts,
            "gs": cols.gs,
            "track": cols.track,
            "vrate": cols.vrate,
        }
        return self._query(self.config.velocities.name, names, cols.ts, start, end)

    def _query(
        self,
        table_name: str,
        names: dict[str, str | None],
        ts_column: str,
        start: datetime | None,
        end: datetime | None,
    ) -> Select[Any]:
        from sqlalchemy import DateTime, Float, column, select, table

        present = {label: name for label, name in names.items() if name}
        ts_type = DateTime() if self.config.time.kind == "datetime" else Float()
        tbl = table(
            table_name,
            *(
                column(name, ts_type) if name == ts_column else column(name)
                for name in dict.fromkeys(present.values())
            ),
        )
        query = select(*(tbl.c[name].label(label) for label, name in present.items()))
        ts = tbl.c[ts_column]
        if start is not None:
            query = query.where(ts >= self._to_db_time(start))
        if end is not None:
            query = query.where(ts < self._to_db_time(end))
        return query.order_by(ts)

    def _rows(self, query: Select[Any]) -> Iterator[dict[str, Any]]:
        with self.engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(query)
            while chunk := result.mappings().fetchmany(self.config.fetch_size):
                for row in chunk:
                    yield dict(row)

    # -- row conversion --------------------------------------------------

    def _position_record(self, row: dict[str, Any]) -> TruthRecord | None:
        if row.get("lat") is None or row.get("lon") is None:
            return None
        altitude = row.get("altitude")
        fields: dict[str, Any] = {}
        if altitude is not None:
            fields[self._alt_field] = float(altitude) * _ALT_TO_FT[self.config.units.altitude]
        if row.get("on_ground") is not None:
            fields["on_ground"] = bool(row["on_ground"])
        try:
            return TruthRecord(
                icao24=str(row["icao24"]),
                ts=self._from_db_time(row["ts"]),
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                source_id=self.source_id,
                callsign=row.get("callsign"),
                **fields,
            )
        except ValueError:
            return None

    def _velocity_record(self, row: dict[str, Any]) -> TruthRecord | None:
        units = self.config.units

        def scaled(key: str, factor: float) -> float | None:
            value = row.get(key)
            return None if value is None else float(value) * factor

        gs = scaled("gs", _SPEED_TO_KT[units.speed])
        track = scaled("track", 1.0)
        vrate = scaled("vrate", _VRATE_TO_FPM[units.vrate])
        if gs is None and track is None and vrate is None:
            return None
        try:
            return TruthRecord(
                icao24=str(row["icao24"]),
                ts=self._from_db_time(row["ts"]),
                lat=None,
                lon=None,
                source_id=self.source_id,
                gs_kt=gs,
                track_deg=track,
                vrate_fpm=vrate,
            )
        except ValueError:
            return None

    # -- TruthSource -----------------------------------------------------

    def replay(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[TruthRecord]:
        """Yield positions and velocities in ``[start, end)``, merged in time order."""
        positions = (
            r
            for r in map(self._position_record, self._rows(self.position_query(start, end)))
            if r is not None
        )
        streams: list[Iterator[TruthRecord]] = [positions]
        if self.config.velocities is not None:
            velocities = (
                r
                for r in map(self._velocity_record, self._rows(self.velocity_query(start, end)))
                if r is not None
            )
            streams.append(velocities)
        yield from heapq.merge(*streams, key=lambda r: r.ts)

    def stream(self) -> AsyncIterator[TruthRecord]:
        """Poll for rows at least ``live_delay_s`` old, every ``poll_interval_s``."""
        return self._poll()

    def _replay_list(self, start: datetime, end: datetime) -> list[TruthRecord]:
        return list(self.replay(start, end))

    async def _poll(self) -> AsyncIterator[TruthRecord]:
        delay = timedelta(seconds=self.config.live_delay_s)
        low = datetime.now(UTC) - delay
        while True:
            await asyncio.sleep(self.config.poll_interval_s)
            high = datetime.now(UTC) - delay
            batch = await asyncio.to_thread(self._replay_list, low, high)
            for record in batch:
                yield record
            low = high
