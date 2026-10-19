from __future__ import annotations

import logging
import os

from google.oauth2 import service_account
from google.oauth2.service_account import Credentials


SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def authenticate(service_account_file: str) -> Credentials | None:
    """Authentifiziert sich über einen Service Account.

    Gibt None zurück, wenn die Authentifizierung fehlschlägt.
    """
    if not os.path.exists(service_account_file):
        logging.error(f"❌ Service-Account-Datei nicht gefunden: {service_account_file}")
        return None

    try:
        return service_account.Credentials.from_service_account_file(
            service_account_file, scopes=SCOPES
        )
    except Exception as e:
        logging.error(f"❌ Fehler bei der Service-Account-Authentifizierung: {e}")
        return None
