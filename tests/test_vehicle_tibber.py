"""Tests für TibberVehicleProvider (Tibber Data API) mit responses-Mock.

Payload-Struktur entspricht einer echten Antwort vom 2026-10-19 (IDs ersetzt).
"""
from __future__ import annotations

import base64
import time

import pytest
import responses

from tibber_charge.config import TibberDataApiConfig
from tibber_charge.tibber.data_api import API_BASE_URL, TibberDataClient, save_tokens
from tibber_charge.vehicle.tibber import TibberVehicleProvider, parse_device


HOME = "home-1"


def _device_id(kind: str, uuid: str) -> str:
    return base64.b64encode(f"{kind}:{uuid}".encode()).decode().rstrip("=")


TESLA_A = _device_id("tesla vehicle", "aaaa")
TESLA_B = _device_id("tesla vehicle", "bbbb")


def _details(device_id: str, external_id: str, soc=19, connector="connected", charging="idle") -> dict:
    return {
        "id": device_id,
        "externalId": external_id,
        "info": {"name": "Tesla 3 50", "brand": "Tesla", "model": "Model3"},
        "status": {"lastSeen": "2026-10-19T18:32:45.1387881+02:00"},
        "attributes": [{"id": "isOnline", "value": True}],
        "capabilities": [
            {"id": "storage.stateOfCharge", "value": soc, "unit": "%"},
            {"id": "storage.targetStateOfCharge", "value": 100, "unit": "%"},
            {"id": "connector.status", "value": connector},
            {"id": "charging.status", "value": charging},
        ],
    }


def _client(tmp_path) -> TibberDataClient:
    path = str(tmp_path / "tibber_data_token.json")
    save_tokens(path, {"access_token": "a", "refresh_token": "r", "expires_at": int(time.time()) + 3600})
    cfg = TibberDataApiConfig(client_id="c", client_secret="s")
    return TibberDataClient(cfg, path)


def _mock_api(devices: list[dict]) -> None:
    responses.get(f"{API_BASE_URL}/v1/homes", json={"homes": [{"id": HOME, "name": "Zuhause"}]})
    responses.get(
        f"{API_BASE_URL}/v1/homes/{HOME}/devices",
        json={"devices": [
            {"id": d["id"], "externalId": d["externalId"], "info": d["info"]} for d in devices
        ]},
    )
    for d in devices:
        responses.get(f"{API_BASE_URL}/v1/homes/{HOME}/devices/{d['id']}", json=d)


# ---- Parsing ----

def test_parse_eingesteckt_nicht_ladend():
    s = parse_device(_details(TESLA_A, "111"), "Model 3", "tibber")
    assert s.soc == 19
    assert s.is_connected is True
    assert s.is_charging is False
    assert s.source == "tibber"
    assert s.last_updated_at.startswith("2026-10-19T18:32")


def test_parse_ladend_gilt_als_eingesteckt():
    s = parse_device(_details(TESLA_A, "111", connector="unknown", charging="charging"), "A", "tibber")
    assert s.is_connected is True
    assert s.is_charging is True


def test_parse_unknown_gilt_als_nicht_eingesteckt():
    s = parse_device(_details(TESLA_A, "111", connector="unknown", charging="unknown"), "A", "tibber")
    assert s.is_connected is False
    assert s.is_charging is False


def test_parse_ohne_soc_wirft():
    with pytest.raises(RuntimeError):
        parse_device(_details(TESLA_A, "111", soc=None), "A", "tibber")


# ---- Provider ----

@responses.activate
def test_auswahl_ueber_external_id(tmp_path):
    _mock_api([_details(TESLA_A, "111", soc=26), _details(TESLA_B, "222", soc=19)])
    p = TibberVehicleProvider(_client(tmp_path), vehicle_id="222", vehicle_name="Model 3")
    s = p.get_status()
    assert s.soc == 19
    assert s.vehicle_name == "Model 3"


@responses.activate
def test_auswahl_ueber_tibber_id(tmp_path):
    _mock_api([_details(TESLA_A, "111", soc=26), _details(TESLA_B, "222", soc=19)])
    p = TibberVehicleProvider(_client(tmp_path), vehicle_id=TESLA_A)
    assert p.get_status().soc == 26


@responses.activate
def test_mehrere_fahrzeuge_ohne_id_wirft_mit_liste(tmp_path):
    _mock_api([_details(TESLA_A, "111"), _details(TESLA_B, "222")])
    p = TibberVehicleProvider(_client(tmp_path), vehicle_id=None)
    with pytest.raises(RuntimeError, match="tibber_vehicle_id=111.*tibber_vehicle_id=222"):
        p.get_status()


@responses.activate
def test_einziges_fahrzeug_ohne_id_wird_genommen(tmp_path):
    _mock_api([_details(TESLA_A, "111", soc=42)])
    p = TibberVehicleProvider(_client(tmp_path), vehicle_id=None)
    assert p.get_status().soc == 42


@responses.activate
def test_unbekannte_id_wirft(tmp_path):
    _mock_api([_details(TESLA_A, "111")])
    p = TibberVehicleProvider(_client(tmp_path), vehicle_id="999")
    with pytest.raises(RuntimeError, match="nicht gefunden"):
        p.get_status()


@responses.activate
def test_nicht_fahrzeug_geraete_werden_ignoriert(tmp_path):
    charger = _details(_device_id("easee charger", "cccc"), "333")
    _mock_api([charger, _details(TESLA_A, "111", soc=55)])
    p = TibberVehicleProvider(_client(tmp_path), vehicle_id=None)
    assert p.get_status().soc == 55
