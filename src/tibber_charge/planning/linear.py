"""Lineares V1-Lademodell: konstante Wallbox-Power, ignoriert BMS-Throttling.

Genauigkeit kompensiert durch:
- Puffer-Faktor (Default +10 %), der etwas mehr lädt als rechnerisch nötig.
- 15-Minuten-Re-Planung — der reale SoC wird jeden Lauf neu gelesen, Pläne
  passen sich automatisch an Modellfehler an.

Block-Bonus für Fragmentierungs-Vermeidung ist bewusst NICHT eingebaut. Der
Cycle-Counter im ChargePlan dient als Warn-Mechanismus (max_cycles_warning
in der Config). Algorithmische Block-Präferenz wird V1.5+.
"""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timedelta

from dateutil import parser as date_parser

from tibber_charge.gcal.charge_targets import ChargeTarget
from tibber_charge.planning.types import ChargeBlock, ChargePlan
from tibber_charge.prices.types import UnifiedPrice


class LinearStrategy:
    name = "linear"

    def __init__(
        self,
        buffer_factor: float = 1.10,
        slot_minutes: int = 15,
        max_cycles_warning: int = 3,
    ) -> None:
        self.buffer_factor = buffer_factor
        self.slot_minutes = slot_minutes
        self.max_cycles_warning = max_cycles_warning

    def plan(
        self,
        target: ChargeTarget,
        current_soc: int,
        capacity_kwh: float,
        net_kw: float,
        prices: list[UnifiedPrice],
        now: datetime,
    ) -> ChargePlan:
        warnings: list[str] = []

        # 1) Benötigte Energie inkl. Puffer
        soc_delta = max(0, target.target_soc - current_soc)
        kwh_needed = (soc_delta / 100.0) * capacity_kwh * self.buffer_factor

        if soc_delta == 0:
            return self._empty_plan(target, current_soc, capacity_kwh, net_kw, kwh_needed,
                                    warnings=["Ziel-SoC bereits erreicht — nichts zu tun."])

        if net_kw <= 0:
            warnings.append("WALLBOX.net_kw ≤ 0 — keine Planung möglich.")
            return self._empty_plan(target, current_soc, capacity_kwh, net_kw, kwh_needed, warnings)

        # 2) Anzahl benötigter Slots (in slot_minutes-Granularität)
        hours_per_slot = self.slot_minutes / 60.0
        kwh_per_slot = net_kw * hours_per_slot
        slots_needed = math.ceil(kwh_needed / kwh_per_slot)

        # 3) Preise auf Sub-Slot-Granularität expandieren.
        # Tibber/Open-Meteo liefern typischerweise stündlich; mit slot_minutes=15
        # zerlegen wir jeden Preis-Eintrag in 4 Sub-Slots gleichen Preises. Damit
        # kann der Allokator Schaltfenster auf 15-min-Ebene zuschneiden statt
        # ganze Stunden zu blockieren.
        expanded = self._expand_to_slot_granularity(prices)

        # 4) Verfügbare Slots zwischen now (auf Stundenanfang gerundet) und deadline
        deadline = target.deadline
        cutoff = now.replace(minute=0, second=0, microsecond=0)
        available = [(ts, p) for ts, p in expanded if cutoff <= ts < deadline]

        if not available:
            warnings.append(f"Keine Slots zwischen jetzt und Deadline {deadline.isoformat()}.")
            return self._empty_plan(target, current_soc, capacity_kwh, net_kw, kwh_needed, warnings)

        # 4) Greedy: günstigste Slots wählen
        sorted_by_price = sorted(available, key=lambda tp: tp[1].total)
        chosen = sorted_by_price[:slots_needed]
        feasible = len(chosen) >= slots_needed
        if not feasible:
            warnings.append(
                f"Nur {len(chosen)} von {slots_needed} Slots verfügbar — Ziel-SoC wird voraussichtlich nicht erreicht."
            )

        # 5) kWh cheapest-first auf Slots verteilen.
        # Hintergrund: ceil() reserviert oft einen Slot mehr als nötig (z.B. 31 kWh →
        # 4 Slots à 10 kW = 40 kWh Kapazität). Das Auto stoppt bei Ziel-SoC, also
        # gibt der teuerste der gewählten Slots nur seinen anteiligen Beitrag her.
        # Verteilung in Preis-Reihenfolge: günstigste zuerst voll, der teuerste partial.
        kwh_alloc: dict[datetime, float] = {}
        remaining = kwh_needed if feasible else (len(chosen) * kwh_per_slot)
        for ts, _price in chosen:  # bereits nach Preis sortiert
            take = min(kwh_per_slot, remaining)
            kwh_alloc[ts] = take
            remaining -= take
            if remaining <= 1e-9:
                break

        # 6) Sortiere chronologisch und mergen-zu-Blöcken
        chosen.sort(key=lambda tp: tp[0])
        blocks = self._merge_into_blocks(chosen, kwh_alloc)

        if len(blocks) > self.max_cycles_warning:
            warnings.append(
                f"⚠️ Ladeplan enthält {len(blocks)} Start/Stopp-Zyklen "
                f"(> {self.max_cycles_warning}). Häufiges Schalten belastet die Wallbox-Schütze."
            )

        # Wenn die Forecast-Anteile dominieren, eine sanfte Konfidenz-Warnung.
        forecast_kwh = sum(b.expected_kwh for b in blocks if b.source != "tibber")
        if forecast_kwh > 0:
            ratio = forecast_kwh / sum(b.expected_kwh for b in blocks) if blocks else 0
            if ratio > 0.5:
                warnings.append(
                    f"⚠️ {ratio*100:.0f} % der Ladung beruht auf Forecast-Schätzungen — "
                    "tatsächliche Kosten/Verfügbarkeit können abweichen."
                )

        plan_hash = self._hash(target, blocks)
        return ChargePlan(
            target_event_id=target.source_event_id,
            target_soc=target.target_soc,
            deadline=deadline,
            current_soc=current_soc,
            capacity_kwh=capacity_kwh,
            net_kw=net_kw,
            kwh_needed=kwh_needed,
            blocks=tuple(blocks),
            feasible=feasible,
            plan_hash=plan_hash,
            warnings=tuple(warnings),
        )

    def _merge_into_blocks(
        self,
        chosen: list[tuple[datetime, UnifiedPrice]],
        kwh_alloc: dict[datetime, float],
    ) -> list[ChargeBlock]:
        """Aggregiert chronologisch sortierte Slots zu zusammenhängenden Blöcken.
        Zwei Slots gelten als zusammenhängend, wenn sie direkt aneinander grenzen.
        """
        if not chosen:
            return []
        slot_delta = timedelta(minutes=self.slot_minutes)
        blocks: list[ChargeBlock] = []
        run: list[tuple[datetime, UnifiedPrice]] = [chosen[0]]
        for ts, p in chosen[1:]:
            prev_ts, _ = run[-1]
            if ts - prev_ts == slot_delta:
                run.append((ts, p))
            else:
                blocks.append(self._flush_run(run, kwh_alloc))
                run = [(ts, p)]
        blocks.append(self._flush_run(run, kwh_alloc))
        return blocks

    def _flush_run(
        self,
        run: list[tuple[datetime, UnifiedPrice]],
        kwh_alloc: dict[datetime, float],
    ) -> ChargeBlock:
        slot_delta = timedelta(minutes=self.slot_minutes)
        start = run[0][0]
        end = run[-1][0] + slot_delta

        # Echte kWh-Allokation (cheapest-first verteilt), nicht slots × kWh_per_slot
        block_kwh = sum(kwh_alloc.get(ts, 0.0) for ts, _ in run)
        # Gewichteter Preis: jeder Slot-Preis mit seiner allokierten kWh gewichten
        if block_kwh > 0:
            weighted_price = (
                sum(p.total * kwh_alloc.get(ts, 0.0) for ts, p in run) / block_kwh
            )
        else:
            weighted_price = run[0][1].total
        cost = block_kwh * weighted_price
        # Source: wenn alle gleich → einzeln; sonst "mixed"
        sources = {p.source for _, p in run}
        source = next(iter(sources)) if len(sources) == 1 else "mixed"
        # Confidence: min im Run (vorsichtig)
        confidence = min(p.confidence for _, p in run)
        return ChargeBlock(
            start=start,
            end=end,
            expected_kwh=block_kwh,
            expected_cost_eur=cost,
            price_eur_per_kwh=weighted_price,
            source=source,
            confidence=confidence,
        )

    def _hash(self, target: ChargeTarget, blocks: list[ChargeBlock]) -> str:
        h = hashlib.md5()
        h.update(f"{target.source_event_id}|{target.target_soc}|{target.deadline.isoformat()}".encode())
        for b in blocks:
            h.update(
                f"|{b.start.isoformat()}|{b.end.isoformat()}|{round(b.price_eur_per_kwh, 4)}".encode()
            )
        return h.hexdigest()

    def _expand_to_slot_granularity(
        self,
        prices: list[UnifiedPrice],
    ) -> list[tuple[datetime, UnifiedPrice]]:
        """Expandiert Preise auf `self.slot_minutes`-Granularität.

        Detektiert die native Auflösung aus dem Spacing der ersten zwei Preise.
        Wenn größer als `slot_minutes` → jeder Preis-Eintrag wird in N=native/slot
        Sub-Slots gleichen Preises aufgeteilt. Wenn gleich oder kleiner → keine
        Expansion, Original-Timestamps werden geparst und durchgereicht.

        Liefert eine Liste `[(slot_start, price)]` sortiert nach `slot_start`.
        """
        if not prices:
            return []

        sorted_prices = sorted(prices, key=lambda p: p.startsAt)

        # Native Granularität bestimmen
        native_minutes = self.slot_minutes
        if len(sorted_prices) >= 2:
            try:
                t0 = date_parser.isoparse(sorted_prices[0].startsAt)
                t1 = date_parser.isoparse(sorted_prices[1].startsAt)
                delta_min = (t1 - t0).total_seconds() / 60
                if delta_min > 0:
                    native_minutes = max(self.slot_minutes, int(round(delta_min)))
            except (ValueError, TypeError):
                native_minutes = 60

        result: list[tuple[datetime, UnifiedPrice]] = []
        if native_minutes <= self.slot_minutes:
            for p in sorted_prices:
                try:
                    ts = date_parser.isoparse(p.startsAt)
                except (ValueError, TypeError):
                    continue
                result.append((ts, p))
            return result

        n_subs = native_minutes // self.slot_minutes
        for p in sorted_prices:
            try:
                start = date_parser.isoparse(p.startsAt)
            except (ValueError, TypeError):
                continue
            for j in range(n_subs):
                sub_ts = start + timedelta(minutes=j * self.slot_minutes)
                result.append((sub_ts, p))
        return result

    def _empty_plan(
        self,
        target: ChargeTarget,
        current_soc: int,
        capacity_kwh: float,
        net_kw: float,
        kwh_needed: float,
        warnings: list[str],
    ) -> ChargePlan:
        return ChargePlan(
            target_event_id=target.source_event_id,
            target_soc=target.target_soc,
            deadline=target.deadline,
            current_soc=current_soc,
            capacity_kwh=capacity_kwh,
            net_kw=net_kw,
            kwh_needed=kwh_needed,
            blocks=(),
            feasible=current_soc >= target.target_soc,
            plan_hash=hashlib.md5(
                f"{target.source_event_id}|empty|{current_soc}".encode()
            ).hexdigest(),
            warnings=tuple(warnings),
        )
