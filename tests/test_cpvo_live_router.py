"""Tests for CPVO wiring into LiveRouter (Phase 2.5.4 — quality-aware optimizer).

These verify that LiveRouter.adjusts base rates with the CPVO quality penalty
before handing them to the RoutingOptimizer, that failures fall back to base
rates, that token-mismatch audit works, and that an end-to-end scenario
actually flips a routing decision based on provider quality.

TDD: written BEFORE implementation. Expect RED until live_router.py is wired.
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.live_router import LiveRouter


# ── Schema (mirrors zai_proxy.py _TELEMETRY_SCHEMA) ──────────────────────────

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


def _populate_telemetry(
    db_path: str,
    provider: str,
    total: int,
    success: int,
    mismatch: int = 0,
    latency_ms: int = 200,
) -> None:
    """Insert ``total`` telemetry rows for ``provider``.

    ``success`` rows are valid; the rest invalid. ``mismatch`` rows carry the
    billing-mismatch flag. Timestamps are recent so the 24h window sees them.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_TELEMETRY_SCHEMA)
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for i in range(total):
            valid = 1 if i < success else 0
            mm = 1 if i < mismatch else 0
            rows.append((now, provider, 1, valid, latency_ms, "none", 500, 480, mm))
        conn.executemany(
            "INSERT INTO provider_telemetry "
            "(ts, provider, response_received, response_valid, "
            "latency_ms, error_type, billed_tokens, actual_tokens, "
            "token_mismatch) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singleton():
    LiveRouter.reset_instance()
    yield
    LiveRouter.reset_instance()


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def converged_rates():
    return {
        "ours": 0.001,
        "friend": 0.029,
        "ollama_cloud": 0.024,
        "ppq": 0.14,
        "openrouter": 0.135,
        "deepinfra": 1.30,
    }


def _make_router(db_path, converged_rates):
    """Fresh LiveRouter (not singleton) with CPVO pointed at db_path."""
    LiveRouter.reset_instance()
    return LiveRouter(db_path=db_path, converged_rates=converged_rates)


# ── CPVO effective-rate adjustment ───────────────────────────────────────────


class TestCPVOAdjustsRates:
    def test_cpvo_adjusts_rates(self, tmp_db, converged_rates):
        """80% success → effective rate = base / 0.8 = base * 1.25."""
        _populate_telemetry(tmp_db, "ollama_cloud", total=200, success=160)
        router = _make_router(tmp_db, converged_rates)
        effective = router._get_effective_rates()
        base = converged_rates["ollama_cloud"]
        assert effective["ollama_cloud"] == pytest.approx(base / 0.8, rel=1e-6)
        # sanity: ~25% increase
        assert effective["ollama_cloud"] > base * 1.2

    def test_cpvo_no_adjustment_high_success(self, tmp_db, converged_rates):
        """99% success (>= 0.95 threshold) → no penalty, rate unchanged."""
        _populate_telemetry(tmp_db, "ollama_cloud", total=200, success=198)
        router = _make_router(tmp_db, converged_rates)
        effective = router._get_effective_rates()
        assert effective["ollama_cloud"] == pytest.approx(
            converged_rates["ollama_cloud"], rel=1e-9
        )

    def test_cpvo_insufficient_data_no_change(self, tmp_db, converged_rates):
        """< MIN_SAMPLES (100) → not enough data, rate unchanged."""
        _populate_telemetry(tmp_db, "ollama_cloud", total=50, success=10)
        router = _make_router(tmp_db, converged_rates)
        effective = router._get_effective_rates()
        assert effective["ollama_cloud"] == pytest.approx(
            converged_rates["ollama_cloud"], rel=1e-9
        )

    def test_cpvo_failure_falls_back(self, tmp_db, converged_rates):
        """DB error (inaccessible path) → falls back to base rates unchanged.

        Populate data that WOULD trigger a 1.25x penalty, then point the
        router at a bad db_path (a directory) so every query errors. The
        effective rate must be the unadjusted base rate, proving the fallback.
        """
        _populate_telemetry(tmp_db, "ollama_cloud", total=200, success=160)
        bad_path = tempfile.mkdtemp()  # a directory — sqlite cannot open as DB
        try:
            router = _make_router(bad_path, converged_rates)
            effective = router._get_effective_rates()
            # Fallback: rate unchanged despite the 80%-success data existing
            assert effective["ollama_cloud"] == pytest.approx(
                converged_rates["ollama_cloud"], rel=1e-9
            )
        finally:
            os.rmdir(bad_path)

    def test_cpvo_cache_returns_same_within_ttl(self, tmp_db, converged_rates):
        """Within the 5-min TTL, the cached result is returned without re-query."""
        _populate_telemetry(tmp_db, "ollama_cloud", total=200, success=160)
        router = _make_router(tmp_db, converged_rates)
        first = router._get_effective_rates()
        # Mutate the DB after the first call; cache should mask the change.
        _populate_telemetry(tmp_db, "ollama_cloud", total=1, success=1)
        second = router._get_effective_rates()
        assert second == first  # served from cache, not re-queried


# ── Token mismatch audit ─────────────────────────────────────────────────────


class TestTokenMismatchAudit:
    def test_token_mismatch_detected(self, tmp_db):
        from src.token_audit import audit_token_count

        # billed=100, actual=50 → rate 0.5 > 0.20 → mismatch
        actual, mismatch, rate = audit_token_count(100, b"x" * 200)
        assert actual == 50
        assert mismatch is True
        assert rate == pytest.approx(0.5)

    def test_token_mismatch_no_crash(self):
        from src.token_audit import audit_token_count

        # None buffer / zero billed / garbage — never raises, never blocks
        for args in [(100, None), (0, b"x" * 400), ("bad", None)]:
            actual, mismatch, rate = audit_token_count(*args)  # type: ignore[arg-type]
            assert mismatch is False


# ── quality_score in get_kalman_state ────────────────────────────────────────


class TestQualityScoreInState:
    def test_quality_score_in_state(self, tmp_db, converged_rates):
        """get_kalman_state includes a quality_score dict per provider with
        success_rate, avg_latency_ms, token_mismatch_rate."""
        _populate_telemetry(
            tmp_db, "ollama_cloud", total=200, success=160, mismatch=40,
            latency_ms=300,
        )
        router = _make_router(tmp_db, converged_rates)
        state = router.get_kalman_state()
        assert "ollama_cloud" in state
        qs = state["ollama_cloud"].get("quality_score")
        assert qs is not None, "quality_score must be present per provider"
        assert "success_rate" in qs
        assert "avg_latency_ms" in qs
        assert "token_mismatch_rate" in qs
        # 160/200 = 0.8 success, 40/200 = 0.2 mismatch
        assert qs["success_rate"] == pytest.approx(0.8, abs=1e-6)
        assert qs["token_mismatch_rate"] == pytest.approx(0.2, abs=1e-6)
        assert qs["avg_latency_ms"] == pytest.approx(300.0, abs=1e-6)


# ── End-to-end: telemetry → CPVO → optimizer → better provider ───────────────


class TestEndToEndQualityAwareRouting:
    def test_end_to_end_quality_aware_pick(
        self, tmp_db, converged_rates
    ):
        """Quality penalty flips the failover decision.

        Setup (two HIGH-tier providers, both viable, off-peak):
          - ollama_cloud: base $0.024/M, 80% success → effective $0.030/M
          - friend:       base $0.029/M, 99% success → effective $0.029/M

        Without CPVO the optimizer picks ollama (cheaper sticker price).
        WITH CPVO the quality penalty makes friend cheaper → friend is chosen.
        """
        # ollama_cloud is cheap but low-quality (80% success)
        _populate_telemetry(tmp_db, "ollama_cloud", total=200, success=160)
        # friend is pricier but reliable (99% success → no penalty)
        _populate_telemetry(tmp_db, "friend", total=200, success=198)

        # ── WITH CPVO: friend should win (0.029 < 0.030) ──────────────────
        router_cpvo = _make_router(tmp_db, converged_rates)
        eff = router_cpvo._get_effective_rates()
        # sanity-check the math direction (divide by success_rate, not multiply)
        assert eff["ollama_cloud"] == pytest.approx(0.024 / 0.8, rel=1e-6)
        assert eff["friend"] == pytest.approx(0.029, rel=1e-6)

        quota = {
            "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "friend":       {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
            "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
            "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
            "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
            "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
        }
        health = {
            "ours": False, "friend": True, "ollama_cloud": True,
            "ppq": True, "openrouter": True, "deepinfra": True,
        }
        chosen, _fallback = router_cpvo.select_failover(
            quota_state=quota, health_state=health, peak=False,
        )
        assert chosen[0] == "friend", (
            "quality-aware optimizer must pick the reliable 'friend' over the "
            "cheap-but-flaky 'ollama_cloud'"
        )

    def test_end_to_end_without_cpvo_picks_cheapest_sticker(
        self, tmp_db, converged_rates
    ):
        """Control: with NO telemetry data, the optimizer falls back to base
        rates and picks the cheaper sticker price (ollama_cloud)."""
        router_plain = _make_router(tmp_db, converged_rates)
        quota = {
            "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "friend":       {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
            "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
            "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
            "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
            "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
        }
        health = {
            "ours": False, "friend": True, "ollama_cloud": True,
            "ppq": True, "openrouter": True, "deepinfra": True,
        }
        chosen, _fallback = router_plain.select_failover(
            quota_state=quota, health_state=health, peak=False,
        )
        # No quality data → base rates → ollama ($0.024) beats friend ($0.029)
        assert chosen[0] == "ollama_cloud"
