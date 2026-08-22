"""Tests for src/token_predictor.py — per-model p50/p90 token predictor (CG-3).

Plan: docs/PLAN-cost-gate-reform-v2-2026-08-21.md §6 CG-3 (v1 CG-2 core):

  - p50/p90 of ``total_tokens`` per model from ``api_calls``
    (status_code=200, recent window)
  - task dimension via ``TASK_PROFILES.budget_mult`` (A3) until the proxy
    logs ``task_type`` (CG-5) — the join key cannot be seeded from history
    because the column is all-NULL today
  - confidence bucket by sample count (n<30 → low)
  - cold model → conservative default × penalty, still answers (the gate
    fails closed on *price*; the predictor must ALWAYS return a number with
    a confidence flag)
  - NO Kalman inputs until ``kalman_health.py --short`` is green
    (verified ✗ unhealthy 2026-08-22 — pinned by tests below)
  - drift re-seed: seed-then-replace (a re-seed fully replaces the stats
    table; stale models must not linger)
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.cost_gate import evaluate_cost_gate  # noqa: E402
from src.dispatch_gate import TASK_PROFILES, resolve_task_profile  # noqa: E402

# Module under test — import at the END of the import block is fine for
# pytest, but keep it here so a missing module fails every test loudly.
from src import token_predictor as tp  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

NOW = 1_800_000_000.0  # fixed clock for deterministic windows
WINDOW_DAYS = 30


def make_source_db(path, rows):
    """Create a synthetic zai_usage.db-shaped source with ``api_calls`` rows.

    ``rows``: iterable of ``(model, total_tokens, status_code, ts_offset_days)``
    (``ts`` = NOW - offset_days*86400).
    """
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY,
            ts REAL, key_name TEXT, key_suffix TEXT, model TEXT,
            prompt_tokens INTEGER, completion_tokens INTEGER,
            total_tokens INTEGER, tier TEXT, cache_hit INTEGER DEFAULT 0,
            ollama_hit INTEGER DEFAULT 0, ppq_hit INTEGER DEFAULT 0,
            status_code INTEGER, error TEXT, duration_ms INTEGER,
            cost_usd REAL, cost_source TEXT, session_id TEXT, task_type TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO api_calls (ts, model, total_tokens, status_code) "
        "VALUES (?, ?, ?, ?)",
        [(NOW - off * 86400.0, m, t, s) for (m, t, s, off) in rows],
    )
    conn.commit()
    conn.close()


def ref_percentile(xs, pct):
    """Independent linear-interpolation percentile (numpy default method)."""
    xs = sorted(float(v) for v in xs)
    if not xs:
        raise ValueError("empty")
    if len(xs) == 1:
        return xs[0]
    rank = (pct / 100.0) * (len(xs) - 1)
    lo, hi = math.floor(rank), math.ceil(rank)
    return xs[lo] * (1 - (rank - lo)) + xs[hi] * (rank - lo)


# ── percentile correctness on synthetic sqlite ───────────────────────────────


class TestComputeModelStats:
    def test_p50_p90_exact_literals(self, tmp_path):
        """tokens 10..100 step 10 → p50=55.0, p90=91.0 (hand-computed)."""
        rows = [("model-a", 10 * i, 200, 0.1) for i in range(1, 11)]
        src = str(tmp_path / "src.db")
        make_source_db(src, rows)
        stats = tp.compute_model_stats(src, window_days=WINDOW_DAYS, now_ts=NOW)
        assert stats["models"]["model-a"]["n"] == 10
        assert stats["models"]["model-a"]["p50"] == pytest.approx(55.0)
        assert stats["models"]["model-a"]["p90"] == pytest.approx(91.0)

    def test_matches_reference_percentile_random(self, tmp_path):
        import random

        rng = random.Random(42)
        vals = [rng.randint(500, 90_000) for _ in range(137)]
        rows = [("model-b", v, 200, 0.2) for v in vals]
        src = str(tmp_path / "src.db")
        make_source_db(src, rows)
        stats = tp.compute_model_stats(src, window_days=WINDOW_DAYS, now_ts=NOW)
        got = stats["models"]["model-b"]
        assert got["p50"] == pytest.approx(ref_percentile(vals, 50))
        assert got["p90"] == pytest.approx(ref_percentile(vals, 90))
        assert got["mean"] == pytest.approx(sum(vals) / len(vals))
        assert got["max"] == float(max(vals))

    def test_status_code_filter(self, tmp_path):
        """Only status_code=200 rows feed the stats (429/500 excluded)."""
        rows = (
            [("model-c", 1000, 200, 0.1)] * 5
            + [("model-c", 999_999, 429, 0.1)] * 3
            + [("model-c", 888_888, 500, 0.1)] * 2
        )
        src = str(tmp_path / "src.db")
        make_source_db(src, rows)
        stats = tp.compute_model_stats(src, window_days=WINDOW_DAYS, now_ts=NOW)
        assert stats["models"]["model-c"]["n"] == 5
        assert stats["models"]["model-c"]["p50"] == pytest.approx(1000.0)

    def test_window_filter(self, tmp_path):
        """Rows older than window_days are excluded."""
        rows = [("model-d", 1000, 200, 0.5)] * 4 + [("model-d", 77_777, 200, 45.0)]
        src = str(tmp_path / "src.db")
        make_source_db(src, rows)
        stats = tp.compute_model_stats(src, window_days=WINDOW_DAYS, now_ts=NOW)
        assert stats["models"]["model-d"]["n"] == 4

    def test_zero_and_null_tokens_excluded(self, tmp_path):
        rows = [
            ("model-e", 500, 200, 0.1),
            ("model-e", 0, 200, 0.1),
            ("model-e", None, 200, 0.1),
        ]
        src = str(tmp_path / "src.db")
        make_source_db(src, rows)
        stats = tp.compute_model_stats(src, window_days=WINDOW_DAYS, now_ts=NOW)
        assert stats["models"]["model-e"]["n"] == 1

    def test_missing_source_raises(self, tmp_path):
        """Source DB is opened READ-ONLY: a missing file must NOT be created."""
        with pytest.raises(sqlite3.OperationalError):
            tp.compute_model_stats(
                str(tmp_path / "nope.db"), window_days=WINDOW_DAYS, now_ts=NOW
            )
        assert not (tmp_path / "nope.db").exists()

    def test_meta_carries_provenance(self, tmp_path):
        rows = [("model-f", 1000, 200, 0.1)]
        src = str(tmp_path / "src.db")
        make_source_db(src, rows)
        stats = tp.compute_model_stats(src, window_days=7, now_ts=NOW)
        assert stats["meta"]["window_days"] == 7
        assert stats["meta"]["seeded_at"] == pytest.approx(NOW)
        assert stats["meta"]["source"] == src


# ── seed-then-replace (drift re-seed) ────────────────────────────────────────


class TestSeedThenReplace:
    def _seed(self, stats_db, src):
        return tp.seed_token_stats(src, stats_db, window_days=WINDOW_DAYS, now_ts=NOW)

    def test_reseed_replaces_not_merges(self, tmp_path):
        """Drift re-seed: models absent from the new source disappear."""
        src_a, src_b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
        stats_db = str(tmp_path / "stats.db")
        make_source_db(src_a, [("old-x", 10_000, 200, 0.1)] * 50)
        make_source_db(src_b, [("new-y", 20_000, 200, 0.1)] * 50)
        self._seed(stats_db, src_a)
        self._seed(stats_db, src_b)  # drift: the fleet changed shape
        loaded = tp.load_token_stats(stats_db)
        assert "old-x" not in loaded["models"]  # replaced, not merged
        assert loaded["models"]["new-y"]["p50"] == pytest.approx(20_000.0)

    def test_reseed_updates_values(self, tmp_path):
        src_a, src_b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
        stats_db = str(tmp_path / "stats.db")
        make_source_db(src_a, [("m", 10_000, 200, 0.1)] * 50)
        make_source_db(src_b, [("m", 30_000, 200, 0.1)] * 50)
        self._seed(stats_db, src_a)
        self._seed(stats_db, src_b)
        loaded = tp.load_token_stats(stats_db)
        assert loaded["models"]["m"]["p50"] == pytest.approx(30_000.0)
        assert loaded["models"]["m"]["n"] == 50  # n replaced too, not summed

    def test_failed_reseed_preserves_old_stats(self, tmp_path):
        """Empty source → error, and the previous stats survive intact."""
        src_good = str(tmp_path / "good.db")
        src_empty = str(tmp_path / "empty.db")
        stats_db = str(tmp_path / "stats.db")
        make_source_db(src_good, [("m", 10_000, 200, 0.1)] * 50)
        make_source_db(src_empty, [("m", 10_000, 500, 0.1)] * 50)  # no 200s
        self._seed(stats_db, src_good)
        with pytest.raises(ValueError):
            self._seed(stats_db, src_empty)
        loaded = tp.load_token_stats(stats_db)
        assert loaded["models"]["m"]["n"] == 50  # untouched

    def test_seed_writes_meta(self, tmp_path):
        src = str(tmp_path / "src.db")
        stats_db = str(tmp_path / "stats.db")
        make_source_db(src, [("m", 10_000, 200, 0.1)] * 50)
        self._seed(stats_db, src)
        loaded = tp.load_token_stats(stats_db)
        assert loaded["meta"]["seeded_at"] == pytest.approx(NOW)
        assert loaded["meta"]["window_days"] == WINDOW_DAYS

    def test_load_missing_stats_returns_none(self, tmp_path):
        assert tp.load_token_stats(str(tmp_path / "missing.db")) is None


# ── confidence buckets ───────────────────────────────────────────────────────


class TestConfidence:
    @pytest.mark.parametrize(
        "n,expected",
        [
            (0, "low"),
            (1, "low"),
            (29, "low"),   # plan-pinned threshold: n<30 → low
            (30, "medium"),
            (199, "medium"),
            (200, "high"),
            (10_000, "high"),
        ],
    )
    def test_buckets(self, n, expected):
        assert tp.confidence_bucket(n) == expected

    def test_n_below_30_is_low_even_with_model_stats(self, tmp_path):
        """Exact model match but thin history → low confidence, still used."""
        rows = [("thin", 5_000, 200, 0.1)] * 29
        src, stats_db = str(tmp_path / "s.db"), str(tmp_path / "t.db")
        make_source_db(src, rows)
        tp.seed_token_stats(src, stats_db, window_days=WINDOW_DAYS, now_ts=NOW)
        pred = tp.predict_tokens(
            tp.load_token_stats(stats_db), model="thin", now_ts=NOW
        )
        assert pred["confidence"] == "low"
        assert pred["source"] == "model_stats"
        assert pred["raw_p50_tokens"] == pytest.approx(5_000)

    def test_stale_stats_force_low_confidence(self, tmp_path):
        rows = [("m", 5_000, 200, 0.1)] * 500
        src, stats_db = str(tmp_path / "s.db"), str(tmp_path / "t.db")
        make_source_db(src, rows)
        tp.seed_token_stats(src, stats_db, window_days=WINDOW_DAYS, now_ts=NOW)
        pred = tp.predict_tokens(
            tp.load_token_stats(stats_db),
            model="m",
            now_ts=NOW + 8 * 86400,  # stats a week stale
        )
        assert pred["stale"] is True
        assert pred["confidence"] == "low"


# ── cold model — always answers, conservative ────────────────────────────────


class TestColdModel:
    def _seeded(self, tmp_path, extra=None):
        rows = [
            ("known-a", 10_000, 200, 0.1),
            ("known-b", 4_000, 200, 0.1),
        ] * 100
        src, stats_db = str(tmp_path / "s.db"), str(tmp_path / "t.db")
        make_source_db(src, rows + (extra or []))
        tp.seed_token_stats(src, stats_db, window_days=WINDOW_DAYS, now_ts=NOW)
        return tp.load_token_stats(stats_db)

    def test_unknown_model_conservative_default_with_penalty(self, tmp_path):
        stats = self._seeded(tmp_path)
        pred = tp.predict_tokens(stats, model="never-seen", now_ts=NOW)
        # conservative base = WORST observed per-model p90 (10_000), × penalty
        base = max(
            stats["models"][m]["p90"] for m in stats["models"]
        )
        assert pred["source"] == "cold_default"
        assert pred["cold_model"] is True
        assert pred["confidence"] == "low"
        assert pred["predicted_p90_tokens"] == pytest.approx(
            base * tp.COLD_MODEL_PENALTY
        )

    def test_cold_model_with_empty_stats_still_answers(self):
        """Stats DB missing entirely → absolute fallback constant, no raise."""
        pred = tp.predict_tokens(None, model="anything", now_ts=NOW)
        assert pred["cold_model"] is True
        assert pred["source"] == "cold_default"
        assert pred["predicted_p90_tokens"] >= tp.DEFAULT_COLD_TOKENS
        assert pred["confidence"] == "low"

    def test_predict_never_returns_none_tokens(self):
        """Felix's rule: the predictor ALWAYS returns a number + flag."""
        for stats in (None, {"models": {}, "meta": {}}):
            pred = tp.predict_tokens(stats, model="x", now_ts=NOW)
            assert isinstance(pred["predicted_p50_tokens"], int)
            assert isinstance(pred["predicted_p90_tokens"], int)
            assert pred["predicted_p90_tokens"] >= pred["predicted_p50_tokens"] > 0


