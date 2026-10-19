"""Tests für should_fetch_forecast + fetch_forecast_with_cache."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses

from tibber_charge.config import Config, ForecastConfig
from tibber_charge.prices.forecast.cache import (
    fetch_forecast_with_cache,
    should_fetch_forecast,
)
from tibber_charge.prices.forecast.corrently import CORRENTLY_API_URL
from tibber_charge.state import ForecastCache, State, load_state, save_state


def _now() -> datetime:
    return datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc).astimezone()


def _config(enabled: bool = True) -> Config:
    """Test-Config nutzt explizit den Corrently-Provider, weil das responses-Mock
    in den Tests die Corrently-URL bedient. Ohne Rückfall-Provider, damit die
    Fehlerpfade isoliert getestet werden. Default in Produktion ist EpexPredictor."""
    return Config(
        tibber_api_key="x",
        google_calendar_id="y",
        forecast=ForecastConfig(
            enabled=enabled, provider="corrently", zip_code="23552", fallback_provider=None,
        ),
    )


def test_leerer_state_fetch_True():
    s = State()
    do, reason = should_fetch_forecast(s, _now())
    assert do is True
    assert reason == "forecast_leer"


def test_backoff_aktiv_fetch_False():
    s = State()
    s.forecast.next_retry_after = (_now() + timedelta(hours=1)).isoformat()
    s.forecast.last_success_at = _now().isoformat()
    s.forecast.forecasts = [{"a": 1}]
    do, reason = should_fetch_forecast(s, _now())
    assert do is False
    assert "backoff" in reason


def test_direkt_nach_erfolg_vor_naechster_refresh_False():
    s = State()
    # Erfolgreich um 14:30 lokal — nächste Refresh-Stunde ist 14:00 morgen
    now_local = _now().replace(hour=14, minute=30)
    s.forecast.last_success_at = now_local.isoformat()
    s.forecast.forecasts = [{"a": 1}]
    # 1 Stunde später, also 15:30 — noch kein Refresh fällig (nächste: 06:00 morgen)
    do, reason = should_fetch_forecast(s, now_local + timedelta(hours=1))
    assert do is False
    assert "naechster_refresh" in reason


def test_nach_refresh_stunde_True():
    s = State()
    # Letzter Erfolg gestern 14:30 — heute 06:00 sollte Refresh-pflichtig sein
    yesterday = _now().replace(hour=14, minute=30) - timedelta(days=1)
    s.forecast.last_success_at = yesterday.isoformat()
    s.forecast.forecasts = [{"a": 1}]
    today_morning = _now().replace(hour=7, minute=0)
    do, reason = should_fetch_forecast(s, today_morning)
    assert do is True
    assert "refresh_faellig" in reason


def test_forecast_deaktiviert_kein_fetch():
    s = State()
    config = _config(enabled=False)
    prices, fresh = fetch_forecast_with_cache(s, config, _now())
    assert prices == []
    assert fresh is False


@responses.activate
def test_erfolg_befuellt_cache():
    responses.add(
        responses.GET, CORRENTLY_API_URL,
        json={"forecast": [
            {"epochtime": int(_now().timestamp()) + 40 * 3600, "gsi": 70},
            {"epochtime": int(_now().timestamp()) + 41 * 3600, "gsi": 75},
        ]},
        status=200,
    )
    s = State()
    config = _config()
    prices, fresh = fetch_forecast_with_cache(s, config, _now())
    assert fresh is True
    assert len(prices) == 2
    assert s.forecast.provider == "corrently"
    assert s.forecast.last_success_at is not None
    assert s.forecast.last_error is None
    assert len(s.forecast.forecasts) == 2


@responses.activate
def test_netzfehler_setzt_backoff_und_liefert_cache():
    s = State()
    s.forecast.last_success_at = (_now() - timedelta(days=2)).isoformat()
    s.forecast.forecasts = [{
        "startsAt": _now().isoformat(),
        "total": 0.22, "level": "NORMAL",
        "source": "corrently", "confidence": 0.5, "raw_total": 0.20,
    }]
    responses.add(responses.GET, CORRENTLY_API_URL, body=requests.ConnectionError("boom"))
    config = _config()
    prices, fresh = fetch_forecast_with_cache(s, config, _now())
    assert fresh is False
    assert len(prices) == 1  # Cache liefert weiter
    assert s.forecast.next_retry_after is not None
    assert s.forecast.backoff_seconds > 0
    assert "boom" in (s.forecast.last_error or "")


@responses.activate
def test_unbekannter_provider_kein_crash():
    s = State()
    config = Config(
        tibber_api_key="x", google_calendar_id="y",
        forecast=ForecastConfig(enabled=True, provider="unknown_provider"),
    )
    prices, fresh = fetch_forecast_with_cache(s, config, _now())
    assert prices == []
    assert fresh is False
    assert s.forecast.last_error is not None


def test_state_ohne_forecast_block_laed_sauber():
    """Alte state.json (Phase 2) ohne forecast-Block muss tolerant geladen werden."""
    import json
    with tempfile.TemporaryDirectory() as tmp:
        import os
        path = os.path.join(tmp, "state.json")
        with open(path, "w") as f:
            json.dump({"schema_version": 1, "tibber": {}}, f)
        s = load_state(tmp)
        assert s.schema_version == 1
        assert s.forecast.provider is None
        assert s.forecast.forecasts == []


def test_state_roundtrip_mit_forecast():
    """Forecast-State überlebt save/load."""
    with tempfile.TemporaryDirectory() as tmp:
        s = State()
        s.forecast.provider = "corrently"
        s.forecast.forecasts = [{
            "startsAt": "2026-05-22T10:00:00+02:00",
            "total": 0.22, "level": "NORMAL",
            "source": "corrently", "confidence": 0.5, "raw_total": 0.20,
        }]
        s.forecast.backoff_seconds = 1800
        save_state(tmp, s)
        s2 = load_state(tmp)
        assert s2.forecast.provider == "corrently"
        assert len(s2.forecast.forecasts) == 1
        assert s2.forecast.backoff_seconds == 1800
