#!/usr/bin/env python3
"""ADR-015: per-(endpoint, LLM) HealthKalman (filter dynamics + probe ingest).

Run:
  /usr/bin/python3 -m pytest tests/test_health_kalman.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import health_kalman  # noqa: E402


def _hk(tmp_path, **kw):
    return health_kalman.HealthKalman(tmp_path / "health_kalman.json", **kw)


def test_single_failure_bumps_but_does_not_exclude(tmp_path):
    hk = _hk(tmp_path)
    hk.observe("p", ok=False)
    x = hk.error_prob("p")
    assert x is not None and 0.15 < x < 0.55, x
    assert hk.is_unhealthy("p") is False
    assert hk.health_multiplier("p") > 1.0


def test_sustained_failures_exclude(tmp_path):
    hk = _hk(tmp_path)
    for _ in range(3):
        hk.observe("p", ok=False)
    assert hk.is_unhealthy("p") is True
    assert hk.health_multiplier("p") == float("inf")


def test_success_decays(tmp_path):
    hk = _hk(tmp_path)
    hk.observe("p", ok=False)
    hk.observe("p", ok=False)
    x_fail, m_fail = hk.error_prob("p"), hk.health_multiplier("p")
    hk.observe("p", ok=True)
    hk.observe("p", ok=True)
    assert hk.error_prob("p") < x_fail
    assert hk.health_multiplier("p") < m_fail


def test_healthy_lane_multiplier_is_one(tmp_path):
    hk = _hk(tmp_path)
    hk.observe("q", ok=True)
    assert hk.error_prob("q") <= health_kalman.ERR_ONSET
    assert hk.health_multiplier("q") == 1.0


def test_ingest_probe_dedupes_by_ts(tmp_path):
    hk = _hk(tmp_path)
    snap = {"p": {"ts": 100.0, "ok": False, "model": "m"}}
    assert hk.ingest_probe(snap) == 1
    assert hk.ingest_probe(snap) == 0
    assert hk.samples("p", "m") == 1
    assert hk.ingest_probe({"p": {"ts": 200.0, "ok": True, "model": "m"}}) == 1
    assert hk.samples("p", "m") == 2


def test_ingest_ignores_rows_without_ok(tmp_path):
    hk = _hk(tmp_path)
    assert hk.ingest_probe({"p": {"ts": 1.0, "healthy": True}}) == 0
    assert hk.ingest_probe({"_meta": {"ts": 1.0}, "p": {"ts": 2.0, "ok": True}}) == 1


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "health_kalman.json"
    hk1 = health_kalman.HealthKalman(path)
    hk1.observe("p", ok=False)
    hk1.observe("p", ok=False)
    x1 = hk1.error_prob("p")
    hk2 = health_kalman.HealthKalman(path)
    assert hk2.error_prob("p") == x1
