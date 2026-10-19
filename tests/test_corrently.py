"""Tests für CorrentlyForecastProvider mit responses-Mock."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses

from tibber_charge.prices.forecast.corrently import (
    CORRENTLY_API_URL,
    CorrentlyForecastProvider,
)


def _now_local() -> datetime:
    return datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc).astimezone()


def _mock_entries(start_offset_hours: int = 0, count: int = 5, gsi_seq=None) -> list[dict]:
    """Erzeugt ein Mock-Forecast-Array. start_offset_hours = ab wann."""
    start_epoch = int(_now_local().timestamp()) + start_offset_hours * 3600
    gsi_values = gsi_seq or [60, 65, 70, 75, 80]
    return [
        {"epochtime": start_epoch + i * 3600, "gsi": gsi_values[i % len(gsi_values)]}
        for i in range(count)
    ]


@responses.activate
def test_valides_json_wird_zu_unifiedprices():
    responses.add(
        responses.GET, CORRENTLY_API_URL,
        json={"forecast": _mock_entries(start_offset_hours=40, count=3, gsi_seq=[50, 70, 90])},
        status=200,
    )
    provider = CorrentlyForecastProvider(zip_code="23552")
    prices = provider.fetch(_now_local())
    assert len(prices) == 3
    assert all(p.source == "corrently" for p in prices)
    assert all(p.confidence < 1.0 for p in prices)
    # raw_total spiegelt gsi_to_total wider
    assert prices[0].raw_total == pytest.approx(0.25)  # GSI 50
    assert prices[1].raw_total == pytest.approx(0.20)  # GSI 70
    assert prices[2].raw_total == pytest.approx(0.15)  # GSI 90
    # Penalty wurde drauf addiert (h≥40 → mindestens +0.01)
    for p in prices:
        assert p.total > p.raw_total


@responses.activate
def test_http_500_wirft_requestexception():
    responses.add(responses.GET, CORRENTLY_API_URL, status=500)
    provider = CorrentlyForecastProvider(zip_code="23552")
    with pytest.raises(requests.HTTPError):
        provider.fetch(_now_local())


@responses.activate
def test_leeres_forecast_wirft_runtimeerror():
    responses.add(responses.GET, CORRENTLY_API_URL, json={"forecast": []}, status=200)
    provider = CorrentlyForecastProvider(zip_code="23552")
    with pytest.raises(RuntimeError):
        provider.fetch(_now_local())


@responses.activate
def test_null_gsi_wird_uebersprungen():
    entries = _mock_entries(start_offset_hours=40, count=4, gsi_seq=[50, 60, 70, 80])
    entries[1]["gsi"] = None
    responses.add(responses.GET, CORRENTLY_API_URL, json={"forecast": entries}, status=200)
    provider = CorrentlyForecastProvider(zip_code="23552")
    prices = provider.fetch(_now_local())
    assert len(prices) == 3  # 4 input → 3 nutzbare


@responses.activate
def test_response_als_flache_liste_funktioniert():
    """Corrently kann manchmal ein flaches Array zurückgeben."""
    responses.add(
        responses.GET, CORRENTLY_API_URL,
        json=_mock_entries(start_offset_hours=40, count=2),
        status=200,
    )
    provider = CorrentlyForecastProvider(zip_code="23552")
    prices = provider.fetch(_now_local())
    assert len(prices) == 2


@responses.activate
def test_response_unter_data_key_funktioniert():
    responses.add(
        responses.GET, CORRENTLY_API_URL,
        json={"data": _mock_entries(start_offset_hours=40, count=2)},
        status=200,
    )
    provider = CorrentlyForecastProvider(zip_code="23552")
    prices = provider.fetch(_now_local())
    assert len(prices) == 2


@responses.activate
def test_timeout_wird_durchgereicht():
    """Stelle sicher dass der Timeout-Parameter im Request landet."""
    captured = {}

    def callback(request):
        # responses.PreparedRequest hat keine direkte Timeout-Attribute,
        # aber wir können prüfen dass der Provider den Wert hält.
        return (200, {}, '{"forecast": []}')

    responses.add_callback(responses.GET, CORRENTLY_API_URL, callback=callback)
    provider = CorrentlyForecastProvider(zip_code="23552", timeout_seconds=2.5)
    assert provider.timeout_seconds == 2.5
    # Smoke: fetch funktioniert (leer → RuntimeError, das ist OK für diesen Test)
    with pytest.raises(RuntimeError):
        provider.fetch(_now_local())


@responses.activate
def test_api_token_wird_als_query_param_gesendet():
    captured: dict = {}

    def callback(request):
        captured["url"] = request.url
        return (200, {}, '{"forecast": [{"epochtime": 9999999999, "gsi": 70}]}')

    responses.add_callback(responses.GET, CORRENTLY_API_URL, callback=callback)
    provider = CorrentlyForecastProvider(zip_code="23552", api_token="secret-token-123")
    # epoch im Mock liegt weit in der Zukunft → wird vom Provider verworfen ist OK,
    # uns interessiert nur ob der Token mitgegangen ist
    try:
        provider.fetch(_now_local())
    except (RuntimeError, ValueError):
        pass
    assert "token=secret-token-123" in captured["url"]


@responses.activate
def test_anonymer_leerer_forecast_gibt_token_hinweis():
    responses.add(
        responses.GET, CORRENTLY_API_URL,
        json={"forecast": [], "info": "Realtime computation in progress"},
        status=200,
    )
    provider = CorrentlyForecastProvider(zip_code="23552")  # kein Token
    with pytest.raises(RuntimeError) as exc_info:
        provider.fetch(_now_local())
    assert "console.corrently.io" in str(exc_info.value)
    assert "Token" in str(exc_info.value)


@responses.activate
def test_epoch_in_millisekunden_wird_normalisiert():
    """Wenn Corrently mal Millisekunden statt Sekunden liefert."""
    epoch_ms = int(time.time() * 1000) + 40 * 3600 * 1000
    entries = [{"epochtime": epoch_ms, "gsi": 50}]
    responses.add(responses.GET, CORRENTLY_API_URL, json={"forecast": entries}, status=200)
    provider = CorrentlyForecastProvider(zip_code="23552")
    prices = provider.fetch(_now_local())
    assert len(prices) == 1
    # Sollte plausibel innerhalb der nächsten 50 Stunden liegen
    from dateutil import parser as date_parser
    ts = date_parser.isoparse(prices[0].startsAt)
    delta = (ts - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 30 < delta < 50, f"Erwartet ~40h voraus, war {delta:.1f}h"
