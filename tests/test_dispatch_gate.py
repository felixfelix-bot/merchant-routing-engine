"""Tests for dispatch_gate — Kalman-gated kanban dispatch decision (P5.1).

Implements the two-dimension gate from IMPL-SPEC-kalman-dispatch-gate.md
plus the v2 HARDWARE GATE addition:

  DIMENSION 1: Hardware availability  (binary, checked first)
  DIMENSION 2: Quota sufficiency       (predictive, hardware-scaled margin)
  DIMENSION 3: Price optimization      (informational; scarcity override)

Covers:
  - Task type → model + budget_mult profiles (new: mechanical/research/review/docs)
  - Legacy aliases (coding/reasoning/chat/simple) preserved
  - 2x safety margin (software) + hardware-scaled margins (board=4x, dual=6x, dq05=3x)
  - Flash downgrade path (0.3x budget) before holding
  - Hold when both keys exhausted even at flash
  - Hardware unavailable → HOLD regardless of quota
  - scarcity_override: hardware present + peak → dispatch anyway, flag set
  - scarcity_factor ramp: 1.0 below 50%, 2.0 at 100%
  - burn_rate_pct_per_hour surfaces in response
  - hours_until_exhaustion computation
  - Hardware duration quota impact (concurrent burn added to required headroom)
  - Unhealthy / >=95% keys skipped
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dispatch_gate import (
    TASK_PROFILES,
    HARDWARE_SAFETY_MARGIN,
    FLASH_BUDGET_FACTOR,
    DURATION_MINUTES,
    QUOTA_USED_HOLD_PCT,
    DEFAULT_HARDWARE_REQ,
    normalize_task_type,
    resolve_task_profile,
    evaluate_dispatch,
)


# ── helpers ──────────────────────────────────────────────────────────────────

QUOTA_TOTAL = 2_000_000


def _quota(ours_pct=10.0, friend_pct=10.0, ours_healthy=True, friend_healthy=True):
    """Build a quota dict with healthy keys at the given used-pct."""
    return {
        "ours": {
            "used_pct": ours_pct,
            "remaining": QUOTA_TOTAL * (1.0 - ours_pct / 100.0),
            "healthy": ours_healthy,
        },
        "friend": {
            "used_pct": friend_pct,
            "remaining": QUOTA_TOTAL * (1.0 - friend_pct / 100.0),
            "healthy": friend_healthy,
        },
    }


def _call(task_type="coding", estimated_tokens=200000, hardware_req="none",
          quota=None, burn=None, is_peak=False, peak_mult=1.0,
          converged_rates=None, hardware_state=None, task_subtype=None):
    return evaluate_dispatch(
        estimated_tokens=estimated_tokens,
        task_type=task_type,
        hardware_req=hardware_req,
        task_subtype=task_subtype,
        quota=quota or _quota(),
        burn_rate_pct_per_hour=burn or {"ours": 5.0, "friend": 5.0},
        converged_rates=converged_rates or {"ours": 0.003, "friend": 0.003},
        is_peak=is_peak,
        peak_mult=peak_mult,
        quota_total=QUOTA_TOTAL,
        hardware_state=hardware_state,
    )


# ── task profiles ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "task_type, model, budget_mult",
    [
        ("mechanical", "glm-4.5-flash", 0.25),
        ("coding", "glm-5.2", 1.0),
        ("research", "glm-5.2", 2.5),
        ("review", "glm-5.2", 0.5),
        ("docs", "glm-4.5-flash", 0.5),
    ],
)
def test_new_task_type_profiles(task_type, model, budget_mult):
    """Spec table: each new task type maps to the exact model + budget_mult."""
    prof = resolve_task_profile(task_type)
    assert prof["model"] == model
    assert prof["budget_mult"] == pytest.approx(budget_mult)


@pytest.mark.parametrize(
    "task_type, model, budget_mult",
    [
        ("reasoning", "glm-4.5", 2.0),
        ("chat", "glm-4.5-air", 0.5),
        ("simple", "glm-4.5-flash", 0.25),
    ],
)
def test_legacy_task_type_aliases_preserved(task_type, model, budget_mult):
    """Existing coding/chat/simple/reasoning kept as aliases — same models."""
    prof = resolve_task_profile(task_type)
    assert prof["model"] == model
    assert prof["budget_mult"] == pytest.approx(budget_mult)


def test_unknown_task_type_defaults_to_coding():
    """Unknown/unrecognised task_type falls back to the coding profile."""
    assert normalize_task_type("nonsense") == "coding"
    assert normalize_task_type("") == "coding"
    assert normalize_task_type(None) == "coding"
    prof = resolve_task_profile("bogus")
    assert prof["model"] == "glm-5.2"
    assert prof["budget_mult"] == pytest.approx(1.0)


def test_every_profile_has_model_and_budget_mult():
    """All eight task profiles are complete — no half entries."""
    for name, prof in TASK_PROFILES.items():
        assert "model" in prof and prof["model"], name
        assert "budget_mult" in prof and prof["budget_mult"] > 0, name


# ── 2x safety margin (software) ──────────────────────────────────────────────


def test_dispatches_when_headroom_exceeds_2x_budget():
    """200K coding task → 200K budget → needs >400K headroom. 1.8M remaining OK."""
    r = _call(task_type="coding", estimated_tokens=200000,
              quota=_quota(ours_pct=10.0))  # 1.8M remaining
    assert r["can_dispatch"] is True
    assert r["downgraded"] is False
    assert r["safety_margin"] == pytest.approx(2.0)
    assert r["reason"]


def test_holds_when_headroom_under_2x_budget_without_flash_room():
    """Edge: remaining just below 2x budget AND below flash headroom → hold."""
    # coding 200K → budget 200K → 2x = 400K headroom needed.
    # Set remaining to 80K (4% used → 1.92M... too much). Force scarcity:
    # use research 2.5x: 200K*2.5 = 500K budget → 2x = 1.0M headroom.
    # ours at 50% → 1.0M remaining. friend at 50%. Flash: 500K*0.3=150K → 300K headroom.
    # 1.0M > 300K → flash OK, so it downgrades. To force a HOLD we need flash to fail too.
    # research 500K budget, flash req = 150K*2 = 300K. Set remaining to 200K (90% used).
    r = _call(task_type="research", estimated_tokens=200000,
              quota=_quota(ours_pct=90.0, friend_pct=90.0))
    # 200K remaining; full needs 1.0M; flash needs 300K → both fail → HOLD
    assert r["can_dispatch"] is False
    assert r["recommended_model"] is None
    assert "exhaust" in r["reason"].lower() or "flash" in r["reason"].lower()


def test_safety_margin_reflects_in_task_budget_field():
    """task_budget = estimated_tokens * budget_mult (pre-margin)."""
    r = _call(task_type="research", estimated_tokens=200000)
    assert r["task_budget"] == 500000  # 200K * 2.5


# ── flash downgrade path ─────────────────────────────────────────────────────


def test_flash_downgrade_when_tight_but_flash_fits():
    """Full budget fails 2x margin but flash (0.3x) fits → downgraded=True."""
    # coding 200K → budget 200K → full needs 400K. flash 60K → needs 120K.
    # remaining 250K (87.5% used): 250K < 400K (full fail), 250K > 120K (flash ok)
    r = _call(task_type="coding", estimated_tokens=200000,
              quota=_quota(ours_pct=87.5, friend_pct=87.5))
    assert r["can_dispatch"] is True
    assert r["downgraded"] is True
    assert r["recommended_model"] == "glm-4.5-flash"
    assert "flash" in r["reason"].lower()


def test_flash_budget_factor_is_0p3():
    """Spec: flash model uses ~30% of task budget."""
    assert FLASH_BUDGET_FACTOR == pytest.approx(0.3)


def test_flash_downgrade_uses_margin_too():
    """Flash path applies the SAME safety margin (2x for software)."""
    # flash required = budget*0.3*margin = 200K*0.3*2 = 120K.
    # remaining 100K (< 120K) → flash also fails → HOLD.
    r = _call(task_type="coding", estimated_tokens=200000,
              quota=_quota(ours_pct=95.0, friend_pct=95.0))
    # remaining = 100K. full 400K, flash 120K → both fail
    assert r["can_dispatch"] is False


def test_downgraded_false_when_full_budget_fits():
    """No downgrade when the full-model budget has sufficient headroom."""
    r = _call(task_type="coding", estimated_tokens=200000,
              quota=_quota(ours_pct=10.0))
    assert r["downgraded"] is False


# ── hardware gate margins ────────────────────────────────────────────────────


def test_hardware_safety_margin_table():
    """v2: none=2x, board=4x, dual_board=6x, dq05=3x."""
    assert HARDWARE_SAFETY_MARGIN["none"] == pytest.approx(2.0)
    assert HARDWARE_SAFETY_MARGIN["board"] == pytest.approx(4.0)
    assert HARDWARE_SAFETY_MARGIN["dual_board"] == pytest.approx(6.0)
    assert HARDWARE_SAFETY_MARGIN["dq05"] == pytest.approx(3.0)


def test_board_task_uses_4x_margin():
    """board task needs 4x headroom, not 2x."""
    # coding 200K → budget 200K. board margin 4x → needs 800K.
    # ours at 50% → 1.0M remaining → OK (with board present + free).
    hw = {"board_present": True, "lock_status": "free", "board_id": "F242D"}
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="board",
              hardware_state=hw)
    assert r["can_dispatch"] is True
    assert r["safety_margin"] == pytest.approx(4.0)
    assert r["hardware"]["required"] == "board"
    assert r["hardware"]["available"] is True


def test_board_task_holds_when_2x_ok_but_4x_not():
    """remaining between 2x and 4x → software would pass, board holds/downgrades."""
    # coding 200K → budget 200K. 2x=400K, 4x=800K.
    # remaining 600K (70% used): >400K (2x ok) but <800K (4x fail).
    # flash: 60K → 4x = 240K. 600K > 240K → flash downgrade.
    hw = {"board_present": True, "lock_status": "free", "board_id": "F242D"}
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="board",
              quota=_quota(ours_pct=70.0, friend_pct=70.0), hardware_state=hw)
    assert r["can_dispatch"] is True
    assert r["downgraded"] is True
    assert r["recommended_model"] == "glm-4.5-flash"


def test_dual_board_uses_6x_margin():
    """dual_board needs 6x — the tightest margin."""
    hw = {"board_count": 2, "lock_status": "free",
          "board_present": True, "board_id": "F242D,F242E"}
    r = _call(task_type="coding", estimated_tokens=200000,
              hardware_req="dual_board", hardware_state=hw)
    assert r["safety_margin"] == pytest.approx(6.0)
    assert r["hardware"]["available"] is True


def test_dq05_uses_3x_margin():
    """dq05 (remote) uses 3x margin."""
    hw = {"dq05_reachable": True}
    r = _call(task_type="coding", estimated_tokens=200000,
              hardware_req="dq05", hardware_state=hw)
    assert r["safety_margin"] == pytest.approx(3.0)
    assert r["hardware"]["available"] is True


# ── hardware unavailable → HOLD ──────────────────────────────────────────────


def test_hardware_unavailable_holds_regardless_of_quota():
    """Dimension 1: board required but absent → HOLD even with full quota."""
    hw = {"board_present": False, "lock_status": "unknown"}
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="board",
              quota=_quota(ours_pct=1.0, friend_pct=1.0), hardware_state=hw)
    assert r["can_dispatch"] is False
    assert "hardware" in r["reason"].lower() or "unavailable" in r["reason"].lower()
    assert r["hardware"]["available"] is False


def test_board_present_but_locked_holds():
    """Board present but lock held by another task → unavailable."""
    hw = {"board_present": True, "lock_status": "held", "board_id": "F242D"}
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="board",
              hardware_state=hw)
    assert r["can_dispatch"] is False
    assert r["hardware"]["available"] is False


def test_dq05_unreachable_holds():
    hw = {"dq05_reachable": False}
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="dq05",
              hardware_state=hw)
    assert r["can_dispatch"] is False
    assert r["hardware"]["available"] is False


def test_no_hardware_required_always_available():
    """hardware_req=none → available True, no hold on hardware grounds."""
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="none")
    assert r["hardware"]["available"] is True
    assert r["hardware"]["required"] == "none"


def test_default_hardware_req_is_none():
    assert DEFAULT_HARDWARE_REQ == "none"


# ── scarcity override (Dimension 3) ──────────────────────────────────────────


def test_scarcity_override_when_hardware_present_and_peak():
    """Board in hand + peak hours → dispatch anyway, scarcity_override=True."""
    hw = {"board_present": True, "lock_status": "free", "board_id": "F242D"}
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="board",
              is_peak=True, peak_mult=3.0, hardware_state=hw)
    assert r["can_dispatch"] is True
    assert r["scarcity_override"] is True


def test_no_scarcity_override_when_no_hardware():
    """Software task during peak → no override."""
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="none",
              is_peak=True, peak_mult=3.0)
    assert r["scarcity_override"] is False


def test_no_scarcity_override_when_off_peak():
    """Hardware present but off-peak → no override needed."""
    hw = {"board_present": True, "lock_status": "free", "board_id": "F242D"}
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="board",
              is_peak=False, peak_mult=1.0, hardware_state=hw)
    assert r["scarcity_override"] is False


def test_no_scarcity_override_when_hardware_unavailable():
    """Hardware required but absent → override stays False (can't dispatch)."""
    hw = {"board_present": False, "lock_status": "unknown"}
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="board",
              is_peak=True, peak_mult=3.0, hardware_state=hw)
    assert r["scarcity_override"] is False
    assert r["can_dispatch"] is False


