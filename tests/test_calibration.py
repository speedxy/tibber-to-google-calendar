"""Tests für Kalibrierungs-Logging."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tibber_charge.prices.forecast.calibration import (
    MAX_SAMPLES,
    match_observations,
    record_predictions,
)
from tibber_charge.prices.types import UnifiedPrice
from tibber_charge.state import State


def _now() -> datetime:
    return datetime(2026, 5, 20, 22, 0, tzinfo=timezone.utc).astimezone()


def _up(hour_offset: int, raw: float = 0.20, total: float = 0.21,
        meta: dict | None = None) -> UnifiedPrice:
    ts = _now() + timedelta(hours=hour_offset)
    return UnifiedPrice(
        startsAt=ts.isoformat(),
        total=total, level="NORMAL",
        source="open_meteo", confidence=0.5,
        raw_total=raw,
        meta=meta,
    )


def test_record_predictions_legt_samples_an():
    s = State()
    forecasts = [_up(48), _up(49), _up(50)]
    added = record_predictions(s, forecasts, _now())
    assert added == 3
    assert len(s.forecast.calibration_samples) == 3
    first = s.forecast.calibration_samples[0]
    assert first["actual_total"] is None
    assert first["forecast_source"] == "open_meteo"
    assert first["forecast_raw_total"] == 0.20
    assert first["forecast_total"] == 0.21
    # Context-Features sind immer da
    assert "hour_of_day_local" in first
    assert "weekday" in first
    assert "month" in first
    # actual_tibber_level wartet auf Match
    assert first["actual_tibber_level"] is None


def test_record_predictions_uebernimmt_meta_features():
    s = State()
    meta = {
        "wind_kmh": 16.7, "solar_wm2": 579.0, "temperature_c": 12.4,
        "renewable_index": 67.9, "latitude": 53.87, "longitude": 10.69,
    }
    record_predictions(s, [_up(48, meta=meta)], _now())
    sample = s.forecast.calibration_samples[0]
    assert sample["wind_kmh"] == 16.7
    assert sample["solar_wm2"] == 579.0
    assert sample["temperature_c"] == 12.4
    assert sample["renewable_index"] == 67.9
    assert sample["latitude"] == 53.87
    assert sample["longitude"] == 10.69


def test_record_predictions_ohne_meta_wird_None_eingetragen():
    s = State()
    record_predictions(s, [_up(48, meta=None)], _now())
    sample = s.forecast.calibration_samples[0]
    assert sample["wind_kmh"] is None
    assert sample["solar_wm2"] is None
    assert sample["latitude"] is None


def test_record_predictions_dedupliziert_pro_target_hour():
    """First-write-wins — wenn dieselbe Stunde nochmal vorhergesagt wird, kein neues Sample."""
    s = State()
    record_predictions(s, [_up(48, raw=0.20, total=0.21)], _now())
    # Späterer Lauf, andere Werte für gleiche Stunde
    later_now = _now() + timedelta(hours=12)
    added = record_predictions(s, [_up(48, raw=0.18, total=0.19)], later_now)
    assert added == 0
    assert len(s.forecast.calibration_samples) == 1
    # Erste Vorhersage ist erhalten geblieben
    assert s.forecast.calibration_samples[0]["forecast_raw_total"] == 0.20


def test_match_observations_befuellt_actual():
    s = State()
    # Prognose für eine Stunde
    forecast = _up(48, raw=0.20, total=0.21)
    record_predictions(s, [forecast], _now())
    # Tibber liefert jetzt einen Preis für genau diese Stunde
    s.tibber.today_prices = [{"startsAt": forecast.startsAt, "total": 0.185, "level": "CHEAP"}]

    matched = match_observations(s, _now() + timedelta(hours=48))
    assert matched == 1
    sample = s.forecast.calibration_samples[0]
    assert sample["actual_total"] == 0.185
    assert sample["actual_tibber_level"] == "CHEAP"
    assert sample["actual_filled_at"] is not None


def test_match_observations_idempotent():
    """Wenn schon befüllt, nicht nochmal überschreiben."""
    s = State()
    forecast = _up(48, raw=0.20)
    record_predictions(s, [forecast], _now())
    s.tibber.today_prices = [{"startsAt": forecast.startsAt, "total": 0.185, "level": "CHEAP"}]
    match_observations(s, _now())
    # Tibber-Preis ändert sich (sollte real nicht passieren, aber zur Sicherheit)
    s.tibber.today_prices = [{"startsAt": forecast.startsAt, "total": 0.200, "level": "NORMAL"}]
    matched_again = match_observations(s, _now())
    assert matched_again == 0
    assert s.forecast.calibration_samples[0]["actual_total"] == 0.185


def test_match_observations_ohne_tibber_daten():
    s = State()
    record_predictions(s, [_up(48)], _now())
    matched = match_observations(s, _now())
    assert matched == 0


def test_match_observations_nutzt_tomorrow_prices():
    s = State()
    forecast = _up(36, raw=0.20)
    record_predictions(s, [forecast], _now())
    s.tibber.tomorrow_prices = [{"startsAt": forecast.startsAt, "total": 0.22, "level": "NORMAL"}]
    matched = match_observations(s, _now())
    assert matched == 1


def test_trim_dropt_unbefuellt_zuerst():
    s = State()
    # Über das Limit hinaus produzieren
    n = MAX_SAMPLES + 50
    forecasts = [_up(h) for h in range(n)]
    record_predictions(s, forecasts, _now())
    # Alle waren unbefüllt → trim auf MAX_SAMPLES
    assert len(s.forecast.calibration_samples) == MAX_SAMPLES


def test_trim_behaelt_befuellte_bevorzugt():
    s = State()
    # MAX_SAMPLES gefüllte + 50 unbefüllte → unbefüllte sollten zuerst raus
    filled_count = MAX_SAMPLES
    forecasts_filled = [_up(h) for h in range(filled_count)]
    record_predictions(s, forecasts_filled, _now())
    # Künstlich befüllen
    for sample in s.forecast.calibration_samples:
        sample["actual_total"] = 0.20
        sample["actual_filled_at"] = _now().isoformat()
    # Jetzt unbefüllte hinzufügen
    forecasts_unfilled = [_up(filled_count + h) for h in range(50)]
    record_predictions(s, forecasts_unfilled, _now())
    # MAX_SAMPLES insgesamt → die 50 unbefüllten wurden gedroppt
    assert len(s.forecast.calibration_samples) == MAX_SAMPLES
    assert all(s.get("actual_total") is not None for s in s.forecast.calibration_samples)


def test_record_ignoriert_ungueltigen_starts_at():
    s = State()
    invalid = UnifiedPrice(
        startsAt="not-a-date", total=0.20, level="NORMAL",
        source="open_meteo", confidence=0.5, raw_total=0.18,
    )
    added = record_predictions(s, [invalid], _now())
    assert added == 0


def _tibber_format(starts_at: str) -> str:
    """Tibber schreibt Millisekunden: '2026-05-22T22:00:00.000+02:00'."""
    ts = datetime.fromisoformat(starts_at)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000") + ts.strftime("%z")[:3] + ":" + ts.strftime("%z")[3:]


def test_match_trotz_unterschiedlicher_schreibweise():
    """Regression 2026-10-19: Textvergleich '…:00.000+02:00' vs '…:00+02:00'
    hat seit Mai kein einziges Sample gematcht (0 von 336)."""
    s = State()
    forecast = _up(48)
    record_predictions(s, [forecast], _now())
    s.tibber.today_prices = [{"startsAt": _tibber_format(forecast.startsAt), "total": 0.41, "level": "NORMAL"}]
    assert _tibber_format(forecast.startsAt) != forecast.startsAt

    assert match_observations(s, _now() + timedelta(hours=48)) == 1
    assert s.forecast.calibration_samples[0]["actual_total"] == 0.41


def test_match_nutzt_auch_yesterday_prices():
    s = State()
    forecast = _up(48)
    record_predictions(s, [forecast], _now())
    s.tibber.yesterday_prices = [{"startsAt": _tibber_format(forecast.startsAt), "total": 0.39, "level": "NORMAL"}]
    assert match_observations(s, _now() + timedelta(hours=72)) == 1


def test_record_dedupliziert_ueber_schreibweisen_hinweg():
    s = State()
    first = _up(48)
    record_predictions(s, [first], _now())
    same_hour_other_format = UnifiedPrice(
        startsAt=_tibber_format(first.startsAt), total=0.3, level="NORMAL",
        source="epex_predictor", confidence=0.5, raw_total=0.3,
    )
    assert record_predictions(s, [same_hour_other_format], _now()) == 0
