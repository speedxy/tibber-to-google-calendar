"""Provider-agnostischer Preis-Typ.

Bricht die Kopplung an Tibbers Dict-Format auf, ohne `group_price_periods` zu
brechen — `to_dict()` liefert weiterhin `{startsAt, total, level}`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnifiedPrice:
    startsAt: str        # ISO-8601 mit Zonen-Offset (Tibber-kompatibel)
    total: float         # €/kWh — bei Forecast inkl. Penalty
    level: str           # NEGATIVE | VERY_CHEAP | CHEAP | NORMAL | EXPENSIVE | VERY_EXPENSIVE
    source: str          # "tibber" | "corrently" | ...
    confidence: float    # 1.0 für gemessene/day-ahead, <1.0 für Forecast
    raw_total: float | None = None  # ungestraft (Debug/Calendar-Description)
    # Provider-spezifische Rohfeatures (z.B. wind_kmh, solar_wm2, temperature_c).
    # Transient — wird nicht via to_dict serialisiert. Nur zum Anlegen von
    # Kalibrierungs-Samples mit echten Eingabe-Features genutzt.
    meta: dict | None = None

    def to_dict(self) -> dict:
        """Liefert ein Tibber-kompatibles Dict — abwärtskompatibel zu group_price_periods()."""
        return {
            "startsAt": self.startsAt,
            "total": self.total,
            "level": self.level,
            "source": self.source,
            "confidence": self.confidence,
        }

    @classmethod
    def from_tibber_dict(cls, price: dict) -> "UnifiedPrice":
        """Konvertiert ein Tibber-Antwort-Dict (startsAt/total/level) in UnifiedPrice."""
        return cls(
            startsAt=price["startsAt"],
            total=float(price["total"]),
            level=price["level"],
            source="tibber",
            confidence=1.0,
        )
