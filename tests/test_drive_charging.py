"""Tests für _drive_charging — Schalt-Logik + Idempotenz + Heartbeat."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tibber_charge.config import (
    ChargingConfig,
    Config,
    ForecastConfig,
    HomeAssistantConfig,
    VehicleConfig,
    WallboxConfig,
)
from tibber_charge.main import _drive_charging
from tibber_charge.planning.types import ChargeBlock, ChargePlan
from tibber_charge.state import State


def _now() -> datetime:
    return datetime(2026, 5, 21, 22, 0, tzinfo=timezone.utc)


def _config(enabled: bool = True, heartbeat: str | None = None) -> Config:
    return Config(
        tibber_api_key="x", google_calendar_id="y",
        forecast=ForecastConfig(enabled=False),
        vehicle=VehicleConfig(),
        wallbox=WallboxConfig(),
        charging=ChargingConfig(),
        homeassistant=HomeAssistantConfig(
            enabled=enabled,
            api_token="tok",
            charging_entity_id="input_boolean.wb",
            heartbeat_entity_id=heartbeat,
        ),
    )


def _plan(blocks: tuple[ChargeBlock, ...], deadline_offset: int = 10) -> ChargePlan:
    return ChargePlan(
        target_event_id="evt1", target_soc=80,
        deadline=_now() + timedelta(hours=deadline_offset),
        current_soc=10, capacity_kwh=60.0, net_kw=10.0,
        kwh_needed=42.0, blocks=blocks, feasible=True,
        plan_hash="h", warnings=(),
    )


def _block(start_offset: int, end_offset: int) -> ChargeBlock:
    return ChargeBlock(
        start=_now() + timedelta(hours=start_offset),
        end=_now() + timedelta(hours=end_offset),
        expected_kwh=10.0, expected_cost_eur=2.0,
        price_eur_per_kwh=0.20, source="tibber", confidence=1.0,
    )


# --- Schalt-Logik ---

@patch("tibber_charge.main.HomeAssistantClient")
def test_now_innerhalb_block_schaltet_ON(client_cls):
    client = client_cls.return_value
    client.set_input_boolean.return_value = True
    plan = _plan(blocks=(_block(-1, 1),))  # now ist drin
    state = State()

    _drive_charging(_config(), state, [plan], _now())

    client.set_input_boolean.assert_called_once_with("input_boolean.wb", True)
    assert state.hardware.last_charging_state == "on"
    assert state.hardware.last_charging_set_at is not None


@patch("tibber_charge.main.HomeAssistantClient")
def test_now_ausserhalb_aller_bloecke_schaltet_OFF(client_cls):
    client = client_cls.return_value
    client.set_input_boolean.return_value = True
    plan = _plan(blocks=(_block(3, 5),))  # now ist davor
    state = State()
    state.hardware.last_charging_state = "on"  # vorher war an

    _drive_charging(_config(), state, [plan], _now())

    client.set_input_boolean.assert_called_once_with("input_boolean.wb", False)
    assert state.hardware.last_charging_state == "off"


@patch("tibber_charge.main.HomeAssistantClient")
def test_unveraenderter_state_kein_call(client_cls):
    """Idempotenz: wenn last_state == desired_state, kein HA-Call."""
    client = client_cls.return_value
    plan = _plan(blocks=(_block(-1, 1),))
    state = State()
    state.hardware.last_charging_state = "on"  # bereits an

    _drive_charging(_config(heartbeat=None), state, [plan], _now())

    client.set_input_boolean.assert_not_called()


@patch("tibber_charge.main.HomeAssistantClient")
def test_block_grenze_exclusive_end(client_cls):
    """Block 03:00-04:00 → bei 04:00:00 ist now NICHT mehr drin."""
    client = client_cls.return_value
    client.set_input_boolean.return_value = True
    # Block endet exakt bei now
    plan = _plan(blocks=(_block(-1, 0),))
    state = State()

    _drive_charging(_config(), state, [plan], _now())
    # Erwartung: OFF, weil end == now
    client.set_input_boolean.assert_called_once_with("input_boolean.wb", False)


@patch("tibber_charge.main.HomeAssistantClient")
def test_block_grenze_inclusive_start(client_cls):
    """Block 22:00-23:00 → bei 22:00:00 ist now drin."""
    client = client_cls.return_value
    client.set_input_boolean.return_value = True
    plan = _plan(blocks=(_block(0, 1),))
    state = State()

    _drive_charging(_config(), state, [plan], _now())
    client.set_input_boolean.assert_called_once_with("input_boolean.wb", True)


@patch("tibber_charge.main.HomeAssistantClient")
def test_keine_plaene_schaltet_OFF(client_cls):
    """Wenn überhaupt kein Plan vorliegt → OFF."""
    client = client_cls.return_value
    client.set_input_boolean.return_value = True
    state = State()
    state.hardware.last_charging_state = "on"

    _drive_charging(_config(), state, [], _now())
    client.set_input_boolean.assert_called_once_with("input_boolean.wb", False)


@patch("tibber_charge.main.HomeAssistantClient")
def test_ha_disabled_kein_call(client_cls):
    plan = _plan(blocks=(_block(-1, 1),))
    state = State()
    _drive_charging(_config(enabled=False), state, [plan], _now())
    client_cls.assert_not_called()


@patch("tibber_charge.main.HomeAssistantClient")
def test_kein_token_kein_call(client_cls):
    config = Config(
        tibber_api_key="x", google_calendar_id="y",
        homeassistant=HomeAssistantConfig(enabled=True, api_token=None),
    )
    plan = _plan(blocks=(_block(-1, 1),))
    _drive_charging(config, State(), [plan], _now())
    client_cls.assert_not_called()


# --- Schaltfehler ---

@patch("tibber_charge.main.HomeAssistantClient")
def test_ha_fehler_state_bleibt_unveraendert(client_cls):
    client = client_cls.return_value
    client.set_input_boolean.return_value = False  # Fehler
    plan = _plan(blocks=(_block(-1, 1),))
    state = State()
    state.hardware.last_charging_state = "off"

    _drive_charging(_config(), state, [plan], _now())
    # State NICHT geändert — beim nächsten Lauf nochmal versuchen
    assert state.hardware.last_charging_state == "off"


# --- Heartbeat ---

@patch("tibber_charge.main.HomeAssistantClient")
def test_heartbeat_jeden_lauf(client_cls):
    client = client_cls.return_value
    client.set_input_boolean.return_value = True
    client.write_input_datetime.return_value = True
    plan = _plan(blocks=(_block(-1, 1),))
    state = State()

    _drive_charging(_config(heartbeat="input_datetime.heartbeat"), state, [plan], _now())

    client.write_input_datetime.assert_called_once()
    assert state.hardware.last_heartbeat_at is not None


@patch("tibber_charge.main.HomeAssistantClient")
def test_heartbeat_auch_wenn_schalt_state_unveraendert(client_cls):
    """Heartbeat läuft jeden Run — auch wenn input_boolean nicht angefasst wird."""
    client = client_cls.return_value
    client.write_input_datetime.return_value = True
    plan = _plan(blocks=(_block(-1, 1),))
    state = State()
    state.hardware.last_charging_state = "on"  # idempotent

    _drive_charging(_config(heartbeat="input_datetime.heartbeat"), state, [plan], _now())
    client.set_input_boolean.assert_not_called()
    client.write_input_datetime.assert_called_once()


@patch("tibber_charge.main.HomeAssistantClient")
def test_heartbeat_disabled_kein_call(client_cls):
    client = client_cls.return_value
    client.set_input_boolean.return_value = True
    plan = _plan(blocks=(_block(-1, 1),))
    state = State()
    _drive_charging(_config(heartbeat=None), state, [plan], _now())
    client.write_input_datetime.assert_not_called()


# ---- Log-Level der Plan-Hinweise ----

def _plan_with(feasible: bool, warnings: list[str]):
    from types import SimpleNamespace
    return SimpleNamespace(
        deadline=_now() + timedelta(days=1), current_soc=20, target_soc=80, kwh_needed=10.0,
        cycles=1, total_cost_eur=4.0, feasible=feasible, blocks=(), warnings=warnings,
    )


def test_hinweise_machbarer_plaene_nur_info(caplog):
    """96 Cron-Läufe am Tag sollen das Log nicht mit 'Ziel schon erreicht' füllen."""
    import logging
    from types import SimpleNamespace
    from tibber_charge.main import _log_plan
    caplog.set_level(logging.INFO)
    _log_plan(SimpleNamespace(target_soc=10, car_id=None), _plan_with(True, ["Ziel-SoC bereits erreicht"]))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_nicht_machbarer_plan_warnt(caplog):
    import logging
    from types import SimpleNamespace
    from tibber_charge.main import _log_plan
    caplog.set_level(logging.INFO)
    _log_plan(SimpleNamespace(target_soc=100, car_id=None), _plan_with(False, ["zu wenig Zeit"]))
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("NICHT erreichbar" in m for m in warnings)
    assert any("zu wenig Zeit" in m for m in warnings)
