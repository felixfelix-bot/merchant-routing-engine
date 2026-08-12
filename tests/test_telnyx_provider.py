"""Tests for the Telnyx provider integration (TELNYX-6.1).

Covers the five areas required by the task spec:

1) Provider name normalization — ``normalize_provider_name('telnyx') == 'telnyx'``
2) Model mapping — ``get_model('telnyx', 'coding') == 'kimi-k3'``
3) Pricing engine integration — ``quota_pressure_factor`` with Telnyx params
4) Model name translation — ``_PROVIDER_MODEL_NAMES['telnyx']['kimi-k3']
   == 'moonshotai/Kimi-K3'`` (lives in the production proxy)
5) Balance collector logic — ``collect_telnyx_balance``, ``telnyx_quota_entry``,
   ``store_telnyx_balance`` / ``get_latest_telnyx_balance`` round-trip, CLI dispatch
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time

import pytest

# Ensure the repo root is on sys.path so ``src.*`` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.provider_names import CANONICAL_PROVIDERS, normalize_provider_name
from src.model_mapping import (
    DEFAULT_MODEL,
    MODEL_MAP,
    TASK_TYPES,
    get_model,
    get_models_for_provider,
    normalize_service,
)
from src.pricing_engine import (
    TELNYX_CREDIT_PRESSURE_ONSET,
    TELNYX_CREDIT_PRESSURE_ASYMPTOTE,
    TELNYX_STARTING_BALANCE,
    _single_window_factor,
    quota_pressure_factor,
)
from src.balance_collectors import (
    TELNYX_DEFAULT_STARTING_BALANCE,
    TELNYX_STARTING_ENV,
    TelnyxBalance,
    _telnyx_usage_fraction,
    collect_and_store_telnyx,
    collect_telnyx_balance,
    default_db_path,
    get_latest_telnyx_balance,
    main,
    store_telnyx_balance,
    telnyx_quota_entry,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_db(tmp_path, rows=None):
    """Create a tmp SQLite DB with an ``api_calls`` table and optional rows.

    Each row is ``(ts, key_name, cost_usd)``.
    """
    db = str(tmp_path / "burn.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            model TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_usd REAL,
            cost_source TEXT
        )"""
    )
    for row in rows or []:
        conn.execute(
            "INSERT INTO api_calls (ts, key_name, cost_usd) VALUES (?, ?, ?)",
            row,
        )
    conn.commit()
    conn.close()
    return db


