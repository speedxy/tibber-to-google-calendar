"""Phase 6: synchronisiert berechnete Ladepläne mit dem Google Calendar.

Pro `#charge`-Ziel werden `#charging`-Sub-Events erstellt (eines pro
ChargeBlock) und die Description des `#charge`-Events selbst wird mit
einer Zusammenfassung des Plans aktualisiert. Hash-Diffing sorgt dafür,
dass unveränderte Pläne **null** Calendar-API-Calls erzeugen.
"""
from __future__ import annotations

import logging
from datetime import datetime

from google.oauth2.service_account import Credentials

from datetime import timedelta

from tibber_charge.gcal.events import (
    create_event,
    delete_event_by_id,
    list_events_matching,
    update_event_description,
)
from tibber_charge.planning.types import ChargeBlock, ChargePlan
from tibber_charge.state import PlansCache


# Horizont für Orphan-Scan: muss mindestens so weit wie Tibber+Forecast reichen.
ORPHAN_SCAN_DAYS = 14


def build_charging_event_title(plan: ChargePlan, vehicle_name: str) -> str:
    return f"🔋 Lade {vehicle_name} (⇒ {plan.target_soc}%) #charging"


def build_charging_event_description(block: ChargeBlock, plan: ChargePlan) -> str:
    date_str = block.start.strftime("%d.%m.%Y")
    start_str = block.start.strftime("%H:%M")
    end_str = block.end.strftime("%H:%M")
    forecast_note = ""
    if block.source != "tibber":
        forecast_note = f"\n⚠️ Preis ist Schätzung (Quelle: {block.source}, Konfidenz {block.confidence:.2f})"
    return (
        f"{date_str} {start_str}–{end_str}: "
        f"{block.expected_kwh:.1f} kWh auf {plan.target_soc}% "
        f"({block.expected_cost_eur:.2f} €)"
        f"{forecast_note}"
    )


def build_target_description(plan: ChargePlan, vehicle_name: str) -> str:
    """Beschreibung für das #charge-Event: alle Blöcke + Gesamtsumme + Warnungen."""
    lines = [
        f"Ladeplan für {vehicle_name}: SoC {plan.current_soc}% → {plan.target_soc}% "
        f"({plan.kwh_needed:.1f} kWh, {plan.net_kw:.1f} kW Wallbox-Netto)"
    ]
    if not plan.blocks:
        lines.append("")
        lines.append("⚠️ Kein Ladeplan möglich — keine geeigneten Slots gefunden.")
    else:
        lines.append("")
        for b in plan.blocks:
            d = b.start.strftime("%d.%m.%Y")
            s = b.start.strftime("%H:%M")
            e = b.end.strftime("%H:%M")
            tag = "" if b.source == "tibber" else f" 🔮 {b.source}"
            lines.append(
                f"  {d} {s}–{e}: {b.expected_kwh:.1f} kWh "
                f"@ {b.price_eur_per_kwh*100:.1f}ct = {b.expected_cost_eur:.2f} €{tag}"
            )
        lines.append("")
        lines.append(f"Gesamt: {plan.total_cost_eur:.2f} €  ({plan.cycles} Block(s))")
        if not plan.feasible:
            lines.append("")
            lines.append("⚠️ Ziel-SoC wird mit den verfügbaren Slots NICHT erreicht.")

    if plan.warnings:
        lines.append("")
        lines.append("Hinweise:")
        for w in plan.warnings:
            lines.append(f"  • {w}")

    return "\n".join(lines)


def cleanup_untracked_charging_events(
    calendar_id: str,
    creds: Credentials,
    plans_cache: PlansCache,
    now: datetime,
) -> int:
    """Löscht `#charging`-Events im Horizont, deren ID nicht im State getrackt ist.

    Schützt gegen Duplikate, wenn:
    - state.json verloren ging (manueller Reset oder Crash)
    - User hat Events manuell kopiert
    - Plan-Hash hat sich geändert UND alter delete fehlschlug

    Liefert die Anzahl gelöschter Events.
    """
    known_ids: set[str] = set()
    for entry in plans_cache.by_target.values():
        for cid in entry.get("child_event_ids") or []:
            if cid:
                known_ids.add(cid)

    end_time = now + timedelta(days=ORPHAN_SCAN_DAYS)
    candidates = list_events_matching(calendar_id, creds, now, end_time, "#charging")

    deleted = 0
    for evt in candidates:
        eid = evt.get("id")
        if not eid or eid in known_ids:
            continue
        if delete_event_by_id(calendar_id, creds, eid):
            deleted += 1
    if deleted:
        logging.info(f"🧹 {deleted} untracked #charging-Event(s) aufgeräumt.")
    return deleted


