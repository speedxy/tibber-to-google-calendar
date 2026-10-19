"""Pytest-Bootstrap: src/ in sys.path damit `tibber_charge.*` importierbar ist
ohne `pip install -e .`. Hält die Cron-Pfad-Stabilität intakt.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
