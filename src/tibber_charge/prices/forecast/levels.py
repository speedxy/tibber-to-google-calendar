"""Dynamische Level-Klassifizierung für Forecast-Preise.

Tibbers absolute Schwellwerte werden bewusst NICHT übernommen — unsere
Forecast-Skala ist heuristisch und kann verzerrt sein. Quantil-basierte
Klassifizierung auf der Verteilung des aktuellen Forecast-Fensters gibt
konsistente Kalender-Optik unabhängig von der absoluten Skala.
"""
from __future__ import annotations

from dataclasses import replace

from tibber_charge.prices.types import UnifiedPrice


def compute_dynamic_level(total: float, distribution: list[float]) -> str:
    """Klassifiziert einen Forecast-Preis relativ zur Verteilung der anderen Forecast-Preise.

    Regeln:
        total < 0           → NEGATIVE
        total < Q20         → VERY_CHEAP
        total < Q40         → CHEAP
        total < Q70         → NORMAL
        total < Q90         → EXPENSIVE
        sonst               → VERY_EXPENSIVE

    Bei leerer oder uniformer Verteilung → NORMAL (außer NEGATIVE).
    """
    if total < 0:
        return "NEGATIVE"
    if not distribution:
        return "NORMAL"

    q20 = _quantile(distribution, 0.20)
    q40 = _quantile(distribution, 0.40)
    q70 = _quantile(distribution, 0.70)
    q90 = _quantile(distribution, 0.90)

    # Uniforme Verteilung (alle Quantile gleich) → alles NORMAL
    if q20 == q90:
        return "NORMAL"

    if total < q20:
        return "VERY_CHEAP"
    if total < q40:
        return "CHEAP"
    if total < q70:
        return "NORMAL"
    if total < q90:
        return "EXPENSIVE"
    return "VERY_EXPENSIVE"


def apply_dynamic_levels(prices: list[UnifiedPrice]) -> list[UnifiedPrice]:
    """Setzt für jeden Preis das Level relativ zur Gesamtverteilung."""
    if not prices:
        return []
    distribution = [p.total for p in prices]
    return [replace(p, level=compute_dynamic_level(p.total, distribution)) for p in prices]


def _quantile(values: list[float], q: float) -> float:
    """Lineare Interpolation, Index-basiert auf sortierter Kopie. Pure stdlib."""
    if not values:
        raise ValueError("leere Distribution")
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    idx = q * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    fraction = idx - lo
    return sorted_values[lo] + fraction * (sorted_values[hi] - sorted_values[lo])
