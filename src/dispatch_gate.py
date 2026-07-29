"""dispatch_gate.py — Kalman-gated kanban dispatch decision (P5.1).

Pure, side-effect-free decision function for the ``/v1/dispatch_gate`` proxy
endpoint.  Implements the three-dimension gate from
``docs/IMPL-SPEC-kalman-dispatch-gate.md`` (v1 quota gate + v2 hardware gate):

    DIMENSION 1 — Hardware availability (binary, checked first)
        Board present?  DQ05 reachable?  Lock free?
        → NO  : HOLD, re-check next tick.
        → YES : escalate, relax the price gate (scarcity override).

    DIMENSION 2 — Quota sufficiency (predictive, hardware-scaled margin)
        Will either key exhaust within the task budget?
        Safety margin scales with hardware blast radius:
        none=2x, board=4x, dual_board=6x, dq05=3x.
        → enough headroom : proceed.
        → tight           : try flash downgrade (0.3x) before holding.

    DIMENSION 3 — Price optimization (informational)
        Peak-hour 3x multiplier reported; never blocks on its own.
        When hardware is present during peak, ``scarcity_override=True``
        ("a board in hand beats waiting").

This module does NO I/O and holds NO global state — every signal (quota
snapshot, burn-rate predictions, hardware probe result) is passed in as an
argument so the decision is fully deterministic and unit-testable.  The proxy
endpoint (``zai_proxy.py``) gathers live state and calls :func:`evaluate_dispatch`.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "TASK_PROFILES",
    "HARDWARE_SAFETY_MARGIN",
    "FLASH_BUDGET_FACTOR",
    "DURATION_MINUTES",
    "QUOTA_USED_HOLD_PCT",
    "DEFAULT_HARDWARE_REQ",
    "DEFAULT_TASK_TYPE",
    "MIN_EFFECTIVE_PRICE",
    "DEFAULT_QUOTA_TOTAL",
    "normalize_task_type",
    "resolve_task_profile",
    "build_hardware_info",
    "concurrent_burn_tokens",
    "evaluate_dispatch",
]


# ── constants ────────────────────────────────────────────────────────────────

#: Task type → ``{model, budget_mult}``.  The five spec types
#: (mechanical/coding/research/review/docs) are authoritative; the four legacy
#: aliases (reasoning/chat/simple) preserve the pre-P5.1 models so existing
#: callers keep working unchanged.
TASK_PROFILES: dict[str, dict[str, Any]] = {
    # ── spec table (IMPL-SPEC §3) ────────────────────────────────────────────
    "mechanical": {"model": "glm-4.5-flash", "budget_mult": 0.25},
    "coding":     {"model": "glm-5.2",        "budget_mult": 1.0},
    "research":   {"model": "glm-5.2",        "budget_mult": 2.5},
    "review":     {"model": "glm-5.2",        "budget_mult": 0.5},
    "docs":       {"model": "glm-4.5-flash", "budget_mult": 0.5},
    # ── legacy aliases (kept for backward compatibility) ─────────────────────
    "reasoning":  {"model": "glm-4.5",        "budget_mult": 2.0},
    "chat":       {"model": "glm-4.5-air",   "budget_mult": 0.5},
    "simple":     {"model": "glm-4.5-flash", "budget_mult": 0.25},
}

DEFAULT_TASK_TYPE: str = "coding"

#: Safety margin per hardware requirement (v2 §Hardware Kalman Margin Logic).
#: Multiplier applied to the task budget to compute required headroom.
HARDWARE_SAFETY_MARGIN: dict[str, float] = {
    "none":       2.0,   # software: 2x budget headroom
    "board":      4.0,   # single board: flash + test takes time
    "dual_board": 6.0,   # two boards: harder to coordinate
    "dq05":       3.0,   # remote: network adds variance
}

DEFAULT_HARDWARE_REQ: str = "none"

#: Flash downgrade uses ~30% of the full task budget (spec §Decision Logic).
FLASH_BUDGET_FACTOR: float = 0.3

#: A key at/above this used-pct is treated as exhausted and skipped entirely.
QUOTA_USED_HOLD_PCT: float = 95.0

#: Estimated wall-clock minutes per hardware sub-task (v2 §Duration Impact).
DURATION_MINUTES: dict[str, int] = {
    "flash":      10,   # pio upload + verify
    "capture":    20,   # serial capture window
    "throughput": 15,   # tx/rx test cycle
    "handshake":  60,   # two-node integration test
}
_DEFAULT_DURATION_MINUTES: int = 15

#: Floor for effective price (ADR-004 — effective price is ALWAYS > 0).
MIN_EFFECTIVE_PRICE: float = 0.001

#: z.ai flat-rate quota per key (tokens / 5h window).
DEFAULT_QUOTA_TOTAL: int = 2_000_000


# ── helpers ──────────────────────────────────────────────────────────────────


def normalize_task_type(task_type: str | None) -> str:
    """Map a (possibly unknown) task type to a canonical profile key.

    Unknown / empty / ``None`` values fall back to :data:`DEFAULT_TASK_TYPE`
    (``"coding"``) — the conservative default per spec §Safety Properties #4.

    >>> normalize_task_type("research")
    'research'
    >>> normalize_task_type("nonsense")
    'coding'
    >>> normalize_task_type(None)
    'coding'
    """
    if not task_type or task_type not in TASK_PROFILES:
        return DEFAULT_TASK_TYPE
    return task_type


def resolve_task_profile(task_type: str | None) -> dict[str, Any]:
    """Return ``{model, budget_mult}`` for a task type (defaults to coding)."""
    return TASK_PROFILES[normalize_task_type(task_type)]


def _hardware_available(hardware_req: str, state: dict[str, Any] | None) -> bool:
    """Dimension 1: is the required hardware present and free RIGHT NOW?"""
    if hardware_req == "none":
        return True
    hs = state or {}
    lock_free = hs.get("lock_status") == "free"
    if hardware_req == "board":
        return bool(hs.get("board_present")) and lock_free
    if hardware_req == "dual_board":
        return hs.get("board_count", 0) >= 2 and lock_free
    if hardware_req == "dq05":
        return bool(hs.get("dq05_reachable"))
    # Unknown hardware requirement — fail open (treat as software).
    return True


def build_hardware_info(
    hardware_req: str,
    state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the ``hardware`` response object from a probe result.

    ``state`` is whatever the proxy gathered from the board watcher, the lock
    monitor JSON, and the DQ05 reachability check.  Missing signals degrade
    to safe defaults (absent / unknown).
    """
    hs = state or {}
    available = _hardware_available(hardware_req, hs)
    return {
        "required": hardware_req,
        "available": available,
        "board_present": bool(hs.get("board_present", False)),
        "board_id": hs.get("board_id"),
        "lock_status": hs.get("lock_status", "unknown"),
        "queue_depth": int(hs.get("queue_depth", 0)),
        "estimated_wait_minutes": int(hs.get("estimated_wait_minutes", 0)),
    }


