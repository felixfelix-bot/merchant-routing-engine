# Routstr / Tunnel Competitive Pricing — Definition (kanban t_277fee01, plan T-E)

**Status:** Implemented + TDD verified + pushed; pending manager review (Gate 6).
**Date:** 2026-09-07
**Author:** worker-routing (task: "routstr: competitive undercut pricing, 30% margin retained")
**Repo/branch:** `~/merchant-routing-engine` → `t_277fee01/routstr-undercut-pricing`
**Companion plan:** `docs/PLAN-routstr-serving-lane-2026-09-07.md` §5 T-E
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

### R3 — Sales lane profitable in USD, not just sats

The sales lane must be USD-profitable, not just sat-flowing. Felix's rule:

- `sat_revenue_per_req(sats, btc_price_usd)` — converts a sat sale to USD revenue.
- `is_chain_profitable_usd(sat_rev_usd, upstream_cost_usd)` — the USD cost must be covered
  by the sat revenue. Never declare a sale lane profitable on sat-margin alone.

### R4 — Chain normalization (passthrough carries no margin)

The margin is placed **once** on the selling hop; every downstream passthrough hop is
normalized to `1.0`. `normalize_chain(fees)` folds the 30% margin into a single
`public_sell` hop — the chain keeps the total product exactly `1.43`.

---

## 3. Files

| file | role |
|------|------|
| `src/routstr_chain_pricing.py` | pricing definition — pure functions + `RoutstrPricingEngine` |
| `tests/test_routstr_chain_pricing.py` | 14 tests mapping 1:1 to R1–R4 (Gate 1/2) |
| `scripts/routstr-pnl-collect.py` | P&L collector — MARGIN guard + chain product (R1/R4) |

---

## 4. Verification

- **Gate 1 (TDD):** suite observed **RED** (collection error when the module is absent) then
  **GREEN** (14 passed) once the implementation is present.
- **Gate 2 (tests):** `python3 -m pytest tests/test_routstr_chain_pricing.py` → **14 passed**
  in the isolated worktree (clean base `main`, branch `t_277fee01/routstr-undercut-pricing`).
- **Gate 3 (docs):** this document ships in the same atomic commit as the code.

---

## 5. P0-2 — Application to the live DB (2026-09-08, kanban t_2bba6329)

P0-2 in `PLAN-wire-full-provider-pool-rugpull-resilience-2026-09-08.md` applied R1–R4 to the
live routstr fleet on VPS2 (direct DB write on both routstr containers). Exact changes:

| Node | slug / setting | before | after |
|------|----------------|--------|-------|
| routstr-public | `zai-proxy` (id3, selling hop) provider_fee | 2.0 | **1.43** |
| routstr-proxy  | `zai-proxy-tunnel` (id4, passthrough) provider_fee | 1.43 | **1.0** |
| routstr-proxy  | settings blob `exchange_fee` | 1.005 | **1.0** |
| routstr-proxy  | settings blob `upstream_provider_fee` | 1.05 | **1.0** |

`routstr-public` settings blob already had `exchange_fee`/`upstream_provider_fee` = 1.0/1.0
(unchanged). Resulting rollup:

**chain = routstr-public `zai-proxy` 1.43 × routstr-proxy `zai-proxy-tunnel` 1.0
× settings (1.0 × 1.0) = 1.43** — exactly the operator-authorized 30% margin, with the
margin placed once on the selling hop and the passthrough normalized (R4).

The `zai-proxy-tunnel` provider_fee is auto-refreshed by the node's 60s
`refresh_model_maps()` task; the settings blob is applied on container (re)start, so the
friends node was restarted after the edit to reload the normalized settings.

The P&L collector (`scripts/routstr-pnl-collect.py`) was upgraded to the chain-pricing model:
- The MARGIN guard now flags **enabled provider_fee > 1.43** (over-margin, on revenue cap)
  instead of the old per-node floor of 1.27 — a 1.0 passthrough is now correctly treated as
  expected under R4, not a violation.
- A **chain summary** line is emitted computing
  `public zai-proxy fee × friends zai-proxy-tunnel fee` and asserting MARGIN OK (1.43).
- Disabled providers' fees no longer trip the guard.

Runbook: rerun `scripts/routstr-pnl-collect.py`; it reports `MARGIN OK` for both nodes and a
chain line reading `... = 1.430 ... | MARGIN OK (1.43)`.