"""Tessie als Vehicle-Telemetrie-Provider.

API: https://api.tessie.com/{vin}/state?use_cache=true mit Bearer-Token.
Wir nutzen IMMER `use_cache=true` — das liest Tessies internen Cache
ohne das Auto zu wecken (Vampire-Drain-Schutz).

Doku: https://developer.tessie.com/
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from tibber_charge.vehicle.base import VehicleStatus


TESSIE_BASE_URL = "https://api.tessie.com"


class TessieVehicleProvider:
    name = "tessie"

    def __init__(
        self,
        vin: str,
        api_token: str,
        vehicle_name: str = "Auto",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not vin:
            raise ValueError("Tessie: VIN fehlt in der Konfiguration.")
        if not api_token:
            raise ValueError("Tessie: api_token fehlt in der Konfiguration.")
        self.vin = vin
        self.api_token = api_token
        self.vehicle_name = vehicle_name
        self.timeout_seconds = timeout_seconds

    def get_status(self) -> VehicleStatus:
        url = f"{TESSIE_BASE_URL}/{self.vin}/state"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }
        logging.info(f"📡 Hole Tessie-Status (VIN ...{self.vin[-4:] if len(self.vin) >= 4 else self.vin}, cached)...")
        response = requests.get(
            url,
            headers=headers,
            params={"use_cache": "true"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()

        return _parse_state(data, self.vehicle_name, self.name)


def _parse_state(data: dict, vehicle_name: str, source: str) -> VehicleStatus:
    """Extrahiert SoC + Connection-Status defensiv aus dem Tessie-State-Payload."""
    charge_state = data.get("charge_state") or {}
    vehicle_state = data.get("vehicle_state") or {}

    soc = charge_state.get("battery_level")
    if soc is not None:
        try:
            soc = int(soc)
        except (TypeError, ValueError):
            soc = None

    # Tesla liefert "Charging", "Stopped", "Disconnected", "Complete", "NoPower" usw.
    charging_state = (charge_state.get("charging_state") or "").strip()
    is_charging = charging_state == "Charging"

    # Stecker erkannt: Tesla-Field conn_charge_cable ist non-leer ("IEC", "SAE", ...) wenn angeschlossen.
    # Alternativ: charge_port_door_open + charging_state != "Disconnected" + charge_port_latch
    cable = (charge_state.get("conn_charge_cable") or "").strip()
    is_connected = bool(cable) and charging_state != "Disconnected"
    # Fallback: wenn cable-Feld fehlt aber charging_state aussagekräftig ist
    if not cable and charging_state in ("Charging", "Stopped", "Complete"):
        is_connected = True

    timestamp_ms = vehicle_state.get("timestamp")
    last_updated_at: str | None = None
    if timestamp_ms:
        try:
            ts = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)
            last_updated_at = ts.astimezone().isoformat()
        except (TypeError, ValueError):
            last_updated_at = None

    if soc is None and not is_connected:
        # Defensive Diagnose: leerer Payload sollte nicht still passieren
        raise RuntimeError(
            "Tessie lieferte unvollständigen Status (kein SoC, keine Connection). "
            "Möglicherweise ist das Auto seit langer Zeit offline oder VIN/Token falsch."
        )

    logging.info(
        f"✅ Tessie: SoC={soc}%, connected={is_connected}, charging={is_charging}, "
        f"last_updated={last_updated_at}"
    )
    return VehicleStatus(
        soc=soc,
        is_connected=is_connected,
        is_charging=is_charging,
        last_updated_at=last_updated_at,
        vehicle_name=vehicle_name,
        source=source,
        raw=None,  # Roh-Payload bewusst nicht persistieren (groß, sensibel)
    )