def concurrent_burn_tokens(
    hardware_req: str,
    task_subtype: str | None,
    burn_rate_pct_per_hour: dict[str, float],
    quota_total: int,
) -> float:
    """Extra headroom for quota burned by OTHER tasks during a hardware task.

    Hardware tasks run for a known wall-clock duration (flash=10 min, …).
    While the board is busy, the rest of the fleet keeps burning quota, so the
    gate must reserve room for that concurrent burn on top of the task's own
    budget.  Uses the worst-case (max) burn rate of the two keys.

    Software tasks (``hardware_req == "none"``) add nothing.
    """
    if hardware_req == "none":
        return 0.0
    duration_min = DURATION_MINUTES.get(task_subtype or "", _DEFAULT_DURATION_MINUTES)
    duration_hours = duration_min / 60.0
    burn = max(
        burn_rate_pct_per_hour.get("ours", 0.0),
        burn_rate_pct_per_hour.get("friend", 0.0),
        0.0,
    )
    return (burn / 100.0) * quota_total * duration_hours


def _scarcity_factor(quota_used_pct: float) -> float:
    """Deterministic ramp: 1.0 below 50%, → 2.0 at 100% (matches price_kalman)."""
    return 1.0 + max(0.0, (quota_used_pct - 50.0) / 50.0)


def _candidates_with_headroom(
    quota: dict[str, dict[str, Any]],
    required_headroom: float,
) -> list[str]:
    """Keys that are healthy, under the hold threshold, and have headroom."""
    out = []
    for key in ("ours", "friend"):
        kq = quota.get(key, {})
        if not kq.get("healthy", True):
            continue
        if kq.get("used_pct", 0.0) >= QUOTA_USED_HOLD_PCT:
            continue
        if kq.get("remaining", 0.0) >= required_headroom:
            out.append(key)
    return out


# ── main decision ────────────────────────────────────────────────────────────


