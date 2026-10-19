"""EpexPredictor als Forecast-Provider (Standard).

Open-Source-Prognose der EPEX-Day-Ahead-Börsenpreise (LightGBM auf Wetter
mehrerer Standorte, Netzlast, Gaspreis, Kalender; rollierend 180 Tage
trainiert). Liefert für bereits auktionierte Stunden die echten Börsenpreise,
danach Prognosen, insgesamt rund 7 Tage in 15-Minuten-Auflösung.

Projekt: https://github.com/b3nn0/EpexPredictor (BSD-3). Öffentliche Instanz
ohne Garantie und mit "fair use" — wir fragen nur 2×/Tag ab. Selbst-Hosting
per Docker ist möglich (`base_url` umstellen).

WICHTIG: `raw_total` ist der reine Börsenpreis in €/kWh netto, NICHT der
Tibber-Endpreis. Die Umrechnung (Fixkosten + MwSt) lernt `prices/mapping.py`
aus überlappenden Tibber-Stunden. Ohne gelernte Umrechnung darf diese
Prognose nicht in die Planung.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from tibber_charge.prices.forecast.penalty import forecast_confidence, forecast_penalty
from tibber_charge.prices.types import UnifiedPrice


EPEX_PREDICTOR_URL = "https://epexpredictor.batzill.com/prices_short"


class EpexPredictorForecastProvider:
    name = "epex_predictor"
    raw_is_spot = True

    def __init__(
        self,
        region: str = "DE",
        base_url: str = EPEX_PREDICTOR_URL,
        timeout_seconds: float = 10.0,
        horizon_days: int = 7,
    ) -> None:
        self.region = region
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.horizon_hours = max(24, min(168, horizon_days * 24))

    def fetch(self, now: datetime) -> list[UnifiedPrice]:
        logging.info(f"📡 Sende Forecast-Anfrage an EpexPredictor ({self.region}, {self.horizon_hours} h)...")
        response = requests.get(
            self.base_url,
            params={
                "region": self.region,
                "surcharge": 0,      # reiner Börsenpreis, Umrechnung lernt mapping.py
                "taxPercent": 0,
                "unit": "EUR_PER_KWH",
                "hours": self.horizon_hours,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()

        starts = data.get("s") or []
        values = data.get("t") or []
        if not starts:
            raise RuntimeError("EpexPredictor lieferte keine Prognose-Einträge.")
        if len(starts) != len(values):
            raise RuntimeError(f"EpexPredictor-Antwort inkonsistent: s={len(starts)}, t={len(values)}")

        # 15-Minuten-Werte zu Stundenmitteln zusammenfassen (Tibber und Planer arbeiten stündlich).
        local_tz = now.astimezone().tzinfo
        by_hour: dict[datetime, list[float]] = {}
        for epoch, value in zip(starts, values):
            if value is None:
                continue
            try:
                ts = datetime.fromtimestamp(float(epoch), tz=timezone.utc).astimezone(local_tz)
                by_hour.setdefault(ts.replace(minute=0, second=0, microsecond=0), []).append(float(value))
            except (TypeError, ValueError, OverflowError):
                continue

        prices: list[UnifiedPrice] = []
        for hour in sorted(by_hour):
            spot = sum(by_hour[hour]) / len(by_hour[hour])
            prices.append(
                UnifiedPrice(
                    startsAt=hour.isoformat(),
                    total=spot + forecast_penalty(hour, now),
                    level="NORMAL",
                    source=self.name,
                    confidence=forecast_confidence(hour, now),
                    raw_total=spot,
                    meta={"spot_eur_kwh": round(spot, 5), "region": self.region},
                )
            )
        logging.info(f"✅ EpexPredictor-Antwort: {len(prices)} Forecast-Stunden geparst.")
        return prices
