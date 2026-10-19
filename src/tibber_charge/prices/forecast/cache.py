"""Smart-Polling für Forecast: 2× pro Tag holen, Backoff bei Fehler,
sonst Cache aus state.json zurückliefern.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests
from dateutil import parser as date_parser

from tibber_charge.config import Config, ForecastConfig
from tibber_charge.prices.forecast.base import PriceForecastProvider
from tibber_charge.prices.forecast.corrently import CorrentlyForecastProvider
from tibber_charge.prices.forecast.epex_predictor import EpexPredictorForecastProvider
from tibber_charge.prices.forecast.open_meteo import OpenMeteoForecastProvider
from tibber_charge.prices.types import UnifiedPrice
from tibber_charge.state import ForecastCache, State


# Forecasts werden 2× pro Tag aktualisiert: morgens (vor Tagesplanung) und
# nachmittags nach der EPEX-Day-Ahead-Auktion.
_REFRESH_HOURS: tuple[int, ...] = (6, 14)
_BACKOFF_INITIAL_SECONDS = 30 * 60
_BACKOFF_MAX_SECONDS = 4 * 60 * 60


def should_fetch_forecast(state: State, now: datetime) -> tuple[bool, str]:
    """Liefert (do_fetch, Grund)."""
    cache = state.forecast

    # Backoff hat Vorrang
    if cache.next_retry_after:
        try:
            retry_at = date_parser.isoparse(cache.next_retry_after)
            if now < retry_at:
                return False, f"backoff_bis_{cache.next_retry_after}"
        except (ValueError, TypeError):
            pass

    if not cache.last_success_at or not cache.forecasts:
        return True, "forecast_leer"

    try:
        last = date_parser.isoparse(cache.last_success_at)
    except (ValueError, TypeError):
        return True, "last_success_at_unparsbar"

    # Wenn eine Refresh-Stunde zwischen `last` und `now` lag → neu holen
    next_refresh = _next_refresh_after(last)
    if now >= next_refresh:
        return True, f"refresh_faellig_ab_{next_refresh.isoformat()}"

    return False, f"naechster_refresh_{next_refresh.isoformat()}"


def fetch_forecast_with_cache(
    state: State,
    config: Config,
    now: datetime,
) -> tuple[list[UnifiedPrice], bool]:
    """Holt den Forecast wenn fällig, sonst aus Cache. Liefert (Preise, fresh_fetch).

    `fresh_fetch=True` bedeutet: in diesem Lauf wurde ein neuer Forecast geladen.
    PR 3 nutzt diesen Boolean, um nur dann `#Vorhersage`-Events neu zu schreiben.
    """
    fc = config.forecast
    cache = state.forecast

    if not fc.enabled:
        logging.info("📋 Forecast deaktiviert (FORECAST.enabled=false) — keine Daten.")
        return [], False

    do_fetch, reason = should_fetch_forecast(state, now)
    logging.info(f"📋 Forecast-Polling: fetch={do_fetch} ({reason})")
    if not do_fetch:
        return _load_cached(cache), False

    cache.last_fetch_at = now.isoformat()
    try:
        provider = _build_provider(fc)
    except ValueError as e:
        cache.last_error = f"CONFIG: {e}"
        logging.warning(f"⚠️ Forecast-Provider unbekannt ({e}); fallback auf Cache.")
        return _load_cached(cache), False

    try:
        provider, fresh = _fetch_with_fallback(fc, provider, now)
    except requests.RequestException as e:
        cache.last_error = f"NETWORK: {e}"
        _apply_backoff(cache, now)
        logging.warning(
            f"⚠️ Forecast-Fetch fehlgeschlagen (Netz: {e}); nächster Versuch ab {cache.next_retry_after}"
        )
        return _load_cached(cache), False
    except (ValueError, RuntimeError, KeyError) as e:
        cache.last_error = f"PARSE: {e}"
        _apply_backoff(cache, now)
        logging.warning(
            f"⚠️ Forecast-Antwort unbrauchbar ({e}); nächster Versuch ab {cache.next_retry_after}"
        )
        return _load_cached(cache), False

    cache.last_success_at = now.isoformat()
    cache.last_error = None
    cache.next_retry_after = None
    cache.backoff_seconds = 0
    cache.provider = provider.name
    # raw_total mit serialisieren — wird in PR 4 für die Kalibrierung gebraucht.
    cache.forecasts = [{**p.to_dict(), "raw_total": p.raw_total} for p in fresh]
    logging.info(f"✅ Forecast-Cache aktualisiert: {len(fresh)} Stunden ({provider.name})")
    return fresh, True


def _fetch_with_fallback(
    fc: ForecastConfig,
    provider: PriceForecastProvider,
    now: datetime,
) -> tuple[PriceForecastProvider, list[UnifiedPrice]]:
    """Holt beim Haupt-Provider, bei Fehler einmal beim Rückfall-Provider.

    Wirft den Fehler des Rückfalls weiter (bzw. des Haupt-Providers, wenn kein
    Rückfall konfiguriert ist), damit der Caller Backoff + Cache greifen lässt.
    """
    try:
        return provider, provider.fetch(now)
    except (requests.RequestException, ValueError, RuntimeError, KeyError) as e:
        if not fc.fallback_provider or fc.fallback_provider == provider.name:
            raise
        logging.warning(
            f"⚠️ Forecast-Provider {provider.name} fehlgeschlagen ({e}); Rückfall auf {fc.fallback_provider}."
        )
        fallback = _build_provider(fc, fc.fallback_provider)
        return fallback, fallback.fetch(now)


def _build_provider(fc: ForecastConfig, name: str | None = None) -> PriceForecastProvider:
    name = name or fc.provider
    if name == "epex_predictor":
        return EpexPredictorForecastProvider(
            region=fc.region,
            base_url=fc.epex_predictor_url,
            timeout_seconds=fc.timeout_seconds,
            horizon_days=fc.horizon_days,
        )
    if name == "open_meteo":
        return OpenMeteoForecastProvider(
            latitude=fc.latitude,
            longitude=fc.longitude,
            timeout_seconds=fc.timeout_seconds,
            forecast_days=fc.horizon_days,
        )
    if name == "corrently":
        return CorrentlyForecastProvider(
            zip_code=fc.zip_code,
            timeout_seconds=fc.timeout_seconds,
            api_token=fc.api_token,
        )
    raise ValueError(f"unbekannter Forecast-Provider: {name!r}")


def _apply_backoff(cache: ForecastCache, now: datetime) -> None:
    next_seconds = cache.backoff_seconds * 2 if cache.backoff_seconds > 0 else _BACKOFF_INITIAL_SECONDS
    next_seconds = min(next_seconds, _BACKOFF_MAX_SECONDS)
    cache.backoff_seconds = next_seconds
    cache.next_retry_after = (now + timedelta(seconds=next_seconds)).isoformat()


def _next_refresh_after(ts: datetime) -> datetime:
    """Liefert den nächsten Refresh-Zeitpunkt nach `ts`.

    Refresh-Stunden sind in lokaler Zeit (CET/CEST) zu interpretieren.
    `ts` wird vor dem Vergleich in die lokale Zone gewandelt.
    """
    local = ts.astimezone()
    for h in _REFRESH_HOURS:
        candidate = local.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > local:
            return candidate
    # Alle Refresh-Stunden für heute waren schon — nimm erste Stunde morgen
    tomorrow = local + timedelta(days=1)
    return tomorrow.replace(hour=_REFRESH_HOURS[0], minute=0, second=0, microsecond=0)


def _load_cached(cache: ForecastCache) -> list[UnifiedPrice]:
    """Rekonstruiert UnifiedPrice-Liste aus dem gespeicherten Cache."""
    result: list[UnifiedPrice] = []
    for d in cache.forecasts:
        try:
            result.append(
                UnifiedPrice(
                    startsAt=d["startsAt"],
                    total=float(d["total"]),
                    level=d.get("level", "NORMAL"),
                    source=d.get("source", "corrently"),
                    confidence=float(d.get("confidence", 0.4)),
                    raw_total=float(d["raw_total"]) if d.get("raw_total") is not None else None,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return result
