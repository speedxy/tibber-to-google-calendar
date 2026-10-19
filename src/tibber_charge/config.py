from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ForecastConfig:
    enabled: bool = True
    # Steuert nur die Calendar-Events (#Vorhersage). Bei `False` werden
    # Vorhersage-Daten weiterhin geholt und für die Ladeplanung genutzt,
    # aber kein Kalender-Event geschrieben. Bestehende #Vorhersage-Events
    # werden beim nächsten Lauf aufgeräumt.
    calendar_events_enabled: bool = True
    # "epex_predictor" (Standard, Börsenpreis-Prognose) | "open_meteo" (Wetter-Heuristik) | "corrently"
    provider: str = "epex_predictor"
    # Springt ein, wenn der Haupt-Provider nicht antwortet. None = kein Rückfall.
    fallback_provider: str | None = "open_meteo"
    # Nur für Provider "epex_predictor": Marktgebiet und API-Adresse (für Selbst-Hosting).
    region: str = "DE"
    epex_predictor_url: str = "https://epexpredictor.batzill.com/prices_short"
    # Koordinaten für Open-Meteo. Default: Lübeck.
    # Eigene Koordinaten finden z.B. via https://www.openstreetmap.org/ (Rechtsklick → "Show address").
    latitude: float = 53.87
    longitude: float = 10.69
    # Nur für Provider "corrently": PLZ
    zip_code: str = "23552"
    horizon_days: int = 7
    timeout_seconds: float = 10.0
    # Nur für Provider "corrently": Token (https://console.corrently.io/).
    # Hinweis: kostenloser Token hat tier="anonymous" und liefert leere
    # Forecasts — Corrently ist faktisch nur mit Paid-Tier nutzbar.
    api_token: str | None = None


@dataclass(frozen=True)
class VehicleConfig:
    enabled: bool = True
    provider: str = "manual"   # "manual" | "tessie" | "tibber"
    vehicle_name: str = "Auto"
    timeout_seconds: float = 10.0
    # Akkukapazität für Ladeplanung. Defaults: Model 3 RWD 2023 mit LFP-Pack.
    battery_capacity_kwh: float = 60.0
    # Tessie-spezifisch:
    vin: str | None = None
    api_token: str | None = None
    # Tibber-spezifisch: externalId (oder Tibber-id) aus `scripts/tibber_data_auth.py --probe`.
    # Nur nötig, wenn mehrere Fahrzeuge im Tibber-Konto sind.
    tibber_vehicle_id: str | None = None
    # Manual-spezifisch (Fallback, falls kein Provider verbunden):
    manual_soc: int = 50
    manual_connected: bool = False
    manual_charging: bool = False


@dataclass(frozen=True)
class WallboxConfig:
    """Spezifikationen der Wallbox-Hardware."""
    gross_kw: float = 11.0   # Marketing-Wert (z.B. KEBA P30 11 kW)
    net_kw: float = 10.0     # nach Ladeverlusten — was tatsächlich im Akku landet


@dataclass(frozen=True)
class ChargingConfig:
    """Verhaltens-Tuning des Planers."""
    buffer_factor: float = 1.10        # +10 % Reserve, gleicht Modell-Ungenauigkeit aus
    # Slot-Granularität in Minuten. Tibber/Open-Meteo liefern stündlich, der
    # Planer expandiert intern zu 15-min-Sub-Slots (Preis pro Stunde geerbt),
    # um Schaltfenster passgenauer schneiden zu können.
    slot_minutes: int = 15
    max_cycles_warning: int = 3        # Schwellwert für Warnung bei Fragmentierung
    # Kalender, in denen nach #charge-Ladezielen gesucht wird. Leer = nur
    # GOOGLE_CALENDAR_ID. Lesezugriff reicht; mit Schreibzugriff bekommt der
    # #charge-Termin zusätzlich den Ladeplan in die Beschreibung.
    target_calendar_ids: tuple[str, ...] = ()

    def targets_or(self, default_calendar_id: str) -> tuple[str, ...]:
        return self.target_calendar_ids or (default_calendar_id,)


