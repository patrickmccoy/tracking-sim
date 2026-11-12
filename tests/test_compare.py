import dataclasses
from pathlib import Path

import pytest

from tracking_sim.cli import main
from tracking_sim.geo import LocalFrame
from tracking_sim.truth import TruthRecord, build_truth
from tracking_sim.truth.compare import assess_altitude_reference, compare_truth
from tracking_sim.truth.sources.recording import write_records
from tracking_sim.truth.synthetic import ReceiverConfig, SyntheticTraffic, generate_traffic


@pytest.fixture(scope="module")
def traffic() -> SyntheticTraffic:
    # A reference receiver that reports baro and geometric altitude (like
    # aircraft.json), and two exported-database-like sources with an unknown
    # altitude field: one really barometric, one really geometric.
    return generate_traffic(
        seed=3,
        duration_s=600,
        receivers=(
            ReceiverConfig("ref", report_geom=True),
            ReceiverConfig("ps-baro", altitude_field="alt_unknown_ft"),
            ReceiverConfig("ps-geom", altitude_field="alt_unknown_ft", altitude_kind="geom"),
        ),
    )


def records(traffic: SyntheticTraffic, source_id: str) -> list[TruthRecord]:
    return [r for r in traffic.records if r.source_id == source_id]


def test_independent_sources_agree_within_truth_sigma(traffic: SyntheticTraffic) -> None:
    frame = LocalFrame(*traffic.origin)
    report = compare_truth(
        build_truth(records(traffic, "ps-baro"), frame),
        build_truth(records(traffic, "ref"), frame),
    )
    assert report.agrees
    assert report.horizontal_median_m < 15.0
    assert {a.icao24 for a in report.aircraft} == {f.icao24 for f in traffic.flights}
    assert "AGREE" in report.table()


def test_offset_source_disagrees(traffic: SyntheticTraffic) -> None:
    frame = LocalFrame(*traffic.origin)
    shifted = [
        dataclasses.replace(r, lat=r.lat + 0.001) if r.lat is not None else r  # ~111 m north
        for r in records(traffic, "ps-baro")
    ]
    report = compare_truth(build_truth(shifted, frame), build_truth(records(traffic, "ref"), frame))
    assert not report.agrees
    assert report.horizontal_median_m == pytest.approx(111, abs=10)


@pytest.mark.parametrize(("source", "verdict"), [("ps-baro", "baro"), ("ps-geom", "geom")])
def test_altitude_reference_is_identified(
    traffic: SyntheticTraffic, source: str, verdict: str
) -> None:
    frame = LocalFrame(*traffic.origin)
    result = assess_altitude_reference(records(traffic, source), records(traffic, "ref"), frame)
    assert result.verdict == verdict, result.summary()


def test_altitude_reference_without_geometric_reference(traffic: SyntheticTraffic) -> None:
    frame = LocalFrame(*traffic.origin)
    baro_only = [dataclasses.replace(r, alt_geom_ft=None) for r in records(traffic, "ref")]
    result = assess_altitude_reference(records(traffic, "ps-baro"), baro_only, frame)
    assert result.verdict == "baro"
    assert "no geometric reference" in result.explanation
    result = assess_altitude_reference(records(traffic, "ps-geom"), baro_only, frame)
    assert result.verdict == "inconclusive"


def test_cli_compare(
    traffic: SyntheticTraffic, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = tmp_path / "ps.parquet", tmp_path / "ref.parquet"
    write_records(a, records(traffic, "ps-baro"))
    write_records(b, records(traffic, "ref"))
    assert main(["compare", str(a), str(b), "--altitude"]) == 0
    out = capsys.readouterr().out
    assert "AGREE" in out and "verdict: baro" in out
