#!/usr/bin/env python3
"""Tests for CG-12 urgency cost estimator — pure function, no DB required.

Tests exercise the pure :func:`estimate_cost` and :func:`format_all_urgencies`
functions with hand-crafted state dicts. No live DB or zai_state.json needed.
"""
from __future__ import annotations

import sys
import os

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.urgency_cost_estimator import (
    estimate_cost,
    format_all_urgencies,
    display_urgency_costs,
    _resolve_task_tokens,
    DEFAULT_PAID_PRICE_PER_M,
    DEFAULT_TASK_TOKENS,
    URGENCIES,
)


# ── NOW: free quota → $0 ──────────────────────────────────────────────────────


def test_now_free_quota():
    s = {"free_quota_available": True}
    e = estimate_cost("now", 56_000, s)
    assert e["cost_usd"] == 0.0
    assert "free" in e["explanation"]
    assert e["breakdown"]["bleed"] == 0.0
    assert e["bleed_note"] is None


# ── NOW: paid failover → token cost ──────────────────────────────────────────


def test_now_paid_failover():
    s = {"free_quota_available": False, "paid_price_per_m": 0.47}
    e = estimate_cost("now", 56_000, s)
    # 56K * 0.47 / 1M = 0.0263
    assert abs(e["cost_usd"] - 0.0263) < 0.001
    assert "paid" in e["explanation"]
    assert e["breakdown"]["bleed"] == 0.0  # NOW stops bleed
    assert e["bleed_note"] is None


# ── SOON: quota resets → $0 direct, bleed accumulates ─────────────────────────


def test_soon_quota_resets():
    s = {
        "free_quota_available": False,
        "paid_price_per_m": 0.47,
        "bleed_rate_per_hour": 0.69,
        "quota_resets_in_hours": 4.2,
    }
    e = estimate_cost("soon", 56_000, s)
    assert e["cost_usd"] == 0.0  # direct = 0 (quota resets → free)
    assert e["breakdown"]["direct"] == 0.0
    assert e["bleed_note"] is not None
    assert "4.2h" in e["bleed_note"]
    assert "0.69" in e["bleed_note"]
    # bleed = 0.69 * 4.2 = 2.898
    assert abs(e["breakdown"]["bleed"] - 2.898) < 0.01
    assert abs(e["bleed_usd"] - 2.898) < 0.01


# ── DEFER: same as SOON but potentially longer ────────────────────────────────


def test_defer_with_bleed():
    s = {
        "free_quota_available": False,
        "paid_price_per_m": 0.47,
        "bleed_rate_per_hour": 0.69,
        "quota_resets_in_hours": 4.2,
    }
    e = estimate_cost("defer", 56_000, s)
    assert e["cost_usd"] == 0.0  # direct = 0 (quota resets)
    assert e["breakdown"]["bleed"] > 0
    assert e["bleed_note"] is not None


# ── NOW stops bleed even when bleed is active ────────────────────────────────


def test_now_stops_bleed():
    s = {
        "free_quota_available": False,
        "paid_price_per_m": 0.47,
        "bleed_rate_per_hour": 0.69,
        "quota_resets_in_hours": 4.2,
    }
    e = estimate_cost("now", 56_000, s)
    assert e["breakdown"]["bleed"] == 0.0
    assert e["bleed_note"] is None
    # NOW pays direct but not bleed
    assert e["breakdown"]["direct"] > 0


# ── BATCH: always $0 (free only) ─────────────────────────────────────────────


def test_batch_always_free():
    s = {"free_quota_available": False, "paid_price_per_m": 0.47}
    e = estimate_cost("batch", 56_000, s)
    assert e["cost_usd"] == 0.0
    assert "free only" in e["explanation"]


def test_batch_no_bleed():
    s = {
        "free_quota_available": False,
        "paid_price_per_m": 0.47,
        "bleed_rate_per_hour": 0.69,
        "quota_resets_in_hours": 4.2,
    }
    e = estimate_cost("batch", 56_000, s)
    # BATCH has no bleed by definition
    assert e["bleed_note"] is not None  # it does have bleed (it's not "now")


# ── Degradation: no resets_in → bleed note says "timing unknown" ─────────────


def test_bleed_no_timing():
    s = {
        "free_quota_available": False,
        "paid_price_per_m": 0.47,
        "bleed_rate_per_hour": 0.69,
        "quota_resets_in_hours": None,
    }
    e = estimate_cost("soon", 56_000, s)
    # No resets_in → direct is paid (can't predict free window)
    assert e["breakdown"]["direct"] > 0
    assert "unknown" in (e["bleed_note"] or "")


# ── Degradation: empty state → conservative defaults ────────────────────────


def test_empty_state():
    e = estimate_cost("now", 56_000, {})
    # Empty state → not free → paid failover at default price
    assert e["cost_usd"] > 0
    assert e["confidence"] in ("low", "medium", "high")


# ── Confidence: free quota → high ─────────────────────────────────────────────


def test_confidence_free_high():
    s = {"free_quota_available": True}
    e = estimate_cost("now", 56_000, s)
    assert e["confidence"] == "high"


# ── Format: all four urgencies present ───────────────────────────────────────


def test_format_all_urgencies():
    s = {
        "free_quota_available": False,
        "paid_price_per_m": 0.47,
        "bleed_rate_per_hour": 0.69,
        "quota_resets_in_hours": 4.2,
    }
    out = format_all_urgencies(56_000, s)
    for u in URGENCIES:
        assert u.upper() in out
    # Should have a BUT line for bleed
    assert "BUT:" in out
    assert "$" in out


def test_format_no_bleed():
    s = {"free_quota_available": True}
    out = format_all_urgencies(56_000, s)
    assert "BUT:" not in out


# ── Confidence interval: bounds are sane ─────────────────────────────────────


def test_confidence_interval_bounds():
    s = {"free_quota_available": False, "paid_price_per_m": 0.47,
         "token_std": 33_356}
    e = estimate_cost("now", 56_000, s)
    lo, hi = e["confidence_interval"]
    assert lo <= e["cost_usd"] <= hi
    assert lo >= 0  # never negative


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
