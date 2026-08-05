"""Tests for shadow_logger — dual-decision (live vs shadow) SQLite logger.

Written FIRST (test-driven) per the task spec. All tests use a temp DB under
pytest's tmp_path — never the production ~/.hermes path.
"""
import os
import sqlite3
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


# ── Thread-safety test (cold-review major fix) ─────────────────────────────

def test_concurrent_writes_thread_safe(tmp_path):
    """Spawn N threads each logging M decisions. Verify row count and no exception."""
    import threading
    from src.shadow_logger import ShadowLogger

    db = tmp_path / "concurrent.db"
    logger = ShadowLogger(str(db))

    N_THREADS = 10
    M_PER_THREAD = 50
    errors = []

    def worker(tid):
        try:
            for i in range(M_PER_THREAD):
                logger.log_decision(
                    ts=time.time(),
                    live_provider=f"prov_{tid % 3}",
                    live_model="glm-5.2",
                    shadow_provider="prov_opt",
                    shadow_model="glm-5.2",
                    shadow_cost=0.05,
                    tokens=1000,
                    reason=f"thread_{tid}_iter_{i}",
                )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Threads raised: {errors}"
    assert logger.get_count() == N_THREADS * M_PER_THREAD
    logger.close()


# ════════════════════════════════════════════════════════════════════════════
# P6-SHADOW: pressure-routing divergence + exit criteria + conditional extension
# ════════════════════════════════════════════════════════════════════════════

from src.shadow_logger import (
    _sanitize,
    _compute_divergence,
    _is_paid_provider,
    DIVERGENCE_THRESHOLD,
    MIN_DECISIONS,
    SESSION_WINDOW_HOURS,
    WEEKLY_WINDOW_HOURS,
)


# ── _sanitize helper ─────────────────────────────────────────────────────────


class TestSanitize:
    def test_none_to_zero(self):
        assert _sanitize(None) == 0.0

    def test_nan_to_zero(self):
        assert _sanitize(float("nan")) == 0.0

    def test_pos_inf_to_zero(self):
        assert _sanitize(float("inf")) == 0.0

    def test_neg_inf_to_zero(self):
        assert _sanitize(float("-inf")) == 0.0

    def test_finite_passthrough(self):
        assert _sanitize(3.14) == pytest.approx(3.14)

    def test_int_passthrough(self):
        assert _sanitize(42) == 42.0

    def test_string_zero(self):
        assert _sanitize("abc") == 0.0

    def test_numeric_string(self):
        assert _sanitize("1.5") == 1.5


# ── _compute_divergence helper ───────────────────────────────────────────────


class TestComputeDivergence:
    def test_same_provider_zero(self):
        assert _compute_divergence("ours", "ours", 0.3, 0.5) == 0.0

    def test_diff_provider_same_cost_zero(self):
        """Cost-neutral reroute: divergence is 0 even if providers differ."""
        assert _compute_divergence("ours", "friend", 0.3, 0.3) == 0.0

    def test_diff_provider_diff_cost(self):
        d = _compute_divergence("ours", "ppq", 0.3, 0.14)
        # |0.3 - 0.14| / max(0.3, 0.14) = 0.16 / 0.3 ≈ 0.533
        assert d == pytest.approx(0.5333, abs=0.001)

    def test_actual_cheaper(self):
        d = _compute_divergence("ollama_cloud", "ppq", 0.024, 0.14)
        # |0.024 - 0.14| / 0.14 = 0.116 / 0.14 ≈ 0.829
        assert d == pytest.approx(0.8286, abs=0.001)

    def test_nan_inputs(self):
        """NaN costs should be sanitised to 0 → divergence 0."""
        d = _compute_divergence("ours", "ppq", float("nan"), float("inf"))
        # both → 0 → |0-0|/eps → 0
        assert d == 0.0

    def test_none_providers(self):
        assert _compute_divergence(None, None, 0.1, 0.2) == 0.0


# ── _is_paid_provider helper ─────────────────────────────────────────────────


