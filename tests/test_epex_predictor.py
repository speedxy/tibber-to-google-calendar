"""Tests für EpexPredictorForecastProvider und den Rückfall auf Open-Meteo."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses

from tibber_charge.config import Config, ForecastConfig
from tibber_charge.prices.forecast.cache import fetch_forecast_with_cache
from tibber_charge.prices.forecast.epex_predictor import EPEX_PREDICTOR_URL, EpexPredictorForecastProvider
from tibber_charge.prices.forecast.open_meteo import OPEN_METEO_API_URL
from tibber_charge.state import State


NOW = datetime(2026, 10, 19, 21, 0, tzinfo=timezone.utc).astimezone()


def _payload(start: datetime, values: list[float]) -> dict:
    """Format der echten API: parallele Arrays, s = Epoch-Sekunden, 15-min-Raster."""
    epoch = int(start.timestamp())
    return {"s": [epoch + 900 * i for i in range(len(values))], "t": values}


@responses.activate
def test_viertelstunden_werden_zu_stundenmitteln():
    start = NOW + timedelta(days=2)
    responses.get(EPEX_PREDICTOR_URL, json=_payload(start, [0.10, 0.12, 0.14, 0.16, 0.20, 0.20, 0.20, 0.20]))
    prices = EpexPredictorForecastProvider().fetch(NOW)

    assert len(prices) == 2
    assert abs(prices[0].raw_total - 0.13) < 1e-9
    assert abs(prices[1].raw_total - 0.20) < 1e-9
    assert prices[0].source == "epex_predictor"
    assert prices[0].total > prices[0].raw_total   # Risikoaufschlag bei 2 Tagen Vorlauf
    assert prices[0].meta["region"] == "DE"


@responses.activate
def test_anfrage_ohne_aufschlaege():
    responses.get(EPEX_PREDICTOR_URL, json=_payload(NOW, [0.1] * 4))
    EpexPredictorForecastProvider(region="AT", horizon_days=5).fetch(NOW)
    params = responses.calls[0].request.params
    assert params["region"] == "AT"
    assert params["surcharge"] == "0"
    assert params["taxPercent"] == "0"
    assert params["hours"] == "120"


@responses.activate
def test_leere_antwort_wirft():
    responses.get(EPEX_PREDICTOR_URL, json={"s": [], "t": []})
    with pytest.raises(RuntimeError):
        EpexPredictorForecastProvider().fetch(NOW)


@responses.activate
def test_ausfall_faellt_auf_open_meteo_zurueck():
    responses.get(EPEX_PREDICTOR_URL, body=requests.ConnectionError("down"))
    t0 = NOW.astimezone(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    responses.get(OPEN_METEO_API_URL, json={"hourly": {
        "time": [(t0 + timedelta(hours=i)).isoformat() for i in range(3)],
        "wind_speed_100m": [10.0, 20.0, 30.0],
        "shortwave_radiation": [0.0, 0.0, 0.0],
        "temperature_2m": [10.0, 10.0, 10.0],
    }})
    state = State()
    config = Config(tibber_api_key="x", google_calendar_id="y", forecast=ForecastConfig())

    prices, fresh = fetch_forecast_with_cache(state, config, NOW)

    assert fresh is True
    assert len(prices) == 3
    assert state.forecast.provider == "open_meteo"


@responses.activate
def test_ohne_rueckfall_greift_backoff():
    responses.get(EPEX_PREDICTOR_URL, body=requests.ConnectionError("down"))
    state = State()
    config = Config(tibber_api_key="x", google_calendar_id="y",
                    forecast=ForecastConfig(fallback_provider=None))
    prices, fresh = fetch_forecast_with_cache(state, config, NOW)
    assert prices == [] and fresh is False
    assert state.forecast.next_retry_after is not None
