from __future__ import annotations

import logging
from datetime import datetime, timedelta

from dateutil import parser


PRICE_LEVELS = ("NEGATIVE", "CHEAP", "VERY_CHEAP", "EXPENSIVE", "VERY_EXPENSIVE")


def group_price_periods(prices: list[dict]) -> dict[str, list[tuple[datetime, datetime]]]:
    """Gruppiert Preisperioden anhand der Tibber-Kategorisierung.

    Stunden mit negativem Preis werden separat als NEGATIVE gruppiert und
    nicht zusätzlich in CHEAP/VERY_CHEAP einsortiert.
    """
    categorized_periods: dict[str, list[tuple[datetime, datetime]]] = {
        level: [] for level in PRICE_LEVELS
    }

    if not prices:
        return categorized_periods

    current_periods: dict[str, datetime | None] = {key: None for key in categorized_periods}
    last_time = None

    for price in prices:
        price_level = "NEGATIVE" if price["total"] < 0 else price["level"]
        price_time = parser.isoparse(price["startsAt"])
        last_time = price_time

        if price_level == "NORMAL":
            for level in current_periods:
                start = current_periods[level]
                if start is not None:
                    categorized_periods[level].append((start, price_time))
                    current_periods[level] = None
            continue

        if price_level in categorized_periods:
            if current_periods[price_level] is None:
                current_periods[price_level] = price_time
            else:
                continue

        for level in current_periods:
            start = current_periods[level]
            if level != price_level and start is not None:
                categorized_periods[level].append((start, price_time))
                current_periods[level] = None

    # Offene Perioden mit der letzten bekannten Stunde + 1h abschließen
    if last_time is not None:
        for level in current_periods:
            start = current_periods[level]
            if start is not None:
                categorized_periods[level].append((start, last_time + timedelta(hours=1)))

    logging.info("📊 Erkannte Preisperioden:")
    for level, periods in categorized_periods.items():
        for start, end in periods:
            logging.info(f"  {level}: {start.strftime('%d.%m.%Y %H:%M')} - {end.strftime('%d.%m.%Y %H:%M')}")

    return categorized_periods
