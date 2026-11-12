"""Compare two truth sources on the same aircraft at the same times.

Two uses:

* **Agreement.** Milestone 1 is done when the same aircraft from two
  independent sources (two receivers, or a receiver and an SBS export from a
  database) agree within the truth sigma.
  ``compare_truth`` samples both smoothed trajectories on a common time grid
  and reports the horizontal and vertical differences per aircraft.
* **Altitude reference.** A source that was not decoded live, such as an SBS
  file exported from a database, may carry barometric or geometric altitude.
  ``assess_altitude_reference`` compares a candidate source's altitude
  against a reference source's barometric and, if present, geometric
  altitude for the same aircraft and times, and says which one it tracks.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from tracking_sim.geo import LocalFrame
from tracking_sim.truth.builder import FlightSegment, TruthBuilderConfig, TruthSet, build_truth
from tracking_sim.truth.record import TruthRecord

__all__ = [
    "AircraftComparison",
    "AltitudeAssessment",
    "ComparisonReport",
    "assess_altitude_reference",
    "compare_truth",
]

M_TO_FT = 1 / 0.3048


@dataclass
class AircraftComparison:
    """Differences between two sources for one aircraft, ``b - a``."""

    icao24: str
    samples: int
    horizontal_m: np.ndarray
    vertical_m: np.ndarray  # NaN where either side has no altitude
    alt_reference_a: str | None
    alt_reference_b: str | None

    @property
    def horizontal_median_m(self) -> float:
        return float(np.median(self.horizontal_m))

    @property
    def horizontal_p95_m(self) -> float:
        return float(np.percentile(self.horizontal_m, 95))

    @property
    def vertical_mean_m(self) -> float:
        finite = self.vertical_m[np.isfinite(self.vertical_m)]
        return float(finite.mean()) if finite.size else float("nan")

    @property
    def vertical_std_m(self) -> float:
        finite = self.vertical_m[np.isfinite(self.vertical_m)]
        return float(finite.std()) if finite.size else float("nan")


@dataclass
class ComparisonReport:
    sigma_m: float
    aircraft: list[AircraftComparison] = field(default_factory=list)
    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)

    @property
    def horizontal_m(self) -> np.ndarray:
        if not self.aircraft:
            return np.array([])
        return np.concatenate([a.horizontal_m for a in self.aircraft])

    @property
    def vertical_m(self) -> np.ndarray:
        if not self.aircraft:
            return np.array([])
        v = np.concatenate([a.vertical_m for a in self.aircraft])
        return v[np.isfinite(v)]

    @property
    def horizontal_median_m(self) -> float:
        h = self.horizontal_m
        return float(np.median(h)) if h.size else float("nan")

    @property
    def agrees(self) -> bool:
        """True when the median horizontal difference is within the truth sigma."""
        return bool(self.aircraft) and self.horizontal_median_m <= self.sigma_m

    def table(self) -> str:
        header = (
            f"{'icao24':<8} {'samples':>7} {'h med m':>8} {'h p95 m':>8} "
            f"{'v mean ft':>9} {'v std ft':>8}  refs (a/b)"
        )
        rows = [header, "-" * len(header)]
        for a in sorted(self.aircraft, key=lambda x: x.icao24):
            rows.append(
                f"{a.icao24:<8} {a.samples:>7d} {a.horizontal_median_m:>8.1f} "
                f"{a.horizontal_p95_m:>8.1f} {a.vertical_mean_m * M_TO_FT:>9.0f} "
                f"{a.vertical_std_m * M_TO_FT:>8.0f}  {a.alt_reference_a}/{a.alt_reference_b}"
            )
        rows.append("-" * len(header))
        verdict = "AGREE" if self.agrees else "DISAGREE"
        rows.append(
            f"{len(self.aircraft)} aircraft in both, {len(self.only_in_a)} only in a, "
            f"{len(self.only_in_b)} only in b; median horizontal difference "
            f"{self.horizontal_median_m:.1f} m vs truth sigma {self.sigma_m:.0f} m: {verdict}"
        )
        return "\n".join(rows)


def _overlap_samples(
    a: FlightSegment, b: FlightSegment, step_s: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    lo, hi = max(a.t[0], b.t[0]), min(a.t[-1], b.t[-1])
    if hi - lo < step_s:
        return None
    t = np.arange(lo, hi, step_s)
    pa, _, va = a.evaluate(t)
    pb, _, vb = b.evaluate(t)
    ok = va & vb
    return t[ok], pa[ok], pb[ok]


def compare_truth(
    a: TruthSet,
    b: TruthSet,
    *,
    step_s: float = 5.0,
    min_samples: int = 10,
    sigma_m: float | None = None,
) -> ComparisonReport:
    """Compare two truth sets built in the same frame, aircraft by aircraft.

    Differences are ``b - a`` at common times every ``step_s`` seconds inside
    overlapping segments. ``sigma_m`` defaults to the larger of the two sets'
    horizontal truth sigma.
    """
    if a.frame != b.frame:
        raise ValueError("both truth sets must be built in the same LocalFrame")
    sigma = sigma_m or max(a.config.default_horizontal_sigma_m, b.config.default_horizontal_sigma_m)
    report = ComparisonReport(sigma_m=sigma)
    ia, ib = set(a.icao24s), set(b.icao24s)
    report.only_in_a = sorted(ia - ib)
    report.only_in_b = sorted(ib - ia)
    for icao in sorted(ia & ib):
        horiz, vert = [], []
        refs_a, refs_b = set(), set()
        for sa in a.by_icao(icao):
            for sb in b.by_icao(icao):
                samples = _overlap_samples(sa, sb, step_s)
                if samples is None or len(samples[0]) == 0:
                    continue
                _, pa, pb = samples
                horiz.append(np.hypot(pb[:, 0] - pa[:, 0], pb[:, 1] - pa[:, 1]))
                dz = pb[:, 2] - pa[:, 2]
                if not (sa.has_altitude and sb.has_altitude):
                    dz = np.full_like(dz, np.nan)
                vert.append(dz)
                refs_a.add(sa.alt_reference)
                refs_b.add(sb.alt_reference)
        if not horiz:
            continue
        h = np.concatenate(horiz)
        if len(h) < min_samples:
            continue
        report.aircraft.append(
            AircraftComparison(
                icao24=icao,
                samples=len(h),
                horizontal_m=h,
                vertical_m=np.concatenate(vert),
                alt_reference_a="/".join(sorted(str(r) for r in refs_a)),
                alt_reference_b="/".join(sorted(str(r) for r in refs_b)),
            )
        )
    return report


@dataclass
class AltitudeAssessment:
    """How a candidate source's altitude compares with known references."""

    verdict: Literal["baro", "geom", "inconclusive"]
    explanation: str
    vs_baro: ComparisonReport | None
    vs_geom: ComparisonReport | None

    @staticmethod
    def _stats(report: ComparisonReport | None) -> tuple[float, float, int]:
        if report is None or report.vertical_m.size == 0:
            return float("nan"), float("nan"), 0
        v = report.vertical_m * M_TO_FT
        return float(np.median(np.abs(v))), float(np.std(v)), len(report.aircraft)

    def summary(self) -> str:
        lines = []
        for name, report in (("barometric", self.vs_baro), ("geometric", self.vs_geom)):
            med, std, n = self._stats(report)
            if n:
                lines.append(
                    f"vs {name:<10}: median |diff| {med:6.0f} ft, std {std:5.0f} ft "
                    f"over {n} aircraft"
                )
            else:
                lines.append(f"vs {name:<10}: no reference data")
        lines.append(f"verdict: {self.verdict} ({self.explanation})")
        return "\n".join(lines)


