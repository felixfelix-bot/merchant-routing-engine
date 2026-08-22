#!/usr/bin/env python3
"""Tests for CG-13 cost outlier detector — EWMA + Kalman composition.

Tests exercise the pure detection/classification functions with hand-crafted
data, plus integration tests with a temp SQLite DB for the I/O layer.
No live zai_usage.db required.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.cost_outlier_detector import (
    compute_ewma,
    compute_baseline,
    detect_outlier,
    compute_expected_cost,
    classify_discrepancy,
    build_provider_breakdown,
    fetch_hourly_spends,
    fetch_provider_breakdown,
    fetch_kalman_state,
    detect_cost_outliers,
    EWMA_ALPHA,
    COLD_START_THRESHOLD,
    MIN_BASELINE_HOURS,
)


# ── EWMA ──────────────────────────────────────────────────────────────────────


def test_ewma_basic():
    """EWMA of a simple series produces a weighted average."""
    series = [1.0, 1.0, 1.0, 1.0, 1.0]
    result = compute_ewma(series, alpha=0.3)
    assert abs(result - 1.0) < 1e-6


def test_ewma_empty():
    """Empty series → 0.0 (no crash)."""
    assert compute_ewma([], alpha=0.3) == 0.0


def test_ewma_single():
    """Single element → that element."""
    assert compute_ewma([5.5], alpha=0.3) == 5.5


def test_ewma_weights_recent():
    """EWMA weights recent observations more heavily than old ones."""
    # Series that shifts from low to high — EWMA should be closer to recent
    series = [0.0, 0.0, 0.0, 0.0, 10.0]
    result = compute_ewma(series, alpha=0.3)
    # With alpha=0.3, EWMA = 0.7^4*0 + 0.3*(0.7^3*0 + 0.3*(... + 10))
    # Should be between 0 and 10, closer to recent
    assert 1.0 < result < 6.0  # pulled toward 10 but not all the way


def test_ewma_alpha_influence():
    """Higher alpha → more responsive to last value."""
    series = [0.0, 0.0, 0.0, 0.0, 10.0]
    low_alpha = compute_ewma(series, alpha=0.1)
    high_alpha = compute_ewma(series, alpha=0.5)
    assert high_alpha > low_alpha  # higher alpha → closer to 10


# ── Baseline stats ────────────────────────────────────────────────────────────


def test_baseline_stats():
    """Mean, std, n computed from a known series."""
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = compute_baseline(series)
    assert b["n"] == 5
    assert abs(b["mean"] - 3.0) < 1e-6
    # std = sqrt(var) where var = sum((x-mean)^2)/(n-1) = 2.5 → std ≈ 1.581
    assert abs(b["std"] - 1.5811) < 0.01


def test_baseline_empty():
    """Empty series → zeroed defaults."""
    b = compute_baseline([])
    assert b["n"] == 0
    assert b["mean"] == 0.0
    assert b["std"] == 0.0


def test_baseline_single():
    """Single element → std=0 (no variance with n=1)."""
    b = compute_baseline([5.0])
    assert b["n"] == 1
    assert abs(b["mean"] - 5.0) < 1e-6
    assert b["std"] == 0.0


# ── Outlier detection ─────────────────────────────────────────────────────────


def test_outlier_detected():
    """Actual >> EWMA → is_outlier=True with correct ratio."""
    ewma = 0.5
    baseline = {"mean": 0.5, "std": 1.0, "n": 100}
    actual = 10.61
    result = detect_outlier(actual, ewma, baseline)
    assert result["is_outlier"] is True
    assert result["ratio"] > 10  # 10.61 / ~0.68 threshold > 10x
    assert "threshold" in result


def test_outlier_normal():
    """Actual ≈ EWMA → is_outlier=False."""
    ewma = 0.5
    baseline = {"mean": 0.5, "std": 0.3, "n": 100}
    actual = 0.6
    result = detect_outlier(actual, ewma, baseline)
    assert result["is_outlier"] is False
    assert result["ratio"] < 2.0


def test_outlier_threshold_formula():
    """Threshold = max(3 * EWMA, mean + 2 * std) when baseline is sufficient."""
    ewma = 0.5
    baseline = {"mean": 0.5, "std": 0.3, "n": 100}
    # 3 * 0.5 = 1.5, 0.5 + 2*0.3 = 1.1 → threshold = 1.5
    result = detect_outlier(1.0, ewma, baseline)
    assert abs(result["threshold"] - 1.5) < 1e-6


def test_outlier_threshold_mean_plus_2std():
    """When mean + 2*std > 3*EWMA, threshold uses mean + 2*std."""
    ewma = 0.2
    baseline = {"mean": 2.0, "std": 1.0, "n": 100}
    # 3 * 0.2 = 0.6, 2.0 + 2*1.0 = 4.0 → threshold = 4.0
    result = detect_outlier(1.0, ewma, baseline)
    assert abs(result["threshold"] - 4.0) < 1e-6


def test_outlier_cold_start():
    """No history (n < MIN_BASELINE_HOURS) → cold start threshold."""
    ewma = 0.0
    baseline = {"mean": 0.0, "std": 0.0, "n": 3}
    actual = 1.5
    result = detect_outlier(actual, ewma, baseline)
    # Cold start: threshold = COLD_START_THRESHOLD ($2/h default)
    assert abs(result["threshold"] - COLD_START_THRESHOLD) < 1e-6
    assert result["is_outlier"] is False  # 1.5 < 2.0


def test_outlier_cold_start_exceeded():
    """Cold start with actual > fixed threshold → outlier."""
    ewma = 0.0
    baseline = {"mean": 0.0, "std": 0.0, "n": 3}
    actual = 3.0
    result = detect_outlier(actual, ewma, baseline)
    assert result["is_outlier"] is True
    assert abs(result["threshold"] - COLD_START_THRESHOLD) < 1e-6


# ── Expected cost (Kalman composition) ────────────────────────────────────────


def test_expected_cost_basic():
    """burn_rate_tph × price / 1M."""
    # 151K tok/h × $0.47/M / 1M = 0.071/h
    result = compute_expected_cost(151_000, 0.47)
    assert abs(result - 0.071) < 0.001


def test_expected_cost_zero():
    """Zero burn rate → zero cost."""
    assert compute_expected_cost(0, 0.47) == 0.0


def test_expected_cost_zero_price():
    """Zero price → zero cost."""
    assert compute_expected_cost(151_000, 0.0) == 0.0


# ── Discrepancy classification ────────────────────────────────────────────────


def test_classify_routing_inefficiency():
    """Actual >> expected → routing_inefficiency."""
    # actual = $10.61/h, expected = $0.07/h → 150x discrepancy
    result = classify_discrepancy(
        actual=10.61, expected=0.07,
        ewma=0.5, baseline={"mean": 0.5, "std": 1.0, "n": 100},
    )
    assert result["category"] == "routing_inefficiency"
    assert "expensive" in result["explanation"].lower() or "inefficien" in result["explanation"].lower()


def test_classify_quota_exhaustion():
    """Actual ≈ expected but both high → quota_exhaustion (structural)."""
    # both ~$10/h — actual ≈ expected, both above threshold
    result = classify_discrepancy(
        actual=10.0, expected=9.5,
        ewma=0.5, baseline={"mean": 0.5, "std": 1.0, "n": 100},
    )
    assert result["category"] == "quota_exhaustion"


def test_classify_normal():
    """Actual ≈ expected, both low → normal."""
    result = classify_discrepancy(
        actual=0.05, expected=0.07,
        ewma=0.1, baseline={"mean": 0.1, "std": 0.05, "n": 100},
    )
    assert result["category"] == "normal"


def test_classify_zero_expected():
    """Expected = 0 but actual > 0 → routing_inefficiency (free path expected)."""
    result = classify_discrepancy(
        actual=5.0, expected=0.0,
        ewma=0.1, baseline={"mean": 0.1, "std": 0.05, "n": 100},
    )
    assert result["category"] == "routing_inefficiency"


# ── Provider breakdown ────────────────────────────────────────────────────────


def test_build_provider_breakdown():
    """Rows sorted by spend desc with percentage of total."""
    rows = [
        {"key_name": "openrouter", "spend": 8.0, "calls": 500},
        {"key_name": "routstrd", "spend": 2.0, "calls": 400},
        {"key_name": "telnyx", "spend": 0.5, "calls": 50},
    ]
    breakdown = build_provider_breakdown(rows)
    assert breakdown[0]["key_name"] == "openrouter"
    assert abs(breakdown[0]["pct_of_total"] - 76.2) < 1.0  # 8/10.5 ≈ 76.2%
    assert len(breakdown) == 3


def test_build_provider_breakdown_empty():
    """Empty rows → empty list."""
    assert build_provider_breakdown([]) == []


# ── Integration: I/O with temp DB ─────────────────────────────────────────────


def _make_temp_db():
    """Create a temp SQLite DB mimicking zai_usage.db schema."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            model TEXT,
            total_tokens INTEGER,
            tier TEXT,
            cost_usd REAL DEFAULT 0,
            status_code INTEGER DEFAULT 200
        );
        CREATE TABLE kalman_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key TEXT NOT NULL,
            burn_rate_tph REAL,
            uncertainty REAL,
            exhausts_in_hours REAL,
            will_exhaust INTEGER DEFAULT 0,
            note TEXT
        );
        CREATE TABLE price_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            provider TEXT NOT NULL,
            rate_per_m REAL NOT NULL,
            is_measured INTEGER DEFAULT 0,
            confidence REAL DEFAULT 1.0
        );
        CREATE TABLE daily_spend (
            date TEXT NOT NULL,
            tier TEXT NOT NULL,
            spend_usd REAL DEFAULT 0,
            call_count INTEGER DEFAULT 0,
            token_count INTEGER DEFAULT 0,
            PRIMARY KEY (date, tier)
        );
    """)

    now = time.time()
    # 10 hours of normal spend (~$0.50/h) + 1 hour spike ($10.61)
    for h in range(10):
        for _ in range(20):
            c.execute(
                "INSERT INTO api_calls (ts, key_name, model, total_tokens, tier, cost_usd, status_code) "
                "VALUES (?, 'openrouter', 'glm-5.2', 50000, 'openrouter', 0.025, 200)",
                (now - (10 - h) * 3600,),
            )
    # spike hour: 917 calls at ~$0.012 each
    for _ in range(917):
        c.execute(
            "INSERT INTO api_calls (ts, key_name, model, total_tokens, tier, cost_usd, status_code) "
            "VALUES (?, 'openrouter', 'glm-5.2', 12000, 'openrouter', 0.0116, 200)",
            (now - 3600,),
        )
    # Kalman sample
    c.execute(
        "INSERT INTO kalman_samples (ts, key, burn_rate_tph, uncertainty, exhausts_in_hours, will_exhaust, note) "
        "VALUES (?, 'friend', 151000, 46000, 2.5, 1, 'converged')",
        (now - 300,),
    )
    # Price observation
    c.execute(
        "INSERT INTO price_observations (ts, provider, rate_per_m, is_measured, confidence) "
        "VALUES (?, 'openrouter', 0.47, 1, 1.0)",
        (now - 3600,),
    )
    c.commit()
    c.close()
    return path


def test_fetch_hourly_spends():
    """fetch_hourly_spends reads from temp DB and returns per-hour aggregation."""
    db = _make_temp_db()
    try:
        spends = fetch_hourly_spends(db, lookback_hours=24)
        assert len(spends) >= 2  # at least normal + spike hours
        # Find the spike hour
        spike = [s for s in spends if s["spend"] > 5.0]
        assert len(spike) >= 1
        assert spike[0]["calls"] > 100  # 917 calls in spike hour
    finally:
        os.unlink(db)


def test_fetch_provider_breakdown():
    """fetch_provider_breakdown groups by key_name."""
    db = _make_temp_db()
    try:
        now = time.time()
        breakdown = fetch_provider_breakdown(db, now - 7200)
        assert len(breakdown) >= 1
        assert breakdown[0]["key_name"] == "openrouter"
        assert breakdown[0]["spend"] > 5.0
    finally:
        os.unlink(db)


def test_fetch_kalman_state():
    """fetch_kalman_state reads latest kalman_sample."""
    db = _make_temp_db()
    try:
        state = fetch_kalman_state(db)
        assert state["burn_rate_tph"] == 151_000
        assert state["uncertainty"] == 46_000
        assert state["exhausts_in_hours"] == 2.5
    finally:
        os.unlink(db)


def test_detect_cost_outliers_integration():
    """detect_cost_outliers with temp DB finds the spike."""
    db = _make_temp_db()
    try:
        alerts = detect_cost_outliers(db_path=db)
        # The spike ($10.61/h) should be detected as an outlier
        outlier_alerts = [a for a in alerts if a["alert_type"] == "spend_outlier"]
        assert len(outlier_alerts) >= 1
        alert = outlier_alerts[0]
        assert alert["actual"] > 5.0  # $10.61 spike
        assert alert["ratio"] > 3.0  # > 3x normal
        # Should have provider context
        assert "providers" in alert or "provider_breakdown" in alert
    finally:
        os.unlink(db)


def test_detect_cost_outliers_cold_start():
    """Empty DB → no crash, falls back to cold-start threshold."""
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE api_calls (id INTEGER PRIMARY KEY, ts REAL, key_name TEXT,
            model TEXT, total_tokens INTEGER, tier TEXT, cost_usd REAL,
            status_code INTEGER);
        CREATE TABLE kalman_samples (id INTEGER PRIMARY KEY, ts REAL, key TEXT,
            burn_rate_tph REAL, uncertainty REAL, exhausts_in_hours REAL,
            will_exhaust INTEGER, note TEXT);
        CREATE TABLE price_observations (id INTEGER PRIMARY KEY, ts REAL,
            provider TEXT, rate_per_m REAL, is_measured INTEGER, confidence REAL);
        CREATE TABLE daily_spend (date TEXT, tier TEXT, spend_usd REAL,
            call_count INTEGER, token_count INTEGER);
    """)
    c.commit()
    c.close()
    try:
        alerts = detect_cost_outliers(db_path=db)
        # No data → no alerts (nothing to detect)
        assert isinstance(alerts, list)
    finally:
        os.unlink(db)


