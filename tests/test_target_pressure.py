#!/usr/bin/env python3
"""ADR-016: target-pressure quota controller (bisection).

Run:
  /usr/bin/python3 -m pytest tests/test_target_pressure.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import target_pressure as tp  # noqa: E402


def test_burn_at_basic():
    assert tp.burn_at(1.0, 100.0, 0.0) == 100.0
    assert tp.burn_at(2.0, 100.0, 1.0) == 50.0
    assert tp.burn_at(0.0, 100.0, 0.0) == 100.0  # clamp, no div-by-zero
    assert tp.burn_at(2.0, 0.0, 1.0) == 0.0


def test_degenerate_inputs_return_unchanged():
    assert tp.solve_multiplier(quota_used=0, quota_total=0, burn_rate=100,
                               time_remaining_hours=1, elasticity=0.5) == 1.0
    assert tp.solve_multiplier(quota_used=1, quota_total=100, burn_rate=0,
                               time_remaining_hours=1, elasticity=0.5) == 1.0
    assert tp.solve_multiplier(quota_used=1, quota_total=100, burn_rate=10,
                               time_remaining_hours=0, elasticity=0.5) == 1.0


def test_over_target_raises_price_to_hit_reset():
    m = tp.solve_multiplier(quota_used=800, quota_total=1000, burn_rate=400,
                            time_remaining_hours=1.0, elasticity=0.5)
    assert abs(m - 4.0) < 0.05, m
    proj = tp.projected_usage(m, quota_used=800, burn_rate=400,
                              time_remaining_hours=1.0, elasticity=0.5)
    assert abs(proj - 1000.0) < 2.0, proj


def test_under_target_lowers_price():
    m = tp.solve_multiplier(quota_used=100, quota_total=1000, burn_rate=100,
                            time_remaining_hours=2.0, elasticity=0.5)
    assert m == tp.M_LO_DEFAULT


def test_price_insensitive_cannot_shed_returns_ceiling():
    m = tp.solve_multiplier(quota_used=800, quota_total=1000, burn_rate=400,
                            time_remaining_hours=1.0, elasticity=0.0)
    assert m == tp.M_HI_DEFAULT


def test_already_at_quota_returns_ceiling():
    m = tp.solve_multiplier(quota_used=1000, quota_total=1000, burn_rate=10,
                            time_remaining_hours=1.0, elasticity=0.5)
    assert m == tp.M_HI_DEFAULT


def test_monotone_in_usage():
    lo = tp.solve_multiplier(quota_used=400, quota_total=1000, burn_rate=400,
                             time_remaining_hours=1.0, elasticity=0.5)
    hi = tp.solve_multiplier(quota_used=850, quota_total=1000, burn_rate=400,
                             time_remaining_hours=1.0, elasticity=0.5)
    assert hi > lo, (lo, hi)


def test_multiplier_for_window_matches_anchor():
    now = 1_000_000.0
    resets_at = now + 3600.0
    m = tp.multiplier_for_window(quota_used=800, quota_total=1000, burn_rate=400,
                                 resets_at=resets_at, now=now, window_hours=5.0,
                                 elasticity=0.5)
    direct = tp.solve_multiplier(quota_used=800, quota_total=1000, burn_rate=400,
                                 time_remaining_hours=1.0, elasticity=0.5)
    assert abs(m - direct) < 1e-6


def test_multiplier_for_window_clamps_to_window_length():
    m = tp.multiplier_for_window(quota_used=0, quota_total=1000, burn_rate=10,
                                 resets_at=10 * 3600.0, now=0.0, window_hours=5.0,
                                 elasticity=0.5)
    direct = tp.solve_multiplier(quota_used=0, quota_total=1000, burn_rate=10,
                                 time_remaining_hours=5.0, elasticity=0.5)
    assert abs(m - direct) < 1e-9


def test_multiplier_for_window_bad_input_returns_one():
    assert tp.multiplier_for_window(quota_used=1, quota_total=100, burn_rate=10,
                                    resets_at=10, now=0, window_hours=0) == 1.0
