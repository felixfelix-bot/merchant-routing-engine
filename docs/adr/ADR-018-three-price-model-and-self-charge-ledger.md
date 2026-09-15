# ADR-018: Three-Price Model + Self-Charge (Customer-Price) Ledger

- **Status:** Accepted (operator-ratified 2026-09-15)
- **Related:** ADR-005 (three-layer actor separation), ADR-007 (routster marketplace
  intelligence), `DESIGN-routstr-sell-lane-kalman-tracking-2026-09-08.md`.

## Context

Three distinct prices are already conceptually in play, but they are not named or
versioned as one model:

1. **Routing price** — the internal market signal the router minimizes (ADR-016).
2. **Accounting cost** — our true usage cost, for efficiency study.
3. **Sale price** — what we charge customers for the flat market-based live router.

The operator wants (a) the sale price set to **maximize profit**, and (b) to **act as
an infinite-money customer** on our own router: every request is charged at the
exposed sale price for **actual tokens**, and the notional charge is recorded — as a
**system-health metric** and a **profitability** measure. This is a **dress rehearsal
for selling tokens on routstr**.

## Decision

1. **Name and version the three prices** (routing, accounting, sale). Invariants:
   effective routing price **always > 0** (ADR-004); recurring/amortized fees (e.g.
   the z.ai friend key) **are** part of cost; genuinely **sunk** costs are excluded.
2. **Sale price** maximizes `Σ demand(p)·(p − c)` — demand from `DemandKalman`,
   `c` = upstream cost from `PriceKalman`/`cost_observer`. Kept in `margin_layer`
   (ADR-005 Layer 2); exposed via `pricing_exposure` (`GET /v1/pricing`).
3. **Self-charge ledger** (`sell_ledger`): for every router request record
   `tokens_actual × exposed_sale_price`. The operator is modelled as an infinite-money
   customer (no balance constraint). Ledger columns at minimum: timestamp, model,
   lane, tokens, exposed price, notional charge, accounting cost, margin.
4. **Use**: notional revenue and margin feed (a) a system-health metric and (b)
   profitability reporting (`routstr-pnl-collect`), pre-empting real routstr sales.
5. **Dress rehearsal**: routed internal traffic exercises the same price/accounting
   path that routstr will use, so the sale lane is validated before real money.

## Consequences

- (+) One coherent price model; routing/accounting/sale cannot drift silently.
- (+) Profitability and health become observable now, without real customers.
- (+) routstr integration is de-risked (same ledger/objective).
- (−) Ledger adds a write per request (bounded; same DB as `api_calls`).
- (−) Requirements must be met (a minimum viable market); if a lane is sold under
  cost due to bad demand estimates, guard with a cost floor and alerts.
- **Risk if ignored:** no independent profitability signal; routstr pricing is
  untested until real money is at stake.

## References

- `src/margin_layer.py`, `src/profit_tracker.py`, `src/pricing_exposure.py`,
  `src/realtime_pricing.py`, `src/cost_observer.py`, `scripts/routstr-pnl-collect.py`,
  `scripts/routstrd_funding_guard.py`.
- ADR-005, ADR-007, ADR-016; plan §32; hermes `DECISIONS.md` D-144.
