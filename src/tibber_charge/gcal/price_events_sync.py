"""Hash-Diffing-Sync für `#Tibber`- und `#Vorhersage`-Calendar-Events.

Pattern: Hash über die Eingabe-Preise bilden, mit gespeichertem Hash vergleichen,
bei Unterschied per Hashtag-Match (`delete_events_matching`) ALLE Events im
Fenster löschen und frisch anlegen. Bei Match: null API-Calls.

Warum Hashtag-Delete statt per-ID-Delete:
- Robust gegen fehlgeschlagene Deletes in vorherigen Läufen (Orphans werden
  beim nächsten Hash-Wechsel mit aufgeräumt).
- Robust gegen state.json-Verlust (keine IDs nötig).
- Robust gegen manuell duplizierte Events.
Trade-off: zwischen zwei Hash-Wechseln können externe Duplikate ~12 h überleben.
Hash wechselt typischerweise 2-3×/Tag (Tibber-Update + Tagewechsel).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

from dateutil import parser as date_parser
from google.oauth2.service_account import Credentials

from tibber_charge.gcal.events import (
    create_event,
    delete_events_matching,
)
from tibber_charge.prices.forecast.levels import apply_dynamic_levels
from tibber_charge.prices.types import UnifiedPrice
from tibber_charge.state import CalendarEventsCache
from tibber_charge.tibber.grouping import group_price_periods


# -------- Event-Builder --------

def build_tibber_event(
    level: str,
    period_prices: list[tuple[datetime, float]],
) -> tuple[str, str]:
    """Titel + Description eines #Tibber-Preisinformations-Events."""
    if period_prices:
        min_price = min(p[1] for p in period_prices)
        max_price = max(p[1] for p in period_prices)
        if min_price == max_price:
            price_range = f"{min_price:.1f}ct"
        else:
            price_range = f"{min_price:.1f}ct - {max_price:.1f}ct"

        if level == "NEGATIVE":
            title_prefix = "Negativer Strompreis"
        elif level in ("CHEAP", "VERY_CHEAP"):
            title_prefix = "Niedriger Strompreis"
        else:
            title_prefix = "Strompreis"
        description = "\n".join(
            f"{pt.strftime('%H:%M')}: {pp:.1f}ct" for pt, pp in period_prices
        )
    else:
        price_range = "Unbekannt"
        title_prefix = "Strompreis"
        description = "Keine Preisdaten verfügbar"

    title = f"⚡ {title_prefix}: {price_range} [{level}] #Tibber"
    return title, description


def build_forecast_event(
    level: str,
    period_prices: list[UnifiedPrice],
    source_name: str,
    generated_at: str | None,
) -> tuple[str, str]:
    """Titel + Description eines #Vorhersage-Events. Macht Unsicherheit explizit."""
    if not period_prices:
        return f"🔮 Vorhersage: Unbekannt [{level}] #Vorhersage", "Keine Forecast-Daten verfügbar"

    totals_ct = [round(p.total * 100, 1) for p in period_prices]
    min_ct, max_ct = min(totals_ct), max(totals_ct)
    price_range = f"{min_ct:.1f}ct" if min_ct == max_ct else f"{min_ct:.1f}ct - {max_ct:.1f}ct"

    if level == "NEGATIVE":
        prefix = "Negativer Strompreis (Schätzung)"
    elif level in ("CHEAP", "VERY_CHEAP"):
        prefix = "Niedriger Strompreis (Schätzung)"
    else:
        prefix = "Strompreis (Schätzung)"

    title = f"🔮 {prefix}: {price_range} [{level}] #Vorhersage"

    lines = [
        "⚠️ Schätzung — kein verlässlicher Strompreis!",
        f"Quelle: {source_name}",
    ]
    if generated_at:
        lines.append(f"Erzeugt: {generated_at}")
    confidences = [p.confidence for p in period_prices]
    lines.append(f"Konfidenz: {min(confidences):.2f} - {max(confidences):.2f}")
    lines.append("")
    lines.append("Stunden-Details:")
    for p in period_prices:
        try:
            ts = date_parser.isoparse(p.startsAt)
            time_str = ts.strftime("%d.%m. %H:%M")
        except (ValueError, TypeError):
            time_str = p.startsAt
        raw_ct = round((p.raw_total or p.total) * 100, 1)
        penalty_ct = round((p.total - (p.raw_total or p.total)) * 100, 1)
        if penalty_ct > 0:
            lines.append(f"  {time_str}: {p.total*100:.1f}ct  (Roh {raw_ct:.1f}ct + Risiko {penalty_ct:.1f}ct)")
        else:
            lines.append(f"  {time_str}: {p.total*100:.1f}ct")

    return title, "\n".join(lines)


