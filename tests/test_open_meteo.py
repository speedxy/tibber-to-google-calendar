"""Tests für OpenMeteoForecastProvider und weather_to_renewable_index."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses

from tibber_charge.prices.forecast.open_meteo import (
    OPEN_METEO_API_URL,
    OpenMeteoForecastProvider,
    weather_to_renewable_index,
)


def _now_local() -> datetime:
    return datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc).astimezone()


def _mock_response(times: list[str], winds: list, solars: list, temps: list | None = None) -> dict:
    return {
        "hourly": {
            "time": times,
            "wind_speed_100m": winds,
            "shortwave_radiation": solars,
            "temperature_2m": temps if temps is not None else [15.0] * len(times),
        }
    }


def test_renewable_index_grenzwerte():
    assert weather_to_renewable_index(0, 0) == pytest.approx(0.0)
    assert weather_to_renewable_index(30, 600) == pytest.approx(100.0)


def test_renewable_index_clamping_nach_oben():
    """Sehr starker Sturm + Sonne bleiben bei 100."""
    assert weather_to_renewable_index(200, 1500) == pytest.approx(100.0)


def test_renewable_index_clamping_nach_unten():
    """Negative Werte (sollte nicht vorkommen) sicher bei 0."""
    assert weather_to_renewable_index(-5, -10) == pytest.approx(0.0)


def test_renewable_index_wind_dominiert():
    """Wind 70 %, Solar 30 %: voll Wind, kein Solar → 70."""
    assert weather_to_renewable_index(30, 0) == pytest.approx(70.0)
    assert weather_to_renewable_index(0, 600) == pytest.approx(30.0)


@responses.activate
def test_valides_json_wird_zu_unifiedprices():
    # Stunden weit in der Zukunft (>36h), damit Penalty nicht 0 ist
    base = _now_local() + timedelta(hours=48)
    times = [(base + timedelta(hours=i)).astimezone(timezone.utc).replace(tzinfo=None).isoformat()
             for i in range(3)]
    responses.add(
        responses.GET, OPEN_METEO_API_URL,
        json=_mock_response(times, [30, 60, 0], [0, 600, 100], temps=[10.0, 12.0, 14.0]),
        status=200,
    )
    provider = OpenMeteoForecastProvider(latitude=53.87, longitude=10.69)
    prices = provider.fetch(_now_local())
    assert len(prices) == 3
    assert all(p.source == "open_meteo" for p in prices)
    assert all(p.raw_total is not None for p in prices)
    # mehr Wind+Solar → niedrigerer Roh-Preis
    assert prices[1].raw_total < prices[0].raw_total
    # Penalty greift bei h>36
    assert all(p.total > p.raw_total for p in prices)
    # meta enthält die Rohfeatures für späteres Logging
    assert prices[0].meta is not None
    assert prices[0].meta["wind_kmh"] == 30
    assert prices[0].meta["solar_wm2"] == 0
    assert prices[0].meta["temperature_c"] == 10.0
    assert prices[0].meta["latitude"] == 53.87
    assert prices[0].meta["longitude"] == 10.69


@responses.activate
def test_http_500_wirft_requestexception():
    responses.add(responses.GET, OPEN_METEO_API_URL, status=500)
    provider = OpenMeteoForecastProvider(latitude=53.87, longitude=10.69)
    with pytest.raises(requests.HTTPError):
        provider.fetch(_now_local())


@responses.activate
def test_leere_zeitreihe_wirft_runtimeerror():
    responses.add(
        responses.GET, OPEN_METEO_API_URL,
        json={"hourly": {"time": [], "wind_speed_100m": [], "shortwave_radiation": []}},
        status=200,
    )
    provider = OpenMeteoForecastProvider(latitude=53.87, longitude=10.69)
    with pytest.raises(RuntimeError):
        provider.fetch(_now_local())


@responses.activate
def test_inkonsistente_laengen_wirft_runtimeerror():
    base = _now_local() + timedelta(hours=48)
    times = [(base + timedelta(hours=i)).astimezone(timezone.utc).replace(tzinfo=None).isoformat()
             for i in range(3)]
    responses.add(
        responses.GET, OPEN_METEO_API_URL,
        json=_mock_response(times, [10, 20], [0, 0, 0]),  # Wind hat 2 statt 3 Werte
        status=200,
    )
    provider = OpenMeteoForecastProvider(latitude=53.87, longitude=10.69)
    with pytest.raises(RuntimeError) as exc_info:
        provider.fetch(_now_local())
    assert "inkonsistent" in str(exc_info.value)


@responses.activate
def test_null_werte_werden_uebersprungen():
    base = _now_local() + timedelta(hours=48)
    times = [(base + timedelta(hours=i)).astimezone(timezone.utc).replace(tzinfo=None).isoformat()
             for i in range(4)]
    responses.add(
        responses.GET, OPEN_METEO_API_URL,
        json=_mock_response(times, [30, None, 40, 50], [100, 200, None, 400]),
        status=200,
    )
    provider = OpenMeteoForecastProvider(latitude=53.87, longitude=10.69)
    prices = provider.fetch(_now_local())
    assert len(prices) == 2  # 2 mit None → übersprungen


@responses.activate
def test_koordinaten_und_horizon_landen_in_url():
    captured: dict = {}

    def callback(request):
        captured["url"] = request.url
        return (200, {}, '{"hourly":{"time":[],"wind_speed_100m":[],"shortwave_radiation":[]}}')

    responses.add_callback(responses.GET, OPEN_METEO_API_URL, callback=callback)
    provider = OpenMeteoForecastProvider(latitude=54.12, longitude=10.34, forecast_days=5)
    try:
        provider.fetch(_now_local())
    except RuntimeError:
        pass
    assert "latitude=54.12" in captured["url"]
    assert "longitude=10.34" in captured["url"]
    assert "forecast_days=5" in captured["url"]
