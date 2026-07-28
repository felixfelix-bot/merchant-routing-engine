"""Tests for the model-aware CPVO extension (Phase 4.5b).

These tests verify that :class:`~src.cpvo_calculator.CPVOCalculator` correctly
tracks quality per ``(provider, model)`` pair when the ``provider_telemetry``
table has a ``model`` column, while remaining fully backward-compatible with
old schemas that lack it.

Key scenarios covered:

* **Model-filtered queries**: when *model* is given and the column exists,
  only rows for that model are counted.
* **Same provider, different quality**: ``glm-5.2`` (reliable) vs
  ``glm-4.5-flash`` (flaky) on the same zai provider get different penalties.
* **Fallback**: per-model sample count < MIN_SAMPLES → falls back to
  provider-level aggregate.
* **Backward compatibility**: tables without a ``model`` column still work
  (model parameter is silently ignored).
* **Integration with model_mapping**: resolving models via
  ``get_model(provider, task_type)`` and using them for quality lookups.
* **Never raises**: error paths return base rates, never exceptions.

The module under test is ``src.cpvo_calculator.CPVOCalculator``.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# ── Import path setup ──────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.cpvo_calculator import CPVOCalculator, MIN_SAMPLES
from src.model_mapping import get_model

# ── Schema variants ────────────────────────────────────────────────────────

#: Schema WITHOUT a model column (legacy / pre-4.5b).
_SCHEMA_NO_MODEL = """CREATE TABLE IF NOT EXISTS provider_telemetry (
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

#: Schema WITH a model column (Phase 4.5b).
_SCHEMA_WITH_MODEL = """CREATE TABLE IF NOT EXISTS provider_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="cpvo_model_test_")
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


# ── Helpers ────────────────────────────────────────────────────────────────


def _insert_row(
    conn: sqlite3.Connection,
    provider: str = "zai",
    model: str | None = None,
    response_valid: bool = True,
    response_received: bool = True,
    latency_ms: int = 200,
    billed_tokens: int = 1000,
    actual_tokens: int = 1000,
    token_mismatch: bool = False,
    error_type: str = "none",
    ts: datetime | None = None,
    has_model_col: bool = True,
):
    """Insert one telemetry row.

    When *has_model_col* is True, the row includes the ``model`` column;
    otherwise it's inserted into the legacy schema.
    """
    if ts is None:
        ts = datetime.now(timezone.utc)
    if has_model_col:
        conn.execute(
            "INSERT INTO provider_telemetry "
            "(ts, provider, model, response_received, response_valid, "
            "latency_ms, error_type, billed_tokens, actual_tokens, "
            "token_mismatch) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ts.isoformat(),
                provider,
                model,
                int(response_received),
                int(response_valid),
                latency_ms,
                error_type,
                billed_tokens,
                actual_tokens,
                int(token_mismatch),
            ),
        )
    else:
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


def _create_db(db_path: str, schema: str) -> None:
    """Create the telemetry table with the given schema."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(schema)
    conn.close()


# ── Tests: compute_cpvo with model dimension ───────────────────────────────


class TestComputeCpvoModelAware:
    """compute_cpvo respects the model filter when the column exists."""

    def test_model_filter_isolates_quality(self, calc, tmp_db_path):
        """Two models on the same provider have different CPVO.

        glm-5.2: 200 requests, all valid, 1000 billed each.
        glm-4.5-flash: 200 requests, 80% valid, 1000 billed each (even
        the failed ones — wasted spend is what CPVO captures).

        Without model filtering, they'd be mixed.  With it, each model
        gets its own correct CPVO.
        """
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        # glm-5.2: 200 valid
        for _ in range(200):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=True, billed_tokens=1000)
        # glm-4.5-flash: 160 valid + 40 invalid (all billed 1000)
        for _ in range(160):
            _insert_row(conn, provider="zai", model="glm-4.5-flash",
                        response_valid=True, billed_tokens=1000)
        for _ in range(40):
            _insert_row(conn, provider="zai", model="glm-4.5-flash",
                        response_valid=False, billed_tokens=1000,
                        response_received=False, error_type="timeout")
        conn.close()

        # glm-5.2: all 200 valid
        # total_cost = 200 * 1000 / 1M * 1.0 = 0.2; CPVO = 0.2 / 200 = 0.001
        cpvo_good = calc.compute_cpvo("zai", base_rate=1.0, model="glm-5.2")
        assert cpvo_good is not None
        assert abs(cpvo_good - 0.001) < 1e-9

        # glm-4.5-flash: 200 total billed but only 160 successes
        # total_cost = 200 * 1000 / 1M * 1.0 = 0.2; CPVO = 0.2 / 160 = 0.00125
        cpvo_bad = calc.compute_cpvo("zai", base_rate=1.0,
                                     model="glm-4.5-flash")
        assert cpvo_bad is not None
        assert abs(cpvo_bad - 0.00125) < 1e-9

    def test_model_none_aggregates_all_models(self, calc, tmp_db_path):
        """model=None aggregates across all models (provider-level)."""
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        for _ in range(100):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=True, billed_tokens=500)
        for _ in range(100):
            _insert_row(conn, provider="zai", model="glm-4.5-flash",
                        response_valid=True, billed_tokens=1500)
        conn.close()

        # Provider-level: 200 total, all valid
        # total_cost = (100*500 + 100*1500) / 1M * 1.0 = 200000/1M = 0.2
        # CPVO = 0.2 / 200 = 0.001
        cpvo = calc.compute_cpvo("zai", base_rate=1.0, model=None)
        assert cpvo is not None
        assert abs(cpvo - 0.001) < 1e-9

    def test_model_filter_excludes_other_models(self, calc, tmp_db_path):
        """Querying model A does not see model B's data."""
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        # 100 valid for glm-5.2
        for _ in range(100):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=True, billed_tokens=1000)
        # 100 invalid for glm-4.5-flash (would tank provider-level rate)
        for _ in range(100):
            _insert_row(conn, provider="zai", model="glm-4.5-flash",
                        response_valid=False, billed_tokens=0,
                        response_received=False, error_type="error")
        conn.close()

        # glm-5.2 query should NOT be affected by flash failures
        score = calc.get_quality_score("zai", base_rate=1.0, model="glm-5.2")
        assert score["success_rate"] == 1.0  # all 100 glm-5.2 valid
        assert score["sample_count"] == 100

    def test_model_with_insufficient_samples(self, calc, tmp_db_path):
        """Per-model < MIN_SAMPLES → compute_cpvo returns base_rate."""
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        for _ in range(50):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=True, billed_tokens=1000)
        conn.close()

        cpvo = calc.compute_cpvo("zai", base_rate=2.0, model="glm-5.2")
        assert cpvo == 2.0  # insufficient → base_rate


