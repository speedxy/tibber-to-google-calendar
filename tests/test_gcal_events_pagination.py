"""Tests für Pagination in gcal/events.py — list_events_matching folgt nextPageToken."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

from tibber_charge.gcal.events import delete_events_matching, list_events_matching


def _now() -> datetime:
    return datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


@patch("tibber_charge.gcal.events.build")
def test_list_events_matching_folgt_pagetoken(build_mock):
    """Wenn die erste Response nextPageToken hat, müssen weitere Seiten geholt werden.

    Vorher: nur Page 1 ausgewertet (default 250 Events). Bei Akkumulation
    über die Zeit fehlten Orphan-Detektionen für Events jenseits Page 1.
    """
    service = MagicMock()
    build_mock.return_value = service

    page1 = {
        "items": [
            {"id": "a", "summary": "X #Tibber"},
            {"id": "b", "summary": "Y other"},
        ],
        "nextPageToken": "tok2",
    }
    page2 = {
        "items": [
            {"id": "c", "summary": "Z #Tibber"},
        ],
        # kein nextPageToken → Ende
    }
    service.events.return_value.list.return_value.execute.side_effect = [page1, page2]

    result = list_events_matching("cal", creds=None, start_time=_now(), end_time=_now() + timedelta(days=3), search_string="#Tibber")

    # Beide Seiten gefiltert: nur Events mit #Tibber im Summary
    ids = [r["id"] for r in result]
    assert ids == ["a", "c"]
    # Zwei list()-Calls (eine pro Page)
    assert service.events.return_value.list.call_count == 2


@patch("tibber_charge.gcal.events.build")
def test_list_events_matching_eine_page_kein_token(build_mock):
    service = MagicMock()
    build_mock.return_value = service
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "a", "summary": "X #Tibber"}],
    }

    result = list_events_matching("cal", creds=None, start_time=_now(), end_time=_now() + timedelta(days=3), search_string="#Tibber")
    assert [r["id"] for r in result] == ["a"]
    assert service.events.return_value.list.call_count == 1


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=b"err")


@patch("tibber_charge.gcal.events.build")
def test_delete_events_matching_einzelner_fehler_bricht_loop_nicht_ab(build_mock):
    """Regression: vorher hat ein HttpError beim ersten delete den gesamten
    Loop abgebrochen — alle folgenden Duplikate blieben für immer im Kalender.
    Jetzt per-Event try/except.
    """
    service = MagicMock()
    build_mock.return_value = service
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "a", "summary": "X #Tibber"},
            {"id": "b", "summary": "Y #Tibber"},
            {"id": "c", "summary": "Z #Tibber"},
        ],
    }
    # Erster delete schlägt fehl, andere klappen.
    service.events.return_value.delete.return_value.execute.side_effect = [
        _http_error(500),
        None,
        None,
    ]

    deleted = delete_events_matching(
        "cal", creds=None, start_time=_now(), end_time=_now() + timedelta(days=3),
        search_string="#Tibber",
    )

    # 2 von 3 erfolgreich gelöscht — der Loop wurde nicht abgebrochen
    assert deleted == 2
    assert service.events.return_value.delete.call_count == 3


@patch("tibber_charge.gcal.events.build")
def test_delete_events_matching_404_zaehlt_als_erfolg(build_mock):
    """Event schon weg → idempotent als Erfolg behandeln."""
    service = MagicMock()
    build_mock.return_value = service
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "a", "summary": "X #Tibber"}],
    }
    service.events.return_value.delete.return_value.execute.side_effect = _http_error(404)

    deleted = delete_events_matching(
        "cal", creds=None, start_time=_now(), end_time=_now() + timedelta(days=3),
        search_string="#Tibber",
    )
    assert deleted == 1


@patch("tibber_charge.gcal.events.build")
def test_delete_events_matching_ueberspringt_andere_summaries(build_mock):
    service = MagicMock()
    build_mock.return_value = service
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "a", "summary": "X #Tibber"},
            {"id": "b", "summary": "Y unrelated"},
            {"id": "c", "summary": "Z #Tibber"},
        ],
    }
    service.events.return_value.delete.return_value.execute.return_value = None

    deleted = delete_events_matching(
        "cal", creds=None, start_time=_now(), end_time=_now() + timedelta(days=3),
        search_string="#Tibber",
    )
    assert deleted == 2
    # Nur 2 Deletes (Event "b" wurde übersprungen)
    assert service.events.return_value.delete.call_count == 2
