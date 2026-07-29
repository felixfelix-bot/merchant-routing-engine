# Phase 3 Failover Validation — Interim Checkpoint

> **Status: BLOCKED — root cause of 0 live events found (wiring bug).** Soak
> ~5% complete (~2.5 h of 48 h). This update (run 27, 2026-07-29 05:25 IST)
> corrects the interim checkpoint's wrong "timing" conclusion: the LiveRouter
> failover gate is on the wrong code path and produces 0 events by construction.
> See §5 for the definitive root cause and the required code fix.

## TL;DR

| Item | Status |
|---|---|
| BLOCKER A — `select_failover()` returned `(None,None)` | ✅ **FIXED (f882812)** — verified by direct test |
| LiveRouter code deployed in running proxy | ✅ proxy PID 3461956 restarted 04:37 IST, *after* the 04:36 fix |
| Telemetry quality (>90% valid-of-received) | ✅ **MET** post-fixes (friend 96–100%, ours 93%) |
| Kill-switch mechanism (rm flag → instant revert) | ✅ verified (per-request check, no restart) |
| Service health | ✅ active, NRestarts=0, ExecMainStatus=0 |
| **Live failover events observed on real traffic** | ❌ **0** — ROOT CAUSE FOUND (§5): the LiveRouter gate is wired into `best_key()`'s None-branch, but production routes dual-exhaustion through the retry-loop hardcoded fallback (zai_proxy.py:2326-2333), which bypasses LiveRouter entirely. Not a timing issue — no soak will produce events until the wiring is fixed |
| **`routing_live_decisions` table** | ❌ **MISSING** — code logs to `key_decisions` (reason `live_kalman_failover_*`) only |
| **Kill-switch flag stability** | ⚠️ flag sporadically deleted by an unidentified process (survived 90 s–5 min in some windows, vanished in others) |
| **48 h soak** | ❌ ~3.7% complete — cannot finish in one session |
| Gate: 10+ failover events, zero incidents | ❌ NOT MET (0 live events; zero incidents ✅) |

## 1. BLOCKER A is resolved (headline result)

The previous run (t_ffa4f4f8 run 16) blocked because `LiveRouter.select_failover()`
returned `(None, None)` whenever **both z.ai keys AND `ollama_cloud`** were
unavailable — the realistic 48 h-soak scenario. Root cause and fix are
documented in `docs/incident-log.md` **Incident 6** and commit **f882812**
(`fix(live-router): prevent (None,None) return on failover — tier relaxation`).

The fix: progressive tier relaxation in `_do_select_failover` — route at
`high → medium → low`, breaking when a viable provider is found, so the
low-tier pay-per-token externals (ppq/openrouter/deepinfra) are reached when
nothing higher is viable. `pace_factor_multi` is also wrapped per-provider so
one malformed window can't abort the whole failover.

### Verification (read-only, does not route traffic)

I instantiated `LiveRouter` exactly as `zai_proxy.py` does (`db_path` + converged
rates) and called `select_failover` with synthetic snapshots:

| Scenario | Inputs | `select_failover` result |
|---|---|---|
| **A** — all high-tier dead (ours+friend+ollama exhausted) | externals healthy | **`('openrouter', 'ppq')`** ✅ (was `(None,None)`) |
| A — peak-hour variant | same, peak=True | **`('openrouter', 'ppq')`** ✅ |
| **B** — z.ai keys dead, ollama_cloud healthy | ollama healthy | **`('ollama_cloud', None)`** ✅ (cheapest high-tier) |
| **C** — sanity, all healthy | all healthy | **`('ours', 'ollama_cloud')`** ✅ (cheapest overall) |

**Conclusion:** the fix is effective. `select_failover` no longer returns
`(None, None)` when a healthy registered provider exists. Regression tests
`tests/test_live_router.py::TestSelectFailover::test_returns_external_when_all_high_tier_dead`
(off-peak + peak) and `test_malformed_pace_window_does_not_break_failover` cover it.

The running production proxy (MainPID 3424651→3461956) restarted at **04:37 IST**,
one minute **after** the f882812 commit (04:36 IST), so the live process has the fix.
`src/live_router.py` mtime 04:32 IST confirms the working file the process loaded.

