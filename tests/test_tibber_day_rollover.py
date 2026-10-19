"""Tests für Tagewechsel-Verhalten im TibberCache.

Bug 2026-05-29: Bei Tagewechsel wurden die letzten Stunden des vorigen Tages
aus dem Cache entfernt, sodass Preisperioden, die über Mitternacht spannten
(z.B. CHEAP 22:00 day-1 bis 03:00 day), nach Mitternacht auf "ab 00:00" gekürzt
wurden. Außerdem blieb tomorrow_date stale, was zu Daten-Duplizität im _merge führte.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tibber_charge.state import TibberCache
from tibber_charge.tibber.cache import _merge, _store_prices


def _price(date_iso: str, hour: int, total: float = 0.20, level: str = "NORMAL") -> dict:
    return {
        "startsAt": f"{date_iso}T{hour:02d}:00:00+02:00",
        "total": total,
        "level": level,
    }


def _now(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone(timedelta(hours=2)))


def test_store_prices_rotiert_today_zu_yesterday_bei_tageswechsel():
    cache = TibberCache(
        today_date="2026-05-28",
        today_prices=[_price("2026-05-28", h) for h in range(24)],
        tomorrow_date="2026-05-29",
        tomorrow_prices=[_price("2026-05-29", h) for h in range(24)],
    )
    # Cron-Tick um 00:30 am 2026-05-29 → Tibber liefert nur today (= 2026-05-29)
    now = _now(2026, 5, 29, 0)
    new_data = [_price("2026-05-29", h) for h in range(24)]
    _store_prices(cache, new_data, now)

    # Alter today wird zu yesterday
    assert cache.yesterday_date == "2026-05-28"
    assert len(cache.yesterday_prices) == 24
    # Neuer today
    assert cache.today_date == "2026-05-29"
    assert len(cache.today_prices) == 24
    # Stale tomorrow gelöscht (war für 2026-05-29 = jetzt today)
    assert cache.tomorrow_date is None
    assert cache.tomorrow_prices == []


def test_store_prices_leert_yesterday_bei_grossem_zeitsprung():
    """Wenn cache.today_date >1 Tag alt ist (Skript war pausiert), yesterday-Cache wird leer."""
    cache = TibberCache(
        today_date="2026-05-20",  # 9 Tage alt
        today_prices=[_price("2026-05-20", h) for h in range(24)],
        tomorrow_date="2026-05-21",
        tomorrow_prices=[_price("2026-05-21", h) for h in range(24)],
    )
    now = _now(2026, 5, 29, 14)
    new_data = [_price("2026-05-29", h) for h in range(24)]
    _store_prices(cache, new_data, now)

    assert cache.yesterday_date is None  # Vorgestern (2026-05-20) ist zu alt
    assert cache.yesterday_prices == []
    assert cache.today_date == "2026-05-29"


def test_store_prices_leert_stale_yesterday():
    """Wenn cache.yesterday_date != tatsächlich gestern (z.B. 2 Tage alt), leeren."""
    cache = TibberCache(
        yesterday_date="2026-05-27",  # vorgestern
        yesterday_prices=[_price("2026-05-27", h) for h in range(24)],
        today_date="2026-05-29",  # heute (bereits korrekt)
        today_prices=[_price("2026-05-29", h) for h in range(24)],
    )
    now = _now(2026, 5, 29, 14)
    _store_prices(cache, [_price("2026-05-29", h) for h in range(24)], now)

    # 2026-05-27 ist nicht gestern (2026-05-28) → leeren
    assert cache.yesterday_date is None
    assert cache.yesterday_prices == []


def test_store_prices_kein_tageswechsel_unveraendert():
    """Im normalen Tageslauf passiert nichts mit yesterday/tomorrow."""
    yesterday_data = [_price("2026-05-28", h) for h in range(24)]
    today_data = [_price("2026-05-29", h) for h in range(24)]
    tomorrow_data = [_price("2026-05-30", h) for h in range(24)]
    cache = TibberCache(
        yesterday_date="2026-05-28", yesterday_prices=yesterday_data,
        today_date="2026-05-29", today_prices=today_data,
        tomorrow_date="2026-05-30", tomorrow_prices=tomorrow_data,
    )
    now = _now(2026, 5, 29, 15)
    _store_prices(cache, today_data + tomorrow_data, now)

    assert cache.yesterday_date == "2026-05-28"
    assert cache.today_date == "2026-05-29"
    assert cache.tomorrow_date == "2026-05-30"
    assert len(cache.yesterday_prices) == 24
    assert len(cache.today_prices) == 24
    assert len(cache.tomorrow_prices) == 24


def test_merge_gibt_alle_drei_tage_zurueck():
    cache = TibberCache(
        yesterday_date="2026-05-28",
        yesterday_prices=[_price("2026-05-28", h) for h in range(20, 24)],  # 22:00-23:00 etc.
        today_date="2026-05-29",
        today_prices=[_price("2026-05-29", h) for h in range(24)],
        tomorrow_date="2026-05-30",
        tomorrow_prices=[_price("2026-05-30", h) for h in range(24)],
    )
    merged = _merge(cache)
    # 4 (yesterday) + 24 (today) + 24 (tomorrow) = 52
    assert len(merged) == 52
    # Yesterday-Stunden sind dabei
    starts = [p["startsAt"] for p in merged]
    assert any("2026-05-28T20:00" in s for s in starts)
    assert any("2026-05-28T23:00" in s for s in starts)
    # Today-Stunden
    assert any("2026-05-29T00:00" in s for s in starts)
    assert any("2026-05-29T23:00" in s for s in starts)
    # Tomorrow-Stunden
    assert any("2026-05-30T00:00" in s for s in starts)


def test_merge_mit_leerem_yesterday():
    cache = TibberCache(
        today_date="2026-05-29",
        today_prices=[_price("2026-05-29", h) for h in range(24)],
    )
    merged = _merge(cache)
    assert len(merged) == 24


def test_szenario_preisperiode_ueberlebt_tageswechsel():
    """End-to-End: Eine CHEAP-Periode 22:00 day-1 bis 03:00 day überlebt
    den Tagewechsel und ist nach Mitternacht weiterhin als ein Block aus
    den merged Preisen ableitbar."""
    from tibber_charge.tibber.grouping import group_price_periods

    # Vor Tagewechsel: today=day-1 mit CHEAP ab 22:00, tomorrow=day mit CHEAP bis 03:00
    cache = TibberCache(
        today_date="2026-05-28",
        today_prices=[
            _price("2026-05-28", h, 0.30, "NORMAL") for h in range(0, 22)
        ] + [
            _price("2026-05-28", h, 0.15, "CHEAP") for h in range(22, 24)
        ],
        tomorrow_date="2026-05-29",
        tomorrow_prices=[
            _price("2026-05-29", h, 0.15, "CHEAP") for h in range(0, 3)
        ] + [
            _price("2026-05-29", h, 0.30, "NORMAL") for h in range(3, 24)
        ],
    )

    # Vor Tagewechsel: Periode spannt 22:00 day-1 bis 03:00 day
    merged_before = _merge(cache)
    periods_before = group_price_periods(merged_before)
    cheap_periods_before = periods_before["CHEAP"]
    assert len(cheap_periods_before) == 1
    start, end = cheap_periods_before[0]
    assert start.day == 28 and start.hour == 22
    assert end.day == 29 and end.hour == 3

    # Tagewechsel simulieren: 00:30 am 2026-05-29
    now = _now(2026, 5, 29, 0)
    new_data = [
        _price("2026-05-29", h, 0.15, "CHEAP") for h in range(0, 3)
    ] + [
        _price("2026-05-29", h, 0.30, "NORMAL") for h in range(3, 24)
    ]
    _store_prices(cache, new_data, now)

    # Nach Tagewechsel: dieselbe CHEAP-Periode sollte erhalten bleiben
    merged_after = _merge(cache)
    periods_after = group_price_periods(merged_after)
    cheap_periods_after = periods_after["CHEAP"]
    assert len(cheap_periods_after) == 1
    start, end = cheap_periods_after[0]
    assert start.day == 28 and start.hour == 22, f"CHEAP-Start sollte 28.05. 22:00 sein, war {start}"
    assert end.day == 29 and end.hour == 3, f"CHEAP-Ende sollte 29.05. 03:00 sein, war {end}"