# ── task dimension via TASK_PROFILES ─────────────────────────────────────────


class TestTaskDimension:
    def _seeded(self, tmp_path):
        rows = [("the-model", 10_000, 200, 0.1)] * 100
        src, stats_db = str(tmp_path / "s.db"), str(tmp_path / "t.db")
        make_source_db(src, rows)
        tp.seed_token_stats(src, stats_db, window_days=WINDOW_DAYS, now_ts=NOW)
        return tp.load_token_stats(stats_db)

    def test_budget_mult_scaling(self, tmp_path):
        stats = self._seeded(tmp_path)
        p50 = stats["models"]["the-model"]["p50"]
        p90 = stats["models"]["the-model"]["p90"]
        for task_type, mult in [
            ("coding", 1.0),
            ("research", 2.5),
            ("review", 0.5),
            ("mechanical", 0.25),
        ]:
            pred = tp.predict_tokens(
                stats, model="the-model", task_type=task_type, now_ts=NOW
            )
            assert pred["budget_mult"] == pytest.approx(mult)
            assert pred["raw_p50_tokens"] == pytest.approx(p50)  # raw unscaled
            assert pred["predicted_p50_tokens"] == pytest.approx(p50 * mult)
            assert pred["predicted_p90_tokens"] == pytest.approx(p90 * mult)

    def test_unknown_task_type_normalizes_to_coding(self, tmp_path):
        stats = self._seeded(tmp_path)
        pred = tp.predict_tokens(
            stats, model="the-model", task_type="nonsense", now_ts=NOW
        )
        assert pred["task_type"] == "coding"

    def test_model_defaults_from_profile(self, tmp_path):
        """model=None → TASK_PROFILES model for the task type."""
        stats = self._seeded(tmp_path)
        pred = tp.predict_tokens(stats, task_type="coding", now_ts=NOW)
        assert pred["model"] == resolve_task_profile("coding")["model"]

    def test_uses_dispatch_gate_task_profiles_by_identity(self):
        """Composition, no duplication: same object as dispatch_gate."""
        assert tp.TASK_PROFILES is TASK_PROFILES


