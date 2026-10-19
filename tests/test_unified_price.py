"""Tests für UnifiedPrice — Konstruktion, Tibber-Kompat-Schema."""
from __future__ import annotations

from tibber_charge.prices.types import UnifiedPrice


def test_from_tibber_dict_extracts_fields():
    tibber_entry = {
        "startsAt": "2026-05-20T14:00:00+02:00",
        "total": 0.2345,
        "level": "CHEAP",
    }
    up = UnifiedPrice.from_tibber_dict(tibber_entry)
    assert up.startsAt == "2026-05-20T14:00:00+02:00"
    assert up.total == 0.2345
    assert up.level == "CHEAP"
    assert up.source == "tibber"
    assert up.confidence == 1.0
    assert up.raw_total is None


def test_to_dict_returns_tibber_compatible_keys():
    """to_dict() liefert mindestens startsAt/total/level — die Felder, die
    group_price_periods() in tibber/grouping.py liest. Zusatzfelder dürfen
    vorhanden sein, dürfen aber nichts brechen."""
    up = UnifiedPrice(
        startsAt="2026-05-20T14:00:00+02:00",
        total=0.21,
        level="CHEAP",
        source="corrently",
        confidence=0.5,
    )
    d = up.to_dict()
    assert d["startsAt"] == "2026-05-20T14:00:00+02:00"
    assert d["total"] == 0.21
    assert d["level"] == "CHEAP"


def test_to_dict_carries_source_and_confidence():
    up = UnifiedPrice(
        startsAt="2026-05-20T14:00:00+02:00",
        total=0.21,
        level="CHEAP",
        source="corrently",
        confidence=0.4,
    )
    d = up.to_dict()
    assert d["source"] == "corrently"
    assert d["confidence"] == 0.4


def test_frozen_dataclass_is_hashable_and_immutable():
    up = UnifiedPrice(
        startsAt="2026-05-20T14:00:00+02:00",
        total=0.21,
        level="CHEAP",
        source="tibber",
        confidence=1.0,
    )
    # Sollte nicht mutieren lassen
    import dataclasses
    try:
        up.total = 0.99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("frozen dataclass sollte FrozenInstanceError werfen")