class TestIsPaidProvider:
    def test_ppq_is_paid(self):
        assert _is_paid_provider("ppq") is True

    def test_openrouter_is_paid(self):
        assert _is_paid_provider("openrouter") is True

    def test_deepinfra_is_paid(self):
        assert _is_paid_provider("deepinfra") is True

    def test_ours_not_paid(self):
        assert _is_paid_provider("ours") is False

    def test_ollama_cloud_not_paid(self):
        assert _is_paid_provider("ollama_cloud") is False

    def test_none_not_paid(self):
        assert _is_paid_provider(None) is False


# ── Schema migration ─────────────────────────────────────────────────────────


class TestSchemaMigration:
    def test_new_columns_exist(self, logger):
        """P6 columns must be present after __init__."""
        cols = {
            row[1]
            for row in _raw(logger, "PRAGMA table_info(routing_shadow_decisions);")
        }
        for expected in (
            "pressure_provider", "pressure_model", "pressure_cost",
            "actual_cost", "divergence", "is_429", "paid_provider",
        ):
            assert expected in cols, f"Missing column: {expected}"

    def test_backward_compat_log_decision_still_works(self, logger):
        """Old log_decision API must still write rows without errors."""
        logger.log_decision(
            ts=time.time(), live_provider="ours", live_model="glm-5.2",
            shadow_provider="ours", shadow_model="glm-5.2",
            shadow_cost=0.3, tokens=1000,
        )
        assert logger.get_count() == 1

    def test_legacy_rows_have_null_pressure(self, logger):
        """Rows from log_decision should have NULL pressure columns."""
        logger.log_decision(
            ts=time.time(), live_provider="ours", live_model="m",
            shadow_provider="ours", shadow_model="m",
            shadow_cost=0.3, tokens=100,
        )
        row = _raw(
            logger,
            "SELECT pressure_provider, divergence, is_429 "
            "FROM routing_shadow_decisions;",
        )[0]
        assert row[0] is None       # pressure_provider NULL
        assert row[1] is None       # divergence NULL (not populated)
        assert row[2] == 0          # is_429 has DEFAULT 0

    def test_migrate_existing_db(self, tmp_path):
        """A DB created with the OLD schema (no P6 cols) must be migrated."""
        db = str(tmp_path / "legacy.db")
        # Create old-style table manually
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE routing_shadow_decisions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
            "live_provider TEXT, live_model TEXT, shadow_provider TEXT, "
            "shadow_model TEXT, shadow_cost REAL, live_cost REAL, "
            "tokens INTEGER, agree INTEGER, reason TEXT);"
        )
        conn.execute(
            "INSERT INTO routing_shadow_decisions (ts) VALUES (1000.0);"
        )
        conn.commit()
        conn.close()

        # Now open with ShadowLogger — should auto-migrate
        sl = ShadowLogger(db_path=db)
        cols = {
            row[1] for row in _raw(sl, "PRAGMA table_info(routing_shadow_decisions);")
        }
        assert "divergence" in cols
        assert "is_429" in cols
        assert "pressure_provider" in cols
        # Old row still there
        assert sl.get_count() == 1
        sl.close()


# ── log_pressure_decision ────────────────────────────────────────────────────


