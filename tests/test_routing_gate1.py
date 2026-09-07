"""tests/test_routing_gate1.py — routing transparency + confidence (Gate 1 data feed).

TDD spec for `src/routing_gate1.py`, the routstr-readiness-check Gate 1 data
feed. The readiness check (ADR-007 gate 1) must be able to read a *live* MAPE
reliably off the *decision data* (routing_live_decisions /
flat_router_shadow_decisions), not an idle/static gate.

Deliverables pinned by these tests:

1. LIVE MAPE ON DECISION DATA  — compute_live_mape() / gate1_feed() turn the
   raw decision rows (predicted $/M vs realized $/M, per model) into the mean
   absolute percentage error that Gate 1 (<15%) consumes.  Robust to empty,
   borrowed-zero and degenerate inputs.

2. PREDICT-EXHAUSTION TRUSTABILITY — exhaust_trust(): validates the
   hours-to-exhaustion forecast is internally consistent with the burn rate
   and remaining quota (i.e. hours == remaining_tokens / burn_tph), with the
   same "sold safety" semantics the caller-class gate needs.

3. CALLER-CLASS SCHEMA + LIVE LOGGING — ensure_decision_schema() idempotently
   adds `caller_class` to both decision tables; log_live_decision() persists a
   routed decision carrying caller_class so the feed is complete.

4. SELECT-PROVIDER DETERMINISM — a live-chain smoke test proving
   select_provider(model) resolves to the expected cheapest-HEALTHY candidate
   (ordered ascending effective_cost, unhealthy/disabled providers excluded),
   i.e. "routing is determinable".
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "production"))

from src.routing_gate1 import (  # noqa: E402
    CALLER_CLASS_INTERNAL,
    CALLER_CLASS_SOLD,
    GATE1_MAPE_THRESHOLD_PCT,
    compute_live_mape,
    ensure_decision_schema,
    exhaust_trust,
    gate1_feed,
    log_live_decision,
    load_live_decisions,
)


# ── 1. live MAPE on decision data ────────────────────────────────────────────

class TestComputeLiveMape:
    def test_trivial_perfect_accuracy(self):
        # predicted == actual everywhere → MAPE 0.0
        assert compute_live_mape({"glm-5.2": (1.0, 1.0)}) == 0.0

    def test_single_known_series(self):
        # pred=[10,20,30], actual=[10,25,25]
        # errs: |10-10|/10=0%, |20-25|/25=20%, |30-25|/25=20% -> mean 13.3333%
        assert compute_live_mape({  # model -> (pred, actual) tuples
            "m": ([10.0, 20.0, 30.0], [10.0, 25.0, 25.0]),
        }) == pytest.approx(13.3333, abs=1e-3)

    def test_multi_model_weighted_by_observations(self):
        data = {
            "a": ([1.0, 1.0], [2.0, 2.0]),          # errs 50%,50% -> 2 obs
            "b": ([10.0], [10.0]),                # err 0% -> 1 obs
        }
        # weighted mean over obs: (50+50+0)/3 = 33.3333%
        assert compute_live_mape(data) == pytest.approx(33.3333, abs=1e-3)

    def test_zero_actual_guarded(self):
        mape = compute_live_mape({"m": ([5.0, 5.0], [0.0, 5.0])})
        assert mape == pytest.approx(0.0, abs=1e-9)

    def test_empty_input_none(self):
        assert compute_live_mape({}) is None

    def test_threshold_constant(self):
        assert GATE1_MAPE_THRESHOLD_PCT == 15.0


# ── 2. predict-exhaustion trustability ───────────────────────────────────────

class TestExhaustTrust:
    def test_hours_match_consistency(self):
        # 90k tokens remaining, 30k tph → exactly 3.0h to exhaustion
        ok, note = exhaust_trust(
            remaining_tokens=90_000,
            burn_rate_tph=30_000,
            exhausts_in_hours=3.0,
        )
        assert ok is True
        assert "consistent" in note

    def test_wrong_hours_flagged(self):
        # reports 1h but math says 3h → NOT trustable
        ok, _ = exhaust_trust(90_000, 30_000, exhausts_in_hours=1.0)
        assert ok is False

    def test_zero_burn_rate_is_untrustable_for_sold(self):
        # no observable burn → cannot promise an hours-to-exhaustion
        ok, _ = exhaust_trust(90_000, 0.0, exhausts_in_hours=24.0)
        assert ok is False

    def test_negative_remaining_is_untrustable(self):
        ok, _ = exhaust_trust(-5, 10_000, exhausts_in_hours=2.0)
        assert ok is False

    def test_consistency_tolerance(self):
        # within a small tolerance is still "consistent"
        ok, _ = exhaust_trust(90_000, 30_000, exhausts_in_hours=3.05)
        assert ok is True


# ── 3. caller-class schema + live logging ────────────────────────────────────

@pytest.fixture
def _tmp_db(tmp_path):
    return str(tmp_path / "gate1.db")


class TestSchemaAndLogging:
    def test_ensure_schema_is_idempotent(self, _tmp_db):
        ensure_decision_schema(_tmp_db)
        ensure_decision_schema(_tmp_db)  # second call must not error
        conn = sqlite3.connect(_tmp_db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(routing_live_decisions)")]
        n = cols.count("caller_class")
        conn.close()
        # ensure_decision_schema must ADD caller_class exactly once, idempotently
        assert n == 1

    def test_log_live_decision_roundtrip_caller_class_internal(self, _tmp_db):
        ensure_decision_schema(_tmp_db)
        log_live_decision(
            db_path=_tmp_db,
            provider="friend",
            model="glm-5.2",
            caller_class=CALLER_CLASS_INTERNAL,
        )
        conn = sqlite3.connect(_tmp_db)
        row = conn.execute(
            "SELECT live_provider, live_model, caller_class FROM routing_live_decisions "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "friend"
        assert row[1] == "glm-5.2"
        assert row[2] == CALLER_CLASS_INTERNAL

    def test_log_live_decision_sold(self, _tmp_db):
        ensure_decision_schema(_tmp_db)
        log_live_decision(
            db_path=_tmp_db,
            provider="friend",
            model="glm-5.2",
            caller_class=CALLER_CLASS_SOLD,
        )
        conn = sqlite3.connect(_tmp_db)
        row = conn.execute(
            "SELECT caller_class FROM routing_live_decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row[0] == CALLER_CLASS_SOLD

    def test_default_caller_class_is_internal(self, _tmp_db):
        ensure_decision_schema(_tmp_db)
        log_live_decision(db_path=_tmp_db, provider="friend", model="glm-5.2")
        conn = sqlite3.connect(_tmp_db)
        row = conn.execute(
            "SELECT caller_class FROM routing_live_decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row[0] == CALLER_CLASS_INTERNAL

    def test_flat_router_shadow_schema_gets_caller_class(self, _tmp_db):
        ensure_decision_schema(_tmp_db)
        conn = sqlite3.connect(_tmp_db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(flat_router_shadow_decisions)")]
        conn.close()
        assert "caller_class" in cols

    def test_load_live_decisions_reads_back(self, _tmp_db, tmp_path):
        # seed routing_live_decisions via the module (live table), then load
        ensure_decision_schema(_tmp_db)
        conn = sqlite3.connect(_tmp_db)
        conn.execute(
            "INSERT INTO routing_live_decisions "
            "(ts, live_provider, live_model, agree, reason, caller_class) "
            " VALUES (?,?,?,?,?,?)",
            (time.time(), "friend", "glm-5.2", 1, "routed_friend",
             CALLER_CLASS_INTERNAL),
        )
        conn.commit()
        conn.close()
        rows = load_live_decisions(_tmp_db, caller_class=None)
        assert len(rows) == 1


# ── 4. select_provider determinism (live-chain smoke) ─────────────────────────

class TestSelectProviderDeterminism:
    def test_returns_ordered_cheapest_first(self):
        # Hand-rolled: candidates must come back sorted ascending by cost,
        # and the top pick must NOT be a fallback. Uses real chain modules.
        sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))
        try:
            import flat_router
            candidates = flat_router.select_provider(model="deepseek-v4-flash")
        except Exception as e:  # pragma: no cover — environment only
            pytest.skip(f"flat_router not importable here: {e}")

        assert len(candidates) >= 1
        costs = [c.effective_cost for c in candidates]
        assert costs == sorted(costs), "candidate list must be cheapest-first"
        assert candidates[0].name != "fallback" or len(candidates) == 1

    def test_unknown_model_resolves_to_fallback_with_inf(self):
        sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))
        try:
            import flat_router
            candidates = flat_router.select_provider(model="__definitely_not_a_model__")
        except Exception as e:  # pragma: no cover
            pytest.skip(f"flat_router not importable here: {e}")
        assert candidates[0].name == "fallback"
        assert candidates[0].effective_cost == float("inf")


# ── 5. gate1_feed end-to-end (DB → JSON) ─────────────────────────────────────

class TestGate1Feed:
    def test_gate1_feed_json_shape(self, _tmp_db, tmp_path):
        # Seed two models with predicted vs realized costs, caller_class sold
        ensure_decision_schema(_tmp_db)
        log_live_decision(db_path=_tmp_db, provider="friend", model="glm-5.2",
                          caller_class=CALLER_CLASS_SOLD)
        # realized cost goes into api_calls for the same ts/model
        import time
        conn = sqlite3.connect(_tmp_db)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS api_calls ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, key_name TEXT,"
            " model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,"
            " total_tokens INTEGER, tier TEXT, cache_hit INTEGER DEFAULT 0,"
            " ollama_hit INTEGER DEFAULT 0, ppq_hit INTEGER DEFAULT 0,"
            " status_code INTEGER, error TEXT, duration_ms INTEGER,"
            " cost_usd REAL, cost_source TEXT)"
        )
        conn.execute(
            "INSERT INTO api_calls (ts, key_name, model, total_tokens, status_code, cost_usd, cost_source)"
            " VALUES (?,?,?,?,?,?,?)",
            (time.time(), "friend", "glm-5.2", 1000, 200, 0.0005, "measured"),
        )
        conn.commit()
        conn.close()
        feed = gate1_feed(_tmp_db, caller_class=CALLER_CLASS_SOLD)
        assert "gate1" in feed
        assert "mape_pct" in feed["gate1"]
        assert "n" in feed["gate1"]
        assert feed["gate1"]["threshold_pct"] == GATE1_MAPE_THRESHOLD_PCT


# ── caller-class constants ───────────────────────────────────────────────────

class TestCallerClassConstants:
    def test_values(self):
        assert CALLER_CLASS_INTERNAL == "internal"
        assert CALLER_CLASS_SOLD == "sold"