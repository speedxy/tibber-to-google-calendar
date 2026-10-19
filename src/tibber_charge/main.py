from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta

from dateutil import parser

from tibber_charge.config import load_config
from tibber_charge.gcal.auth import authenticate
from tibber_charge.gcal.charge_targets import detect_conflicts, load_charge_targets
from tibber_charge.gcal.charging_sync import (
    discard_charging_events_for_targets,
    sync_charge_plans,
)
from tibber_charge.gcal.price_events_sync import sync_forecast_events, sync_tibber_events
from tibber_charge.hardware.homeassistant import HomeAssistantClient
from tibber_charge.planning.linear import LinearStrategy
from tibber_charge.planning.types import ChargePlan
from tibber_charge.prices.forecast.cache import fetch_forecast_with_cache
from tibber_charge.prices.forecast.calibration import match_observations, record_predictions
from tibber_charge.prices.forecast.epex_predictor import EpexPredictorForecastProvider
from tibber_charge.prices.mapping import (
    PriceMapping,
    apply_mapping,
    fit_mapping,
    log_mapping,
    resolve_mapping,
)
from tibber_charge.prices.merger import unified_prices
from tibber_charge.prices.types import UnifiedPrice
from tibber_charge.vehicle.base import VehicleStatus
from tibber_charge.vehicle.cache import fetch_status_with_cache
from tibber_charge.state import CalendarEventsCache, load_state, save_state, state_lock
from tibber_charge.tibber.cache import fetch_with_cache
from tibber_charge.tibber.grouping import group_price_periods


# Fenster, in dem wir nach #charge-Targets im Kalender suchen.
CHARGE_TARGET_LOOKAHEAD = timedelta(days=7)


# Standard-Level für Cron-Läufe (keine Mails). Kann per CLI überschrieben werden.
DEFAULT_LOG_LEVEL = logging.WARNING


