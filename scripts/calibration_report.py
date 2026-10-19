#!/usr/bin/env python3
"""Adhoc-Auswertung der Forecast-Kalibrierungs-Samples in state.json.

Aufruf:
    python scripts/calibration_report.py
    python scripts/calibration_report.py /pfad/zur/state.json

Druckt:
- Gesamt-MAE und Median-Bias zwischen Forecast und realem Tibber-Preis
- Aufschlüsselung nach Forecast-Horizont (Buckets)
- Aufschlüsselung nach Preis-Bereich (für Anker-Justierung)
"""
from __future__ import annotations

import json
import os
import statistics
import sys


def _load_samples(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    samples = state.get("forecast", {}).get("calibration_samples", [])
    return [s for s in samples if s.get("actual_total") is not None]


def _bucket_horizon(h: float) -> str:
    if h < 36:
        return "<36 h"
    if h < 48:
        return "36-48 h"
    if h < 96:
        return "48-96 h"
    if h < 168:
        return "96-168 h"
    return ">168 h"


def _bucket_price(actual_eur: float) -> str:
    if actual_eur < 0.15:
        return "<15 ct"
    if actual_eur < 0.20:
        return "15-20 ct"
    if actual_eur < 0.25:
        return "20-25 ct"
    if actual_eur < 0.30:
        return "25-30 ct"
    if actual_eur < 0.40:
        return "30-40 ct"
    return ">40 ct"


def _summary(label: str, samples: list[dict]) -> None:
    if not samples:
        print(f"  {label:18s}  (keine Daten)")
        return
    errors_raw = [s["forecast_raw_total"] - s["actual_total"] for s in samples
                  if s.get("forecast_raw_total") is not None]
    errors_total = [s["forecast_total"] - s["actual_total"] for s in samples]
    mae_raw = statistics.mean(abs(e) for e in errors_raw) if errors_raw else 0.0
    mae_total = statistics.mean(abs(e) for e in errors_total)
    bias_raw = statistics.median(errors_raw) if errors_raw else 0.0
    bias_total = statistics.median(errors_total)
    print(
        f"  {label:18s}  n={len(samples):3d}   "
        f"MAE raw={mae_raw*100:5.1f}ct  MAE total={mae_total*100:5.1f}ct   "
        f"Bias raw={bias_raw*100:+5.1f}ct  Bias total={bias_total*100:+5.1f}ct"
    )


def main(state_path: str) -> int:
    if not os.path.exists(state_path):
        print(f"❌ state.json nicht gefunden: {state_path}", file=sys.stderr)
        return 1

    samples = _load_samples(state_path)
    if not samples:
        print("ℹ️ Keine gefüllten Kalibrierungs-Samples in state.json.")
        print("   Sammeln dauert mind. 1-2 Tage Live-Betrieb nach Phase 4b PR 4.")
        return 0

    print(f"\n📊 Kalibrierungs-Report — {len(samples)} matched Forecast/Tibber-Pairs")
    print("=" * 84)

    # Pro Quelle getrennt: Open-Meteo-Samples vor 2026-10-19 enthalten noch
    # unumgerechnete Rohwerte, EpexPredictor-Samples umgerechnete Endpreise.
    by_source: dict[str, list[dict]] = {}
    for s in samples:
        by_source.setdefault(s.get("forecast_source") or "unbekannt", []).append(s)

    for source, group in sorted(by_source.items()):
        print(f"\n### Quelle: {source}")
        print("\nGesamt:")
        _summary("Alles", group)

        print("\nNach Forecast-Horizont (Stunden bei Vorhersage):")
        by_horizon: dict[str, list[dict]] = {}
        for s in group:
            bucket = _bucket_horizon(s.get("hours_ahead_at_forecast", 0))
            by_horizon.setdefault(bucket, []).append(s)
        for bucket in ("<36 h", "36-48 h", "48-96 h", "96-168 h", ">168 h"):
            _summary(bucket, by_horizon.get(bucket, []))

        print("\nNach realem Preis (Tibber-Wert):")
        by_price: dict[str, list[dict]] = {}
        for s in group:
            bucket = _bucket_price(s["actual_total"])
            by_price.setdefault(bucket, []).append(s)
        for bucket in ("<15 ct", "15-20 ct", "20-25 ct", "25-30 ct", "30-40 ct", ">40 ct"):
            _summary(bucket, by_price.get(bucket, []))

    print("\nLesehilfe:")
    print("  MAE = Mean Absolute Error (durchschnittlicher Fehler, immer positiv)")
    print("  Bias = Median-Fehler (positiv = wir prognostizieren zu hoch, neg = zu niedrig)")
    print("  raw = umgerechneter Preis ohne Risikoaufschlag, total = inkl. Risikoaufschlag")
    print("  (Open-Meteo-Samples vor dem 19.10.2026: raw = unumgerechnete Heuristik)")
    print("\nDie Umrechnung Prognose → Tibber-Preis lernt das Skript selbst (prices/mapping.py).")
    print("  Der Report dient zur Kontrolle und als Basis für einen gemessenen Risikoaufschlag.")
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "state.json",
    )
    raise SystemExit(main(path))
