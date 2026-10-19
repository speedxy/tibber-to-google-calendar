"""Provider-Protocol für Fahrzeug-Telemetrie (SoC + Steckverbindung).

Strategy Pattern. V1 hat zwei Implementierungen: Manual (Config-basiert,
Default + Test) und Tessie (echte Tesla-Telemetrie über Drittanbieter).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VehicleStatus:
    soc: int | None              # State of Charge in % (None wenn unbekannt)
    is_connected: bool           # Ladekabel angeschlossen
    is_charging: bool            # Aktuell ladend
    last_updated_at: str | None  # ISO-Timestamp wann der Provider die Daten erhob
    vehicle_name: str            # Anzeigename ("Auto", "Model 3 Limousine", ...)
    source: str                  # "manual" | "tessie"
    raw: dict | None = None      # Provider-spezifische Rohdaten (Debug)

    def to_dict(self) -> dict:
        return {
            "soc": self.soc,
            "is_connected": self.is_connected,
            "is_charging": self.is_charging,
            "last_updated_at": self.last_updated_at,
            "vehicle_name": self.vehicle_name,
            "source": self.source,
        }


class VehicleProvider(Protocol):
    name: str

    def get_status(self) -> VehicleStatus:
        """Holt den aktuellen Fahrzeug-Status.

        Implementierungen MÜSSEN Vampire-Drain vermeiden — bei API-Providern
        heißt das: niemals das Auto wecken, nur gecachte/passive Endpoints
        nutzen. Wirft `requests.RequestException` oder `RuntimeError` bei
        nicht erholbaren Fehlern.
        """
        ...
