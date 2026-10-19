"""Open-Meteo als Forecast-Provider.

Holt 7-Tage-Wetterforecast (Wind 100m, Solar-Strahlung) für eine
Koordinate und mapped das auf einen 0..100-EE-Index, der dann durch
die GSI-Ankerpunkte zum Preis interpoliert wird.

API: https://open-meteo.com/  — anonym, ohne Token, gute Verfügbarkeit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from tibber_charge.prices.forecast.corrently import gsi_to_total
from tibber_charge.prices.forecast.penalty import forecast_confidence, forecast_penalty
from tibber_charge.prices.types import UnifiedPrice


OPEN_METEO_API_URL = "https://api.open-meteo.com/v1/forecast"

# Normalisierungs-Referenzwerte für den EE-Index.
# Wind 100m in km/h: 30 km/h ≈ 8 m/s entspricht hohem Wind in Norddeutschland
# (P95 für Lübeck). 60 km/h (alte Referenz) wurde quasi nie erreicht, was
# den Index dauerhaft drückte und Preise systematisch zu hoch ausspielte.
_WIND_REF_KMH = 30.0
# Solar shortwave in W/m²: ~600 W/m² ≈ heller Sommertag (P90).
_SOLAR_REF_WM2 = 600.0
# Gewichtung: deutsche EE-Erzeugung ist windlastig.
_WIND_WEIGHT = 0.7
_SOLAR_WEIGHT = 0.3


def weather_to_renewable_index(
    wind_speed_100m_kmh: float,
    shortwave_radiation_wm2: float,
) -> float:
    """Mappt Wetter auf einen 0..100-EE-Index analog zum Corrently GSI.

    Hohe Werte ⇒ viel EE-Erzeugung erwartet ⇒ niedriger Preis.
    """
    wind = max(0.0, min(100.0, wind_speed_100m_kmh / _WIND_REF_KMH * 100.0))
    solar = max(0.0, min(100.0, shortwave_radiation_wm2 / _SOLAR_REF_WM2 * 100.0))
    return _WIND_WEIGHT * wind + _SOLAR_WEIGHT * solar


class OpenMeteoForecastProvider:
    """Forecast aus Wetterdaten."""

    name = "open_meteo"

    def __init__(
        self,
        latitude: float,
        longitude: float,
        timeout_seconds: float = 10.0,
        forecast_days: int = 7,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.timeout_seconds = timeout_seconds
        self.forecast_days = max(1, min(16, forecast_days))

    def fetch(self, now: datetime) -> list[UnifiedPrice]:
        logging.info(
            f"📡 Sende Forecast-Anfrage an Open-Meteo ({self.latitude}, {self.longitude}, "
            f"{self.forecast_days}d)..."
        )
        response = requests.get(
            OPEN_METEO_API_URL,
            params={
                "latitude": self.latitude,
                "longitude": self.longitude,
                # temperature_2m ist heute unused für die Heuristik, aber wird in
                # die Kalibrierungs-Samples gelegt — Heizbedarf treibt Winter-Peaks.
                "hourly": "wind_speed_100m,shortwave_radiation,temperature_2m",
                "forecast_days": self.forecast_days,
                "timezone": "UTC",
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()

        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        winds = hourly.get("wind_speed_100m") or []
        solars = hourly.get("shortwave_radiation") or []
        temps = hourly.get("temperature_2m") or [None] * len(times)

        if not times:
            raise RuntimeError("Open-Meteo lieferte keine Forecast-Einträge.")
        if not (len(times) == len(winds) == len(solars)):
            raise RuntimeError(
                f"Open-Meteo-Antwort inkonsistent: time={len(times)}, "
                f"wind={len(winds)}, solar={len(solars)}"
            )

        local_tz = now.astimezone().tzinfo
        prices: list[UnifiedPrice] = []
        skipped = 0
        for i, t_iso in enumerate(times):
            try:
                wind = winds[i]
                solar = solars[i]
                temp = temps[i] if i < len(temps) else None
                if wind is None or solar is None:
                    skipped += 1
                    continue
                # Open-Meteo "time" mit timezone=UTC ist ISO ohne Offset-Suffix
                ts_naive = datetime.fromisoformat(t_iso)
                ts_utc = ts_naive.replace(tzinfo=timezone.utc) if ts_naive.tzinfo is None else ts_naive
                ts_local = ts_utc.astimezone(local_tz)
            except (ValueError, TypeError, IndexError):
                skipped += 1
                continue

            ren_idx = weather_to_renewable_index(float(wind), float(solar))
            raw_total = gsi_to_total(int(round(ren_idx)))
            penalty = forecast_penalty(ts_local, now)
            total = raw_total + penalty
            confidence = forecast_confidence(ts_local, now)

            prices.append(
                UnifiedPrice(
                    startsAt=ts_local.isoformat(),
                    total=total,
                    level="NORMAL",
                    source=self.name,
                    confidence=confidence,
                    raw_total=raw_total,
                    meta={
                        "wind_kmh": float(wind),
                        "solar_wm2": float(solar),
                        "temperature_c": float(temp) if temp is not None else None,
                        "renewable_index": round(ren_idx, 1),
                        "latitude": self.latitude,
                        "longitude": self.longitude,
                    },
                )
            )

        if skipped:
            logging.info(f"ℹ️ {skipped} Open-Meteo-Stunden ohne Werte übersprungen.")
        logging.info(f"✅ Open-Meteo-Antwort: {len(prices)} Forecast-Stunden geparst.")
        return prices
