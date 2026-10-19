"""Datentypen für die Ladeplanung."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ChargeBlock:
    """Ein zusammenhängender Lade-Block."""
    start: datetime
    end: datetime
    expected_kwh: float
    expected_cost_eur: float
    price_eur_per_kwh: float    # Durchschnittspreis (inkl. Penalty bei Forecast)
    source: str                 # "tibber" | "open_meteo" | "corrently"
    confidence: float           # 1.0 für Tibber, <1.0 für Forecast

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "expected_kwh": self.expected_kwh,
            "expected_cost_eur": self.expected_cost_eur,
            "price_eur_per_kwh": self.price_eur_per_kwh,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ChargePlan:
    """Ergebnis der Planung für ein einzelnes Ladeziel."""
    target_event_id: str
    target_soc: int
    deadline: datetime
    current_soc: int
    capacity_kwh: float
    net_kw: float
    kwh_needed: float
    blocks: tuple[ChargeBlock, ...]
    feasible: bool
    plan_hash: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_cost_eur(self) -> float:
        return sum(b.expected_cost_eur for b in self.blocks)

    @property
    def cycles(self) -> int:
        """Anzahl Start/Stopp-Zyklen — Block-Anzahl."""
        return len(self.blocks)

    def to_dict(self) -> dict:
        return {
            "target_event_id": self.target_event_id,
            "target_soc": self.target_soc,
            "deadline": self.deadline.isoformat(),
            "current_soc": self.current_soc,
            "capacity_kwh": self.capacity_kwh,
            "net_kw": self.net_kw,
            "kwh_needed": self.kwh_needed,
            "blocks": [b.to_dict() for b in self.blocks],
            "feasible": self.feasible,
            "plan_hash": self.plan_hash,
            "warnings": list(self.warnings),
        }
