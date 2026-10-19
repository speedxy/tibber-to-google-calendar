"""Tests für fetch_status_with_cache + State-Integration."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses

from tibber_charge.config import Config, ForecastConfig, VehicleConfig
from tibber_charge.state import State, VehicleStateCache, load_state, save_state
from tibber_charge.vehicle.cache import fetch_status_with_cache
from tibber_charge.vehicle.tessie import TESSIE_BASE_URL


VIN = "5YJ3E1EA4MF000001"


def _now() -> datetime:
    return datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc).astimezone()


def _config_manual(**overrides) -> Config:
    base = dict(
        enabled=True, provider="manual", vehicle_name="TestAuto",
        manual_soc=42, manual_connected=True, manual_charging=False,
    )
    base.update(overrides)
    return Config(
        tibber_api_key="x", google_calendar_id="y",
        forecast=ForecastConfig(enabled=False),
        vehicle=VehicleConfig(**base),
    )


def _config_tessie(**overrides) -> Config:
    base = dict(
        enabled=True, provider="tessie", vehicle_name="Model 3",
        vin=VIN, api_token="tok",
    )
    base.update(overrides)
    return Config(
        tibber_api_key="x", google_calendar_id="y",
        forecast=ForecastConfig(enabled=False),
        vehicle=VehicleConfig(**base),
    )


def test_manual_provider_liefert_status():
    s = State()
    status = fetch_status_with_cache(s, _config_manual(), _now())
    assert status is not None
    assert status.soc == 42
    assert status.is_connected is True
    assert s.vehicle.last_status is not None
    assert s.vehicle.provider == "manual"


def test_disabled_liefert_None_oder_cache():
    s = State()
    status = fetch_status_with_cache(s, _config_manual(enabled=False), _now())
    assert status is None  # noch nichts im Cache


def test_disabled_aber_cache_vorhanden():
    s = State()
    s.vehicle.last_status = {
        "soc": 80, "is_connected": True, "is_charging": False,
        "last_updated_at": "2026-05-20T12:00:00+02:00",
        "vehicle_name": "Model 3", "source": "tessie",
    }
    status = fetch_status_with_cache(s, _config_manual(enabled=False), _now())
    assert status is not None
    assert status.soc == 80


@responses.activate
def test_tessie_erfolg_befuellt_cache():
    responses.add(
        responses.GET, f"{TESSIE_BASE_URL}/{VIN}/state",
        json={
            "charge_state": {"battery_level": 70, "conn_charge_cable": "IEC", "charging_state": "Stopped"},
            "vehicle_state": {"timestamp": 1779228000000},
        }, status=200,
    )
    s = State()
    status = fetch_status_with_cache(s, _config_tessie(), _now())
    assert status is not None
    assert status.soc == 70
    assert s.vehicle.provider == "tessie"
    assert s.vehicle.last_success_at is not None
    assert s.vehicle.backoff_seconds == 0


@responses.activate
def test_tessie_netzfehler_setzt_backoff_und_nutzt_cache():
    s = State()
    s.vehicle.last_status = {
        "soc": 55, "is_connected": True, "is_charging": False,
        "last_updated_at": "2026-05-20T10:00:00+02:00",
        "vehicle_name": "Model 3", "source": "tessie",
    }
    responses.add(
        responses.GET, f"{TESSIE_BASE_URL}/{VIN}/state",
        body=requests.ConnectionError("boom"),
    )
    status = fetch_status_with_cache(s, _config_tessie(), _now())
    assert status is not None
    assert status.soc == 55
    assert s.vehicle.next_retry_after is not None
    assert s.vehicle.backoff_seconds > 0
    assert "boom" in (s.vehicle.last_error or "")


def test_tessie_ohne_vin_oder_token_kein_crash():
    s = State()
    status = fetch_status_with_cache(s, _config_tessie(vin=None), _now())
    assert status is None
    assert s.vehicle.last_error is not None


def test_backoff_aktiv_skipped_request():
    s = State()
    s.vehicle.next_retry_after = (_now() + timedelta(minutes=10)).isoformat()
    s.vehicle.last_status = {
        "soc": 33, "is_connected": False, "is_charging": False,
        "last_updated_at": "2026-05-20T10:00:00+02:00",
        "vehicle_name": "Auto", "source": "tessie",
    }
    # Sollte den Cache zurückgeben, ohne HTTP zu rufen — responses ist
    # nicht aktiviert, also würde ein echter Call netzweit fehlschlagen.
    status = fetch_status_with_cache(s, _config_tessie(), _now())
    assert status is not None
    assert status.soc == 33


def test_unbekannter_provider_kein_crash():
    s = State()
    status = fetch_status_with_cache(
        s,
        Config(
            tibber_api_key="x", google_calendar_id="y",
            forecast=ForecastConfig(enabled=False),
            vehicle=VehicleConfig(provider="weird_unknown"),
        ),
        _now(),
    )
    assert status is None
    assert s.vehicle.last_error is not None


def test_state_ohne_vehicle_block_laed_sauber():
    """Alte state.json ohne vehicle-Block muss tolerant geladen werden."""
    import json
    with tempfile.TemporaryDirectory() as tmp:
        import os
        path = os.path.join(tmp, "state.json")
        with open(path, "w") as f:
            json.dump({"schema_version": 1, "tibber": {}, "forecast": {}}, f)
        s = load_state(tmp)
        assert s.schema_version == 1
        assert s.vehicle.provider is None
        assert s.vehicle.last_status is None


def test_state_roundtrip_mit_vehicle():
    with tempfile.TemporaryDirectory() as tmp:
        s = State()
        s.vehicle.provider = "tessie"
        s.vehicle.last_status = {
            "soc": 60, "is_connected": True, "is_charging": False,
            "last_updated_at": "2026-05-20T12:00:00+02:00",
            "vehicle_name": "Model 3", "source": "tessie",
        }
        s.vehicle.backoff_seconds = 300
        save_state(tmp, s)
        s2 = load_state(tmp)
        assert s2.vehicle.provider == "tessie"
        assert s2.vehicle.last_status["soc"] == 60
        assert s2.vehicle.backoff_seconds == 300
