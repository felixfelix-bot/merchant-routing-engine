"""Tests for src/cpvo_calculator.py — Cost Per Valid Output calculator.

Phase 2.5.2 (Gate 1, TDD): written BEFORE the implementation.

CPVO = SUM(cost) / SUM(success) per provider per time window.  This is the
quality-aware cost metric that makes the routing optimizer penalise providers
with low success rates instead of just picking the cheapest sticker price.

Key invariant the cold reviewer checks (Gate 2.5):
    CPVO divides by **success_count**, NOT **total_count**.
    Dividing by total would understate the cost of failures.

Every test uses a throwaway SQLite file — the production usage DB
(``~/.hermes/bot/zai_usage.db``) is never touched.

The module under test is ``src.cpvo_calculator.CPVOCalculator``.
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# ── Import path setup ──────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.cpvo_calculator import CPVOCalculator

# ── Schema (mirrors zai_proxy._TELEMETRY_SCHEMA) ───────────────────────────

_TELEMETRY_SCHEMA = """CREATE TABLE IF NOT EXISTS provider_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    response_received INTEGER,
    response_valid INTEGER,
    latency_ms INTEGER,
    error_type TEXT,
    billed_tokens INTEGER,
    actual_tokens INTEGER,
    token_mismatch INTEGER
)"""


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path():
    """A fresh temp file path for an isolated SQLite DB. Cleaned up after."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="cpvo_test_")
    os.close(fd)
    os.unlink(path)  # let the test create it fresh
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


@pytest.fixture
def calc(tmp_db_path):
    """A CPVOCalculator pointed at the temp DB."""
    return CPVOCalculator(db_path=tmp_db_path)


def _insert_row(
    conn: sqlite3.Connection,
    provider: str = "test_prov",
    response_valid: bool = True,
    response_received: bool = True,
    latency_ms: int = 200,
    billed_tokens: int = 1000,
    actual_tokens: int = 1000,
    token_mismatch: bool = False,
    error_type: str = "none",
    ts: datetime | None = None,
):
    """Insert one telemetry row.  ``ts`` defaults to 'now'."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO provider_telemetry "
        "(ts, provider, response_received, response_valid, "
        "latency_ms, error_type, billed_tokens, actual_tokens, "
        "token_mismatch) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            ts.isoformat(),
            provider,
            int(response_received),
            int(response_valid),
            latency_ms,
            error_type,
            billed_tokens,
            actual_tokens,
            int(token_mismatch),
        ),
    )


def _seed_db(db_path: str, rows: list[dict]) -> None:
    """Create the telemetry table and insert rows from a list of dicts."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(_TELEMETRY_SCHEMA)
    for r in rows:
        _insert_row(conn, **r)
    conn.close()


# ── Tests: compute_cpvo ────────────────────────────────────────────────────


