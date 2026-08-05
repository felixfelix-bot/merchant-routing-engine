# Consultant Review: Implementation Plan for Universal Exponential Pricing

> **Reviewer:** Pricing systems consultant
> **Date:** 2026-08-05
> **Plan reviewed:** `docs/plan-remaining-implementation.md`
> **Code state verified:** commit `facc977` (HEAD at review time)
> **Test suite:** 1332 passed, 0 skipped, 0 failed (20.26s)

---

## 1. Plan Assessment (PASS/FAIL/REVISE)

### **REVISE** — the plan is structurally sound but factually out of date by several commits.

The plan's "What's Done" / "What's Not Done" split is **wrong for Phase 1**. Tasks 1A and 1B describe work that is **already implemented and tested**. The plan claims "4 skipped tests (pending wiring)" but the actual test suite shows **0 skipped, 1332 passing** — the four named tests (`test_price_rises_with_usage`, `test_100_pct_trips_breaker`, `test_superposition_in_integration`, `test_deepinfra_pressure_rises_with_spend`, `test_exhausted_openrouter_is_inf`) all exist in `tests/test_universal_pressure.py` and **pass**. They use `monkeypatch.setattr(lr, "_ZAI_QUOTA_PRESSURE_ENABLED", True)` to activate the code path — the wiring is live, just gated behind env-flag kill switches that default to `false`.

This is not a cosmetic error: it means **2 of the 10 tasks are zero-work**, the dependency chain's Phase 1→Phase 4 link is already satisfied, and the "Estimated Tasks: 10" is actually 8. Felix will waste kanban slots on phantom work if this is scheduled as-is.

The plan's Phase 2–3 architecture (balance collectors + trailing base rates) is sound and actionable. Phase 4 references a flag that does not exist. Fix the factual errors, re-scope to 8 tasks, and this is schedulable.

---

## 2. Critical Issues (must fix before scheduling)

### C1. Phase 1 is already done — the wiring exists and all tests pass

**Evidence:** `src/live_router.py`, `_do_select_failover()` lines 963–1019:
- Line 970: `if name in ("ours", "friend") and _ZAI_QUOTA_PRESSURE_ENABLED:` → calls `_compute_zai_pressure(qs)`
- Line 982: `if name == "ppq" and _PPQ_QUOTA_PRESSURE_ENABLED:` → calls `_compute_ppq_pressure(qs)`
- Line 995: `if name == "openrouter" and _OPENROUTER_CREDIT_PRESSURE_ENABLED:` → calls `_compute_credit_pressure(...)`
- Line 1008: `if name == "deepinfra" and _DEEPINFRA_CREDIT_PRESSURE_ENABLED:` → calls `_compute_credit_pressure(...)`

The helpers are called, the pressure factors multiply the base rate, and `+inf` results trip the breaker (`healthy = False`). The 5 named tests in `test_universal_pressure.py` exercise this end-to-end and pass.

**Action:** Delete Phase 1 tasks 1A and 1B. Replace with a single task: "Enable per-provider kill switches in production env (set the 5 `*_ENABLED=true` flags)". Or fold this into Phase 4B.

### C2. Task 4B references `_UNIVERSAL_PRESSURE_ENABLED` — this flag does not exist

The plan says: *"Set `_UNIVERSAL_PRESSURE_ENABLED = True`"*. There is no such flag anywhere in the codebase (`grep -rn '_UNIVERSAL_PRESSURE_ENABLED'` → 0 hits). The actual kill switches are **five separate per-provider flags**:

```python
_QUOTA_PRESSURE_ENABLED             # Ollama (OLLAMA_QUOTA_PRESSURE_ENABLED)
_ZAI_QUOTA_PRESSURE_ENABLED          # z.ai   (ZAI_QUOTA_PRESSURE_ENABLED)
_PPQ_QUOTA_PRESSURE_ENABLED          # PPQ    (PPQ_QUOTA_PRESSURE_ENABLED)
_OPENROUTER_CREDIT_PRESSURE_ENABLED  # OpenRouter
_DEEPINFRA_CREDIT_PRESSURE_ENABLED   # DeepInfra
```

