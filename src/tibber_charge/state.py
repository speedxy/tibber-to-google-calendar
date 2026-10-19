"""Lokaler State (Cache, Backoff, später Plan/SoC).

Schreibt atomar via tmp+os.replace und schützt parallele Läufe per fcntl.flock.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import IO, Iterator


SCHEMA_VERSION = 1


@dataclass
class TibberCache:
    # Yesterday-Cache: hält die Preise des vorigen Tages, damit Preisperioden,
    # die über Mitternacht spannen (z.B. CHEAP 22:00 bis 03:00 nächster Tag),
    # auch nach Tagewechsel als ein zusammenhängender Termin im Kalender
    # bestehen bleiben. Tibbers API liefert nur today+tomorrow, also rotieren
    # wir den alten today bei _store_prices selbst nach yesterday.
    yesterday_date: str | None = None
    yesterday_prices: list[dict] = field(default_factory=list)
    today_date: str | None = None
    today_prices: list[dict] = field(default_factory=list)
    tomorrow_date: str | None = None
    tomorrow_prices: list[dict] = field(default_factory=list)
    last_fetch_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    next_retry_after: str | None = None
    backoff_seconds: int = 0
    tomorrow_given_up_for_date: str | None = None


@dataclass
class ForecastCache:
    provider: str | None = None
    forecasts: list[dict] = field(default_factory=list)
    last_fetch_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    next_retry_after: str | None = None
    backoff_seconds: int = 0
    # FIFO-Puffer für späteres Backtesting der Heuristik (max 200 Einträge)
    calibration_samples: list[dict] = field(default_factory=list)
    # Zuletzt gelernte Umrechnung Prognose → Tibber-Preis (PriceMapping.to_dict()).
    price_mapping: dict | None = None


@dataclass
class VehicleStateCache:
    provider: str | None = None
    last_status: dict | None = None     # VehicleStatus.to_dict() oder None
    last_fetch_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    next_retry_after: str | None = None
    backoff_seconds: int = 0


@dataclass
class PlansCache:
    """Persistierte Ladepläne. Key = source_event_id des #charge-Events."""
    by_target: dict[str, dict] = field(default_factory=dict)


@dataclass
class HardwareState:
    """Letzter bekannter Hardware-Steuerzustand. Vermeidet redundante HA-Calls."""
    last_charging_state: str | None = None       # "on" | "off" | None
    last_charging_set_at: str | None = None      # ISO
    last_heartbeat_at: str | None = None         # ISO


@dataclass
class CalendarEventsCache:
    """Hash + Event-IDs für eine Calendar-Event-Klasse (z.B. #Tibber, #Vorhersage).

    Bei unverändertem Hash überspringt der Sync den kompletten Delete+Create-Zyklus
    und macht null Calendar-API-Calls.

    `confirmed_empty` wird True, sobald ein erzwungener Cleanup (Feature
    deaktiviert) 0 Events vorfand — verhindert, dass danach jeder Cron-Tick
    erneut einen unnötigen List-Call absetzt.
    """
    events_hash: str | None = None
    event_ids: list[str] = field(default_factory=list)
    confirmed_empty: bool = False


@dataclass
class State:
    schema_version: int = SCHEMA_VERSION
    tibber: TibberCache = field(default_factory=TibberCache)
    forecast: ForecastCache = field(default_factory=ForecastCache)
    vehicle: VehicleStateCache = field(default_factory=VehicleStateCache)
    plans: PlansCache = field(default_factory=PlansCache)
    hardware: HardwareState = field(default_factory=HardwareState)
    tibber_events: CalendarEventsCache = field(default_factory=CalendarEventsCache)
    forecast_events: CalendarEventsCache = field(default_factory=CalendarEventsCache)


def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, "state.json")


def _lock_path(base_dir: str) -> str:
    return os.path.join(base_dir, "state.lock")


