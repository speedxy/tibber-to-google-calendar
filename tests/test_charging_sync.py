"""Tests für sync_charge_plans — Hash-Diffing, Orphan-Cleanup, Builder."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tibber_charge.gcal.charging_sync import (
    build_charging_event_description,
    build_charging_event_title,
    build_target_description,
    cleanup_untracked_charging_events,
    discard_charging_events_for_targets,
    sync_charge_plans,
)
from tibber_charge.planning.types import ChargeBlock, ChargePlan
from tibber_charge.state import PlansCache


def _now() -> datetime:
    return datetime(2026, 5, 20, 22, 0, tzinfo=timezone.utc)


def _block(hour_offset: int, hours: int = 1, price: float = 0.20, source: str = "tibber") -> ChargeBlock:
    base = datetime(2026, 5, 21, 3, 0, tzinfo=timezone.utc)
    start = base + timedelta(hours=hour_offset)
    end = start + timedelta(hours=hours)
    kwh = 10.0 * hours
    return ChargeBlock(
        start=start, end=end,
        expected_kwh=kwh,
        expected_cost_eur=kwh * price,
        price_eur_per_kwh=price,
        source=source,
        confidence=1.0 if source == "tibber" else 0.5,
    )


def _plan(target_id: str = "tgt1", target_soc: int = 80, hash_: str = "h1",
          blocks: tuple[ChargeBlock, ...] | None = None,
          feasible: bool = True, warnings: tuple[str, ...] = ()) -> ChargePlan:
    if blocks is None:
        blocks = (_block(0), _block(1))
    return ChargePlan(
        target_event_id=target_id, target_soc=target_soc,
        deadline=datetime(2026, 5, 21, 7, 0, tzinfo=timezone.utc),
        current_soc=10, capacity_kwh=60.0, net_kw=10.0,
        kwh_needed=42.0, blocks=blocks, feasible=feasible,
        plan_hash=hash_, warnings=warnings,
    )


# --- Builder Tests ---

def test_charging_title_format():
    p = _plan(target_soc=80)
    assert build_charging_event_title(p, "Model 3") == "🔋 Lade Model 3 (⇒ 80%) #charging"


def test_charging_description_enthaelt_block_daten():
    p = _plan()
    b = _block(0, hours=2, price=0.15)
    desc = build_charging_event_description(b, p)
    # kWh prominent, kein irreführendes kW im Block-String (Wallbox-Max ≠ Block-Average)
    assert "20.0 kWh auf 80%" in desc  # 2h * 10kW = 20 kWh
    assert "3.00" in desc  # 2h * 10kWh * 0.15€ = 3.00€


def test_charging_description_forecast_zeigt_warnung():
    p = _plan()
    b = _block(0, source="open_meteo")
    desc = build_charging_event_description(b, p)
    assert "Schätzung" in desc
    assert "open_meteo" in desc


def test_target_description_enthaelt_alle_bloecke():
    p = _plan(blocks=(_block(0), _block(2), _block(5)))
    desc = build_target_description(p, "Auto")
    # Drei Block-Zeilen, kWh prominent statt irreführendes Wallbox-kW
    assert "10.0 kWh @ 20.0ct" in desc
    assert "Wallbox-Netto" in desc  # bleibt im Header
    assert "Gesamt:" in desc
    assert "3 Block" in desc


def test_target_description_zeigt_infeasible():
    p = _plan(feasible=False, warnings=("Nur 1 von 4 Slots",))
    desc = build_target_description(p, "Auto")
    assert "NICHT erreicht" in desc
    assert "Nur 1 von 4 Slots" in desc


def test_target_description_leere_blocks():
    p = _plan(blocks=(), warnings=("Keine Slots vor Deadline",))
    desc = build_target_description(p, "Auto")
    assert "Kein Ladeplan möglich" in desc


# --- Sync Logic Tests ---

def _cache_with(target_id: str, plan_hash: str, child_ids: list[str]) -> PlansCache:
    cache = PlansCache()
    cache.by_target[target_id] = {
        "plan_hash": plan_hash,
        "child_event_ids": child_ids,
        "plan_snapshot": {},
    }
    return cache


@patch("tibber_charge.gcal.charging_sync.list_events_matching", return_value=[])
@patch("tibber_charge.gcal.charging_sync.update_event_description")
@patch("tibber_charge.gcal.charging_sync.delete_event_by_id")
@patch("tibber_charge.gcal.charging_sync.create_event")
def test_unveraenderter_hash_macht_null_api_calls(create_mock, delete_mock, patch_mock, list_mock):
    plan = _plan(hash_="abc")
    cache = _cache_with("tgt1", plan_hash="abc", child_ids=["evt_a", "evt_b"])

    new_state = sync_charge_plans("cal", creds=None, plans_cache=cache, plans=[plan], vehicle_name="Auto", now=_now())

    assert create_mock.call_count == 0
    assert delete_mock.call_count == 0
    assert patch_mock.call_count == 0
    # State trägt alte child_event_ids unverändert weiter
    assert new_state["tgt1"]["plan_hash"] == "abc"
    assert new_state["tgt1"]["child_event_ids"] == ["evt_a", "evt_b"]


@patch("tibber_charge.gcal.charging_sync.list_events_matching", return_value=[])
@patch("tibber_charge.gcal.charging_sync.update_event_description", return_value=True)
@patch("tibber_charge.gcal.charging_sync.delete_event_by_id", return_value=True)
@patch("tibber_charge.gcal.charging_sync.create_event", side_effect=["new_a", "new_b"])
def test_hash_diff_loescht_alte_und_erstellt_neue(create_mock, delete_mock, patch_mock, list_mock):
    plan = _plan(hash_="NEW")
    cache = _cache_with("tgt1", plan_hash="OLD", child_ids=["evt_old1", "evt_old2"])

    new_state = sync_charge_plans("cal", creds=None, plans_cache=cache, plans=[plan], vehicle_name="Auto", now=_now())

    # Beide alten gelöscht
    assert delete_mock.call_count == 2
    # Zwei neue (= zwei Blöcke im Plan)
    assert create_mock.call_count == 2
    # Description gepatcht
    assert patch_mock.call_count == 1
    # Neue IDs im State
    assert new_state["tgt1"]["plan_hash"] == "NEW"
    assert new_state["tgt1"]["child_event_ids"] == ["new_a", "new_b"]


@patch("tibber_charge.gcal.charging_sync.list_events_matching", return_value=[])
@patch("tibber_charge.gcal.charging_sync.update_event_description", return_value=True)
@patch("tibber_charge.gcal.charging_sync.delete_event_by_id", return_value=True)
@patch("tibber_charge.gcal.charging_sync.create_event", side_effect=["fresh"])
def test_neuer_target_ohne_history_erstellt_nur(create_mock, delete_mock, patch_mock, list_mock):
    plan = _plan(hash_="new", blocks=(_block(0),))
    cache = PlansCache()  # leer

    new_state = sync_charge_plans("cal", creds=None, plans_cache=cache, plans=[plan], vehicle_name="Auto", now=_now())

    assert delete_mock.call_count == 0  # nichts altes zu löschen
    assert create_mock.call_count == 1
    assert new_state["tgt1"]["child_event_ids"] == ["fresh"]


@patch("tibber_charge.gcal.charging_sync.list_events_matching", return_value=[])
@patch("tibber_charge.gcal.charging_sync.delete_event_by_id", return_value=True)
def test_orphan_cleanup_loescht_alte_children(delete_mock, list_mock):
    """Ein Target im State, aber nicht mehr in den aktuellen Plänen → Children löschen."""
    cache = PlansCache()
    cache.by_target["orphan_target"] = {
        "plan_hash": "h", "child_event_ids": ["o1", "o2", "o3"], "plan_snapshot": {},
    }
    # Pläne leer → orphan komplett
    new_state = sync_charge_plans("cal", creds=None, plans_cache=cache, plans=[], vehicle_name="Auto", now=_now())

    assert delete_mock.call_count == 3
    assert "orphan_target" not in new_state


# --- Cleanup Untracked Tests ---

@patch("tibber_charge.gcal.charging_sync.delete_event_by_id", return_value=True)
@patch("tibber_charge.gcal.charging_sync.list_events_matching")
def test_cleanup_loescht_unbekannte_charging_events(list_mock, delete_mock):
    """Wenn ein #charging-Event im Kalender ist aber NICHT in state.plans → löschen."""
    list_mock.return_value = [
        {"id": "in_state_a", "summary": "🔋 Lade Auto #charging"},
        {"id": "orphan_b", "summary": "🔋 Lade Auto #charging"},
        {"id": "orphan_c", "summary": "🔋 Lade Auto #charging"},
    ]
    cache = PlansCache()
    cache.by_target["t1"] = {"plan_hash": "h", "child_event_ids": ["in_state_a"], "plan_snapshot": {}}

    deleted = cleanup_untracked_charging_events("cal", creds=None, plans_cache=cache, now=_now())

    assert deleted == 2  # orphan_b, orphan_c
    # in_state_a darf NICHT gelöscht werden
    deleted_ids = {call.args[2] for call in delete_mock.call_args_list}
    assert deleted_ids == {"orphan_b", "orphan_c"}
    assert "in_state_a" not in deleted_ids