def _load_proxy_provider_model_names():
    """Load ``_PROVIDER_MODEL_NAMES`` from the production proxy source.

    The proxy (``~/.hermes/bot/zai_proxy.py``) is the source of truth for
    per-provider model name translation.  Importing it directly has heavy
    side-effects (starts servers, reads ``.env``), so we parse the dict
    from source via a controlled ``exec`` of just the assignment block.
    """
    proxy_path = os.path.expanduser("~/.hermes/bot/zai_proxy.py")
    if not os.path.isfile(proxy_path):
        pytest.skip(f"proxy source not found at {proxy_path}")
    src = open(proxy_path, encoding="utf-8", errors="replace").read()
    marker = "_PROVIDER_MODEL_NAMES"
    idx = src.find(marker)
    if idx < 0:
        pytest.skip("_PROVIDER_MODEL_NAMES not found in proxy source")
    # Extract from the assignment to the closing brace of the outer dict.
    # The dict is a top-level assignment: _PROVIDER_MODEL_NAMES = { ... }
    brace_start = src.find("{", idx)
    if brace_start < 0:
        pytest.skip("could not find opening brace for _PROVIDER_MODEL_NAMES")
    depth = 0
    end = -1
    for i in range(brace_start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        pytest.skip("could not find closing brace for _PROVIDER_MODEL_NAMES")
    snippet = src[idx:end]
    namespace: dict = {}
    exec(snippet, namespace)  # noqa: S102 — controlled parse
    return namespace["_PROVIDER_MODEL_NAMES"]


# ════════════════════════════════════════════════════════════════════════════
# 1 — Provider name normalization
# ════════════════════════════════════════════════════════════════════════════

class TestProviderNameNormalization:
    """Telnyx must be a recognised canonical provider and normalise to itself."""

    def test_telnyx_in_canonical_providers(self):
        assert "telnyx" in CANONICAL_PROVIDERS

    def test_telnyx_normalizes_to_telnyx(self):
        assert normalize_provider_name("telnyx") == "telnyx"

    def test_telnyx_normalization_idempotent(self):
        once = normalize_provider_name("telnyx")
        twice = normalize_provider_name(once)
        assert once == twice == "telnyx"

    def test_telnyx_not_aliased_to_ours(self):
        """Telnyx is an external provider — must not collapse to 'ours'."""
        assert normalize_provider_name("telnyx") != "ours"

    def test_telnyx_uppercase_passes_through(self):
        """Unrecognised variants pass through unchanged (not in _PROVIDER_MAPPINGS)."""
        assert normalize_provider_name("Telnyx") == "Telnyx"

    def test_telnyx_normalize_service(self):
        """normalize_service passes telnyx through (not a z.ai alias)."""
        assert normalize_service("telnyx") == "telnyx"


# ════════════════════════════════════════════════════════════════════════════
# 2 — Model mapping
# ════════════════════════════════════════════════════════════════════════════

class TestModelMapping:
    """Telnyx (provider, task_type) → model_name lookups."""

    def test_telnyx_coding_is_kimi_k3(self):
        assert get_model("telnyx", "coding") == "kimi-k3"

    def test_telnyx_reasoning_is_kimi_k3(self):
        assert get_model("telnyx", "reasoning") == "kimi-k3"

    def test_telnyx_chat_is_kimi_k2_5(self):
        assert get_model("telnyx", "chat") == "kimi-k2.5"

    def test_telnyx_simple_is_kimi_k2_5(self):
        assert get_model("telnyx", "simple") == "kimi-k2.5"

    def test_telnyx_default_task_type_is_coding(self):
        """Unknown / None task_type falls back to 'coding' → kimi-k3."""
        assert get_model("telnyx", None) == "kimi-k3"
        assert get_model("telnyx", "unknown_task") == "kimi-k3"

    def test_telnyx_in_model_map_table(self):
        """All four telnyx entries exist in the hardcoded MODEL_MAP."""
        assert MODEL_MAP[("telnyx", "coding")] == "kimi-k3"
        assert MODEL_MAP[("telnyx", "reasoning")] == "kimi-k3"
        assert MODEL_MAP[("telnyx", "chat")] == "kimi-k2.5"
        assert MODEL_MAP[("telnyx", "simple")] == "kimi-k2.5"

    def test_telnyx_get_models_for_provider(self):
        """get_models_for_provider returns all 4 task types for telnyx."""
        models = get_models_for_provider("telnyx", model_map=MODEL_MAP)
        assert models == {
            "coding": "kimi-k3",
            "reasoning": "kimi-k3",
            "chat": "kimi-k2.5",
            "simple": "kimi-k2.5",
        }

    def test_telnyx_provider_default_is_kimi_k3(self):
        """Per-provider static default for telnyx is kimi-k3."""
        from src.model_mapping import _PROVIDER_DEFAULTS
        assert _PROVIDER_DEFAULTS.get("telnyx") == "kimi-k3"

    def test_telnyx_models_are_valid_task_types(self):
        """Every telnyx entry uses a recognised task type."""
        for (prov, task_type), _model in MODEL_MAP.items():
            if prov == "telnyx":
                assert task_type in TASK_TYPES, f"unknown task type {task_type!r}"

    def test_telnyx_not_aliased_to_zai(self):
        """normalize_service('telnyx') must NOT return 'zai' — it's external."""
        assert normalize_service("telnyx") != "zai"

    def test_telnyx_unknown_provider_fallback(self):
        """get_model with an unknown provider doesn't return telnyx models."""
        result = get_model("nonexistent_provider", "coding")
        assert result == DEFAULT_MODEL

    def test_telnyx_all_model_map_entries_resolve(self):
        """Every telnyx entry in MODEL_MAP resolves via get_model when injected."""
        for (prov, task_type), model in MODEL_MAP.items():
            if prov == "telnyx":
                assert get_model(prov, task_type, model_map=MODEL_MAP) == model


# ════════════════════════════════════════════════════════════════════════════
# 3 — Pricing engine integration (quota_pressure_factor with Telnyx params)
# ════════════════════════════════════════════════════════════════════════════

class TestPricingEngineIntegration:
    """quota_pressure_factor with Telnyx credit-pressure parameters.

    Telnyx is credit-based (self-tracked): onset=0.80, asymptote=1.5,
    hard_limit=True (no extra-usage path — when credits run out, it's
    unreachable).
    """

    def test_telnyx_pressure_constants(self):
        assert TELNYX_CREDIT_PRESSURE_ONSET == pytest.approx(0.80)
        assert TELNYX_CREDIT_PRESSURE_ASYMPTOTE == pytest.approx(1.5)
        assert TELNYX_STARTING_BALANCE == pytest.approx(10.0)

    def test_telnyx_onset_matches_credit_provider_pattern(self):
        """Onset must match other credit-based providers (0.80)."""
        from src.pricing_engine import (
            DEEPINFRA_CREDIT_PRESSURE_ONSET,
            OPENROUTER_CREDIT_PRESSURE_ONSET,
            PPQ_QUOTA_PRESSURE_ONSET,
        )
        assert TELNYX_CREDIT_PRESSURE_ONSET == pytest.approx(
            OPENROUTER_CREDIT_PRESSURE_ONSET
        )
        assert TELNYX_CREDIT_PRESSURE_ONSET == pytest.approx(
            DEEPINFRA_CREDIT_PRESSURE_ONSET
        )
        assert TELNYX_CREDIT_PRESSURE_ONSET == pytest.approx(PPQ_QUOTA_PRESSURE_ONSET)

    def test_telnyx_asymptote_matches_uniform_1_5(self):
        """Asymptote must match the uniform 1.5 across all credit providers."""
        from src.pricing_engine import (
            DEEPINFRA_CREDIT_PRESSURE_ASYMPTOTE,
            OPENROUTER_CREDIT_PRESSURE_ASYMPTOTE,
            PPQ_QUOTA_PRESSURE_ASYMPTOTE,
        )
        assert TELNYX_CREDIT_PRESSURE_ASYMPTOTE == pytest.approx(1.5)
        assert TELNYX_CREDIT_PRESSURE_ASYMPTOTE == pytest.approx(
            OPENROUTER_CREDIT_PRESSURE_ASYMPTOTE
        )
        assert TELNYX_CREDIT_PRESSURE_ASYMPTOTE == pytest.approx(
            DEEPINFRA_CREDIT_PRESSURE_ASYMPTOTE
        )

    def test_below_onset_no_pressure(self):
        """u=0.5 (below onset 0.80) → factor = 1.0 (no penalty)."""
        factor = quota_pressure_factor(
            0.5,
            onset=TELNYX_CREDIT_PRESSURE_ONSET,
            asymptote=TELNYX_CREDIT_PRESSURE_ASYMPTOTE,
            hard_limit=True,
        )
        assert factor == pytest.approx(1.0)

    def test_at_onset_is_one(self):
        """u=0.80 (exactly at onset) → factor = 1.0 (pressure just begins)."""
        factor = quota_pressure_factor(
            TELNYX_CREDIT_PRESSURE_ONSET,
            onset=TELNYX_CREDIT_PRESSURE_ONSET,
            asymptote=TELNYX_CREDIT_PRESSURE_ASYMPTOTE,
            hard_limit=True,
        )
        assert factor == pytest.approx(1.0)

    def test_above_onset_pressure_active(self):
        """u=0.9 (above onset) → factor > 1.0 (pressure active)."""
        factor = quota_pressure_factor(
            0.9,
            onset=TELNYX_CREDIT_PRESSURE_ONSET,
            asymptote=TELNYX_CREDIT_PRESSURE_ASYMPTOTE,
            hard_limit=True,
        )
        assert factor > 1.0
        assert math.isfinite(factor)

    def test_gate_0_9_matches_single_window_factor(self):
        """GATE: factor at u=0.9 matches _single_window_factor(0.9, 0.80, 1.5)."""
        u = 0.9
        factor = quota_pressure_factor(
            u,
            onset=TELNYX_CREDIT_PRESSURE_ONSET,
            asymptote=TELNYX_CREDIT_PRESSURE_ASYMPTOTE,
            hard_limit=True,
        )
        expected = _single_window_factor(
            u, TELNYX_CREDIT_PRESSURE_ONSET, TELNYX_CREDIT_PRESSURE_ASYMPTOTE
        )
        assert factor == pytest.approx(expected, rel=1e-9)

    def test_exhausted_is_inf(self):
        """u=1.0 (balance exhausted) → hard_limit=True → +inf."""
        factor = quota_pressure_factor(
            1.0,
            onset=TELNYX_CREDIT_PRESSURE_ONSET,
            asymptote=TELNYX_CREDIT_PRESSURE_ASYMPTOTE,
            hard_limit=True,
        )
        assert factor == math.inf

    def test_overrun_is_inf(self):
        """u=1.5 (overrun) → hard_limit=True → +inf."""
        factor = quota_pressure_factor(
            1.5,
            onset=TELNYX_CREDIT_PRESSURE_ONSET,
            asymptote=TELNYX_CREDIT_PRESSURE_ASYMPTOTE,
            hard_limit=True,
        )
        assert factor == math.inf

    def test_monotonic_from_onset_to_full(self):
        """Factor must be strictly monotonically increasing from onset to 1.0."""
        usages = [0.80, 0.85, 0.90, 0.95, 0.99]
        factors = [
            quota_pressure_factor(
                u,
                onset=TELNYX_CREDIT_PRESSURE_ONSET,
                asymptote=TELNYX_CREDIT_PRESSURE_ASYMPTOTE,
                hard_limit=True,
            )
            for u in usages
        ]
        for i in range(1, len(factors)):
            assert factors[i] > factors[i - 1], (
                f"not monotonic: u={usages[i]} factor={factors[i]} "
                f"<= u={usages[i-1]} factor={factors[i-1]}"
            )

    def test_hard_limit_true_vs_false_at_exhaustion(self):
        """With hard_limit=False, exhaustion caps at asymptote; with True → inf."""
        # hard_limit=False (Ollama-style)
        factor_soft = quota_pressure_factor(
            1.0,
            onset=TELNYX_CREDIT_PRESSURE_ONSET,
            asymptote=TELNYX_CREDIT_PRESSURE_ASYMPTOTE,
            hard_limit=False,
        )
        assert factor_soft == pytest.approx(TELNYX_CREDIT_PRESSURE_ASYMPTOTE)
        # hard_limit=True (Telnyx-style)
        factor_hard = quota_pressure_factor(
            1.0,
            onset=TELNYX_CREDIT_PRESSURE_ONSET,
            asymptote=TELNYX_CREDIT_PRESSURE_ASYMPTOTE,
            hard_limit=True,
        )
        assert factor_hard == math.inf


# ════════════════════════════════════════════════════════════════════════════
# 4 — Model name translation (_PROVIDER_MODEL_NAMES in the production proxy)
# ════════════════════════════════════════════════════════════════════════════

class TestModelNameTranslation:
    """Per-provider model name translation in the production proxy.

    The proxy maps short canonical model IDs to provider-specific model names
    (e.g. ``kimi-k3`` → ``moonshotai/Kimi-K3`` on Telnyx).
    """

    @pytest.fixture
    def provider_model_names(self):
        return _load_proxy_provider_model_names()

    def test_telnyx_section_exists(self, provider_model_names):
        assert "telnyx" in provider_model_names

    def test_kimi_k3_maps_to_moonshotai(self, provider_model_names):
        assert provider_model_names["telnyx"]["kimi-k3"] == "moonshotai/Kimi-K3"

    def test_kimi_k2_5_maps_to_moonshotai(self, provider_model_names):
        assert provider_model_names["telnyx"]["kimi-k2.5"] == "moonshotai/Kimi-K2.5"

    def test_glm_5_2_maps_to_zai_org(self, provider_model_names):
        assert provider_model_names["telnyx"]["glm-5.2"] == "zai-org/GLM-5.2"

    def test_minimax_m3_maps_to_minimaxai(self, provider_model_names):
        assert provider_model_names["telnyx"]["minimax-m3"] == "MiniMaxAI/MiniMax-M3-MXFP8"

    def test_kimi_k3_cloud_alias(self, provider_model_names):
        """kimi-k3:cloud should also map to moonshotai/Kimi-K3."""
        assert provider_model_names["telnyx"]["kimi-k3:cloud"] == "moonshotai/Kimi-K3"

    def test_kimi_k2_7_code_alias(self, provider_model_names):
        """kimi-k2.7-code should map to a Kimi model on Telnyx."""
        val = provider_model_names["telnyx"]["kimi-k2.7-code"]
        assert val.startswith("moonshotai/")

    def test_all_telnyx_values_are_provider_specific(self, provider_model_names):
        """Every Telnyx model name must contain a '/' (provider/Model format)."""
        for short, full in provider_model_names["telnyx"].items():
            assert "/" in full, f"model {short!r} → {full!r} missing '/'"

    def test_telnyx_model_count(self, provider_model_names):
        """At least 4 canonical model mappings for telnyx."""
        assert len(provider_model_names["telnyx"]) >= 4

    def test_provider_priority_telnyx_is_last_resort(self):
        """Telnyx should be priority 3 (last resort) in the failover chain."""
        proxy_path = os.path.expanduser("~/.hermes/bot/zai_proxy.py")
        if not os.path.isfile(proxy_path):
            pytest.skip("proxy source not found")
        src = open(proxy_path, encoding="utf-8", errors="replace").read()
        # Parse _PROVIDER_PRIORITY dict from source
        marker = "_PROVIDER_PRIORITY"
        idx = src.find(marker)
        if idx < 0:
            pytest.skip("_PROVIDER_PRIORITY not found in proxy source")
        brace_start = src.find("{", idx)
        depth = 0
        end = -1
        for i in range(brace_start, len(src)):
            ch = src[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        snippet = src[idx:end]
        ns: dict = {}
        exec(snippet, ns)  # noqa: S102
        priority = ns["_PROVIDER_PRIORITY"]
        assert priority.get("telnyx") == 3
        # Telnyx should be the highest priority number (last resort)
        assert priority["telnyx"] == max(priority.values())


# ════════════════════════════════════════════════════════════════════════════
# 5 — Balance collector logic
# ════════════════════════════════════════════════════════════════════════════

class TestUsageFraction:
    """_telnyx_usage_fraction edge cases."""

    def test_fresh_no_spend(self):
        assert _telnyx_usage_fraction(10.0, 10.0) == pytest.approx(0.0)

    def test_half_spent(self):
        assert _telnyx_usage_fraction(5.0, 10.0) == pytest.approx(0.5)

    def test_exhausted_zero(self):
        assert _telnyx_usage_fraction(0.0, 10.0) == 1.0

    def test_overrun_negative(self):
        assert _telnyx_usage_fraction(-2.0, 10.0) == 1.0

    def test_starting_zero_misconfig(self):
        assert _telnyx_usage_fraction(5.0, 0.0) == 0.0

    def test_starting_negative_misconfig(self):
        assert _telnyx_usage_fraction(5.0, -1.0) == 0.0

    def test_remaining_none(self):
        assert _telnyx_usage_fraction(None, 10.0) == 0.0

    def test_clamp_above_one(self):
        """remaining > starting → negative usage → clamped to 0.0."""
        assert _telnyx_usage_fraction(15.0, 10.0) == pytest.approx(0.0)


class TestCollectTelnyxBalance:
    """collect_telnyx_balance with a real tmp SQLite DB."""

    def test_happy_path_with_spend(self, tmp_path):
        db = _make_db(tmp_path, [
            (time.time(), "telnyx", 3.0),
            (time.time(), "telnyx", 2.0),
        ])
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        assert bal.ok
        assert bal.error is None
        assert bal.total_spent_usd == pytest.approx(5.0)
        assert bal.remaining_usd == pytest.approx(5.0)
        assert bal.usage_fraction == pytest.approx(0.5)
        assert bal.is_exhausted is False
        assert bal.starting == 10.0

    def test_empty_db_no_api_calls_table(self, tmp_path):
        """Fresh DB with no table → collect returns error, never raises."""
        db = str(tmp_path / "fresh.db")
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        # _query_telnyx_spent creates the table if missing → SUM returns 0
        assert bal.ok
        assert bal.total_spent_usd == pytest.approx(0.0)
        assert bal.usage_fraction == pytest.approx(0.0)
        assert bal.is_exhausted is False

    def test_non_telnyx_rows_excluded(self, tmp_path):
        db = _make_db(tmp_path, [
            (time.time(), "telnyx", 3.0),
            (time.time(), "ppq", 100.0),
            (time.time(), "openrouter", 50.0),
        ])
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        assert bal.ok
        assert bal.total_spent_usd == pytest.approx(3.0)
        assert bal.remaining_usd == pytest.approx(7.0)

    def test_starting_balance_from_explicit_arg(self, tmp_path):
        db = _make_db(tmp_path, [(time.time(), "telnyx", 4.0)])
        bal = collect_telnyx_balance(starting=20.0, db_path=db)
        assert bal.starting == 20.0
        assert bal.remaining_usd == pytest.approx(16.0)
        assert bal.usage_fraction == pytest.approx(0.2)

    def test_starting_balance_from_env(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path, [(time.time(), "telnyx", 4.0)])
        monkeypatch.setenv(TELNYX_STARTING_ENV, "50.0")
        bal = collect_telnyx_balance(db_path=db)
        assert bal.starting == pytest.approx(50.0)
        assert bal.remaining_usd == pytest.approx(46.0)

    def test_default_starting_balance_constant(self):
        assert TELNYX_DEFAULT_STARTING_BALANCE == pytest.approx(10.0)

    def test_never_raises_on_bad_db_path(self):
        bal = collect_telnyx_balance(starting=10.0, db_path="/nonexistent/path/db.db")
        # Should not raise — returns error in the balance object
        assert isinstance(bal, TelnyxBalance)
        assert bal.error is not None or bal.total_spent_usd is not None

    def test_exhausted_balance(self, tmp_path):
        db = _make_db(tmp_path, [(time.time(), "telnyx", 10.0)])
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        assert bal.ok
        assert bal.remaining_usd == pytest.approx(0.0)
        assert bal.is_exhausted is True
        assert bal.usage_fraction == pytest.approx(1.0)

    def test_overrun_balance(self, tmp_path):
        db = _make_db(tmp_path, [(time.time(), "telnyx", 12.0)])
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        assert bal.ok
        assert bal.remaining_usd == pytest.approx(-2.0)
        assert bal.is_exhausted is True
        assert bal.usage_fraction == pytest.approx(1.0)


class TestStoreAndGetLatestTelnyx:
    """store_telnyx_balance → get_latest_telnyx_balance round-trip."""

    def test_round_trip(self, tmp_path):
        db = str(tmp_path / "balances.db")
        bal = TelnyxBalance(
            total_spent_usd=5.0,
            starting=10.0,
            remaining_usd=5.0,
            usage_fraction=0.5,
            is_exhausted=False,
            collected_at=time.time(),
        )
        assert store_telnyx_balance(db, bal) is True
        got = get_latest_telnyx_balance(db)
        assert got is not None
        assert got.total_spent_usd == pytest.approx(5.0)
        assert got.starting == pytest.approx(10.0)
        assert got.remaining_usd == pytest.approx(5.0)
        assert got.usage_fraction == pytest.approx(0.5)
        assert got.is_exhausted is False

    def test_none_balance_not_stored(self, tmp_path):
        db = str(tmp_path / "balances.db")
        assert store_telnyx_balance(db, None) is False

    def test_get_latest_none_when_empty(self, tmp_path):
        db = str(tmp_path / "balances.db")
        assert get_latest_telnyx_balance(db) is None

    def test_idempotent_table_creation(self, tmp_path):
        """Storing twice should not fail (table already exists)."""
        db = str(tmp_path / "balances.db")
        bal1 = TelnyxBalance(
            total_spent_usd=2.0, starting=10.0, remaining_usd=8.0,
            usage_fraction=0.2, collected_at=time.time(),
        )
        bal2 = TelnyxBalance(
            total_spent_usd=4.0, starting=10.0, remaining_usd=6.0,
            usage_fraction=0.4, collected_at=time.time() + 1,
        )
        assert store_telnyx_balance(db, bal1) is True
        assert store_telnyx_balance(db, bal2) is True
        got = get_latest_telnyx_balance(db)
        assert got is not None
        assert got.total_spent_usd == pytest.approx(4.0)  # latest

    def test_time_series_latest_is_most_recent(self, tmp_path):
        """Multiple stores → get_latest returns the newest by collected_at."""
        db = str(tmp_path / "balances.db")
        t0 = time.time()
        for i, spent in enumerate([1.0, 3.0, 7.0]):
            bal = TelnyxBalance(
                total_spent_usd=spent,
                starting=10.0,
                remaining_usd=10.0 - spent,
                usage_fraction=spent / 10.0,
                collected_at=t0 + i,
            )
            assert store_telnyx_balance(db, bal) is True
        got = get_latest_telnyx_balance(db)
        assert got is not None
        assert got.total_spent_usd == pytest.approx(7.0)

    def test_store_never_raises_on_bad_path(self):
        bal = TelnyxBalance(total_spent_usd=1.0, starting=10.0, remaining_usd=9.0)
        assert store_telnyx_balance("/nonexistent/dir/db.db", bal) is False

    def test_get_latest_never_raises_on_bad_path(self):
        assert get_latest_telnyx_balance("/nonexistent/dir/db.db") is None


class TestCollectAndStoreTelnyx:
    """collect_and_store_telnyx — cron-friendly collect + persist."""

    def test_happy_path(self, tmp_path):
        db = _make_db(tmp_path, [(time.time(), "telnyx", 3.0)])
        result = collect_and_store_telnyx(db_path=db, starting=10.0)
        assert result is not None
        assert result.ok
        assert result.total_spent_usd == pytest.approx(3.0)

    def test_empty_db_returns_balance(self, tmp_path):
        """Empty DB (no spend) is still 'ok' — spend=0, usage=0."""
        db = str(tmp_path / "fresh.db")
        result = collect_and_store_telnyx(db_path=db, starting=10.0)
        assert result is not None
        assert result.ok
        assert result.total_spent_usd == pytest.approx(0.0)

    def test_bad_db_returns_none(self):
        result = collect_and_store_telnyx(
            db_path="/nonexistent/path/db.db", starting=10.0
        )
        # _query_telnyx_spent creates the table → SUM returns 0 even on fresh
        # path. But if the directory doesn't exist, sqlite3.connect fails
        # and _query_telnyx_spent returns (None, error) → bal.error set → not ok.
        if result is not None:
            assert not result.ok or result.error is not None


class TestTelnyxQuotaEntry:
    """telnyx_quota_entry bridge: provider_balances → quota_state['telnyx']."""

    def test_cold_start_empty_db_returns_empty_dict(self, tmp_path):
        """No stored row → {} (proxy falls back to {used_pct:0, remaining:inf})."""
        db = str(tmp_path / "balances.db")
        entry = telnyx_quota_entry(db_path=db)
        assert entry == {}

    def test_fresh_row_returns_full_entry(self, tmp_path):
        db = str(tmp_path / "balances.db")
        bal = TelnyxBalance(
            total_spent_usd=5.0,
            starting=10.0,
            remaining_usd=5.0,
            usage_fraction=0.5,
            is_exhausted=False,
            collected_at=time.time(),
        )
        store_telnyx_balance(db, bal)
        entry = telnyx_quota_entry(db_path=db)
        assert "used_pct" in entry
        assert "remaining" in entry
        assert "starting" in entry
        assert "is_exhausted" in entry
        assert "collected_at" in entry
        assert entry["used_pct"] == pytest.approx(50.0)
        assert entry["remaining"] == pytest.approx(5.0)
        assert entry["starting"] == pytest.approx(10.0)
        assert entry["is_exhausted"] is False

    def test_stale_row_returns_empty(self, tmp_path):
        """Row older than max_age → {} (cold-start contract)."""
        db = str(tmp_path / "balances.db")
        bal = TelnyxBalance(
            total_spent_usd=5.0,
            starting=10.0,
            remaining_usd=5.0,
            usage_fraction=0.5,
            collected_at=time.time() - 9999,  # very old
        )
        store_telnyx_balance(db, bal)
        entry = telnyx_quota_entry(db_path=db, max_age=100.0)
        assert entry == {}

    def test_stale_row_with_max_age_none_returns_it(self, tmp_path):
        """max_age=None → use newest row regardless of age."""
        db = str(tmp_path / "balances.db")
        bal = TelnyxBalance(
            total_spent_usd=5.0,
            starting=10.0,
            remaining_usd=5.0,
            usage_fraction=0.5,
            collected_at=time.time() - 9999,
        )
        store_telnyx_balance(db, bal)
        entry = telnyx_quota_entry(db_path=db, max_age=None)
        assert entry != {}
        assert entry["used_pct"] == pytest.approx(50.0)

    def test_exhausted_balance_entry(self, tmp_path):
        db = str(tmp_path / "balances.db")
        bal = TelnyxBalance(
            total_spent_usd=10.0,
            starting=10.0,
            remaining_usd=0.0,
            usage_fraction=1.0,
            is_exhausted=True,
            collected_at=time.time(),
        )
        store_telnyx_balance(db, bal)
        entry = telnyx_quota_entry(db_path=db)
        assert entry["used_pct"] == pytest.approx(100.0)
        assert entry["remaining"] == pytest.approx(0.0)
        assert entry["is_exhausted"] is True

    def test_never_raises_on_bad_path(self):
        entry = telnyx_quota_entry(db_path="/nonexistent/path/db.db")
        assert entry == {}


class TestTelnyxBalanceDataclass:
    """TelnyxBalance dataclass properties."""

    def test_ok_true_when_no_error_and_spent_set(self):
        bal = TelnyxBalance(total_spent_usd=5.0, starting=10.0, remaining_usd=5.0)
        assert bal.ok is True

    def test_ok_false_when_error(self):
        bal = TelnyxBalance(error="db error")
        assert bal.ok is False

    def test_ok_false_when_spent_none(self):
        bal = TelnyxBalance(total_spent_usd=None, error=None)
        assert bal.ok is False

    def test_used_pct_property(self):
        bal = TelnyxBalance(usage_fraction=0.5)
        assert bal.used_pct == pytest.approx(50.0)

    def test_used_pct_zero(self):
        bal = TelnyxBalance(usage_fraction=0.0)
        assert bal.used_pct == pytest.approx(0.0)

    def test_used_pct_full(self):
        bal = TelnyxBalance(usage_fraction=1.0)
        assert bal.used_pct == pytest.approx(100.0)

    def test_default_starting(self):
        bal = TelnyxBalance()
        assert bal.starting == TELNYX_DEFAULT_STARTING_BALANCE


class TestTelnyxCLI:
    """CLI main() dispatch for --provider telnyx."""

    def test_cli_happy_path(self, tmp_path, monkeypatch, capsys):
        db = _make_db(tmp_path, [(time.time(), "telnyx", 3.0)])
        monkeypatch.setenv(TELNYX_STARTING_ENV, "10.0")
        rc = main(["--provider", "telnyx", "--db", db])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["provider"] == "telnyx"
        assert data["ok"] is True
        assert data["total_spent_usd"] == pytest.approx(3.0)
        assert data["starting"] == pytest.approx(10.0)
        assert data["db_path"] == db

    def test_cli_empty_db(self, tmp_path, monkeypatch, capsys):
        """Fresh DB with no spend → ok=True, spent=0."""
        db = str(tmp_path / "fresh.db")
        monkeypatch.setenv(TELNYX_STARTING_ENV, "10.0")
        rc = main(["--provider", "telnyx", "--db", db])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["ok"] is True
        assert data["total_spent_usd"] == pytest.approx(0.0)

    def test_cli_unknown_provider(self, capsys):
        rc = main(["--provider", "unknown"])
        assert rc == 1
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["ok"] is False

    def test_cli_missing_provider(self, capsys):
        rc = main([])
        assert rc == 1
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["ok"] is False


# ════════════════════════════════════════════════════════════════════════════
# Integration: live_router credit pressure with Telnyx
# ════════════════════════════════════════════════════════════════════════════

class TestLiveRouterTelnyxPressure:
    """_compute_credit_pressure with Telnyx parameters in live_router."""

    def test_cold_start_conservative(self, tmp_path):
        """No spend rows → cold start → pressure > 1.0 (conservative)."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_db(tmp_path)
        try:
            p = _compute_credit_pressure(
                db, "telnyx", 10.0,
                onset=0.80, asymptote=1.5,
            )
            assert p > 1.0
            assert p == pytest.approx(1.5)
        finally:
            _credit_spend_cache.clear()

    def test_fresh_balance_no_pressure(self, tmp_path):
        """Rows exist, spend=0 → u=0 → pressure = 1.0."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_db(tmp_path)
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'telnyx', 'glm-5.2', 0.0)",
                (time.time(),),
            )
            conn.commit()
            conn.close()
            p = _compute_credit_pressure(
                db, "telnyx", 10.0,
                onset=0.80, asymptote=1.5,
            )
            assert p == pytest.approx(1.0)
        finally:
            _credit_spend_cache.clear()

    def test_high_spend_raises_pressure(self, tmp_path):
        """$9 spent of $10 → u=0.9 → pressure > 1.0 (above onset 0.80)."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_db(tmp_path)
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'telnyx', 'kimi-k3', 9.0)",
                (time.time(),),
            )
            conn.commit()
            conn.close()
            p = _compute_credit_pressure(
                db, "telnyx", 10.0,
                onset=0.80, asymptote=1.5,
            )
            assert p > 1.0
            assert math.isfinite(p)
        finally:
            _credit_spend_cache.clear()

    def test_exhausted_is_inf(self, tmp_path):
        """$10 spent of $10 → u=1.0 → hard_limit → +inf."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_db(tmp_path)
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'telnyx', 'kimi-k3', 10.0)",
                (time.time(),),
            )
            conn.commit()
            conn.close()
            p = _compute_credit_pressure(
                db, "telnyx", 10.0,
                onset=0.80, asymptote=1.5,
            )
            assert p == math.inf
        finally:
            _credit_spend_cache.clear()

    def test_overrun_is_inf(self, tmp_path):
        """$11 spent of $10 → remaining < 0 → +inf."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_db(tmp_path)
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'telnyx', 'kimi-k3', 11.0)",
                (time.time(),),
            )
            conn.commit()
            conn.close()
            p = _compute_credit_pressure(
                db, "telnyx", 10.0,
                onset=0.80, asymptote=1.5,
            )
            assert p == math.inf
        finally:
            _credit_spend_cache.clear()

    def test_non_telnyx_rows_excluded(self, tmp_path):
        """Only rows with key_name='telnyx' count toward Telnyx spend."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_db(tmp_path)
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'telnyx', 'kimi-k3', 1.0)",
                (time.time(),),
            )
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'ppq', 'kimi-k3', 100.0)",
                (time.time(),),
            )
            conn.commit()
            conn.close()
            p = _compute_credit_pressure(
                db, "telnyx", 10.0,
                onset=0.80, asymptote=1.5,
            )
            # Only $1 of $10 spent → u=0.1 → below onset → 1.0
            assert p == pytest.approx(1.0)
        finally:
            _credit_spend_cache.clear()

    def test_never_raises_on_bad_db(self):
        """Bad DB path → returns conservative cold-start pressure, never raises."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        try:
            p = _compute_credit_pressure(
                "/nonexistent/path/db.db", "telnyx", 10.0,
                onset=0.80, asymptote=1.5,
            )
            assert p >= 1.0  # cold-start pressure (conservative)
        finally:
            _credit_spend_cache.clear()

    def test_telnyx_in_external_providers(self):
        """Telnyx must appear in _EXTERNAL_PROVIDERS tuple."""
        from src.live_router import _EXTERNAL_PROVIDERS
        assert "telnyx" in _EXTERNAL_PROVIDERS