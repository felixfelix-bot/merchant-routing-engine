#!/usr/bin/env python3
"""target_pressure.py — quota target-pressure price controller (ADR-016).

The router prices each lane so that a quota window is consumed **just before it
resets**: never exhausted early (service failure), never left unused (wasted
flat-rate entitlement). This module solves for the price multiplier ``m`` that
makes projected window usage at reset land on the target (100%).

It is the control-theoretic counterpart to ``pricing_engine.pace_factor`` (a
heuristic). Callers pass the *current* burn rate (tokens/hour at m=1) plus a
demand-response model; the controller returns the multiplier to apply on top of
the existing price chain.

Demand response
---------------
Higher price ⇒ less usage. We model ``burn(m) = burn_1 * m**(-elasticity)``:
  * ``elasticity == 0`` → demand is price-insensitive (own traffic is routed by
    *other* lanes' prices, so a single lane's price barely changes own burn).
  * ``elasticity > 0``  → sold/elastic traffic (DemandKalman) shrinks as price
    rises.
Bisection handles any monotone non-increasing ``burn(m)``; the power-law is the
default because it is smooth and needs one parameter.

Windows must be anchored to the provider's real ``resets_at`` (ADR-017) — pass
``time_elapsed_frac`` computed from that anchor, not from a rolling ``now``
window. The controller never raises.
"""
from __future__ import annotations

import math

# Default bisection bounds for the multiplier (applied on top of the price chain).
M_LO_DEFAULT = 0.25   # cheapest we will price a lane (attract usage)
M_HI_DEFAULT = 8.0    # most expensive before we stop trying to shed load
TOL_DEFAULT = 1e-3
MAX_ITER_DEFAULT = 80


def burn_at(multiplier: float, burn_rate: float, elasticity: float = 0.0) -> float:
    """Demand-response burn (tokens/hour) at a given price multiplier."""
    if burn_rate <= 0.0:
        return 0.0
    m = max(multiplier, 1e-9)
    e = max(elasticity, 0.0)
    if e == 0.0:
        return burn_rate
    return burn_rate * (m ** (-e))


def projected_usage(
    multiplier: float,
    *,
    quota_used: float,
    burn_rate: float,
    time_remaining_hours: float,
    elasticity: float = 0.0,
) -> float:
    """Projected total window usage at reset for a candidate multiplier."""
    return quota_used + burn_at(multiplier, burn_rate, elasticity) * max(0.0, time_remaining_hours)


def solve_multiplier(
    *,
    quota_used: float,
    quota_total: float,
    burn_rate: float,
    time_remaining_hours: float,
    elasticity: float = 0.0,
    m_lo: float = M_LO_DEFAULT,
    m_hi: float = M_HI_DEFAULT,
    tol: float = TOL_DEFAULT,
    max_iter: int = MAX_ITER_DEFAULT,
) -> float:
    """Price multiplier that drives projected usage at reset toward 100%.

    Returns the multiplier ``m`` minimising ``|projected_usage(m) - quota_total|``,
    found by bisection (``projected_usage`` is non-increasing in ``m``).

    Degenerate cases return ``1.0`` (leave pricing unchanged):
      * no quota data / no burn rate, or the window has already elapsed;
    and clamp to the bounds when the target is unreachable:
      * even at ``m_lo`` we would under-use  → return ``m_lo`` (attract traffic);
      * even at ``m_hi`` we would over-use    → return ``m_hi`` (shed as much as
        we can, and let health/other lanes carry the rest).
    """
    try:
        if quota_total is None or quota_total <= 0.0:
            return 1.0
        if burn_rate is None or burn_rate <= 0.0:
            return 1.0
        if time_remaining_hours is None or time_remaining_hours <= 0.0:
            return 1.0  # window resets now — no pacing decision to make
        if quota_used is None:
            quota_used = 0.0
        if m_lo <= 0.0:
            m_lo = M_LO_DEFAULT
        if m_hi < m_lo:
            m_hi = m_lo

        def proj(m: float) -> float:
            return projected_usage(
                m, quota_used=quota_used, burn_rate=burn_rate,
                time_remaining_hours=time_remaining_hours, elasticity=elasticity)

        # Already at/over quota: shed hard (can only be priced out).
        if quota_used >= quota_total:
            return m_hi

        p_lo, p_hi = proj(m_lo), proj(m_hi)
        # Even the cheapest price can't reach the target → under-using; attract.
        if p_lo <= quota_total:
            return m_lo
        # Even the dearest price can't shed enough → return the ceiling.
        if p_hi >= quota_total:
            return m_hi

        lo, hi = m_lo, m_hi
        for _ in range(max_iter):
            mid = 0.5 * (lo + hi)
            if proj(mid) > quota_total:
                lo = mid          # still over target → raise price
            else:
                hi = mid          # under target → lower price
            if (hi - lo) <= tol * max(1.0, hi):
                break
        return 0.5 * (lo + hi)
    except Exception:
        return 1.0


def multiplier_for_window(
    *,
    quota_used: float,
    quota_total: float,
    burn_rate: float,
    resets_at: float,
    now: float,
    window_hours: float,
    elasticity: float = 0.0,
    **kw,
) -> float:
    """Convenience wrapper that derives the elapsed fraction from a real reset
    anchor (``resets_at``) — the ADR-017 phase-correct path."""
    try:
        wh = float(window_hours or 0.0)
        if wh <= 0.0:
            return 1.0
        time_remaining_hours = max(0.0, (float(resets_at) - float(now)) / 3600.0)
        # Clamp to the window length in case of clock skew / bad input.
        time_remaining_hours = min(time_remaining_hours, wh)
    except Exception:
        return 1.0
    return solve_multiplier(
        quota_used=quota_used, quota_total=quota_total, burn_rate=burn_rate,
        time_remaining_hours=time_remaining_hours, elasticity=elasticity, **kw)


if __name__ == "__main__":  # pragma: no cover
    print(__doc__)
