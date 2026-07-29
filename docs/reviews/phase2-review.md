# Phase 2 Code Review Findings

Reviewed: 2026-07-29  
Scope: all Phase 2 components (shadow logger, shadow hook, routing advisor, profit tracker, proxy wiring).  
Commit baseline: `728bee2` (HEAD detached from converged-rate-replay branch).  
Reviewer: worker-inspector

## Checklist Results

| # | Item | Status | Evidence |
|---|------|--------|----------|
| 1 | Shadow logger is READ-ONLY (no routing changes when disabled) | **PASS** | `src/shadow_logger.py` only writes to `routing_shadow_decisions`; `shadow_hook.compare()` is called in a `try/except` in `zai_proxy.py` L2419 and never assigns its return value to `chosen`. |
| 2 | Advisor mode has feature flag (`.optimizer_advisor_mode` file check) | **PASS** | `~/.hermes/bot/zai_proxy.py` L711 defines `_ADVISOR_FLAG`; `_ProxyRoutingAdvisor.enabled()` L733-736 returns `True` when the marker file exists; `ROUTING_ADVISOR_ENABLED` env var is also honoured. `tests/test_proxy_advisor_wiring.py` pins all three states (10 cases, all pass). |
| 3 | `best_key()` fallback works on ANY optimizer exception | **PASS** | `src/routing_advisor.py` `decide()` L146-153 catches `Exception` and calls `_fallback()`; proxy integration L2032-2051 wraps advisor call in `try/except` and falls back to `best_key()` if `chosen` remains `None`. `test_optimizer_exception_falls_back` + `test_flag_on_optimizer_exception_falls_back` pass. |
| 4 | Peak hour check removal doesn't break existing behavior | **PASS** | Original peak pre-check is still intact at `zai_proxy.py` L2054-2059 and runs whenever the advisor flag is OFF. Flag ON: optimizer itself applies per-provider peak multipliers; the proxy still computes `peak = _is_peak_hour()` L2029 and passes it downstream. `_is_peak_hour()` L232-235 is unchanged and the `_PEAK_HOURS_UTC` set is the same as in `zai_proxy.py.bak.phase2`. |
| 5 | Profit tracker runs in background thread | **PASS** | `src/profit_tracker.py` L114-117 starts a daemon `ProfitTracker-writer` thread; `record_decision()` enqueues and returns immediately. `tests/test_profit_tracker.py::test_concurrent_writes_all_persist` verifies 200/200 rows under 8 threads. |
| 6 | No secrets in proxy modifications | **PASS** | `grep -E 'API_KEY|SECRET|TOKEN|PWD|PASS' ~/.hermes/bot/zai_proxy.py` returned only existing key-loading code and Ollama/PPQ/OpenRouter env-var references that already existed in `zai_proxy.py.bak.phase2`. No Phase 2 code paths hard-code credentials. |
| 7 | All tests pass | **PARTIAL** | Phase 2 tests green: shadow_hook, shadow_logger, advisor_integration, proxy_advisor_wiring, profit_tracker → **89/89 passed**. Full suite has 2 pre-existing failures (see Issues). |
| 8 | Proxy backup file exists | **PASS** | `~/.hermes/bot/zai_proxy.py.bak.phase2` exists (size 91,286 bytes, mtime 2026-07-25 18:30). |

## Issues

- **[HIGH] Phase 4.5 test regression unrelated to Phase 2.** Full test suite reports 2 failures in `test_cpvo_live_router.py`:
  - `TestEndToEndQualityAwareRouting::test_end_to_end_quality_aware_pick`
  - `TestEndToEndQualityAwareRouting::test_end_to_end_without_cpvo_picks_cheapest_sticker`
  Both assertions compare a bare provider string (`"friend"`, `"ollama_cloud"`) against the tuple `("provider", "model")` now returned by `select_failover()`. This is a pre-existing test/contract mismatch from the LiveRouter/CPVO work (parent task `t_0e389717` metadata already noted "2 failed (pre-existing P4.5 LiveRouter regressions, unrelated)"). Not a Phase 2 safety issue, but it means the task-mandated `pytest tests/` does **not** pass cleanly.

- **[MEDIUM] Shadow hook `_last_update` is unused and singleton lacks reset mechanism.** `src/shadow_hook.py` L111 stores `_last_update` but it is never read. In long-running proxy processes this dead state is harmless, but there is no method to clear `_instance` for unit isolation. Tests already set `ShadowHook._instance = None` (L59), so this is noted only as a cleanliness issue.

- **[MEDIUM] Profit tracker effective_price defaults to 0.0 on coercion.** Bad or missing effective prices are silently stored as 0.0, which can inflate `savings_per_1m` when `next_best_price` is valid. The contract says "we never claim savings we can't prove", but `savings_per_1m = 0.5 - 0.0 = 0.5` in `test_bad_effective_price_coerced_to_zero` shows the opposite. If bad prices are common, dashboards will over-report savings. Recommend storing `effective_price` as NULL when invalid, or capping `savings_per_1m` at 0 when either price is missing/invalid.

- **[LOW] Two differently-named Phase 2 proxy backups exist.** Both `zai_proxy.py.bak-phase2` and `zai_proxy.py.bak.phase2` are present. This is not a safety problem, but the canonical dotted suffix referenced by the task (`zai_proxy.py.bak.phase2`) is the larger, more recent backup and should be the one operators rely on for rollback.

- **[LOW] Advisor `decide()` uses `estimated_tokens=0` in proxy call.** `zai_proxy.py` L2034 calls `_routing_advisor.decide(difficulty="medium", estimated_tokens=0)`. The optimizer will treat this as a low-token request, which can bias its cost model. Phase 2 tests do not exercise real token counts, but production shadow comparisons already use actual token counts via `_shadow_hook.compare()`; advisor mode currently ignores request size. Consider plumbing `estimated_tokens` from the incoming request body in a follow-up.

## Recommendations

1. **Do not block Phase 2 on the P4.5 test failures** — they are unrelated to shadow/advisor/profit code and the Phase 2 tests are green.
2. **Fix the P4.5 CPVO LiveRouter tests** by updating assertions to unpack the `(provider, model)` tuple returned by `select_failover()`.
3. **Reconsider coercion of invalid `effective_price` in profit tracker** so that dashboards cannot show spurious savings when the actual price is unknown.
4. **Clean up or document the duplicate `zai_proxy.py.bak*` files** so a rollback uses the intended backup.
5. **Plumb `estimated_tokens` into the advisor call** once token extraction in `_proxy()` is reliable.

## Verdict

Phase 2 integration is **safe to run in production**: shadow mode remains read-only, advisor mode is gated, fallbacks are defensive, and the proxy can be reverted from the backup. Full-suite test failures are isolated to unrelated P4.5 CPVO work.