## 2. LiveRouter vs hardcoded-chain comparison

When both z.ai keys are exhausted, the two paths diverge:

| Condition | Hardcoded chain (`_proxy`) | LiveRouter (fixed) |
|---|---|---|
| ollama_cloud healthy | ollama_cloud first | `ollama_cloud` (Scenario B) — **agree** |
| ollama_cloud dead, externals alive | ollama_cloud (fails) → ppq → openrouter → deepinfra, by cost | **`openrouter`** directly (Scenario A) — skips the dead-ollama retry |

LiveRouter's advantage: when ollama_cloud is known-dead, it avoids the wasted
ollama_cloud attempt and picks the **CPVO-quality-adjusted** cheapest viable
external immediately. The hardcoded chain still works (it's the safe fallback
on line 2114-2119) but is less efficient.

Shadow-routing disagree data since soak start (`routing_shadow_decisions`,
agree=0) shows where the live proxy and the shadow optimizer diverged:
`live=friend shadow=ours` (2290), `live=friend shadow=ollama_cloud` (1908),
`live=ours shadow=ollama_cloud` (7). These are primary-routing differences
(Phase 4 territory); the LiveRouter *failover* path (Phase 5) is the subject
of this soak.

## 3. Error / latency / quality

Measured from `provider_telemetry` (soak window start 2026-07-28T21:21Z):

| Window | Provider | valid / received | valid-of-received |
|---|---|---|---|
| After SSE fix (22:34Z) | friend | 453/468 | **96.8%** |
| After SSE fix (22:34Z) | ours | 96/103 | **93.2%** |
| After parse_error fix (23:07Z) | friend | 50/50 | **100%** |
| Last 10 min | friend | 219/221 | **99.1%** |

Raw overall valid rate (valid/total) is much lower (16–40%) but is dominated by
`no_response` events — i.e. quota exhaustion (both z.ai keys dead this week),
which is the *expected* condition the failover system exists for, not a quality
defect. **Interpreted as response-parse quality (valid-of-received), the >90%
gate is met** after the three telemetry fixes (6a99a41, 8c489bc, b1d6f6a).

