#!/usr/bin/env python3
"""ADR-015/016: health_mult on compute_effective_price (merchant wire-in)."""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pricing_engine as pe  # noqa: E402


def _price(**kw):
    return pe.compute_effective_price(base_rate=1.0, provider="deepinfra",
                                      quota_pct=0.0, hour_utc=12, **kw)


def test_health_mult_scales_price():
    base = _price()
    twice = _price(health_mult=2.0)
    assert abs(twice - 2.0 * base) < 1e-9


def test_health_mult_inf_is_unreachable():
    assert math.isinf(_price(health_mult=float("inf")))


def test_health_mult_bad_input_neutral():
    base = _price()
    for bad in (0.0, -1.0, float("nan")):
        assert abs(_price(health_mult=bad) - base) < 1e-9, bad
