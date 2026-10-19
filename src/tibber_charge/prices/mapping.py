"""Selbstlernende Umrechnung Prognose → Tibber-Endpreis.

Der Tibber-Preis ist linear im Börsenpreis: Fixkosten (Netzentgelt, Steuern,
Umlagen, Tibber-Aufschlag) plus MwSt-Faktor, im Oktober 2026 gemessen
`0.199 + 1.19 × spot`. Diese Werte ändern sich (Netzentgelt zum Jahreswechsel,
MwSt), deshalb werden `a` und `b` bei jedem Lauf per kleinster Quadrate aus
allen Stunden bestimmt, für die es Tibber-Preis UND Prognose gibt.

Für die Wetter-Heuristik (Open-Meteo) funktioniert dieselbe Formel als
Niveau- und Spreizungs-Korrektur: dort liegt `b` typischerweise weit unter 1,
weil die Heuristik nur einen Teil der Preisschwankung erklärt.

Gibt es zu wenige Vergleichsstunden (z.B. mittags vor Tibbers Day-Ahead-
Veröffentlichung), wird die zuletzt gelernte Umrechnung derselben Quelle
wiederverwendet (persistiert in `state.forecast.price_mapping`).
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from dateutil import parser as date_parser

from tibber_charge.prices.types import UnifiedPrice


# Mindestzahl an Stunden mit Tibber- UND Prognosewert für einen neuen Fit.
MIN_OVERLAP = 12
# Plausibler Bereich für den Faktor b. Außerhalb (z.B. Ausreißer, falsche
# Einheit) wird nur das Niveau verschoben (b = 1).
_B_RANGE = (0.0, 3.0)


@dataclass(frozen=True)
class PriceMapping:
    a: float            # €/kWh Fixanteil
    b: float            # Faktor auf den Prognose-Rohwert
    samples: int        # Anzahl Vergleichsstunden des Fits
    source: str         # Prognose-Provider, für den der Fit gilt
    fitted_at: str      # ISO-Zeitpunkt des Fits

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "PriceMapping | None":
        if not d:
            return None
        try:
            return cls(
                a=float(d["a"]), b=float(d["b"]), samples=int(d["samples"]),
                source=str(d["source"]), fitted_at=str(d["fitted_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def hour_key(starts_at: str) -> str | None:
    """Stundenschlüssel unabhängig von der ISO-Schreibweise (".000", Offset)."""
    try:
        ts = date_parser.isoparse(starts_at)
    except (ValueError, TypeError):
        return None
    return ts.astimezone().replace(minute=0, second=0, microsecond=0).isoformat()


def fit_mapping(
    tibber_prices: list[dict],
    forecast_prices: list[UnifiedPrice],
    now: datetime,
) -> PriceMapping | None:
    """Fittet `tibber = a + b × raw` über alle überlappenden Stunden."""
    if not forecast_prices:
        return None
    tibber_by_hour: dict[str, float] = {}
    for p in tibber_prices:
        key = hour_key(p.get("startsAt", ""))
        if key is not None and p.get("total") is not None:
            tibber_by_hour[key] = float(p["total"])

    xs: list[float] = []
    ys: list[float] = []
    for up in forecast_prices:
        key = hour_key(up.startsAt)
        if key in tibber_by_hour:
            xs.append(up.raw_total if up.raw_total is not None else up.total)
            ys.append(tibber_by_hour[key])
    if len(xs) < MIN_OVERLAP:
        return None

    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    b = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var_x if var_x > 1e-9 else None
    if b is None or not (_B_RANGE[0] <= b <= _B_RANGE[1]):
        # Kein sinnvoller Anstieg schätzbar: nur das Niveau verschieben.
        b = 1.0
        a = statistics.median(y - x for x, y in zip(xs, ys))
    else:
        a = mean_y - b * mean_x

    return PriceMapping(
        a=a, b=b, samples=len(xs), source=forecast_prices[0].source, fitted_at=now.isoformat(),
    )


def resolve_mapping(
    fitted: PriceMapping | None,
    stored: PriceMapping | None,
    source: str | None,
) -> PriceMapping | None:
    """Neuer Fit gewinnt, sonst die gespeicherte Umrechnung derselben Quelle."""
    if fitted is not None:
        return fitted
    if stored is not None and stored.source == source:
        return stored
    return None


def apply_mapping(forecast_prices: list[UnifiedPrice], mapping: PriceMapping) -> list[UnifiedPrice]:
    """Rechnet Rohwerte in Tibber-Endpreise um. Der Risikoaufschlag bleibt erhalten."""
    result = []
    for up in forecast_prices:
        raw = up.raw_total if up.raw_total is not None else up.total
        penalty = up.total - raw
        mapped = mapping.a + mapping.b * raw
        result.append(replace(up, total=mapped + penalty, raw_total=mapped))
    return result


def log_mapping(mapping: PriceMapping, fresh: bool) -> None:
    herkunft = f"neu gelernt aus {mapping.samples} Vergleichsstunden" if fresh else f"gespeichert vom {mapping.fitted_at[:16]}"
    logging.info(
        f"📐 Prognose-Umrechnung ({mapping.source}): Tibber-Preis = "
        f"{mapping.a*100:.1f} ct + {mapping.b:.2f} × Prognose ({herkunft})."
    )
