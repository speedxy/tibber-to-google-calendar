"""Tests für gsi_to_total()."""
from __future__ import annotations

import pytest

from tibber_charge.prices.forecast.corrently import gsi_to_total


def test_ankerpunkte_exakt():
    assert gsi_to_total(0) == pytest.approx(0.42)
    assert gsi_to_total(30) == pytest.approx(0.32)
    assert gsi_to_total(50) == pytest.approx(0.25)
    assert gsi_to_total(70) == pytest.approx(0.20)
    assert gsi_to_total(90) == pytest.approx(0.15)
    assert gsi_to_total(100) == pytest.approx(0.10)


def test_interpolation_zwischen_ankerpunkten():
    # Mitte zwischen 30 (0.32) und 50 (0.25) → 40 = 0.285
    assert gsi_to_total(40) == pytest.approx(0.285)
    # Mitte zwischen 50 (0.25) und 70 (0.20) → 60 = 0.225
    assert gsi_to_total(60) == pytest.approx(0.225)
    # Mitte zwischen 70 (0.20) und 90 (0.15) → 80 = 0.175
    assert gsi_to_total(80) == pytest.approx(0.175)


def test_clamping_negativ():
    assert gsi_to_total(-10) == pytest.approx(0.42)
    assert gsi_to_total(-1000) == pytest.approx(0.42)


def test_clamping_ueber_100():
    assert gsi_to_total(101) == pytest.approx(0.10)
    assert gsi_to_total(1000) == pytest.approx(0.10)


def test_monoton_fallend():
    """Mehr Erneuerbare → niedrigerer Preis."""
    prev = gsi_to_total(0)
    for gsi in range(0, 101, 5):
        curr = gsi_to_total(gsi)
        assert curr <= prev + 1e-9, f"Monotonie verletzt bei GSI {gsi}: {prev} → {curr}"
        prev = curr


def test_float_input_akzeptiert():
    assert gsi_to_total(50.5) == pytest.approx(0.25)  # wird auf int(50) gecastet