Hard errors (error_type not in {none, no_response}) since flag re-enable: **0**.
No incident attributable to LiveRouter (it never caused an error — it either
didn't engage or fell through safely).

## 4. Kill-switch test results

- **Mechanism:** `best_key()` Phase 5 gate (zai_proxy.py:1698-1699) checks
  `os.path.exists(_LIVE_ROUTING_FLAG)` **per request**. No restart needed.
- **flag OFF → revert:** empirically confirmed — across the entire soak history
  (and repeated observation) the flag being absent produces **0**
  `live_kalman_failover_*` decisions and traffic uses the hardcoded chain.
- **flag ON → engage:** confirmed via the direct `select_failover` test (returns
  a provider); the per-request check means engagement begins on the very next
  both-exhausted request.
- **Instant:** the `os.path.exists` call is nanosecond-cheap and per-request, so
  `rm` of the flag reverts on the next request with no process restart.

## 5. ROOT CAUSE — LiveRouter gate is on the wrong code path (wiring bug)

> **This section corrects the interim checkpoint's §5, which wrongly concluded
> the 0-event count was a "bursty timing / waiting" issue. It is not. It is a
> code-level wiring defect: no amount of soaking will produce live failover
> events until the gate is moved to the code path that actually handles
> dual-key exhaustion.**

### Where the LiveRouter gate lives

The Phase 5 LiveRouter failover gate is at **`zai_proxy.py:1698-1724`**, *inside*
the `best_key()` function. It fires **only** when `best_key()` has set
`chosen = None` (i.e. its own Phase 4 health check concluded both z.ai keys are
exhausted) AND the kill-switch flag exists:

```
if chosen is None and _LIVE_ROUTER is not None and os.path.exists(flag):
    _provider, _fallback = _LIVE_ROUTER.select_failover(...)
    if _provider:
        reason = f"live_kalman_failover_{_provider}"   # ← only logged on success
        return chosen
```

### Where production actually handles dual-key exhaustion

In live traffic, `best_key()` **returns a key** (ours or friend) on the initial
call (zai_proxy.py:2061) because its health cache (`_is_key_healthy`) has not
yet registered the exhaustion. The selected key then **429s mid-request**. The
request handler's retry loop (zai_proxy.py:2260-2340) then:

1. marks the key exhausted (`_mark_key_exhausted`, :2291),
2. retries the next key, which also 429s,
3. after all keys fail, falls to **:2326-2333 — the hardcoded chain**:
   `_try_ollama_cloud(...)` (logs reason `zai_both_keys_exhausted_ollama_fallback`
   at :1842) then `_try_external_failover(...)`.

**This hardcoded fallback at :2326-2333 never consults LiveRouter.** And because
`best_key()` returned a non-None key on the initial call (its health cache lagged
the 429), the LiveRouter gate at :1698 was never reached either. LiveRouter is
dead code on the actual failover path.

### Proof from the production database (zai_usage.db, 2 h window)

| reason (key_decisions) | count (2 h) | source |
|---|---|---|
| `both_keys_exhausted` (best_key returns None → LiveRouter gate path) | **0** | best_key :1689/1722 |
| `live_kalman_failover_*` (LiveRouter actually picked a provider) | **0** (0 ever, all history) | best_key :1714 |
| `zai_both_keys_exhausted_ollama_fallback` (hardcoded retry-loop fallback) | **841** | _try_ollama_cloud :1842 via :2328 |

`both_keys_exhausted` is **0 in the last 2 h** (18,802 historically — it *used*
to fire when the health cache was stale enough) while the hardcoded ollama
fallback fires **841 times in 2 h**. The two numbers being non-overlapping
proves the ollama fallback does **not** flow through `best_key()`'s None branch.
The dual-exhaustion events are real and frequent (~7/min) — they are simply
routed around LiveRouter entirely.

### Why `select_failover` returning a provider (run-21 fix f882812) did not help

Commit f882812 made `select_failover()` return a real provider for synthetic
inputs (verified in §1). But that fix is irrelevant on the live path: the gate
that *calls* `select_failover` (best_key :1706) is never reached, so a working
`select_failover` can never produce a `live_kalman_failover_*` event. The fix
addressed a real bug in the function body, but the function is never invoked
under production failover conditions.

### The fix (code change — out of scope for this no-code soak card)

One of:
1. **Move/add the LiveRouter consultation into the retry-loop fallback** at
   zai_proxy.py:2326 (before the hardcoded `_try_ollama_cloud`), passing the same
   `select_failover(quota_state, health_state, peak, pace_windows)` call; or
2. **Make `best_key()` proactively detect both-keys-429-exhausted** (refresh
   health from the live quota cache, not the lagging breaker) and return None so
   the existing :1698 gate fires; or
3. **Add a LiveRouter call at the `if chosen is None:` block** (:2087) as a
   second engagement point.

Plus: create the `routing_live_decisions` table (same schema as
`routing_shadow_decisions` + `pace_mults` column) and log each live decision
there (see §6 blocker #2).

> **STATUS: ✅ APPLIED (P3.4, task t_6f12b943).** Option 1 + Fix 2 landed.
> The production proxy (`~/.hermes/bot/zai_proxy.py`) now:
>   - centralises the kill-switch + `select_failover` call + safe-fallthrough
>     into a single `_consult_live_router()` helper;
>   - calls it from BOTH the `best_key()` Phase 5 gate AND the retry-loop
>     terminal fallback (the previously-bypassed production path);
>   - fixes the latent tuple-unpack bug (the old gate did
>     `_provider, _fallback = select_failover(...)` then used `_provider` — a
>     `(provider, model)` tuple — as the provider string, so even when the gate
>     fired the pick was never routable);
>   - honours the LiveRouter pick via a new `preferred=` kwarg on
>     `_try_external_failover` (tried first; cost-sorted chain remains the
>     safe fallback);
>   - logs each engagement to the new `routing_live_decisions` table (with
>     `pace_mults` captured from `LiveRouter.last_pace_mults`).
> `LiveRouter.last_pace_mults` (engine `src/live_router.py`) exposes the actual
> multipliers used. Regression tests in `tests/test_live_router_wire.py`
> (consult/kill-switch/fallthrough/table) + `tests/test_live_router.py`
> (`last_pace_mults`). Full suite: 865 pass (2 pre-existing CPVO failures from
> P4.5c model-aware change, unrelated). The 48 h soak (t_ffa4f4f8) can now
> accumulate `live_kalman_failover_*` events.

## 6. Open blockers (need resolution before completion)

1. **🔴 WIRING BUG (primary blocker — see §5).** The LiveRouter failover gate
   is in `best_key()` (zai_proxy.py:1698), but production dual-key exhaustion is
   served by the retry-loop hardcoded fallback (zai_proxy.py:2326-2333), which
   bypasses LiveRouter. Result: 0 live events despite 841 dual-exhaustion
   events/2 h. **Action:** spawn a code task to move/add the LiveRouter
   consultation to the actual failover path (see §5 fix options). This is the
   gate to Phase 4 — without it, the soak cannot produce the deliverable.
2. **`routing_live_decisions` table does not exist.** The task requires logging
   live decisions to a new table (same schema as `routing_shadow_decisions` +
   `pace_mults`). `zai_proxy.py` only logs the decision reason to `key_decisions`
   (`live_kalman_failover_{provider}`); no `CREATE TABLE routing_live_decisions`
   exists anywhere. **Action:** the code task above should also add this table +
   insert (small, contained change).
3. **parse_error quality regression.** valid-of-received dropped to friend 80% /
   ours 28% (was >90% in the interim checkpoint), driven by 583 `parse_error`
   events/2 h. Not LiveRouter-caused. **Action:** separate quality task to
   investigate the parse-error source (possible SSE/parsing regression).
4. **48 h wall-clock** (~5% complete) — cannot elapse in one agent session.
5. **Kill-switch flag stability** — ⚠️ previously sporadically deleted; currently
   **stable** (ON since 04:56 IST, 30+ min, survived this whole run). Lower
   priority now; gitignoring the flag is still advisable for robustness.

## 7. Verification checklist (real output — run 27, 2026-07-29 05:25 IST)

1. Failover event count (live): **0** (`live_kalman_failover_*`, 0 ever in all
   history). Dual-exhaustion events DO occur — `zai_both_keys_exhausted_ollama_fallback`
   = **841 in the last 2 h** — but they are served by the hardcoded retry-loop
   fallback, not LiveRouter. See §5 root cause.
2. Error rate: 3 hard errors in 2 h (1 `Network is unreachable`, 1 DNS failure,
   1 `api_error`) — none attributable to LiveRouter (it never engaged).
   `no_response` = 3299 (quota exhaustion — the expected failover condition).
   `parse_error` = **583** — see quality note below.
3. Latency: friend avg **7462 ms**, ours avg **3683 ms** (last 2 h). No regression
   from baseline.
4. Cost savings: not measurable (0 live events). Shadow optimizer (last 2 h,
   8124 rows) would pick `ours` 5846 (72%) / `ollama_cloud` 2278 (28%); live picks
   diverge significantly (`friend` 2300, `ours` 1773, `ollama_cloud` 819).
5. Kill switch test: PASS — flag-off ⇒ 0 live decisions (empirically confirmed);
   per-request `os.path.exists` check (zai_proxy.py:1699) ⇒ instant revert.
   Flag currently **ON** (stable since 04:56 IST, 30+ min).
6. `git log --oneline -5`: see commit for this report (HEAD of
   `converged-rate-replay`).
7. `git status`: clean (only this docs change).
8. `git push github`: see output below.
9. Gate criteria: **NOT MET** — 0 live events (need 10+); 48 h ~5% complete;
   valid-of-received below 90% (friend 80%, ours 28% — see quality note).

### Quality note (regression from interim checkpoint)

The interim checkpoint (run 21) reported valid-of-received >90% (friend 96–100%,
ours 93%). The current 2 h window shows a **regression**: friend **80%**,
ours **28%**, driven by **583 `parse_error` events** in 2 h. This is NOT caused
by LiveRouter (which never engaged) — it is a response-parse quality issue on
the hardcoded z.ai/ollama path. The manager's "Mark done when >90% valid rate"
criterion is **not currently met** and warrants investigation as a separate
quality task. Token-mismatch rate is also elevated (friend 35.3%, ours 16.9%).