# ── Tests: get_effective_rates_model_aware ─────────────────────────────────


class TestGetEffectiveRatesModelAware:
    """get_effective_rates_model_aware penalises per (provider, model)."""

    def test_different_penalty_for_different_models(self, calc, tmp_db_path):
        """Same provider, different models → different effective rates.

        glm-5.2: 96% success → no penalty.
        glm-4.5-flash: 80% success → base / 0.8 penalty.
        """
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        # glm-5.2: 192 valid + 8 invalid = 96% success
        for _ in range(192):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=True, billed_tokens=100)
        for _ in range(8):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=False, billed_tokens=0,
                        response_received=False, error_type="timeout")
        # glm-4.5-flash: 160 valid + 40 invalid = 80% success
        for _ in range(160):
            _insert_row(conn, provider="zai", model="glm-4.5-flash",
                        response_valid=True, billed_tokens=100)
        for _ in range(40):
            _insert_row(conn, provider="zai", model="glm-4.5-flash",
                        response_valid=False, billed_tokens=0,
                        response_received=False, error_type="timeout")
        conn.close()

        rates = calc.get_effective_rates_model_aware({
            ("zai", "glm-5.2"): 1.0,
            ("zai", "glm-4.5-flash"): 1.0,
        })
        assert rates[("zai", "glm-5.2")] == 1.0           # 96% → no penalty
        assert abs(rates[("zai", "glm-4.5-flash")] - 1.25) < 1e-9  # 80% → 1/0.8

    def test_fallback_to_provider_level(self, calc, tmp_db_path):
        """Insufficient per-model samples → falls back to provider-level.

        Provider has 200 total samples (100% success) but model X only has 50.
        The method should use provider-level quality (100%) → no penalty.
        """
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        # model X: only 50 samples (below MIN_SAMPLES)
        for _ in range(50):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=True, billed_tokens=100)
        # model Y: 200 samples, also valid
        for _ in range(200):
            _insert_row(conn, provider="zai", model="glm-4.5-flash",
                        response_valid=True, billed_tokens=100)
        conn.close()

        rates = calc.get_effective_rates_model_aware({
            ("zai", "glm-5.2"): 1.0,  # only 50 per-model → fallback
        })
        # Fallback to provider-level: 250 total, 250 valid = 100% → no penalty
        assert rates[("zai", "glm-5.2")] == 1.0

    def test_fallback_still_insufficient(self, calc, tmp_db_path):
        """Both per-model and provider-level insufficient → base rate."""
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        for _ in range(10):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=True, billed_tokens=100)
        conn.close()

        rates = calc.get_effective_rates_model_aware({
            ("zai", "glm-5.2"): 3.3,
        })
        # Only 10 samples total → insufficient → base rate
        assert rates[("zai", "glm-5.2")] == 3.3

    def test_zero_success_max_penalty(self, calc, tmp_db_path):
        """Per-model 0% success → max penalty."""
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        for _ in range(100):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=False, billed_tokens=0,
                        response_received=False, error_type="error")
        conn.close()

        rates = calc.get_effective_rates_model_aware({
            ("zai", "glm-5.2"): 1.0,
        })
        assert rates[("zai", "glm-5.2")] >= 1e6

    def test_model_none_key(self, calc, tmp_db_path):
        """Key with model=None uses provider-level aggregation."""
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        for _ in range(160):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=True, billed_tokens=100)
        for _ in range(40):
            _insert_row(conn, provider="zai", model="glm-4.5-flash",
                        response_valid=False, billed_tokens=0,
                        response_received=False, error_type="timeout")
        conn.close()

        rates = calc.get_effective_rates_model_aware({
            ("zai", None): 1.0,  # provider-level: 160/200 = 80%
        })
        assert abs(rates[("zai", None)] - 1.25) < 1e-9

    def test_empty_table(self, calc, tmp_db_path):
        """No data at all → base rates unchanged."""
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        rates = calc.get_effective_rates_model_aware({
            ("zai", "glm-5.2"): 1.0,
            ("ppq", "kimi-k3"): 2.0,
        })
        assert rates == {("zai", "glm-5.2"): 1.0, ("ppq", "kimi-k3"): 2.0}


