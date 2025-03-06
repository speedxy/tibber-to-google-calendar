import os
import logging
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Pfade zu den Anmeldedaten
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")
CLIENT_SECRET_FILE = os.path.join(BASE_DIR, "your-client-secret.json")
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

def authenticate_google_calendar():
    """Authentifiziert sich bei der Google Calendar API und erneuert das Token, falls nötig."""
    creds = None

    # Prüfe, ob ein gespeichertes Token existiert
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    # Falls das Token nicht existiert oder nicht gültig ist, neue Authentifizierung
    if not creds or not creds.valid:
        try:
            if creds and creds.expired and creds.refresh_token:
                logging.info("🔄 Token ist abgelaufen – versuche zu erneuern...")
                creds.refresh(Request())
            else:
                logging.warning("⚠️ Kein gültiges Token gefunden – erneute Anmeldung erforderlich.")
                flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
                
                # Nutzung von run_console() für Server ohne GUI
                creds = flow.run_local_server(port=0, open_browser=False)

            # Speichere das neue Token
            with open(TOKEN_FILE, "w") as token:
                token.write(creds.to_json())
                logging.info("✅ Neues Token gespeichert!")

        except RefreshError:
            logging.error("❌ Token konnte nicht erneuert werden. Es wurde widerrufen oder ist abgelaufen.")
            logging.info("🔄 Lösche altes Token und fordere eine neue Anmeldung an...")
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)

            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            
            # Wieder run_console(), um im Terminal die Anmeldung zu machen
            creds = flow.run_local_server(port=0, open_browser=False)

            with open(TOKEN_FILE, "w") as token:
                token.write(creds.to_json())
                logging.info("✅ Neues Token gespeichert!")

    return creds

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