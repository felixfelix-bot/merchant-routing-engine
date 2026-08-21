"""cost_gate.py — percentile cost gate (CG-1, plan v2 §5).

Pure, side-effect-free cost-gate decision function implementing the
percentile gating Felix chose (Q1: "run the cron jobs when we are in the
lower 20% of our average cost") with the §5 fail-closed matrix, §2.2–§2.5
gate mechanics and §3 override semantics from
``docs/PLAN-cost-gate-reform-v2-2026-08-21.md``.

Like :mod:`src.dispatch_gate`, this module does NO I/O and holds NO global
state: every signal (effective price, price history, budget spend, override
grant, freeze/dead-key markers) is passed in as an argument, so the decision
is fully deterministic and unit-testable.  The one I/O-adjacent helper,
:func:`load_budget_config`, is a standalone parser used by the CG-7 CLI and
is never called from :func:`evaluate_cost_gate`.

Decision semantics (Q2)::

    ALLOW  cheap band, or non-deferrable work under the backstop
    DEFER  deferrable work outside the band — skip and reschedule;
           NO model downgrade, NO auto-substitution
    DENY   hard fail-closed rows of the §5 matrix

§5 fail-closed matrix, in evaluation order (DENY rows outrank the DEFER row
so a misconfiguration surfaces loudly instead of rescheduling into itself):

    1. freeze marker present            → DENY freeze_marker (override-immune)
    2. dead/locked z.ai key (zai path)  → DENY dead_or_locked_key (override-immune)
    3. price unreachable / stale >15m   → DENY infra_down (loud; escape =
                                          override scope ``infra_down`` — Q10)
    4. effective price unknown          → DENY price_unknown
    5. budget config missing/unparsable → DENY budget_unconfigured
    6. paid-tier backstop exceeded      → DENY for PAID tiers only;
                                          subscription routes are freed (§2.5)
    7. <48 h price history, deferrable  → DEFER price_history_insufficient
    8. outside p20 band (deferrable)    → DEFER price_outside_band

Composition (CG-1 scope note: "composes src/dispatch_gate.py (TASK_PROFILES,
margins) — no duplication"): task identity and the safety-margin table are
imported from :mod:`src.dispatch_gate` — the same objects, never copies.
``resolve_task_profile`` supplies the model and ``budget_mult``;
``HARDWARE_SAFETY_MARGIN`` scales the informational
``required_headroom_usd`` exactly as the dispatch gate scales quota headroom.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import yaml

from src.dispatch_gate import (
    DEFAULT_HARDWARE_REQ,
    HARDWARE_SAFETY_MARGIN,
    MIN_EFFECTIVE_PRICE,
    TASK_PROFILES,
    normalize_task_type,
    resolve_task_profile,
)

__all__ = [
    "ALLOW",
    "DEFER",
    "DENY",
    "SUBSCRIPTION",
    "PAID",
    "PERCENTILE",
    "EXIT_BAND_MULTIPLIER",
    "DWELL_SECONDS",
    "MIN_HISTORY_SAMPLES",
    "HISTORY_WINDOW_DAYS",
    "PRICE_STALE_MAX_MIN",
    "DEFAULT_DAILY_CAP_USD",
    "BACKSTOP_WARN_PCT",
    "OVERRIDE_SCOPES",
    # re-exported from dispatch_gate (composition, no duplication)
    "TASK_PROFILES",
    "HARDWARE_SAFETY_MARGIN",
    "MIN_EFFECTIVE_PRICE",
    # functions
    "percentile",
    "percentile_rank",
    "is_override_active",
    "load_budget_config",
    "resolve_route_tier",
    "evaluate_cost_gate",
]


# ── constants (plan §2, §3, §5) ──────────────────────────────────────────────

ALLOW: str = "ALLOW"
DEFER: str = "DEFER"
DENY: str = "DENY"

#: Route tiers for the §2.5 backstop.  ``subscription`` covers z.ai keys and
#: ollama_cloud included quota; ``paid`` covers pay-per-token tiers
#: (routstrd, telnyx, openrouter, ppq, deepinfra, ollama above quota).
SUBSCRIPTION: str = "subscription"
PAID: str = "paid"

#: §2.2 — gate percentile: ALLOW deferrable work iff current effective price
#: sits in the cheapest 20% of the trailing-week hourly medians.
PERCENTILE: float = 20.0

#: §2.3 — hysteresis: once CHEAP, stays CHEAP until price > p20 × 1.20.
EXIT_BAND_MULTIPLIER: float = 1.20

#: §2.3 — minimum dwell between state flips (30 minutes), seconds.
DWELL_SECONDS: float = 30.0 * 60.0

#: §2.4 — cold start: fewer than 48 hourly samples → deferrable work DEFERs.
MIN_HISTORY_SAMPLES: int = 48

#: §2.2 — trailing window of hourly median observations, days.
HISTORY_WINDOW_DAYS: int = 7

#: §5 matrix — a price observation older than this is stale (minutes).
PRICE_STALE_MAX_MIN: float = 15.0

#: §2.5 — budget backstop defaults (mirrors ``config/budget.yaml``).
DEFAULT_DAILY_CAP_USD: float = 15.0
BACKSTOP_WARN_PCT: float = 50.0

#: §3 — override scopes.  ``paid_ceiling`` belongs to CG-6's static $/M
#: ceiling; this module never consumes it (scope isolation).
OVERRIDE_SCOPES: frozenset[str] = frozenset(
    {"budget", "price_history", "infra_down", "paid_ceiling"}
)

#: reason_code used when an override rescue is what produced the ALLOW,
#: keyed by the reason_code that WOULD have been returned.
_OVERRIDE_REASON_CODE: dict[str, str] = {
    "infra_down": "infra_down_override",
    "budget_unconfigured": "budget_unconfigured_override",
    "backstop_exceeded": "backstop_override",
    "price_history_insufficient": "price_history_override",
}


# ── pure statistics helpers ─────────────────────────────────────────────────


def percentile(values: Sequence[float], pct: float) -> float:
    """``pct``-th percentile with linear interpolation (numpy default).

    >>> percentile([float(i) for i in range(1, 101)], 20)
    20.8
    >>> percentile([5.0], 37.5)
    5.0
    """
    xs = sorted(float(v) for v in values)
    if not xs:
        raise ValueError("percentile of empty sequence")
    if len(xs) == 1:
        return xs[0]
    rank = (pct / 100.0) * (len(xs) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    frac = rank - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def percentile_rank(values: Sequence[float], value: float) -> float:
    """Percentile rank of ``value``: share of samples ≤ value, ×100.

    >>> percentile_rank([float(i) for i in range(1, 101)], 22.0)
    22.0
    """
    xs = [float(v) for v in values]
    if not xs:
        raise ValueError("percentile_rank of empty sequence")
    return 100.0 * sum(1.0 for v in xs if v <= value) / len(xs)


# ── §3 override handling ────────────────────────────────────────────────────


def is_override_active(override: Mapping[str, Any] | None, now_ts: float) -> bool:
    """True iff the grant is well-formed, unexpired and single-scoped.

    TTL is mandatory (§3): a grant without a numeric ``expires_ts`` strictly
    greater than ``now_ts`` is invalid, as is an unknown scope.
    """
    if not isinstance(override, Mapping):
        return False
    scope = override.get("scope")
    if scope not in OVERRIDE_SCOPES:
        return False
    expires_ts = override.get("expires_ts")
    if not isinstance(expires_ts, (int, float)) or isinstance(expires_ts, bool):
        return False
    return float(expires_ts) > float(now_ts)


def _consume_override(
    override: Mapping[str, Any],
    now_ts: float,
    would_decision: str,
    would_reason_code: str,
) -> dict[str, Any]:
    """Audit record for a consumed override (CG-4 persists this)."""
    return {
        "scope": override.get("scope"),
        "issued_by": override.get("issued_by"),
        "reason": override.get("reason"),
        "expires_ts": override.get("expires_ts"),
        "consumed_at_ts": now_ts,
        "would_have_been": {
            "decision": would_decision,
            "reason_code": would_reason_code,
        },
    }


# ── config/budget.yaml ──────────────────────────────────────────────────────


def load_budget_config(path: str) -> dict[str, Any] | None:
    """Parse ``config/budget.yaml`` (§2.5 backstop defaults).

    Returns ``{"daily_cap_usd", "warn_at_pct", "paid_tiers",
    "subscription_routes_never_blocked"}`` or ``None`` when the file is
    missing, unparsable or lacks a positive ``daily_cap_usd`` — ``None``
    feeds :func:`evaluate_cost_gate` as ``budget_cap_usd=None`` →
    ``DENY budget_unconfigured`` (fail-closed, §5 matrix).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    cap = raw.get("daily_cap_usd")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        return None
    warn = raw.get("warn_at_pct", BACKSTOP_WARN_PCT)
    if isinstance(warn, bool) or not isinstance(warn, (int, float)) \
            or not 0.0 < float(warn) <= 100.0:
        return None
    paid = raw.get("paid_tiers", [])
    if not isinstance(paid, list) or not all(isinstance(t, str) for t in paid):
        return None
    subs = raw.get("subscription_routes_never_blocked", [])
    if not isinstance(subs, list) or not all(isinstance(t, str) for t in subs):
        return None
    return {
        "daily_cap_usd": float(cap),
        "warn_at_pct": float(warn),
        "paid_tiers": list(paid),
        "subscription_routes_never_blocked": list(subs),
    }