All default to `false`. **Action:** Rewrite task 4B to flip these five env vars, not a phantom sixth flag.

### C3. Task 1B describes the wrong data source for DeepInfra/OpenRouter pressure

The plan says: *"Compute `balance_usage = 1 - (remaining / starting_budget)` from `quota_state`"* for credit-based providers. This is true **only for PPQ** (`_compute_ppq_pressure` reads `used_pct` from `quota_state`).

For **DeepInfra and OpenRouter**, the code does NOT read `quota_state` at all. It calls `_compute_credit_pressure(db_path, key_name, starting_balance, ...)` which queries `SUM(cost_usd) FROM api_calls WHERE key_name = ?` directly. The `quota_state` dict is bypassed entirely. If a worker implements task 1B as literally written (populating `quota_state["deepinfra"]["used_pct"]`), the value will be **silently ignored**.

**Action:** Correct task 1B (or its successor) to describe the actual mechanism: DeepInfra/OpenRouter pressure is self-tracked from `api_calls.cost_usd`. The collector work (Phase 2B/2C) must ensure `cost_usd` is logged per request, not that a separate "balance" value is injected into `quota_state`.

### C4. PPQ cold-start returns 1.0 (optimistic), contradicting the ADR's seed of 0.5

The ADR (§Cold-Start Seeding) specifies: *"For paid endpoints with no balance data yet, seed `balance_usage = 0.5` (conservative)."* The code does the **opposite**: `_compute_ppq_pressure` returns `1.0` (no penalty) when `used_pct` is `None`, and `_compute_credit_pressure` returns `1.0` when the DB has no spend rows (because `spend=0 → remaining=starting → u=0`).

This means on a cold start, a PPQ/DeepInfra/OpenRouter endpoint with an actually-depleted balance will appear **cheaper than it should** until the first collector query completes. For OpenRouter (known-exhausted, $0 balance) this is especially dangerous: if the DB has no `openrouter` spend rows yet, `_compute_credit_pressure` returns 1.0, and the optimizer may route traffic to a dead endpoint.

**Action:** Either (a) change the cold-start return from 1.0 to a conservative value (ADR-aligned), or (b) explicitly document the deviation and add a startup "prefetch" step in Phase 2 so the first balance query completes before routing begins. The current code's optimism is an unreviewed deviation from the accepted ADR.

### C5. Stale comments contradict the A=1.5 decision

Two locations carry stale asymptote values that contradict the accepted ADR:
- `src/live_router.py` line 811: `# FELIX DECISION (Aug 5): uniform asymptote 5.0 for ALL quota endpoints.` → should say 1.5
- `src/pricing_engine.py` line 737 (docstring): `hard_limit=True, asymptote=2.0` → should say 1.5

These are comments/docstrings, not logic, but they will mislead the next developer. The ADR itself flags the second one (§Negative/Risks). **Action:** Fix both as a zero-cost add-on to the first task scheduled.

---

## 3. Recommended Changes

### R1. Collapse Phase 1 into Phase 4's activation step

Since the wiring is done, remove tasks 1A/1B as standalone kanban items. The remaining work is: (a) flip the env flags, (b) verify in shadow mode, (c) monitor. This is purely Phase 4. The revised task count drops from 10 → 8.

### R2. Re-sequence: build balance collectors BEFORE enabling pressure

The plan's dependency graph shows Phases 1/2/3 in parallel, with Phase 4 gating on all. But if you enable z.ai/PPQ pressure (Phase 1, already wired) without the PPQ balance collector (Phase 2A), the PPQ pressure will always return 1.0 (cold-start optimism, see C4). The optimizer will treat PPQ as perpetually fresh even if credits are low. **Recommended ordering:**

