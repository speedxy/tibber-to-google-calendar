"""Wrapper für Fahrzeug-Status-Abrufe mit State-Persistenz und Backoff.

Strategie:
- Manual-Provider: jeder Lauf, kein Backoff (nur lokale Config-Werte).
- Tessie-Provider: jeder Lauf, aber via cached Endpoint (Tessies eigener
  Cache schützt das Auto vor Wakeups). Bei API-Fehler exp. Backoff.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests

from tibber_charge.config import Config
from tibber_charge.state import State, VehicleStateCache
from tibber_charge.tibber.data_api import TibberDataClient, token_path
from tibber_charge.vehicle.base import VehicleProvider, VehicleStatus
from tibber_charge.vehicle.manual import ManualVehicleProvider
from tibber_charge.vehicle.tessie import TessieVehicleProvider
from tibber_charge.vehicle.tibber import TibberVehicleProvider


_BACKOFF_INITIAL_SECONDS = 5 * 60
_BACKOFF_MAX_SECONDS = 60 * 60


def fetch_status_with_cache(
    state: State,
    config: Config,
    now: datetime,
    base_dir: str | None = None,
) -> VehicleStatus | None:
    """Holt aktuellen Fahrzeug-Status, mit Backoff bei Fehlern. Returnt None
    nur wenn `vehicle.enabled=false` oder Provider-Bauen fehlschlägt
    UND kein Cache vorhanden ist.

    `base_dir` wird nur für den Tibber-Provider gebraucht (Token-Datei).
    """
    vc = config.vehicle
    cache = state.vehicle

    if not vc.enabled:
        logging.info("📋 Vehicle-Layer deaktiviert (VEHICLE.enabled=false).")
        return _load_cached_status(cache)

    # Backoff?
    if cache.next_retry_after:
        try:
            from dateutil import parser as date_parser
            retry_at = date_parser.isoparse(cache.next_retry_after)
            if now < retry_at:
                logging.info(
                    f"📋 Vehicle-Backoff aktiv bis {cache.next_retry_after} — nutze Cache."
                )
                return _load_cached_status(cache)
        except (ValueError, TypeError):
            pass

    cache.last_fetch_at = now.isoformat()
    try:
        provider = _build_provider(config, base_dir)
    except ValueError as e:
        cache.last_error = f"CONFIG: {e}"
        logging.warning(f"⚠️ Vehicle-Provider nicht baubar ({e}); nutze Cache.")
        return _load_cached_status(cache)

    try:
        status = provider.get_status()
    except requests.RequestException as e:
        cache.last_error = f"NETWORK: {e}"
        _apply_backoff(cache, now)
        logging.warning(
            f"⚠️ Vehicle-Fetch fehlgeschlagen (Netz: {e}); nächster Versuch ab {cache.next_retry_after}"
        )
        return _load_cached_status(cache)
    except (RuntimeError, KeyError, ValueError) as e:
        cache.last_error = f"PARSE: {e}"
        _apply_backoff(cache, now)
        logging.warning(
            f"⚠️ Vehicle-Antwort unbrauchbar ({e}); nächster Versuch ab {cache.next_retry_after}"
        )
        return _load_cached_status(cache)

    cache.last_success_at = now.isoformat()
    cache.last_error = None
    cache.next_retry_after = None
    cache.backoff_seconds = 0
    cache.provider = status.source
    cache.last_status = status.to_dict()
    return status


def _build_provider(config: Config, base_dir: str | None) -> VehicleProvider:
    vc = config.vehicle
    if vc.provider == "manual":
        return ManualVehicleProvider(
            vehicle_name=vc.vehicle_name,
            soc=vc.manual_soc,
            is_connected=vc.manual_connected,
            is_charging=vc.manual_charging,
        )
    if vc.provider == "tessie":
        if not vc.vin:
            raise ValueError("Tessie-Provider braucht VEHICLE.vin in config.json.")
        if not vc.api_token:
            raise ValueError("Tessie-Provider braucht VEHICLE.api_token in config.json.")
        return TessieVehicleProvider(
            vin=vc.vin,
            api_token=vc.api_token,
            vehicle_name=vc.vehicle_name,
            timeout_seconds=vc.timeout_seconds,
        )
    if vc.provider == "tibber":
        if config.tibber_data_api is None:
            raise ValueError("Tibber-Provider braucht den Block TIBBER_DATA_API in config.json.")
        if base_dir is None:
            raise ValueError("Tibber-Provider braucht base_dir für die Token-Datei.")
        client = TibberDataClient(
            config.tibber_data_api, token_path(base_dir), timeout_seconds=vc.timeout_seconds,
        )
        return TibberVehicleProvider(
            client=client,
            vehicle_id=vc.tibber_vehicle_id,
            vehicle_name=vc.vehicle_name,
        )
    raise ValueError(f"unbekannter Vehicle-Provider: {vc.provider!r}")


def _apply_backoff(cache: VehicleStateCache, now: datetime) -> None:
    next_seconds = cache.backoff_seconds * 2 if cache.backoff_seconds > 0 else _BACKOFF_INITIAL_SECONDS
    next_seconds = min(next_seconds, _BACKOFF_MAX_SECONDS)
    cache.backoff_seconds = next_seconds
    cache.next_retry_after = (now + timedelta(seconds=next_seconds)).isoformat()


def _load_cached_status(cache: VehicleStateCache) -> VehicleStatus | None:
    if not cache.last_status:
        return None
    d = cache.last_status
    try:
        return VehicleStatus(
            soc=d.get("soc"),
            is_connected=bool(d.get("is_connected", False)),
            is_charging=bool(d.get("is_charging", False)),
            last_updated_at=d.get("last_updated_at"),
            vehicle_name=d.get("vehicle_name", "Auto"),
            source=d.get("source", "cache"),
        )
    except (TypeError, ValueError):
        return None
