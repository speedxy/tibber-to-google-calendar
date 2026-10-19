"""Manual-Provider: SoC und Connected-Status kommen aus config.json.

Nützlich für:
- Out-of-the-box-Setup ohne Tessie-Konto
- Tests (deterministisch, ohne Netz)
- Trockenlauf einer Ladeplanung mit hypothetischem SoC
"""
from __future__ import annotations

from datetime import datetime

from tibber_charge.vehicle.base import VehicleStatus


class ManualVehicleProvider:
    name = "manual"

    def __init__(
        self,
        vehicle_name: str,
        soc: int,
        is_connected: bool,
        is_charging: bool = False,
    ) -> None:
        self.vehicle_name = vehicle_name
        self.soc = soc
        self.is_connected = is_connected
        self.is_charging = is_charging

    def get_status(self) -> VehicleStatus:
        return VehicleStatus(
            soc=max(0, min(100, int(self.soc))),
            is_connected=bool(self.is_connected),
            is_charging=bool(self.is_charging),
            last_updated_at=datetime.now().astimezone().isoformat(),
            vehicle_name=self.vehicle_name,
            source=self.name,
            raw=None,
        )