```
Phase 2 (collectors) + Phase 3 (base rates)  ← build first, in parallel
         ↓
Phase 4A (shadow mode, all 5 flags ON)
         ↓
Phase 4B (activate: flip flags to true in production env)
```

This ensures all balance data feeds are live before the pressure curves are exercised.

### R3. Split Phase 2A (PPQ) from 2B/2C (DeepInfra/OpenRouter) by mechanism

The plan groups all three collectors under `src/balance_collectors.py`, but they have fundamentally different data paths:
- **PPQ:** needs a real API call (`POST /credits/balance`) that returns a balance, which then populates `quota_state["ppq"]["used_pct"]`.
- **DeepInfra/OpenRouter:** no balance API needed — the existing `_compute_credit_pressure` already self-tracks from `SUM(cost_usd)`. The "collector" work here is ensuring `cost_usd` is logged to `api_calls` on every request (which may already be done by the cost_extraction pipeline — verify before building).

Treat 2A as "API integration" and 2B/2C as "cost-logging verification" — they are different types of work.

### R4. Use `real_price_tracker.get_real_rate()` for base rates — it already exists

Phase 3 says "extend `src/real_price_tracker.py`", but `get_real_rate(provider, model, window_hours)` already exists and does exactly `SUM(cost_usd)/SUM(total_tokens)*1e6` over a trailing window. Tasks 3A/3B/3C are essentially one-line calls:
- 3A: `get_real_rate("ours", window_hours=365*24)` (with the $300/yr amortization applied on top)
- 3B: `get_real_rate("ollama_cloud", window_hours=90*24)`
- 3C: `get_real_rate("ppq", window_hours=30*24)`, etc.

The real work is **wiring the result into `_DEFAULT_CONVERGED_RATES`** (the hardcoded dict at `live_router.py` line 186) and the `LiveRouter.__init__` rate override. Consider merging 3A/3B/3C into a single "dynamic base rates" task.

---

## 4. Missing Steps

### M1. No step to populate `quota_state["ppq"]["used_pct"]` from the PPQ collector

The plan's task 2A says the collector writes to `~/.hermes/bot/api_burn.db` table `provider_balance`. But `_compute_ppq_pressure` reads from the `quota_state` dict passed to `select_failover()`, not from `api_burn.db`. There is a **missing bridge**: something must read the balance from the DB and inject it into the `quota_state` dict before `select_failover` is called. This is production-proxy-side wiring that the plan does not mention. (Contrast: DeepInfra/OpenRouter read the DB directly, so they don't need this bridge.)

### M2. No step to verify that `cost_usd` is actually being logged for DeepInfra/OpenRouter

The entire DeepInfra/OpenRouter pressure mechanism depends on `api_calls.cost_usd` being populated. If the cost-extraction pipeline (`src/cost_extraction.py`) doesn't log costs for these providers, `_compute_credit_pressure` will always return 1.0. **Add a verification task:** confirm `cost_usd IS NOT NULL` for `key_name IN ('openrouter', 'deepinfra')` in the production DB.

### M3. No rollback procedure beyond "set flag to false"

Task 4B says *"Rollback = set flag to False."* But with 5 separate flags, the rollback procedure needs to list all 5. Also, if the env flags are set in a systemd unit or Docker compose file, the rollback requires a service restart. Document the exact rollback command(s).

### M4. No step to fix the stale `scarcity_factor` double-counting for activated providers

The code at line 1035 sets `prov_quota_total = None` when a provider has pressure, which neutralizes `scarcity_factor` for that provider. Good. But this only works if `prov_has_pressure` evaluates correctly. Verify that when all 5 flags are ON, scarcity is neutralized for ALL 6 providers (z.ai ours, z.ai friend, ollama_cloud, ppq, openrouter, deepinfra). There's no test for this cross-cutting behavior.

### M5. No integration test for the full "all flags ON" routing scenario