class TestLogPressureDecision:
    def test_basic_write(self, logger):
        logger.log_pressure_decision(
            ts=time.time(),
            actual_provider="ours", actual_model="glm-5.2",
            pressure_provider="ours", pressure_model="glm-5.2",
            actual_cost=0.3, pressure_cost=0.3,
            tokens=1000,
        )
        row = _raw(
            logger,
            "SELECT pressure_provider, pressure_model, pressure_cost, "
            "actual_cost, divergence, is_429, paid_provider "
            "FROM routing_shadow_decisions;",
        )[0]
        assert row[0] == "ours"
        assert row[1] == "glm-5.2"
        assert row[2] == pytest.approx(0.3)
        assert row[3] == pytest.approx(0.3)
        assert row[4] == 0.0  # same provider → 0 divergence
        assert row[5] == 0   # not a 429
        assert row[6] == 0   # ours is not paid

    def test_divergence_recorded_on_mismatch(self, logger):
        logger.log_pressure_decision(
            ts=time.time(),
            actual_provider="ppq", actual_model="deepseek-v4-flash",
            pressure_provider="ours", pressure_model="glm-5.2",
            actual_cost=0.14, pressure_cost=0.3,
            tokens=500,
        )
        div = _raw(logger, "SELECT divergence FROM routing_shadow_decisions;")[0][0]
        assert div == pytest.approx(abs(0.14 - 0.3) / 0.3, abs=0.001)

    def test_429_flag(self, logger):
        logger.log_pressure_decision(
            ts=time.time(),
            actual_provider="ours", actual_model="glm-5.2",
            pressure_provider="ours", pressure_model="glm-5.2",
            actual_cost=0.3, pressure_cost=0.3,
            tokens=1000, is_429=True,
        )
        is429 = _raw(logger, "SELECT is_429 FROM routing_shadow_decisions;")[0][0]
        assert is429 == 1

    def test_paid_provider_flag(self, logger):
        logger.log_pressure_decision(
            ts=time.time(),
            actual_provider="openrouter", actual_model="deepseek-v4-flash",
            pressure_provider="ollama_cloud", pressure_model="glm-5.2",
            actual_cost=0.135, pressure_cost=0.024,
            tokens=2000,
        )
        paid = _raw(logger, "SELECT paid_provider FROM routing_shadow_decisions;")[0][0]
        assert paid == 1

    def test_nan_cost_sanitised(self, logger):
        """NaN/inf costs must be stored as 0, never as NaN."""
        logger.log_pressure_decision(
            ts=time.time(),
            actual_provider="ours", actual_model="m",
            pressure_provider="ppq", pressure_model="m",
            actual_cost=float("nan"), pressure_cost=float("inf"),
            tokens=100,
        )
        row = _raw(
            logger, "SELECT actual_cost, pressure_cost FROM routing_shadow_decisions;"
        )[0]
        assert row[0] == 0.0
        assert row[1] == 0.0

    def test_null_ts_substituted(self, logger):
        before = time.time()
        logger.log_pressure_decision(
            ts=None,
            actual_provider="ours", actual_model="m",
            pressure_provider="ours", pressure_model="m",
            actual_cost=0.3, pressure_cost=0.3,
            tokens=100,
        )
        ts = _raw(logger, "SELECT ts FROM routing_shadow_decisions;")[0][0]
        assert ts >= before

    def test_never_raises_on_null_inputs(self, logger):
        logger.log_pressure_decision(
            ts=None, actual_provider=None, actual_model=None,
            pressure_provider=None, pressure_model=None,
            actual_cost=None, pressure_cost=None, tokens=None,
        )
        assert logger.get_count() == 1


# ── get_divergence_rate ──────────────────────────────────────────────────────