# ── Tests: get_quality_score with model dimension ──────────────────────────


class TestGetQualityScoreModelAware:
    """get_quality_score respects the model filter."""

    def test_model_specific_quality(self, calc, tmp_db_path):
        """Quality score for one model doesn't leak another's data."""
        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        # glm-5.2: all valid
        for _ in range(200):
            _insert_row(conn, provider="zai", model="glm-5.2",
                        response_valid=True, billed_tokens=1000,
                        latency_ms=100)
        # glm-4.5-flash: 50% valid (but enough to be > MIN_SAMPLES)
        for _ in range(100):
            _insert_row(conn, provider="zai", model="glm-4.5-flash",
                        response_valid=True, billed_tokens=1000,
                        latency_ms=500)
        for _ in range(100):
            _insert_row(conn, provider="zai", model="glm-4.5-flash",
                        response_valid=False, billed_tokens=0,
                        response_received=False, error_type="timeout",
                        latency_ms=5000)
        conn.close()

        score_good = calc.get_quality_score("zai", base_rate=1.0,
                                            model="glm-5.2")
        assert score_good["success_rate"] == 1.0
        assert score_good["sample_count"] == 200
        assert score_good["effective_rate"] == 1.0  # 100% → no penalty

        score_bad = calc.get_quality_score("zai", base_rate=1.0,
                                           model="glm-4.5-flash")
        assert abs(score_bad["success_rate"] - 0.5) < 1e-9
        assert score_bad["sample_count"] == 200
        assert abs(score_bad["effective_rate"] - 2.0) < 1e-9  # 1.0/0.5


# ── Tests: backward compatibility ───────────────────────────────────────────


