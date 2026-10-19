"""Smart-Polling: entscheidet, ob Tibber wirklich abgefragt werden muss,
und behandelt Backoff bei 504/Netzfehlern.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests
from dateutil import parser as date_parser

from tibber_charge.state import State, TibberCache
from tibber_charge.tibber.client import fetch_tibber_prices


_BACKOFF_INITIAL_SECONDS = 15 * 60   # 15 min
_BACKOFF_MAX_SECONDS = 60 * 60       # 1 h
_TOMORROW_AVAILABLE_FROM_HOUR = 13   # lokal: Tibber publiziert Day-Ahead ~13:00
_TOMORROW_GIVE_UP_HOUR = 23          # ab dieser Stunde keine weiteren Versuche heute


def _yesterday_iso(now: datetime) -> str:
    return (now.date() - timedelta(days=1)).isoformat()


def _today_iso(now: datetime) -> str:
    return now.date().isoformat()


def _tomorrow_iso(now: datetime) -> str:
    return (now.date() + timedelta(days=1)).isoformat()


def should_fetch(state: State, now: datetime) -> tuple[bool, str]:
    """Entscheidet, ob Tibber jetzt abgefragt werden soll. Liefert (bool, Grund)."""
    today = _today_iso(now)
    tomorrow = _tomorrow_iso(now)
    cache = state.tibber

    if cache.today_date != today:
        return True, "today_fehlt_oder_veraltet"

    if cache.tomorrow_date == tomorrow:
        return False, "tomorrow_im_cache"

    if now.hour < _TOMORROW_AVAILABLE_FROM_HOUR:
        return False, f"vor_{_TOMORROW_AVAILABLE_FROM_HOUR}_uhr"

    if cache.tomorrow_given_up_for_date == today:
        return False, "tomorrow_heute_aufgegeben"

    if cache.next_retry_after:
        try:
            retry_at = date_parser.isoparse(cache.next_retry_after)
            if now < retry_at:
                return False, f"backoff_bis_{cache.next_retry_after}"
        except (ValueError, TypeError):
            pass

    return True, "tomorrow_holen"


def fetch_with_cache(state: State, api_key: str, now: datetime) -> list[dict]:
    """Holt Tibber-Preise wenn nötig, gibt die kombinierte today+tomorrow-Liste aus dem Cache zurück."""
    cache = state.tibber
    do_fetch, reason = should_fetch(state, now)
    logging.info(f"📋 Tibber-Polling: fetch={do_fetch} ({reason})")

    if do_fetch:
        cache.last_fetch_at = now.isoformat()
        try:
            all_prices = fetch_tibber_prices(api_key)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "??"
            cache.last_error = f"HTTP {status}: {e}"
            _apply_backoff(cache, now)
            logging.warning(f"⚠️ Tibber-Fetch fehlgeschlagen ({status}); nächster Versuch ab {cache.next_retry_after}")
            _check_give_up(cache, now)
            return _merge(cache)
        except requests.RequestException as e:
            cache.last_error = f"NETWORK: {e}"
            _apply_backoff(cache, now)
            logging.warning(f"⚠️ Tibber-Fetch fehlgeschlagen (Netz: {e}); nächster Versuch ab {cache.next_retry_after}")
            _check_give_up(cache, now)
            return _merge(cache)
        except RuntimeError as e:
            cache.last_error = f"PARSE: {e}"
            logging.warning(f"⚠️ Tibber-Antwort unbrauchbar ({e}); nutze Cache.")
            return _merge(cache)

        cache.last_success_at = now.isoformat()
        cache.last_error = None
        cache.next_retry_after = None
        cache.backoff_seconds = 0
        _store_prices(cache, all_prices, now)

    _check_give_up(cache, now)
    return _merge(cache)


def _apply_backoff(cache: TibberCache, now: datetime) -> None:
    next_seconds = cache.backoff_seconds * 2 if cache.backoff_seconds > 0 else _BACKOFF_INITIAL_SECONDS
    next_seconds = min(next_seconds, _BACKOFF_MAX_SECONDS)
    cache.backoff_seconds = next_seconds
    cache.next_retry_after = (now + timedelta(seconds=next_seconds)).isoformat()


def _check_give_up(cache: TibberCache, now: datetime) -> None:
    today = _today_iso(now)
    tomorrow = _tomorrow_iso(now)
    if (
        cache.tomorrow_date != tomorrow
        and now.hour >= _TOMORROW_GIVE_UP_HOUR
        and cache.tomorrow_given_up_for_date != today
    ):
        cache.tomorrow_given_up_for_date = today
        logging.warning(f"⚠️ Tomorrow-Preise für heute aufgegeben (>={_TOMORROW_GIVE_UP_HOUR}:00 Uhr).")


def _store_prices(cache: TibberCache, all_prices: list[dict], now: datetime) -> None:
    yesterday_iso = _yesterday_iso(now)
    today_iso = _today_iso(now)
    tomorrow_iso = _tomorrow_iso(now)

    # Tagewechsel-Erkennung: wenn cache.today_date != heute, ist der Cache abgelaufen.
    # Wenn der alte today_date GENAU gestern war, rotieren wir die Daten nach yesterday.
    # Wenn er älter ist (Skript lief länger nicht), yesterday wird sicher geleert.
    if cache.today_date and cache.today_date != today_iso:
        if cache.today_date == yesterday_iso:
            cache.yesterday_date = cache.today_date
            cache.yesterday_prices = cache.today_prices
            logging.info(f"📅 Tagewechsel — alte today-Preise nach yesterday rotiert ({yesterday_iso}).")
        else:
            logging.info(
                f"📅 Cache-today_date {cache.today_date} ist älter als gestern ({yesterday_iso}) — "
                "yesterday-Cache wird geleert."
            )
            cache.yesterday_date = None
            cache.yesterday_prices = []
        # Stale tomorrow ist beim Tagewechsel jetzt = today oder älter → leeren,
        # bis frische Daten nach 13:00 reinkommen.
        cache.tomorrow_date = None
        cache.tomorrow_prices = []

    # Yesterday auch unabhängig bereinigen, falls er älter als gestern ist
    # (z.B. Skript lief >2 Tage nicht)
    if cache.yesterday_date and cache.yesterday_date != yesterday_iso:
        cache.yesterday_date = None
        cache.yesterday_prices = []

    today_prices: list[dict] = []
    tomorrow_prices: list[dict] = []
    other_count = 0

    for price in all_prices:
        try:
            ts = date_parser.isoparse(price["startsAt"])
        except (KeyError, ValueError):
            continue
        date_iso = ts.date().isoformat()
        if date_iso == today_iso:
            today_prices.append(price)
        elif date_iso == tomorrow_iso:
            tomorrow_prices.append(price)
        else:
            other_count += 1

    if today_prices:
        cache.today_date = today_iso
        cache.today_prices = today_prices
    if tomorrow_prices:
        cache.tomorrow_date = tomorrow_iso
        cache.tomorrow_prices = tomorrow_prices
    if other_count:
        logging.info(f"ℹ️ {other_count} Preise außerhalb today/tomorrow ignoriert.")


def _merge(cache: TibberCache) -> list[dict]:
    """Liefert yesterday + today + tomorrow. Duplikate werden im aufrufenden
    Code (unified_prices) per Stundenschlüssel dedupliziert.
    """
    return (
        list(cache.yesterday_prices or [])
        + list(cache.today_prices or [])
        + list(cache.tomorrow_prices or [])
    )
