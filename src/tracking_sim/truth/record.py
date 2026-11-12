"""The canonical truth record every truth source emits.

A ``TruthRecord`` is one decoded message (or a merged group of fields) for one
aircraft at one receiver time. Records are sparse: position and velocity
usually arrive in separate messages, so most fields are optional and the truth
builder merges them per aircraft.

Values keep the source's native units (feet, knots, degrees, feet per minute).
Conversion to SI happens only in the truth builder.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

__all__ = ["TRUTH_RECORD_FIELDS", "TruthRecord", "ensure_utc"]


def ensure_utc(ts: datetime) -> datetime:
    """Return ``ts`` as an aware UTC datetime.

    Naive datetimes are taken to already be UTC; aware ones are converted.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TruthRecord:
    """One sparse ADS-B observation of one aircraft.

    The first five fields are required. Everything else is ``None`` when the
    message that produced the record did not carry it. ``lat`` and ``lon`` may
    also be ``None`` for velocity-only or identity-only messages.
    """

    icao24: str
    ts: datetime
    lat: float | None
    lon: float | None
    source_id: str
    alt_baro_ft: float | None = None
    alt_geom_ft: float | None = None
    alt_unknown_ft: float | None = None
    gs_kt: float | None = None
    track_deg: float | None = None
    vrate_fpm: float | None = None
    callsign: str | None = None
    on_ground: bool | None = None
    nic: int | None = None
    nacp: int | None = None
    _extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        icao = self.icao24.strip().lower()
        if not icao:
            raise ValueError("icao24 must not be empty")
        object.__setattr__(self, "icao24", icao)
        object.__setattr__(self, "ts", ensure_utc(self.ts))
        if self.callsign is not None:
            callsign = self.callsign.strip()
            object.__setattr__(self, "callsign", callsign or None)
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must both be set or both be None")
        if (
            self.lat is not None
            and self.lon is not None
            and not (-90.0 <= self.lat <= 90.0 and -180.0 <= self.lon <= 180.0)
        ):
            raise ValueError(f"position out of range: {self.lat}, {self.lon}")
        for name in _FLOAT_FIELDS:
            value = getattr(self, name)
            if value is not None and math.isnan(value):
                object.__setattr__(self, name, None)

    @property
    def has_position(self) -> bool:
        return self.lat is not None

    @property
    def has_velocity(self) -> bool:
        return self.gs_kt is not None and self.track_deg is not None

    @property
    def altitude_ft(self) -> float | None:
        """The best available altitude: geometric, then barometric, then unknown."""
        for value in (self.alt_geom_ft, self.alt_baro_ft, self.alt_unknown_ft):
            if value is not None:
                return value
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return the record as a plain dict of the canonical fields."""
        data = asdict(self)
        data.pop("_extra")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TruthRecord:
        """Build a record from a dict, ignoring keys that are not canonical fields."""
        kwargs = {k: data[k] for k in TRUTH_RECORD_FIELDS if k in data}
        ts = kwargs.get("ts")
        if isinstance(ts, str):
            kwargs["ts"] = datetime.fromisoformat(ts)
        return cls(**kwargs)


TRUTH_RECORD_FIELDS: tuple[str, ...] = tuple(
    f.name for f in fields(TruthRecord) if not f.name.startswith("_")
)
_FLOAT_FIELDS = (
    "alt_baro_ft",
    "alt_geom_ft",
    "alt_unknown_ft",
    "gs_kt",
    "track_deg",
    "vrate_fpm",
)
