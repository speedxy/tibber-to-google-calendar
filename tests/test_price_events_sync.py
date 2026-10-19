"""Tests für price_events_sync — Hash-Diffing + Hashtag-Delete für #Tibber und #Vorhersage."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from tibber_charge.gcal.price_events_sync import (
    hash_forecast_prices,
    hash_tibber_prices,
    sync_forecast_events,
    sync_tibber_events,
)
from tibber_charge.prices.forecast.levels import apply_dynamic_levels
from tibber_charge.prices.types import UnifiedPrice
from tibber_charge.state import CalendarEventsCache


def _now() -> datetime:
    return datetime(2026, 5, 21, 22, 0, tzinfo=timezone.utc)


def _tibber(hour: int, total: float = 0.20, level: str = "NORMAL") -> dict:
    base = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    ts = base.replace(hour=hour) if hour < 24 else base.replace(day=base.day + 1, hour=hour - 24)
    return {"startsAt": ts.isoformat(), "total": total, "level": level}


def _forecast(hour: int, total: float = 0.22, source: str = "open_meteo") -> UnifiedPrice:
    base = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    ts = base.replace(hour=hour) if hour < 24 else base.replace(day=base.day + 1, hour=hour - 24)
    return UnifiedPrice(
        startsAt=ts.isoformat(), total=total, level="NORMAL",
        source=source, confidence=0.5, raw_total=0.20,
    )


# ---- Hash-Funktionen ----

def test_hash_tibber_identisch_bei_gleichen_inputs():
    a = [_tibber(10, 0.20, "NORMAL"), _tibber(11, 0.18, "CHEAP")]
    b = [_tibber(11, 0.18, "CHEAP"), _tibber(10, 0.20, "NORMAL")]  # andere Reihenfolge
    assert hash_tibber_prices(a) == hash_tibber_prices(b)


def test_hash_tibber_aendert_sich_bei_preis_diff():
    a = [_tibber(10, 0.20)]
    b = [_tibber(10, 0.21)]
    assert hash_tibber_prices(a) != hash_tibber_prices(b)


def test_hash_tibber_aendert_sich_bei_level_diff():
    a = [_tibber(10, 0.20, "NORMAL")]
    b = [_tibber(10, 0.20, "CHEAP")]
    assert hash_tibber_prices(a) != hash_tibber_prices(b)


def test_hash_forecast_identisch_bei_gleichen_inputs():
    a = apply_dynamic_levels([_forecast(10), _forecast(11)])
    b = apply_dynamic_levels([_forecast(11), _forecast(10)])
    assert hash_forecast_prices(a) == hash_forecast_prices(b)


def test_hash_forecast_aendert_sich_bei_preis_diff():
    a = apply_dynamic_levels([_forecast(10, 0.20)])
    b = apply_dynamic_levels([_forecast(10, 0.21)])
    assert hash_forecast_prices(a) != hash_forecast_prices(b)


# ---- Sync: Tibber ----

@patch("tibber_charge.gcal.price_events_sync.create_event")
@patch("tibber_charge.gcal.price_events_sync.delete_events_matching")
def test_sync_tibber_unveraenderter_hash_macht_keine_mutationen(delete_mock, create_mock):
    """Bei unverändertem Hash: null API-Calls (kein Delete, kein Create, keine Liste)."""
    prices = [_tibber(h, 0.20, "NORMAL") for h in range(5)]
    cache = CalendarEventsCache(events_hash=hash_tibber_prices(prices), event_ids=["a", "b"])

    new_cache = sync_tibber_events("cal", None, cache, prices, _now())

    assert create_mock.call_count == 0
    assert delete_mock.call_count == 0
    assert new_cache.event_ids == ["a", "b"]
    assert new_cache.events_hash == cache.events_hash


@patch("tibber_charge.gcal.price_events_sync.create_event", side_effect=["new_a", "new_b", "new_c"])
@patch("tibber_charge.gcal.price_events_sync.delete_events_matching")
def test_sync_tibber_hash_diff_loescht_per_hashtag_und_erstellt_neue(delete_mock, create_mock):
    prices = [
        _tibber(0, 0.10, "CHEAP"), _tibber(1, 0.10, "CHEAP"),
        _tibber(2, 0.30, "EXPENSIVE"), _tibber(3, 0.30, "EXPENSIVE"),
        _tibber(4, 0.10, "CHEAP"),
    ]
    cache = CalendarEventsCache(events_hash="old_hash", event_ids=["old1", "old2"])
    new_cache = sync_tibber_events("cal", None, cache, prices, _now())

    # Genau 1 delete-by-hashtag (statt mehrerer per-ID-Deletes)
    assert delete_mock.call_count == 1
    args = delete_mock.call_args.args
    assert args[4] == "#Tibber"
    # Fenster: now-1d bis now+3d
    assert args[2] < _now()
    assert args[3] > _now()
    # Mindestens ein Create
    assert create_mock.call_count >= 1
    assert new_cache.events_hash != "old_hash"
    assert len(new_cache.event_ids) >= 1


@patch("tibber_charge.gcal.price_events_sync.create_event")
@patch("tibber_charge.gcal.price_events_sync.delete_events_matching")
def test_sync_tibber_leere_eingabe_kein_call(delete_mock, create_mock):
    cache = CalendarEventsCache(events_hash="h", event_ids=["a"])
    new_cache = sync_tibber_events("cal", None, cache, [], _now())
    assert delete_mock.call_count == 0
    assert create_mock.call_count == 0
    assert new_cache is cache


# ---- Sync: Vorhersage ----

@patch("tibber_charge.gcal.price_events_sync.create_event")
@patch("tibber_charge.gcal.price_events_sync.delete_events_matching")
def test_sync_forecast_unveraenderter_hash_macht_keine_mutationen(delete_mock, create_mock):
    raw = [_forecast(h, 0.22) for h in range(5)]
    leveled = apply_dynamic_levels(raw)
    cache = CalendarEventsCache(events_hash=hash_forecast_prices(leveled), event_ids=["v1"])

    new_cache = sync_forecast_events(
        "cal", None, cache, raw,
        provider_name="open_meteo", generated_at=None, horizon_days=7, now=_now(),
    )

    assert create_mock.call_count == 0
    assert delete_mock.call_count == 0
    assert new_cache.event_ids == ["v1"]


@patch("tibber_charge.gcal.price_events_sync.create_event")
@patch("tibber_charge.gcal.price_events_sync.delete_events_matching")
def test_sync_forecast_hash_diff_loescht_per_hashtag_und_erstellt_neue(delete_mock, create_mock):
    create_mock.side_effect = lambda *a, **kw: f"new_{create_mock.call_count}"
    raw = [_forecast(h, 0.10 + h * 0.05) for h in range(5)]
    cache = CalendarEventsCache(events_hash="old", event_ids=["old_v1"])
    new_cache = sync_forecast_events(
        "cal", None, cache, raw,
        provider_name="open_meteo", generated_at=None, horizon_days=7, now=_now(),
    )
    assert delete_mock.call_count == 1
    args = delete_mock.call_args.args
    assert args[4] == "#Vorhersage"
    assert create_mock.call_count >= 1
    assert new_cache.events_hash != "old"


@patch("tibber_charge.gcal.price_events_sync.create_event")
@patch("tibber_charge.gcal.price_events_sync.delete_events_matching")
def test_sync_forecast_leere_eingabe_ohne_cache_macht_nichts(delete_mock, create_mock):
    """Wenn forecast_only leer UND Cache leer ist: nichts zu tun."""
    cache = CalendarEventsCache()
    new_cache = sync_forecast_events(
        "cal", None, cache, [],
        provider_name="open_meteo", generated_at=None, horizon_days=7, now=_now(),
    )
    assert delete_mock.call_count == 0
    assert create_mock.call_count == 0
    assert new_cache is cache


@patch("tibber_charge.gcal.price_events_sync.create_event")
@patch("tibber_charge.gcal.price_events_sync.delete_events_matching", return_value=2)
def test_sync_forecast_leere_eingabe_aber_alter_cache_raeumt_auf(delete_mock, create_mock):
    """Wenn forecast_only leer UND Cache vorher Events hatte: löschen + Cache reset.

    Sonst blieben #Vorhersage-Events für immer im Kalender hängen, wenn
    Tibber den gesamten Horizont abdeckt oder der Forecast-Provider länger
    ausfällt.
    """
    cache = CalendarEventsCache(events_hash="h", event_ids=["v"])
    new_cache = sync_forecast_events(
        "cal", None, cache, [],
        provider_name="open_meteo", generated_at=None, horizon_days=7, now=_now(),
    )
    assert delete_mock.call_count == 1
    args = delete_mock.call_args.args
    assert args[4] == "#Vorhersage"
    assert create_mock.call_count == 0
    # Cache wurde zurückgesetzt
    assert new_cache.events_hash is None
    assert new_cache.event_ids == []
    # delete fand 2 Events → noch nicht bestätigt leer
    assert new_cache.confirmed_empty is False


@patch("tibber_charge.gcal.price_events_sync.create_event")
@patch("tibber_charge.gcal.price_events_sync.delete_events_matching", return_value=3)
def test_sync_forecast_force_cleanup_loescht_auch_ohne_cache_hash(delete_mock, create_mock):
    """User hat Forecast in Config ausgeschaltet, aber state.json kennt keine
    #Vorhersage-IDs (alte Code-Version oder manueller Reset). Mit
    force_cleanup=True wird trotzdem ein Hashtag-Delete abgesetzt, damit
    alte Events nicht ewig im Kalender hängen bleiben.
    """
    cache = CalendarEventsCache()  # leer
    new_cache = sync_forecast_events(
        "cal", None, cache, [],
        provider_name="open_meteo", generated_at=None, horizon_days=7, now=_now(),
        force_cleanup=True,
    )
    assert delete_mock.call_count == 1
    assert create_mock.call_count == 0
    # 3 deleted → noch nicht bestätigt leer
    assert new_cache.confirmed_empty is False


@patch("tibber_charge.gcal.price_events_sync.create_event")
@patch("tibber_charge.gcal.price_events_sync.delete_events_matching", return_value=0)
def test_sync_forecast_force_cleanup_setzt_confirmed_empty_bei_0_deletes(delete_mock, create_mock):
    """Nach einem erzwungenen Cleanup mit 0 Treffern: confirmed_empty=True."""
    cache = CalendarEventsCache()
    new_cache = sync_forecast_events(
        "cal", None, cache, [],
        provider_name="open_meteo", generated_at=None, horizon_days=7, now=_now(),
        force_cleanup=True,
    )
    assert delete_mock.call_count == 1
    assert new_cache.confirmed_empty is True


@patch("tibber_charge.gcal.price_events_sync.create_event")
@patch("tibber_charge.gcal.price_events_sync.delete_events_matching")
def test_sync_forecast_force_cleanup_skip_wenn_bereits_confirmed_empty(delete_mock, create_mock):
    """confirmed_empty=True + leere Eingabe → kein erneuter List/Delete-Call.

    Verhindert, dass alle 15 min unnötig die Calendar-Liste abgefragt wird,
    wenn Forecast dauerhaft aus ist und kein Event mehr existiert.
    """
    cache = CalendarEventsCache(confirmed_empty=True)
    new_cache = sync_forecast_events(
        "cal", None, cache, [],
        provider_name="open_meteo", generated_at=None, horizon_days=7, now=_now(),
        force_cleanup=True,
    )
    assert delete_mock.call_count == 0
    assert create_mock.call_count == 0
    assert new_cache is cache
