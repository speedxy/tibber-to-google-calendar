"""Provider-Protocol für Preis-Prognosen.

Strategy Pattern, aber V1 ist nur eine Implementierung (Corrently GSI). Wenn
ein zweiter Provider dazu kommt, extrahiert sich das natürlich raus.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from tibber_charge.prices.types import UnifiedPrice


class PriceForecastProvider(Protocol):
    name: str

    def fetch(self, now: datetime) -> list[UnifiedPrice]:
        """Liefert stündliche Forecast-Preise ab `now` für den Provider-Horizont.

        Wirft `requests.RequestException` (Netz/HTTP), `RuntimeError` (Parse-/
        Logik-Fehler) oder `ValueError` (ungültige Antwort-Struktur) bei Problemen.
        Der Caller fängt diese ab und fällt auf Cache zurück.
        """
        ...
