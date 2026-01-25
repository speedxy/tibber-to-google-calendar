import requests
import os
import json
import logging
from datetime import datetime, timedelta, timezone
from dateutil import parser  # Für robustere ISO 8601-Datumsverarbeitung
from google_calendar_utils import authenticate_google_calendar, create_google_calendar_event, delete_existing_events

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 🔹 HIER LOG-LEVEL MANUELL ÄNDERN (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL = logging.WARNING  # Standard: WARNING (keine Cron-Mails). Setze auf DEBUG für mehr Logs.

# ====== 🔧 KONFIGURATION AUS `config.json` LADEN ======
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

if not os.path.exists(CONFIG_FILE):
    raise FileNotFoundError(f"❌ Konfigurationsdatei '{CONFIG_FILE}' nicht gefunden!")

with open(CONFIG_FILE, "r") as f:
    config = json.load(f)

TIBBER_API_KEY = config["TIBBER_API_KEY"]
GOOGLE_CALENDAR_ID = config["GOOGLE_CALENDAR_ID"]

# ====== 🛠️ LOGGING KONFIGURIEREN ======
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)  # Unterdrückt unwichtige Google-API-Warnungen

# ====== 📡 TIBBER API ======
def fetch_tibber_prices(api_key):
    """Holt die Strompreise von Tibber"""
    url = 'https://api.tibber.com/v1-beta/gql'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    query = """
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
    logging.info("📡 Sende Anfrage an Tibber API...")
    response = requests.post(url, headers=headers, json={'query': query})
    data = response.json()
    prices = data['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']
    logging.info(f"✅ Tibber API Antwort: {len(prices['today']) + len(prices.get('tomorrow', []))} Preisdaten gefunden.")
    
    # 📜 Empfangene Preisdaten auflisten
    for price in prices['today'] + prices.get('tomorrow', []):
        time = parser.isoparse(price['startsAt'])
        formatted_time = time.strftime('%d.%m.%Y %H:%M')
        logging.info(f"{formatted_time} - {formatted_time[:-5]}: {price['total']:.2f} € ({price['level']})")
    
    return prices['today'] + prices.get('tomorrow', [])

# ====== 📊 PREIS-PERIODEN GRUPPIEREN ======
def group_price_periods(prices):
    """Gruppiert Preisperioden anhand der Tibber-Kategorisierung"""
    categorized_periods = {
        "CHEAP": [], "VERY_CHEAP": [],
        "EXPENSIVE": [], "VERY_EXPENSIVE": []
    }
    current_periods = {key: None for key in categorized_periods}

    for price in prices:
        price_level = price['level']
        time = parser.isoparse(price['startsAt'])

        if price_level == "NORMAL":
            for level in current_periods.keys():
                if current_periods[level] is not None:
                    categorized_periods[level].append((current_periods[level], time))
                    current_periods[level] = None
            continue

        if price_level in categorized_periods:
            if current_periods[price_level] is None:
                current_periods[price_level] = time
            else:
                continue

        for level in current_periods.keys():
            if level != price_level and current_periods[level] is not None:
                categorized_periods[level].append((current_periods[level], time))
                current_periods[level] = None

    for level in current_periods.keys():
        if current_periods[level] is not None:
            categorized_periods[level].append((current_periods[level], time + timedelta(hours=1)))

    logging.info("📊 Erkannte Preisperioden:")
    for level, periods in categorized_periods.items():
        for start, end in periods:
            logging.info(f"  {level}: {start.strftime('%d.%m.%Y %H:%M')} - {end.strftime('%d.%m.%Y %H:%M')}")
    
    return categorized_periods

# ====== 🚀 HAUPTLAUF ======
def main():
    logging.info("📡 Tibber-Strompreise abrufen...")
    tibber_prices = fetch_tibber_prices(TIBBER_API_KEY)
    creds = authenticate_google_calendar()
    
    categorized_periods = group_price_periods(tibber_prices)

    # Bestimme die Zeit-Range der Tibber-Daten
    all_times = [parser.isoparse(p['startsAt']) for p in tibber_prices]
    if all_times:
        min_time = min(all_times)
        max_time = max(all_times) + timedelta(hours=1)  # Endzeit inkl. letzter Stunde
        start_str = min_time.strftime('%d.%m. %H:%M')
        end_str = max_time.strftime('%d.%m. %H:%M')

        logging.info(f"Lösche alle Tibber-Kalendereinträge im Zeitraum: {start_str} - {end_str}")
        delete_existing_events(GOOGLE_CALENDAR_ID, creds, min_time, max_time, '#Tibber')

    # Erstelle neue Termine für die Strompreisperioden
    for level, periods in categorized_periods.items():
        for start_time, end_time in periods:
            period_prices = [
                (parser.isoparse(p['startsAt']), round(p['total'] * 100, 1))  
                for p in tibber_prices if start_time <= parser.isoparse(p['startsAt']) < end_time
            ]

            if period_prices:
                min_price = min(p[1] for p in period_prices)
                max_price = max(p[1] for p in period_prices)

                if min_price == max_price:
                    price_range = f"{min_price:.1f}ct"
                else:
                    price_range = f"{min_price:.1f}ct - {max_price:.1f}ct"
                
                title_prefix = "Niedriger Strompreis" if level in ["CHEAP", "VERY_CHEAP"] else "Strompreis"

                description = "\n".join(f"{time.strftime('%H:%M')}: {price:.1f}ct" for time, price in period_prices)
            else:
                price_range = "Unbekannt"
                title_prefix = "Strompreis"
                description = "Keine Preisdaten verfügbar"

            event_title = f"⚡ {title_prefix}: {price_range} [{level}] #Tibber"
            create_google_calendar_event(start_time, end_time, event_title, GOOGLE_CALENDAR_ID, creds, description)

    logging.info("Skript abgeschlossen.")


if __name__ == '__main__':
    main()
