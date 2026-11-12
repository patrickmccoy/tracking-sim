"""Probability of detection: radar cross-section classes and the radar equation.

A radar's chance of detecting an aircraft depends on how much energy comes
back: the aircraft's radar cross-section (RCS) and, through the radar
equation, the fourth power of range. This module gives each aircraft an RCS
class and turns class and slant range into a single-scan Pd.

**RCS classes.** ``light``, ``small``, ``large``, ``heavy``,
``rotorcraft`` and ``unknown``. An aircraft's class comes, in order, from an
optional table of ICAO address to class (CSV with columns ``icao24`` and
``class``), then from its ADS-B emitter category (``aircraft.json`` reports
it), then a default. The mean RCS of each class is part of the sensor spec,
because RCS depends on the radar's frequency and aspect; the defaults are
rough textbook values for an L/S-band surveillance radar.

**Radar equation.** The spec gives the signal-to-noise ratio a 1 m^2
target would have at a reference range, which folds transmitter power,
antenna gain, wavelength, losses and noise into one number that's easy to
tune ("a 1 m^2 target at 50 km gives 13 dB")::

    SNR = SNR_ref * (RCS / 1 m^2) * (R_ref / R)^4

with R the slant range. Pd for a single look with false-alarm probability
``Pfa`` per resolution cell:

* **Swerling 1** (RCS fluctuating scan to scan, the usual model for
  aircraft): ``Pd = Pfa ** (1 / (1 + SNR))``, exact.
* **Swerling 0** (steady RCS): ``Pd = Q1(sqrt(2 SNR), sqrt(-2 ln Pfa))``, the
  Marcum Q function, computed as a non-central chi-squared survival
  function.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.stats import ncx2

__all__ = [
    "CATEGORY_CLASS",
    "RCS_CLASSES",
    "AircraftClasses",
    "RcsClass",
    "load_aircraft_classes",
    "pd_swerling",
    "snr_radar_equation",
]

FloatArray = NDArray[np.float64]
RcsClass = Literal["light", "small", "large", "heavy", "rotorcraft", "unknown"]
RCS_CLASSES: tuple[RcsClass, ...] = ("light", "small", "large", "heavy", "rotorcraft", "unknown")

# ADS-B emitter categories (DO-260B set A and B) to RCS class.
CATEGORY_CLASS: dict[str, RcsClass] = {
    "A1": "light",  # light, under 15,500 lb
    "A2": "small",  # small, 15,500 to 75,000 lb
    "A3": "large",  # large, 75,000 to 300,000 lb
    "A4": "large",  # high-vortex large (B757)
    "A5": "heavy",  # heavy, over 300,000 lb
    "A6": "small",  # high performance
    "A7": "rotorcraft",
    "B1": "light",  # glider
    "B2": "light",  # lighter than air
    "B3": "light",  # parachutist
    "B4": "light",  # ultralight
    "B6": "light",  # unmanned
    "B7": "light",  # space vehicle (no better guess)
}


def load_aircraft_classes(path: str | Path) -> dict[str, RcsClass]:
    """Read an ``icao24,class`` CSV (header row required) into a mapping."""
    out: dict[str, RcsClass] = {}
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            icao = (row.get("icao24") or "").strip().lower()
            cls = (row.get("class") or "").strip().lower()
            if not icao:
                continue
            if cls not in RCS_CLASSES:
                raise ValueError(f"{path}: unknown class {cls!r} for {icao} (use {RCS_CLASSES})")
            out[icao] = cls
    return out


class AircraftClasses:
    """Which RCS class each aircraft is in: table, then ADS-B category, then default."""

    def __init__(
        self, table: Mapping[str, RcsClass] | None = None, default: RcsClass = "unknown"
    ) -> None:
        self.table = {k.lower(): v for k, v in (table or {}).items()}
        self.default: RcsClass = default

    def classify(self, icao24: str, category: str | None) -> RcsClass:
        if icao24.lower() in self.table:
            return self.table[icao24.lower()]
        if category and category.upper() in CATEGORY_CLASS:
            return CATEGORY_CLASS[category.upper()]
        return self.default

    def digest(self) -> str:
        return repr((sorted(self.table.items()), self.default))


def snr_radar_equation(
    slant_range_m: ArrayLike,
    rcs_m2: ArrayLike,
    reference_snr_db: float,
    reference_range_m: float,
) -> FloatArray:
    """Linear SNR from the radar equation, scaled from a 1 m^2 reference."""
    r = np.asarray(slant_range_m, dtype=float)
    snr: FloatArray = (
        10 ** (reference_snr_db / 10)
        * np.asarray(rcs_m2, dtype=float)
        * (reference_range_m / np.maximum(r, 1.0)) ** 4
    )
    return snr


def pd_swerling(snr: ArrayLike, pfa: float, swerling: Literal[0, 1] = 1) -> FloatArray:
    """Single-look Pd for a square-law detector at false-alarm probability ``pfa``."""
    s = np.asarray(snr, dtype=float)
    if swerling == 1:
        pd: FloatArray = pfa ** (1.0 / (1.0 + s))
        return pd
    threshold = -2.0 * np.log(pfa)
    steady: FloatArray = np.asarray(ncx2.sf(threshold, 2, 2.0 * s), dtype=float)
    return steady
