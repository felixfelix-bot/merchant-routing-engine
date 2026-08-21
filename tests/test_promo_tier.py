"""Tests for src/promo_tier.py — OX-1 promo-tier guard (pure module).

Contract under test — docs/PLAN-oxalpha-promo-2026-08-21.md §2/§3:

  - §2.3 expiry flip: PROMO_END=2026-08-28T00:00Z; strictly before → tier
    active (effective price at the ADR-004 floor, NEVER $0.00); at/after →
    auto-disable + effective price flips to the pessimistic post-promo
    estimate ($10/$30 per M) so the tier can never silently bill.
  - §2.4 spend guard: ANY observed nonzero charge → immediate disable +
    ONE anomaly_events-shaped row (severity/category/title/detail). The kill
    fires exactly once; re-enable is impossible in-process (human-only).
  - §2.4 402 path: HTTP 402 on a $0 promo tier → disable for the promo
    remainder (terms changed), anomaly row emitted.
  - §2.5 allowlist: only vision / bulk_summarize / shadow_eval reach the
    tier; everything else (incl. None, case variants, non-strings) rejected.
  - §3.1/§3.2 promo tag: oxalpha price rows carry source='promo' at the
    $0.001 floor; promo-tagged rows (by source OR provider registry) are
    excluded from the p20 percentile history — during AND after the promo.
  - ADR-004: effective price is never below the floor in price_observations,
    in every state (promo, post-promo, misconfigured, disabled-early).
  - §2.6 rate-limit backoff policy constants (60/120/300 s, circuit breaker
    5 failures / 300 s cooldown) — pure, no sleeping.

Pure module: no DB, no network, no filesystem. anomaly events are RETURNED
(and collected on the guard) — the caller (OX-2 proxy wiring) inserts them.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import promo_tier  # noqa: E402
from src.promo_tier import PromoTierGuard  # noqa: E402

# ── Shared time fixtures (D3: hard deadline 2026-08-28T00:00Z) ──────────────
PROMO_END = datetime(2026, 8, 28, 0, 0, 0, tzinfo=timezone.utc)
BEFORE = PROMO_END - timedelta(seconds=1)
AT = PROMO_END
AFTER = PROMO_END + timedelta(hours=1)
FLOOR = 0.001


# ── Module defaults ─────────────────────────────────────────────────────────


class TestDefaults:
    def test_promo_end_default_is_felix_approved_deadline(self):
        # D3: conservative hard deadline — protect against silent edits.
        assert promo_tier.PROMO_END_DEFAULT == "2026-08-28T00:00:00Z"

    def test_post_promo_pessimistic_default(self):
        assert promo_tier.POST_PROMO_PESSIMISTIC_PER_M_DEFAULT == {
            "input": 10.0,
            "output": 30.0,
        }

    def test_registry_contains_oxalpha(self):
        assert promo_tier.PROMO_PROVIDERS == frozenset({"oxalpha"})
        assert promo_tier.PROVIDER_NAME == "oxalpha"
        assert promo_tier.MODEL_NAME == "stealth/ox-alpha"

    def test_allowlist_default_exactly_three_types(self):
        assert promo_tier.ALLOWED_TASK_TYPES_DEFAULT == frozenset(
            {"vision", "bulk_summarize", "shadow_eval"}
        )


# ── §2.3 Expiry flip ────────────────────────────────────────────────────────


class TestExpiryFlip:
    def test_before_deadline_active_at_floor(self):
        g = PromoTierGuard()
        st = g.status(BEFORE)
        assert st["enabled"] is True
        assert st["in_promo"] is True
        assert st["disable_reason"] is None
        # "active at $0" cash-wise, but effective price NEVER $0.00 (ADR-004)
        assert st["effective_price_per_m"] == {"input": FLOOR, "output": FLOOR}

    def test_at_deadline_exactly_flips(self):
        g = PromoTierGuard()
        st = g.status(AT)  # inclusive boundary: at/after → disabled
        assert st["enabled"] is False
        assert st["disable_reason"] == promo_tier.REASON_PROMO_EXPIRED
        assert st["effective_price_per_m"] == {"input": 10.0, "output": 30.0}

    def test_after_deadline_disabled_and_pessimistic(self):
        g = PromoTierGuard()
        st = g.status(AFTER)
        assert st["enabled"] is False
        assert st["disable_reason"] == promo_tier.REASON_PROMO_EXPIRED
        assert st["effective_price_per_m"] == {"input": 10.0, "output": 30.0}

    def test_flip_is_sticky_once_fired(self):
        g = PromoTierGuard()
        assert g.status(BEFORE)["enabled"] is True
        assert g.status(AFTER)["enabled"] is False
        # state does not un-flip even if asked about an earlier instant
        assert g.status(BEFORE)["enabled"] is False

    def test_expiry_is_expected_not_anomalous(self):
        g = PromoTierGuard()
        g.status(AFTER)
        assert g.anomaly_events == []  # no anomaly row for the planned flip

    def test_default_guard_is_not_yet_flipped_at_construction(self):
        # constructed without arguments must not pre-disable (D3 deadline)
        g = PromoTierGuard()
        assert g.disabled_reason is None


# ── §2.4 Cost>0 kill ────────────────────────────────────────────────────────


class TestChargeKill:
    def test_zero_cost_is_the_normal_path(self):
        g = PromoTierGuard()
        assert g.observe_charge(0.0, now=BEFORE) is None
        assert g.observe_charge(0, now=BEFORE) is None
        assert g.observe_charge(None, now=BEFORE) is None
        assert g.status(BEFORE)["enabled"] is True
        assert g.anomaly_events == []

    def test_nonzero_charge_kills_and_emits_exactly_one_anomaly(self):
        g = PromoTierGuard()
        ev = g.observe_charge(0.0031, now=BEFORE)
        assert ev is not None
        # event matches the shared anomaly_events schema shape
        assert ev["severity"] == "critical"
        assert ev["category"] == "promo_spend"
        assert "oxalpha" in ev["title"]
        assert isinstance(ev["ts"], float) and ev["ts"] > 0
        detail = json.loads(ev["detail"])
        assert detail["cost_usd"] == pytest.approx(0.0031)
        assert detail["reason"] == promo_tier.REASON_NONZERO_CHARGE
        # disable happened (kill → disabled, event emitted)
        assert g.status(BEFORE)["enabled"] is False
        assert g.status(BEFORE)["disable_reason"] == promo_tier.REASON_NONZERO_CHARGE

    def test_kill_fires_once_even_on_repeated_charges(self):
        g = PromoTierGuard()
        assert g.observe_charge(0.5, now=BEFORE) is not None
        assert g.observe_charge(5.0, now=BEFORE) is None  # no second event
        assert g.observe_charge(0.01, now=AFTER) is None
        assert len(g.anomaly_events) == 1
        assert g.status(BEFORE)["enabled"] is False

    def test_killed_tier_prices_pessimistic_even_inside_promo(self):
        # a charge means promo terms changed → never trust $0 again
        g = PromoTierGuard()
        g.observe_charge(0.25, now=BEFORE)
        st = g.status(BEFORE)  # still inside the promo window
        assert st["enabled"] is False
        assert st["effective_price_per_m"] == {"input": 10.0, "output": 30.0}

    def test_negative_wallet_delta_same_kill(self):
        # §2.4 cross-check: negative OpenRouter wallet delta while oxalpha is
        # the only consumer → the SAME kill + anomaly event, fired once.
        g = PromoTierGuard()
        assert g.observe_wallet_delta(0.42, now=BEFORE) is None  # credit = fine
        ev = g.observe_wallet_delta(-0.05, now=BEFORE)
        assert ev is not None
        detail = json.loads(ev["detail"])
        assert detail["reason"] == promo_tier.REASON_NONZERO_CHARGE
        assert detail["source"] == "wallet_delta"
        assert g.status(BEFORE)["enabled"] is False
        # shared once-flag with usage.cost detector
        assert g.observe_charge(9.9, now=BEFORE) is None
        assert len(g.anomaly_events) == 1

    def test_charge_after_expiry_disable_still_alerts(self):
        # expiry already disabled the tier; an observed charge is still an
        # independent alarm (in-flight request billed post-promo).
        g = PromoTierGuard()
        g.status(AFTER)
        ev = g.observe_charge(1.0, now=AFTER)
        assert ev is not None
        assert len(g.anomaly_events) == 1


# ── §2.4 402 path ───────────────────────────────────────────────────────────


class TestHttp402:
    def test_402_disables_for_promo_remainder(self):
        g = PromoTierGuard()
        ev = g.observe_http_status(402, now=BEFORE)
        assert ev is not None
        assert ev["severity"] == "warning"
        detail = json.loads(ev["detail"])
        assert detail["reason"] == promo_tier.REASON_HTTP_402
        st = g.status(BEFORE)
        assert st["enabled"] is False
        assert st["in_promo"] is True  # disabled FOR the promo remainder
        assert st["effective_price_per_m"] == {"input": 10.0, "output": 30.0}

    def test_402_fires_once(self):
        g = PromoTierGuard()
        assert g.observe_http_status(402, now=BEFORE) is not None
        assert g.observe_http_status(402, now=BEFORE) is None
        assert len(g.anomaly_events) == 1

    def test_other_statuses_do_not_disable(self):
        g = PromoTierGuard()
        for code in (200, 400, 404, 429, 500, 503):
            assert g.observe_http_status(code, now=BEFORE) is None
        assert g.status(BEFORE)["enabled"] is True

    def test_disable_has_no_inprocess_reenable(self):
        g = PromoTierGuard()
        g.observe_http_status(402, now=BEFORE)
        # structurally impossible to re-enable: no method exists
        assert not hasattr(g, "re_enable") and not hasattr(g, "enable")
        assert not hasattr(g, "reset")


# ── §2.5 Allowlist ──────────────────────────────────────────────────────────


class TestAllowlist:
    @pytest.mark.parametrize("task", ["vision", "bulk_summarize", "shadow_eval"])
    def test_approved_types_accepted(self, task):
        assert PromoTierGuard().task_type_allowed(task) is True

    @pytest.mark.parametrize(
        "task",
        [
            None,
            "",
            "coding",
            "reasoning",
            "chat",
            "simple",
            "VISION",  # case-sensitive: header must be exact
            " vision",  # whitespace is not tolerated
            "vision ",
            "summarize",
            123,  # non-strings rejected, never raise
            b"vision",
        ],
    )
    def test_everything_else_rejected(self, task):
        assert PromoTierGuard().task_type_allowed(task) is False


# ── ADR-004 floor ───────────────────────────────────────────────────────────


class TestFloor:
    def test_promo_price_never_below_floor(self):
        p = PromoTierGuard().effective_price_per_m(BEFORE)
        assert p["input"] >= FLOOR and p["output"] >= FLOOR
        assert p["input"] > 0 and p["output"] > 0  # never $0.00

    def test_post_promo_price_never_below_floor(self):
        p = PromoTierGuard().effective_price_per_m(AFTER)
        assert p["input"] >= FLOOR and p["output"] >= FLOOR

    def test_misconfigured_pessimistic_below_floor_clamps(self):
        g = PromoTierGuard(post_promo_per_m={"input": 0.0, "output": 0.0000001})
        p = g.effective_price_per_m(AFTER)
        assert p == {"input": FLOOR, "output": FLOOR}

    def test_zero_floor_rejected(self):
        with pytest.raises(ValueError):
            PromoTierGuard(min_effective_price=0.0)

    def test_price_observation_row_never_below_floor_or_zero(self):
        g = PromoTierGuard()
        for t in (BEFORE, AT, AFTER):
            row = g.price_observation_row(t)
            assert row["rate_per_m"] >= FLOOR
            assert row["rate_per_m"] > 0

    def test_price_row_shape_is_promo_tagged_unmeasured(self):
        row = PromoTierGuard().price_observation_row(BEFORE)
        assert row["provider"] == "oxalpha"
        assert row["model"] == "stealth/ox-alpha"
        assert row["rate_per_m"] == FLOOR
        assert row["is_measured"] is False
        assert row["source"] == "promo"  # §3.1: tag joins CG-2's source column

    def test_price_row_post_promo_is_conservative_scalar(self):
        row = PromoTierGuard().price_observation_row(AFTER)
        assert row["rate_per_m"] == 30.0  # max(input=10, output=30): priced OUT

    def test_effective_rate_scalar_is_conservative(self):
        assert PromoTierGuard().effective_rate_per_m(BEFORE) == FLOOR
        assert PromoTierGuard().effective_rate_per_m(AFTER) == 30.0


# ── §3.2 p20 filter (promo-tag registry) ────────────────────────────────────


def _p20(values):
    return statistics.quantiles(values, n=100, method="inclusive")[19]


class TestP20Filter:
    def _rows(self):
        # synthetic price_observations window: real tiers + promo rows in
        # both tagging styles (source column AND provider-registry-only)
        return [
            {"provider": "zai", "model": "glm-5.2", "rate_per_m": 0.0043},
            {"provider": "oxalpha", "model": "stealth/ox-alpha",
             "rate_per_m": 0.001, "source": "promo"},
            {"provider": "zai", "model": "glm-5.2", "rate_per_m": 0.0051},
            {"provider": "oxalpha", "model": "stealth/ox-alpha",
             "rate_per_m": 0.001},  # untagged row: caught by the registry
            {"provider": "deepinfra", "model": "deepseek-v4-flash",
             "rate_per_m": 0.0062},
            {"provider": "openrouter", "model": "deepseek-v4-flash",
             "rate_per_m": 0.0075, "source": "measured"},
            {"provider": "zai", "model": "glm-5.3", "rate_per_m": 0.0084},
        ]

    def test_promo_rows_excluded_in_both_tag_styles(self):
        kept = promo_tier.filter_promo_rows(self._rows())
        assert [r["provider"] for r in kept] == [
            "zai", "zai", "deepinfra", "openrouter", "zai",
        ]
        # order of non-promo rows preserved
        assert kept[0]["rate_per_m"] == 0.0043

    def test_source_tag_alone_triggers_exclusion(self):
        rows = [{"provider": "zai", "rate_per_m": 1.0, "source": "promo"}]
        assert promo_tier.filter_promo_rows(rows) == []

    def test_no_promo_rows_identity(self):
        rows = [
            {"provider": "zai", "rate_per_m": 0.0043, "source": "measured"},
            {"provider": "ppq", "rate_per_m": 0.0090},
        ]
        assert promo_tier.filter_promo_rows(rows) == rows

    def test_empty_window(self):
        assert promo_tier.filter_promo_rows([]) == []

    def test_p20_band_not_distorted_with_promo_rows_present(self):
        rows = self._rows()
        filtered = [r["rate_per_m"] for r in promo_tier.filter_promo_rows(rows)]
        real_only = [0.0043, 0.0051, 0.0062, 0.0075, 0.0084]  # known real tiers
        unfiltered = [r["rate_per_m"] for r in rows]
        # band over filtered == band over real-only …
        assert _p20(filtered) == pytest.approx(_p20(real_only))
        # … and != band polluted by $0.001 promo rows
        assert _p20(unfiltered) < _p20(real_only)

    def test_post_promo_pessimistic_rows_also_filtered(self):
        # after the flip, oxalpha rows at $10/$30 must not spike the band
        rows = self._rows() + [
            {"provider": "oxalpha", "model": "stealth/ox-alpha",
             "rate_per_m": 30.0, "source": "promo"},
        ]
        kept = promo_tier.filter_promo_rows(rows)
        assert all(r["provider"] != "oxalpha" for r in kept)
        assert all(r["rate_per_m"] < 1.0 for r in kept)

    def test_sql_helper_full(self):
        sql = promo_tier.promo_exclusion_sql()
        assert "source" in sql and "promo" in sql
        assert "provider" in sql and "oxalpha" in sql

    def test_sql_helper_without_source_column(self):
        # CG-2 pre-source-column composition: registry-only clause
        sql = promo_tier.promo_exclusion_sql(source_col=None)
        assert "source" not in sql
        assert "provider" in sql and "oxalpha" in sql


# ── §2.6 Backoff policy (pure constants) ────────────────────────────────────


class TestBackoffPolicy:
    @pytest.mark.parametrize(
        "n,expected",
        [(0, 0.0), (-3, 0.0), (1, 60.0), (2, 120.0), (3, 300.0),
         (4, 300.0), (50, 300.0)],
    )
    def test_backoff_sequence(self, n, expected):
        assert promo_tier.rate_limit_backoff_s(n) == expected

    def test_sequence_constant_matches_plan(self):
        assert promo_tier.RATE_LIMIT_BACKOFF_SEQUENCE_S == (60.0, 120.0, 300.0)

    def test_circuit_breaker_mirrors_strategy_defaults(self):
        assert promo_tier.CIRCUIT_BREAKER_THRESHOLD == 5
        assert promo_tier.CIRCUIT_BREAKER_COOLDOWN_S == 300.0


# ── from_config (providers.yaml oxalpha block) ─────────────────────────────


_OXALPHA_CFG = {
    "base_url": "https://openrouter.ai/api/v1",
    "key_env": "OPENROUTER_OXALPHA_KEY",
    "pricing_model": "promo_zero",
    "promo": {
        "expires_at": "2026-08-28T00:00:00Z",
        "post_promo_pessimistic_per_m": {"input": 10.0, "output": 30.0},
        "verified_rate": {"input": 0.0, "output": 0.0},
    },
    "budget_usd": 0,
    "data_sensitivity": "allowlist",
    "allowlist_task_types": ["vision", "bulk_summarize", "shadow_eval"],
}


class TestFromConfig:
    def test_full_block(self):
        g = PromoTierGuard.from_config(_OXALPHA_CFG)
        assert g.promo_end == PROMO_END
        assert g.post_promo_per_m == {"input": 10.0, "output": 30.0}
        assert g.budget_usd == 0.0
        assert g.allowed_task_types == frozenset(
            {"vision", "bulk_summarize", "shadow_eval"}
        )
        assert g.min_effective_price == FLOOR
        assert g.verified_rate_per_m == {"input": 0.0, "output": 0.0}

    def test_empty_config_uses_defaults(self):
        g = PromoTierGuard.from_config({})
        assert g.promo_end == PROMO_END
        assert g.post_promo_per_m == {"input": 10.0, "output": 30.0}
        assert g.allowed_task_types == promo_tier.ALLOWED_TASK_TYPES_DEFAULT

    def test_none_config_uses_defaults(self):
        g = PromoTierGuard.from_config(None)
        assert g.promo_end == PROMO_END

    def test_strategy_floor_override(self):
        g = PromoTierGuard.from_config(
            _OXALPHA_CFG, strategy_cfg={"min_effective_price": 0.002}
        )
        assert g.effective_price_per_m(BEFORE) == {"input": 0.002, "output": 0.002}

    def test_bad_expires_at_raises(self):
        with pytest.raises(ValueError):
            PromoTierGuard.from_config(
                {"promo": {"expires_at": "not-a-date"}}
            )

    def test_expires_at_z_suffix_parses_utc(self):
        g = PromoTierGuard.from_config(_OXALPHA_CFG)
        assert g.promo_end.tzinfo is not None
        assert g.promo_end.utcoffset().total_seconds() == 0

    def test_repo_providers_yaml_block_parses(self):
        # the config fixture committed with OX-1 must load through yaml and
        # produce a guard identical to the inline block above
        import yaml

        repo_cfg_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "config", "providers.yaml",
        )
        with open(repo_cfg_path) as f:
            cfg = yaml.safe_load(f)
        assert "oxalpha" in cfg, "repo providers.yaml must carry the OX-1 block"
        g = PromoTierGuard.from_config(cfg["oxalpha"], strategy_cfg=cfg.get("strategy"))
        assert g.promo_end == PROMO_END
        assert g.allowed_task_types == frozenset(
            {"vision", "bulk_summarize", "shadow_eval"}
        )
        assert g.effective_price_per_m(BEFORE)["input"] >= FLOOR


# ── parse_promo_end ─────────────────────────────────────────────────────────


class TestParsePromoEnd:
    def test_z_suffix_string(self):
        assert promo_tier.parse_promo_end("2026-08-28T00:00:00Z") == PROMO_END

    def test_offset_string(self):
        assert promo_tier.parse_promo_end("2026-08-28T02:00:00+02:00") == PROMO_END

    def test_naive_datetime_assumed_utc(self):
        naive = datetime(2026, 8, 28, 0, 0, 0)
        assert promo_tier.parse_promo_end(naive) == PROMO_END

    def test_datetime_passthrough_coerced(self):
        assert promo_tier.parse_promo_end(PROMO_END) == PROMO_END

    def test_garbage_raises(self):
        for bad in ("not-a-date", "", 12345, None):
            with pytest.raises(ValueError):
                promo_tier.parse_promo_end(bad)

    def test_numeric_ts_rejected(self):
        with pytest.raises(ValueError):
            promo_tier.parse_promo_end(1787232000)
