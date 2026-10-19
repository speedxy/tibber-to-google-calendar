"""Tests für load_config — fokussiert auf FORECAST-Block-Parsing."""
from __future__ import annotations

import json
from pathlib import Path

from tibber_charge.config import ForecastConfig, load_config


def _write_config(tmp_path: Path, data: dict) -> str:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(data))
    return str(tmp_path)


def test_forecast_calendar_events_enabled_default_true(tmp_path):
    base = _write_config(tmp_path, {
        "TIBBER_API_KEY": "k", "GOOGLE_CALENDAR_ID": "c",
        "FORECAST": {"enabled": True, "provider": "open_meteo"},
    })
    cfg = load_config(base)
    assert cfg.forecast.calendar_events_enabled is True


def test_forecast_calendar_events_enabled_can_be_disabled(tmp_path):
    base = _write_config(tmp_path, {
        "TIBBER_API_KEY": "k", "GOOGLE_CALENDAR_ID": "c",
        "FORECAST": {"enabled": True, "calendar_events_enabled": False, "provider": "open_meteo"},
    })
    cfg = load_config(base)
    assert cfg.forecast.enabled is True
    assert cfg.forecast.calendar_events_enabled is False


def test_forecast_block_missing_uses_defaults(tmp_path):
    base = _write_config(tmp_path, {
        "TIBBER_API_KEY": "k", "GOOGLE_CALENDAR_ID": "c",
    })
    cfg = load_config(base)
    assert cfg.forecast.enabled is True
    assert cfg.forecast.calendar_events_enabled is True


def test_tibber_data_api_block_wird_geparst(tmp_path):
    base = _write_config(tmp_path, {
        "TIBBER_API_KEY": "k", "GOOGLE_CALENDAR_ID": "c",
        "TIBBER_DATA_API": {"client_id": "id", "client_secret": "sec"},
    })
    cfg = load_config(base)
    assert cfg.tibber_data_api is not None
    assert cfg.tibber_data_api.client_id == "id"
    assert cfg.tibber_data_api.redirect_uri == "http://localhost:8765/callback"


def test_tibber_data_api_ohne_secret_ist_none(tmp_path):
    base = _write_config(tmp_path, {
        "TIBBER_API_KEY": "k", "GOOGLE_CALENDAR_ID": "c",
        "TIBBER_DATA_API": {"client_id": "id"},
    })
    assert load_config(base).tibber_data_api is None


def test_charging_ohne_block_nutzt_15_minuten_slots(tmp_path):
    """Regression: load_config setzte ohne CHARGING-Block 60 min ein, obwohl
    Dataclass und README 15 min versprechen. Folge: Wallbox lief bis zu 1 h
    länger als für die geplante kWh-Menge nötig."""
    base = _write_config(tmp_path, {"TIBBER_API_KEY": "k", "GOOGLE_CALENDAR_ID": "c"})
    assert load_config(base).charging.slot_minutes == 15


def test_forecast_config_default():
    fc = ForecastConfig()
    assert fc.enabled is True
    assert fc.calendar_events_enabled is True