def resolve_route_tier(provider: str, budget_cfg: Mapping[str, Any] | None) -> str:
    """Classify a provider against the budget config's paid-tier list.

    Unknown providers default to :data:`SUBSCRIPTION`; an unconfigured
    budget (``None``) is the caller's ``budget_unconfigured`` DENY, not a
    tier judgement.
    """
    if budget_cfg and provider in budget_cfg.get("paid_tiers", []):
        return PAID
    return SUBSCRIPTION


# ── §2.3 hysteresis state machine ───────────────────────────────────────────


def _normalize_hysteresis(state: Mapping[str, Any] | None) -> dict[str, Any]:
    st = dict(state) if isinstance(state, Mapping) else {}
    cheap = bool(st.get("cheap", False))
    last_flip = st.get("last_flip_ts")
    if not isinstance(last_flip, (int, float)) or isinstance(last_flip, bool):
        last_flip = None
    return {"cheap": cheap, "last_flip_ts": last_flip}


def _advance_hysteresis(
    price: float,
    p20: float,
    state: Mapping[str, Any] | None,
    now_ts: float,
) -> dict[str, Any]:
    """Advance the CHEAP state machine (§2.3).

    - Enter CHEAP: ``price ≤ p20``
    - Exit CHEAP:  ``price > p20 × 1.20`` (20% exit band)
    - No flip until ``DWELL_SECONDS`` have elapsed since the last flip
      (a state with no prior flip may flip immediately).
    """
    h = _normalize_hysteresis(state)
    cheap, last_flip = h["cheap"], h["last_flip_ts"]
    dwell_ok = last_flip is None or (now_ts - last_flip) >= DWELL_SECONDS
    exit_threshold = p20 * EXIT_BAND_MULTIPLIER

    if dwell_ok:
        if not cheap and price <= p20:
            cheap, last_flip = True, now_ts
        elif cheap and price > exit_threshold:
            cheap, last_flip = False, now_ts

    remaining = 0.0
    if last_flip is not None:
        remaining = max(0.0, DWELL_SECONDS - (now_ts - last_flip))
    return {
        "cheap": cheap,
        "last_flip_ts": last_flip,
        "dwell_remaining_s": remaining,
    }


