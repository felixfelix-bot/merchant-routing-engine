# Plan v2: Universal Exponential Pricing — Remaining Steps

> **Status:** APPROVED after consultant review (2026-08-05)
> **Supersedes:** docs/plan-remaining-implementation.md (v1)
> **Consultant review:** docs/plan-review-consultant.md
> **ADR:** docs/ADR-asymptote-pricing.md

## What Changed from v1

| v1 (rejected) | v2 (this plan) | Why |
|---|---|---|
| Phase 1: Wire z.ai/credit pressure (2 tasks) | DELETED — already done (1332 tests pass) | Consultant verified helpers are wired |
| 10 tasks | 8 tasks | Removed zero-work tasks |
| `_UNIVERSAL_PRESSURE_ENABLED` flag | 5 per-provider kill switches | Code already uses per-provider flags |
| Credit pressure from quota_state | DeepInfra/OpenRouter self-track via DB | Consultant found actual mechanism |
| 48h shadow mode | 7-day shadow mode | Need weekly window visibility |
| Cold start returns 1.0 | Cold start returns conservative seed | Don't under-penalize when blind |

---

## Phase 1: Balance Collectors (3 tasks, can run in parallel)

### Task 1: PPQ balance collector
**File:** `src/balance_collectors.py` (new)
**What:**
- Query PPQ API: `POST https://api.ppq.ai/credits/balance`
- Parse response, store to SQLite
- Cron-compatible: standalone script, exits after collection
- Starting balance from env `PPQ_STARTING_BALANCE` (default $20)

**Gate:** Live API returns valid balance. Stored in DB.

### Task 2: DeepInfra balance collector
**File:** `src/balance_collectors.py`
**What:**
- Query DeepInfra billing API for accumulated spending
- Compute: `remaining = starting_budget - total_spent`
- Starting balance from env `DEEPINFRA_STARTING_BALANCE` (default $5)
- Alternative: accumulate from `inference_status.cost` in per-request webhooks (if API doesn't expose totals)

**Gate:** Returns valid remaining balance or documented API limitation.

### Task 3: OpenRouter balance collector
**File:** `src/balance_collectors.py`
**What:**
- Query: `GET https://openrouter.ai/api/v1/key`
- Parse `usage` and `limit` fields
- Handle `limit=-1` (unlimited) → usage_fraction = 0
- Handle exhausted ($0 remaining) → usage_fraction = 1.0

**Gate:** Returns valid usage/limit from API.

---

## Phase 2: Cold-Start Fix + Trailing Base Rates (3 tasks)

### Task 4: Fix cold-start pressure to return conservative seed
**File:** `src/pricing_engine.py`, `src/live_router.py`
**What:**
- When no balance data exists: return usage=0.5 (conservative, not 1.0)
- When no token history exists: use ADR seed rates
- Add `COLD_START_USAGE_SEED` constant (default 0.5)

**Why:** Current code returns 1.0 (no penalty) when blind — the OPPOSITE of conservative.
**Gate:** Unit test: no-data path returns pressure > 1.0.

### Task 5: z.ai trailing-365d amortized rate
**File:** `src/real_price_tracker.py` (extend)
**What:**
- `zai_rate = 300.0 / (SUM(total_tokens WHERE provider LIKE 'zai%') / 1M)`
- Friend key: same rate × 1.21 premium
- If < 30 days of data: use seed $0.014/M
- Update daily (not per-request)

**Gate:** Calculated rate within 50% of $0.014/M with >30d data.

### Task 6: Ollama + paid endpoints trailing rates
**File:** `src/real_price_tracker.py` (extend)
**What:**
- Ollama 90d: `SUM(cost_usd) / SUM(total_tokens_in_M)` → expect ~$0.016/M
- PPQ 30d: same formula → expect ~$0.14/M
- DeepInfra 30d: same → expect ~$0.05/M
- OpenRouter 30d: same → expect ~$0.135/M
- Merge into one task (same pattern, 4 providers)

**Gate:** All rates non-zero when data exists; seeds when cold.

---

## Phase 3: Shadow Mode (1 task)

### Task 7: 7-day shadow logger with divergence analysis
**File:** `src/shadow_logger.py` (extend)
**What:**
- Enable per-provider kill switches in SHADOW mode (log, don't route)
- Log: timestamp, model, chosen endpoint (current), WOULD-BE endpoint (with pressure), prices for both
- Daily divergence report: % of routing decisions that changed
- Run for 7 DAYS (not 48h — need weekly window visibility)
- Metrics to track:
  - Divergence rate (should be <5% at low load, up to 30% at peak)
  - z.ai 429 rate (should not increase)
  - Cost delta (new pricing should be ≤ old pricing)

**Gate:** 7-day log with <5% divergence at low load, 0 increase in 429s.

---

## Phase 4: Activation (1 task)

### Task 8: Enable universal pressure + monitor
**File:** `config/providers.yaml`, `src/live_router.py`
**What:**
- Set per-provider kill switches to True (one at a time, not all at once)
- Order: Ollama first (has extra-usage safety net), then z.ai, then paid endpoints
- Monitor each for 24h before enabling next
- Rollback: set any kill switch back to False

**Gate:** 24h clean per-endpoint. No 429 spikes. No cost anomalies. Rollback path tested.

---

## Dependency Chain

```
Task 1 (PPQ balance) ─────┐
Task 2 (DeepInfra)  ──────┤
Task 3 (OpenRouter) ──────┤
                          ├──→ Task 7 (shadow) ──→ Task 8 (activate)
Task 4 (cold-start fix) ──┤
Task 5 (z.ai rate) ───────┤
Task 6 (Ollama+paid rates)┘
```

Tasks 1-6 run in PARALLEL (independent code paths).
Task 7 depends on all of 1-6.
Task 8 depends on 7.

## Risk Mitigation

| Risk | Mitigation |
|---|---|
| z.ai 429s increase | hard_limit=True trips breaker before 429 |
| Cold start under-penalizes | Task 4: conservative seed (0.5 not 1.0) |
| Superposition too aggressive | A=1.5 keeps compound manageable |
| Shadow too short | 7 days (captures weekly window) |
| Activation breaks routing | One endpoint at a time, 24h each |

## Total Tasks: 8
- Phase 1: 3 tasks (balance collectors, parallel)
- Phase 2: 3 tasks (cold-start fix + rates, parallel)
- Phase 3: 1 task (shadow mode, after 1+2)
- Phase 4: 1 task (activation, after 3)