# ── scarcity_factor ramp ─────────────────────────────────────────────────────


def test_scarcity_factor_1_below_50pct():
    r = _call(quota=_quota(ours_pct=30.0, friend_pct=20.0))
    assert r["scarcity_factor"] == pytest.approx(1.0)


def test_scarcity_factor_ramps_to_2_at_100pct():
    r = _call(quota=_quota(ours_pct=100.0, friend_pct=100.0),
              estimated_tokens=1)  # tiny budget so it still tries
    assert r["scarcity_factor"] == pytest.approx(2.0)


def test_scarcity_factor_uses_max_of_both_keys():
    """scarcity keyed on the WORST (most-used) key."""
    r = _call(quota=_quota(ours_pct=75.0, friend_pct=10.0))
    # 75% → 1 + (75-50)/50 = 1.5
    assert r["scarcity_factor"] == pytest.approx(1.5)


def test_scarcity_factor_midpoint():
    r = _call(quota=_quota(ours_pct=60.0, friend_pct=60.0))
    # 1 + (60-50)/50 = 1.2
    assert r["scarcity_factor"] == pytest.approx(1.2)


# ── burn_rate_pct_per_hour + hours_until_exhaustion ──────────────────────────


def test_burn_rate_surfaces_in_response():
    r = _call(burn={"ours": 9.2, "friend": 6.1})
    assert r["burn_rate_pct_per_hour"]["ours"] == pytest.approx(9.2)
    assert r["burn_rate_pct_per_hour"]["friend"] == pytest.approx(6.1)


