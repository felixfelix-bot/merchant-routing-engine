# LiveRouter Failover Behavior

> **Owner**: worker-merchant · **Status**: production (Phase 1.2 + t_2532b185 fix)
> **Source**: `src/live_router.py` (`LiveRouter.select_failover`) ·
> **Caller**: `~/.hermes/bot/zai_proxy.py` (`best_key()`, Phase 5 block)

This documents *when* LiveRouter's failover selection runs, *how* it picks a
provider, and the **invariant** that fixes the regression tracked in
`t_2532b185` (LiveRouter returning `None` on failover).

## 1. When it runs

`LiveRouter.select_failover()` is invoked from the production proxy's
`best_key()` **only** when all of the following hold:

1. Both z.ai keys are exhausted / unhealthy → `chosen is None` after the
   Phase 4 health check.
2. The singleton `_LIVE_ROUTER` initialized successfully at import time.
3. The kill-switch file exists:
   `~/.hermes/bot/.enable_live_routing` (checked via `os.path.exists`; an
   empty file counts as ON). The flag is re-checked on **every** failover
   call — no restart needed. `rm` the file to disable; `touch` to enable.

On any exception, or when `select_failover` returns `None` for the chosen
provider, `best_key()` falls through to `None` and the hardcoded
`ollama → ppq → openrouter` chain in `_proxy()` runs. The bug below was
precisely this fall-through happening **silently** when a Kalman-optimal
external provider *did* exist.

## 2. How it picks a provider

`select_failover` → `_do_select_failover` builds a fresh
`RoutingOptimizer` with **all** providers (ours, friend, ollama_cloud,
ppq, openrouter, deepinfra), seeds each with its CPVO-adjusted effective
rate, then routes.

### Provider tiers

| Provider       | Tier registered | Peak window | Notes |
|----------------|-----------------|-------------|-------|
| ours           | high            | z.ai (6–10) | subscription key |
| friend         | high            | z.ai (6–10) | courtesy key |
| ollama_cloud   | high            | none        | rate-limited daily |
| ppq            | low             | none        | pay-per-token |
| openrouter     | low             | none        | pay-per-token |
| deepinfra      | low             | none        | pay-per-token |

### Tier relaxation (the fix)

The optimizer gates providers by quality tier: `difficulty="high"` requires
tier rank ≥ 2, `"medium"` ≥ 1, `"low"` ≥ 0. The pay-per-token externals are
rank 0, so they are **only** reachable at `difficulty="low"`.

`_do_select_failover` therefore routes with **progressive relaxation**:

```
for difficulty in ("high", "medium", "low"):
    result = optimizer.route(difficulty=difficulty, ...)
    if result.chosen_provider is a real provider:   # not None/"fallback"
        break
```

This prefers the highest-quality viable provider and only steps down when no
higher tier has a viable candidate. It guarantees the invariant below.

### Invariant

> **`select_failover` never returns `(None, None)` when at least one
> registered provider is viable.** Specifically, when both z.ai keys **and**
> ollama_cloud are down, the pay-per-token externals take over at the
> `"low"` step.

Returning `None` for the chosen provider is only correct when **every**
provider is non-viable (all unhealthy / exhausted).

## 3. Robustness — routing failure must never break production

`select_failover` **never raises**:

- The whole body is wrapped in `try/except` → `(None, None)` on any error.
- **Per-provider `pace_factor_multi` is wrapped individually** so one
  provider's malformed pace-window tuple cannot abort the entire failover
  (which would otherwise surface as a swallowed `(None, None)`). Bad windows
  are skipped; that provider falls back to a 1.0 pace multiplier.
- CPVO / DB errors fall back to the unadjusted base rates (see
  `_get_effective_rates`).

## 4. Regression (t_2532b185)

**Symptom**: With both z.ai keys exhausted, `select_failover` returned
`(None, None)` and the proxy fell back to the hardcoded chain instead of
using Kalman-optimized selection — but *only* when ollama_cloud was also
unavailable (the realistic 48h-soak scenario where ollama is rate-limited
daily). When ollama was still up, the bug was masked (ollama is high-tier).

**Root cause**: The optimizer was queried at `difficulty="high"` only. The
pay-per-token externals (tier `low`, rank 0) failed the high-tier gate
(rank 2). With no high-tier provider viable, `RoutingOptimizer.route()`
returned `chosen_provider="fallback"` → `select_failover` mapped that to
`None` → returned `(None, None)`.

**Secondary cause**: `pace_factor_multi` was called without a per-provider
guard; a malformed pace tuple from one provider could raise and be swallowed
into `(None, None)`.

**Fix**: progressive tier relaxation (`high → medium → low`) so low-tier
externals are reached, plus per-provider `pace_factor_multi` wrapping.

**Regression tests**: `tests/test_live_router.py::TestSelectFailover`
- `test_returns_external_when_all_high_tier_dead` (off-peak)
- `test_returns_external_when_all_high_tier_dead_peak` (peak)
- `test_malformed_pace_window_does_not_break_failover` (pace robustness)

All three fail against the pre-fix code and pass after the fix.

## 5. Monitoring

`LiveRouter.get_kalman_state()` exposes per-provider `base_rate`,
`burn_rate`, `tokens_used`, `update_count`, and CPVO `quality_score`
(success_rate, avg_latency_ms, token_mismatch_rate) for dashboards.