@dataclass(frozen=True)
class HomeAssistantConfig:
    """HA-REST-API für Wallbox-Schaltung und Heartbeat. Default: deaktiviert."""
    enabled: bool = False
    base_url: str = "http://homeassistant.local:8123"
    api_token: str | None = None
    charging_entity_id: str = "input_boolean.tibber_charging_active"
    # Optionaler Heartbeat — input_datetime-Entity, in die jeder Lauf den aktuellen
    # Timestamp schreibt. Damit kann eine HA-Automation die Wallbox abschalten,
    # wenn der Heartbeat zu alt wird (Software-Failsafe ohne KEBA-Watchdog).
    heartbeat_entity_id: str | None = None
    # Optionaler Stecker-Sensor der Wallbox (binary_sensor, on = Auto eingesteckt).
    # Ersetzt den Steckerstatus des Fahrzeug-Providers, weil Tibber ihn teils
    # stundenlang verzögert meldet. Z.B. binary_sensor.keba_p30_plug.
    plug_entity_id: str | None = None
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class TibberDataApiConfig:
    """OAuth-Client für die Tibber Data API (Fahrzeug-Akkustand u.a.).

    Client anlegen unter https://data-api.tibber.com/clients/manage/.
    `redirect_uri` muss exakt der dort eingetragenen Adresse entsprechen.
    """
    client_id: str
    client_secret: str
    redirect_uri: str = "http://localhost:8765/callback"


@dataclass(frozen=True)
class Config:
    tibber_api_key: str
    google_calendar_id: str
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    vehicle: VehicleConfig = field(default_factory=VehicleConfig)
    wallbox: WallboxConfig = field(default_factory=WallboxConfig)
    charging: ChargingConfig = field(default_factory=ChargingConfig)
    homeassistant: HomeAssistantConfig = field(default_factory=HomeAssistantConfig)
    tibber_data_api: TibberDataApiConfig | None = None


