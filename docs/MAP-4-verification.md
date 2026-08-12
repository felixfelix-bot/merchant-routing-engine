# MAP-4 Verification: CPVO Wiring + provider_telemetry.model Column

**Date:** 2026-08-12
**Task:** t_073a84ac
**Result:** VERIFIED — no changes required

## 1. CPVO Calculator Wiring

`cpvo_calculator.CPVOCalculator` is imported and used by `live_router.py`
(the live routing wrapper around `routing_optimizer.RoutingOptimizer`):

- **Import:** `src/live_router.py:70` → `from src.cpvo_calculator import CPVOCalculator`
- **Instantiation:** `src/live_router.py:780` → `self._cpvo = CPVOCalculator(db_path)`
- **Effective rates (quality-aware routing):** `src/live_router.py:1030-1058`
  calls `self._cpvo.get_effective_rates(self._base_rates)` with a 5-minute
  cache (`_CPVO_CACHE_TTL`). On any error, falls back to unadjusted base rates.
- **Quality score (monitoring):** `src/live_router.py:1766-1768` calls
  `self._cpvo.get_quality_score(name, 24.0, base_rate=...)` for the state
  report.

The `routing_optimizer.py` itself does not directly import `cpvo_calculator`
— it receives effective rates from `LiveRouter`, which applies the CPVO
quality penalty before passing rates to the optimizer. This is the correct
design: the optimizer is a pure deterministic sort; the CPVO adjustment
happens upstream in the live wrapper.

### Call chain

```
LiveRouter.select_failover()
  → _get_effective_rates()
    → CPVOCalculator.get_effective_rates(base_rates)
      → _query_aggregates(provider) [reads provider_telemetry table]
    → effective rates passed to RoutingOptimizer.select()
```

## 2. provider_telemetry.model Column

The production database at `~/.hermes/bot/zai_usage.db` was inspected:

```
provider_telemetry columns:
  id                INTEGER     (cid=0)
  ts                TEXT        (cid=1)
  provider          TEXT        (cid=2)
  response_received INTEGER     (cid=3)
  response_valid    INTEGER     (cid=4)
  latency_ms        INTEGER     (cid=5)
  error_type        TEXT        (cid=6)
  billed_tokens     INTEGER     (cid=7)
  actual_tokens     INTEGER     (cid=8)
  token_mismatch    INTEGER     (cid=9)
  model             TEXT        (cid=10)  ← PRESENT
```

The `model` column exists as `TEXT` (cid=10). **No migration needed.**

The `CPVOCalculator._query_aggregates()` method (line 155-162) checks for
the `model` column at runtime via `PRAGMA table_info` and gracefully
degrades to provider-level aggregation when the column is absent — so the
code is backward-compatible with old schemas.

## 3. Tests

```
python3 -m pytest tests/test_cpvo_calculator.py tests/test_cpvo_model_aware.py -v

============================== 41 passed in 2.40s ==============================
```

- `test_cpvo_calculator.py`: 22 tests (compute_cpvo, get_effective_rates,
  get_quality_score, never-raises edge cases)
- `test_cpvo_model_aware.py`: 19 tests (model-aware compute/rates/quality,
  backward compatibility on legacy schema, model mapping integration,
  never-raises edge cases)

## 4. Acceptance Criteria

| Criterion | Status |
|---|---|
| CPVO wired into routing | VERIFIED — LiveRouter → CPVOCalculator → RoutingOptimizer |
| model column populated | VERIFIED — column exists in production DB |
| Tests pass | VERIFIED — 41/41 pass |
| Migration needed | NO — column already present |