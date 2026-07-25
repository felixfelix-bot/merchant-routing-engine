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

# ── ADR-003/004: deterministic health/circuit-breaker multiplier ────────────
# A tripped circuit breaker (is_healthy=False) makes a provider unreachable
# (price = +inf). A burst of HTTP 429s (>3) applies a soft 2.0x penalty before
# the breaker fully trips.
HEALTH_429_THRESHOLD: int = 3      # strictly greater than this → penalty
HEALTH_429_PENALTY: float = 2.0


def peak_multiplier(provider: str, hour_utc: int | None = None) -> float:
    """Deterministic peak-hour step function (ADR-003).

    Returns PEAK_MULTIPLIER (3.0) when *provider* is a z.ai key AND *hour_utc*
    falls inside the peak window {6, 7, 8, 9}; 1.0 otherwise. The step is
    instantaneous at the hour boundary — no filtering.

    Args:
        provider: Provider identifier. Any value whose lower-cased form starts
            with "zai" (e.g. "zai", "zai_ours", "zai_friend") is treated as a
            z.ai key and subject to peak pricing. All other providers return
            1.0 (flat-rate cloud and per-token providers have no peak window).
        hour_utc: UTC hour of day (0-23). If None, the current UTC hour is
            used (datetime.now(timezone.utc).hour, per ADR-003 invariant #1).

    Returns:
        3.0 during z.ai peak hours, else 1.0.
    """
    if hour_utc is None:
        hour_utc = datetime.now(timezone.utc).hour
    if str(provider).lower().startswith("zai") and hour_utc in PEAK_HOURS_UTC:
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


def health_factor(is_healthy: bool, recent_429: int = 0) -> float:
    """Deterministic health/circuit-breaker multiplier (ADR-003 / ADR-004).

    Precedence (first match wins):
        1. is_healthy is False → +inf   (circuit breaker tripped: unreachable)
        2. recent_429 > 3       → 2.0    (429 burst: soft penalty)
        3. otherwise            → 1.0    (healthy)

    +inf is intentional: it is the ONLY mechanism that makes a provider
    unselectable in the routing optimizer, and compute_effective_price
    preserves it rather than flooring it (ADR-004 invariants #3, #4).

    Args:
        is_healthy: False when the provider's circuit breaker has tripped.
        recent_429: Count of recent HTTP 429 (rate-limited) responses.

    Returns:
        +inf if unhealthy, 2.0 on a 429 burst (>3), else 1.0.
    """
    if not is_healthy:
        return math.inf
    if recent_429 > HEALTH_429_THRESHOLD:
        return HEALTH_429_PENALTY
    return 1.0


def compute_effective_price(
    base_rate: float,
    provider: str,
    quota_pct: float,
    is_healthy: bool,
    recent_429: int = 0,
    hour_utc: int | None = None,
) -> float:
    """Effective price = base_rate * peak * scarcity * health (ADR-009).

    Pure deterministic composition of the three multipliers above applied to
    the (Kalman-smoothed) base rate. No state, no side effects.

    ADR-004 constraint: the result is ALWAYS strictly positive. The global
    floor MIN_EFFECTIVE_PRICE (0.001 $/M) is enforced for finite results. A
    health_factor of +inf is preserved unchanged so an unhealthy provider
    stays "unreachable" to the optimizer rather than being floored.

    Args:
        base_rate: Base $/M rate (typically from price_kalman.base_rate, or a
            provider's fixed per-token price).
        provider: Provider identifier (see peak_multiplier).
        quota_pct: Quota used percentage (see scarcity_factor).
        is_healthy: Provider health flag (see health_factor).
        recent_429: Recent 429 count (see health_factor).
        hour_utc: UTC hour 0-23; None → current UTC hour.

    Returns:
        Effective price in $/M. Always > 0: finite results are >= 0.001, and
        an unhealthy provider yields +inf (preserved, not floored).
    """
    peak = peak_multiplier(provider, hour_utc)
    scarcity = scarcity_factor(quota_pct)
    health = health_factor(is_healthy, recent_429)

    price = base_rate * peak * scarcity * health

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