class TestBackwardCompatibility:
    """Old tables without a model column still work with model parameter."""

    def test_model_param_ignored_on_legacy_schema(self, calc, tmp_db_path):
        """model=X on a table WITHOUT model column → provider-level query."""
        _create_db(tmp_db_path, _SCHEMA_NO_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        for _ in range(200):
            _insert_row(conn, provider="zai", response_valid=True,
                        billed_tokens=1000, has_model_col=False)
        conn.close()

        # Passing model="glm-5.2" should NOT break — column doesn't exist,
        # so it silently falls back to provider-level.
        cpvo = calc.compute_cpvo("zai", base_rate=1.0, model="glm-5.2")
        assert cpvo is not None
        # Provider-level: 200 * 1000 / 1M * 1.0 / 200 = 0.001
        assert abs(cpvo - 0.001) < 1e-9

    def test_model_aware_rates_on_legacy_schema(self, calc, tmp_db_path):
        """get_effective_rates_model_aware works on legacy schema."""
        _create_db(tmp_db_path, _SCHEMA_NO_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        for _ in range(160):
            _insert_row(conn, provider="zai", response_valid=True,
                        billed_tokens=100, has_model_col=False)
        for _ in range(40):
            _insert_row(conn, provider="zai", response_valid=False,
                        billed_tokens=0, response_received=False,
                        error_type="timeout", has_model_col=False)
        conn.close()

        rates = calc.get_effective_rates_model_aware({
            ("zai", "glm-5.2"): 1.0,
        })
        # No model column → provider-level: 160/200 = 80% → 1/0.8
        assert abs(rates[("zai", "glm-5.2")] - 1.25) < 1e-9

    def test_quality_score_on_legacy_schema(self, calc, tmp_db_path):
        """get_quality_score with model param on legacy schema."""
        _create_db(tmp_db_path, _SCHEMA_NO_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        for _ in range(200):
            _insert_row(conn, provider="zai", response_valid=True,
                        billed_tokens=100, has_model_col=False)
        conn.close()

        score = calc.get_quality_score("zai", base_rate=1.0,
                                       model="glm-5.2")
        assert score["sample_count"] == 200
        assert score["success_rate"] == 1.0


# ── Tests: model_mapping integration ────────────────────────────────────────


class TestModelMappingIntegration:
    """Integration with src.model_mapping.get_model()."""

    def test_resolve_and_query(self, calc, tmp_db_path):
        """Resolve model via get_model, then use it for quality lookup.

        This simulates the real routing flow:
        1. Router decides to use provider 'zai' for 'coding' task.
        2. get_model('zai', 'coding') → 'glm-5.2'.
        3. CPVO calculator queries quality for (zai, glm-5.2).
        """
        resolved_model = get_model("ours", "coding")
        assert resolved_model == "glm-5.2"

        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        for _ in range(200):
            _insert_row(conn, provider="zai", model=resolved_model,
                        response_valid=True, billed_tokens=1000)
        conn.close()

        # Now use the resolved model for quality
        rates = calc.get_effective_rates_model_aware({
            ("zai", resolved_model): 1.0,
        })
        assert rates[("zai", resolved_model)] == 1.0  # 100% → no penalty

    def test_different_task_types_different_models(self, calc, tmp_db_path):
        """coding and simple tasks resolve to different models."""
        coding_model = get_model("zai", "coding")
        simple_model = get_model("zai", "simple")
        assert coding_model != simple_model  # glm-5.2 vs glm-4.5-flash

        _create_db(tmp_db_path, _SCHEMA_WITH_MODEL)
        conn = sqlite3.connect(tmp_db_path, isolation_level=None)
        # coding model: reliable
        for _ in range(200):
            _insert_row(conn, provider="zai", model=coding_model,
                        response_valid=True, billed_tokens=1000)
        # simple model: less reliable
        for _ in range(160):
            _insert_row(conn, provider="zai", model=simple_model,
                        response_valid=True, billed_tokens=1000)
        for _ in range(40):
            _insert_row(conn, provider="zai", model=simple_model,
                        response_valid=False, billed_tokens=0,
                        response_received=False, error_type="timeout")
        conn.close()

        rates = calc.get_effective_rates_model_aware({
            ("zai", coding_model): 1.0,
            ("zai", simple_model): 1.0,
        })
        assert rates[("zai", coding_model)] == 1.0  # 100% → no penalty
        assert abs(rates[("zai", simple_model)] - 1.25) < 1e-9  # 80% → 1/0.8


# ── Tests: never raises ─────────────────────────────────────────────────────


class TestModelAwareNeverRaises:
    """Model-aware paths must never raise."""

    def test_model_aware_bad_path(self):
        """Bad DB path → returns base rates, never raises."""
        calc = CPVOCalculator(db_path="/nonexistent/path/that/does/not/exist.db")
        rates = calc.get_effective_rates_model_aware({
            ("zai", "glm-5.2"): 1.0,
            ("ppq", "kimi-k3"): 2.0,
        })
        assert rates == {("zai", "glm-5.2"): 1.0, ("ppq", "kimi-k3"): 2.0}

    def test_model_compute_bad_path(self):
        """Bad DB path + model → returns base_rate."""
        calc = CPVOCalculator(db_path="/nonexistent/path/that/does/not/exist.db")
        assert calc.compute_cpvo("zai", base_rate=5.0, model="glm-5.2") == 5.0

    def test_model_quality_score_bad_path(self):
        """Bad DB path + model → returns valid score dict."""
        calc = CPVOCalculator(db_path="/nonexistent/path/that/does/not/exist.db")
        score = calc.get_quality_score("zai", base_rate=1.0, model="glm-5.2")
        assert isinstance(score, dict)
        assert score["sample_count"] == 0
