"""Tests for src/cost_gate.py — percentile cost gate (CG-1, plan v2 §5).

Covers every row of the §5 fail-closed matrix, the §2.2–§2.4 gate mechanics
(p20 threshold, 20% hysteresis exit band, 30-min dwell, <48-sample cold start,
job-run snapshot), the §2.5 budget backstop (paid tiers only), §3 override
consumption + scope isolation + freeze/dead-key immunity, composition with
src/dispatch_gate.py (TASK_PROFILES + HARDWARE_SAFETY_MARGIN, no duplication),
and the config/budget.yaml loader.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.dispatch_gate as dispatch_gate
from src.cost_gate import (
    ALLOW,
    DEFER,
    DENY,
    BACKSTOP_WARN_PCT,
    DEFAULT_DAILY_CAP_USD,
    DWELL_SECONDS,
    EXIT_BAND_MULTIPLIER,
    HARDWARE_SAFETY_MARGIN,
    HISTORY_WINDOW_DAYS,
    MIN_HISTORY_SAMPLES,
    MIN_EFFECTIVE_PRICE,
    OVERRIDE_SCOPES,
    PAID,
    PERCENTILE,
    PRICE_STALE_MAX_MIN,
    SUBSCRIPTION,
    TASK_PROFILES,
    evaluate_cost_gate,
    is_override_active,
    load_budget_config,
    percentile,
    percentile_rank,
    resolve_route_tier,
)


# ── helpers ──────────────────────────────────────────────────────────────────

#: 100 hourly-median samples, values 1.0 .. 100.0 → p20 = 20.8, exit = 24.96
HISTORY = [float(i) for i in range(1, 101)]
P20 = 20.8
EXIT_THRESHOLD = P20 * 1.20  # 24.96

#: 47 / 48-sample histories for the cold-start boundary (§2.4)
HISTORY_47 = [float(i) for i in range(1, 48)]   # p20 = 1 + 0.2*46 = 10.2
HISTORY_48 = [float(i) for i in range(1, 49)]   # p20 = 1 + 0.2*47 = 10.4

NOW = 1_000_000.0
CAP = 15.0

CHEAP_PRICE = 10.0        # ≤ p20
IN_BAND_PRICE = 22.0      # p20 < price ≤ p20×1.2 (hysteresis stay-cheap zone)
EXPENSIVE_PRICE = 50.0    # > p20×1.2


def _call(**kw):
    """evaluate_cost_gate with §5-shaped sane defaults (ALLOW path)."""
    defaults = dict(
        model="glm-5.2",
        task_type="coding",
        deferrable=True,
        route_tier=SUBSCRIPTION,
        effective_price_usd_per_m=CHEAP_PRICE,
        price_source="v1/pricing?horizon_min=30",
        price_age_min=0.0,
        price_unreachable=False,
        price_history=HISTORY,
        history_window_days=HISTORY_WINDOW_DAYS,
        rolling_paid_spend_usd=0.0,
        budget_cap_usd=CAP,
        override=None,
        freeze_marker=False,
        zai_key_dead_or_locked=False,
        estimated_tokens=None,
        hardware_req="none",
        hysteresis_state=None,
        now_ts=NOW,
    )
    defaults.update(kw)
    return evaluate_cost_gate(**defaults)


def _override(scope="budget", expires_ts=None):
    """A valid §3 override grant (TTL mandatory, single scope)."""
    return {
        "scope": scope,
        "expires_ts": NOW + 3600.0 if expires_ts is None else expires_ts,
        "issued_by": "felix",
        "reason": "test grant",
    }


def _cheap_state(last_flip_ts=None):
    return {"cheap": True, "last_flip_ts": last_flip_ts}


# ── constants match plan §2/§5 ───────────────────────────────────────────────


class TestConstants:
    def test_plan_constants(self):
        assert PERCENTILE == 20.0
        assert EXIT_BAND_MULTIPLIER == pytest.approx(1.20)
        assert DWELL_SECONDS == pytest.approx(30 * 60)
        assert MIN_HISTORY_SAMPLES == 48
        assert HISTORY_WINDOW_DAYS == 7
        assert PRICE_STALE_MAX_MIN == pytest.approx(15.0)
        assert DEFAULT_DAILY_CAP_USD == pytest.approx(15.0)
        assert BACKSTOP_WARN_PCT == pytest.approx(50.0)

    def test_override_scopes_match_plan_section_3(self):
        assert set(OVERRIDE_SCOPES) == {
            "budget", "price_history", "infra_down", "paid_ceiling",
        }

    def test_decision_strings(self):
        assert (ALLOW, DEFER, DENY) == ("ALLOW", "DEFER", "DENY")


# ── composition with dispatch_gate (no duplication) ─────────────────────────


class TestComposition:
    def test_task_profiles_are_dispatch_gate_tables(self):
        # CG-1: "Composes src/dispatch_gate.py (TASK_PROFILES, margins) — no
        # duplication." The tables must be the SAME objects, not copies.
        assert TASK_PROFILES is dispatch_gate.TASK_PROFILES
        assert HARDWARE_SAFETY_MARGIN is dispatch_gate.HARDWARE_SAFETY_MARGIN

    def test_min_effective_price_reused(self):
        assert MIN_EFFECTIVE_PRICE == dispatch_gate.MIN_EFFECTIVE_PRICE

    def test_model_resolved_from_task_type_when_model_none(self):
        v = _call(model=None, task_type="research")
        assert v["model"] == "glm-5.2"          # TASK_PROFILES["research"]
        assert v["provenance"]["model"] == "glm-5.2"

    def test_unknown_task_type_falls_back_to_coding_profile(self):
        v = _call(model=None, task_type="nonsense")
        assert v["model"] == dispatch_gate.TASK_PROFILES["coding"]["model"]

    def test_explicit_model_wins(self):
        v = _call(model="glm-4.5-flash", task_type="research")
        assert v["model"] == "glm-4.5-flash"

    def test_margin_used_from_dispatch_gate_table(self):
        # informational margin-scaled headroom mirrors dispatch_gate's
        # hardware-scaled margin concept using the SAME table.
        v = _call(
            estimated_tokens=1_000_000,
            hardware_req="board",          # margin 4.0
        )
        assert v["predicted_cost_usd"] == pytest.approx(CHEAP_PRICE * 1.0)
        assert v["required_headroom_usd"] == pytest.approx(
            CHEAP_PRICE * HARDWARE_SAFETY_MARGIN["board"]
        )


# ── §5 fail-closed matrix — every row ────────────────────────────────────────


class TestFailClosedMatrix:
    # Row 1: freeze marker present → DENY (hard; overrides never apply)
    def test_freeze_marker_denies(self):
        v = _call(freeze_marker=True)
        assert v["decision"] == DENY
        assert v["reason_code"] == "freeze_marker"
        assert v["override_consumed"] is None

    @pytest.mark.parametrize("scope", sorted(OVERRIDE_SCOPES))
    def test_freeze_marker_immune_to_every_override_scope(self, scope):
        v = _call(freeze_marker=True, override=_override(scope))
        assert v["decision"] == DENY
        assert v["reason_code"] == "freeze_marker"
        assert v["override_consumed"] is None

    # Row 2: dead/locked z.ai key on the z.ai path → DENY (hard; immune)
    def test_dead_or_locked_key_denies(self):
        v = _call(zai_key_dead_or_locked=True)
        assert v["decision"] == DENY
        assert v["reason_code"] == "dead_or_locked_key"

    @pytest.mark.parametrize("scope", sorted(OVERRIDE_SCOPES))
    def test_dead_key_immune_to_every_override_scope(self, scope):
        v = _call(zai_key_dead_or_locked=True, override=_override(scope))
        assert v["decision"] == DENY
        assert v["reason_code"] == "dead_or_locked_key"
        assert v["override_consumed"] is None

    # Row 3: price endpoint unreachable / stale >15 min → DENY + loud (Q10)
    def test_price_unreachable_denies_infra_down(self):
        v = _call(price_unreachable=True)
        assert v["decision"] == DENY
        assert v["reason_code"] == "infra_down"
        assert v["reason_json"]["loud"] is True

    def test_price_stale_over_15_min_denies_infra_down(self):
        v = _call(price_age_min=15.5)
        assert v["decision"] == DENY
        assert v["reason_code"] == "infra_down"
        assert v["reason_json"]["loud"] is True

    def test_price_age_exactly_15_min_is_fresh(self):
        v = _call(price_age_min=PRICE_STALE_MAX_MIN)
        assert v["decision"] == ALLOW

    def test_infra_down_escape_via_q6_override(self):
        v = _call(price_unreachable=True, override=_override("infra_down"))
        assert v["decision"] == ALLOW
        assert v["reason_code"] == "infra_down_override"
        assert v["override_consumed"]["scope"] == "infra_down"
        assert v["override_consumed"]["would_have_been"] == {
            "decision": DENY, "reason_code": "infra_down",
        }

    # Row 4: effective price unknown → DENY price_unknown
    def test_effective_price_unknown_denies(self):
        v = _call(effective_price_usd_per_m=None)
        assert v["decision"] == DENY
        assert v["reason_code"] == "price_unknown"

    # Row 5: <48h price history (deferrable task) → DEFER
    def test_history_below_48_defers_deferrable(self):
        v = _call(price_history=HISTORY_47)
        assert v["decision"] == DEFER
        assert v["reason_code"] == "price_history_insufficient"
        assert v["threshold_p20"] is None
        assert v["percentile_rank"] is None

    def test_history_at_exactly_48_proceeds(self):
        v = _call(price_history=HISTORY_48, effective_price_usd_per_m=5.0)
        assert v["decision"] == ALLOW
        assert v["threshold_p20"] == pytest.approx(10.4)

    def test_short_history_never_defers_non_deferrable(self):
        # §2.4: interactive/urgent work is never deferred on price-history
        # grounds — the budget backstop is its only gate.
        v = _call(price_history=HISTORY_47, deferrable=False)
        assert v["decision"] == ALLOW
        assert v["reason_code"] == "not_deferrable_backstop_only"

    def test_short_history_escape_via_override(self):
        v = _call(price_history=HISTORY_47, override=_override("price_history"))
        assert v["decision"] == ALLOW
        assert v["reason_code"] == "price_history_override"
        assert v["override_consumed"]["would_have_been"]["reason_code"] == \
            "price_history_insufficient"

    # Row 6: budget config missing/unparsable → DENY budget_unconfigured
    def test_budget_missing_denies(self):
        v = _call(budget_cap_usd=None)
        assert v["decision"] == DENY
        assert v["reason_code"] == "budget_unconfigured"
        assert v["headroom_usd"] is None

    def test_budget_missing_escape_via_override(self):
        v = _call(budget_cap_usd=None, override=_override("budget"))
        assert v["decision"] == ALLOW
        assert v["reason_code"] == "budget_unconfigured_override"
        assert v["override_consumed"]["scope"] == "budget"

    def test_budget_missing_outranks_history_defer(self):
        # DENY (fail-closed, surfaces the misconfig) beats DEFER (which would
        # silently reschedule into the same broken state forever).
        v = _call(budget_cap_usd=None, price_history=HISTORY_47)
        assert v["decision"] == DENY
        assert v["reason_code"] == "budget_unconfigured"

    # Row 7: paid-tier backstop exceeded → DENY paid only, subscription freed
    def test_backstop_denies_paid_tier_at_cap(self):
        v = _call(route_tier=PAID, rolling_paid_spend_usd=CAP)
        assert v["decision"] == DENY
        assert v["reason_code"] == "backstop_exceeded"
        assert v["headroom_usd"] == pytest.approx(0.0)

    def test_backstop_denies_paid_tier_over_cap(self):
        v = _call(route_tier=PAID, rolling_paid_spend_usd=CAP + 0.01)
        assert v["decision"] == DENY
        assert v["reason_code"] == "backstop_exceeded"

    def test_backstop_escape_via_override(self):
        v = _call(
            route_tier=PAID,
            rolling_paid_spend_usd=CAP,
            override=_override("budget"),
        )
        assert v["decision"] == ALLOW
        assert v["reason_code"] == "backstop_override"
        assert v["override_consumed"]["would_have_been"] == {
            "decision": DENY, "reason_code": "backstop_exceeded",
        }

    def test_backstop_denial_frees_subscription_routes(self):
        # §2.5: subscription routes are NEVER blocked by the backstop.
        v = _call(route_tier=SUBSCRIPTION, rolling_paid_spend_usd=CAP + 5.0)
        assert v["decision"] == ALLOW
        assert v["backstop"]["paid_blocked"] is True   # visibility only
        assert v["reason_code"] == "within_p20_band"

    def test_backstop_warn_at_50pct_still_allows(self):
        v = _call(route_tier=PAID, rolling_paid_spend_usd=CAP * 0.5)
        assert v["decision"] == ALLOW
        assert v["backstop"]["warn"] is True

    def test_no_warn_below_50pct(self):
        v = _call(route_tier=PAID, rolling_paid_spend_usd=CAP * 0.49)
        assert v["decision"] == ALLOW
        assert v["backstop"]["warn"] is False

    # Row 8: valid scoped override active → overrides ONLY its scope
    def test_paid_ceiling_scope_does_not_escape_backstop(self):
        # paid_ceiling is CG-6's static $/M ceiling scope — NOT the daily cap.
        v = _call(
            route_tier=PAID,
            rolling_paid_spend_usd=CAP,
            override=_override("paid_ceiling"),
        )
        assert v["decision"] == DENY
        assert v["reason_code"] == "backstop_exceeded"
        assert v["override_consumed"] is None


# ── §2.2/§2.3 percentile band + hysteresis ──────────────────────────────────


class TestPercentileBand:
    def test_p20_threshold_on_known_history(self):
        v = _call()
        assert v["threshold_p20"] == pytest.approx(P20)
        assert v["exit_threshold_p20"] == pytest.approx(EXIT_THRESHOLD)

    def test_price_at_or_below_p20_allows(self):
        v = _call(effective_price_usd_per_m=P20)
        assert v["decision"] == ALLOW
        assert v["reason_code"] == "within_p20_band"
        assert v["hysteresis"]["cheap"] is True

    def test_price_above_p20_defers_when_not_already_cheap(self):
        v = _call(effective_price_usd_per_m=IN_BAND_PRICE)
        assert v["decision"] == DEFER
        assert v["reason_code"] == "price_outside_band"
        assert v["hysteresis"]["cheap"] is False

    def test_hysteresis_stays_cheap_inside_exit_band(self):
        # §2.3: once cheap, stays cheap until price > p20×1.20.
        v = _call(
            effective_price_usd_per_m=IN_BAND_PRICE,
            hysteresis_state=_cheap_state(last_flip_ts=NOW - 3600.0),
        )
        assert v["decision"] == ALLOW
        assert v["reason_code"] == "within_exit_band_hysteresis"
        assert v["hysteresis"]["cheap"] is True

    def test_hysteresis_exits_above_band_when_dwell_elapsed(self):
        v = _call(
            effective_price_usd_per_m=EXPENSIVE_PRICE,
            hysteresis_state=_cheap_state(last_flip_ts=NOW - 3600.0),
        )
        assert v["decision"] == DEFER
        assert v["reason_code"] == "price_outside_band"
        assert v["hysteresis"]["cheap"] is False
        assert v["hysteresis"]["last_flip_ts"] == pytest.approx(NOW)

    def test_percentile_rank_formula(self):
        # rank = share of history samples ≤ current price (×100)
        v = _call(effective_price_usd_per_m=IN_BAND_PRICE)
        assert v["percentile_rank"] == pytest.approx(22.0)
        assert percentile_rank(HISTORY, CHEAP_PRICE) == pytest.approx(10.0)

    def test_percentile_linear_interpolation(self):
        assert percentile(HISTORY, 20.0) == pytest.approx(20.8)
        assert percentile([5.0], 20.0) == pytest.approx(5.0)

    def test_non_deferrable_ignores_band(self):
        # §2.6: ALLOW (non-deferrable under backstop) — price band only
        # defers deferrable work.
        v = _call(effective_price_usd_per_m=EXPENSIVE_PRICE, deferrable=False)
        assert v["decision"] == ALLOW
        assert v["reason_code"] == "not_deferrable_backstop_only"
        # rank/threshold still reported for visibility
        assert v["percentile_rank"] == pytest.approx(50.0)
        assert v["threshold_p20"] == pytest.approx(P20)

    def test_min_effective_price_floor_is_cheap(self):
        # ADR-004 floor ($0.001/M) must sit in the band for any real history.
        v = _call(effective_price_usd_per_m=MIN_EFFECTIVE_PRICE)
        assert v["decision"] == ALLOW


# ── §2.3 dwell timing (30 min between flips) ─────────────────────────────────


class TestDwell:
    def test_flip_blocked_before_dwell_elapses(self):
        # Enter CHEAP at t0; price exits the band at t0+900s (15 min) —
        # dwell has not elapsed, state must stay cheap (anti-flapping).
        v = _call(
            effective_price_usd_per_m=EXPENSIVE_PRICE,
            hysteresis_state=_cheap_state(last_flip_ts=NOW),
            now_ts=NOW + 900.0,
        )
        assert v["decision"] == ALLOW
        assert v["reason_code"] == "within_exit_band_hysteresis"
        assert v["hysteresis"]["cheap"] is True
        assert v["hysteresis"]["dwell_remaining_s"] == pytest.approx(900.0)

    def test_flip_blocked_just_before_boundary(self):
        v = _call(
            effective_price_usd_per_m=EXPENSIVE_PRICE,
            hysteresis_state=_cheap_state(last_flip_ts=NOW),
            now_ts=NOW + DWELL_SECONDS - 1.0,
        )
        assert v["decision"] == ALLOW
        assert v["hysteresis"]["cheap"] is True

    def test_flip_allowed_exactly_at_dwell_boundary(self):
        v = _call(
            effective_price_usd_per_m=EXPENSIVE_PRICE,
            hysteresis_state=_cheap_state(last_flip_ts=NOW),
            now_ts=NOW + DWELL_SECONDS,
        )
        assert v["decision"] == DEFER
        assert v["hysteresis"]["cheap"] is False
        assert v["hysteresis"]["last_flip_ts"] == pytest.approx(NOW + DWELL_SECONDS)

    def test_reentry_also_waits_for_dwell(self):
        # Exit CHEAP at t=1800; price re-enters band at t=2700 (15 min later)
        # — still not cheap; at t=3600 the entry flip is allowed.
        after_exit = {"cheap": False, "last_flip_ts": NOW + 1800.0}
        v_mid = _call(
            effective_price_usd_per_m=CHEAP_PRICE,
            hysteresis_state=after_exit,
            now_ts=NOW + 2700.0,
        )
        assert v_mid["decision"] == DEFER
        assert v_mid["hysteresis"]["cheap"] is False

        v_ok = _call(
            effective_price_usd_per_m=CHEAP_PRICE,
            hysteresis_state=after_exit,
            now_ts=NOW + 3600.0,
        )
        assert v_ok["decision"] == ALLOW
        assert v_ok["hysteresis"]["cheap"] is True
        assert v_ok["hysteresis"]["last_flip_ts"] == pytest.approx(NOW + 3600.0)

    def test_first_evaluation_enters_band_immediately(self):
        # No prior state → no prior flip → dwell satisfied at once.
        v = _call(hysteresis_state=None)
        assert v["decision"] == ALLOW
        assert v["hysteresis"]["cheap"] is True
        assert v["hysteresis"]["last_flip_ts"] == pytest.approx(NOW)
        # just flipped at NOW → next flip allowed after a full dwell
        assert v["hysteresis"]["dwell_remaining_s"] == pytest.approx(DWELL_SECONDS)


# ── §2.3 job-burst stickiness: verdict snapshot covers whole job run ────────


class TestVerdictSnapshot:
    def test_allow_verdict_snapshots_whole_job_run(self):
        v = _call()
        assert v["verdict_snapshot"]["covers_job_run"] is True
        assert v["verdict_snapshot"]["evaluated_at_ts"] == pytest.approx(NOW)

    def test_defer_and_deny_verdicts_also_snapshot(self):
        for v in (
            _call(effective_price_usd_per_m=EXPENSIVE_PRICE),
            _call(freeze_marker=True),
        ):
            assert v["verdict_snapshot"]["covers_job_run"] is True
            assert v["verdict_snapshot"]["evaluated_at_ts"] == pytest.approx(NOW)


# ── §3 override validity, consumption, scope isolation ──────────────────────


class TestOverride:
    def test_expired_override_is_inactive(self):
        # TTL is mandatory; expiry is strict (expires_ts must be > now).
        assert is_override_active(_override(expires_ts=NOW), NOW) is False
        v = _call(price_unreachable=True, override=_override(
            scope="infra_down", expires_ts=NOW))
        assert v["decision"] == DENY
        assert v["reason_code"] == "infra_down"
        assert v["override_consumed"] is None

    def test_override_without_expiry_is_invalid(self):
        ov = {"scope": "budget", "issued_by": "felix", "reason": "x"}
        assert is_override_active(ov, NOW) is False
        v = _call(budget_cap_usd=None, override=ov)
        assert v["decision"] == DENY

    def test_override_with_unknown_scope_is_invalid(self):
        assert is_override_active(_override(scope="everything"), NOW) is False

    def test_valid_override_is_active(self):
        assert is_override_active(_override(), NOW) is True

    def test_scope_isolation_budget_does_not_escape_infra_down(self):
        v = _call(price_unreachable=True, override=_override("budget"))
        assert v["decision"] == DENY
        assert v["reason_code"] == "infra_down"
        assert v["override_consumed"] is None

    def test_scope_isolation_price_history_does_not_escape_backstop(self):
        v = _call(
            route_tier=PAID,
            rolling_paid_spend_usd=CAP,
            override=_override("price_history"),
        )
        assert v["decision"] == DENY
        assert v["reason_code"] == "backstop_exceeded"

    def test_scope_isolation_infra_down_does_not_rescue_backstop(self):
        # infra_down rescues the stale-feed DENY, but must NOT touch the
        # budget backstop (different scope). The consumption is still
        # reported for audit (§3: every consumed override logs a row) even
        # though the final verdict is denied for another reason.
        v = _call(
            route_tier=PAID,
            rolling_paid_spend_usd=CAP,
            price_unreachable=True,
            override=_override("infra_down"),
        )
        assert v["decision"] == DENY
        assert v["reason_code"] == "backstop_exceeded"
        assert v["override_consumed"]["scope"] == "infra_down"
        assert v["override_consumed"]["would_have_been"] == {
            "decision": DENY, "reason_code": "infra_down",
        }

    def test_consumed_record_carries_provenance(self):
        v = _call(budget_cap_usd=None, override=_override("budget"))
        rec = v["override_consumed"]
        assert rec["scope"] == "budget"
        assert rec["issued_by"] == "felix"
        assert rec["reason"] == "test grant"
        assert rec["expires_ts"] == pytest.approx(NOW + 3600.0)
        assert rec["consumed_at_ts"] == pytest.approx(NOW)
        assert rec["would_have_been"]["decision"] == DENY

    def test_override_not_consumed_when_not_needed(self):
        # An override must only be consumed when it actually changes the
        # verdict (§3: "every gate invocation that CONSUMED an override").
        v = _call(override=_override("budget"))   # budget is fine already
        assert v["decision"] == ALLOW
        assert v["override_consumed"] is None

    def test_backstop_override_still_defers_on_price(self):
        # budget override lifts the backstop DENY only; the percentile gate
        # can still DEFER (deferral is rescheduling, not denial — Q2).
        v = _call(
            route_tier=PAID,
            rolling_paid_spend_usd=CAP,
            effective_price_usd_per_m=EXPENSIVE_PRICE,
            hysteresis_state={"cheap": False, "last_flip_ts": NOW - 7200.0},
            override=_override("budget"),
        )
        assert v["decision"] == DEFER
        assert v["reason_code"] == "price_outside_band"
        assert v["override_consumed"]["scope"] == "budget"


# ── output fields: provenance, headroom, predicted cost ─────────────────────


class TestOutputFields:
    def test_provenance_echoes_sources(self):
        v = _call(price_history=HISTORY_48)
        p = v["provenance"]
        assert p["price_source"] == "v1/pricing?horizon_min=30"
        assert p["history_n"] == 48
        assert p["window_days"] == HISTORY_WINDOW_DAYS
        assert p["price_age_min"] == pytest.approx(0.0)
        assert p["model"] == "glm-5.2"

    def test_headroom_is_cap_minus_spend(self):
        v = _call(rolling_paid_spend_usd=3.0)
        assert v["headroom_usd"] == pytest.approx(12.0)

    def test_headroom_clamped_at_zero_when_overspent(self):
        v = _call(rolling_paid_spend_usd=CAP + 2.0)
        assert v["headroom_usd"] == pytest.approx(0.0)

    def test_predicted_cost_uses_profile_budget_mult(self):
        # coding budget_mult = 1.0, research = 2.5 (TASK_PROFILES).
        v_cod = _call(estimated_tokens=1_000_000)
        assert v_cod["predicted_cost_usd"] == pytest.approx(CHEAP_PRICE * 1.0)
        v_res = _call(task_type="research", model=None, estimated_tokens=1_000_000)
        assert v_res["predicted_cost_usd"] == pytest.approx(
            CHEAP_PRICE * TASK_PROFILES["research"]["budget_mult"]
        )

    def test_predicted_cost_absent_without_token_estimate(self):
        v = _call()
        assert v["predicted_cost_usd"] is None
        assert v["required_headroom_usd"] is None

    def test_reason_json_always_present(self):
        for v in (
            _call(),
            _call(effective_price_usd_per_m=EXPENSIVE_PRICE),
            _call(freeze_marker=True),
            _call(budget_cap_usd=None),
        ):
            assert isinstance(v["reason_json"], dict)
            assert v["reason_code"]

    def test_echoes_task_identity(self):
        v = _call(deferrable=False, route_tier=PAID)
        assert v["deferrable"] is False
        assert v["route_tier"] == PAID
        assert v["task_type"] == "coding"


# ── config/budget.yaml loader ────────────────────────────────────────────────


REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
BUDGET_YAML = os.path.join(REPO_ROOT, "config", "budget.yaml")


class TestBudgetConfig:
    def test_shipped_defaults_load(self):
        cfg = load_budget_config(BUDGET_YAML)
        assert cfg is not None
        assert cfg["daily_cap_usd"] == pytest.approx(15.0)
        assert cfg["warn_at_pct"] == pytest.approx(50.0)
        assert set(cfg["paid_tiers"]) >= {
            "routstrd", "telnyx", "openrouter", "ppq", "deepinfra",
            "ollama_cloud_above_quota",
        }

    def test_unparsable_yaml_returns_none(self, tmp_path):
        bad = tmp_path / "budget.yaml"
        bad.write_text("daily_cap_usd: [unclosed\n  - :{bad")
        assert load_budget_config(str(bad)) is None

    def test_missing_cap_returns_none(self, tmp_path):
        f = tmp_path / "budget.yaml"
        f.write_text("warn_at_pct: 50\n")
        assert load_budget_config(str(f)) is None

    def test_nonpositive_cap_returns_none(self, tmp_path):
        f = tmp_path / "budget.yaml"
        f.write_text("daily_cap_usd: -3\n")
        assert load_budget_config(str(f)) is None

    def test_non_dict_yaml_returns_none(self, tmp_path):
        f = tmp_path / "budget.yaml"
        f.write_text("- just\n- a\n- list\n")
        assert load_budget_config(str(f)) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert load_budget_config(str(tmp_path / "nope.yaml")) is None

    def test_resolve_route_tier_paid(self):
        cfg = load_budget_config(BUDGET_YAML)
        assert resolve_route_tier("routstrd", cfg) == PAID
        assert resolve_route_tier("ollama_cloud_above_quota", cfg) == PAID

    def test_resolve_route_tier_subscription_default(self):
        cfg = load_budget_config(BUDGET_YAML)
        assert resolve_route_tier("zai_ours", cfg) == SUBSCRIPTION
        assert resolve_route_tier("anything-else", cfg) == SUBSCRIPTION
        # None cfg (unconfigured) — the CALLER must DENY budget_unconfigured;
        # tier resolution itself just defaults to subscription.
        assert resolve_route_tier("routstrd", None) == SUBSCRIPTION