def sync_charge_plans(
    calendar_id: str,
    creds: Credentials,
    plans_cache: PlansCache,
    plans: list[ChargePlan],
    vehicle_name: str,
    now: datetime,
) -> dict[str, dict]:
    """Synchronisiert berechnete Pläne mit dem Calendar. Liefert das neue
    `by_target`-Dict für den State (mit aktuellen child_event_ids).

    Hash-Diffing: bei unverändertem Hash werden keine Calendar-API-Calls
    abgesetzt — abgesehen vom einmaligen Orphan-Scan vor der Iteration.
    """
    # Erst Orphans aufräumen — wenn state.json verloren ging, gibt es sonst Duplikate.
    cleanup_untracked_charging_events(calendar_id, creds, plans_cache, now)

    new_state: dict[str, dict] = {}
    seen_targets = {plan.target_event_id for plan in plans}

    for plan in plans:
        target_id = plan.target_event_id
        existing = plans_cache.by_target.get(target_id, {})
        old_hash = existing.get("plan_hash")

        if old_hash == plan.plan_hash:
            logging.info(
                f"📋 Plan für Target {target_id[:12]}... unverändert "
                f"(hash={plan.plan_hash[:8]}); skip Calendar-Sync."
            )
            new_state[target_id] = existing
            continue

        logging.info(
            f"📋 Plan-Hash für Target {target_id[:12]}... geändert "
            f"({(old_hash or 'neu')[:8]} → {plan.plan_hash[:8]}); synchronisiere."
        )
        # Alte #charging-Children weglöschen
        old_children: list[str] = list(existing.get("child_event_ids") or [])
        for child_id in old_children:
            delete_event_by_id(calendar_id, creds, child_id)

        # Neue #charging-Events erstellen
        new_children: list[str] = []
        title = build_charging_event_title(plan, vehicle_name)
        for block in plan.blocks:
            description = build_charging_event_description(block, plan)
            event_id = create_event(
                start_time=block.start,
                end_time=block.end,
                summary=title,
                calendar_id=calendar_id,
                creds=creds,
                description=description,
                reminders_minutes=None,
            )
            if event_id:
                new_children.append(event_id)

        # Description des #charge-Targets patchen
        target_description = build_target_description(plan, vehicle_name)
        update_event_description(calendar_id, creds, target_id, target_description)

        new_state[target_id] = {
            "plan_hash": plan.plan_hash,
            "child_event_ids": new_children,
            "plan_snapshot": plan.to_dict(),
        }

    # Orphan-Cleanup: Targets, die im State sind aber nicht mehr in den aktuellen Plänen
    # (z.B. weil das #charge-Event vom User gelöscht wurde).
    for orphan_id, orphan_state in plans_cache.by_target.items():
        if orphan_id in seen_targets:
            continue
        old_children = list(orphan_state.get("child_event_ids") or [])
        if old_children:
            logging.info(
                f"🗑️ Orphan-Target {orphan_id[:12]}... weg — räume "
                f"{len(old_children)} hängende #charging-Events auf."
            )
            for child_id in old_children:
                delete_event_by_id(calendar_id, creds, child_id)

    return new_state


def discard_charging_events_for_targets(
    calendar_id: str,
    creds: Credentials,
    plans_cache: PlansCache,
) -> dict[str, dict]:
    """Löscht alle bekannten #charging-Children. Genutzt wenn keine Pläne mehr existieren
    (z.B. weil SoC fehlt und Planung übersprungen wurde, oder alle Targets weg sind).

    Liefert ein leeres dict — Caller setzt damit den State zurück.
    """
    for target_id, target_state in plans_cache.by_target.items():
        for child_id in target_state.get("child_event_ids") or []:
            delete_event_by_id(calendar_id, creds, child_id)
    return {}
