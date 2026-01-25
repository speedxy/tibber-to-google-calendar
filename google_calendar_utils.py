import os
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Pfade zu den Anmeldedaten
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "service_account.json")
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

def authenticate_google_calendar():
    """
    Authentifiziert sich über einen Service Account.
    Ersetzt den alten interaktiven OAuth-Flow.
    """
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logging.error(f"❌ Service-Account-Datei nicht gefunden: {SERVICE_ACCOUNT_FILE}")
        return None

    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, 
            scopes=SCOPES
        )
        return creds
    except Exception as e:
        logging.error(f"❌ Fehler bei der Service-Account-Authentifizierung: {e}")
        return None

# ====== 📅 KALENDER-EVENTS ERSTELLEN ======
def create_google_calendar_event(start_time, end_time, summary, calendar_id, creds, description=""):
    """Erstellt einen Google-Kalendereintrag"""
    try:
        service = build('calendar', 'v3', credentials=creds)
        event = {
            'summary': summary,
            'description': description,
            'start': {'dateTime': start_time.isoformat(), 'timeZone': 'UTC'},
            'end': {'dateTime': end_time.isoformat(), 'timeZone': 'UTC'},
        }
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        
        # Formatierung des Log-Eintrags mit "DD.MM. HH:MM"
        start_str = start_time.strftime('%d.%m. %H:%M')
        end_str = end_time.strftime('%d.%m. %H:%M')
        logging.info(f'✅ Erstellt: {summary} ({start_str} - {end_str})')

    except HttpError as error:
        logging.error(f'❌ Fehler beim Erstellen von Events: {error}')

# ====== 🗑️ KALENDER-EVENTS LÖSCHEN ======
def delete_existing_events(calendar_id, creds, start_time, end_time, search_string):
    """Löscht bestehende Kalendereinträge innerhalb des gegebenen Zeitraums, die den Such-String enthalten"""
    try:
        service = build('calendar', 'v3', credentials=creds)
        events = service.events().list(
            calendarId=calendar_id,
            timeMin=start_time.isoformat(),
            timeMax=end_time.isoformat(),
            singleEvents=True
        ).execute()
        for event in events.get('items', []):
            if search_string in event.get('summary', ''):
                service.events().delete(calendarId=calendar_id, eventId=event['id']).execute()
                logging.info(f'🗑️ Gelöscht: {event["summary"]}')
    except HttpError as error:
        logging.error(f'❌ Fehler beim Löschen von Events: {error}')