class TestComputeCpvo:
    def test_cpvo_computation(self, calc, tmp_db_path):
        """Known data → known CPVO.

        100 successful requests, 1000 billed_tokens each, base_rate $1.0/M.
        total_cost = 100_000 tokens / 1_000_000 * $1.0 = $0.10
        success_count = 100
        CPVO = $0.10 / 100 = $0.001 per successful request.
        """
        rows = [
            dict(provider="test_prov", response_valid=True, billed_tokens=1000)
            for _ in range(100)
        ]
        _seed_db(tmp_db_path, rows)

        cpvo = calc.compute_cpvo("test_prov", base_rate=1.0)
        assert cpvo is not None
        assert abs(cpvo - 0.001) < 1e-9  # $0.10 / 100

    def test_cpvo_without_base_rate(self, calc, tmp_db_path):
        """Without base_rate, CPVO returns billed-tokens-per-success."""
        rows = [
            dict(provider="test_prov", response_valid=True, billed_tokens=500)
            for _ in range(100)
        ]
        _seed_db(tmp_db_path, rows)

        cpvo = calc.compute_cpvo("test_prov")
        # 100 * 500 / 100 = 500 tokens per success
        assert cpvo == 500.0

    def test_zero_success_returns_infinite(self, calc, tmp_db_path):
        """No successes → CPVO is infinite (cost is unbounded)."""
        rows = [
            dict(provider="test_prov", response_valid=False, billed_tokens=100,
                 response_received=False, error_type="timeout")
            for _ in range(100)
        ]
        _seed_db(tmp_db_path, rows)

        cpvo = calc.compute_cpvo("test_prov", base_rate=1.0)
        assert cpvo == float("inf")

    def test_zero_success_without_base_rate(self, calc, tmp_db_path):
        """No successes without base_rate → still inf."""
        rows = [
            dict(provider="test_prov", response_valid=False, billed_tokens=0,
                 response_received=False, error_type="error")
            for _ in range(100)
        ]
        _seed_db(tmp_db_path, rows)

        assert calc.compute_cpvo("test_prov") == float("inf")

    def test_insufficient_data_returns_base(self, calc, tmp_db_path):
        """Fewer than 100 requests → returns base_rate (no adjustment)."""
        rows = [
            dict(provider="test_prov", response_valid=True, billed_tokens=100)
            for _ in range(50)  # < MIN_SAMPLES (100)
        ]
        _seed_db(tmp_db_path, rows)

        cpvo = calc.compute_cpvo("test_prov", base_rate=2.5)
        assert cpvo == 2.5  # returns base_rate unchanged

    def test_insufficient_data_returns_none_without_base(self, calc, tmp_db_path):
        """Insufficient data without base_rate → None."""
        rows = [
            dict(provider="test_prov", response_valid=True, billed_tokens=100)
            for _ in range(10)
        ]
        _seed_db(tmp_db_path, rows)

        assert calc.compute_cpvo("test_prov") is None

    def test_exactly_100_samples_is_sufficient(self, calc, tmp_db_path):
        """Exactly 100 requests IS sufficient (boundary check)."""
        rows = [
            dict(provider="test_prov", response_valid=True, billed_tokens=100)
            for _ in range(100)
        ]
        _seed_db(tmp_db_path, rows)

        cpvo = calc.compute_cpvo("test_prov", base_rate=1.0)
        assert cpvo is not None
        assert cpvo != 1.0  # not just returning base_rate

    def test_window_filter(self, calc, tmp_db_path):
        """Old rows outside the window are excluded from CPVO."""
        now = datetime.now(timezone.utc)
        old_ts = now - timedelta(hours=48)
        recent_rows = [
            dict(provider="test_prov", response_valid=True, billed_tokens=1000,
                 ts=now)
            for _ in range(100)
        ]
        old_rows = [
            dict(provider="test_prov", response_valid=False, billed_tokens=99999,
                 ts=old_ts)
            for _ in range(100)
        ]
        _seed_db(tmp_db_path, old_rows + recent_rows)

        # 24h window should only see the 100 recent successes
        cpvo = calc.compute_cpvo("test_prov", window_hours=24, base_rate=1.0)
        assert cpvo is not None
        assert abs(cpvo - 0.1 / 100) < 1e-9  # 100*1000/1M*1.0 / 100

    def test_only_counts_target_provider(self, calc, tmp_db_path):
        """Telemetry from other providers is excluded."""
        rows_a = [
            dict(provider="prov_a", response_valid=True, billed_tokens=500)
            for _ in range(100)
        ]
        rows_b = [
            dict(provider="prov_b", response_valid=True, billed_tokens=99999)
            for _ in range(100)
        ]
        _seed_db(tmp_db_path, rows_a + rows_b)

        cpvo = calc.compute_cpvo("prov_a", base_rate=1.0)
        # Only prov_a: 100 * 500 / 1M * 1.0 / 100 = 0.0005
        assert abs(cpvo - 0.0005) < 1e-9


# ── Tests: get_effective_rates ─────────────────────────────────────────────


