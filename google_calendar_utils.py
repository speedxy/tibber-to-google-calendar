import os
import logging
from datetime import datetime, timezone
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ====== 🔐 GOOGLE CALENDAR AUTH ======
def authenticate_google_calendar():
    """Authentifiziert sich bei der Google Calendar API und speichert das Token"""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json')

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                os.path.join(BASE_DIR, 'your-client-secret.json'), ['https://www.googleapis.com/auth/calendar.events']
            )
            creds = flow.run_local_server(port=0)

        with open('token.json', 'w') as token:
            token.write(creds.to_json())

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