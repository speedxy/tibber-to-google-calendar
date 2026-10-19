"""Kalibrierungs-Logging: Forecast-Vorhersagen mit realen Tibber-Preisen pairen.

Workflow:
1. `record_predictions(state, forecasts, now)` legt für jede Forecast-Stunde
   einen Sample-Eintrag an. **First-write-wins** pro `target_hour` — wir
   bevorzugen die früheste/blindeste Vorhersage als härtesten Heuristik-Test.
2. Bei jedem Lauf prüft `match_observations(state, now)`, ob für unbefüllte
   Samples die Stunde inzwischen in `state.tibber` veröffentlicht wurde, und
   schreibt `actual_total`.
3. FIFO-Cap (500), drop unbefüllte Samples zuerst — gefüllte Pairs sind die
   eigentlich wertvolle Datenbasis für späteres Curve-Fitting (V1.5+).

Kein Auto-Tuning in V1 — die Daten landen in `state.forecast.calibration_samples`
und werden via `scripts/calibration_report.py` ausgewertet.
"""
from __future__ import annotations

import logging
from datetime import datetime

from dateutil import parser as date_parser

from tibber_charge.prices.mapping import hour_key
from tibber_charge.prices.types import UnifiedPrice
from tibber_charge.state import State


# Cap auf den Sample-Puffer. ~200 Bytes/Sample * 500 = 100 KB im state.json.
MAX_SAMPLES = 500


def record_predictions(state: State, forecasts: list[UnifiedPrice], now: datetime) -> int:
    """Legt für jede neue Forecast-Stunde einen Kalibrierungs-Sample an.

    Erfasst alle relevanten Inputfeatures, damit V2 ein Regression-Modell
    fitten kann, ohne erneut die Wetter-API zu pollen.

    Returnt die Anzahl neu angelegter Samples.
    """
    samples = state.forecast.calibration_samples
    seen = {hour_key(s.get("target_hour") or "") for s in samples}
    new_count = 0

    for up in forecasts:
        key = hour_key(up.startsAt)
        if key is None or key in seen:
            continue  # First-write-wins: behalte die früheste Vorhersage
        try:
            ts = date_parser.isoparse(up.startsAt)
        except (ValueError, TypeError):
            continue
        hours_ahead = (ts - now).total_seconds() / 3600.0
        meta = up.meta or {}
        sample = {
            "target_hour": up.startsAt,
            "forecasted_at": now.isoformat(),
            "hours_ahead_at_forecast": round(hours_ahead, 1),

            # Forecast outputs
            "forecast_raw_total": up.raw_total,
            "forecast_total": up.total,
            "forecast_source": up.source,

            # Provider raw features (für späteres Regression-Fitting)
            "wind_kmh": meta.get("wind_kmh"),
            "solar_wm2": meta.get("solar_wm2"),
            "temperature_c": meta.get("temperature_c"),
            "renewable_index": meta.get("renewable_index"),
            "gsi": meta.get("gsi"),
            "latitude": meta.get("latitude"),
            "longitude": meta.get("longitude"),
            "zip_code": meta.get("zip_code"),

            # Kontext-Features (aus target_hour ableitbar, hier für Convenience)
            "hour_of_day_local": ts.hour,
            "weekday": ts.weekday(),         # 0=Mo, 6=So
            "month": ts.month,

            # Reality — wird nachträglich von match_observations gefüllt
            "actual_total": None,
            "actual_filled_at": None,
            "actual_tibber_level": None,
        }
        samples.append(sample)
        seen.add(key)
        new_count += 1

    if new_count:
        logging.info(f"📊 Kalibrierung: {new_count} neue Prognose-Samples angelegt.")
    _trim_samples(state)
    return new_count


def match_observations(state: State, now: datetime) -> int:
    """Für jeden unbefüllten Sample, schau ob die Stunde inzwischen in Tibber ist.

    Erfasst zusätzlich Tibbers eigenes Level (CHEAP/NORMAL/...) als
    quantilbasiertes Vergleichs-Signal für späteres Bias-Diagnose.

    Returnt die Anzahl gerade gematchter Pairs.
    """
    # Abgleich über einen normalisierten Stundenschlüssel, NICHT über den Text:
    # Tibber schreibt "…T00:00:00.000+02:00", Prognosen "…T00:00:00+02:00".
    # Bis 2026-10-19 wurde per Textvergleich nie ein einziges Sample gematcht.
    tibber_by_hour: dict[str, dict] = {}
    tibber = state.tibber
    for p in (
        list(tibber.yesterday_prices or [])
        + list(tibber.today_prices or [])
        + list(tibber.tomorrow_prices or [])
    ):
        key = hour_key(p.get("startsAt") or "")
        total = p.get("total")
        if key is None or total is None:
            continue
        try:
            tibber_by_hour[key] = {
                "total": float(total),
                "level": p.get("level"),
            }
        except (TypeError, ValueError):
            continue

    if not tibber_by_hour:
        return 0

    filled = 0
    for s in state.forecast.calibration_samples:
        if s.get("actual_total") is not None:
            continue
        match = tibber_by_hour.get(hour_key(s.get("target_hour") or ""))
        if match is not None:
            s["actual_total"] = match["total"]
            s["actual_tibber_level"] = match.get("level")
            s["actual_filled_at"] = now.isoformat()
            filled += 1

    if filled:
        logging.info(f"📊 Kalibrierung: {filled} Forecast-Stunden mit Tibber-Realität gematcht.")
    return filled


def _trim_samples(state: State, max_samples: int = MAX_SAMPLES) -> None:
    """FIFO-Trim mit Preference für gefüllte Samples.

    Bei Überschreitung des Limits: gefüllte Pairs bleiben, unbefüllte werden
    nach Alter (oldest first) gedroppt. Wenn auch nach Drop-Unfilled noch zu
    viele bleiben, dann oldest gefüllte raus.
    """
    samples = state.forecast.calibration_samples
    if len(samples) <= max_samples:
        return

    filled = [s for s in samples if s.get("actual_total") is not None]
    unfilled = [s for s in samples if s.get("actual_total") is None]

    # Beide nach forecasted_at sortieren (oldest first)
    filled.sort(key=lambda s: s.get("forecasted_at") or "")
    unfilled.sort(key=lambda s: s.get("forecasted_at") or "")

    # Bevorzugt unfilled droppen
    overflow = len(samples) - max_samples
    if overflow >= len(unfilled):
        # Alle unfilled raus, dann oldest filled
        remaining = max_samples
        kept_filled = filled[-remaining:] if remaining > 0 else []
        state.forecast.calibration_samples = kept_filled
        logging.info(
            f"📊 Kalibrierungs-Puffer auf {max_samples} getrimmt — "
            f"{len(unfilled)} unbefüllte verworfen, {len(filled) - len(kept_filled)} älteste gefüllte verworfen."
        )
    else:
        kept_unfilled = unfilled[overflow:]  # die neuesten unfilled bleiben
        state.forecast.calibration_samples = filled + kept_unfilled
        logging.info(
            f"📊 Kalibrierungs-Puffer auf {max_samples} getrimmt — "
            f"{overflow} älteste unbefüllte verworfen."
        )
