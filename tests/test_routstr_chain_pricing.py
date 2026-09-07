"""Tests for routstr_chain_pricing — Verify the routstr/tunnel chain pricing definition.

Covers (per PLAN-routstr-serving-lane-2026-09-07 T-E and routstr-node-ops skill):

  R1. Chain margin, not head margin: the 30% margin (fee 1.43) is composed over the
      ENTIRE chain product of per-hop fees x settings multipliers, so the billed
      multiplier equals 1/(1-margin) regardless of how many hops exist. Stacks are
      detected and reported, never silently accepted.
  R2. Undercut by mirroring market cost: offered price = market_cost x 1.43 where
      market_cost is the cheapest-healthy upstream effective cost. This is what lets
      us undercut peers (who mark up a worse/rawer upstream) while keeping 30%.
  R3. Sales lane profitable: upstream_cost_usd_per_req < sat_revenue_per_req x btc_price.
      A chain whose compounded fee multiplies raw cost to less than 1.43 is a loss
      (margin-on-revenue guard). Live, not static.
  R4. Chain normalization: intermediate passthrough hops carry fee=1.0 (no margin);
      margin lives once on the selling hop so the total product preserves 1.43.

These are TDD; the implementation in src/routstr_chain_pricing.py only lands after
this suite demonstrably fails (RED) then passes (GREEN).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.routstr_chain_pricing import (
    MARGIN_30_PCT,
    chain_markup,
    los_compounded,
    chain_margin,
    undercut_price,
    sat_revenue_per_req,
    is_chain_profitable_usd,
    price_distribution,
    normalize_chain,
)


# ---- R1: margin formula provenance (fee = 1/(1-margin), margin on revenue) ----


def test_margin_30_pct_produces_fee_143():
    """30% margin ON REVENUE implies fee = 1/(1-0.30) = 1.43 (Felix's rule, verified)."""
    assert MARGIN_30_PCT == pytest.approx(1.4286, abs=1e-3)
    assert (MARGIN_30_PCT - 1) / MARGIN_30_PCT == pytest.approx(0.30, abs=1e-6)


def test_single_hop_public_fee_is_143():
    """A single sell hop carrying the full margin must be 1.43, not 2.0."""
    hops = {"public": 1.43}
    assert chain_markup(hops) == pytest.approx(1.43, abs=1e-6)


# ---- R1: chain (compounded) not head ----


def test_chain_markup_compounds_settings_and_hops():
    """Total chain markup = product of per-hop fees x settings multipliers."""
    hops = {"public": 2.0, "friends": 1.43}
    settings = {"exchange_fee": 1.005, "upstream_provider_fee": 1.05}
    # Current live chain: 2.0 x 1.43 x 1.005 x 1.05 ~= 3.018
    total = chain_markup(hops, settings)
    assert total == pytest.approx(2.0 * 1.43 * 1.005 * 1.05, rel=1e-6)
    assert total > MARGIN_30_PCT  # the stacked chain is OVER the 30% margin


def test_chain_margin_equals_30_only_when_product_is_143():
    """Margin on revenue of the CHAIN is (product-1)/product; 1.43 implies 30%."""
    # Single canonical hop at exactly MARGIN_30_PCT == 30% margin
    assert chain_margin({"public": 1.0 / 0.7}) == pytest.approx(0.30, abs=1e-9)
    # stacked 2.86 chain is NOT 30%: (2.86-1)/2.86 = 65%
    assert chain_margin({"public": 2.0, "friends": 1.43}) == pytest.approx(0.6503, abs=1e-3)


def test_los_compounded_flag_over_margin():
    """A two-hop chain compounding above 1.43 must be flagged as over-margin."""
    assert los_compounded({"public": 1.43, "friends": 1.43}) > MARGIN_30_PCT


# ---- R2: undercut by mirroring market cost ----


def test_undercut_price_is_market_cost_times_143():
    """Offer price = market_cost x 1.43; margin-on-revenue holds automatically."""
    market_cost = 0.45
    price = undercut_price(market_cost)
    assert price == pytest.approx(market_cost * MARGIN_30_PCT, rel=1e-6)
    assert (price - market_cost) / price == pytest.approx(0.30, rel=1e-6)


def test_undercut_beats_a_raiser_peer():
    """If a peer marks up the same market cost by 2.0, we are cheaper."""
    market_cost = 0.45
    peer = market_cost * 2.0
    ours = undercut_price(market_cost)
    assert ours < peer


def test_undercut_mirrors_cheapest_healthy():
    """Offer price should track the cheapest-healthy upstream, not a fixed float."""
    cheap, dear = 0.096, 0.53  # Chutes vs ollama_cloud glm-5.2 (cost audit)
    assert undercut_price(cheap) < undercut_price(dear)


# ---- R3: sales lane profitability (sat vs USD) ----


def test_sat_revenue_conversion():
    """sats -> USD at a live btc price."""
    assert sat_revenue_per_req(sats=1000, btc_price_usd=95_000.0) == pytest.approx(0.95)
    assert sat_revenue_per_req(sats=0, btc_price_usd=95_000.0) == 0.0


def test_sales_lane_profitable_when_usd_covered():
    """upstream_cost < sat_revenue x btc -> profitable (Felix's rule)."""
    assert is_chain_profitable_usd(
        sat_revenue_per_req(45.6, 95_000.0), upstream_cost_usd=0.0
    ) is True


def test_sales_lane_unprofitable_when_usd_exceeds_sat():
    """Raw cost exceeding sat revenue is USD-negative regardless of sat margin."""
    assert is_chain_profitable_usd(
        sat_revenue_per_req(45.6, 95_000.0), upstream_cost_usd=0.10
    ) is False


# ---- R4: chain normalization (passthrough carries no margin) ----


def test_passthrough_hop_carries_fee_1():
    """Intermediate passthrough hop normalized to fee=1.0 keeps total == 1.43."""
    hops = {"sell": 1.43, "pass": 1.0}
    assert chain_markup(hops) == pytest.approx(1.43, rel=1e-6)


def test_normalize_chain_keeps_total_143():
    """normalize_chain folds margin into a single sell hop, passthroughs to 1.0."""
    chain = {"public_sell": 2.0, "friends_pass": 1.43, "tunnel": 1.0}
    norm = normalize_chain(chain)
    assert chain_markup(norm) == pytest.approx(MARGIN_30_PCT, rel=1e-6)
    # public_sell (head) carries the margin; downstream hops become 1.0
    assert norm["public_sell"] == pytest.approx(MARGIN_30_PCT, rel=1e-9)
    assert norm["friends_pass"] == pytest.approx(1.0)
    assert norm["tunnel"] == pytest.approx(1.0)


def test_price_distribution_total():
    """price_distribution composes per-hop fees without duplicate margin."""
    distro = price_distribution()
    assert distro["total"] == pytest.approx(MARGIN_30_PCT, rel=1e-6)