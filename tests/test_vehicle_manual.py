"""Tests für ManualVehicleProvider."""
from __future__ import annotations

from tibber_charge.vehicle.manual import ManualVehicleProvider


def test_liefert_konfigurierte_werte():
    p = ManualVehicleProvider("Model 3", soc=42, is_connected=True, is_charging=False)
    s = p.get_status()
    assert s.soc == 42
    assert s.is_connected is True
    assert s.is_charging is False
    assert s.vehicle_name == "Model 3"
    assert s.source == "manual"
    assert s.last_updated_at is not None


def test_clamping_soc_oben():
    p = ManualVehicleProvider("X", soc=150, is_connected=False)
    assert p.get_status().soc == 100


def test_clamping_soc_unten():
    p = ManualVehicleProvider("X", soc=-10, is_connected=False)
    assert p.get_status().soc == 0


def test_to_dict_roundtrip_kompatibel():
    p = ManualVehicleProvider("X", soc=55, is_connected=True, is_charging=True)
    d = p.get_status().to_dict()
    assert d["soc"] == 55
    assert d["is_connected"] is True
    assert d["is_charging"] is True
    assert d["source"] == "manual"
