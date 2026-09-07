"""routstr_chain_pricing — Competitive undercut pricing for routstr/tunnel provider rows.

Implements the T-E definition from PLAN-routstr-serving-lane-2026-09-07:

  how we price the routstr/tunnel provider rows so we undercut other routstr nodes
  while keeping Felix's 30% margin on the CHAIN, not just the head, and mirror the
  marketplace zai/tunnel cost so the cheapest-healthy wins.

Four pricing rules, expressed as pure functions so they are unit-testable and
operators can audit a live chain against them:

  R1  Chain margin, not head margin : compounded product of per-hop fees x settings
      == 1/(1-margin). Felix's 30% margin (fee 1.43) lives on the whole chain, never
      per-hop stacked (the current live 2.0-public x 1.43-friends = 2.86x is a 65%
      margin that does NOT undercut anyone).
  R2  Undercut by mirroring market cost : offer = cheapest_healthy_upstream $/M * 1.43.
      As the flat router's cheapest-healthy drifts, our offer tracks it; we undercut
      peers who mark up a rawer/worse upstream from the same market cost.
  R3  Sales lane profitable : upstream_cost_usd < sat_revenue x btc_price at a live
      BTC/USD. Never declare a sale lane profitable on sat-margin alone.
  R4  Chain normalization : passthrough hops carry fee=1.0; the margin is placed once
      on the selling hop so the total product stays 1.43.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

MARGIN_30_PCT = 1.0 / (1.0 - 0.30)  # 30% margin ON REVENUE = fee multiplier 1.4286

# The flat-market router routes cheapest-healthy; these helpers give the SAME
# answer a price-merit fallback would, but they are NOT the live router's
# selection code — they encode the DEFINITION only. The router lives in
# flat_router.py/production/zai_proxy.py and must stay the source of truth for
# live dispatch. This module is the spec of how the provider rows are priced.


def _prod(values):
    """Product of a list of positive numbers (identity 1.0)."""
    return float(math.prod(values))


def chain_markup(fees, settings=None):
    """Compounded end-to-end markup of the routstr/tunnel chain.

    fees     : dict hop-name -> per-hop provider_fee multiplier (>= 1.0).
    settings : optional dict with 'exchange_fee' and 'upstream_provider_fee'
               (the routstr settings-blob multipliers applied per node).

    Returns the single multiplier a customer is billed for raw upstream cost.
    """
    hops = list(fees.values()) or [1.0]
    base = _prod(max(float(f), 1.0) for f in hops)
    if settings:
        base *= float(settings.get("exchange_fee", 1.0))
        base *= float(settings.get("upstream_provider_fee", 1.0))
    return base


def los_compounded(fees):
    """Return the total chain markup; > MARGIN_30_PCT flags a stacked over-margin.

    Number-named to read as "line of sight compounded" — the true customer-facing
    multiplier once every hop's fee is multiplied through.
    """
    return chain_markup(fees)


def chain_margin(fees, settings=None):
    """Margin ON REVENUE of the whole chain: (product-1)/product."""
    p = chain_markup(fees, settings)
    return (p - 1.0) / p


def undercut_price(market_cost_per_m):
    """Priced to undercut peers while keeping 30% margin: market_price * 1.43.

    market_cost_per_m is the cheapest-healthy upstream effective $/M from the
    flat router's price board (mirror the marketplace), so as the router's
    cheapest-healthy drifts down, our offer follows and we undercut peers who
    mark up a worse/rawer upstream.
    """
    return float(market_cost_per_m) * MARGIN_30_PCT


def sat_revenue_per_req(sats, btc_price_usd):
    """USD revenue from a sats sale at a live btc price."""
    return float(sats) * float(btc_price_usd) / 100_000_000.0


def is_chain_profitable_usd(sat_rev_usd, upstream_cost_usd):
    """Felix's rule: upstream_cost < sat_revenue (USD) means the lane is profitable."""
    return upstream_cost_usd < sat_rev_usd


def normalize_chain(fees):
    """Collapse a possibly-stacked chain to the 30% canonical fee (R4).

    The selling hop (the FIRST hop, i.e. the public reseller head customers see)
    carries the full 1/(1-margin) multiplier; every downstream passthrough hop is
    normalized to 1.0 so the compounded product equals exactly MARGIN_30_PCT.
    """
    if not fees:
        return {}
    order = list(fees.keys())
    out = {}
    for i, name in enumerate(order):
        out[name] = MARGIN_30_PCT if i == 0 else 1.0
    return out


def price_distribution(chain_markup=None, wholesale_markup=None):
    """Return a canonical single-sale distribution dict set to the 30% margin.

    If no explicit chain is given, defaults to a single sell hop carrying the whole
    chain at MARGIN_30_PCT (total == MARGIN_30_PCT). It carries a 'total' key so
    the operator's per-hop distribution always reconciles to the canonical 30%.
    """
    if chain_markup is None:
        return {"sale": MARGIN_30_PCT, "total": MARGIN_30_PCT}
    distro = dict(chain_markup)
    distro["total"] = _prod(max(float(v), 1.0) for v in chain_markup.values())
    return distro


@dataclass
class RoutstrPricingEngine:
    """Pricing definition for the routstr/tunnel provider rows (T-E).

    Holds the three live knobs and returns money-safe prices:

        undercut_cap     : true to price market_cost * 1.43 (default).
        btc_price_usd    : live BTC/USD for sat<->USD checks (default $95k = cost-audit anchor).
        health_filter    : pre-selected provider set; caller decides cheapest-healthy.
    """

    undercut_cap: bool = True
    btc_price_usd: float = 95_000.0
    health_filter: list = field(default_factory=list)

    def offer_per_m(self, market_cost_per_m):
        """Suggested advertised $/M for the public/tunnel row."""
        if not self.undercut_cap:
            return float(market_cost_per_m)
        return undercut_price(market_cost_per_m)

    def fee_multiplier(self, hops=None, settings=None):
        """The single chain multiplier (default single-hop 1.43)."""
        return chain_markup(hops or {"sale": MARGIN_30_PCT}, settings)

    def profitable(self, sats, upstream_cost_usd):
        """True when the sales lane clears USD profit at the configured btc price."""
        return is_chain_profitable_usd(
            sat_revenue_per_req(sats, self.btc_price_usd), upstream_cost_usd
        )