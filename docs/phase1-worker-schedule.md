# Phase 1 Worker Schedule

**Date**: 2025-07-25
**Status**: IN PROGRESS — Batch 1 complete, Batches 2-3 scheduled
**Plan**: See `price-first-kalman-plan.md`
**ADRs**: ADR-001 through ADR-007

---

## LESSON LEARNED: Worker Timeout

Both batch-1 workers timed out at 300s. Root cause: tasks too large for a single
worker within the timeout. Fix: smaller task scopes, workers get ONE file each.

---

## BATCH 1 — COMPLETE (done by manager after worker timeouts)

| Task | Deliverable | Tests | Status |
|------|-------------|-------|--------|
| P1.1 | config/providers.yaml (enhanced pricing) | N/A (config) | DONE |
| P1.2 | src/price_kalman.py | 21/21 pass | DONE |
| P1.3 | src/consumption_kalman.py | 18/18 pass | DONE |

47/47 tests pass. Committed + pushed.

---

## BATCH 2 — ROUTING OPTIMIZER + SHADOW LOGGER

### Worker C: Routing Optimizer (src/routing_optimizer.py)

**Model**: glm-5.2
**Toolsets**: terminal, file
**Depends on**: price_kalman.py + consumption_kalman.py (both exist)
**Delivers**: src/routing_optimizer.py + tests/test_routing_optimizer.py

**Task**:
Build a deterministic cost minimizer. Collects effective prices from all
providers (using PriceKalman + ConsumptionKalman), filters exhausted/unhealthy/
low-quality, sorts by price ascending, returns cheapest viable provider.

**Inputs per provider**:
- PriceKalman instance (effective_price)
- ConsumptionKalman instance (will_exhaust)
- quota_remaining
- breaker_tripped (bool)
- model_tier

**Output**:
```python
{
    "chosen_provider": "zai_ours",
    "chosen_model": "glm-5.2",
    "effective_cost_per_1m": 0.068,
    "reason": "cheapest viable — peak=off, scarcity=1.0, health=1.0",
    "candidates_evaluated": [
        {"provider": "zai_ours", "price": 0.068, "viable": true},
        {"provider": "zai_friend", "price": 0.082, "viable": true},
        {"provider": "ollama", "price": 0.024, "viable": false, "reason": "breaker_tripped"},
        {"provider": "ppq", "price": 0.280, "viable": true},
    ]
}
```

**Tests**:
- Cheapest viable provider selected
- Exhausted providers filtered (will_exhaust = True)
- Circuit-breaker providers filtered (health = inf → price = inf)
- Quality threshold enforced (low-tier models filtered for high-difficulty tasks)
- Returns reason string for each decision
- All providers exhausted → returns fallback
- Effective price always > 0

### Worker D: Shadow Logger (src/shadow_logger.py)

**Model**: glm-5.2
**Toolsets**: terminal, file
**Depends on**: routing_optimizer (Worker C)
**Delivers**: src/shadow_logger.py + tests/test_shadow_logger.py

**Task**:
Read-only tap that logs both the live routing decision (what best_key() chose)
and the shadow decision (what routing_optimizer would choose) for every API call.

**DB schema** (writes to zai_usage.db):
```sql
CREATE TABLE IF NOT EXISTS routing_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    live_provider TEXT,       -- what best_key() chose
    live_model TEXT,
    shadow_provider TEXT,     -- what optimizer would choose
    shadow_model TEXT,
    shadow_cost REAL,         -- optimizer's effective_cost
    live_cost REAL,           -- best_key's effective cost (estimated)
    tokens INTEGER,
    agree INTEGER,            -- 1 if live==shadow, 0 if different
    reason TEXT
);
```

**Tests**:
- Mock request → verify both decisions logged
- Verify DB write succeeds
- Verify agree flag correct
- Verify no exceptions on edge cases (all providers down)
- <1ms overhead per call (benchmark test)

---

## BATCH 3 — INTEGRATION + COLD REVIEW

### Worker E: Integration Tests (tests/test_integration.py)

**Model**: glm-5.2
**Delivers**: tests/test_integration.py

**Tests**:
- Full pipeline: price_kalman → consumption_kalman → routing_optimizer → shadow_logger
- Feed mock API call data (varying quotas, peak hours, provider health)
- Verify routing_optimizer produces valid decisions for all provider combinations
- Verify shadow_logger captures both decisions
- Edge cases: all providers exhausted, all unhealthy, zero tokens, peak transition mid-run
- Coverage target: 80% minimum, 90% target

### Worker F: Cold Reviewer

**Model**: glm-5.2
**Context**: ONLY the git diff of all Phase 1 code. Zero implementation context.
**Delivers**: Review report (APPROVED or CHANGES_REQUESTED)

**Checks**:
1. Correctness — does code do what task asks?
2. Security — injection, hardcoded secrets, unsafe patterns
3. Edge cases — null/empty inputs, boundary conditions
4. Architectural fit — matches ADRs (peak is deterministic, price > 0, etc.)

**If CHANGES_REQUESTED**: issues go back to relevant worker. Max 2 cycles.

---

## POST-REVIEW (MANAGER)

1. Run full test suite — verify real output
2. Check git log — conventional commits
3. Check git status — clean tree
4. Check git push — both remotes
5. Verify no secrets
6. Report to operator with evidence

---

## TIMELINE

| Phase | Duration | Status |
|-------|----------|--------|
| Batch 1 | Done | COMPLETE |
| Batch 2 | ~10-15 min | SCHEDULED |
| Batch 3 | ~10-15 min | SCHEDULED |
| Cold review | ~5 min | SCHEDULED |
| Total Phase 1 | ~35-45 min remaining | IN PROGRESS |
| Shadow validation | 48h (data collection) | AFTER CODE COMPLETE |

---

## DISPATCH RULES (from timeout lessons)

1. ONE file per worker — no multi-file tasks
2. Worker gets exact file path, class name, method signatures
3. Worker reads the relevant ADRs + existing Kalman code first
4. TDD enforced: test file first, then implementation
5. Quality-gates skill loaded
6. If timeout: manager finishes the work directly
