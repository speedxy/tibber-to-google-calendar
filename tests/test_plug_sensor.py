"""Tests für den Wallbox-Stecker-Sensor (HA) als Quelle für "eingesteckt"."""
from __future__ import annotations

import responses

from tibber_charge.config import Config, HomeAssistantConfig
from tibber_charge.hardware.homeassistant import HomeAssistantClient
from tibber_charge.main import _apply_plug_sensor
from tibber_charge.vehicle.base import VehicleStatus


BASE = "http://ha.local:8123"
PLUG = "binary_sensor.keba_p30_plug"


def _config(plug: str | None = PLUG, enabled: bool = True) -> Config:
    return Config(
        tibber_api_key="x", google_calendar_id="y",
        homeassistant=HomeAssistantConfig(
            enabled=enabled, base_url=BASE, api_token="tok", plug_entity_id=plug,
        ),
    )


def _status(connected: bool) -> VehicleStatus:
    return VehicleStatus(
        soc=19, is_connected=connected, is_charging=False,
        last_updated_at=None, vehicle_name="Model 3", source="tibber",
    )


@responses.activate
def test_get_state_liefert_zustand():
    responses.get(f"{BASE}/api/states/{PLUG}", json={"entity_id": PLUG, "state": "on"})
    assert HomeAssistantClient(BASE, "tok").get_state(PLUG) == "on"


@responses.activate
def test_get_state_bei_fehler_none():
    responses.get(f"{BASE}/api/states/{PLUG}", status=404)
    assert HomeAssistantClient(BASE, "tok").get_state(PLUG) is None


@responses.activate
def test_wallbox_sensor_ueberschreibt_verzoegerten_providerwert():
    """Tibber meldet noch "nicht eingesteckt", die Wallbox weiß es besser."""
    responses.get(f"{BASE}/api/states/{PLUG}", json={"state": "on"})
    result = _apply_plug_sensor(_config(), _status(connected=False))
    assert result.is_connected is True
    assert result.soc == 19  # Rest unverändert


@responses.activate
def test_wallbox_sensor_aus_setzt_nicht_eingesteckt():
    responses.get(f"{BASE}/api/states/{PLUG}", json={"state": "off"})
    assert _apply_plug_sensor(_config(), _status(connected=True)).is_connected is False


@responses.activate
def test_unbrauchbarer_sensorwert_behaelt_providerwert():
    responses.get(f"{BASE}/api/states/{PLUG}", json={"state": "unavailable"})
    assert _apply_plug_sensor(_config(), _status(connected=True)).is_connected is True


def test_ohne_plug_entity_kein_ha_call():
    # Ohne responses-Mock würde ein echter Call scheitern: es darf keiner passieren.
    status = _status(connected=True)
    assert _apply_plug_sensor(_config(plug=None), status) is status


def test_ha_deaktiviert_kein_ha_call():
    status = _status(connected=False)
    assert _apply_plug_sensor(_config(enabled=False), status) is status


def test_ohne_status_none():
    assert _apply_plug_sensor(_config(), None) is None