The existing tests enable one or two flags at a time. There is no test that enables all 5 simultaneously and verifies the full cascade (z.ai → ollama → friend → DeepInfra → OpenRouter → PPQ). **Add this as a gate for Phase 4B.**

---

## 5. API Verification

### PPQ (`POST https://api.ppq.ai/credits/balance`)

| Check | Status | Notes |
|---|---|---|
| URL base | ⚠️ **Ambiguous** | `config/providers.yaml` lists `base_url: "https://api.ppq.ai/v1"`, but the plan and ADR say `POST /credits/balance` without the `/v1` prefix. Is the real URL `https://api.ppq.ai/v1/credits/balance` or `https://api.ppq.ai/credits/balance`? **Verify before implementing.** |
| Auth | ⚠️ **Underspecified** | Plan says `PPQ_CREDIT_ID`, `PPQ_API_KEY` from env. What header? `Authorization: Bearer`? Custom? The DQ05 monitor's `dq05_ppq` tool uses `POST /credits/balance` — check its implementation for the real auth method. |
| Response format | ❌ **Inconsistent** | Plan says `{"balance": 12.50, "currency": "USD"}`. ADR says it returns `remaining` (credits in $). Which field name? `balance` or `remaining`? The `_compute_ppq_pressure` code reads `used_pct` (0-100), not a dollar balance — so there's a unit conversion gap not described anywhere. |

### DeepInfra (`GET https://api.deepinfra.com/v1/user/usage`)

| Check | Status | Notes |
|---|---|---|
| URL | ⚠️ **Speculative** | The plan hedges: *"GET .../v1/user/usage or: query total_spent from billing endpoint."* Two different endpoints mentioned, neither confirmed. The ADR's analysis doc says "Has billing endpoints at https://deepinfra.com" with no path. **The worker cannot implement this without knowing the real endpoint.** |
| Auth | Not specified | The code already self-tracks via `SUM(cost_usd)`, making this API call **optional**. Recommend: skip the API, ensure `cost_usd` is logged, and use the existing `_compute_credit_pressure` path. |
| Response format | Unknown | DeepInfra's billing API response shape is not documented in any file in this repo. |

### OpenRouter (`GET https://openrouter.ai/api/v1/key`)

| Check | Status | Notes |
|---|---|---|
| URL | ✅ **Correct** | `config/providers.yaml` has `base_url: "https://openrouter.ai/api/v1"`, and `/key` is a documented OpenRouter endpoint. Full URL: `https://openrouter.ai/api/v1/key`. |
| Auth | ✅ **Standard** | `Authorization: Bearer $OPENROUTER_API_KEY` (standard OpenRouter auth). |
| Response format | ⚠️ **Verify field names** | Plan says `{"data": {"usage": 0.50, "limit": 10.00}}`. OpenRouter's `/key` endpoint returns `{"data": {"label": ..., "usage": ..., "limit": ..., "is_free_tier": ...}}`. Field names look correct but the `limit` field returns `-1` when unlimited, which would break `remaining = limit - usage`. **Handle the unlimited case.** |
| Currently exhausted | ℹ️ | Code comment says OpenRouter is at $0 balance. If `usage >= limit`, pressure = +inf, endpoint excluded. Expected behavior, but the collector should handle this gracefully (not error). |

---

## 6. Cold Start Assessment

### Is the seed-rate fallback sound? **Partially — with a significant gap.**

**What the plan says (task 3C):** "If no data: use seed rates (PPQ=$0.14, DeepInfra=$0.05, OpenRouter=$0.135)." These are **base rate** seeds — they tell the optimizer what to charge per token. This part is sound: the seeds match the ADR's cost table and are conservative (real measured rates will likely be close).

**What the plan does NOT address: the `balance_usage` (pressure input) cold start.** The ADR specifies seeding `balance_usage = 0.5` for unmeasured paid endpoints (conservative — biases away from unknown-balance providers). But the actual code returns **1.0** (no pressure) on cold start:

| Code path | Cold-start `usage` | Cold-start pressure | ADR says |
|---|---|---|---|
| `_compute_ppq_pressure` (PPQ) | `used_pct=None → returns 1.0` | 1.0 (no penalty) | Should be 0.5 |
| `_compute_credit_pressure` (DeepInfra/OR) | `spend=0 → u=0 → returns 1.0` | 1.0 (no penalty) | Should be 0.5 |
| `_compute_zai_pressure` (z.ai) | No windows → returns 1.0 | 1.0 (no penalty) | 0.0 from API (correct — z.ai API is called on startup) |

### What happens if the first 30 days have no real data?

**Scenario:** New deployment, `api_calls` table empty, no balance queries yet.

1. **Base rates:** All providers use hardcoded `_DEFAULT_CONVERGED_RATES` (line 186) or `LAST_RESORT_RATES` from `real_price_tracker.py`. These are reasonable estimates. ✅
2. **Pressure (z.ai):** z.ai quota API is called on startup → real `used_pct` → real pressure. ✅
3. **Pressure (PPQ):** `used_pct` in `quota_state` is `None` until the PPQ collector runs → pressure = 1.0 → PPQ looks perpetually fresh. ⚠️ If PPQ credits are actually low, the optimizer won't know.
4. **Pressure (DeepInfra/OR):** `SUM(cost_usd) = 0` → `u = 0` → pressure = 1.0 → looks fresh. ⚠️ Same risk.

**Risk assessment:** The cold-start gap is **low severity for z.ai/Ollama** (API provides immediate data) and **medium severity for PPQ/DeepInfra/OpenRouter** (optimistic until first data arrives). The mitigation is the 5-minute collector cadence — after 5 minutes, real data replaces the optimistic default. The window is small but nonzero.

**Recommendation:** Either implement the ADR's 0.5 seed (requires changing `_compute_ppq_pressure` and `_compute_credit_pressure` to accept a `cold_start_seed` parameter), or document the deviation and add a "prefetch balance before first route" step to the startup sequence.

---

## 7. Shadow Mode Assessment

### Is 48h sufficient? **Borderline — sufficient for session-window validation, insufficient for weekly/monthly windows.**

| Window type | Reset period | Windows captured in 48h | Adequate? |
|---|---|---|---|
| z.ai 5h session | 5 hours | ~9–10 cycles | ✅ Yes — captures multiple depletion/recovery cycles |
| z.ai weekly | 168 hours (7 days) | **0 resets** | ❌ No — 48h sees only a partial weekly window; you never observe a weekly reset and re-depletion |
| z.ai monthly | 720 hours (30 days) | **0 resets** | ❌ No — impossible in 48h |
| Ollama session | 5 hours | ~9–10 cycles | ✅ Yes |
| Ollama weekly | 168 hours | **0 resets** | ❌ No |
| Credit balances | N/A (manual refill) | Slow depletion | ⚠️ May see no pressure events at all if traffic is low |

**The 48h window validates the session-window pressure curves but cannot validate the superposition behavior** (the key feature of the A=1.5 decision — three windows multiplying). The ADR's decisive scenario (session=90%, weekly=70%, monthly=40%) requires a mid-month state that 48h of shadow mode will never observe.

### Metrics that should trigger keeping shadow mode vs going live

The plan says "48 hours" but does not define **exit criteria**. Recommend these explicit gates:

