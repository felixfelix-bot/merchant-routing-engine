# Merchant Routing Engine — Execution Schedule

**Date**: 2025-07-25
**Plan**: [price-first-kalman-plan.md](price-first-kalman-plan.md)
**ADRs**: [decisions.md](decisions.md) (16 ADRs) + [adr/](adr/) (7 files)
**Board**: `merchant-routing`
**Repo**: `~/merchant-routing-engine/` (PUBLIC — GitHub + ngit)

## Workers

| Profile | Model | Role |
|---------|-------|------|
| `worker-merchant` | glm-5.2 (reasoning) | Implementation — Kalman filters, pricing engine, routing optimizer |
| `worker-inspector` | kimi-k2.7-code (code specialist) | Code review — ADR compliance, correctness, test verification |

**Why these models**: Phase 1 involves Kalman filter math, linear algebra, and
architectural decisions. glm-5.2 is a reasoning model suited for this. The
inspector reviews with kimi-k2.7-code for code-quality scrutiny.

## Phase 1 — Shadow Mode Infrastructure (8 tasks)

All code lands in `~/merchant-routing-engine/`. No changes to the live proxy
until Phase 2. Shadow mode is read-only — it logs decision comparisons without
altering routing.

### Dependency Graph

```
P1.1 (providers.yaml)
  ├──→ P1.2 (price_kalman.py)
  ├──→ P1.3 (pricing_engine.py)     ──┐
  ├──→ P1.4 (consumption_kalman.py) ──┤
  │                                   ↓
  │              P1.5 (routing_optimizer.py) ← depends on P1.2 + P1.3 + P1.4
  │                    │
  │                    ↓
  │              P1.6 (shadow_logger.py) ← depends on P1.5
  │                    │
  │              P1.7 (tests) ← depends on P1.2 + P1.3 + P1.4 + P1.5
  │                    │
  │                    ↓
  └────────────→ R1.1 (code review) ← depends on P1.7
```

### Task Breakdown

| ID | Task | Assignee | Priority | Dependencies | Est. Time |
|----|------|----------|----------|--------------|-----------|
| P1.1 | Write `config/providers.yaml` — 5 providers, dynamic pricing | worker-merchant | 0 | none | 15 min |
| P1.2 | Write `src/price_kalman.py` — Base-Rate Kalman per provider | worker-merchant | 1 | P1.1 | 45 min |
| P1.3 | Write `src/pricing_engine.py` — deterministic multipliers | worker-merchant | 1 | P1.1 | 30 min |
| P1.4 | Write `src/consumption_kalman.py` — extract burn predictor | worker-merchant | 1 | P1.1 | 30 min |
| P1.5 | Write `src/routing_optimizer.py` — argmin cost minimizer | worker-merchant | 2 | P1.2, P1.3, P1.4 | 30 min |
| P1.6 | Write `src/shadow_logger.py` — read-only proxy tap | worker-merchant | 2 | P1.5 | 30 min |
| P1.7 | Write comprehensive pytest tests (≥80% coverage) | worker-merchant | 3 | P1.2, P1.3, P1.4, P1.5 | 30 min |
| R1.1 | Code review — ADR compliance, correctness, tests pass | worker-inspector | 4 | P1.7 | 20 min |

**Total estimated worker time**: ~3.5 hours

### What Each Module Does

**P1.1 — providers.yaml**: Single config file defining all 5 providers
(zai_ours, zai_friend, ollama_cloud, ppq, openrouter) with pricing models,
peak hours, backoff config, quality thresholds. Source of truth for all modules.

**P1.2 — price_kalman.py**: 2-state Kalman filter `[base_rate, rate_velocity]`.
Estimates amortized cost per 1M tokens from subscription_cost / tokens_so_far.
Smooth, slow-changing. The ONLY Kalman-smoothed component of price (ADR-009).
Per-token providers (PPQ, OpenRouter) skip Kalman — return fixed rate.

**P1.3 — pricing_engine.py**: Pure deterministic functions. No state, no Kalman.
`effective_price = base_rate × peak_mult × scarcity_mult × health_mult`.
Peak = 3.0 during UTC [6,7,8,9] (instant step, ADR-009).
Scarcity = 1.0 at 50% quota → 2.0 at 100%. Health = ∞ if circuit breaker tripped.
effective_price > 0 always (ADR-004).

**P1.4 — consumption_kalman.py**: Extracted from `~/.hermes/bot/burn_predictor.py`.
Provider-agnostic version. Predicts quota exhaustion from 12h burn history.
State: `[burn_rate, burn_velocity]`. Batch-retrained per call.

**P1.5 — routing_optimizer.py**: Deterministic argmin. Filters unhealthy/exhausted
providers, filters below quality threshold, picks cheapest effective_price.
Forward-looking: penalizes providers whose 1h-ahead price projection spikes >50%.
Returns `RouteDecision` dataclass.

**P1.6 — shadow_logger.py**: Taps the live proxy read-only. For each request,
computes what the price-first engine would have chosen. Logs both decisions to
`routing_shadow_decisions` table. Runs in background daemon thread.

**P1.7 — tests**: pytest suite for all modules. TDD mandated by quality-gates skill.
≥80% coverage. Tests for edge cases (empty history, NaN, div-by-zero).

**R1.1 — review**: worker-inspector checks ADR compliance, correctness, test results.
Writes findings to `docs/reviews/phase1-review.md`. Creates fix tasks if needed.

### Validation Gate (after R1.1)

Before Phase 2, all of these must be true:
- [ ] All 8 tasks completed
- [ ] `python3 -m pytest tests/ -v` passes with ≥80% coverage
- [ ] Code review approved by worker-inspector
- [ ] All commits pushed to GitHub + ngit
- [ ] `git status` shows clean working tree
- [ ] Shadow logger wired into proxy (read-only)
- [ ] 48h shadow data collected showing agreement rate >70%

## Phase 2 — Advisor Mode (future, not yet scheduled)

Once Phase 1 validated:
- Wire routing_optimizer into live proxy as ADVISOR (proxy calls it first, falls back to best_key() on exception)
- Remove hardcoded peak-hour check — peak becomes a price signal
- 72h validation: zero increase in failures, provider distribution shifts to cheaper

## Phase 3 — Primary Mode (future, not yet scheduled)

- Remove best_key() entirely — routing optimizer is sole router
- Delete cascade logic from zai_proxy.py
- Demand Kalman + profit tracking for Routster integration

## Phase 4 — Standalone + Routster (future, not yet scheduled)

- pip-installable package with providers.yaml config
- Merchant entry point (ProfitOptimizer) for Routster node
- Marketplace intelligence (competitor scraping, reliability tracking)
- Web-of-trust whitelist for provider vetting

## Quality Gates (every task)

Per the quality-gates skill, every task must pass:
1. TDD — test exists, observed failing before implementation
2. Tests pass — full suite run, output verified
3. Docs updated — in the same commit as code changes
4. Atomic commits — one concern per commit, conventional messages
5. PUSHED — `git push` succeeded, remote verified

Each task body includes explicit commit + push instructions:
```bash
cd /home/c03rad0r/merchant-routing-engine
git add -A && git commit -m '<conventional message>'
git push github master && ngit sync
```

## Resource Constraints

- T470: 7GB RAM, 2 cores
- Max 2 concurrent workers (resource gate enforced)
- Dispatch daemon checks resource gate + quota gate before spawning
- If resources tight, tasks queue — they don't fail

## Current Status

- **P1.1**: RUNNING (dispatch daemon picked up)
- **P1.2–P1.7, R1.1**: BLOCKED (waiting on dependencies)
- **Resource gate**: PASS (warning level)
- **Quota gate**: PASS
- **Dispatch daemon**: RUNNING (PID 5593)
