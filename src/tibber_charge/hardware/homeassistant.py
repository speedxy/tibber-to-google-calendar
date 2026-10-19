"""Home-Assistant REST-API-Client.

Wird genutzt um:
- `input_boolean` zum An/Aus-Schalten der Wallbox zu steuern
- Einen Heartbeat in eine `input_datetime` zu schreiben (Software-Failsafe-Anker —
  HA-Automation kann ausschalten, wenn der Heartbeat zu alt wird).

Methoden returnen `True` bei Erfolg, `False` bei Fehler (Netz, HTTP, Timeout).
Keine Exceptions werden weitergereicht — die Steuerung darf nicht das ganze
Skript zum Crashen bringen.
"""
from __future__ import annotations

import logging
from datetime import datetime

import requests


class HomeAssistantClient:
    def __init__(self, base_url: str, api_token: str, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def get_state(self, entity_id: str) -> str | None:
        """Liest den aktuellen Zustand einer Entity. None bei Fehler."""
        url = f"{self.base_url}/api/states/{entity_id}"
        try:
            response = requests.get(url, headers=self._headers(), timeout=self.timeout_seconds)
            response.raise_for_status()
            return response.json().get("state")
        except (requests.RequestException, ValueError) as e:
            logging.warning(f"⚠️ HA-Lesefehler ({entity_id}): {e}")
            return None

    def set_input_boolean(self, entity_id: str, on: bool) -> bool:
        """Setzt input_boolean per Service-Call (idempotent in HA)."""
        service = "turn_on" if on else "turn_off"
        url = f"{self.base_url}/api/services/input_boolean/{service}"
        try:
            response = requests.post(
                url,
                json={"entity_id": entity_id},
                headers=self._headers(),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            logging.error(f"❌ HA-Schaltfehler ({entity_id} → {service}): {e}")
            return False
        return True

    def write_input_datetime(self, entity_id: str, ts: datetime) -> bool:
        """Schreibt ISO-Timestamp in eine input_datetime-Entity (Heartbeat-Anker)."""
        url = f"{self.base_url}/api/services/input_datetime/set_datetime"
        try:
            response = requests.post(
                url,
                json={"entity_id": entity_id, "datetime": ts.isoformat()},
                headers=self._headers(),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            logging.error(f"❌ HA-Heartbeat-Fehler ({entity_id}): {e}")
            return False
        return True
