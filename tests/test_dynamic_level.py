"""Tests für compute_dynamic_level + apply_dynamic_levels."""
from __future__ import annotations

from tibber_charge.prices.forecast.levels import (
    apply_dynamic_levels,
    compute_dynamic_level,
)
from tibber_charge.prices.types import UnifiedPrice


def _make(total: float, level: str = "NORMAL") -> UnifiedPrice:
    return UnifiedPrice(
        startsAt="2026-05-22T10:00:00+02:00",
        total=total, level=level,
        source="open_meteo", confidence=0.4,
    )


def test_negative_immer_NEGATIVE():
    assert compute_dynamic_level(-0.01, []) == "NEGATIVE"
    assert compute_dynamic_level(-5.0, [0.1, 0.2, 0.3]) == "NEGATIVE"


def test_leere_distribution_NORMAL():
    assert compute_dynamic_level(0.5, []) == "NORMAL"


def test_uniforme_distribution_NORMAL():
    # Alle Werte gleich → NORMAL für alle (außer NEGATIVE)
    distribution = [0.25] * 10
    assert compute_dynamic_level(0.25, distribution) == "NORMAL"
    assert compute_dynamic_level(0.30, distribution) == "NORMAL"


def test_quantile_zuordnung():
    # Klare Verteilung: 0.10, 0.15, 0.20, ..., 0.55 (10 Werte)
    distribution = [0.10 + 0.05 * i for i in range(10)]
    # Q20 ≈ 0.19, Q40 ≈ 0.28, Q70 ≈ 0.415, Q90 ≈ 0.505
    assert compute_dynamic_level(0.05, distribution) == "VERY_CHEAP"   # unter Q20
    assert compute_dynamic_level(0.22, distribution) == "CHEAP"        # zwischen Q20 und Q40
    assert compute_dynamic_level(0.35, distribution) == "NORMAL"       # zwischen Q40 und Q70
    assert compute_dynamic_level(0.45, distribution) == "EXPENSIVE"    # zwischen Q70 und Q90
    assert compute_dynamic_level(0.60, distribution) == "VERY_EXPENSIVE"  # über Q90


def test_apply_dynamic_levels_leer():
    assert apply_dynamic_levels([]) == []


def test_apply_dynamic_levels_setzt_levels():
    prices = [_make(0.10), _make(0.20), _make(0.30), _make(0.40), _make(0.50)]
    result = apply_dynamic_levels(prices)
    assert len(result) == 5
    levels = [p.level for p in result]
    # Mindestens ein CHEAP/VERY_CHEAP unten und EXPENSIVE/VERY_EXPENSIVE oben
    assert levels[0] in ("VERY_CHEAP", "CHEAP")
    assert levels[-1] in ("EXPENSIVE", "VERY_EXPENSIVE")


def test_apply_dynamic_levels_erhaelt_andere_felder():
    prices = [_make(0.10), _make(0.50)]
    result = apply_dynamic_levels(prices)
    assert all(p.source == "open_meteo" for p in result)
    assert all(p.confidence == 0.4 for p in result)
    assert result[0].total == 0.10
    assert result[1].total == 0.50


def test_apply_dynamic_levels_setzt_negative_auf_NEGATIVE():
    prices = [_make(-0.05), _make(0.10), _make(0.20), _make(0.30)]
    result = apply_dynamic_levels(prices)
    assert result[0].level == "NEGATIVE"
    assert result[1].level in ("VERY_CHEAP", "CHEAP", "NORMAL")
