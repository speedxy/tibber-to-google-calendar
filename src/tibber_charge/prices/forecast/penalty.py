"""Forecast-Penalty: linearer Risikoaufschlag in €/kWh.

Idee: Tibber-Domäne ist cent-genau (0 Penalty), Forecast wird mit wachsender
zeitlicher Distanz unsicherer und bekommt einen Aufschlag, damit der Planer
nicht eine spekulative ferne Vorhersage über eine nahe Realität bevorzugt.
"""
from __future__ import annotations

from datetime import datetime


def forecast_penalty(forecasted_hour: datetime, now: datetime) -> float:
    """Penalty in €/kWh als Funktion der Stunden-Distanz zu `now`.

    Kurve:
        h < 36           → 0.00 (sollte in Tibber-Domäne nicht aufgerufen werden)
        36 ≤ h < 48      → 0.01
        48 ≤ h < 96      → linear 0.01 → 0.03
        96 ≤ h ≤ 168     → linear 0.03 → 0.05 (Cap)
        h > 168          → 0.05 (Cap)
    """
    hours_ahead = (forecasted_hour - now).total_seconds() / 3600
    if hours_ahead < 36:
        return 0.00
    if hours_ahead < 48:
        return 0.01
    if hours_ahead < 96:
        ratio = (hours_ahead - 48) / (96 - 48)
        return 0.01 + ratio * (0.03 - 0.01)
    if hours_ahead < 168:
        ratio = (hours_ahead - 96) / (168 - 96)
        return 0.03 + ratio * (0.05 - 0.03)
    return 0.05


def forecast_confidence(forecasted_hour: datetime, now: datetime) -> float:
    """Konfidenz-Wert (0..1) als Funktion der Distanz.

    Spiegelbild zum Penalty, aber zum Anzeigen/Loggen — nicht zum Rechnen.
    """
    hours_ahead = (forecasted_hour - now).total_seconds() / 3600
    if hours_ahead < 36:
        return 1.0
    if hours_ahead < 48:
        return 0.6
    if hours_ahead < 96:
        ratio = (hours_ahead - 48) / (96 - 48)
        return 0.6 - ratio * (0.6 - 0.45)
    if hours_ahead < 168:
        ratio = (hours_ahead - 96) / (168 - 96)
        return 0.45 - ratio * (0.45 - 0.30)
    return 0.30
