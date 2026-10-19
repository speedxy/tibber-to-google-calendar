"""#charge-Events lesen und in ChargeTargets übersetzen.

Title-Spec: ein Hashtag `#charge` (exakt, case-insensitive) + irgendwo eine
Prozentzahl `XX%` (1–3 Ziffern, optional in Klammern, optional mit Leerzeichen).
Zusätzliche Hashtags (z.B. `#model3`) gelten als Car-ID. Zusatztext davor/danach
wird ignoriert.

Deadline = `event.start`. Das Auto muss zu Event-Beginn geladen sein.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from dateutil import parser as date_parser
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


_HASHTAG_RE = re.compile(r"#(\w+)")
# Erlaubt optionale Klammern um die Zahl (Spec: "[X]% #charge")
_SOC_RE = re.compile(r"\[?\s*(\d{1,3})\s*\]?\s*%")


@dataclass(frozen=True)
class ChargeTarget:
    deadline: datetime
    target_soc: int
    car_id: str | None
    source_event_id: str
    source_event_summary: str
    parse_warnings: tuple[str, ...] = field(default_factory=tuple)


def parse_title(summary: str) -> tuple[int | None, str | None, list[str]]:
    """Parsed Titel eines möglichen #charge-Events.

    Liefert (target_soc, car_id, warnings). target_soc=None heißt "ablehnen".
    car_id ist None, wenn kein zweiter Hashtag vorhanden.
    """
    warnings: list[str] = []
    hashtags = _HASHTAG_RE.findall(summary)
    if not any(h.lower() == "charge" for h in hashtags):
        return None, None, ["kein_#charge_tag"]

    car_tags = [h for h in hashtags if h.lower() != "charge"]
    car_id = car_tags[0].lower() if car_tags else None
    if len(car_tags) > 1:
        warnings.append(f"mehrere_car_tags: {car_tags}")

    soc_matches = _SOC_RE.findall(summary)
    if not soc_matches:
        return None, car_id, warnings + ["keine_soc_gefunden"]
    if len(soc_matches) > 1:
        warnings.append(f"mehrere_soc_werte: {soc_matches}, nehme ersten")

    target_soc = int(soc_matches[0])
    if not 1 <= target_soc <= 100:
        warnings.append(f"soc_außerhalb_1_100: {target_soc}")
        return None, car_id, warnings

    return target_soc, car_id, warnings


def load_charge_targets(
    calendar_id: str,
    creds: Credentials,
    time_min: datetime,
    time_max: datetime,
) -> list[ChargeTarget]:
    """Liest #charge-Events aus dem Kalender und parst sie zu ChargeTargets.

    All-Day-Events werden mit Warnung übersprungen (keine konkrete Deadline).
    Eigene #charging-Output-Events werden gar nicht erst gematcht.
    """
    targets: list[ChargeTarget] = []
    try:
        service = build("calendar", "v3", credentials=creds)
        response = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            q="#charge",
        ).execute()
    except HttpError as error:
        logging.error(f"❌ Fehler beim Lesen der Kalender-Events: {error}")
        return targets

    for event in response.get("items", []):
        summary = event.get("summary", "")
        event_id = event.get("id", "")

        # Doppelt absichern: q-Parameter ist fuzzy
        if not any(h.lower() == "charge" for h in _HASHTAG_RE.findall(summary)):
            continue

        start = event.get("start", {})
        if "dateTime" not in start:
            logging.warning(f"⏭️ All-Day #charge-Event übersprungen: {summary!r}")
            continue

        try:
            deadline = date_parser.isoparse(start["dateTime"])
        except (ValueError, TypeError) as e:
            logging.warning(f"⏭️ Unparsbarer Start-Zeitpunkt in #charge-Event {summary!r}: {e}")
            continue

        target_soc, car_id, warnings = parse_title(summary)
        if target_soc is None:
            logging.warning(f"⏭️ #charge ohne erkennbares Ziel: {summary!r} ({warnings})")
            continue

        targets.append(
            ChargeTarget(
                deadline=deadline,
                target_soc=target_soc,
                car_id=car_id,
                source_event_id=event_id,
                source_event_summary=summary,
                parse_warnings=tuple(warnings),
            )
        )

    return targets


def detect_conflicts(
    targets: list[ChargeTarget],
) -> list[tuple[ChargeTarget, ChargeTarget]]:
    """Findet Targets mit identischer Deadline (V1: ein Auto, daher jede Doppelbelegung).

    Echte Zeit-Range-Konflikte (Ladefenster überlappen) entstehen erst nach
    der Planung in Phase 5 — hier vergleichen wir nur die User-spezifizierten
    Deadlines auf 1-Auto-Doppelbuchung.
    """
    conflicts: list[tuple[ChargeTarget, ChargeTarget]] = []
    sorted_targets = sorted(targets, key=lambda t: t.deadline)
    for i, a in enumerate(sorted_targets):
        for b in sorted_targets[i + 1 :]:
            if a.deadline == b.deadline:
                conflicts.append((a, b))
    return conflicts