@patch("tibber_charge.gcal.charging_sync.delete_event_by_id", return_value=True)
@patch("tibber_charge.gcal.charging_sync.list_events_matching", return_value=[])
def test_cleanup_keine_events_kein_call(list_mock, delete_mock):
    cache = PlansCache()
    deleted = cleanup_untracked_charging_events("cal", creds=None, plans_cache=cache, now=_now())
    assert deleted == 0
    delete_mock.assert_not_called()


@patch("tibber_charge.gcal.charging_sync.delete_event_by_id", return_value=True)
@patch("tibber_charge.gcal.charging_sync.list_events_matching")
def test_cleanup_alle_in_state_kein_delete(list_mock, delete_mock):
    list_mock.return_value = [
        {"id": "a", "summary": "🔋 Lade Auto #charging"},
        {"id": "b", "summary": "🔋 Lade Auto #charging"},
    ]
    cache = PlansCache()
    cache.by_target["t1"] = {"plan_hash": "h", "child_event_ids": ["a", "b"], "plan_snapshot": {}}

    deleted = cleanup_untracked_charging_events("cal", creds=None, plans_cache=cache, now=_now())
    assert deleted == 0
    delete_mock.assert_not_called()


# --- Discard Tests ---

@patch("tibber_charge.gcal.charging_sync.delete_event_by_id", return_value=True)
def test_discard_loescht_alle_children(delete_mock):
    cache = PlansCache()
    cache.by_target["t1"] = {"plan_hash": "h", "child_event_ids": ["a", "b"], "plan_snapshot": {}}
    cache.by_target["t2"] = {"plan_hash": "h", "child_event_ids": ["c"], "plan_snapshot": {}}

    result = discard_charging_events_for_targets("cal", creds=None, plans_cache=cache)

    assert delete_mock.call_count == 3
    assert result == {}