# ── NO Kalman inputs until convergence green ─────────────────────────────────


class TestKalmanGate:
    def test_flag_disabled(self):
        """kalman_health.py --short was ✗ unhealthy on 2026-08-22 (verified,
        not assumed) — Kalman inputs must be disabled until it goes green."""
        assert tp.KALMAN_INPUTS_ENABLED is False

    def test_module_imports_nothing_kalman(self):
        """Source-level pin: no kalman/burn_predictor imports sneak in."""
        src_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "token_predictor.py"
        )
        with open(src_path) as fh:
            imports = re.findall(r"^\s*(?:import|from)\s+(\S+)", fh.read(), re.M)
        for mod in imports:
            root = mod.split(".")[0].strip("\"'")
            assert root not in {
                "burn_predictor",
                "kalman_health",
                "consumption_kalman",
                "demand_kalman",
                "price_kalman",
            }, f"kalman-family import found: {mod}"

    def test_prediction_carries_kalman_provenance(self):
        pred = tp.predict_tokens(None, model="m", now_ts=NOW)
        assert pred["kalman_inputs"] is False


# ── integration with the CG-1 cost gate ──────────────────────────────────────


class TestCostGateIntegration:
    def test_gate_feed_applies_budget_mult_exactly_once(self, tmp_path):
        """gate_estimated_tokens() returns RAW p90 — the gate itself applies
        budget_mult, so scaling must not happen twice."""
        rows = [("the-model", 10_000, 200, 0.1)] * 100
        src, stats_db = str(tmp_path / "s.db"), str(tmp_path / "t.db")
        make_source_db(src, rows)
        tp.seed_token_stats(src, stats_db, window_days=WINDOW_DAYS, now_ts=NOW)
        stats = tp.load_token_stats(stats_db)

        feed = tp.gate_estimated_tokens(stats, model="the-model", task_type="research")
        assert feed == pytest.approx(10_000)  # raw p90, NOT ×2.5

        verdict = evaluate_cost_gate(
            model="the-model",
            task_type="research",
            deferrable=False,
            effective_price_usd_per_m=0.01,
            price_history=[0.01] * 50,
            budget_cap_usd=15.0,
            estimated_tokens=feed,
            now_ts=NOW,
        )
        # research mult = 2.5 → 0.01 $/M × 10_000 × 2.5 / 1M = 0.00025 exactly
        assert verdict["predicted_cost_usd"] == pytest.approx(0.00025)

    def test_prediction_output_is_gate_compatible(self):
        """predict_tokens output can feed evaluate_cost_gate directly."""
        pred = tp.predict_tokens(None, model="glm-5.2", task_type="coding", now_ts=NOW)
        verdict = evaluate_cost_gate(
            model=pred["model"],
            task_type=pred["task_type"],
            deferrable=False,
            effective_price_usd_per_m=0.01,
            price_history=[0.01] * 50,
            budget_cap_usd=15.0,
            estimated_tokens=pred["predicted_p90_tokens"],
            now_ts=NOW,
        )
        assert verdict["decision"] in {"ALLOW", "DEFER", "DENY"}
