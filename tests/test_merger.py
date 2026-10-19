"""Tests für unified_prices() — Sortierung, Tibber-wins, Lücken, Horizont."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from tibber_charge.prices.merger import unified_prices
from tibber_charge.prices.types import UnifiedPrice


# Hilfs-Konstruktoren
def _tibber(hour_iso: str, total: float = 0.20, level: str = "NORMAL") -> dict:
    return {"startsAt": hour_iso, "total": total, "level": level}


def _forecast(hour_iso: str, total: float = 0.25, level: str = "NORMAL") -> UnifiedPrice:
    return UnifiedPrice(
        startsAt=hour_iso, total=total, level=level,
        source="corrently", confidence=0.5,
    )


def _utc(year, month, day, hour=0):
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def test_leerer_input_liefert_leere_liste():
    now = _utc(2026, 5, 20, 14)
    assert unified_prices([], [], now) == []


def test_nur_tibber_input():
    now = _utc(2026, 5, 20, 14)
    result = unified_prices(
        [_tibber("2026-05-20T15:00:00+00:00", 0.21, "CHEAP")],
        [],
        now,
    )
    assert len(result) == 1
    assert result[0].source == "tibber"
    assert result[0].confidence == 1.0
    assert result[0].total == 0.21


def test_nur_forecast_input():
    now = _utc(2026, 5, 20, 14)
    result = unified_prices(
        [],
        [_forecast("2026-05-23T10:00:00+00:00", 0.18)],
        now,
    )
    assert len(result) == 1
    assert result[0].source == "corrently"


def test_tibber_gewinnt_bei_overlap():
    """Wenn Tibber und Forecast dieselbe Stunde haben, gewinnt Tibber."""
    now = _utc(2026, 5, 20, 14)
    hour = "2026-05-20T15:00:00+00:00"
    result = unified_prices(
        [_tibber(hour, 0.21, "CHEAP")],
        [_forecast(hour, 0.99)],
        now,
    )
    assert len(result) == 1
    assert result[0].source == "tibber"
    assert result[0].total == 0.21


def test_strikt_aufsteigend_sortiert():
    now = _utc(2026, 5, 20, 14)
    # Bewusst unsortiert eingegeben
    result = unified_prices(
        [
            _tibber("2026-05-20T17:00:00+00:00"),
            _tibber("2026-05-20T15:00:00+00:00"),
            _tibber("2026-05-20T16:00:00+00:00"),
        ],
        [_forecast("2026-05-23T10:00:00+00:00")],
        now,
    )
    starts = [p.startsAt for p in result]
    assert starts == sorted(starts)


def test_tibber_vergangenheit_wird_behalten():
    """Tibber-Vergangenheit bleibt drin — der Kalender-Sync zeigt sie ebenfalls."""
    now = _utc(2026, 5, 20, 14)
    result = unified_prices(
        [
            _tibber("2026-05-20T10:00:00+00:00"),  # vor now
            _tibber("2026-05-20T15:00:00+00:00"),  # nach now
        ],
        [],
        now,
    )
    assert len(result) == 2


def test_forecast_vergangenheit_wird_verworfen():
    """Forecast-Vergangenheit ist sinnlos und wird verworfen."""
    now = _utc(2026, 5, 20, 14)
    result = unified_prices(
        [],
        [
            _forecast("2026-05-20T10:00:00+00:00"),  # vor now
            _forecast("2026-05-23T10:00:00+00:00"),  # nach now
        ],
        now,
    )
    assert len(result) == 1
    assert result[0].startsAt == "2026-05-23T10:00:00+00:00"


def test_horizon_wird_eingehalten():
    now = _utc(2026, 5, 20, 14)
    result = unified_prices(
        [_tibber("2026-05-21T15:00:00+00:00")],
        [
            _forecast("2026-05-25T10:00:00+00:00"),  # < 7d
            _forecast("2026-06-01T10:00:00+00:00"),  # > 7d
        ],
        now,
        horizon=timedelta(days=7),
    )
    starts = [p.startsAt for p in result]
    assert "2026-05-25T10:00:00+00:00" in starts
    assert "2026-06-01T10:00:00+00:00" not in starts


def test_ungueltiges_tibber_dict_wird_geloggt_und_geskippt(caplog):
    now = _utc(2026, 5, 20, 14)
    with caplog.at_level(logging.WARNING):
        result = unified_prices(
            [
                {"startsAt": "2026-05-20T15:00:00+00:00", "total": 0.21, "level": "CHEAP"},
                {"kein_startsAt": True},
            ],
            [],
            now,
        )
    assert len(result) == 1
    assert any("Tibber-Eintrag" in rec.message for rec in caplog.records)


def test_luecke_wird_geloggt_aber_nicht_synthetisiert(caplog):
    now = _utc(2026, 5, 20, 14)
    with caplog.at_level(logging.INFO):
        result = unified_prices(
            [
                _tibber("2026-05-20T15:00:00+00:00"),
                _tibber("2026-05-20T19:00:00+00:00"),  # 4h Lücke
            ],
            [],
            now,
        )
    # Lücke nicht gefüllt — exakt 2 Einträge
    assert len(result) == 2
    assert any("Lücke" in rec.message for rec in caplog.records)


def test_forecast_in_tibber_range_wird_verworfen(caplog):
    """Forecast-Einträge für Stunden, die Tibber liefert, werden ignoriert."""
    now = _utc(2026, 5, 20, 14)
    with caplog.at_level(logging.INFO):
        result = unified_prices(
            [
                _tibber("2026-05-20T15:00:00+00:00"),
                _tibber("2026-05-20T16:00:00+00:00"),
            ],
            [
                _forecast("2026-05-20T15:00:00+00:00"),  # ←  konkurriert mit Tibber
                _forecast("2026-05-23T10:00:00+00:00"),  # ← außerhalb Tibber-Range
            ],
            now,
        )
    assert len(result) == 3
    sources = {p.startsAt: p.source for p in result}
    assert sources["2026-05-20T15:00:00+00:00"] == "tibber"
    assert sources["2026-05-23T10:00:00+00:00"] == "corrently"
    assert any("Tibber-Range verworfen" in rec.message for rec in caplog.records)


def test_to_dict_kompatibel_mit_grouping():
    """Smoke: das, was der Merger ausspuckt, muss group_price_periods akzeptieren."""
    from tibber_charge.tibber.grouping import group_price_periods

    now = _utc(2026, 5, 20, 14)
    merged = unified_prices(
        [_tibber("2026-05-20T15:00:00+00:00", 0.05, "CHEAP")],
        [_forecast("2026-05-23T10:00:00+00:00", 0.30, "EXPENSIVE")],
        now,
    )
    dicts = [p.to_dict() for p in merged]
    periods = group_price_periods(dicts)
    # Sollte ohne KeyError durchlaufen und CHEAP/EXPENSIVE-Buckets populieren
    assert "CHEAP" in periods
    assert "EXPENSIVE" in periods