def load_state(base_dir: str) -> State:
    """Lädt den State. Toleriert fehlende, kaputte und falsch-versionierte Dateien.

    Liefert in solchen Fällen einen frischen leeren State und loggt eine Warnung.
    """
    path = _state_path(base_dir)
    if not os.path.exists(path):
        logging.info("ℹ️ Keine state.json gefunden — starte mit leerem State.")
        return State()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logging.warning(f"⚠️ State-Datei konnte nicht gelesen werden ({e}). Starte mit leerem State.")
        return State()

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        logging.warning(
            f"⚠️ State-Schema-Version {version!r} != erwartet {SCHEMA_VERSION}. Starte mit leerem State."
        )
        return State()

    tibber_raw = data.get("tibber", {}) or {}
    try:
        tibber = TibberCache(**tibber_raw)
    except TypeError as e:
        logging.warning(f"⚠️ TibberCache-Felder ungültig ({e}). Starte mit leerem State.")
        return State()

    forecast_raw = data.get("forecast", {}) or {}
    try:
        forecast = ForecastCache(**forecast_raw)
    except TypeError as e:
        logging.warning(f"⚠️ ForecastCache-Felder ungültig ({e}). Verwende leeren Forecast-Cache.")
        forecast = ForecastCache()

    vehicle_raw = data.get("vehicle", {}) or {}
    try:
        vehicle = VehicleStateCache(**vehicle_raw)
    except TypeError as e:
        logging.warning(f"⚠️ VehicleStateCache-Felder ungültig ({e}). Verwende leeren Vehicle-Cache.")
        vehicle = VehicleStateCache()

    plans_raw = data.get("plans", {}) or {}
    try:
        plans = PlansCache(**plans_raw)
    except TypeError as e:
        logging.warning(f"⚠️ PlansCache-Felder ungültig ({e}). Verwende leeren Plans-Cache.")
        plans = PlansCache()

    hardware_raw = data.get("hardware", {}) or {}
    try:
        hardware = HardwareState(**hardware_raw)
    except TypeError as e:
        logging.warning(f"⚠️ HardwareState-Felder ungültig ({e}). Verwende leeren Hardware-State.")
        hardware = HardwareState()

    tibber_events_raw = data.get("tibber_events", {}) or {}
    try:
        tibber_events = CalendarEventsCache(**tibber_events_raw)
    except TypeError as e:
        logging.warning(f"⚠️ CalendarEventsCache (tibber_events) ungültig ({e}).")
        tibber_events = CalendarEventsCache()

    forecast_events_raw = data.get("forecast_events", {}) or {}
    try:
        forecast_events = CalendarEventsCache(**forecast_events_raw)
    except TypeError as e:
        logging.warning(f"⚠️ CalendarEventsCache (forecast_events) ungültig ({e}).")
        forecast_events = CalendarEventsCache()

    return State(
        schema_version=version, tibber=tibber, forecast=forecast,
        vehicle=vehicle, plans=plans, hardware=hardware,
        tibber_events=tibber_events, forecast_events=forecast_events,
    )


def save_state(base_dir: str, state: State) -> None:
    """Atomarer Write: erst in tmp-Datei, dann os.replace auf das Ziel.

    fsync stellt sicher, dass die Daten vor dem Rename auf der Platte sind.
    """
    path = _state_path(base_dir)
    tmp_path = f"{path}.tmp.{os.getpid()}"

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Tmp-Datei aufräumen, Original bleibt intakt
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@contextmanager
def state_lock(base_dir: str) -> Iterator[None]:
    """Exklusiver File-Lock für die Dauer des Blocks.

    Wirft BlockingIOError sofort, wenn ein anderer Lauf den Lock hält.
    POSIX-only (fcntl). Auf macOS/Linux OK, Windows nicht unterstützt.
    """
    lock_file_path = _lock_path(base_dir)
    fh: IO[str] = open(lock_file_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