| Metric | Go-live threshold | Keep-shadow threshold |
|---|---|---|
| **Routing divergence** (new pricing disagrees with old) | < 15% of decisions | ≥ 15% (new pricing is too different to trust) |
| **429 rate from z.ai** (new pricing should prevent these) | ≤ baseline | > baseline (pressure curve isn't diverting in time) |
| **Paid-endpoint spend** (new pricing should minimize this) | ≤ baseline | > baseline (new pricing is routing to paid too early) |
| **NaN/inf in effective_price** | 0 occurrences | > 0 (math bug) |
| **Decisions logged** | ≥ 500 (statistical significance) | < 500 (insufficient data to judge) |
| **At least 1 full z.ai session cycle observed** | Yes | No (extend shadow until a cycle completes) |

**Recommendation:** Keep 48h as the minimum, but add a **conditional extension**: if routing divergence > 15% or fewer than 1 full session cycle is observed, extend to 7 days to capture a weekly window reset. The weekly-window pressure is the highest-risk untested path.

---

## 8. Task Sizing

### Revised task count: 8 (not 10)

| Plan task | Status | Size | Notes |
|---|---|---|---|
| **1A** Wire z.ai pressure | **DELETE** — already done | — | Code at line 970, test passes. |
| **1B** Wire credit pressure | **DELETE** — already done | — | Code at lines 982/995/1008, tests pass. |
| **2A** PPQ balance collector | Keep | **Medium** | Real API integration + DB bridge to `quota_state`. Must resolve URL/auth/response-format ambiguity (see §5). |
| **2B** DeepInfra balance collector | **Re-scope** | **Small** | Not a balance API — it's "verify cost_usd logging + verify _compute_credit_pressure works with real data." If cost_extraction already logs DeepInfra costs, this is a verification task, not a build task. |
| **2C** OpenRouter balance collector | **Re-scope** | **Small** | Same as 2B. If cost_extraction logs OpenRouter costs, pressure already works. The `/api/v1/key` endpoint is optional validation. |
| **3A** z.ai trailing-365d rate | Keep | **Small** | One call to `get_real_rate(window_hours=8760)` + amortize $300/yr. |
| **3B** Ollama trailing-90d rate | Keep | **Small** | One call to `get_real_rate(window_hours=2160)`. |
| **3C** Paid endpoints trailing-30d | Keep | **Small** | Three calls to `get_real_rate(window_hours=720)`. Could merge with 3A/3B into one "dynamic base rates" task. |
| **4A** Shadow logger | Keep | **Medium** | Extend `shadow_logger.py` (exists, 9866 bytes) with divergence tracking. |
| **4B** Activate | Keep | **Small** | Flip 5 env flags (not the phantom `_UNIVERSAL_PRESSURE_ENABLED`). Document rollback for all 5. |

### Sizing assessment

- **Tasks 2A and 4A are the only medium-sized tasks.** 2A has unresolved API questions (URL, auth, response format — see §5). 4A requires designing the divergence metric and logging schema.
- **Tasks 3A/3B/3C are very small** (essentially configuration + one function call each). Consider merging into a single "wire dynamic base rates" task to avoid three near-identical kanban cards.
- **Tasks 2B/2C may be near-zero** if cost logging already works. **Verify before scheduling** — run `SELECT key_name, COUNT(*), SUM(cost_usd) FROM api_calls WHERE key_name IN ('deepinfra','openrouter') GROUP BY key_name` on the production DB. If results exist with non-null costs, the pressure mechanism already works and these tasks shrink to "add a test."
- **No task is too big.** The largest risk is 2A's API uncertainty, not implementation complexity.

### Recommended revised task list (8 tasks)

1. **Fix stale comments** (C5) — trivial, do first
2. **PPQ balance collector** (2A) — resolve API questions first
3. **Verify cost_usd logging for DeepInfra/OpenRouter** (re-scoped 2B+2C) — may be verification-only
4. **Wire dynamic base rates** (merged 3A+3B+3C) — one task, one PR
5. **Shadow logger with divergence metrics** (4A) — define exit criteria
6. **Cold-start safety fix** (C4) — align code with ADR's 0.5 seed, or document deviation
7. **Full-cascade integration test** (M5) — all 5 flags ON, verify routing cascade
8. **Activate + monitor** (4B) — flip 5 flags, 24h watch

---

*End of review. Questions or pushback welcome — the critical issues (C1–C4) should be resolved before this plan is scheduled.*
