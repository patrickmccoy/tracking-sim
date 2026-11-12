"""The ``TruthSource`` protocol shared by every truth adapter.

The rest of the package depends only on this protocol, never on a specific
adapter, so a live Dump1090 feed, a captured SBS file and a recording are
interchangeable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from tracking_sim.truth.record import TruthRecord

__all__ = ["TruthSource", "UnsupportedModeError"]


class UnsupportedModeError(NotImplementedError):
    """Raised when a source does not support replay or streaming."""


@runtime_checkable
class TruthSource(Protocol):
    """A producer of canonical truth records.

    ``replay`` returns records with ``start <= ts < end`` in time order. Either
    bound may be ``None`` to leave that side open. ``stream`` yields records as
    they arrive from a live feed, in arrival order, until cancelled.

    A source that cannot do one of these raises ``UnsupportedModeError``; check
    ``supports_replay`` and ``supports_stream`` first.
    """

    source_id: str
    supports_replay: bool
    supports_stream: bool

    def replay(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[TruthRecord]: ...

    def stream(self) -> AsyncIterator[TruthRecord]: ...
