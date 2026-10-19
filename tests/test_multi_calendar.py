"""Tests: Ladeziele aus mehreren Kalendern, Plan-Beschreibung nur bei Schreibzugriff,
Cache-Reset bei Kalenderwechsel."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from tibber_charge.config import load_config
from tibber_charge.gcal.charge_targets import ChargeTarget, load_charge_targets
from tibber_charge.gcal.charging_sync import sync_charge_plans
from tibber_charge.main import _reset_caches_on_calendar_change
from tibber_charge.planning.types import ChargeBlock, ChargePlan
from tibber_charge.state import CalendarEventsCache, PlansCache, State


NOW = datetime(2026, 10, 19, 22, 0, tzinfo=timezone.utc)


def _target(event_id: str, calendar_id: str, writable: bool) -> ChargeTarget:
    return ChargeTarget(
        deadline=NOW + timedelta(days=1), target_soc=80, car_id=None,
        source_event_id=event_id, source_event_summary="80% #charge",
        calendar_id=calendar_id, writable=writable,
    )


def _plan(target_id: str) -> ChargePlan:
    block = ChargeBlock(start=NOW + timedelta(hours=3), end=NOW + timedelta(hours=4), expected_kwh=10.0,
                        expected_cost_eur=4.0, price_eur_per_kwh=0.4, source="tibber", confidence=1.0)
    return ChargePlan(target_event_id=target_id, target_soc=80, deadline=NOW + timedelta(days=1),
                      current_soc=20, capacity_kwh=60.0, net_kw=10.0, kwh_needed=10.0,
                      blocks=(block,), feasible=True, plan_hash="h-" + target_id, warnings=())


def test_config_liest_mehrere_zielkalender(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "TIBBER_API_KEY": "k", "GOOGLE_CALENDAR_ID": "plan@cal",
        "CHARGING": {"target_calendar_ids": ["plan@cal", "privat@gmail.com"]},
    }))
    cfg = load_config(str(tmp_path))
    assert cfg.charging.targets_or(cfg.google_calendar_id) == ("plan@cal", "privat@gmail.com")


def test_ohne_zielkalender_nur_hauptkalender(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"TIBBER_API_KEY": "k", "GOOGLE_CALENDAR_ID": "plan@cal"}))
    cfg = load_config(str(tmp_path))
    assert cfg.charging.targets_or(cfg.google_calendar_id) == ("plan@cal",)


@patch("tibber_charge.gcal.charge_targets.build")
def test_ladeziel_kennt_kalender_und_schreibrecht(build_mock):
    service = MagicMock()
    build_mock.return_value = service
    service.events.return_value.list.return_value.execute.return_value = {
        "accessRole": "reader",
        "items": [{"id": "e1", "summary": "Hamburg 80% #charge",
                   "start": {"dateTime": "2026-10-21T07:00:00+02:00"}}],
    }
    [t] = load_charge_targets("privat@gmail.com", None, NOW, NOW + timedelta(days=7))
    assert t.calendar_id == "privat@gmail.com"
    assert t.writable is False

    service.events.return_value.list.return_value.execute.return_value["accessRole"] = "owner"
    [t] = load_charge_targets("plan@cal", None, NOW, NOW + timedelta(days=7))
    assert t.writable is True


@patch("tibber_charge.gcal.charging_sync.list_events_matching", return_value=[])
@patch("tibber_charge.gcal.charging_sync.update_event_description", return_value=True)
@patch("tibber_charge.gcal.charging_sync.create_event", return_value="child")
def test_plan_beschreibung_nur_bei_schreibzugriff(create_mock, update_mock, _list):
    targets = {
        "lesbar": _target("lesbar", "privat@gmail.com", writable=False),
        "schreibbar": _target("schreibbar", "andere@cal", writable=True),
    }
    sync_charge_plans("plan@cal", None, PlansCache(), [_plan("lesbar"), _plan("schreibbar")],
                      "Model 3", NOW, targets_by_id=targets)

    # #charging-Termine landen immer im Planungskalender
    assert {c.kwargs["calendar_id"] for c in create_mock.call_args_list} == {"plan@cal"}
    # Beschreibung nur für das schreibbare Ziel, und zwar in dessen Kalender
    assert update_mock.call_count == 1
    assert update_mock.call_args.args[0] == "andere@cal"
    assert update_mock.call_args.args[2] == "schreibbar"


def test_kalenderwechsel_setzt_caches_zurueck():
    state = State()
    state.calendar_id = "alt@cal"
    state.tibber_events = CalendarEventsCache(events_hash="h", event_ids=["a"])
    state.forecast_events = CalendarEventsCache(events_hash="f", event_ids=["b"])
    state.plans.by_target = {"t": {"plan_hash": "x", "child_event_ids": ["c"]}}

    _reset_caches_on_calendar_change(state, "neu@cal")

    assert state.tibber_events.events_hash is None
    assert state.forecast_events.events_hash is None
    assert state.plans.by_target == {}
    assert state.calendar_id == "neu@cal"


def test_gleicher_kalender_behaelt_caches():
    state = State()
    state.calendar_id = "cal"
    state.tibber_events = CalendarEventsCache(events_hash="h", event_ids=["a"])
    _reset_caches_on_calendar_change(state, "cal")
    assert state.tibber_events.events_hash == "h"


def test_erster_lauf_ohne_gespeicherten_kalender_behaelt_caches():
    """Bestehende state.json ohne calendar_id: nicht alles neu anlegen."""
    state = State()
    state.tibber_events = CalendarEventsCache(events_hash="h", event_ids=["a"])
    _reset_caches_on_calendar_change(state, "cal")
    assert state.tibber_events.events_hash == "h"
    assert state.calendar_id == "cal"
