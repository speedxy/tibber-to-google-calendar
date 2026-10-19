"""Tibber Data API als Vehicle-Telemetrie-Provider.

Liest Fahrzeuge, die im Tibber-Konto verbunden sind (Tibber-App →
Geräte). Tibber fragt den Hersteller selbst ab, wir lesen nur Tibbers
gespeicherten Stand: kein Wake-Up, kein Vampire-Drain, kein Abo.

Auswahl des Fahrzeugs über `VEHICLE.tibber_vehicle_id` = `externalId`
(bei Tesla: Teslas interne Fahrzeug-ID, NICHT die VIN) oder Tibbers `id`.
Welche IDs es gibt, zeigt `python scripts/tibber_data_auth.py --probe`.
"""
from __future__ import annotations

import base64
import logging

from tibber_charge.tibber.data_api import TibberDataClient
from tibber_charge.vehicle.base import VehicleStatus


class TibberVehicleProvider:
    name = "tibber"

    def __init__(
        self,
        client: TibberDataClient,
        vehicle_id: str | None,
        vehicle_name: str = "Auto",
    ) -> None:
        self.client = client
        self.vehicle_id = vehicle_id
        self.vehicle_name = vehicle_name

    def get_status(self) -> VehicleStatus:
        logging.info("📡 Hole Fahrzeug-Status über die Tibber Data API...")
        home_id = self._first_home_id()
        devices = _extract_list(self.client.get(f"/v1/homes/{home_id}/devices"), "devices")
        vehicles = [d for d in devices if _is_vehicle(d)]
        device = self._select(vehicles)

        # Die Liste liefert keine Werte, die stehen nur im Detail-Endpunkt.
        details = self.client.get(f"/v1/homes/{home_id}/devices/{device['id']}")
        return parse_device(details, self.vehicle_name, self.name)

    def _first_home_id(self) -> str:
        homes = _extract_list(self.client.get("/v1/homes"), "homes")
        for home in homes:
            if home.get("id"):
                return home["id"]
        raise RuntimeError("Tibber Data API liefert kein Home für dieses Konto.")

    def _select(self, vehicles: list[dict]) -> dict:
        if not vehicles:
            raise RuntimeError(
                "Tibber Data API liefert kein Fahrzeug. Ist das Auto in der Tibber-App "
                "verbunden und der Scope data-api-vehicles-read gesetzt?"
            )
        if self.vehicle_id:
            for v in vehicles:
                if self.vehicle_id in (v.get("id"), v.get("externalId")):
                    return v
            raise RuntimeError(
                f"Fahrzeug {self.vehicle_id!r} nicht gefunden. Verfügbar: {_describe(vehicles)}"
            )
        if len(vehicles) > 1:
            raise RuntimeError(
                "Mehrere Fahrzeuge im Tibber-Konto, bitte VEHICLE.tibber_vehicle_id setzen. "
                f"Verfügbar: {_describe(vehicles)}"
            )
        return vehicles[0]


def parse_device(device: dict, vehicle_name: str, source: str) -> VehicleStatus:
    """Extrahiert SoC + Stecker-/Ladestatus aus einem Data-API-Gerät."""
    caps = {
        c.get("id"): c.get("value")
        for c in device.get("capabilities") or []
        if isinstance(c, dict)
    }

    soc_raw = caps.get("storage.stateOfCharge")
    try:
        soc = int(round(float(soc_raw))) if soc_raw is not None else None
    except (TypeError, ValueError):
        soc = None
    if soc is None:
        raise RuntimeError("Tibber Data API liefert keinen Akkustand (storage.stateOfCharge).")

    connector = str(caps.get("connector.status") or "unknown")
    charging = str(caps.get("charging.status") or "unknown")
    # "unknown" zählt als nicht eingesteckt: lieber nicht planen als ins Leere schalten.
    is_connected = connector == "connected" or charging == "charging"
    is_charging = charging == "charging"
    last_seen = (device.get("status") or {}).get("lastSeen")

    logging.info(
        f"✅ Tibber: SoC={soc}%, Stecker={connector}, Laden={charging}, zuletzt gesehen={last_seen}"
    )
    return VehicleStatus(
        soc=soc,
        is_connected=is_connected,
        is_charging=is_charging,
        last_updated_at=last_seen,
        vehicle_name=vehicle_name,
        source=source,
        raw=None,
    )


def _is_vehicle(device: dict) -> bool:
    """Die Geräteliste liefert keine Capabilities. Die Data-API-ID ist aber
    base64 von z.B. "tesla vehicle:<uuid>", der Typ steckt also in der ID.
    Nicht dekodierbare IDs gelten im Zweifel als Fahrzeug (die Auswahl über
    tibber_vehicle_id grenzt dann ein).
    """
    device_id = str(device.get("id") or "")
    try:
        decoded = base64.b64decode(device_id + "=" * (-len(device_id) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return True
    return "vehicle" in decoded.lower()


def _describe(vehicles: list[dict]) -> str:
    parts = []
    for v in vehicles:
        info = v.get("info") or {}
        label = " ".join(x for x in (info.get("brand"), info.get("name")) if x) or "?"
        parts.append(f"{label} (tibber_vehicle_id={v.get('externalId') or v.get('id')})")
    return "; ".join(parts)


def _extract_list(payload, key: str) -> list[dict]:
    """Die Antwort ist je nach Endpunkt eine Liste oder ein Objekt mit Liste."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for k in (key, "items", "data"):
            value = payload.get(k)
            if isinstance(value, list):
                return [p for p in value if isinstance(p, dict)]
    return []
