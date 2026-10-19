"""Tests für forecast_penalty + forecast_confidence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tibber_charge.prices.forecast.penalty import forecast_confidence, forecast_penalty


NOW = datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc)


def _h(hours: float) -> datetime:
    return NOW + timedelta(hours=hours)


def test_penalty_unter_36h_ist_null():
    assert forecast_penalty(_h(0), NOW) == 0.0
    assert forecast_penalty(_h(35.9), NOW) == 0.0


def test_penalty_zwischen_36_und_48():
    assert forecast_penalty(_h(36), NOW) == 0.01
    assert forecast_penalty(_h(47.9), NOW) == 0.01


def test_penalty_linear_48_bis_96():
    assert forecast_penalty(_h(48), NOW) == pytest.approx(0.01)
    assert forecast_penalty(_h(72), NOW) == pytest.approx(0.02, abs=1e-9)
    assert forecast_penalty(_h(95.999), NOW) == pytest.approx(0.03, abs=1e-4)


def test_penalty_linear_96_bis_168():
    assert forecast_penalty(_h(96), NOW) == pytest.approx(0.03)
    assert forecast_penalty(_h(132), NOW) == pytest.approx(0.04, abs=1e-9)
    assert forecast_penalty(_h(167.999), NOW) == pytest.approx(0.05, abs=1e-4)


def test_penalty_cap_bei_5ct():
    assert forecast_penalty(_h(168), NOW) == 0.05
    assert forecast_penalty(_h(500), NOW) == 0.05


def test_penalty_monoton_wachsend():
    samples = [forecast_penalty(_h(h), NOW) for h in range(0, 200, 6)]
    for prev, curr in zip(samples, samples[1:]):
        assert curr >= prev, f"Penalty muss monoton wachsen: {prev} → {curr}"


def test_confidence_monoton_fallend():
    samples = [forecast_confidence(_h(h), NOW) for h in range(0, 200, 6)]
    for prev, curr in zip(samples, samples[1:]):
        assert curr <= prev, f"Konfidenz muss monoton fallen: {prev} → {curr}"


def test_confidence_in_tibber_domaene_ist_eins():
    assert forecast_confidence(_h(0), NOW) == 1.0
    assert forecast_confidence(_h(35.9), NOW) == 1.0


def test_confidence_cap_bei_0_3():
    assert forecast_confidence(_h(168), NOW) == 0.30
    assert forecast_confidence(_h(500), NOW) == 0.30