def assess_altitude_reference(
    candidate: Iterable[TruthRecord],
    reference: Iterable[TruthRecord],
    frame: LocalFrame,
    *,
    candidate_field: Literal["unknown", "baro", "geom"] = "unknown",
    tolerance_ft: float = 50.0,
    config: TruthBuilderConfig | None = None,
) -> AltitudeAssessment:
    """Decide whether ``candidate``'s altitude is barometric or geometric.

    ``candidate`` is the source under test (for example an SBS export from a
    database, read with ``altitude_field="alt_unknown_ft"``). ``reference`` should come from a
    receiver that reports barometric altitude (any live SBS feed) and ideally
    geometric altitude too (``aircraft.json``), for the same aircraft and
    time window.

    The verdict is ``baro`` (or ``geom``) when the candidate's median
    absolute difference from that reference is within ``tolerance_ft`` and
    clearly smaller than from the other. Without a geometric reference, a
    barometric match is still reported as ``baro``, with a note that the
    geometric case could not be ruled out.
    """
    base = config or TruthBuilderConfig()
    candidate = list(candidate)
    reference = list(reference)

    def build(records: list[TruthRecord], ref: str) -> TruthSet:
        cfg = base.model_copy(update={"altitude_preference": [ref]})
        return build_truth(records, frame, cfg)

    cand = build(candidate, candidate_field)
    reports: dict[str, ComparisonReport | None] = {}
    for ref in ("baro", "geom"):
        truth = build(reference, ref)
        has_alt = any(s.has_altitude for s in truth)
        reports[ref] = compare_truth(cand, truth) if has_alt else None
    stats = {ref: AltitudeAssessment._stats(r) for ref, r in reports.items()}
    baro_med, geom_med = stats["baro"][0], stats["geom"][0]

    verdict: Literal["baro", "geom", "inconclusive"] = "inconclusive"
    if np.isnan(baro_med) and np.isnan(geom_med):
        why = "no overlapping aircraft with altitude in the reference"
    elif np.isnan(geom_med):
        if baro_med <= tolerance_ft:
            verdict = "baro"
            why = (
                f"matches barometric within {tolerance_ft:.0f} ft; no geometric reference, "
                "so a near-standard-pressure day could hide a geometric source"
            )
        else:
            why = f"differs from barometric by {baro_med:.0f} ft and no geometric reference"
    elif np.isnan(baro_med):
        verdict = "geom" if geom_med <= tolerance_ft else "inconclusive"
        why = f"median difference from geometric {geom_med:.0f} ft, no barometric reference"
    else:
        best, other = ("baro", "geom") if baro_med <= geom_med else ("geom", "baro")
        best_med, other_med = stats[best][0], stats[other][0]
        if best_med <= tolerance_ft and other_med >= 2 * best_med + tolerance_ft / 2:
            verdict = best  # type: ignore[assignment]
            why = f"{best_med:.0f} ft from {best} vs {other_med:.0f} ft from {other}"
        else:
            why = (
                f"{baro_med:.0f} ft from baro and {geom_med:.0f} ft from geom; "
                "not clearly separated (pressure may be near standard)"
            )
    return AltitudeAssessment(verdict, why, reports["baro"], reports["geom"])
