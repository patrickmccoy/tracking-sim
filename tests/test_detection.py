from pathlib import Path

import numpy as np
import pytest

from tracking_sim.sensors.detection import (
    AircraftClasses,
    load_aircraft_classes,
    pd_swerling,
    snr_radar_equation,
)


def test_radar_equation_scales_with_rcs_and_range_to_the_fourth() -> None:
    snr = snr_radar_equation([50_000.0, 100_000.0], [1.0, 1.0], 13.0, 50_000.0)
    assert 10 * np.log10(snr[0]) == pytest.approx(13.0)
    assert 10 * np.log10(snr[0] / snr[1]) == pytest.approx(40 * np.log10(2))
    assert snr_radar_equation(50_000.0, 20.0, 13.0, 50_000.0) == pytest.approx(20 * 10**1.3)


def test_swerling_pd() -> None:
    pfa = 1e-6
    # Swerling 1 is closed form: Pd = Pfa ** (1 / (1 + SNR)).
    assert pd_swerling(0.0, pfa, 1) == pytest.approx(pfa)
    assert pd_swerling(100.0, pfa, 1) == pytest.approx(pfa ** (1 / 101))
    # Swerling 0: about 13.2 dB for Pd 0.9 at Pfa 1e-6 (single pulse, textbook value).
    assert pd_swerling(10**1.32, pfa, 0) == pytest.approx(0.9, abs=0.01)
    assert pd_swerling(0.0, pfa, 0) == pytest.approx(pfa, rel=1e-6)
    # A fluctuating target needs more SNR for the same high Pd.
    snr = 10**1.32
    assert pd_swerling(snr, pfa, 1) < pd_swerling(snr, pfa, 0)
    # Monte Carlo check of the steady-target formula.
    rng = np.random.default_rng(0)
    s = 10.0
    noise = (rng.standard_normal(200_000) + 1j * rng.standard_normal(200_000)) / np.sqrt(2)
    power = np.abs(np.sqrt(s) + noise) ** 2
    threshold = -np.log(1e-3)
    assert np.mean(power > threshold) == pytest.approx(pd_swerling(s, 1e-3, 0), abs=0.005)


def test_aircraft_classes(tmp_path: Path) -> None:
    csv = tmp_path / "classes.csv"
    csv.write_text("icao24,class\nABC123,heavy\nabc124 , rotorcraft\n,\n")
    table = load_aircraft_classes(csv)
    assert table == {"abc123": "heavy", "abc124": "rotorcraft"}
    classes = AircraftClasses(table, default="small")
    assert classes.classify("ABC123", "A1") == "heavy"  # the table wins
    assert classes.classify("def456", "a5") == "heavy"
    assert classes.classify("def456", "A7") == "rotorcraft"
    assert classes.classify("def456", "C1") == "small"  # surface vehicle: default
    assert classes.classify("def456", None) == "small"
    csv.write_text("icao24,class\nabc123,zeppelin\n")
    with pytest.raises(ValueError, match="zeppelin"):
        load_aircraft_classes(csv)
