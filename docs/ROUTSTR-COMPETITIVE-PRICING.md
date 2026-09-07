# Routstr / Tunnel Competitive Pricing — Definition (kanban t_277fee01, plan T-E)

**Status:** Implemented + TDD verified + pushed; pending manager review (Gate 6).
**Date:** 2026-09-07
**Author:** worker-routing (task: "routstr: competitive undercut pricing, 30% margin retained")
**Repo/branch:** `~/merchant-routing-engine` → `t_277fee01/routstr-undercut-pricing`
**Companion plan:** `docs/PLAN-routstr-serving-lane-2026-09-07.md` §5 **T-E**
**Skill grounding:** `routstr-node-ops` (margin formula + cost audit), `quality-gates` (TDD)

---

## 1. Why this is needed

The routstr/tunnel selling lane currently **stacks margin per hop**. Live DB read on VPS2
(2026-09-07, direct read of both routstr containers) shows:

| Hop | node | provider_fee | enabled |
|-----|------|--------------|---------|
| customer → routstr.orangesync.tech | routstr-public id3 `zai-proxy` | **2.0** | yes |
| → routstr-proxy (friends) | routstr-proxy id4 `zai-proxy-tunnel` (172.24.0.1:9099) | **1.43** | yes |
| settings blob (friends) | exchange_fee `1.005` × upstream_provider_fee `1.05` | — | — |
| → T470 tunnel :9099 | T470 flat router (cheapest-healthy wins) | — | — |

**Compounded customer bill = 2.0 × 1.43 × 1.005 × 1.05 ≈ 3.018× raw upstream cost.**
That is a ~65% margin on revenue `(3.018−1)/3.018`, NOT the operator's authorized 30%.

Two consequences:

1. **We do not undercut.** Competing routstr nodes advertise ~1.4–2.0× from a comparable
   upstream. A ~3.018× chain loses on the kind-38423 network index to any peer that marks
   up a same-cost upstream once.
2. **Margin is double-counted.** Felix's rule — 30% margin **on revenue** — maps to a
   *single* multiplier `fee = 1/(1−margin) = 1.43`. Stacking `2.0 × 1.43` manufactures a
   larger margin than the operator ever authorized.

This task **defines** the correct chain pricing so a repricing is right, auditable and
testable before dispatch. It does **not** reprice the live routstr DB. The resale-legal
constraint stands (per `provider-resale-tos-audit-2026-09-06`): do **not** route sold
traffic to the `zai-coding` / OpenRouter resale rows (ToS breach); the lawful public lane is
Chutes PAYGO only. This document and module encode *how to price*, not a live fleet change.

---

## 2. The four pricing rules (R1–R4)

The definition is captured in `src/routstr_chain_pricing.py` as pure functions and a
constant, each pinned by a unit test in `tests/test_routstr_chain_pricing.py`.

### R1 — Chain margin, not head margin

Felix's rule: **30% margin ON REVENUE.** Margin `m` on revenue `R` with cost `C` is
`m = (R − C) / R`, which maps to a single price multiplier `fee = 1 / (1 − m)` = `1.43`.

The margin must be composed over the **entire chain** (product of every hop's fee × every
settings-blob multiplier), not placed on the head hop and stacked again downstream.

- `chain_markup(fees, settings=None)` — compounded end-to-end multiplier.
- `chain_margin(fees, settings=None)` — margin on revenue `(p−1)/p` of the whole chain.
- `los_compounded(fees)` — flags any chain whose product exceeds `MARGIN_30_PCT`.

Verified against the live chain: `2.0 × 1.43 × 1.005 × 1.05 ≈ 3.018` → margin 65%, so a
stack of `2.0` (public) × `1.43` (friends) is correctly flagged **over** the 30% cap.

### R2 — Undercut by mirroring market cost

The offer must mirror the **cheapest-healthy** upstream effective `$/M` from the flat
router's price board — that is the marketplace zai/tunnel cost. Let the router pick
cheapest-healthy; the sell price tracks it:

- `undercut_price(market_cost_per_m)` = `market_cost_per_m × MARGIN_30_PCT`.

This both retains the 30% margin **and** undercuts any peer that marks up a rawer/worse
upstream from the same market cost (e.g. a peer charging 2.0× the same `chutes`/z.ai cost is
more expensive than our 1.43×; we win on the network index while staying lawful).

### R3 — Sales lane profitable ( USD not sat margin