# -------- Hash-Funktionen --------

def hash_tibber_prices(prices: list[dict]) -> str:
    """Hash über die Tibber-Eingabe — wenn identisch, sind die Output-Events identisch."""
    h = hashlib.md5()
    sorted_dicts = sorted(prices, key=lambda d: d.get("startsAt", ""))
    for d in sorted_dicts:
        starts = d.get("startsAt", "")
        total = round(float(d.get("total", 0.0)), 6)
        level = d.get("level", "")
        h.update(f"{starts}|{total}|{level}".encode())
    return h.hexdigest()


def hash_forecast_prices(prices: list[UnifiedPrice]) -> str:
    """Hash über Forecast-UnifiedPrices nach Anwendung von apply_dynamic_levels.

    Inkludiert level (das ist quantilbasiert und ändert sich, wenn sich die
    Distribution ändert) — sonst würden Forecast-Updates mit gleichen Werten
    aber neuen Levels den Cache nicht invalidieren.
    """
    h = hashlib.md5()
    sorted_prices = sorted(prices, key=lambda p: p.startsAt)
    for p in sorted_prices:
        total = round(float(p.total), 6)
        h.update(f"{p.startsAt}|{total}|{p.level}|{p.source}".encode())
    return h.hexdigest()


# -------- Sync: #Tibber --------

# Fenster für delete_events_matching bei #Tibber. Großzügig nach hinten gegriffen,
# damit auch über-Mitternacht-spannende Events und Server-TZ-Mismatches (Cron in UTC,
# Tibber-Daten in CEST → Event-Enden können vor `now - 1d` UTC liegen) sauber
# weggeräumt werden.
_TIBBER_DELETE_WINDOW_BACK = timedelta(days=2)
_TIBBER_DELETE_WINDOW_FWD = timedelta(days=4)
# Auch bei #Vorhersage: 1 Tag rückwärts fängt Forecasts auf, die heute Morgen
# noch aktuell waren, jetzt aber von Tibber überdeckt sind und sonst nicht mehr
# im Delete-Fenster lägen (Event-Ende < now).
_FORECAST_DELETE_WINDOW_BACK = timedelta(days=1)


def sync_tibber_events(
    calendar_id: str,
    creds: Credentials,
    cache: CalendarEventsCache,
    tibber_dicts: list[dict],
    now: datetime,
) -> CalendarEventsCache:
    """Schreibt/aktualisiert #Tibber-Events nur wenn sich die Tibber-Eingabe geändert hat.

    Bei Hash-Match: 0 Calendar-API-Calls. Bei Hash-Diff: löscht ALLE
    #Tibber-Events im Zeitfenster und legt frisch an — räumt damit auch
    angesammelte Duplikate aus Vor-Läufen mit auf.
    """
    if not tibber_dicts:
        return cache

    new_hash = hash_tibber_prices(tibber_dicts)
    if cache.events_hash == new_hash:
        logging.info(f"📋 Tibber-Events unverändert (hash={new_hash[:8]}); kein Sync nötig.")
        return cache

    logging.info(
        f"📋 Tibber-Hash geändert ({(cache.events_hash or 'neu')[:8]} → {new_hash[:8]}); "
        "lösche alle #Tibber-Events im Fenster und synchronisiere neu."
    )

    delete_events_matching(
        calendar_id, creds,
        now - _TIBBER_DELETE_WINDOW_BACK,
        now + _TIBBER_DELETE_WINDOW_FWD,
        "#Tibber",
    )

    # Neue Events erstellen
    categorized = group_price_periods(tibber_dicts)
    new_ids: list[str] = []
    for level, periods in categorized.items():
        for start_time, end_time in periods:
            period_prices = [
                (date_parser.isoparse(p["startsAt"]), round(p["total"] * 100, 1))
                for p in tibber_dicts
                if start_time <= date_parser.isoparse(p["startsAt"]) < end_time
            ]
            title, description = build_tibber_event(level, period_prices)
            reminders_minutes = [30, 0] if level == "NEGATIVE" else None
            event_id = create_event(
                start_time=start_time,
                end_time=end_time,
                summary=title,
                calendar_id=calendar_id,
                creds=creds,
                description=description,
                reminders_minutes=reminders_minutes,
            )
            if event_id:
                new_ids.append(event_id)

    logging.info(f"⚡ {len(new_ids)} #Tibber-Events synchronisiert.")
    return CalendarEventsCache(events_hash=new_hash, event_ids=new_ids)