def evaluate_dispatch(
    *,
    estimated_tokens: int,
    task_type: str,
    hardware_req: str = DEFAULT_HARDWARE_REQ,
    task_subtype: str | None = None,
    quota: dict[str, dict[str, Any]],
    burn_rate_pct_per_hour: dict[str, float],
    converged_rates: dict[str, float],
    is_peak: bool = False,
    peak_mult: float = 1.0,
    quota_total: int = DEFAULT_QUOTA_TOTAL,
    min_effective_price: float = MIN_EFFECTIVE_PRICE,
    hardware_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Three-dimension dispatch-gate decision (pure, deterministic).

    Parameters
    ----------
    estimated_tokens:
        Caller's estimate of tokens the task will consume (pre-multiplier).
    task_type:
        One of :data:`TASK_PROFILES`; unknown → coding.
    hardware_req:
        ``none`` / ``board`` / ``dual_board`` / ``dq05``.
    quota:
        ``{"ours": {"used_pct", "remaining", "healthy"}, "friend": {...}}``.
    burn_rate_pct_per_hour:
        ``{"ours": float, "friend": float}`` — from cached Kalman predictions.
    converged_rates:
        ``{"ours": $/M, "friend": $/M}`` — converged PriceKalman base rates.
    hardware_state:
        Probe result from the proxy (board watcher + lock monitor + DQ05).

    Returns
    -------
    dict
        Full gate response with every field from impl-spec v1 + v2.
    """
    # ── 0. resolve profile ───────────────────────────────────────────────────
    profile = resolve_task_profile(task_type)
    model: str | None = profile["model"]
    budget_mult = profile["budget_mult"]
    task_budget = estimated_tokens * budget_mult

    # ── 1. hardware gate (Dimension 1 — binary, first) ────────────────────────
    hardware = build_hardware_info(hardware_req, hardware_state)
    hw_available = hardware["available"]

    # ── 2. quota gate (Dimension 2 — predictive, hardware-scaled margin) ──────
    margin = HARDWARE_SAFETY_MARGIN.get(hardware_req, HARDWARE_SAFETY_MARGIN["none"])
    burn_extra = concurrent_burn_tokens(
        hardware_req, task_subtype, burn_rate_pct_per_hour, quota_total)
    required_headroom = task_budget * margin + burn_extra

    downgraded = False
    can_dispatch = False
    reason = ""

    if not hw_available:
        # Dimension 1 fail → hard hold, regardless of quota.
        can_dispatch = False
        model = None
        reason = (
            f"hardware_unavailable: {hardware_req} required but not present/free"
        )
    else:
        candidates = _candidates_with_headroom(quota, required_headroom)
        if candidates:
            can_dispatch = True
            reason = (
                f"sufficient headroom ({candidates[0]} key) "
                f"with {margin:g}x margin"
            )
        else:
            # Flash downgrade before holding (spec §Decision Logic).
            flash_budget = task_budget * FLASH_BUDGET_FACTOR
            flash_required = flash_budget * margin + burn_extra
            flash_candidates = _candidates_with_headroom(quota, flash_required)
            if flash_candidates:
                model = "glm-4.5-flash"
                downgraded = True
                can_dispatch = True
                task_budget = flash_budget
                reason = (
                    f"downgraded to flash due to quota pressure "
                    f"({flash_candidates[0]} key)"
                )
            else:
                can_dispatch = False
                model = None
                reason = (
                    f"both keys will exhaust within task budget even with "
                    f"flash ({margin:g}x margin)"
                )

    # ── 3. price / scarcity (Dimension 3 — informational + override) ─────────
    scarcity_override = (
        hardware_req != "none"
        and hw_available
        and is_peak
        and can_dispatch
    )
    used_ours = quota.get("ours", {}).get("used_pct", 0.0)
    used_friend = quota.get("friend", {}).get("used_pct", 0.0)
    max_used_pct = max(used_ours, used_friend, 0.0)
    scarcity = _scarcity_factor(max_used_pct)
    base_price = converged_rates.get("ours", 0.001)
    effective_price = max(base_price * peak_mult * scarcity, min_effective_price)
    predicted_cost = (
        effective_price * task_budget / 1_000_000 if task_budget else 0.0
    )

    # ── hours-until-exhaustion (from cached burn-rate predictions) ───────────
    hours_until: dict[str, float | None] = {}
    burn_out: dict[str, float] = {}
    for key in ("ours", "friend"):
        burn = burn_rate_pct_per_hour.get(key, 0.0)
        burn_out[key] = round(burn, 1)
        used = quota.get(key, {}).get("used_pct", 0.0)
        hours_until[key] = round((100.0 - used) / burn, 1) if burn > 0 else None

    return {
        "can_dispatch": can_dispatch,
        "reason": reason,
        "recommended_model": model,
        "effective_price_per_m": round(effective_price, 6),
        "predicted_cost": round(predicted_cost, 6),
        "hours_until_exhaustion": hours_until,
        "quota_used_pct": {
            "ours": round(used_ours, 1),
            "friend": round(used_friend, 1),
        },
        "burn_rate_pct_per_hour": burn_out,
        "is_peak_hour": is_peak,
        "peak_multiplier": peak_mult,
        "scarcity_factor": round(scarcity, 2),
        "downgraded": downgraded,
        "scarcity_override": scarcity_override,
        "hardware": hardware,
        "task_budget": int(task_budget),
        "safety_margin": margin,
    }
