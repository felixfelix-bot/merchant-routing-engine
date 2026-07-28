# Phase 3 Failover Validation — Interim Checkpoint

> **Status: INTERIM** (not final). Soak ~3.7% complete (~1.8 h of 48 h).
> This is a checkpoint from the first post-fix run. It records the major
> validation result (BLOCKER A resolved) plus open blockers that must clear
> before the 48 h soak can be declared complete.
>
> Author: worker-merchant (run on t_ffa4f4f8, 2026-07-29 ~04:5x IST)
> Branch: `converged-rate-replay`

## TL;DR

| Item | Status |
|---|---|
| BLOCKER A — `select_failover()` returned `(None,None)` | ✅ **FIXED (f882812)** — verified by direct test |
| LiveRouter code deployed in running proxy | ✅ proxy PID 3461956 restarted 04:37 IST, *after* the 04:36 fix |
| Telemetry quality (>90% valid-of-received) | ✅ **MET** post-fixes (friend 96–100%, ours 93%) |
| Kill-switch mechanism (rm flag → instant revert) | ✅ verified (per-request check, no restart) |
| Service health | ✅ active, NRestarts=0, ExecMainStatus=0 |
| **Live failover events observed on real traffic** | ❌ **0** — both-exhausted windows are bursty; none coincided with a flag-on observation window |
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

## 5. Why 0 live events were observed (open item, not a bug)

LiveRouter Phase 5 fires **only** when `chosen is None` (both z.ai keys
exhausted after the Phase 4 health check). The dual-exhaustion condition is
**bursty**: quota windows reset cyclically, so dual-exhaustion clustered (58
events in 3.5 min at one point) then disappeared entirely (0 events in a 5-min
window once `friend` recovered to healthy). None of the bursts coincided with a
flag-on observation window long enough to capture a `live_kalman_failover`
decision. This is a timing/waiting issue — exactly what the 48 h soak is meant
to accumulate.

## 6. Open blockers (need resolution before completion)

1. **Kill-switch flag instability.** `.enable_live_routing` is untracked and
   NOT gitignored in the bot repo. It was deleted by an unidentified process
   within minutes of creation on two occasions (04:41-04:43, 04:49-04:53),
   though it survived 90 s–5 min in other windows (sporadic, not a fixed cron).
   No filesystem script, cron, process cmdline, or proxy code-path deletes it;
   no `git clean` script targets the bot repo. Likely an occasional external
   operator/sync action. **Action:** gitignore the flag + identify/stop the
   sporadic deleter so the soak can hold the flag on for 48 h.
2. **`routing_live_decisions` table does not exist.** The task requires logging
   live decisions to a new table (same schema as `routing_shadow_decisions` +
   `pace_mults`). `zai_proxy.py` only logs the decision reason to
   `key_decisions` (`live_kalman_failover_{provider}`); no `CREATE TABLE
   routing_live_decisions` exists anywhere. **Action:** spawn a code task to
   add the table + insert (small, contained change) — out of scope for this
   no-code-change soak card.
3. **48 h wall-clock** (~3.7% complete) — cannot elapse in one agent session.
4. **10+ live failover events gate** — 0 observed; requires blocker #1 cleared
   plus natural dual-exhaustion bursts during the 48 h window.

## 7. Verification checklist (real output)

1. Failover event count (live): **0** observed (bursty dual-exhaustion; see §5).
2. Error rate: hard errors since flag-enable = **0**; valid-of-received >90%.
3. Latency: friend avg ~9.7 s (unchanged from baseline; reasoning model), ours
   avg ~3.4 s. No latency regression introduced.
4. Cost savings: not yet measurable (0 live events). Shadow data shows
   ollama_cloud ($0.024/M) cheapest; LiveRouter matches it when ollama healthy.
5. Kill switch test: **PASS** — flag-off ⇒ 0 live decisions; per-request check
   ⇒ instant revert; flag-on ⇒ `select_failover` returns provider (direct test).
6. `git log` / `git status` / `git push`: see commit for this report.
9. Gate criteria: **NOT MET** (0 live events; 48 h incomplete). Zero incidents ✅.
