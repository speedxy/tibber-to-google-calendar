"""Corrently GSI (GrünstromIndex) als Forecast-Provider.

API: https://api.corrently.io/v2.0/gsi/prediction?zip=<PLZ>
Lieferung: stündlicher GSI-Score (0..100) für ca. 96–120 Stunden.
GSI → €/kWh per stückweise linearer Interpolation über Ankerpunkte.

Heuristisch — die Skala ist kalibriert auf typische deutsche Tibber-Spannen,
aber NICHT cent-genau. Mit `penalty` wird der Planer Risiko-konservativ
gestimmt; siehe `prices/forecast/penalty.py`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from tibber_charge.prices.forecast.penalty import forecast_confidence, forecast_penalty
from tibber_charge.prices.types import UnifiedPrice


CORRENTLY_API_URL = "https://api.corrently.io/v2.0/gsi/prediction"

# Ankerpunkte (GSI, €/kWh). Stückweise linear interpoliert.
# Kalibriert auf beobachtete Tibber-Preise in DE (inkl. Steuern/Abgaben/Netz).
# 2026-05-20 Korrektur: vorherige Kurve war zu pessimistisch — Median lag bei
# 28 ct, Tibber-Realität eher 22–25 ct. Untere Anker hinaufgezogen, da fixe
# Kostenkomponenten (Stromsteuer + Netz + Konzession + MwSt) selbst bei
# negativem Spotpreis ~10–15 ct als Untergrenze produzieren.
_GSI_ANCHORS: list[tuple[int, float]] = [
    (0,   0.42),   # 0 % erneuerbar — Winter-Peak / Gaspeak
    (30,  0.32),
    (50,  0.25),   # Median
    (70,  0.20),
    (90,  0.15),
    (100, 0.10),   # Maximaler EE-Überschuss, durch fixe Kostenkomponenten gedeckelt
]


def gsi_to_total(gsi: int | float) -> float:
    """Liefert geschätzten Rohpreis (€/kWh, ohne Penalty) für einen GSI-Wert.

    Werte außerhalb 0..100 werden geclampt.
    """
    g = max(0, min(100, int(gsi)))
    for (g0, p0), (g1, p1) in zip(_GSI_ANCHORS, _GSI_ANCHORS[1:]):
        if g0 <= g <= g1:
            if g1 == g0:
                return p0
            ratio = (g - g0) / (g1 - g0)
            return p0 + ratio * (p1 - p0)
    return _GSI_ANCHORS[-1][1]


class CorrentlyForecastProvider:
    """Holt den GSI-Forecast und mappt ihn auf UnifiedPrice-Einträge."""

    name = "corrently"

    def __init__(
        self,
        zip_code: str,
        timeout_seconds: float = 10.0,
        api_token: str | None = None,
    ) -> None:
        self.zip_code = zip_code
        self.timeout_seconds = timeout_seconds
        self.api_token = api_token

    def fetch(self, now: datetime) -> list[UnifiedPrice]:
        params: dict[str, str] = {"zip": self.zip_code}
        if self.api_token:
            params["token"] = self.api_token
            token_note = "mit Token"
        else:
            token_note = "anonym"
        logging.info(f"📡 Sende Forecast-Anfrage an Corrently (PLZ {self.zip_code}, {token_note})...")
        response = requests.get(
            CORRENTLY_API_URL,
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()

        entries = _extract_entries(data)
        if not entries:
            hint = ""
            if isinstance(data, dict):
                info = data.get("info") or data.get("provisioning", {}).get("warning")
                if info:
                    hint = f" (API-Hinweis: {info!r})"
                if not self.api_token:
                    hint += (
                        " — anonymer Zugang liefert leeren Forecast; "
                        "Token bei https://console.corrently.io/ holen und in "
                        "config.json unter FORECAST.api_token eintragen."
                    )
            raise RuntimeError(f"Corrently lieferte keine Forecast-Einträge.{hint}")

        local_tz = now.astimezone().tzinfo
        prices: list[UnifiedPrice] = []
        skipped = 0
        for entry in entries:
            try:
                epoch = entry.get("epochtime") or entry.get("timeStamp") or entry.get("timestamp")
                gsi = entry.get("gsi")
                if epoch is None or gsi is None:
                    skipped += 1
                    continue
                # Corrently liefert epoch in Sekunden oder Millisekunden
                epoch_seconds = int(epoch)
                if epoch_seconds > 10**12:  # ms statt s
                    epoch_seconds //= 1000
                ts_utc = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
                ts_local = ts_utc.astimezone(local_tz)
            except (TypeError, ValueError):
                skipped += 1
                continue

            raw_total = gsi_to_total(gsi)
            penalty = forecast_penalty(ts_local, now)
            total = raw_total + penalty
            confidence = forecast_confidence(ts_local, now)

            prices.append(
                UnifiedPrice(
                    startsAt=ts_local.isoformat(),
                    total=total,
                    level="NORMAL",  # finales Level setzt der Quantil-Klassifizierer in PR 3
                    source=self.name,
                    confidence=confidence,
                    raw_total=raw_total,
                    meta={
                        "gsi": int(gsi),
                        "zip_code": self.zip_code,
                    },
                )
            )

        if skipped:
            logging.info(f"ℹ️ {skipped} Corrently-Einträge ohne brauchbares gsi/epoch übersprungen.")

        logging.info(f"✅ Corrently-Antwort: {len(prices)} Forecast-Stunden geparst.")
        return prices


def _extract_entries(data: dict | list) -> list[dict]:
    """Corrently kann das Forecast-Array unter `forecast`, `data` oder als Wurzel ausliefern.
    Defensiv alle Varianten akzeptieren.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("forecast", "data", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []
