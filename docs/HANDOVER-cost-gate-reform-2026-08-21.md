# HANDOVER: Quota Gate → Cost Gate Reform

**From:** balloon-hermes orchestrator CW (manager profile)
**To:** merchant-routing CW (proxy/pricing owner)
**Date:** 2026-08-21
**Status:** NEW WORK REQUEST — needs your planning with consultants

## The Ask (Felix, verbatim intent)

> "Can we turn the quota gate into a cost gate since the cost or price per
> token is essentially downstream of the quota anyway?"

Replace the binary, reactive quota gate with a **cost gate**: dispatch
decisions based on estimated task cost (price-per-token × predicted burn)
against a budget, instead of quota-window booleans. Rationale: quota windows
are just a proxy for cost — the exponential pressure curves already encode
quota depletion INTO price, so cost is the unified dimension. One number to
gate on, not three windows × two keys.

This matches Felix's 2026-08-19 architecture decision: **PRICE OVER
THRESHOLDS. Manager decides model, proxy decides provider via price.**

## Current State (what gates today — 27+ LLM crons)

- Gate scripts: `~/.hermes/profiles/manager/scripts/zai-quota-gate.{sh,py}`
- Logic: check proxy `/quota` → blocked iff friend key `locked` → fallback
  `zai_state.json` → fallback freeze marker `.dispatch_frozen`
- FRIEND-KEY-ONLY since 2026-08-15 (ours key dead at z.ai). PPQ intentionally
  DEAD (no auto-refill; refill trigger = friend weekly >70%).
- Binary + reactive: no prediction, no task-cost awareness, no price input.

## What Already Exists (DO NOT REBUILD)

1. **Approved design: Kalman-Gated Dispatch**
   - `~/.hermes/profiles/manager/skills/devops/zai-quota-gate/references/kalman-dispatch-gate-design.md`
   - `~/merchant-routing-engine/docs/IMPL-SPEC-kalman-dispatch-gate.md`
   - ConsumptionKalman.will_exhaust() (predicts mid-task quota death)
   - Hardware margin: 4x for board tasks, 2x software (compounding failure cost)
   - Scarcity override: hardware physically present → dispatch even at peak

2. **Pricing engine (merchant-routing-engine, all live in dev, partially wired)**
   - Exponential quota pressure: `pressure(u) = 1 + K·t/(1-t)`, asymptote 1.5,
     onset 0.60 (z.ai), superposition of session×weekly×monthly — THIS is the
     "cost downstream of quota" Felix refers to. It's built.
   - Real measured rates: Ollama glm-5.2 $0.0101–0.0155/M, PPQ $0.0197/M
     (balance-delta), DeepInfra working, OpenRouter cost-extraction broken.
   - LiveRouter wired at 4 failover call sites but DISABLED (kill-switch file
     missing — wiring plan `~/plans/liverouter-wiring-plan-2026-08-19.md`).
   - 1740+ tests pass in repo.

3. **Data:** `zai_usage.db` (api_calls with cost_usd), `api_burn.db`
   (provider_balances), provider_telemetry, 7 days of shadow tap data.

## The Gap (the actual work)

The gate family still reads `locked` booleans. Nothing connects the pricing
engine's output to dispatch decisions. Deliverable: a gate that answers
**"can I afford this task now?"** with:

```
est_cost = predicted_tokens(model, task_type) × effective_price(provider, model, quota_state, peak_hour)
allowed = (rolling_spend + est_cost × margin) < budget_cap
```

Suggested scope (yours to re-plan with consultants):
- **CG-1:** Cost-gate module in merchant-routing-engine (pure, tested).
  Inputs: model, task_type, est tokens, current pressure-adjusted price.
  Output: allow/deny/defer + reason JSON. Fail-closed on missing data.
- **CG-2:** Token predictor per (model, task_type) — seed from zai_usage.db
  historicals (p50/p90), seed-then-replace per Felix's pattern.
- **CG-3:** Budget config + rolling-spend tracker (who sets the cap? ask Felix).
- **CG-4:** Rewrite zai-quota-gate.{sh,py} to consult cost gate (keep freeze
  marker + locked-key hard block as backstop; cost gate ADDS granularity).
- **CG-5:** Migrate cron prompts QUOTA GATE line → COST GATE line (27+ crons).
- **CG-6:** Validation: fail-closed both directions, Telnyx-routed models
  (kimi-k3 pay-per-request) must bypass z.ai quota but NOT cost gate.

## Constraints & Pitfalls (hard-won)

- **Fail CLOSED on missing data** — the 2026-08-15 phantom-availability bug
  (empty fetch → "available") burned the fleet. Availability may never default
  optimistic; cost with unknown price should DENY or defer, with a manual
  override escape.
- **Telnyx models bypass z.ai quota** — gate must not block kimi-k3 on z.ai
  state, but SHOULD apply cost logic ($2.70/M in, $13.50/M out).
- **Burn-predictor convergence was RED 2026-08-15** — check
  `kalman-convergence-check` skill before trusting ConsumptionKalman
  predictions. If still red, CG-2 needs the backtest first.
- **Worker model defaults are the bigger lever** (glm-4.5-flash for routine
  tasks) — cost gate complements, doesn't replace.
- **Manager CW cron prompts embed the gate line** — any syntax change must
  sweep ALL 27+ crons (audit method in zai-quota-gate skill references).
- Cross-family review (kimi↔glm) on every PR — quality-gates skill Gate 2.5.

## Working With Consultants

- Root skills to load first: `zai-quota-gate`, `zai-pricing-reform`,
  `price-first-api-routing` (63+67 pitfalls), `kalman-convergence-check`.
- Plan review: dispatch kimi consultant on the CG plan BEFORE implementation
  (Felix's standard: plan+approval BEFORE features).
- Open questions for consultants: budget cap value + who owns it, defer vs
  deny semantics, per-CW budgets or global, how GLM-5.3 exclusivity interacts.

## Verify Before Declaring Done

1. Cost gate blocks a synthetic expensive task at simulated 95% budget.
2. Cost gate allows cheap task at same state.
3. Freeze marker still hard-blocks.
4. All cron prompts updated (grep sweep).
5. `git push` to merchant-routing-engine + production gate scripts deployed.
