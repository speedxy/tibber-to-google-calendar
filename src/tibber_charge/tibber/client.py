from __future__ import annotations

import logging

import requests
from dateutil import parser


TIBBER_API_URL = "https://api.tibber.com/v1-beta/gql"

_PRICE_QUERY = """
{
    viewer {
        homes {
            currentSubscription {
                priceInfo {
                    today {
                        total
                        startsAt
                        level
                    }
                    tomorrow {
                        total
                        startsAt
                        level
                    }
                }
            }
        }
    }
}
"""


def fetch_tibber_prices(api_key: str) -> list[dict]:
    """Holt die Strompreise von Tibber"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    logging.info("📡 Sende Anfrage an Tibber API...")
    response = requests.post(TIBBER_API_URL, headers=headers, json={"query": _PRICE_QUERY})
    response.raise_for_status()

    data = response.json()
    if "errors" in data:
        raise RuntimeError(f"❌ Tibber API Fehler: {data['errors']}")

    try:
        prices = data["data"]["viewer"]["homes"][0]["currentSubscription"]["priceInfo"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"❌ Unerwartete API-Antwortstruktur: {e}") from e

    today_prices = prices.get("today") or []
    tomorrow_prices = prices.get("tomorrow") or []
    all_prices = today_prices + tomorrow_prices

    logging.info(f"✅ Tibber API Antwort: {len(all_prices)} Preisdaten gefunden.")

    for price in all_prices:
        price_time = parser.isoparse(price["startsAt"])
        formatted_time = price_time.strftime("%d.%m.%Y %H:%M")
        logging.info(f"{formatted_time}: {price['total']:.2f} € ({price['level']})")

    return all_prices