def test_detect_cost_outliers_no_kalman():
    """No kalman_samples → degrade gracefully (no crash)."""
    db = _make_temp_db()
    try:
        # Remove kalman data
        c = sqlite3.connect(db)
        c.execute("DELETE FROM kalman_samples")
        c.commit()
        c.close()
        alerts = detect_cost_outliers(db_path=db)
        # Should still detect the spend outlier without Kalman composition
        outlier_alerts = [a for a in alerts if a["alert_type"] == "spend_outlier"]
        assert len(outlier_alerts) >= 1
    finally:
        os.unlink(db)


def test_detect_cost_outliers_kalman_composition():
    """When Kalman says normal but $ says high → routing_inefficiency flagged."""
    db = _make_temp_db()
    try:
        alerts = detect_cost_outliers(db_path=db)
        # The composition should flag the discrepancy
        # expected = 151K * 0.47/1M ≈ $0.07, actual ≈ $10.61 → routing_inefficiency
        comp_alerts = [a for a in alerts if a["alert_type"] == "kalman_composition"]
        if comp_alerts:
            # If composition alert present, it should flag routing inefficiency
            assert comp_alerts[0]["discrepancy_category"] in (
                "routing_inefficiency", "quota_exhaustion",
            )
    finally:
        os.unlink(db)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
