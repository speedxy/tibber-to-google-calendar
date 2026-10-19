"""Tests für die selbstlernende Umrechnung Prognose → Tibber-Preis."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tibber_charge.main import _map_forecast_to_tibber
from tibber_charge.prices.mapping import (
    MIN_OVERLAP,
    PriceMapping,
    apply_mapping,
    fit_mapping,
    hour_key,
    resolve_mapping,
)
from tibber_charge.prices.types import UnifiedPrice
from tibber_charge.state import State


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone(timedelta(hours=2)))


def _hours(n: int, start: datetime = NOW.replace(hour=0)) -> list[datetime]:
    return [start + timedelta(hours=i) for i in range(n)]


def _tibber(ts: datetime, total: float) -> dict:
    # Tibber-Schreibweise mit Millisekunden
    return {"startsAt": ts.strftime("%Y-%m-%dT%H:%M:%S.000%z")[:-2] + ":00", "total": total, "level": "NORMAL"}


def _fc(ts: datetime, raw: float, penalty: float = 0.0, source: str = "epex_predictor") -> UnifiedPrice:
    return UnifiedPrice(startsAt=ts.isoformat(), total=raw + penalty, level="NORMAL",
                        source=source, confidence=0.5, raw_total=raw)


def test_hour_key_ignoriert_schreibweise():
    assert hour_key("2026-10-19T00:00:00.000+02:00") == hour_key("2026-10-19T00:00:00+02:00")
    assert hour_key("2026-10-18T22:00:00+00:00") == hour_key("2026-10-19T00:00:00+02:00")
    assert hour_key("kaputt") is None


def test_fit_findet_tibber_formel_exakt():
    """Tibber = 19,9 ct + 1,19 × Börsenpreis (gemessen im Oktober 2026)."""
    hours = _hours(24)
    spots = [0.08 + 0.004 * i for i in range(24)]
    tibber = [_tibber(h, 0.199 + 1.19 * s) for h, s in zip(hours, spots)]
    m = fit_mapping(tibber, [_fc(h, s) for h, s in zip(hours, spots)], NOW)
    assert m is not None
    assert abs(m.a - 0.199) < 1e-9
    assert abs(m.b - 1.19) < 1e-9
    assert m.samples == 24
    assert m.source == "epex_predictor"


def test_fit_zu_wenige_vergleichsstunden():
    hours = _hours(MIN_OVERLAP - 1)
    m = fit_mapping([_tibber(h, 0.4) for h in hours], [_fc(h, 0.2) for h in hours], NOW)
    assert m is None


def test_fit_ohne_streuung_nur_niveau():
    hours = _hours(20)
    m = fit_mapping([_tibber(h, 0.42) for h in hours], [_fc(h, 0.20) for h in hours], NOW)
    assert m.b == 1.0
    assert abs(m.a - 0.22) < 1e-9


def test_fit_mit_absurdem_faktor_faellt_auf_niveau_zurueck():
    hours = _hours(20)
    raws = [0.20 + 0.0001 * i for i in range(20)]           # kaum Streuung
    tibber = [_tibber(h, 0.40 + 0.01 * i) for i, h in enumerate(hours)]  # starke Streuung → b ≈ 100
    m = fit_mapping(tibber, [_fc(h, r) for h, r in zip(hours, raws)], NOW)
    assert m.b == 1.0


def test_apply_behaelt_risikoaufschlag():
    m = PriceMapping(a=0.2, b=1.19, samples=24, source="epex_predictor", fitted_at=NOW.isoformat())
    [p] = apply_mapping([_fc(NOW, 0.10, penalty=0.02)], m)
    assert abs(p.raw_total - (0.2 + 0.119)) < 1e-9
    assert abs(p.total - (0.2 + 0.119 + 0.02)) < 1e-9


def test_resolve_nutzt_gespeicherte_nur_bei_gleicher_quelle():
    stored = PriceMapping(a=0.2, b=1.19, samples=24, source="epex_predictor", fitted_at="x")
    assert resolve_mapping(None, stored, "epex_predictor") is stored
    assert resolve_mapping(None, stored, "open_meteo") is None
    fitted = PriceMapping(a=0.1, b=1.0, samples=12, source="open_meteo", fitted_at="y")
    assert resolve_mapping(fitted, stored, "open_meteo") is fitted


def test_main_ohne_umrechnung_verwirft_boersenpreis_prognose():
    """Ohne gelernte Formel wäre ein Börsenpreis (~10 ct) gegenüber Tibber
    (~40 ct) viel zu billig. Dann lieber ohne Prognose planen."""
    state = State()
    result = _map_forecast_to_tibber(state, [], [_fc(NOW + timedelta(days=2), 0.10)], NOW)
    assert result == []


def test_main_ohne_umrechnung_laesst_heuristik_unveraendert():
    state = State()
    fc = [_fc(NOW + timedelta(days=2), 0.25, source="open_meteo")]
    assert _map_forecast_to_tibber(state, [], fc, NOW) == fc


def test_main_lernt_speichert_und_nutzt_spaeter_wieder():
    state = State()
    hours = _hours(24)
    spots = [0.08 + 0.004 * i for i in range(24)]
    tibber = [_tibber(h, 0.199 + 1.19 * s) for h, s in zip(hours, spots)]
    future = NOW + timedelta(days=3)
    _map_forecast_to_tibber(state, tibber, [_fc(h, s) for h, s in zip(hours, spots)], NOW)
    assert state.forecast.price_mapping["source"] == "epex_predictor"

    # Späterer Lauf ohne Überlappung: gespeicherte Formel greift.
    [p] = _map_forecast_to_tibber(state, [], [_fc(future, 0.10)], NOW)
    assert abs(p.total - (0.199 + 0.119)) < 1e-9
