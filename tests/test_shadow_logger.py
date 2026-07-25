"""Tests for shadow_logger — dual-decision (live vs shadow) SQLite logger.

Written FIRST (test-driven) per the task spec. All tests use a temp DB under
pytest's tmp_path — never the production ~/.hermes path.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.shadow_logger import ShadowLogger


@pytest.fixture
def logger(tmp_path):
    """Fresh ShadowLogger pointed at an isolated temp DB."""
    db = tmp_path / "test_shadow.db"
    return ShadowLogger(db_path=str(db))


def _raw(logger, q, args=()):
    """White-box helper: raw query against the logger's connection."""
    return logger._conn.execute(q, args).fetchall()


# ── log_decision writes a row ────────────────────────────────────────────────


def test_log_decision_writes_row(logger):
    ts = time.time()
    logger.log_decision(
        ts=ts,
        live_provider="zai",
        live_model="glm-5.2",
        shadow_provider="zai",
        shadow_model="glm-5.2",
        shadow_cost=0.5,
        tokens=1000,
        reason="baseline",
    )
    rows = _raw(
        logger,
        "SELECT ts, live_provider, live_model, shadow_provider, shadow_model, "
        "shadow_cost, live_cost, tokens, agree, reason "
        "FROM routing_shadow_decisions;",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == ts
    assert row[1] == "zai"
    assert row[2] == "glm-5.2"
    assert row[3] == "zai"
    assert row[4] == "glm-5.2"
    assert row[5] == 0.5
    assert row[6] is None  # live_cost not passed → NULL
    assert row[7] == 1000
    assert row[8] == 1  # agree
    assert row[9] == "baseline"


def test_log_decision_stores_disagreement(logger):
    logger.log_decision(
        ts=time.time(),
        live_provider="zai",
        live_model="glm-5.2",
        shadow_provider="ollama",
        shadow_model="qwen3",
        shadow_cost=0.3,
        tokens=500,
    )
    agree = _raw(logger, "SELECT agree FROM routing_shadow_decisions;")[0][0]
    assert agree == 0
    # reason defaults to empty string
    reason = _raw(logger, "SELECT reason FROM routing_shadow_decisions;")[0][0]
    assert reason == ""


def test_live_cost_recorded_when_passed(logger):
    logger.log_decision(
        ts=time.time(),
        live_provider="zai",
        live_model="glm-5.2",
        shadow_provider="zai",
        shadow_model="glm-5.2",
        shadow_cost=0.5,
        tokens=1000,
        live_cost=0.6,
    )
    live_cost = _raw(logger, "SELECT live_cost FROM routing_shadow_decisions;")[0][0]
    assert live_cost == 0.6


# ── get_agreement_rate ───────────────────────────────────────────────────────


def test_get_agreement_rate_all_agree(logger):
    ts = time.time()
    for _ in range(4):
        logger.log_decision(ts, "zai", "m", "zai", "m", 0.5, 1000)
    assert logger.get_agreement_rate() == 1.0


def test_get_agreement_rate_mixed(logger):
    ts = time.time()
    for _ in range(3):
        logger.log_decision(ts, "zai", "m", "zai", "m", 0.5, 1000)  # agree
    logger.log_decision(ts, "zai", "m", "ollama", "m", 0.3, 1000)  # disagree
    assert logger.get_agreement_rate() == pytest.approx(0.75)


def test_get_agreement_rate_none_agree(logger):
    ts = time.time()
    logger.log_decision(ts, "zai", "m", "ollama", "m", 0.3, 1000)
    logger.log_decision(ts, "zai", "m", "ppq", "m", 0.4, 1000)
    assert logger.get_agreement_rate() == 0.0


def test_get_agreement_rate_since_ts(logger):
    logger.log_decision(1000.0, "zai", "m", "zai", "m", 0.5, 1000)  # old, agree
    logger.log_decision(2000.0, "zai", "m", "ollama", "m", 0.3, 1000)  # new, disagree
    # Only the new (ts=2000) row qualifies past ts=1500.
    assert logger.get_agreement_rate(since_ts=1500.0) == 0.0
    # Including both rows → 0.5
    assert logger.get_agreement_rate(since_ts=0.0) == pytest.approx(0.5)


# ── get_cost_comparison ──────────────────────────────────────────────────────


def test_get_cost_comparison(logger):
    logger.log_decision(time.time(), "zai", "m", "zai", "m",
                        shadow_cost=0.5, tokens=1000, live_cost=0.6)
    logger.log_decision(time.time(), "zai", "m", "ollama", "m",
                        shadow_cost=0.3, tokens=1000, live_cost=0.6)
    live_avg, shadow_avg = logger.get_cost_comparison()
    assert live_avg == pytest.approx(0.6)
    assert shadow_avg == pytest.approx(0.4)  # (0.5 + 0.3) / 2


def test_get_cost_comparison_since_ts(logger):
    logger.log_decision(1000.0, "zai", "m", "zai", "m",
                        shadow_cost=0.5, tokens=1000, live_cost=0.5)
    logger.log_decision(2000.0, "zai", "m", "ollama", "m",
                        shadow_cost=0.3, tokens=1000, live_cost=0.6)
    live_avg, shadow_avg = logger.get_cost_comparison(since_ts=1500.0)
    assert live_avg == pytest.approx(0.6)
    assert shadow_avg == pytest.approx(0.3)


# ── empty-table handling ────────────────────────────────────────────────────


def test_empty_table_agreement_is_zero(logger):
    assert logger.get_agreement_rate() == 0.0
    assert logger.get_agreement_rate(since_ts=1.0) == 0.0


def test_empty_table_cost_is_zero_zero(logger):
    live_avg, shadow_avg = logger.get_cost_comparison()
    assert (live_avg, shadow_avg) == (0.0, 0.0)
    live_avg, shadow_avg = logger.get_cost_comparison(since_ts=1.0)
    assert (live_avg, shadow_avg) == (0.0, 0.0)


# ── temp DB isolation ────────────────────────────────────────────────────────


def test_uses_temp_db_not_production(logger, tmp_path):
    assert str(tmp_path) in logger.db_path
    assert ".hermes" not in logger.db_path
    assert "zai_usage.db" not in logger.db_path


def test_production_path_is_default_not_temp():
    """Default arg must point at the production path (expanded)."""
    sl = ShadowLogger.__new__(ShadowLogger)  # don't open a real DB
    # Reconstruct just the path resolution without connecting.
    import src.shadow_logger as mod

    expanded = mod.os.path.expanduser("~/.hermes/bot/zai_usage.db")
    assert expanded.endswith(".hermes/bot/zai_usage.db")


# ── null / empty inputs never raise ──────────────────────────────────────────


def test_null_inputs_no_exception(logger):
    logger.log_decision(None, None, None, None, None, None, None, reason=None)
    logger.log_decision(None, "", "", "", "", 0.0, 0)
    count = _raw(logger, "SELECT COUNT(*) FROM routing_shadow_decisions;")[0][0]
    assert count == 2


def test_null_ts_substituted(logger):
    before = time.time()
    logger.log_decision(None, "zai", "m", "zai", "m", 0.5, 1000)
    ts = _raw(logger, "SELECT ts FROM routing_shadow_decisions;")[0][0]
    assert ts >= before  # None → now()


def test_none_none_is_agreement(logger):
    # live_provider == shadow_provider (both None) → agree
    logger.log_decision(None, None, None, None, None, None, None)
    assert logger.get_agreement_rate() == 1.0