def test_hours_until_exhaustion_computation():
    """hours = (100 - used_pct) / burn_rate."""
    r = _call(quota=_quota(ours_pct=45.0, friend_pct=30.0),
              burn={"ours": 11.0, "friend": 10.0})
    # ours: (100-45)/11 = 5.0; friend: (100-30)/10 = 7.0
    assert r["hours_until_exhaustion"]["ours"] == pytest.approx(5.0)
    assert r["hours_until_exhaustion"]["friend"] == pytest.approx(7.0)


def test_hours_until_exhaustion_none_when_no_burn_data():
    """Zero/missing burn rate → None (can't predict)."""
    r = _call(burn={"ours": 0.0, "friend": 0.0})
    assert r["hours_until_exhaustion"]["ours"] is None
    assert r["hours_until_exhaustion"]["friend"] is None


def test_quota_used_pct_surfaces_in_response():
    r = _call(quota=_quota(ours_pct=45.3, friend_pct=30.1))
    assert r["quota_used_pct"]["ours"] == pytest.approx(45.3)
    assert r["quota_used_pct"]["friend"] == pytest.approx(30.1)


# ── price calculation ────────────────────────────────────────────────────────


def test_effective_price_applies_peak_and_scarcity():
    """effective = base * peak_mult * scarcity, floored at min."""
    r = _call(converged_rates={"ours": 0.003, "friend": 0.003},
              is_peak=True, peak_mult=3.0,
              quota=_quota(ours_pct=10.0, friend_pct=10.0))
    # scarcity 1.0 (10% used) → 0.003 * 3.0 * 1.0 = 0.009
    assert r["effective_price_per_m"] == pytest.approx(0.009)


