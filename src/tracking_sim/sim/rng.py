"""Seeded random streams for a run.

Every random draw in a run comes from one seed. Each sensor gets its own
streams, derived from the seed and the sensor's id (not its position in a
list), so adding or reordering sensors leaves the others' draws unchanged.
Each sensor's streams are split by purpose too: changing the clutter
density does not change which targets are detected or their noise, which
keeps comparisons between configurations like-for-like (common random
numbers).
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np

__all__ = ["SensorStreams", "sensor_streams"]


@dataclass(frozen=True)
class SensorStreams:
    """Independent generators for one sensor's detection draws, noise and clutter."""

    detection: np.random.Generator
    noise: np.random.Generator
    clutter: np.random.Generator


def sensor_streams(seed: int, sensor_id: str) -> SensorStreams:
    """The random streams for ``sensor_id`` in a run seeded with ``seed``."""
    if seed < 0:
        raise ValueError("seed must be a non-negative integer")
    root = np.random.SeedSequence(seed, spawn_key=(zlib.crc32(sensor_id.encode("utf-8")),))
    detection, noise, clutter = (np.random.default_rng(s) for s in root.spawn(3))
    return SensorStreams(detection=detection, noise=noise, clutter=clutter)