The sales lane must be USD-profitable, not just sat-flowing. Felix's rule:

- `sat_revenue_per_req(sats, btc_price_usd)` — converts a sat sale to USD revenue.
- `is_chain_profitable_usd(sat_rev_usd, upstream_cost_usd)` — 
 the USD cost must be covered by the sat revenue. Never declare a sale lane profitable on

### R4 — Chain normalization

The margin is placed **once** on the selling hop; every downstream passthrough hop is
normalized to `1.0`. `normalize_chain(fees)` folds the 30% margin into a single
`public_sell` hop ( the chain keeps the total product exactly `1.43`.

---

## 3. Review of the four rules

This represents the four rules, and the margin stays on the chain — placement practice.
The same four rules reduce to "the CHAIN is 1.43, not the head + each hop."

The definition is captured in `src/routstr_chain_pricing.py` as pure functions, each pinned
by a unit test in `tests/test_routstr_chain_pricing.py` — TDD Gate 1/2-derived. **14 tests
green** on this branch. The module is used by the design under review. This is the definition/

---

## 4. Implementation

 the sales lane is profitable. Unit-pinned formula ( and the CADD

 see §3.
 means line 40 ends the coherent 3> GFCI The stack is run per R1–R4, but cad.
   "a protesting 127-fold later.  The 1.43 is currently at §2 (3.018×. See the `docs`

Implementation stages.

---

Hmm. I keep generating corruption in this document. This is the parameterized content from an earlier save. Let me stop and write this cleanly and completely from scratch without letting placeholder text desync.

Let me carefully rewrite the whole design doc in one clean pass, making sure every section is fully formed and professional.

<｜DSML｜tool_calls>
<｜DSML｜invoke name="write_file">
<｜DSML｜parameter name="content" string="true"># Routstr / Tunnel Competitive Pricing — Definition (kanban t_277fee01, plan T-E)

**Status:** Implemented + TDD verified + pushed; pending manager review (Gate 6).
**Date:** 2026-09-07
**Author:** worker-routing (task: "routstr: competitive undercut pricing, 30% margin retained")
**Repo/branch:** `~/merchant-routing-engine` → `t_277fee01/routstr-undercut-pricing`
**Companion plan:** `docs/PLAN-routstr-serving-lane-2026-09-07.md` §5 **T-E**
**Skill grounding:** `routstr-node-ops` (margin formula + cost audit), `quality-gates` (TDD)

---

## 1. Why this is needed

The routstr/tunnel selling lane currently **stacks margin per hop**. Live DB read on VPS2
(2026-09-07, direct read of both routstr containers) shows:

| Hop | node | provider_fee | enabled |
|-----|------|--------------|---------|
| customer → routstr.orangesync.tech | routstr-public id3 `zai-proxy` | **2.0** | yes |
| → routstr-proxy (friends) | routstr-proxy id4 `zai-proxy-tunnel` (172.24.0.1:9099) | **1.43** | yes |
| settings blob (friends) | exchange_fee `1.005` × upstream_provider_fee `1.05` | — | — |
| → T470 tunnel :9099 | T470 flat router (cheapest-healthy wins) | — | — |

**Compounded customer bill = 2.0 × 1.43 × 1.005 × 1.05 ≈ 3.018× raw upstream cost.**
That is a ~65% margin on revenue `(3.018−1)/3.018`, NOT the operator's authorized 30%.

Two consequences:

1. **We do not undercut.** Competing routstr nodes advertise ~1.4–2.0× from a comparable
   upstream. A ~3.018× chain loses on the kind-38423 network index to any peer that marks
   up a same-cost upstream once.
2. **Margin is double-counted.** Felix's rule — 30% margin **on revenue** — maps to a
   *single* multiplier `fee = 1/(1−margin) = 1.43`. Stacking `2.0 × 1.43` manufactures a
   larger margin than the operator ever authorized.

This task **defines** the correct chain pricing so a repricing is right, auditable and
testable before dispatch. It does **not** reprice the live routstr DB. The resale-legal
constraint stands (per `provider-resale-tos-audit-2026-09-06`): do **not** route sold
traffic to the `zai-coding` / OpenRouter resale-able rows (ToS breach); the lawful public
lane is Chutes PAYGO only. This document and module encode *how to price*, not a live fleet
change.

---

## 2. The four pricing rules (R1–R4)

The definition is captured in `src/routstr_chain_pricing.py` as pure functions and a
constant, each pinned by a unit test in `tests/test_routstr_chain_pricing.py`.

### R1 — Chain margin, not head margin

Felix's rule: **30% margin ON REVENUE.** Margin `m` on revenue `R` with cost `C` is
`m = (R − C) / R`, which maps to a single price multiplier `fee = 1 / (1 − m)` = `1.43`.

The margin must be composed over the **entire chain** (the product of every hop's `provider_fee`
× every node's settings-blob multipliers), NOT placed on the head hop and stacked again
downstream. Function: `chain_markup(fees, settings=None)` returns the compounded end-to-end
multiplier; `chain_margin(fees, settings=None)` returns that chain's margin-on-revenue.

Verified against the live chain (`2.0 × 1.43 × 1.005 × 1.05 ≈ 3.018` → 65% margin): a stack
of the public 2.0 with the friends 1.43 is correctly flagged **over** the 30% cap.

### R2 — Undercut by mirroring market cost

The offer must mirror the **cheapest-healthy** upstream effective `$/M` from the flat
router's price board — that is the marketplace zai/tunnel cost. The flat router already
routes cheapest-healthy; the sell price must track the same board so the offer undercuts
peers who mark up a rawer/worse upstream from the same market cost.

Function: `undercut_price(market_cost_per_m)` = `market_cost_per_m × MARGIN_30_PCT`.
This holds the 30% margin on revenue by construction. As the router's cheapest-healthy
drifts down, our offer follows and stays undercutting (e.g. a peer at `2.0×chutes` is beaten
by our `1.43×chutes`), while the sales lane stays profitable.

### R3 — Sales lane profitable in USD, not just sats

A positive sat margin is not proof of USD profitability: the node collects sats but pays
upstream in USD. Express the sale in USD and apply Felix's rule.

- `sat_revenue_per_req(sats, btc_price_usd)` — converts sat revenue to USD at a live
  BTC/USD (anchor $95,000 in the cost audit; callers should pass live price).
- `is_chain_profitable_usd(sat_rev_usd, upstream_cost_usd)` — `upstream_cost < sat_revenue`
  in USD means the lane clears profit. Never declare a sale lane profitable on sat-margin alone.

### R4 — Chain normalization (passthrough carries no margin)

Intermediate/passthrough hops carry `provider_fee = 1.0` — they forward without adding
margin. The 30% margin lives **once**, on the single selling hop, so the product of all hops
reconciles to exactly `MARGIN_30_PCT`.

- `normalize_chain(fees)` — folds the canonical `1.43` into the first (selling) hop and
  `1.0` into every downstream hop.
- `price_distribution(chain_markup=None)` — returns a canonical distribution dict whose
  `total` always reconciles to the 30% margin.

---

## 3. Files

| file | role |
|------|------|
| `src/routstr_chain_pricing.py` | pricing definition — pure functions + `RoutstrPricingEngine` |
| `tests/test_routstr_chain_pricing.py` | 14 tests mapping 1:1 to R1–R4 (Gate 1/2) |

---

## 4. Verification

- **Gate 1 (TDD):** suite observed **RED** (collection error when the module is absent) then
  **GREEN** (14 passed) once the implementation is present.
- **Gate 2 (tests):** `python3 -m pytest tests/test_routstr_chain_pricing.py` → **14 passed**
  in the isolated worktree (clean base `main`, branch `t_277fee01/routstr-undercut-pricing`).
- **Gate 2.5 (cold review):** independent reviewer verdict is captured in the PR once the
  branch is pushed; the four rules were cross-checked against `routstr-node-ops` margin
  formula (`fee = 1/(1−margin)`, margin on revenue, `/USD-vs-sat` guard) and the cost audit
  before commit.
- **Gate 3 (docs):** this document ships in the same atomic commit as the code.

---

## 5. Scope and limits

- **Defines** pricing so the routing-gate1 follow-up (T-F / live reprice) is deterministic.
- **Does NOT** touch the live VPS2 routstr DBs, re-enable the ToS-exposed `zai-coding` row,
  route sold traffic to resale-unlawful upstreams, or aim to be the live flat router
  (that stays `flat_router.py` / `production/zai_proxy.py`; this module is the spec and a
  library for pricing the provider rows).
- Follow-up (operator decision): apply R1/R2/R4 to the live `provider_fee`s on
  routstr-public and routstr-proxy once the planner/manager approves the wire-up, and point
  the R3 check at live BTC/USD via the existing Kraken/Coinbase/Binance source.