# -------- Sync: #Vorhersage --------

def sync_forecast_events(
    calendar_id: str,
    creds: Credentials,
    cache: CalendarEventsCache,
    forecast_prices: list[UnifiedPrice],
    provider_name: str,
    generated_at: str | None,
    horizon_days: int,
    now: datetime,
    force_cleanup: bool = False,
) -> CalendarEventsCache:
    """Schreibt/aktualisiert #Vorhersage-Events nur bei Forecast-Veränderung.

    Bei Hash-Match: 0 Calendar-API-Calls. Bei Hash-Diff: löscht ALLE
    #Vorhersage-Events im Horizont und legt frisch an.

    Leerer `forecast_prices`:
    - Wenn der Cache vorher Events hatte (`events_hash` gesetzt) → räume auf.
    - Wenn `force_cleanup=True` → räume IMMER auf (auch ohne Cache-Hash).
      Wird gesetzt, wenn Forecast explizit per Config deaktiviert wurde —
      schützt gegen User, deren `state.json` keinen Hash mehr enthält (alte
      Code-Version, manueller Reset), aber noch alte `#Vorhersage`-Events im
      Kalender haben.
    - Sonst (temporär leer wegen Provider-Down) → kein Eingriff, bestehende
      Events bleiben stehen.
    """
    if not forecast_prices:
        # Bereits bestätigt leer + force_cleanup → kein wiederholter List-Call.
        if force_cleanup and cache.confirmed_empty:
            return cache
        if cache.events_hash is None and not force_cleanup:
            return cache
        reason = "Config-Schalter aus" if force_cleanup else "kein Forecast mehr"
        logging.info(f"🔮 Räume #Vorhersage-Events auf ({reason}).")
        deleted = delete_events_matching(
            calendar_id, creds,
            now - _FORECAST_DELETE_WINDOW_BACK,
            now + timedelta(days=horizon_days),
            "#Vorhersage",
        )
        # Nur als "definitiv leer" markieren, wenn wir auch wirklich nichts mehr
        # gefunden haben — sonst räumen wir beim nächsten Lauf weiter auf.
        return CalendarEventsCache(confirmed_empty=(deleted == 0))

    leveled = apply_dynamic_levels(forecast_prices)
    new_hash = hash_forecast_prices(leveled)
    if cache.events_hash == new_hash:
        logging.info(f"🔮 Vorhersage-Events unverändert (hash={new_hash[:8]}); kein Sync nötig.")
        return cache

    logging.info(
        f"🔮 Vorhersage-Hash geändert ({(cache.events_hash or 'neu')[:8]} → {new_hash[:8]}); "
        "lösche alle #Vorhersage-Events im Fenster und synchronisiere neu."
    )

    delete_events_matching(
        calendar_id, creds,
        now,
        now + timedelta(days=horizon_days),
        "#Vorhersage",
    )

    # Neu erstellen
    leveled_dicts = [p.to_dict() for p in leveled]
    periods = group_price_periods(leveled_dicts)
    new_ids: list[str] = []
    for level, level_periods in periods.items():
        for start_time, end_time in level_periods:
            period_unified = [
                p for p in leveled
                if start_time <= date_parser.isoparse(p.startsAt) < end_time
            ]
            title, description = build_forecast_event(level, period_unified, provider_name, generated_at)
            event_id = create_event(
                start_time=start_time,
                end_time=end_time,
                summary=title,
                calendar_id=calendar_id,
                creds=creds,
                description=description,
                reminders_minutes=None,
            )
            if event_id:
                new_ids.append(event_id)

    logging.info(f"🔮 {len(new_ids)} #Vorhersage-Events synchronisiert.")
    return CalendarEventsCache(events_hash=new_hash, event_ids=new_ids)