def test_predicted_cost_uses_task_budget():
    """predicted_cost = effective_price * task_budget / 1e6."""
    r = _call(task_type="coding", estimated_tokens=200000,
              converged_rates={"ours": 0.003, "friend": 0.003},
              quota=_quota(ours_pct=10.0))
    # budget 200K, price 0.003 → 0.003 * 200000 / 1e6 = 0.0006
    assert r["predicted_cost"] == pytest.approx(0.0006)


def test_effective_price_floored_at_min():
    """Free-ish provider (base 0) still returns >= MIN_EFFECTIVE_PRICE."""
    r = _call(converged_rates={"ours": 0.0, "friend": 0.0},
              quota=_quota(ours_pct=10.0))
    assert r["effective_price_per_m"] >= 0.001


def test_is_peak_hour_and_peak_multiplier_in_response():
    r = _call(is_peak=True, peak_mult=3.0)
    assert r["is_peak_hour"] is True
    assert r["peak_multiplier"] == pytest.approx(3.0)
    r2 = _call(is_peak=False, peak_mult=1.0)
    assert r2["is_peak_hour"] is False
    assert r2["peak_multiplier"] == pytest.approx(1.0)


# ── key health / exhaustion edge cases ───────────────────────────────────────


def test_unhealthy_key_skipped():
    """An unhealthy key is never a candidate even with headroom."""
    r = _call(quota=_quota(ours_pct=99.0, ours_healthy=False, friend_pct=10.0))
    # ours unhealthy → only friend considered; friend healthy & headroom → dispatch
    assert r["can_dispatch"] is True