def _configure_logging(level: int | str | None = None) -> None:
    logging.basicConfig(
        level=level if level is not None else DEFAULT_LOG_LEVEL,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def run(base_dir: str, log_level: int | str | None = None) -> None:
    """Hauptlauf: Tibber-Preise abrufen (mit Cache) und als #Tibber-Events im Kalender ablegen."""
    _configure_logging(log_level)
    config = load_config(base_dir)

    try:
        with state_lock(base_dir):
            _run_locked(base_dir, config)
    except BlockingIOError:
        logging.warning("⚠️ Anderer Lauf hält den Lock — Skript beendet ohne Aktion.")


def _plan_charge_targets(
    config,
    creds,
    state,
    vehicle_status: VehicleStatus | None,
    merged_prices: list[UnifiedPrice],
    now: datetime,
) -> tuple[list[ChargePlan], dict]:
    """Liest #charge-Targets aus allen konfigurierten Kalendern, berechnet Pläne, loggt.

    Liefert (Pläne, Ladeziele nach Event-ID). Die Ladeziele braucht der
    Calendar-Sync, um den Plan in den richtigen Quell-Kalender zu schreiben.
    """
    targets: list = []
    seen_ids: set[str] = set()
    for calendar_id in config.charging.targets_or(config.google_calendar_id):
        for t in load_charge_targets(
            calendar_id, creds, time_min=now, time_max=now + CHARGE_TARGET_LOOKAHEAD,
        ):
            # Derselbe Termin kann über Einladungen in mehreren Kalendern stehen.
            if t.source_event_id not in seen_ids:
                seen_ids.add(t.source_event_id)
                targets.append(t)
    targets.sort(key=lambda t: t.deadline)
    targets_by_id = {t.source_event_id: t for t in targets}
    if not targets:
        logging.info("ℹ️ Keine #charge-Ladeziele im 7-Tage-Fenster.")
        return [], targets_by_id

    logging.info(f"🔋 {len(targets)} Ladeziel(e) gefunden:")
    for t in targets:
        car = f" [{t.car_id}]" if t.car_id else ""
        deadline_str = t.deadline.strftime("%d.%m.%Y %H:%M")
        warn_str = f" ⚠️ {list(t.parse_warnings)}" if t.parse_warnings else ""
        logging.info(f"  → {t.target_soc}%{car} bis {deadline_str}{warn_str}")

    for a, b in detect_conflicts(targets):
        logging.warning(
            f"⚠️ Konflikt: zwei Ladeziele mit gleicher Deadline {a.deadline.isoformat()} — "
            f"{a.source_event_summary!r} vs {b.source_event_summary!r}"
        )

    # Wenn wir keinen aktuellen SoC haben, können wir nicht planen.
    if vehicle_status is None or vehicle_status.soc is None:
        logging.warning("⚠️ Kein bekannter SoC — überspringe Ladeplanung.")
        return [], targets_by_id

    strategy = LinearStrategy(
        buffer_factor=config.charging.buffer_factor,
        slot_minutes=config.charging.slot_minutes,
        max_cycles_warning=config.charging.max_cycles_warning,
    )

    plans: list[ChargePlan] = []
    for t in targets:
        plan = strategy.plan(
            target=t,
            current_soc=vehicle_status.soc,
            capacity_kwh=config.vehicle.battery_capacity_kwh,
            net_kw=config.wallbox.net_kw,
            prices=merged_prices,
            now=now,
        )
        plans.append(plan)
        _log_plan(t, plan)

    return plans, targets_by_id


def _log_plan(target, plan: ChargePlan) -> None:
    """Mehrzeiliges INFO-Log mit dem Plan."""
    target_label = f"{target.target_soc}%"
    if target.car_id:
        target_label += f" [{target.car_id}]"
    feasibility = "✅ machbar" if plan.feasible else "⚠️ NICHT machbar"
    logging.info(
        f"📋 Plan für {target_label} bis {plan.deadline.strftime('%d.%m. %H:%M')}: "
        f"SoC {plan.current_soc}→{plan.target_soc}% ({plan.kwh_needed:.1f} kWh), "
        f"{plan.cycles} Block(s), {plan.total_cost_eur:.2f}€ erwartet — {feasibility}"
    )
    for b in plan.blocks:
        forecast_flag = "" if b.source == "tibber" else f" 🔮 {b.source}"
        logging.info(
            f"  ⚡ {b.start.strftime('%d.%m. %H:%M')} - {b.end.strftime('%H:%M')}: "
            f"{b.expected_kwh:.1f} kWh @ {b.price_eur_per_kwh*100:.1f}ct/kWh "
            f"= {b.expected_cost_eur:.2f}€{forecast_flag}"
        )
    # Hinweise zu machbaren Plänen (Ziel schon erreicht, Prognose-Anteil, viele
    # Blöcke) nur als INFO: sonst schreibt jeder 15-min-Cron-Lauf sie ins Log.
    # WARNING nur, wenn das Ziel nicht erreichbar ist.
    if not plan.feasible:
        logging.warning(
            f"⚠️ Ladeziel {target_label} bis {plan.deadline.strftime('%d.%m. %H:%M')} "
            f"ist mit den verfügbaren Slots NICHT erreichbar."
        )
    log = logging.warning if not plan.feasible else logging.info
    for w in plan.warnings:
        log(f"  {w}")


def _run_locked(base_dir: str, config) -> None:
    state = load_state(base_dir)
    now = datetime.now().astimezone()

    tibber_prices = fetch_with_cache(state, config.tibber_api_key, now)
    forecast_prices, forecast_was_fresh = fetch_forecast_with_cache(state, config, now)
    forecast_prices = _map_forecast_to_tibber(state, tibber_prices, forecast_prices, now)
    vehicle_status = _apply_plug_sensor(config, fetch_status_with_cache(state, config, now, base_dir))

    # Kalibrierung: jeden Lauf alte Forecasts mit realem Tibber pairen;
    # nur bei fresh-fetch neue Prognose-Samples anlegen. Erfasst werden die
    # umgerechneten Endpreise, damit actual vs. forecast vergleichbar ist.
    match_observations(state, now)
    if forecast_was_fresh:
        record_predictions(state, forecast_prices, now)

    # Vehicle-Status loggen — Planung kommt in Phase 5.
    if vehicle_status is not None:
        logging.info(
            f"🚗 {vehicle_status.vehicle_name}: SoC={vehicle_status.soc}%, "
            f"connected={vehicle_status.is_connected}, charging={vehicle_status.is_charging} "
            f"(source={vehicle_status.source})"
        )
    else:
        logging.info("🚗 Kein Fahrzeug-Status verfügbar.")

    # State-Stand immer sichern (Backoff, Timestamps, Cache)
    save_state(base_dir, state)

    # Vereinigter Preisvektor (Tibber + Forecast).
    merged = unified_prices(tibber_prices, forecast_prices, now)
    tibber_only_dicts = [p.to_dict() for p in merged if p.source == "tibber"]
    forecast_only = [p for p in merged if p.source != "tibber"]

    if not tibber_only_dicts and not forecast_only:
        logging.warning("⚠️ Keine Preise verfügbar — Kalender-Sync übersprungen.")
        return

    service_account_file = os.path.join(base_dir, "service_account.json")
    creds = authenticate(service_account_file)
    if creds is None:
        logging.error("❌ Google Calendar Authentifizierung fehlgeschlagen. Abbruch.")
        return

    _reset_caches_on_calendar_change(state, config.google_calendar_id)

    # Pläne berechnen → mit Calendar synchronisieren (Hash-Diffing).
    plans, targets_by_id = _plan_charge_targets(config, creds, state, vehicle_status, merged, now)
    if plans:
        state.plans.by_target = sync_charge_plans(
            calendar_id=config.google_calendar_id,
            creds=creds,
            plans_cache=state.plans,
            plans=plans,
            vehicle_name=config.vehicle.vehicle_name,
            now=now,
            targets_by_id=targets_by_id,
        )
    elif state.plans.by_target:
        # Keine aktuellen Pläne mehr (z.B. alle Targets gelöscht oder SoC unbekannt)
        # → bekannte #charging-Children aufräumen, State zurücksetzen.
        state.plans.by_target = discard_charging_events_for_targets(
            config.google_calendar_id, creds, state.plans,
        )

    # Phase 7: Hardware-Steuerung über Home Assistant
    _drive_charging(config, state, plans, now)

    save_state(base_dir, state)

    # --- Tibber-Domäne (#Tibber) mit Hash-Diffing ---
    if tibber_only_dicts:
        state.tibber_events = sync_tibber_events(
            calendar_id=config.google_calendar_id,
            creds=creds,
            cache=state.tibber_events,
            tibber_dicts=tibber_only_dicts,
            now=now,
        )
    else:
        logging.warning("⚠️ Keine Tibber-Preise im Cache — Tibber-Domäne übersprungen.")

    # --- Forecast-Domäne (#Vorhersage) mit Hash-Diffing ---
    # Auch bei leerem forecast_only aufrufen — damit alte #Vorhersage-Events
    # aufgeräumt werden, wenn Tibber jetzt den gesamten Horizont abdeckt oder
    # der Forecast-Provider länger ausfällt.
    # Bei calendar_events_enabled=False oder enabled=False: leere Liste +
    # force_cleanup=True — räumt auch dann auf, wenn der Cache nichts trackt
    # (z.B. User hat state.json gelöscht oder kommt von einer alten Version).
    forecast_calendar_disabled = (
        not config.forecast.enabled or not config.forecast.calendar_events_enabled
    )
    forecast_for_calendar = [] if forecast_calendar_disabled else forecast_only
    state.forecast_events = sync_forecast_events(
        calendar_id=config.google_calendar_id,
        creds=creds,
        cache=state.forecast_events,
        forecast_prices=forecast_for_calendar,
        provider_name=state.forecast.provider or "forecast",
        generated_at=state.forecast.last_success_at,
        horizon_days=config.forecast.horizon_days,
        now=now,
        force_cleanup=forecast_calendar_disabled,
    )

    # State nach Sync persistieren — sonst geht der neue events_hash /
    # confirmed_empty beim nächsten Lauf verloren.
    save_state(base_dir, state)

    logging.info("Skript abgeschlossen.")


def _map_forecast_to_tibber(
    state,
    tibber_prices: list[dict],
    forecast_prices: list[UnifiedPrice],
    now: datetime,
) -> list[UnifiedPrice]:
    """Rechnet Prognose-Rohwerte mit der selbst gelernten Formel in Tibber-Preise um.

    Ohne Umrechnung wird eine Börsenpreis-Prognose verworfen (sie wäre um die
    Fixkosten zu billig); die Wetter-Heuristik bleibt dann unverändert.
    """
    if not forecast_prices:
        return forecast_prices
    source = forecast_prices[0].source
    fitted = fit_mapping(tibber_prices, forecast_prices, now)
    mapping = resolve_mapping(fitted, PriceMapping.from_dict(state.forecast.price_mapping), source)
    if mapping is None:
        if source == EpexPredictorForecastProvider.name:
            logging.warning(
                "⚠️ Noch keine Umrechnung Börsenpreis → Tibber-Preis gelernt; "
                "Prognose wird in diesem Lauf nicht genutzt."
            )
            return []
        return forecast_prices
    if fitted is not None:
        state.forecast.price_mapping = fitted.to_dict()
    log_mapping(mapping, fresh=fitted is not None)
    return apply_mapping(forecast_prices, mapping)


def _reset_caches_on_calendar_change(state, calendar_id: str) -> None:
    """Nach Wechsel von GOOGLE_CALENDAR_ID gelten die Event-Caches für den alten
    Kalender. Ohne Reset hielte das Hash-Diffing den neuen, leeren Kalender für
    "unverändert". Termine im alten Kalender räumt das Skript NICHT auf.
    """
    if state.calendar_id is not None and state.calendar_id != calendar_id:
        logging.warning(
            "⚠️ Ziel-Kalender gewechselt: Termine werden im neuen Kalender neu angelegt. "
            "Alte #Tibber/#Vorhersage/#charging-Termine im vorherigen Kalender bitte selbst löschen."
        )
        state.tibber_events = CalendarEventsCache()
        state.forecast_events = CalendarEventsCache()
        state.plans.by_target = {}
    state.calendar_id = calendar_id


def _apply_plug_sensor(config, status: VehicleStatus | None) -> VehicleStatus | None:
    """Steckerstatus aus dem Wallbox-Sensor in HA statt vom Fahrzeug-Provider.

    Tibber meldet den Stecker teils stundenlang verzögert, der Wallbox-Sensor ist
    sofort aktuell. Bei Lesefehler oder unbrauchbarem Wert bleibt der Provider-Wert.
    """
    ha_cfg = config.homeassistant
    if status is None or not ha_cfg.enabled or not ha_cfg.api_token or not ha_cfg.plug_entity_id:
        return status
    client = HomeAssistantClient(
        base_url=ha_cfg.base_url,
        api_token=ha_cfg.api_token,
        timeout_seconds=ha_cfg.timeout_seconds,
    )
    plug = client.get_state(ha_cfg.plug_entity_id)
    if plug not in ("on", "off"):
        logging.warning(
            f"⚠️ Stecker-Sensor {ha_cfg.plug_entity_id} liefert {plug!r}; nutze Wert vom Fahrzeug-Provider."
        )
        return status
    connected = plug == "on"
    if connected != status.is_connected:
        logging.info(
            f"🔌 Steckerstatus laut Wallbox: {'eingesteckt' if connected else 'nicht eingesteckt'} "
            f"(Fahrzeug-Provider meldete {'eingesteckt' if status.is_connected else 'nicht eingesteckt'})."
        )
    return replace(status, is_connected=connected)


def _drive_charging(config, state, plans: list[ChargePlan], now: datetime) -> None:
    """Phase 7: Hardware-Steuerung der Wallbox via Home Assistant.

    Schaltet `input_boolean.<charging_entity_id>`:
    - ON wenn `now` in mindestens einem geplanten ChargeBlock liegt
    - OFF sonst

    Idempotenz: HA-Calls nur bei State-Wechsel gegenüber `state.hardware`.
    Heartbeat (optional): jeden Lauf in `input_datetime` schreiben, damit
    eine HA-Automation bei verpasstem Heartbeat eigene Abschaltung machen kann.
    """
    ha_cfg = config.homeassistant
    if not ha_cfg.enabled:
        return
    if not ha_cfg.api_token:
        logging.warning("⚠️ HOMEASSISTANT.enabled=true, aber kein api_token — Steuerung übersprungen.")
        return

    client = HomeAssistantClient(
        base_url=ha_cfg.base_url,
        api_token=ha_cfg.api_token,
        timeout_seconds=ha_cfg.timeout_seconds,
    )

    # Bestimme ob now in einem Block liegt
    active_block_info: tuple | None = None
    for plan in plans:
        for block in plan.blocks:
            if block.start <= now < block.end:
                active_block_info = (plan, block)
                break
        if active_block_info:
            break

    desired = "on" if active_block_info else "off"
    last = state.hardware.last_charging_state

    if last == desired:
        logging.info(f"🔌 Wallbox-Schaltzustand unverändert ({desired}); kein HA-Call.")
    else:
        ok = client.set_input_boolean(ha_cfg.charging_entity_id, desired == "on")
        if ok:
            state.hardware.last_charging_state = desired
            state.hardware.last_charging_set_at = now.isoformat()
            if active_block_info:
                plan, block = active_block_info
                logging.info(
                    f"🔌 Wallbox EIN (Ziel {plan.target_soc}%, Block bis {block.end.strftime('%H:%M')}, "
                    f"@ {block.price_eur_per_kwh*100:.1f}ct/kWh)"
                )
            else:
                logging.info("🔌 Wallbox AUS (kein aktiver Lade-Block).")
        else:
            logging.warning(
                f"⚠️ HA-Schaltung fehlgeschlagen — state.hardware NICHT geändert. "
                f"Wallbox bleibt im letzten gemeldeten Zustand ({last!r})."
            )

    # Heartbeat — jeden Lauf, unabhängig vom Schaltzustand
    if ha_cfg.heartbeat_entity_id:
        if client.write_input_datetime(ha_cfg.heartbeat_entity_id, now):
            state.hardware.last_heartbeat_at = now.isoformat()