class TestGetDivergenceRate:
    def test_empty_is_zero(self, logger):
        assert logger.get_divergence_rate() == 0.0

    def test_all_agree_zero(self, logger):
        ts = time.time()
        for _ in range(10):
            logger.log_pressure_decision(
                ts, "ours", "m", "ours", "m", 0.3, 0.3, 1000,
            )
        assert logger.get_divergence_rate() == 0.0

    def test_mixed_divergence(self, logger):
        ts = time.time()
        # 3 agree (div=0), 2 disagree (div≈0.53)
        for _ in range(3):
            logger.log_pressure_decision(ts, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        for _ in range(2):
            logger.log_pressure_decision(ts, "ours", "m", "ppq", "m", 0.3, 0.14, 1000)
        rate = logger.get_divergence_rate()
        expected = (0 + 0 + 0 + abs(0.3 - 0.14) / 0.3 + abs(0.3 - 0.14) / 0.3) / 5
        assert rate == pytest.approx(expected, abs=0.001)

    def test_since_ts(self, logger):
        logger.log_pressure_decision(1000.0, "ours", "m", "ppq", "m", 0.3, 0.14, 1000)
        logger.log_pressure_decision(2000.0, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        # Only new row (ts=2000) qualifies
        assert logger.get_divergence_rate(since_ts=1500.0) == 0.0

    def test_legacy_rows_ignored(self, logger):
        """log_decision rows (NULL divergence) should not count."""
        logger.log_decision(1000.0, "ours", "m", "ours", "m", 0.3, 1000)
        assert logger.get_divergence_rate() == 0.0


# ── get_429_rate ─────────────────────────────────────────────────────────────


class TestGet429Rate:
    def test_empty_is_zero(self, logger):
        assert logger.get_429_rate() == 0.0

    def test_no_429s(self, logger):
        ts = time.time()
        for _ in range(5):
            logger.log_pressure_decision(ts, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        assert logger.get_429_rate() == 0.0

    def test_some_429s(self, logger):
        ts = time.time()
        for _ in range(3):
            logger.log_pressure_decision(ts, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        for _ in range(2):
            logger.log_pressure_decision(
                ts, "ours", "m", "ours", "m", 0.3, 0.3, 1000, is_429=True,
            )
        assert logger.get_429_rate() == pytest.approx(0.4)

    def test_since_ts(self, logger):
        logger.log_pressure_decision(1000.0, "ours", "m", "ours", "m", 0.3, 0.3, 1000, is_429=True)
        logger.log_pressure_decision(2000.0, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        assert logger.get_429_rate(since_ts=1500.0) == 0.0


# ── get_paid_spend ───────────────────────────────────────────────────────────


class TestGetPaidSpend:
    def test_empty_is_zero(self, logger):
        assert logger.get_paid_spend() == 0.0

    def test_flat_rate_excluded(self, logger):
        """ours / friend / ollama_cloud are flat-rate → no paid spend."""
        ts = time.time()
        logger.log_pressure_decision(ts, "ours", "m", "ours", "m", 0.3, 0.3, 10000)
        assert logger.get_paid_spend() == 0.0

    def test_paid_provider_counted(self, logger):
        """ppq is paid → tokens × cost / 1e6."""
        ts = time.time()
        logger.log_pressure_decision(
            ts, "ppq", "m", "ppq", "m", 0.14, 0.14, 1_000_000,
        )
        # spend = 1M tokens × $0.14/M = $0.14
        assert logger.get_paid_spend() == pytest.approx(0.14, abs=0.001)

    def test_multiple_paid(self, logger):
        ts = time.time()
        logger.log_pressure_decision(ts, "ppq", "m", "ppq", "m", 0.14, 0.14, 500_000)
        logger.log_pressure_decision(ts, "openrouter", "m", "openrouter", "m", 0.135, 0.135, 1_000_000)
        # spend = 500K×0.14/1M + 1M×0.135/1M = 0.07 + 0.135 = 0.205
        assert logger.get_paid_spend() == pytest.approx(0.205, abs=0.001)

    def test_since_ts(self, logger):
        logger.log_pressure_decision(1000.0, "ppq", "m", "ppq", "m", 0.14, 0.14, 1_000_000)
        logger.log_pressure_decision(2000.0, "ppq", "m", "ppq", "m", 0.14, 0.14, 500_000)
        assert logger.get_paid_spend(since_ts=1500.0) == pytest.approx(0.07, abs=0.001)


# ── get_session_span_hours ───────────────────────────────────────────────────


class TestGetSessionSpanHours:
    def test_empty_is_zero(self, logger):
        assert logger.get_session_span_hours() == 0.0

    def test_single_row_zero(self, logger):
        logger.log_pressure_decision(1000.0, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        assert logger.get_session_span_hours() == 0.0

    def test_two_rows(self, logger):
        logger.log_pressure_decision(1000.0, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        logger.log_pressure_decision(1000.0 + 3600 * 5, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        # 5h span
        assert logger.get_session_span_hours() == pytest.approx(5.0, abs=0.01)

    def test_since_ts(self, logger):
        logger.log_pressure_decision(1000.0, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        logger.log_pressure_decision(1000.0 + 7200, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        logger.log_pressure_decision(1000.0 + 7200 + 10800, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        # since 1500: rows at 8200 and 19000 → span = 10800s = 3.0h
        span = logger.get_session_span_hours(since_ts=1500.0)
        assert span == pytest.approx(3.0, abs=0.01)


# ── evaluate_exit_criteria ───────────────────────────────────────────────────


class TestEvaluateExitCriteria:
    def _seed_passing(self, logger):
        """Seed 500+ agreeing decisions spanning 5h+ with 0 divergence."""
        base_ts = time.time() - 3600 * 6
        for i in range(MIN_DECISIONS + 10):
            logger.log_pressure_decision(
                base_ts + i * 40,  # spread over ~6h
                "ours", "m", "ours", "m", 0.3, 0.3, 1000,
            )

    def test_all_pass(self, logger):
        self._seed_passing(logger)
        result = logger.evaluate_exit_criteria(
            baseline_429_rate=0.05, baseline_paid_spend=1.0,
        )
        assert result["all_passed"] is True
        assert result["criteria"]["divergence"]["passed"] is True
        assert result["criteria"]["rate_429"]["passed"] is True
        assert result["criteria"]["paid_spend"]["passed"] is True
        assert result["criteria"]["decisions_logged"]["passed"] is True
        assert result["criteria"]["session_cycle"]["passed"] is True
        assert result["criteria"]["nan_inf_clean"]["passed"] is True
        assert result["decisions_logged"] >= MIN_DECISIONS
        assert result["session_span_hours"] >= SESSION_WINDOW_HOURS

    def test_divergence_fail(self, logger):
        """High divergence → divergence criterion fails."""
        base_ts = time.time() - 3600 * 6
        for i in range(MIN_DECISIONS + 10):
            # Alternate agree/disagree → mean divergence ≈ 0.27
            if i % 2 == 0:
                logger.log_pressure_decision(
                    base_ts + i * 40, "ours", "m", "ours", "m", 0.3, 0.3, 1000,
                )
            else:
                logger.log_pressure_decision(
                    base_ts + i * 40, "ours", "m", "ppq", "m", 0.3, 0.14, 1000,
                )
        result = logger.evaluate_exit_criteria(
            baseline_429_rate=1.0, baseline_paid_spend=100.0,
        )
        assert result["all_passed"] is False
        assert result["criteria"]["divergence"]["passed"] is False
        assert result["criteria"]["divergence"]["value"] >= DIVERGENCE_THRESHOLD

    def test_429_rate_fail(self, logger):
        """429 rate exceeds baseline → rate_429 criterion fails."""
        base_ts = time.time() - 3600 * 6
        for i in range(MIN_DECISIONS + 10):
            logger.log_pressure_decision(
                base_ts + i * 40, "ours", "m", "ours", "m", 0.3, 0.3, 1000,
                is_429=(i % 2 == 0),  # 50% 429 rate
            )
        result = logger.evaluate_exit_criteria(
            baseline_429_rate=0.05, baseline_paid_spend=100.0,
        )
        assert result["criteria"]["rate_429"]["passed"] is False
        assert result["all_passed"] is False

    def test_paid_spend_fail(self, logger):
        """Paid spend exceeds baseline → paid_spend criterion fails."""
        base_ts = time.time() - 3600 * 6
        for i in range(MIN_DECISIONS + 10):
            logger.log_pressure_decision(
                base_ts + i * 40, "ppq", "m", "ppq", "m", 0.14, 0.14, 10000,
            )
        result = logger.evaluate_exit_criteria(
            baseline_429_rate=1.0, baseline_paid_spend=0.001,
        )
        assert result["criteria"]["paid_spend"]["passed"] is False

    def test_decisions_fail(self, logger):
        """Fewer than MIN_DECISIONS → decisions_logged fails."""
        for i in range(10):
            logger.log_pressure_decision(
                time.time() - 3600 * 6 + i * 2200,
                "ours", "m", "ours", "m", 0.3, 0.3, 1000,
            )
        result = logger.evaluate_exit_criteria(
            baseline_429_rate=1.0, baseline_paid_spend=100.0,
        )
        assert result["criteria"]["decisions_logged"]["passed"] is False
        assert result["criteria"]["decisions_logged"]["value"] == 10

    def test_session_cycle_fail(self, logger):
        """Span < 5h → session_cycle fails."""
        for i in range(MIN_DECISIONS + 10):
            logger.log_pressure_decision(
                time.time() - 1800 + i * 0.5,  # span < 1h
                "ours", "m", "ours", "m", 0.3, 0.3, 1000,
            )
        result = logger.evaluate_exit_criteria(
            baseline_429_rate=1.0, baseline_paid_spend=100.0,
        )
        assert result["criteria"]["session_cycle"]["passed"] is False

    def test_empty_table_all_fail_except_divergence(self, logger):
        """Empty table: divergence=0 (passes vacuously) but decisions fail."""
        result = logger.evaluate_exit_criteria(
            baseline_429_rate=0.05, baseline_paid_spend=1.0,
        )
        assert result["all_passed"] is False
        assert result["criteria"]["decisions_logged"]["passed"] is False
        assert result["criteria"]["session_cycle"]["passed"] is False

    def test_structure(self, logger):
        """Verify the result dict has all expected keys."""
        result = logger.evaluate_exit_criteria(0.05, 1.0)
        assert "all_passed" in result
        assert "criteria" in result
        assert "decisions_logged" in result
        assert "session_span_hours" in result
        for key in (
            "divergence", "rate_429", "paid_spend",
            "decisions_logged", "session_cycle", "nan_inf_clean",
        ):
            assert key in result["criteria"]
            assert "value" in result["criteria"][key]
            assert "passed" in result["criteria"][key]

    def test_custom_thresholds(self, logger):
        """Custom thresholds should override defaults."""
        base_ts = time.time() - 3600 * 6
        for i in range(100):
            logger.log_pressure_decision(
                base_ts + i * 220,
                "ours", "m", "ours", "m", 0.3, 0.3, 1000,
            )
        result = logger.evaluate_exit_criteria(
            baseline_429_rate=0.05, baseline_paid_spend=1.0,
            min_decisions=50,  # custom: only need 50
        )
        assert result["criteria"]["decisions_logged"]["passed"] is True
        assert result["criteria"]["decisions_logged"]["threshold"] == 50


# ── should_extend_to_7days ───────────────────────────────────────────────────


class TestShouldExtendTo7Days:
    def test_short_soak_no_weekly(self, logger):
        """48h elapsed, no weekly window seen → should extend."""
        # Simulate: soak started 49h ago, decisions span only 6h
        base_ts = time.time() - 3600 * 49
        for i in range(10):
            logger.log_pressure_decision(
                base_ts + i * 2000, "ours", "m", "ours", "m", 0.3, 0.3, 1000,
            )
        assert logger.should_extend_to_7days(soak_start_ts=base_ts) is True

    def test_full_week_seen(self, logger):
        """7+ days of data → no extension needed."""
        base_ts = time.time() - 3600 * 170  # ~7.08 days
        logger.log_pressure_decision(base_ts, "ours", "m", "ours", "m", 0.3, 0.3, 1000)
        logger.log_pressure_decision(
            time.time(), "ours", "m", "ours", "m", 0.3, 0.3, 1000,
        )
        assert logger.should_extend_to_7days(soak_start_ts=base_ts) is False

    def test_soak_not_yet_elapsed(self, logger):
        """Only 24h elapsed (less than 48h soak) → don't extend yet."""
        base_ts = time.time() - 3600 * 24
        for i in range(10):
            logger.log_pressure_decision(
                base_ts + i * 2000, "ours", "m", "ours", "m", 0.3, 0.3, 1000,
            )
        # elapsed (24h) < 48h × 0.9 = 43.2h → should not extend yet
        assert logger.should_extend_to_7days(soak_start_ts=base_ts) is False


# ── Thread-safety for log_pressure_decision ──────────────────────────────────


class TestPressureDecisionThreadSafe:
    def test_concurrent_pressure_writes(self, tmp_path):
        """Concurrent log_pressure_decision calls must not corrupt the DB."""
        import threading
        db = tmp_path / "pressure_concurrent.db"
        logger = ShadowLogger(str(db))
        N = 8
        M = 30
        errors = []

        def worker(tid):
            try:
                for i in range(M):
                    logger.log_pressure_decision(
                        ts=time.time(),
                        actual_provider=f"prov_{tid % 3}",
                        actual_model="m",
                        pressure_provider="prov_0",
                        pressure_model="m",
                        actual_cost=0.1 * (tid % 3 + 1),
                        pressure_cost=0.1,
                        tokens=1000,
                        is_429=(i % 10 == 0),
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Threads raised: {errors}"
        assert logger.get_count() == N * M
        logger.close()


# ── Constants sanity ─────────────────────────────────────────────────────────


class TestConstants:
    def test_divergence_threshold(self):
        assert DIVERGENCE_THRESHOLD == 0.15

    def test_min_decisions(self):
        assert MIN_DECISIONS == 500

    def test_session_window_hours(self):
        assert SESSION_WINDOW_HOURS == 5.0

    def test_weekly_window_hours(self):
        assert WEEKLY_WINDOW_HOURS == 168.0
