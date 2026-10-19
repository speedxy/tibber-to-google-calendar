"""Tests für TessieVehicleProvider mit responses-Mock."""
from __future__ import annotations

import pytest
import requests
import responses

from tibber_charge.vehicle.tessie import TESSIE_BASE_URL, TessieVehicleProvider


VIN = "5YJ3E1EA4MF000000"


def _state_payload(battery: int = 75, cable: str = "IEC", charging_state: str = "Stopped") -> dict:
    return {
        "charge_state": {
            "battery_level": battery,
            "conn_charge_cable": cable,
            "charging_state": charging_state,
            "charge_port_door_open": True,
        },
        "vehicle_state": {
            "timestamp": 1779228000000,  # ms
            "car_version": "2024.20.1",
        },
    }


@responses.activate
def test_valides_payload_wird_geparst():
    responses.add(
        responses.GET, f"{TESSIE_BASE_URL}/{VIN}/state",
        json=_state_payload(battery=80, cable="IEC", charging_state="Charging"),
        status=200,
    )
    p = TessieVehicleProvider(vin=VIN, api_token="t", vehicle_name="Model 3")
    s = p.get_status()
    assert s.soc == 80
    assert s.is_connected is True
    assert s.is_charging is True
    assert s.vehicle_name == "Model 3"
    assert s.source == "tessie"
    assert s.last_updated_at is not None


@responses.activate
def test_disconnected_cable_leer_und_state_disconnected():
    responses.add(
        responses.GET, f"{TESSIE_BASE_URL}/{VIN}/state",
        json=_state_payload(battery=65, cable="", charging_state="Disconnected"),
        status=200,
    )
    p = TessieVehicleProvider(vin=VIN, api_token="t")
    s = p.get_status()
    assert s.soc == 65
    assert s.is_connected is False
    assert s.is_charging is False


@responses.activate
def test_connected_aber_nicht_ladend():
    responses.add(
        responses.GET, f"{TESSIE_BASE_URL}/{VIN}/state",
        json=_state_payload(battery=90, cable="IEC", charging_state="Stopped"),
        status=200,
    )
    p = TessieVehicleProvider(vin=VIN, api_token="t")
    s = p.get_status()
    assert s.is_connected is True
    assert s.is_charging is False


@responses.activate
def test_use_cache_true_im_request():
    captured: dict = {}

    def callback(request):
        captured["url"] = request.url
        captured["auth"] = request.headers.get("Authorization", "")
        return (200, {}, '{"charge_state":{"battery_level":50,"conn_charge_cable":"IEC","charging_state":"Stopped"},"vehicle_state":{"timestamp":1779228000000}}')

    responses.add_callback(responses.GET, f"{TESSIE_BASE_URL}/{VIN}/state", callback=callback)
    p = TessieVehicleProvider(vin=VIN, api_token="my-secret-token")
    p.get_status()
    assert "use_cache=true" in captured["url"]
    assert captured["auth"] == "Bearer my-secret-token"


@responses.activate
def test_http_401_wirft_requestexception():
    responses.add(responses.GET, f"{TESSIE_BASE_URL}/{VIN}/state", status=401)
    p = TessieVehicleProvider(vin=VIN, api_token="bad-token")
    with pytest.raises(requests.HTTPError):
        p.get_status()


@responses.activate
def test_leerer_payload_wirft_runtimeerror():
    responses.add(
        responses.GET, f"{TESSIE_BASE_URL}/{VIN}/state",
        json={"charge_state": {}, "vehicle_state": {}}, status=200,
    )
    p = TessieVehicleProvider(vin=VIN, api_token="t")
    with pytest.raises(RuntimeError):
        p.get_status()


def test_fehlende_vin_wirft_value():
    with pytest.raises(ValueError):
        TessieVehicleProvider(vin="", api_token="t")


def test_fehlender_token_wirft_value():
    with pytest.raises(ValueError):
        TessieVehicleProvider(vin=VIN, api_token="")
