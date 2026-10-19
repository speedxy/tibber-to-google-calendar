"""Merger für Tibber (cent-genau) + Forecast (geschätzt) zu einem unified Preisvektor."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from dateutil import parser as date_parser

from tibber_charge.prices.types import UnifiedPrice



def unified_prices(
    tibber_prices: list[dict],
    forecast_prices: list[UnifiedPrice],
    now: datetime,
    horizon: timedelta = timedelta(days=7),
) -> list[UnifiedPrice]:
    """Mergt Tibber-Dicts und Forecast-UnifiedPrices zu einer geordneten Liste.

    Garantien:
    - Strikt aufsteigend nach startsAt sortiert.
    - Pro Stunde maximal ein Eintrag.
    - Tibber gewinnt bei Stunden-Overlap (höhere Konfidenz).
    - Lücken werden geloggt, NICHT synthetisiert.
    - Stunden außerhalb [now, now+horizon] werden verworfen.
    """
    horizon_end = now + horizon

    # Tibber-Dicts → UnifiedPrice. Past-Stunden NICHT filtern — der bisherige
    # Calendar-Sync zeigt sie ebenfalls, also Verhalten beibehalten.
    tibber_by_hour: dict[str, UnifiedPrice] = {}
    for price_dict in tibber_prices:
        try:
            up = UnifiedPrice.from_tibber_dict(price_dict)
            ts = date_parser.isoparse(up.startsAt)
        except (KeyError, ValueError, TypeError) as e:
            logging.warning(f"⚠️ Überspringe ungültigen Tibber-Eintrag ({e}): {price_dict!r}")
            continue
        if ts > horizon_end:
            continue
        tibber_by_hour[_hour_key(ts)] = up

    # Forecast-Einträge nur dort, wo Tibber nichts hat.
    # Past-Stunden bei Forecast verwerfen — sie hätten ohnehin keine Bedeutung.
    accepted_forecast: dict[str, UnifiedPrice] = {}
    dropped_in_tibber_range = 0
    past_cutoff = now.replace(minute=0, second=0, microsecond=0)
    for up in forecast_prices:
        try:
            ts = date_parser.isoparse(up.startsAt)
        except (ValueError, TypeError) as e:
            logging.warning(f"⚠️ Überspringe Forecast mit ungültigem startsAt ({e}): {up}")
            continue
        if ts < past_cutoff:
            continue
        if ts > horizon_end:
            continue
        key = _hour_key(ts)
        if key in tibber_by_hour:
            dropped_in_tibber_range += 1
            continue
        accepted_forecast[key] = up

    if dropped_in_tibber_range:
        logging.info(
            f"ℹ️ {dropped_in_tibber_range} Forecast-Stunden im Tibber-Range verworfen "
            f"(Tibber gewinnt)."
        )

    merged = list(tibber_by_hour.values()) + list(accepted_forecast.values())
    merged.sort(key=lambda p: p.startsAt)

    _warn_on_gaps(merged)
    return merged


def _hour_key(ts: datetime) -> str:
    """Schlüssel auf Stundenebene — als UTC-ISO, damit Offsets nicht stören."""
    return ts.astimezone().replace(minute=0, second=0, microsecond=0).isoformat()


def _warn_on_gaps(prices: list[UnifiedPrice]) -> None:
    """Loggt Lücken im stündlichen Raster, ohne sie zu füllen."""
    if len(prices) < 2:
        return
    for prev, curr in zip(prices, prices[1:]):
        prev_ts = date_parser.isoparse(prev.startsAt)
        curr_ts = date_parser.isoparse(curr.startsAt)
        delta = curr_ts - prev_ts
        if delta > timedelta(hours=1, minutes=5):  # 5 min Toleranz für DST-Wechsel
            logging.info(
                f"ℹ️ Lücke im Preisvektor: {prev.startsAt} → {curr.startsAt} "
                f"({delta.total_seconds()/3600:.1f}h)"
            )
