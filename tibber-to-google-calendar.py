#!/usr/bin/env python3
"""Cron-Entry-Point. Die Logik liegt in `src/tibber_charge/`."""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "src"))

from tibber_charge.main import run  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tibber-Preise als Google-Calendar-Events synchronisieren.")
    p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log-Level setzen (Standard: WARNING).",
    )
    p.add_argument(
        "--debug",
        action="store_const",
        const="DEBUG",
        dest="log_level",
        help="Shortcut für --log-level DEBUG.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(base_dir=_HERE, log_level=args.log_level)
