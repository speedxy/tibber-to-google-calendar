"""Strategy Pattern für Ladekurven-Algorithmen.

V1 hat nur LinearStrategy. Spätere Strategien können nicht-lineare Ladekurven
(LFP-Plateau über 80 %, Phasen-Modulation, PV-Überschuss) abbilden, ohne den
Aufrufer zu ändern.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from tibber_charge.gcal.charge_targets import ChargeTarget
from tibber_charge.planning.types import ChargePlan
from tibber_charge.prices.types import UnifiedPrice


class ChargingStrategy(Protocol):
    name: str

    def plan(
        self,
        target: ChargeTarget,
        current_soc: int,
        capacity_kwh: float,
        net_kw: float,
        prices: list[UnifiedPrice],
        now: datetime,
    ) -> ChargePlan:
        """Berechnet einen ChargePlan für ein einzelnes Ladeziel."""
        ...
