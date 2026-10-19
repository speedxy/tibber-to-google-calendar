from __future__ import annotations

import logging
from datetime import datetime

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def create_event(
    start_time: datetime,
    end_time: datetime,
    summary: str,
    calendar_id: str,
    creds: Credentials,
    description: str = "",
    reminders_minutes: list[int] | None = None,
) -> str | None:
    """Erstellt einen Google-Kalendereintrag und gibt die Event-ID zurück.

    Standardmäßig werden keine Erinnerungen gesetzt (auch keine Kalender-
    Defaults). Über `reminders_minutes` können Popup-Erinnerungen in
    Minuten vor dem Start angegeben werden.

    Returnt die Event-ID bei Erfolg, sonst None.
    """
    try:
        service = build("calendar", "v3", credentials=creds)
        overrides = [
            {"method": "popup", "minutes": minutes}
            for minutes in (reminders_minutes or [])
        ]
        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_time.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end_time.isoformat(), "timeZone": "UTC"},
            "reminders": {"useDefault": False, "overrides": overrides},
        }
        result = service.events().insert(calendarId=calendar_id, body=event).execute()

        start_str = start_time.strftime("%d.%m. %H:%M")
        end_str = end_time.strftime("%d.%m. %H:%M")
        logging.info(f"✅ Erstellt: {summary} ({start_str} - {end_str})")
        return result.get("id")

    except HttpError as error:
        logging.error(f"❌ Fehler beim Erstellen von Events: {error}")
        return None


def _list_all_events(
    service,
    calendar_id: str,
    start_time: datetime,
    end_time: datetime,
) -> list[dict]:
    """Listet ALLE Events im Zeitraum, mit Pagination via nextPageToken.

    Google-API liefert default 250 Items/Page. Bei mehr Events ohne Pagination
    werden Folgeseiten ignoriert → Duplikate akkumulieren über Zeit.
    """
    items: list[dict] = []
    page_token: str | None = None
    while True:
        request_kwargs = {
            "calendarId": calendar_id,
            "timeMin": start_time.isoformat(),
            "timeMax": end_time.isoformat(),
            "singleEvents": True,
            "maxResults": 2500,  # API-Maximum
        }
        if page_token:
            request_kwargs["pageToken"] = page_token
        response = service.events().list(**request_kwargs).execute()
        items.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def delete_events_matching(
    calendar_id: str,
    creds: Credentials,
    start_time: datetime,
    end_time: datetime,
    search_string: str,
) -> int:
    """Löscht alle Events im Zeitraum, deren Summary `search_string` enthält.

    Liefert die Anzahl erfolgreicher Deletes. Per-Event try/except sorgt
    dafür, dass ein einzelner fehlgeschlagener Delete den restlichen Loop
    NICHT abbricht — vorher führte ein HttpError beim ersten Event dazu,
    dass alle nachfolgenden Duplikate stehen blieben.
    """
    try:
        service = build("calendar", "v3", credentials=creds)
        items = _list_all_events(service, calendar_id, start_time, end_time)
    except HttpError as error:
        logging.error(f"❌ Fehler beim Listen für Delete-by-Hashtag: {error}")
        return 0

    deleted = 0
    for event in items:
        if search_string not in event.get("summary", ""):
            continue
        eid = event.get("id")
        if not eid:
            continue
        try:
            service.events().delete(calendarId=calendar_id, eventId=eid).execute()
            logging.info(f"🗑️ Gelöscht: {event.get('summary', eid)}")
            deleted += 1
        except HttpError as error:
            if getattr(error, "resp", None) is not None and error.resp.status == 404:
                logging.info(f"ℹ️ Event {eid} bereits weg (404).")
                deleted += 1
            else:
                logging.error(f"❌ Delete fehlgeschlagen für {eid}: {error}")
    return deleted


def list_events_matching(
    calendar_id: str,
    creds: Credentials,
    start_time: datetime,
    end_time: datetime,
    search_string: str,
) -> list[dict]:
    """Listet Events im Zeitraum, deren Summary `search_string` enthält.

    Liefert eine Liste von `{id, summary}`-Dicts. Bei API-Fehler leere Liste.
    Paginiert intern, damit auch >250 Events sauber abgefragt werden.
    """
    try:
        service = build("calendar", "v3", credentials=creds)
        items = _list_all_events(service, calendar_id, start_time, end_time)
    except HttpError as error:
        logging.error(f"❌ Fehler beim Listen von Events: {error}")
        return []
    return [
        {"id": e.get("id", ""), "summary": e.get("summary", "")}
        for e in items
        if search_string in e.get("summary", "")
    ]


def delete_event_by_id(
    calendar_id: str,
    creds: Credentials,
    event_id: str,
) -> bool:
    """Löscht ein einzelnes Event per ID. Liefert True bei Erfolg, False sonst.

    404-Fehler (Event existiert nicht mehr) gelten als Erfolg — der Endzustand
    ist erreicht.
    """
    try:
        service = build("calendar", "v3", credentials=creds)
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        logging.info(f"🗑️ Gelöscht: {event_id}")
        return True
    except HttpError as error:
        if error.resp.status == 404:
            logging.info(f"ℹ️ Event {event_id} existiert nicht mehr (404) — überspringe.")
            return True
        logging.error(f"❌ Fehler beim Löschen Event {event_id}: {error}")
        return False


def update_event_description(
    calendar_id: str,
    creds: Credentials,
    event_id: str,
    description: str,
) -> bool:
    """Patcht nur die description eines Events. Andere Felder bleiben unverändert.

    Liefert True bei Erfolg.
    """
    try:
        service = build("calendar", "v3", credentials=creds)
        service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={"description": description},
        ).execute()
        logging.info(f"✏️ Description aktualisiert: {event_id}")
        return True
    except HttpError as error:
        logging.error(f"❌ Fehler beim Patchen Event {event_id}: {error}")
        return False