class TestGetEffectiveRates:
    def test_high_success_no_penalty(self, calc, tmp_db_path):
        """Success rate >= 0.95 → effective = base (no penalty)."""
        # 200 rows: 196 valid (98% success)
        rows = [
            dict(provider="prov", response_valid=True, billed_tokens=100)
            for _ in range(196)
        ] + [
            dict(provider="prov", response_valid=False, billed_tokens=0,
                 response_received=False, error_type="timeout")
            for _ in range(4)
        ]
        _seed_db(tmp_db_path, rows)

        effective = calc.get_effective_rates({"prov": 1.0})
        assert effective["prov"] == 1.0  # no penalty

    def test_low_success_penalty(self, calc, tmp_db_path):
        """Success rate < 0.95 → effective = base / success_rate."""
        # 200 rows: 160 valid (80% success)
        rows = [
            dict(provider="prov", response_valid=True, billed_tokens=100)
            for _ in range(160)
        ] + [
            dict(provider="prov", response_valid=False, billed_tokens=0,
                 response_received=False, error_type="timeout")
            for _ in range(40)
        ]
        _seed_db(tmp_db_path, rows)

        effective = calc.get_effective_rates({"prov": 1.0})
        # 1.0 / 0.80 = 1.25
        assert abs(effective["prov"] - 1.25) < 1e-9

    def test_insufficient_data_returns_base(self, calc, tmp_db_path):
        """Fewer than 100 requests → effective = base."""
        rows = [
            dict(provider="prov", response_valid=False, billed_tokens=0)
            for _ in range(50)
        ]
        _seed_db(tmp_db_path, rows)

        effective = calc.get_effective_rates({"prov": 3.3})
        assert effective["prov"] == 3.3

    def test_empty_table(self, calc, tmp_db_path):
        """No data at all → returns base rates unchanged."""
        # Create empty table
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        conn.execute(_TELEMETRY_SCHEMA)
        conn.close()

        base = {"prov_a": 1.0, "prov_b": 2.0, "prov_c": 0.5}
        effective = calc.get_effective_rates(base)
        assert effective == base

    def test_missing_table(self, calc, tmp_db_path):
        """Table doesn't exist yet → returns base rates (never raises)."""
        # Don't create the table at all
        base = {"prov_a": 1.0}
        effective = calc.get_effective_rates(base)
        assert effective == base

    def test_multiple_providers(self, calc, tmp_db_path):
        """Each provider gets its own quality adjustment independently."""
        # prov_a: 100% success → no penalty
        rows_a = [
            dict(provider="prov_a", response_valid=True, billed_tokens=100)
            for _ in range(100)
        ]
        # prov_b: 80% success → penalty
        rows_b = [
            dict(provider="prov_b", response_valid=True, billed_tokens=100)
            for _ in range(80)
        ] + [
            dict(provider="prov_b", response_valid=False, billed_tokens=0)
            for _ in range(20)
        ]
        _seed_db(tmp_db_path, rows_a + rows_b)

        effective = calc.get_effective_rates({"prov_a": 1.0, "prov_b": 1.0})
        assert effective["prov_a"] == 1.0       # 100% success
        assert abs(effective["prov_b"] - 1.25) < 1e-9  # 1.0/0.80

    def test_zero_success_max_penalty(self, calc, tmp_db_path):
        """0% success → effective is very high (base / epsilon)."""
        rows = [
            dict(provider="prov", response_valid=False, billed_tokens=0,
                 response_received=False, error_type="error")
            for _ in range(100)
        ]
        _seed_db(tmp_db_path, rows)

        effective = calc.get_effective_rates({"prov": 1.0})
        # 0% success → effective = base / epsilon (very large)
        assert effective["prov"] >= 1e6


# ── Tests: get_quality_score ───────────────────────────────────────────────


