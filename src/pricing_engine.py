"""pricing_engine.py — Deterministic multiplier layer for effective price.

Applies deterministic multipliers to the Kalman-smoothed base rate (ADR-009).
This module contains NO Kalman, NO state, NO smoothing — every function is a
pure function of its arguments (ADR-003):

    effective_price = base_rate * peak_mult * scarcity_mult * health_mult

Invariants (ADR-004):
    - effective_price > 0 ALWAYS (MIN_EFFECTIVE_PRICE = 0.001 floor).
    - health_factor = +inf is the ONLY mechanism that makes a provider
      unselectable; it is preserved (never floored) so the routing optimizer
      can treat it as "unreachable".

The base_rate fed into compute_effective_price is expected to come from
price_kalman.base_rate() (the sole Kalman-smoothed component, ADR-009). For
per-token providers (PPQ, OpenRouter) the base rate is their fixed published
price; for "free" providers (local Ollama, shared z.ai friend key) it must be
configured >= MIN_EFFECTIVE_PRICE (ADR-004 invariant #2).

Phase 1.3 of the merchant routing engine.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone

# ── ADR-004: effective price is always positive ─────────────────────────────
MIN_EFFECTIVE_PRICE: float = 0.001  # $/M tokens — global floor for all providers

# ── ADR-003: deterministic peak multiplier (instant step, NOT a Kalman input)
# z.ai peak hours are UTC 06:00–09:59 (Beijing 14:00–17:59). The price triples
# during this window via an INSTANT step function: no smoothing, no
# interpolation, no gradual transition.
#
# NOTE: config/providers.yaml lists `peak_hours_utc: [6, 10]` with an
# "inclusive" comment, which is ambiguous. The authoritative source is
# ADR-003 / the task spec, both of which specify the window as {6, 7, 8, 9}
# (i.e. [6, 10) half-open). Reviewer: reconcile the config comment in R1.
PEAK_HOURS_UTC: frozenset[int] = frozenset({6, 7, 8, 9})
PEAK_MULTIPLIER: float = 3.0

# ── ADR-009: deterministic scarcity multiplier ──────────────────────────────
# Linear ramp starting at 50% quota usage, reaching 2.0x at 100%. Below the
# onset there is no scarcity (factor == 1.0); over 100% it keeps ramping.
# The ramp spans (FULL - ONSET) = 50 percentage points, so the divisor is 50
# (matches the spec formula 1 + max(0, (pct - 50) / 50)).
SCARCITY_ONSET_PCT: float = 50.0   # at/below this → scarcity_factor == 1.0
SCARCITY_FULL_PCT: float = 100.0   # quota % at which scarcity_factor == 2.0

# ── ADR-003/004: graduated health/circuit-breaker multiplier ──────────────
# Replaces the binary health_factor with a five-tier graduated penalty
# based on failure_count. The old 429 burst penalty (2.0x for >3 recent
# 429s) is subsumed: 429s now increment failure_count, so a burst naturally
# falls into the 3-5 range (3.0x) — a stronger, more precise signal.
#
# Graduated scale:
#   0 failures       → 1.0x  (no penalty)
#   1-2 failures     → 1.5x  (soft penalty, transient issue)
#   3-5 failures     → 3.0x  (moderate penalty, clearly problematic)
#   6-10 failures    → 10.0x (severe penalty, almost unreachable)
#   >10 failures     → +inf  (circuit breaker, fully unreachable)
#   breaker_tripped  → +inf  (circuit breaker, fully unreachable)
#
# This is price-based regulation: a struggling key sees its effective price
# rise progressively until the optimizer naturally routes traffic away.
# Only after extreme failure counts (>10) or an explicit breaker trip does
# the price go to infinity (full circuit breaker).
HEALTH_PENALTY_SOFT: float = 1.5      # 1-2 failures
HEALTH_PENALTY_MODERATE: float = 3.0   # 3-5 failures
HEALTH_PENALTY_SEVERE: float = 10.0    # 6-10 failures
HEALTH_BREAKER_THRESHOLD: int = 10     # strictly greater → circuit breaker

# ── Predictive quota-pacing multiplier ──────────────────────────────────────
# Adjusts price based on burn rate vs time remaining in quota windows.
# If burning too fast (will exhaust before reset) → increase price to slow down.
# If underutilizing → decrease price to use more.
# Priority: never run out > use everything.
#
# pace_ratio = predicted_total / quota_total
#   predicted_total = current_used + burn_rate * time_remaining
# pace_factor = max(PACE_FLOOR, min(PACE_CAP, pace_ratio ** 2))
#
# Examples:
#   ratio 0.5 (way under) → 0.25 (price drops to 25% — attract traffic)
#   ratio 0.9 (slightly under) → 0.81 (slight decrease)
#   ratio 1.0 (perfect) → 1.0
#   ratio 1.2 (will exhaust early) → 1.44 (price increases 44%)
#   ratio 2.0 (will exhaust way early) → 4.0 → capped at 3.0
PACE_FLOOR: float = 0.5    # minimum pace_factor (attract traffic aggressively)
PACE_CAP: float = 3.0      # maximum pace_factor (strong slowdown, but finite)

# ── Extra-usage multiplier (ollama Cloud quota regime) ──────────────────────
# When the Ollama Cloud quota is exceeded (regime='extra'), the effective
# rate is multiplied by the extra-usage multiplier. The default multiplier
# of 6.25x maps the $0.024/M base rate to $0.15/M ($0.024 * 6.25 = $0.15).
#
# The multiplier can be overridden via the OLLAMA_EXTRA_USAGE_MULTIPLIER env
# var (EU-R3). This allows tuning without code changes.
#
# When the quota is fully exhausted (regime='exhausted'), the multiplier is
# +inf — same semantics as the health circuit breaker: the provider becomes
# unreachable to the optimizer.
#
# Config source: config/providers.yaml → ollama_cloud.extra_usage_rate_per_m
# The multiplier is derived as: extra_usage_rate_per_m / base_rate.
# Default: 0.15 / 0.024 = 6.25.
EXTRA_USAGE_BASE_RATE: float = 0.024   # $/M — ollama_cloud base rate
EXTRA_USAGE_TARGET_RATE: float = 0.15  # $/M — target effective rate in extra mode
# Recompute default multiplier from the rate values so they stay in sync.
# Allow override via env var (EU-R3: OLLAMA_EXTRA_USAGE_MULTIPLIER).
_DEFAULT_EXTRA_USAGE_MULT = EXTRA_USAGE_TARGET_RATE / EXTRA_USAGE_BASE_RATE  # ≈6.25
EXTRA_USAGE_MULTIPLIER: float = float(
    os.environ.get("OLLAMA_EXTRA_USAGE_MULTIPLIER", _DEFAULT_EXTRA_USAGE_MULT)
)

# ── Continuous quota-pressure multiplier (RP-PRICING / RP-EXP) ───────────────
# The continuous, price-based replacement for the binary extra_usage_multiplier.
# As the Ollama Cloud quota depletes, this factor rises smoothly from 1.0 so the
# routing optimizer reroutes to a cheaper provider (z.ai) the moment Ollama's
# effective price crosses over — NO thresholds, NO regime strings.
#
# Shape (RP-EXP: rational asymptotic curve between ONSET and 100% usage):
#   usage <= ONSET    → 1.0                          (plenty of quota)
#   ONSET < u < 1.0   → 1 + K * t / (1 - t)          (t = (u-onset)/(1-onset))
#   usage >= 1.0      → +inf                         (unreachable — the router
#                                                      ALWAYS finds a cheaper
#                                                      alternative first)
#
#   where K = (extra_rate / base_rate) - 1.0 = EXTRA_USAGE_MULTIPLIER - 1.0
#   (≈5.25 with the default $0.15/$0.024 rates).
#
# The curve passes through the full extra-usage rate (K+1 = 6.25x → $0.15/M) at
# the midpoint of the ramp (u = onset + 0.5*(1-onset) = 0.85), then diverges
# toward INFINITY as usage → 100%. Because the price literally becomes +∞ at full
# quota, the optimizer can never keep Ollama at 100% — it always reroutes to z.ai
# (or an external) first, so the quota never hard-fails for non-exclusive models.
#
# Ollama-EXCLUSIVE models (kimi-k3, gpt-oss, …) are unaffected: live_router
# short-circuits them to ollama_cloud BEFORE the price comparison, so the +∞ at
# 100% never blocks a model that genuinely has no alternative.
#
# Both the 5h session and 7d weekly usage fractions are considered; the WORST
# (max) governs (priority: never run out).
#
# Price table (onset=0.70, base=$0.024/M, K≈5.25):
#   u=0.70 → 1.0x   → $0.024/M   (onset — no penalty)
#   u=0.80 → 3.63x  → $0.087/M
#   u=0.85 → 6.25x  → $0.150/M   (midpoint = extra-usage rate)
#   u=0.90 → 11.5x  → $0.276/M   (> openrouter $0.135)
#   u=0.95 → 27.3x  → $0.654/M   (> deepinfra)
#   u=0.99 → ~153x  → $3.68/M    (unreachable for optimizer)
#   u≥1.00 → +∞     → unreachable (always rerouted to a cheaper alternative)
QUOTA_PRESSURE_ONSET: float = float(
    os.environ.get("OLLAMA_QUOTA_PRESSURE_ONSET", "0.70")
)

# ── Per-provider pressure parameters (universal endpoint pressure) ──────────
# EVERY paid endpoint gets the same RP-EXP exponential pressure curve. Each
# endpoint is a self-contained pricing model (Routstr node pattern): the price
# the router sees depends on remaining quota/credits. Only the onset, the window
# source, and the kill switch differ per provider.
#
# FELIX FINAL DECISION (Aug 5 19:00): ALL endpoints asymptote=1.5 (UNIFORM LOW).
# Strategy: squeeze cheap keys as LONG as possible — do NOT flee to expensive
# endpoints (openrouter/deepinfra) unless absolutely necessary. The low
# asymptote means pressure ramps gently; price only spikes near exhaustion.
#
# Onset points stagger pressure activation:
#   z.ai first (0.60) — 5h window is tiny (~2M tokens), exhausts fast.
#   ollama_cloud next (0.70) — 5h window is large (500M tokens).
#   credit-based last (0.80) — credits deplete slowly, refill manually.
#
# Credit-based providers (PPQ, OpenRouter, DeepInfra) compute their usage
# fraction from remaining balance: u = 1 - (remaining / starting).
# Exhausted balance (≤ 0) → u ≥ 1.0 → +inf (price infinity, must divert).

# Ollama Cloud: quota-windowed (5h session + 7d weekly). Paid extra-usage is
# available so extra_usage_multiplier stays for the legacy path, but the
# pressure curve uses the uniform asymptote (1.5).
OLLAMA_QUOTA_PRESSURE_ASYMPTOTE: float = float(
    os.environ.get("OLLAMA_QUOTA_PRESSURE_ASYMPTOTE", "1.5")
)

# z.ai: 3 windows (5h × weekly × monthly) multiplied via superposition.
#   Onset 0.60 (earlier than Ollama — z.ai's 5h window is ~2M tokens, tiny).
#   hard_limit=True (z.ai has no extra-usage path — at 100% you get 429s).
ZAI_QUOTA_PRESSURE_ONSET: float = float(
    os.environ.get("ZAI_QUOTA_PRESSURE_ONSET", "0.60")
)
ZAI_QUOTA_PRESSURE_ASYMPTOTE: float = float(
    os.environ.get("ZAI_QUOTA_PRESSURE_ASYMPTOTE", "1.5")
)

# ── Credit-based providers (PPQ, OpenRouter, DeepInfra) ─────────────────────
# These have no time windows — their usage fraction is derived from remaining
# credit balance: u = 1 - (remaining / starting_balance).
#   Onset 0.80 for all three (credits deplete slowly).
#   hard_limit=True (exhausted balance = no service → +inf).

# PPQ: credits tracked via /credits/balance API (negative = exhausted).
PPQ_QUOTA_PRESSURE_ONSET: float = float(
    os.environ.get("PPQ_QUOTA_PRESSURE_ONSET", "0.80")
)
PPQ_QUOTA_PRESSURE_ASYMPTOTE: float = float(
    os.environ.get("PPQ_QUOTA_PRESSURE_ASYMPTOTE", "1.5")
)

# OpenRouter: $10 starting credits, currently EXHAUSTED ($0 balance).
# Self-tracked via SUM(cost_usd) in api_calls WHERE key_name='openrouter'.
OPENROUTER_CREDIT_PRESSURE_ONSET: float = float(
    os.environ.get("OPENROUTER_CREDIT_PRESSURE_ONSET", "0.80")
)
OPENROUTER_CREDIT_PRESSURE_ASYMPTOTE: float = float(
    os.environ.get("OPENROUTER_CREDIT_PRESSURE_ASYMPTOTE", "1.5")
)
OPENROUTER_STARTING_BALANCE: float = float(
    os.environ.get("OPENROUTER_STARTING_BALANCE", "10.0")
)

# DeepInfra: $5 starting balance. NO balance API — self-tracked via
# SUM(cost_usd) in api_calls WHERE key_name='deepinfra'.
# remaining = starting_balance - cumulative_spend.
DEEPINFRA_CREDIT_PRESSURE_ONSET: float = float(
    os.environ.get("DEEPINFRA_CREDIT_PRESSURE_ONSET", "0.80")
)
DEEPINFRA_CREDIT_PRESSURE_ASYMPTOTE: float = float(
    os.environ.get("DEEPINFRA_CREDIT_PRESSURE_ASYMPTOTE", "1.5")
)
DEEPINFRA_STARTING_BALANCE: float = float(
    os.environ.get("DEEPINFRA_STARTING_BALANCE", "5.0")
)


def peak_multiplier(provider: str, hour_utc: int | None = None) -> float:
    """Deterministic peak-hour step function (ADR-003).

    Returns PEAK_MULTIPLIER (3.0) when *provider* is a z.ai key AND *hour_utc*
    falls inside the peak window {6, 7, 8, 9}; 1.0 otherwise. The step is
    instantaneous at the hour boundary — no filtering.

    Args:
        provider: Provider identifier. Any value whose lower-cased form starts
            with "zai" (e.g. "zai", "zai_ours", "zai_friend") or is a canonical
            z.ai key name ("ours", "friend") is treated as a z.ai key and
            subject to peak pricing. All other providers return 1.0 (flat-rate
            cloud and per-token providers have no peak window).
        hour_utc: UTC hour of day (0-23). If None, the current UTC hour is
            used (datetime.now(timezone.utc).hour, per ADR-003 invariant #1).

    Returns:
        3.0 during z.ai peak hours, else 1.0.
    """
    if hour_utc is None:
        hour_utc = datetime.now(timezone.utc).hour
    prov_lower = str(provider).lower()
    is_zai = prov_lower.startswith("zai") or prov_lower in ("ours", "friend")
    if is_zai and hour_utc in PEAK_HOURS_UTC:
        return PEAK_MULTIPLIER
    return 1.0


def scarcity_factor(quota_used_pct: float) -> float:
    """Deterministic scarcity multiplier (ADR-009).

    Linear ramp above 50% quota usage:

        pct <= 50  → 1.0   (no scarcity)
        pct == 75  → 1.5
        pct == 100 → 2.0   (quota exhausted)
        pct > 100  → continues ramping (over-quota; no upper clamp)

    Formula: 1 + max(0, (quota_used_pct - SCARCITY_ONSET_PCT)
                          / (SCARCITY_FULL_PCT - SCARCITY_ONSET_PCT))
        == 1 + max(0, (quota_used_pct - 50) / 50)

    Args:
        quota_used_pct: Quota used as a percentage (0-100 typical; may exceed
            100 when a provider is over its allocation).

    Returns:
        Scarcity multiplier, always >= 1.0.
    """
    span = SCARCITY_FULL_PCT - SCARCITY_ONSET_PCT  # 50 percentage points
    return 1.0 + max(0.0, (quota_used_pct - SCARCITY_ONSET_PCT) / span)


def health_pricing_factor(failure_count: int = 0, breaker_tripped: bool = False) -> float:
    """Graduated health multiplier based on failure count (ADR-003 / ADR-004).

    This replaces the binary health_factor with a five-tier graduated
    penalty. A struggling key sees its effective price rise progressively
    until the optimizer naturally routes traffic away — price-based
    regulation, not binary circuit-breaking. Only after extreme failure
    counts (>10) or an explicit breaker trip does the price go to infinity.

    Scale (first applicable tier wins):
        breaker_tripped          → +inf   (circuit breaker: unreachable)
        failure_count > 10       → +inf   (circuit breaker: unreachable)
        6 ≤ failure_count ≤ 10   → 10.0   (severe penalty)
        3 ≤ failure_count ≤ 5    → 3.0    (moderate penalty)
        1 ≤ failure_count ≤ 2    → 1.5    (soft penalty, transient)
        failure_count ≤ 0        → 1.0    (no penalty)

    The old 429 burst penalty (2.0x for >3 recent 429s) is subsumed:
    429s now increment failure_count, so a burst of 4+ 429s falls in the
    3-5 range → 3.0x (stronger than old 2.0x — a 429 burst is a clear
    signal of trouble, and the graduated scale captures that).

    +inf is intentional: it is the ONLY mechanism that makes a provider
    unselectable in the routing optimizer, and compute_effective_price
    preserves it rather than flooring it (ADR-004 invariants #3, #4).

    Args:
        failure_count: Number of consecutive recent failures (429s, 5xx,
            timeouts, auth errors, etc.). Reset to 0 on success.
        breaker_tripped: True when the circuit breaker has been explicitly
            tripped (e.g. by key_health_tracker). Overrides failure_count.

    Returns:
        Graduated multiplier: 1.0, 1.5, 3.0, 10.0, or +inf.
    """
    # Circuit breaker — explicit trip or extreme failure count.
    if breaker_tripped or failure_count > HEALTH_BREAKER_THRESHOLD:
        return math.inf

    # Negative failure_count is treated as 0 (no penalty).
    fc = max(0, failure_count)

    if fc <= 0:
        return 1.0
    if fc <= 2:
        return HEALTH_PENALTY_SOFT       # 1.5x
    if fc <= 5:
        return HEALTH_PENALTY_MODERATE   # 3.0x
    # 6-10
    return HEALTH_PENALTY_SEVERE          # 10.0x


def pace_factor(
    quota_used: float,
    quota_total: float,
    time_elapsed_pct: float,  # 0.0 to 1.0 — how much of the window has elapsed
    burn_rate: float,         # tokens/hour
    window_duration_hours: float = 5.0,  # window duration (5h z.ai, 168h weekly)
) -> float:
    """Predictive quota-pacing multiplier.

    Adjusts price so we use quota without running out.
    Returns multiplier: >1.0 = slow down (will exhaust), <1.0 = speed up (underusing).

    Computation::

        time_remaining_hours = (1 - time_elapsed_pct) * window_duration_hours
        predicted_usage      = burn_rate * time_remaining_hours
        predicted_total      = quota_used + predicted_usage
        pace_ratio           = predicted_total / quota_total
        pace_factor          = max(PACE_FLOOR, min(PACE_CAP, pace_ratio ** 2))

    Priority: **never run out > use everything**.  When ``pace_ratio > 1.0``
    we are on track to exhaust before the window resets, so the factor
    increases (squaring amplifies the penalty).  When ``pace_ratio < 0.9``
    we are underutilizing, so the factor decreases to attract traffic.

    The result is clamped to ``[PACE_FLOOR, PACE_CAP]`` = ``[0.5, 3.0]``.

    Args:
        quota_used: Tokens already consumed in this window.
        quota_total: Total tokens allocated for this window.
        time_elapsed_pct: Fraction of the window that has elapsed (0.0–1.0).
        burn_rate: Current burn rate from ConsumptionKalman (tokens/hour).
        window_duration_hours: Duration of the quota window in hours.
            Default 5.0 (z.ai 5-hour window). Use 168.0 for the weekly window.

    Returns:
        Pace multiplier in ``[0.5, 3.0]``. Returns 1.0 when there is
        insufficient data to make a prediction (zero burn rate, zero
        elapsed time, or zero quota total).
    """
    # ── Guard: insufficient data → no adjustment ──────────────────────
    if burn_rate <= 0:
        return 1.0
    if quota_total <= 0:
        return 1.0
    if time_elapsed_pct <= 0:
        # Window just reset — no pace data yet.
        return 1.0

    # Clamp time_elapsed_pct to [0, 1] (defensive against bad input).
    e = min(time_elapsed_pct, 1.0)

    # ── Compute pace ratio ────────────────────────────────────────────
    time_remaining_hours = (1.0 - e) * window_duration_hours
    predicted_usage = burn_rate * time_remaining_hours
    predicted_total = quota_used + predicted_usage
    pace_ratio = predicted_total / quota_total

    # ── Map ratio → factor (square amplifies deviation from 1.0) ───────
    factor = pace_ratio ** 2
    return max(PACE_FLOOR, min(PACE_CAP, factor))


def pace_factor_multi(
    windows: list[tuple[float, float, float, float, float]],
) -> float:
    """Multi-window pace factor — worst case (MAX) governs.

    Computes :func:`pace_factor` for each quota window and returns the
    maximum. This ensures the most restrictive window drives pricing
    (priority: never run out).

    Args:
        windows: List of tuples, each containing:
            ``(quota_used, quota_total, time_elapsed_pct, burn_rate,
              window_duration_hours)``.

    Returns:
        Maximum pace_factor across all windows, or 1.0 if the list
        is empty (no data, no adjustment).
    """
    if not windows:
        return 1.0
    factors = [
        pace_factor(used, total, elapsed, rate, duration)
        for used, total, elapsed, rate, duration in windows
    ]
    return max(factors)


def extra_usage_multiplier(
    regime: str,
    multiplier: float | None = None,
) -> float:
    """Deterministic extra-usage multiplier based on quota regime.

    Maps the Ollama Cloud quota regime (from
    :func:`ollama_quota_tracker.get_quota_status`) to a price multiplier:

        regime == "included"  → 1.0   (no change — within free quota)
        regime == "extra"     → EXTRA_USAGE_MULTIPLIER (default ≈6.25x,
                                 raising $0.024/M base to $0.15/M)
        regime == "exhausted" → +inf  (provider unreachable, filtered out)

    The *multiplier* override allows callers to supply a config-derived
    value (e.g. from ``providers.yaml`` →
    ``ollama_cloud.extra_usage_rate_per_m`` divided by the provider's
    base rate). When *multiplier* is None, the module-level
    :data:`EXTRA_USAGE_MULTIPLIER` constant is used.

    Args:
        regime: Quota regime string — one of ``"included"``,
            ``"extra"``, or ``"exhausted"`` (as returned by
            :func:`ollama_quota_tracker.get_quota_status`).
        multiplier: Optional override for the extra-mode multiplier.
            If None, uses :data:`EXTRA_USAGE_MULTIPLIER` (≈6.25).

    Returns:
        Multiplier: 1.0 for included, the configured multiplier (>= 1.0)
        for extra, or +inf for exhausted. Unknown regimes default to 1.0
        (fail-safe: no penalty on unrecognised input).
    """
    if regime == "extra":
        return multiplier if multiplier is not None else EXTRA_USAGE_MULTIPLIER
    if regime == "exhausted":
        return math.inf
    # "included" or any unknown regime → no change.
    return 1.0


def _single_window_factor(
    u: float,
    onset: float,
    asymptote: float,
) -> float:
    """Exponential quota-pressure factor for a single window.

    RP-EXP rational asymptotic curve ``1 + K·t/(1-t)``::

        u <= onset    → 1.0
        onset < u < 1 → 1 + K·t/(1-t)   (t = (u-onset)/(1-onset))
        u >= 1.0      → +inf

    where ``K = asymptote - 1.0``.

    Returns +inf at u ≥ 1.0. The caller decides whether to cap it (Ollama:
    extra-usage available) or keep it (z.ai/PPQ: hard limit).
    """
    if u >= 1.0:
        return math.inf
    if u <= onset:
        return 1.0
    span = 1.0 - onset
    k = asymptote - 1.0
    if k <= 0.0:
        return 1.0
    t = (u - onset) / span
    return 1.0 + k * t / (1.0 - t)


def quota_pressure_factor(
    usage: float,
    weekly: float | None = None,
    monthly: float | None = None,
    onset: float = QUOTA_PRESSURE_ONSET,
    asymptote: float = EXTRA_USAGE_MULTIPLIER,
    hard_limit: bool = False,
) -> float:
    """Continuous quota-pressure multiplier with superimposed windows.

    Computes the exponential factor for EACH provided window independently,
    then **multiplies** them. This gives stronger pressure when multiple
    windows deplete simultaneously — a session at 85% AND a weekly at 85%
    produces a combined factor of ~7.7x, not just 2.8x.

    Window types:
    - **usage**: 5-hour session quota (Ollama, z.ai)
    - **weekly**: 7-day weekly quota (Ollama, z.ai)
    - **monthly**: 30-day monthly quota (z.ai)

    Only windows that are not ``None`` contribute to the product. This lets
    each endpoint pass exactly the windows it has.

    Cap behaviour when ANY provided window reaches 100%:

    - ``hard_limit=False`` (Ollama Cloud): caps at *asymptote* (≈6.25x).
      Ollama allows extra usage at the list rate, so exclusive models like
      kimi-k3 remain reachable at their true cost.
    - ``hard_limit=True`` (z.ai, PPQ): returns **+inf**. These providers have
      no extra-usage path — the optimizer MUST divert to another endpoint.

    Superimposed price table (onset=0.70, asymptote=1.5, base=$0.024/M)::

        Session  Weekly  Factor      $/M      Notes
        ───────  ──────  ────────    ─────    ─────────────────────────
        80%      50%     3.63x       $0.087   Session drives
        80%      80%     13.1x       $0.315   Both compound
        85%      85%     39.1x       $0.937   Strong divert
        90%      70%     11.5x       $0.276   Session drives
        90%      90%     132x        $3.17    Compounded — guaranteed divert
        95%      50%     27.3x       $0.654   Session alone
        95%      95%     743x        $17.8    Unreachable
        ≥100%    any     6.25x       $0.15    Extra-usage rate (hard_limit=False)
        ≥100%    any     ∞           —        Hard limit (hard_limit=True)

    This factor **subsumes** :func:`scarcity_factor` for any endpoint that
    passes real window data. Endpoints without window-level data (cold start)
    fall back to :func:`scarcity_factor` via ``compute_effective_price``.

    Args:
        usage: Session usage fraction (0.0–1.0+).
        weekly: Weekly usage fraction (0.0–1.0+). ``None`` = window not tracked.
        monthly: Monthly usage fraction (0.0–1.0+). ``None`` = not tracked.
        onset: Usage fraction at which pressure begins (default 0.75).
        asymptote: Factor at the ramp midpoint (default ≈6.25). Controls
            curve steepness via ``K = asymptote - 1.0``.
        hard_limit: If True, returns +inf when any window ≥ 1.0 (z.ai/PPQ).
            If False, caps at *asymptote* (Ollama extra-usage rate).

    Returns:
        Pressure multiplier, always >= 1.0. May be +inf (hard_limit=True,
        any window exhausted).
    """
    # Collect provided windows.
    windows: list[float] = [usage]
    if weekly is not None:
        windows.append(weekly)
    if monthly is not None:
        windows.append(monthly)

    # If any provided window is exhausted, apply cap behaviour.
    if any(u >= 1.0 for u in windows):
        if hard_limit:
            return math.inf
        return asymptote

    # Multiply single-window factors (superimpose).
    result = 1.0
    for u in windows:
        result *= _single_window_factor(u, onset, asymptote)
    return result


def quota_pressure_factor_superimposed(
    session_usage: float,
    weekly_usage: float | None = None,
    onset: float = QUOTA_PRESSURE_ONSET,
    asymptote: float = EXTRA_USAGE_MULTIPLIER,
) -> float:
    """Superposition of two independent quota-pressure curves (RP-SUPERIMPOSE).

    Felix's direction: instead of driving a single curve off the *worst* (max)
    of the session and weekly usage fractions, evaluate **two separate**
    :func:`quota_pressure_factor` curves — one for the 5-hour *session* window,
    one for the 7-day *weekly* window — and **multiply** them. Both windows
    depleting simultaneously is the genuine worst case, and the product makes it
    dramatically steeper than taking the max::

        max(0.90, 0.90)    → single curve at 0.90        (one curve's value)
        superposition(0.90, 0.90) → curve(0.90)²          (squared — much steeper)

    Since each factor is always >= 1.0, the product is always >= the max-based
    value :func:`quota_pressure_factor` would have produced — so this *never*
    under-penalises a depleting quota; it only sharpens the worst case.

    Degenerate cases fall out of the arithmetic naturally:

    - Both windows below the onset → ``1.0 * 1.0 = 1.0`` (no penalty).
    - One window exhausted (>= 1.0) → ``+inf * finite = +inf`` (unreachable).
    - Both exhausted → ``+inf * +inf = +inf``.

    Args:
        session_usage: 5-hour session usage fraction (0.0–1.0+, from
            ``ollama.com/api/usage`` → ``data.limits.session.usage``).
        weekly_usage: 7-day weekly usage fraction (0.0–1.0+). ``None`` (or 0.0)
            means only the session window governs (the weekly factor is 1.0).
        onset: Usage fraction at which pressure begins (forwarded to BOTH
            curves). Default :data:`QUOTA_PRESSURE_ONSET` (0.70).
        asymptote: Extra-rate/base-rate ratio defining each curve's steepness
            (forwarded to BOTH curves). Default :data:`EXTRA_USAGE_MULTIPLIER`.

    Returns:
        The product ``_single_window_factor(session) *
        _single_window_factor(weekly)``, always >= 1.0, and ``+inf`` if EITHER
        window is exhausted (>= 1.0).
    """
    s = _single_window_factor(session_usage, onset, asymptote)
    w = _single_window_factor(weekly_usage or 0.0, onset, asymptote)
    return s * w


# ── Cold-start seeding (Task 4 / ADR §Cold-Start Seeding) ───────────────────
# For PAID endpoints with no balance/token data yet, we cannot assume a fresh
# balance — an unmeasured provider (e.g. OpenRouter, known-exhausted at $0)
# must not look cheaper than it really is. The ADR seeds the usage fraction to
# 0.5 (assume 50% depleted) as a conservative bias away from blind endpoints.
#
# IMPORTANT: the ADR's own balance table shows usage 0.5 is "below onset — no
# pressure" because every provider's onset (0.60–0.80) is ABOVE the seed. A
# naive seed fed through the normal curve would therefore still yield 1.0 — the
# exact optimism Task 4 eliminates. So cold_start_pressure() evaluates the seed
# with onset=0.0: the protective onset gate exists for MEASURED providers (don't
# penalise them prematurely), but a provider with NO data deserves a baseline
# conservative penalty from the very first request.
#
# At the default seed (0.5) and asymptote (1.5) this returns 1.5x — a moderate,
# short-lived bias that lasts only until the first real balance query lands
# (within ~5 min). Subscription endpoints (z.ai, Ollama) are excluded by the
# ADR: their quota/usage API is called on startup, so there is no cold-start
# gap for them.
COLD_START_USAGE_SEED: float = float(
    os.environ.get("COLD_START_USAGE_SEED", "0.5")
)


def cold_start_pressure(asymptote: float = 1.5, hard_limit: bool = False) -> float:
    """Conservative quota-pressure factor for an endpoint with NO data.

    Returns the RP-EXP curve value at :data:`COLD_START_USAGE_SEED` evaluated
    with ``onset=0.0``, so the seed always produces a penalty (``> 1.0``)
    regardless of any provider's normal onset. This is the cold-start
    replacement for the old optimistic ``return 1.0`` in the live_router
    pressure helpers (``_compute_ppq_pressure``,
    ``_compute_credit_pressure``) — see Task 4 / ADR §Cold-Start Seeding.

    Args:
        asymptote: Curve steepness (``K = asymptote - 1.0``). Defaults to 1.5
            (the uniform asymptote all paid endpoints use). Forward the
            provider's own asymptote when calling.
        hard_limit: If True, returns +inf when the seed ``>= 1.0``. At the
            default seed 0.5 this is irrelevant (0.5 < 1.0), so the result is
            always finite — but the flag is honoured for consistency with
            :func:`quota_pressure_factor`.

    Returns:
        Pressure multiplier ``> 1.0`` for any seed in ``(0, 1)``. Equals the
        *asymptote* at seed 0.5 (e.g. ``1.5`` with the default asymptote).
        Returns 1.0 when the seed is 0.0 (cold-start penalty disabled) and
        ``+inf``/asymptote when the seed ``>= 1.0``.
    """
    return quota_pressure_factor(
        COLD_START_USAGE_SEED,
        onset=0.0,
        asymptote=asymptote,
        hard_limit=hard_limit,
    )


def health_factor(is_healthy: bool, recent_429: int = 0) -> float:
    """Backward-compatible wrapper around health_pricing_factor.

    .. deprecated::
        Use :func:`health_pricing_factor` directly. This wrapper translates
        the old (is_healthy, recent_429) interface into (failure_count,
        breaker_tripped) and delegates.

    Maps the old binary interface:
        is_healthy=False → breaker_tripped=True → +inf
        recent_429 > 3   → failure_count=recent_429 → graduated penalty
        otherwise        → failure_count=0 → 1.0
    """
    if not is_healthy:
        return math.inf
    if recent_429 > 3:
        return health_pricing_factor(failure_count=recent_429)
    return 1.0


def compute_effective_price(
    base_rate: float,
    provider: str,
    quota_pct: float,
    is_healthy: bool | None = None,
    recent_429: int = 0,
    hour_utc: int | None = None,
    failure_count: int = 0,
    breaker_tripped: bool = False,
    pace_mult: float = 1.0,
    extra_usage_regime: str = "included",
    extra_usage_mult: float | None = None,
    quota_pressure: float | None = None,
    zai_session_usage: float | None = None,
    zai_weekly_usage: float | None = None,
    zai_monthly_usage: float | None = None,
) -> float:
    """Effective price = base_rate * peak * scarcity * health * pace * extra.

    Pure deterministic composition of the multipliers above applied to
    the (Kalman-smoothed) base rate. No state, no side effects.

    ADR-004 constraint: the result is ALWAYS strictly positive. The global
    floor MIN_EFFECTIVE_PRICE (0.001 $/M) is enforced for finite results. A
    health factor of +inf is preserved unchanged so an unhealthy provider
    stays "unreachable" to the optimizer rather than being floored.

    Health is computed via :func:`health_pricing_factor` (graduated penalty
    based on failure_count). For backward compatibility, if *is_healthy*
    is provided (not None), the old interface is used: is_healthy=False
    maps to breaker_tripped=True, and recent_429>3 maps to
    failure_count=recent_429.

    The pace multiplier (:func:`pace_factor`) is a predictive quota-pacing
    factor that adjusts price based on burn rate vs time remaining in
    quota windows. It defaults to 1.0 (no adjustment) when not provided.

    The extra-usage multiplier (:func:`extra_usage_multiplier`) applies a
    price penalty when the Ollama Cloud quota regime is "extra" (above
    included quota but not fully exhausted) or makes the provider
    unreachable when "exhausted". It defaults to the "included" regime
    (multiplier = 1.0, no change) so non-Ollama providers are unaffected.

    The *quota-pressure* multiplier (:func:`quota_pressure_factor`,
    RP-PRICING) is the **continuous** replacement for the regime-based
    extra-usage path. When *quota_pressure* is supplied (not None) it takes
    precedence over the legacy *extra_usage_regime* path: the caller computes
    it from the live session/weekly usage fractions via
    :func:`quota_pressure_factor` and passes the result here. This is how
    Ollama's price rises smoothly as its quota depletes until the optimizer
    reroutes to z.ai — no thresholds, no regime strings.

    Args:
        base_rate: Base $/M rate (typically from price_kalman.base_rate, or a
            provider's fixed per-token price).
        provider: Provider identifier (see peak_multiplier).
        quota_pct: Quota used percentage (see scarcity_factor).
        is_healthy: (Legacy) Provider health flag. If None, uses the new
            failure_count/breaker_tripped interface. If False, sets
            breaker_tripped=True.
        recent_429: (Legacy) Recent 429 count — mapped to failure_count
            when >3.
        hour_utc: UTC hour 0-23; None → current UTC hour.
        failure_count: Consecutive recent failures (new interface).
        breaker_tripped: Explicit circuit breaker trip (new interface).
        pace_mult: Predictive quota-pacing multiplier from
            :func:`pace_factor` or :func:`pace_factor_multi`. Default
            1.0 (no pace adjustment).
        extra_usage_regime: Quota regime for Ollama Cloud — one of
            "included", "extra", or "exhausted" (from
            :func:`ollama_quota_tracker.get_quota_status`). Default
            "included" (no penalty — non-Ollama providers unaffected).
        extra_usage_mult: Optional override for the extra-mode multiplier.
            If None, uses :data:`EXTRA_USAGE_MULTIPLIER` (≈6.25).
        quota_pressure: Optional continuous quota-pressure multiplier from
            :func:`quota_pressure_factor`. When provided (not None), it
            **overrides** the legacy *extra_usage_regime* path — use this for
            the smooth, price-based Ollama rerouting (RP-PRICING). When None
            (default), the regime-based :func:`extra_usage_multiplier` is used
            (backward compatible).
        zai_session_usage: 5-hour session usage fraction (0.0–1.0) for a z.ai
            key. When *provider* is a z.ai endpoint (``"zai"`` prefix, or
            ``"ours"``/``"friend"``) AND this is not None, the SCARCITY slot
            switches from the linear :func:`scarcity_factor` to the exponential
            :func:`quota_pressure_factor` with 3 superimposed windows and a
            HARD limit (``hard_limit=True``, ``asymptote``=1.5). At 100% in any
            provided window the result is +inf (must divert). ``None`` (default)
            → cold-start fallback to linear scarcity.
        zai_weekly_usage: 7-day weekly usage fraction (0.0–1.0) for a z.ai key,
            or ``None`` when the window is not tracked.
        zai_monthly_usage: 30-day monthly usage fraction (0.0–1.0) for a z.ai
            key, or ``None`` when not tracked.

    Returns:
        Effective price in $/M. Always > 0: finite results are >= 0.001, and
        an unhealthy or exhausted provider yields +inf (preserved, not floored).
    """
    # Backward compat: if is_healthy is provided, translate to new interface.
    if is_healthy is not None:
        if not is_healthy:
            breaker_tripped = True
        if recent_429 > 3:
            failure_count = max(failure_count, recent_429)

    peak = peak_multiplier(provider, hour_utc)

    # Scarcity / quota pressure.
    #
    # z.ai endpoints (provider starts with "zai", or the canonical key names
    # "ours"/"friend") get the EXPONENTIAL RP-EXP quota-pressure curve with
    # 3 superimposed windows (5h session × 7d weekly × 30d monthly) and a HARD
    # limit — at 100% in ANY window the factor diverges to +inf (z.ai has no
    # extra-usage path; the optimizer must divert to friend/ollama/externals).
    # asymptote=1.5 (uniform across all endpoints — squeeze cheap keys longer).
    #
    # All other providers (and z.ai at cold start, when no window data is
    # available) fall back to the legacy LINEAR scarcity_factor(quota_pct).
    # The peak multiplier stays a separate concern — it is applied on top,
    # never folded into the pressure curve. Asymptote=1.5 (uniform across all
    # endpoints per Felix's final decision — squeeze cheap keys as long as
    # possible; see docs/endpoint-universal-pressure.md).
    prov_lower = str(provider).lower()
    is_zai = prov_lower.startswith("zai") or prov_lower in ("ours", "friend")
    if is_zai and zai_session_usage is not None:
        scarcity = quota_pressure_factor(
            zai_session_usage,
            zai_weekly_usage,
            zai_monthly_usage,
            onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE,
            hard_limit=True,
        )
    else:
        scarcity = scarcity_factor(quota_pct)
    health = health_pricing_factor(failure_count, breaker_tripped)

    # Extra-usage multiplier: prefer the continuous quota-pressure value when
    # the caller supplies it (RP-PRICING); otherwise fall back to the legacy
    # regime-based step function (backward compatible).
    if quota_pressure is not None:
        extra = quota_pressure
    else:
        extra = extra_usage_multiplier(extra_usage_regime, extra_usage_mult)

    price = base_rate * peak * scarcity * health * pace_mult * extra

    # ADR-004 invariant #1: effective_price > 0 always.
    # - NaN arises only from the forbidden 0 * inf combination (invariant #4,
    #   which says zero and infinity never appear in the same product). It
    #   indicates a mis-configured (zero) base rate on an unhealthy provider;
    #   floor it rather than propagate NaN through the optimizer.
    # - +inf (healthy math on a finite base, or any finite base * inf health)
    #   is intentionally preserved: max(inf, MIN) == inf.
    if math.isnan(price):
        return MIN_EFFECTIVE_PRICE
    return max(price, MIN_EFFECTIVE_PRICE)