def test_key_at_95pct_skipped():
    """A key at/above the hold threshold (95%) is not a candidate."""
    # Both at 95%: remaining 100K. coding full needs 400K, flash 120K → HOLD.
    r = _call(task_type="coding", estimated_tokens=200000,
              quota=_quota(ours_pct=95.0, friend_pct=95.0))
    assert r["can_dispatch"] is False


def test_one_key_exhausted_other_ok_still_dispatches():
    r = _call(quota=_quota(ours_pct=99.0, friend_pct=10.0))
    assert r["can_dispatch"] is True
    assert r["downgraded"] is False


def test_quota_hold_threshold_constant():
    assert QUOTA_USED_HOLD_PCT == 95


# ── hardware duration quota impact ───────────────────────────────────────────


def test_duration_minutes_table():
    """v2: flash=10, capture=20, throughput=15, handshake=60."""
    assert DURATION_MINUTES["flash"] == 10
    assert DURATION_MINUTES["capture"] == 20
    assert DURATION_MINUTES["throughput"] == 15
    assert DURATION_MINUTES["handshake"] == 60


def test_hardware_duration_adds_concurrent_burn_to_headroom():
    """Hardware tasks need extra headroom for quota burned DURING execution.

    With board present + free, a flash subtype task at burn 10%/hr for 10 min
    adds (10/100)*2M*(10/60) ≈ 33,333 tokens to required headroom. Verify the
    gate is STRICTER than the pure 4x margin by comparing a software call.
    """
    hw = {"board_present": True, "lock_status": "free", "board_id": "F242D"}
    # coding 200K → budget 200K. board 4x = 800K. concurrent burn extra ~33K.
    # Set remaining to 805K (59.75% used) → just above 800K pure margin but
    # BELOW 800K + 33K = 833K with concurrent burn → must downgrade/hold.
    remaining = 805_000
    used = (1 - remaining / QUOTA_TOTAL) * 100  # ~59.75
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="board",
              task_subtype="flash", burn={"ours": 10.0, "friend": 10.0},
              quota=_quota(ours_pct=used, friend_pct=used), hardware_state=hw)
    # 805K < 833K required → full budget fails. flash: 60K*4=240K+33K=273K,
    # 805K > 273K → flash downgrade.
    assert r["downgraded"] is True