# ── main decision (§5) ──────────────────────────────────────────────────────


def evaluate_cost_gate(
    *,
    model: str | None = None,
    task_type: str | None = None,
    deferrable: bool = True,
    route_tier: str = SUBSCRIPTION,
    effective_price_usd_per_m: float | None = None,
    price_source: str = "unknown",
    price_age_min: float = 0.0,
    price_unreachable: bool = False,
    price_history: Sequence[float] | None = None,
    history_window_days: int = HISTORY_WINDOW_DAYS,
    rolling_paid_spend_usd: float = 0.0,
    budget_cap_usd: float | None = None,
    override: Mapping[str, Any] | None = None,
    freeze_marker: bool = False,
    zai_key_dead_or_locked: bool = False,
    estimated_tokens: int | None = None,
    hardware_req: str = DEFAULT_HARDWARE_REQ,
    hysteresis_state: Mapping[str, Any] | None = None,
    now_ts: float = 0.0,
) -> dict[str, Any]:
    """Percentile cost-gate verdict — pure and deterministic.

    Parameters
    ----------
    model / task_type:
        Task identity.  ``task_type`` resolves via dispatch_gate's
        :data:`TASK_PROFILES` (unknown → coding); an explicit ``model``
        wins over the profile's model.
    deferrable:
        Q2 semantics — only deferrable work can be DEFERred; interactive /
        urgent work is gated by the backstop alone.
    route_tier:
        :data:`SUBSCRIPTION` (z.ai keys, ollama included quota — never
        blocked by the backstop) or :data:`PAID` (per-token tiers).
    effective_price_usd_per_m:
        Cheapest ELIGIBLE provider's price for the model (§2.1), preferably
        the CG-2 forecast variant (``/v1/pricing?horizon_min=``).  ``None``
        → ``DENY price_unknown``.
    price_age_min / price_unreachable:
        Price-feed health.  Stale (>15 min) or unreachable → ``DENY
        infra_down`` unless an ``infra_down`` override is active (Q10).
    price_history:
        Trailing-window hourly median observations for the model (§2.2).
    rolling_paid_spend_usd / budget_cap_usd:
        §2.5 backstop inputs (CG-6/CG-7 daily-spend reader).
        ``budget_cap_usd=None`` → ``DENY budget_unconfigured``.
    override:
        §3 grant from ``.cost_gate_override`` (CG-4); single scope, TTL
        mandatory.  Consumed only when it actually rescues a block.
    freeze_marker / zai_key_dead_or_locked:
        Hard blocks — no override scope applies (§3).
    estimated_tokens / hardware_req:
        Optional cost preview: ``predicted_cost_usd`` = price × tokens ×
        profile ``budget_mult``; ``required_headroom_usd`` scales it by
        dispatch_gate's hardware safety margin (informational, mirroring
        the quota-margin concept — same table, no duplication).
    hysteresis_state:
        Previous ``hysteresis`` block from the last verdict (§2.3), or
        ``None`` on first evaluation.
    now_ts:
        Evaluation timestamp (unix seconds) for dwell/TTL arithmetic.

    Returns
    -------
    dict
        ``{decision, reason_code, reason_json, override_consumed,
        percentile_rank, threshold_p20, exit_threshold_p20, headroom_usd,
        predicted_cost_usd, required_headroom_usd, backstop, hysteresis,
        provenance, verdict_snapshot, ...}``.  The verdict is a snapshot at
        dispatch time: an ALLOW covers the job's entire run (§2.3 job-burst
        stickiness) — callers must NOT re-evaluate mid-job.
    """
    profile = resolve_task_profile(task_type)
    resolved_model = model if model is not None else profile["model"]
    canonical_type = normalize_task_type(task_type)
    history = [float(v) for v in (price_history or [])]
    history_n = len(history)
    price = (
        float(effective_price_usd_per_m)
        if effective_price_usd_per_m is not None
        else None
    )
    spend = float(rolling_paid_spend_usd)

    consumed: dict[str, Any] | None = None
    skip_price_gate = False  # an infra_down override rescued the feed

    hyst = _normalize_hysteresis(hysteresis_state)

    def _verdict(
        decision: str,
        reason_code: str,
        reason_json: dict[str, Any] | None = None,
        threshold_p20: float | None = None,
        exit_threshold: float | None = None,
        rank: float | None = None,
        hysteresis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cap = float(budget_cap_usd) if budget_cap_usd is not None else None
        headroom = max(0.0, cap - spend) if cap is not None else None
        predicted = None
        required = None
        if price is not None and estimated_tokens is not None:
            predicted = (
                price * float(estimated_tokens) * float(profile["budget_mult"])
                / 1_000_000.0
            )
            margin = HARDWARE_SAFETY_MARGIN.get(
                hardware_req, HARDWARE_SAFETY_MARGIN[DEFAULT_HARDWARE_REQ]
            )
            required = predicted * float(margin)
        return {
            "decision": decision,
            "reason_code": reason_code,
            "reason_json": reason_json or {},
            "model": resolved_model,
            "task_type": canonical_type,
            "deferrable": bool(deferrable),
            "route_tier": route_tier,
            "percentile_rank": rank,
            "threshold_p20": threshold_p20,
            "exit_threshold_p20": exit_threshold,
            "headroom_usd": headroom,
            "predicted_cost_usd": predicted,
            "required_headroom_usd": required,
            "backstop": {
                "rolling_paid_spend_usd": spend,
                "budget_cap_usd": cap,
                "warn": cap is not None
                and spend >= cap * (BACKSTOP_WARN_PCT / 100.0),
                "paid_blocked": cap is not None and spend >= cap,
            },
            "hysteresis": hysteresis or hyst,
            "provenance": {
                "price_source": price_source,
                "history_n": history_n,
                "window_days": history_window_days,
                "price_age_min": price_age_min,
                "price_unreachable": price_unreachable,
                "model": resolved_model,
            },
            "override_consumed": consumed,
            "verdict_snapshot": {
                "covers_job_run": True,
                "evaluated_at_ts": now_ts,
            },
        }

    # ── Row 1: freeze marker (hard; overrides never apply) ──────────────────
    if freeze_marker:
        return _verdict(
            DENY, "freeze_marker",
            {"hard_block": True, "override_immune": True},
        )

    # ── Row 2: dead/locked z.ai key on the z.ai path (hard; immune) ────────
    if zai_key_dead_or_locked:
        return _verdict(
            DENY, "dead_or_locked_key",
            {"hard_block": True, "override_immune": True},
        )

    active_override = is_override_active(override, now_ts)
    ov: Mapping[str, Any] | None = override if active_override else None

    # ── Row 3: price feed unreachable / stale >15 min (Q10 strict) ─────────
    if price_unreachable or float(price_age_min) > PRICE_STALE_MAX_MIN:
        if ov is not None and ov.get("scope") == "infra_down":
            consumed = _consume_override(ov, now_ts, DENY, "infra_down")
            skip_price_gate = True  # price gating impossible; override said go
        else:
            return _verdict(
                DENY, "infra_down",
                {
                    "loud": True,  # caller (CG-7 CLI) must log this loudly
                    "price_unreachable": bool(price_unreachable),
                    "price_age_min": float(price_age_min),
                    "stale_max_min": PRICE_STALE_MAX_MIN,
                },
            )

    # ── Row 4: effective price unknown ─────────────────────────────────────
    if price is None and not skip_price_gate:
        return _verdict(
            DENY, "price_unknown",
            {"price_source": price_source, "fail_closed": True},
        )

    # ── Row 5: budget config missing/unparsable (before the history DEFER —
    #    DENY must surface the misconfig rather than reschedule into it) ────
    if budget_cap_usd is None:
        if ov is not None and ov.get("scope") == "budget":
            consumed = _consume_override(
                ov, now_ts, DENY, "budget_unconfigured")
        else:
            return _verdict(
                DENY, "budget_unconfigured",
                {"fail_closed": True, "hint": "config/budget.yaml"},
            )

    # ── Row 6: paid-tier backstop (§2.5) — DENY paid only ──────────────────
    cap = float(budget_cap_usd) if budget_cap_usd is not None else None
    if cap is not None and spend >= cap and route_tier == PAID:
        if ov is not None and ov.get("scope") == "budget":
            consumed = _consume_override(
                ov, now_ts, DENY, "backstop_exceeded")
        else:
            return _verdict(
                DENY, "backstop_exceeded",
                {
                    "rolling_paid_spend_usd": spend,
                    "budget_cap_usd": cap,
                    "scope": "paid_tiers_only",
                },
            )

    # ── Rows 7/8: percentile gate — deferrable work with a known price ────
    # (price None with skip_price_gate=False returned price_unknown above)
    if price is not None and deferrable and not skip_price_gate:
        if history_n < MIN_HISTORY_SAMPLES:
            if ov is not None and ov.get("scope") == "price_history":
                consumed = _consume_override(
                    ov, now_ts, DEFER, "price_history_insufficient")
            else:
                return _verdict(
                    DEFER, "price_history_insufficient",
                    {
                        "history_n": history_n,
                        "required_n": MIN_HISTORY_SAMPLES,
                        "window_days": history_window_days,
                        "fail_closed": True,  # §2.4 cold-start posture
                    },
                )
        else:
            p20 = percentile(history, PERCENTILE)
            exit_threshold = p20 * EXIT_BAND_MULTIPLIER
            rank = percentile_rank(history, price)
            hyst = _advance_hysteresis(price, p20, hysteresis_state, now_ts)
            if hyst["cheap"]:
                band_code = (
                    "within_p20_band"
                    if price <= p20
                    else "within_exit_band_hysteresis"
                )
                if consumed is not None:
                    # the override rescue is why this job runs — attribute it
                    code = _OVERRIDE_REASON_CODE.get(
                        consumed["would_have_been"]["reason_code"],
                        "override_applied",
                    )
                    return _verdict(
                        ALLOW, code,
                        {
                            "override_rescued": True,
                            "band": band_code,
                            "price": price, "threshold_p20": p20,
                            "exit_threshold": exit_threshold,
                        },
                        threshold_p20=p20,
                        exit_threshold=exit_threshold,
                        rank=rank,
                        hysteresis=hyst,
                    )
                return _verdict(
                    ALLOW, band_code,
                    {
                        "price": price, "threshold_p20": p20,
                        "exit_threshold": exit_threshold,
                    },
                    threshold_p20=p20,
                    exit_threshold=exit_threshold,
                    rank=rank,
                    hysteresis=hyst,
                )
            return _verdict(
                DEFER, "price_outside_band",
                {
                    "price": price, "threshold_p20": p20,
                    "exit_threshold": exit_threshold,
                    "defer_semantics": "skip-and-reschedule (Q2: no downgrade)",
                },
                threshold_p20=p20,
                exit_threshold=exit_threshold,
                rank=rank,
                hysteresis=hyst,
            )

    # ── ALLOW: non-deferrable under backstop, infra_down-override pass, or
    #    an override rescued a DENY row above.  Informational p20/rank are
    #    still reported when a price and history exist (visibility only —
    #    non-deferrable work is never gated on them). ─────────────────────
    info_p20 = info_exit = info_rank = None
    if price is not None and history_n > 0:
        info_p20 = percentile(history, PERCENTILE)
        info_exit = info_p20 * EXIT_BAND_MULTIPLIER
        info_rank = percentile_rank(history, price)
    if consumed is not None:
        code = _OVERRIDE_REASON_CODE.get(
            consumed["would_have_been"]["reason_code"], "override_applied")
        return _verdict(
            ALLOW, code,
            {"override_rescued": True, "band_check": "skipped"},
            threshold_p20=info_p20,
            exit_threshold=info_exit,
            rank=info_rank,
        )
    return _verdict(
        ALLOW, "not_deferrable_backstop_only",
        {
            "band_check": "skipped",
            "deferrable": False,
            "price": price,
        },
        threshold_p20=info_p20,
        exit_threshold=info_exit,
        rank=info_rank,
    )