def load_config(base_dir: str) -> Config:
    """Lädt die Konfiguration aus `config.json` im angegebenen Verzeichnis.

    Der `FORECAST`-Block ist optional. Fehlt er, greifen Defaults
    (Corrently-Provider, PLZ 23552, 7-Tage-Horizont, Timeout 10s).
    """
    config_file = os.path.join(base_dir, "config.json")
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"❌ Konfigurationsdatei '{config_file}' nicht gefunden!")

    with open(config_file, "r") as f:
        data = json.load(f)

    forecast_raw = data.get("FORECAST") or {}
    try:
        token_raw = forecast_raw.get("api_token")
        forecast = ForecastConfig(
            enabled=bool(forecast_raw.get("enabled", True)),
            calendar_events_enabled=bool(forecast_raw.get("calendar_events_enabled", True)),
            provider=str(forecast_raw.get("provider", ForecastConfig.provider)),
            fallback_provider=(
                str(forecast_raw["fallback_provider"]) if forecast_raw.get("fallback_provider")
                else (None if "fallback_provider" in forecast_raw else ForecastConfig.fallback_provider)
            ),
            region=str(forecast_raw.get("region", ForecastConfig.region)),
            epex_predictor_url=str(forecast_raw.get("epex_predictor_url", ForecastConfig.epex_predictor_url)),
            latitude=float(forecast_raw.get("latitude", 53.87)),
            longitude=float(forecast_raw.get("longitude", 10.69)),
            zip_code=str(forecast_raw.get("zip_code", "23552")),
            horizon_days=int(forecast_raw.get("horizon_days", 7)),
            timeout_seconds=float(forecast_raw.get("timeout_seconds", 10.0)),
            api_token=str(token_raw) if token_raw else None,
        )
    except (TypeError, ValueError) as e:
        logging.warning(f"⚠️ FORECAST-Block ungültig ({e}). Verwende Defaults.")
        forecast = ForecastConfig()

    vehicle_raw = data.get("VEHICLE") or {}
    try:
        token_raw = vehicle_raw.get("api_token")
        vin_raw = vehicle_raw.get("vin")
        tibber_vehicle_raw = vehicle_raw.get("tibber_vehicle_id")
        vehicle = VehicleConfig(
            enabled=bool(vehicle_raw.get("enabled", True)),
            provider=str(vehicle_raw.get("provider", "manual")),
            vehicle_name=str(vehicle_raw.get("vehicle_name", "Auto")),
            timeout_seconds=float(vehicle_raw.get("timeout_seconds", 10.0)),
            battery_capacity_kwh=float(vehicle_raw.get("battery_capacity_kwh", 60.0)),
            vin=str(vin_raw) if vin_raw else None,
            api_token=str(token_raw) if token_raw else None,
            tibber_vehicle_id=str(tibber_vehicle_raw) if tibber_vehicle_raw else None,
            manual_soc=int(vehicle_raw.get("manual_soc", 50)),
            manual_connected=bool(vehicle_raw.get("manual_connected", False)),
            manual_charging=bool(vehicle_raw.get("manual_charging", False)),
        )
    except (TypeError, ValueError) as e:
        logging.warning(f"⚠️ VEHICLE-Block ungültig ({e}). Verwende Defaults.")
        vehicle = VehicleConfig()

    wallbox_raw = data.get("WALLBOX") or {}
    try:
        wallbox = WallboxConfig(
            gross_kw=float(wallbox_raw.get("gross_kw", 11.0)),
            net_kw=float(wallbox_raw.get("net_kw", 10.0)),
        )
    except (TypeError, ValueError) as e:
        logging.warning(f"⚠️ WALLBOX-Block ungültig ({e}). Verwende Defaults.")
        wallbox = WallboxConfig()

    charging_raw = data.get("CHARGING") or {}
    try:
        charging = ChargingConfig(
            buffer_factor=float(charging_raw.get("buffer_factor", 1.10)),
            slot_minutes=int(charging_raw.get("slot_minutes", ChargingConfig.slot_minutes)),
            max_cycles_warning=int(charging_raw.get("max_cycles_warning", 3)),
            target_calendar_ids=tuple(str(c) for c in (charging_raw.get("target_calendar_ids") or []) if c),
        )
    except (TypeError, ValueError) as e:
        logging.warning(f"⚠️ CHARGING-Block ungültig ({e}). Verwende Defaults.")
        charging = ChargingConfig()

    ha_raw = data.get("HOMEASSISTANT") or {}
    try:
        token_raw = ha_raw.get("api_token")
        hb_raw = ha_raw.get("heartbeat_entity_id")
        homeassistant = HomeAssistantConfig(
            enabled=bool(ha_raw.get("enabled", False)),
            base_url=str(ha_raw.get("base_url", "http://homeassistant.local:8123")),
            api_token=str(token_raw) if token_raw else None,
            charging_entity_id=str(ha_raw.get("charging_entity_id", "input_boolean.tibber_charging_active")),
            heartbeat_entity_id=str(hb_raw) if hb_raw else None,
            plug_entity_id=str(ha_raw["plug_entity_id"]) if ha_raw.get("plug_entity_id") else None,
            timeout_seconds=float(ha_raw.get("timeout_seconds", 5.0)),
        )
    except (TypeError, ValueError) as e:
        logging.warning(f"⚠️ HOMEASSISTANT-Block ungültig ({e}). Verwende Defaults (deaktiviert).")
        homeassistant = HomeAssistantConfig()

    tibber_data_api = _parse_tibber_data_api(data.get("TIBBER_DATA_API") or {})

    return Config(
        tibber_api_key=data["TIBBER_API_KEY"],
        google_calendar_id=data["GOOGLE_CALENDAR_ID"],
        forecast=forecast,
        vehicle=vehicle,
        wallbox=wallbox,
        charging=charging,
        homeassistant=homeassistant,
        tibber_data_api=tibber_data_api,
    )


def _parse_tibber_data_api(raw: dict) -> TibberDataApiConfig | None:
    """Optionaler Block. Ohne client_id/client_secret → None (Feature aus)."""
    client_id = raw.get("client_id")
    client_secret = raw.get("client_secret")
    if not client_id or not client_secret:
        if raw:
            logging.warning("⚠️ TIBBER_DATA_API-Block ohne client_id/client_secret, wird ignoriert.")
        return None
    return TibberDataApiConfig(
        client_id=str(client_id),
        client_secret=str(client_secret),
        redirect_uri=str(raw.get("redirect_uri") or "http://localhost:8765/callback"),
    )