def test_software_task_has_no_concurrent_burn():
    """hardware_req=none → no concurrent-burn headroom added."""
    r = _call(task_type="coding", estimated_tokens=200000, hardware_req="none",
              task_subtype="flash", burn={"ours": 10.0, "friend": 10.0})
    # pure 2x margin only; concurrent burn must not apply to software
    assert r["can_dispatch"] is True


# ── response shape completeness ──────────────────────────────────────────────


def test_response_has_all_spec_fields():
    """Every field from impl-spec v1 + v2 is present in the response."""
    r = _call()
    required = [
        "can_dispatch", "reason", "recommended_model",
        "effective_price_per_m", "predicted_cost",
        "hours_until_exhaustion", "quota_used_pct", "burn_rate_pct_per_hour",
        "is_peak_hour", "peak_multiplier", "scarcity_factor", "downgraded",
        "scarcity_override", "hardware",
    ]
    for field in required:
        assert field in r, f"missing field: {field}"


def test_hardware_object_shape():
    hw = {"board_present": True, "lock_status": "free", "board_id": "F242D",
          "queue_depth": 0, "estimated_wait_minutes": 0}
    r = _call(hardware_req="board", hardware_state=hw)
    h = r["hardware"]
    for field in ("required", "available", "board_present", "board_id",
                  "lock_status", "queue_depth", "estimated_wait_minutes"):
        assert field in h, f"missing hardware field: {field}"


def test_pure_function_no_side_effects():
    """evaluate_dispatch must not mutate its inputs (pure decision)."""
    q = _quota()
    q_copy = {k: dict(v) for k, v in q.items()}
    burn = {"ours": 5.0, "friend": 5.0}
    burn_copy = dict(burn)
    evaluate_dispatch(
        estimated_tokens=200000, task_type="coding", hardware_req="board",
        task_subtype="flash", quota=q, burn_rate_pct_per_hour=burn,
        converged_rates={"ours": 0.003}, is_peak=True, peak_mult=3.0,
        quota_total=QUOTA_TOTAL,
        hardware_state={"board_present": True, "lock_status": "free"},
    )
    assert q == q_copy
    assert burn == burn_copy


def test_friend_key_used_when_ours_exhausted():
    """Prefers the healthy key with headroom; falls back to friend."""
    r = _call(quota=_quota(ours_pct=99.0, friend_pct=10.0))
    assert r["can_dispatch"] is True


# ── determinism ──────────────────────────────────────────────────────────────


def test_deterministic_same_inputs_same_output():
    args = dict(
        estimated_tokens=200000, task_type="research", hardware_req="board",
        task_subtype="capture", quota=_quota(ours_pct=40.0, friend_pct=55.0),
        burn_rate_pct_per_hour={"ours": 7.0, "friend": 4.0},
        converged_rates={"ours": 0.003, "friend": 0.002}, is_peak=True,
        peak_mult=3.0, quota_total=QUOTA_TOTAL,
        hardware_state={"board_present": True, "lock_status": "free",
                        "board_id": "F242D"},
    )
    r1 = evaluate_dispatch(**args)
    r2 = evaluate_dispatch(**args)
    assert r1 == r2
