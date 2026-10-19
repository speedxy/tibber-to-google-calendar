"""Tests für LinearStrategy: Allokation, Blöcke, Hash, Edge Cases."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tibber_charge.gcal.charge_targets import ChargeTarget
from tibber_charge.planning.linear import LinearStrategy
from tibber_charge.prices.types import UnifiedPrice


def _now() -> datetime:
    return datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc)


def _target(soc: int, deadline_hours: int = 24, eid: str = "evt1") -> ChargeTarget:
    return ChargeTarget(
        deadline=_now() + timedelta(hours=deadline_hours),
        target_soc=soc, car_id=None,
        source_event_id=eid, source_event_summary=f"{soc}% #charge",
    )


def _price(hour: int, total: float, source: str = "tibber", confidence: float = 1.0) -> UnifiedPrice:
    return UnifiedPrice(
        startsAt=(_now() + timedelta(hours=hour)).isoformat(),
        total=total, level="NORMAL",
        source=source, confidence=confidence,
    )


def test_soc_bereits_erreicht_kein_block():
    strat = LinearStrategy()
    plan = strat.plan(
        target=_target(soc=50),
        current_soc=60,                # bereits höher als Ziel
        capacity_kwh=60.0, net_kw=10.0,
        prices=[_price(h, 0.20) for h in range(24)],
        now=_now(),
    )
    assert plan.blocks == ()
    assert plan.feasible is True
    assert "bereits erreicht" in plan.warnings[0].lower()


def test_kwh_berechnung_mit_puffer():
    """50 → 80 % bei 60 kWh = 18 kWh; +10 % Puffer = 19.8 kWh."""
    strat = LinearStrategy(buffer_factor=1.10)
    plan = strat.plan(
        target=_target(soc=80),
        current_soc=50,
        capacity_kwh=60.0, net_kw=10.0,
        prices=[_price(h, 0.20) for h in range(24)],
        now=_now(),
    )
    assert plan.kwh_needed == pytest.approx(19.8)


def test_slot_anzahl_aus_kwh_und_netkw():
    """19.8 kWh / 10 kW = 1.98h → 2 Slots (à 60 min), aber expected_kwh == kwh_needed."""
    strat = LinearStrategy()
    plan = strat.plan(
        target=_target(soc=80),
        current_soc=50,
        capacity_kwh=60.0, net_kw=10.0,
        prices=[_price(h, 0.20) for h in range(24)],
        now=_now(),
    )
    # Block(s) decken zusammen exakt kwh_needed (19.8 kWh), nicht 2 × 10 = 20
    total_kwh = sum(b.expected_kwh for b in plan.blocks)
    assert total_kwh == pytest.approx(19.8)
    assert plan.feasible is True


def test_greedy_waehlt_guenstigste_slots():
    """Bei Preisen [10,20,5,30,15] sollten die billigsten zwei (5, 10) gewählt werden."""
    strat = LinearStrategy()
    prices = [
        _price(0, 0.10), _price(1, 0.20), _price(2, 0.05),
        _price(3, 0.30), _price(4, 0.15),
    ]
    plan = strat.plan(
        target=_target(soc=80, deadline_hours=5),
        current_soc=50,
        capacity_kwh=12.0, net_kw=10.0,  # 30% * 12kWh * 1.10 = 3.96kWh → 1 slot
        prices=prices, now=_now(),
    )
    # 1 Slot bei niedrigstem Preis 0.05
    assert len(plan.blocks) == 1
    assert plan.blocks[0].price_eur_per_kwh == pytest.approx(0.05)


def test_zusammenhaengende_slots_werden_zu_einem_block():
    strat = LinearStrategy()
    # Slot 2 und 3 sind die zwei billigsten und adjazent → ein einziger Block
    prices = [
        _price(0, 0.50), _price(1, 0.40),
        _price(2, 0.10), _price(3, 0.10),
        _price(4, 0.50),
    ]
    plan = strat.plan(
        target=_target(soc=80, deadline_hours=5),
        current_soc=50,
        capacity_kwh=60.0, net_kw=10.0,   # 30% * 60kWh * 1.10 = 19.8kWh → 2 slots
        prices=prices, now=_now(),
    )
    assert len(plan.blocks) == 1  # 1 zusammenhängender Block
    b = plan.blocks[0]
    # Block deckt exakt kwh_needed (19.8 kWh) ab, nicht 2 × 10 = 20
    assert b.expected_kwh == pytest.approx(19.8)
    assert b.start == _now() + timedelta(hours=2)
    assert b.end == _now() + timedelta(hours=4)


def test_fragmentierter_plan_warnt_bei_zu_vielen_zyklen():
    strat = LinearStrategy(max_cycles_warning=1)   # 2 Blöcke > 1 Schwelle → Warnung
    # Künstlich zerstückelt: jede zweite Stunde ist billig
    prices = [
        _price(0, 0.10), _price(1, 0.99),
        _price(2, 0.10), _price(3, 0.99),
        _price(4, 0.10), _price(5, 0.99),
        _price(6, 0.10), _price(7, 0.99),
    ]
    plan = strat.plan(
        target=_target(soc=80, deadline_hours=8),
        current_soc=50,
        capacity_kwh=60.0, net_kw=10.0,    # 30%*60*1.10 = 19.8kWh → 2 slots
        prices=prices, now=_now(),
    )
    assert len(plan.blocks) >= 2
    assert any("Zyklen" in w for w in plan.warnings)


def test_zu_wenige_slots_markiert_infeasible():
    strat = LinearStrategy()
    # nur 1 Slot verfügbar, aber 3 nötig
    prices = [_price(0, 0.20)]
    plan = strat.plan(
        target=_target(soc=100, deadline_hours=1),
        current_soc=50,
        capacity_kwh=60.0, net_kw=10.0,   # 50% * 60 * 1.10 = 33 kWh → 4 slots
        prices=prices, now=_now(),
    )
    assert plan.feasible is False
    assert any("nicht erreicht" in w for w in plan.warnings)


def test_keine_slots_vor_deadline():
    strat = LinearStrategy()
    # Alle Preise NACH Deadline → keine Slots verfügbar
    prices = [_price(h, 0.20) for h in range(10, 20)]
    plan = strat.plan(
        target=_target(soc=80, deadline_hours=5),
        current_soc=50,
        capacity_kwh=60.0, net_kw=10.0,
        prices=prices, now=_now(),
    )
    assert plan.blocks == ()
    assert plan.feasible is False
    assert any("Keine Slots" in w for w in plan.warnings)


def test_hash_stabil_bei_gleichem_plan():
    strat = LinearStrategy()
    prices = [_price(h, 0.20) for h in range(24)]
    target = _target(soc=80)
    plan_a = strat.plan(target=target, current_soc=50, capacity_kwh=60.0, net_kw=10.0,
                        prices=prices, now=_now())
    plan_b = strat.plan(target=target, current_soc=50, capacity_kwh=60.0, net_kw=10.0,
                        prices=prices, now=_now())
    assert plan_a.plan_hash == plan_b.plan_hash


def test_hash_aendert_sich_bei_preis_aenderung():
    strat = LinearStrategy()
    target = _target(soc=80)
    prices_a = [_price(h, 0.20) for h in range(24)]
    prices_b = [_price(h, 0.30) for h in range(24)]  # andere Preise
    plan_a = strat.plan(target=target, current_soc=50, capacity_kwh=60.0, net_kw=10.0,
                        prices=prices_a, now=_now())
    plan_b = strat.plan(target=target, current_soc=50, capacity_kwh=60.0, net_kw=10.0,
                        prices=prices_b, now=_now())
    assert plan_a.plan_hash != plan_b.plan_hash


def test_forecast_dominiert_loest_warnung_aus():
    strat = LinearStrategy()
    # Alle Slots Forecast
    prices = [_price(h, 0.20, source="open_meteo", confidence=0.5) for h in range(24)]
    plan = strat.plan(
        target=_target(soc=80),
        current_soc=50,
        capacity_kwh=60.0, net_kw=10.0,
        prices=prices, now=_now(),
    )
    assert any("Forecast" in w and "%" in w for w in plan.warnings)


def test_partial_allocation_teuerster_slot_traegt_rest():
    """Bei 31 kWh Bedarf wählt der Planer 4 Slots à 10 kW. Die billigsten 3 voll,
    der teuerste bekommt nur den Rest (~1 kWh). Gesamt-kWh == kwh_needed.

    User-Bug 2026-05-21: vorher wurde block_kwh als slots × kWh_per_slot gerechnet
    (= 40 kWh statt 31), was die Kosten-Anzeige ~25 % zu hoch machte.
    """
    strat = LinearStrategy()
    # Preise so wählen dass alle 4 frühen Slots gewählt werden, mit unterschiedlichem Preis
    prices = [
        _price(0, 0.15),  # billigst
        _price(1, 0.18),
        _price(2, 0.20),
        _price(3, 0.30),  # teuerst (bekommt Partial)
        _price(4, 0.50),  # nicht gewählt
    ]
    # 47% SoC-Delta * 60 kWh * 1.10 = 31.02 kWh
    plan = strat.plan(
        target=_target(soc=50, deadline_hours=5),
        current_soc=3,
        capacity_kwh=60.0, net_kw=10.0,
        prices=prices, now=_now(),
    )
    total_kwh = sum(b.expected_kwh for b in plan.blocks)
    assert total_kwh == pytest.approx(31.02, abs=1e-9)
    # Kosten: 10×0.15 + 10×0.18 + 10×0.20 + 1.02×0.30 = 1.5 + 1.8 + 2.0 + 0.306 = 5.606
    total_cost = sum(b.expected_cost_eur for b in plan.blocks)
    assert total_cost == pytest.approx(5.606, abs=1e-3)


def test_partial_allocation_uniforme_preise():
    """Mit allen Slots gleich teuer: gleicher Sub-Slot kann partial sein.
    Wichtig: Gesamt-kWh und Gesamt-Kosten bleiben korrekt unabhängig davon
    welcher Slot partial ist."""
    strat = LinearStrategy()
    prices = [_price(h, 0.20) for h in range(6)]
    plan = strat.plan(
        target=_target(soc=50, deadline_hours=6),
        current_soc=3,
        capacity_kwh=60.0, net_kw=10.0,
        prices=prices, now=_now(),
    )
    total_kwh = sum(b.expected_kwh for b in plan.blocks)
    total_cost = sum(b.expected_cost_eur for b in plan.blocks)
    assert total_kwh == pytest.approx(31.02)
    assert total_cost == pytest.approx(31.02 * 0.20, abs=1e-3)


def test_15min_granularitaet_engt_schaltfenster_ein():
    """User-Szenario 2026-05-21: SoC 3→50% mit 4 stündlichen Slots à 20ct.
    Bei slot_minutes=15 wird kein voller 4. Hour-Slot reserviert, sondern nur
    der nötige Bruchteil — Block endet bei 14:15 statt 15:00.
    """
    strat = LinearStrategy(slot_minutes=15)
    # 11-15 Uhr je 20 ct, drumherum teurer
    prices = []
    for h in range(20):
        if 11 <= h <= 14:
            prices.append(_price(h, 0.20))
        else:
            prices.append(_price(h, 0.30))
    plan = strat.plan(
        target=_target(soc=50, deadline_hours=20),
        current_soc=3,
        capacity_kwh=60.0, net_kw=10.0,
        prices=prices, now=_now(),
    )
    # kwh_needed = 47/100 * 60 * 1.10 = 31.02
    # slots_needed = ceil(31.02 / 2.5) = 13 (à 15min × 10kW = 2.5 kWh)
    # 13 × 15min = 3h15min → Block 11:00 - 14:15
    assert len(plan.blocks) == 1
    b = plan.blocks[0]
    assert b.start == _now() + timedelta(hours=11)
    assert b.end == _now() + timedelta(hours=14, minutes=15)
    assert b.expected_kwh == pytest.approx(31.02, abs=1e-9)
    assert b.expected_cost_eur == pytest.approx(31.02 * 0.20, abs=1e-3)


def test_60min_strategy_funktioniert_weiter():
    """slot_minutes=60 ist explizit konfigurierbar und liefert das alte Verhalten."""
    strat = LinearStrategy(slot_minutes=60)
    prices = [_price(h, 0.20) for h in range(24)]
    plan = strat.plan(
        target=_target(soc=80), current_soc=50,
        capacity_kwh=60.0, net_kw=10.0,
        prices=prices, now=_now(),
    )
    # 19.8 kWh → ceil(19.8/10) = 2 60-min-Slots
    total_kwh = sum(b.expected_kwh for b in plan.blocks)
    assert total_kwh == pytest.approx(19.8)
    # Block ist 2 Stunden lang
    for b in plan.blocks:
        duration_h = (b.end - b.start).total_seconds() / 3600
        assert duration_h == pytest.approx(2.0)


def test_expand_to_slot_hourly_zu_4_sub_slots():
    """Hourly-Input wird zu 4×15-min-Sub-Slots gleichen Preises expandiert."""
    strat = LinearStrategy(slot_minutes=15)
    prices = [_price(0, 0.20), _price(1, 0.30)]
    expanded = strat._expand_to_slot_granularity(prices)
    assert len(expanded) == 8  # 2 × 4
    # Erste 4 Sub-Slots gehören zum h=0-Preis
    for ts, p in expanded[:4]:
        assert p.total == 0.20
    # Sub-Slot-Zeiten sind 15 min auseinander
    for i in range(1, 8):
        delta = (expanded[i][0] - expanded[i - 1][0]).total_seconds() / 60
        assert delta == 15


def test_expand_zu_native_keine_expansion():
    """Wenn Native bereits == slot_minutes, keine Vermehrung."""
    strat = LinearStrategy(slot_minutes=15)
    # 15-min-Spacing simulieren
    base = _now() + timedelta(hours=10)
    prices = [
        UnifiedPrice(startsAt=(base + timedelta(minutes=i * 15)).isoformat(),
                     total=0.20, level="NORMAL", source="tibber", confidence=1.0)
        for i in range(4)
    ]
    expanded = strat._expand_to_slot_granularity(prices)
    assert len(expanded) == 4


def test_expand_leerer_input():
    strat = LinearStrategy(slot_minutes=15)
    assert strat._expand_to_slot_granularity([]) == []


def test_to_dict_roundtrip():
    strat = LinearStrategy()
    prices = [_price(h, 0.20) for h in range(24)]
    plan = strat.plan(target=_target(soc=80), current_soc=50, capacity_kwh=60.0,
                      net_kw=10.0, prices=prices, now=_now())
    d = plan.to_dict()
    assert d["target_event_id"] == "evt1"
    assert d["plan_hash"] == plan.plan_hash
    assert len(d["blocks"]) == len(plan.blocks)