class TestGetQualityScore:
    def test_get_quality_score(self, calc, tmp_db_path):
        """All fields are populated with correct values."""
        now = datetime.now(timezone.utc)
        # 200 rows: 180 valid (90%), avg latency 250ms, 10 mismatches
        rows = []
        for i in range(180):
            rows.append(dict(
                provider="prov", response_valid=True, response_received=True,
                latency_ms=250, billed_tokens=1000, actual_tokens=1000,
                token_mismatch=False, ts=now,
            ))
        for i in range(20):
            rows.append(dict(
                provider="prov", response_valid=False, response_received=False,
                latency_ms=5000, billed_tokens=0, actual_tokens=0,
                token_mismatch=False, error_type="timeout", ts=now,
            ))
        # 10 of the successes have token mismatch
        for r in rows[:10]:
            r["token_mismatch"] = True
            r["actual_tokens"] = 800

        _seed_db(tmp_db_path, rows)

        score = calc.get_quality_score("prov", base_rate=1.0)

        assert "success_rate" in score
        assert "avg_latency_ms" in score
        assert "token_mismatch_rate" in score
        assert "sample_count" in score
        assert "cpvo" in score
        assert "effective_rate" in score

        assert score["sample_count"] == 200
        assert abs(score["success_rate"] - 0.9) < 1e-9        # 180/200
        assert abs(score["avg_latency_ms"] - (180 * 250 + 20 * 5000) / 200) < 1e-6
        assert abs(score["token_mismatch_rate"] - 10 / 200) < 1e-9  # 10/200
        assert score["cpvo"] is not None
        # 90% < 95% → penalty: effective = 1.0 / 0.9
        assert abs(score["effective_rate"] - 1.0 / 0.9) < 1e-9

    def test_quality_score_insufficient_data(self, calc, tmp_db_path):
        """Insufficient data → sample_count < 100, rates fall back to base."""
        rows = [
            dict(provider="prov", response_valid=True, billed_tokens=100)
            for _ in range(50)
        ]
        _seed_db(tmp_db_path, rows)

        score = calc.get_quality_score("prov", base_rate=1.0)
        assert score["sample_count"] == 50
        assert score["success_rate"] == 1.0  # all 50 valid
        assert score["effective_rate"] == 1.0  # insufficient → base

    def test_quality_score_no_base_rate(self, calc, tmp_db_path):
        """Without base_rate, effective_rate is a penalty multiplier."""
        rows = [
            dict(provider="prov", response_valid=True, billed_tokens=100)
            for _ in range(160)
        ] + [
            dict(provider="prov", response_valid=False, billed_tokens=0)
            for _ in range(40)
        ]
        _seed_db(tmp_db_path, rows)

        score = calc.get_quality_score("prov")
        # 80% success < 95% → multiplier = 1/0.8 = 1.25
        assert abs(score["effective_rate"] - 1.25) < 1e-9


# ── Tests: never raises ─────────────────────────────────────────────────────


class TestNeverRaises:
    def test_never_raises_bad_path(self):
        """Bad DB path → silent (returns None/base, never raises)."""
        calc = CPVOCalculator(db_path="/nonexistent/path/that/does/not/exist.db")
        # None of these should raise
        result = calc.compute_cpvo("any_provider", base_rate=1.0)
        assert result == 1.0  # falls back to base_rate

        rates = calc.get_effective_rates({"a": 1.0, "b": 2.0})
        assert rates == {"a": 1.0, "b": 2.0}

        score = calc.get_quality_score("any_provider")
        assert isinstance(score, dict)

    def test_never_raises_garbage_provider(self, calc, tmp_db_path):
        """Garbage provider name → returns base/None, never raises."""
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        conn.execute(_TELEMETRY_SCHEMA)
        conn.close()

        assert calc.compute_cpvo("", base_rate=1.0) == 1.0
        assert calc.compute_cpvo(None, base_rate=1.0) == 1.0

    def test_never_raises_none_db_path(self):
        """None db_path → returns base rates, never raises."""
        calc = CPVOCalculator(db_path=None)
        assert calc.compute_cpvo("x", base_rate=5.0) == 5.0
        assert calc.get_effective_rates({"x": 5.0}) == {"x": 5.0}
        assert isinstance(calc.get_quality_score("x"), dict)
