# Design: Two-Layer Pressure-Aware Model+Key Selection

**Status:** DESIGN ONLY — no code changed. Grounded against production `~/.hermes/bot/zai_proxy.py` (4550 lines), `zai_usage.db` (kalman_samples, api_calls), `~/.hermes/profiles/manager/cron/jobs.json`, and `merchant-routing-engine/src/*` on 2026-08-17.

---

## 0. Grounding facts (verified, drive the design)

| # | Fact | Consequence |
|---|------|-------------|
| G1 | Manager profile cron: **93 jobs, 91 with `model:null`** → inherit manager model = glm-5.3 | The single largest friend-quota burner is *cron inheritance*, not interactive turns. Layer 1's biggest lever. |
| G2 | `zai_usage.db.kalman_samples` has rows **only for key=`friend`**, windows `5-hour` + `monthly` (284 samples each, live as of today). Cols: `used_pct_observed, burn_rate_tph, velocity_tph2, uncertainty, exhausts_in_hours, will_exhaust` | Pressure signal exists and is fresh; ours-key has no samples (treat "no data" as green-with-caveat, never as red). |
| G3 | Proxy already reads `X-Model-Tier` (L3534) and `X-Hermes-Session` (L3538, threaded to `api_calls.session_id`) — but **no Hermes client currently sends `X-Model-Tier`** | Header plumbing precedent exists; it's a dead input today. Free to repurpose/extend. |
| G4 | `_select_model_tier = None` (L1280, "model selection is now profile-level") — so `adaptive_model_tuner.py` → `model_tier_thresholds.json` → `model_tier_router.py` chain is **orphaned** | Confirmed. Revive as config calibrator rather than inventing a parallel mechanism. |
| G5 | Silent 5.3→5.2 rewrite in `_try_ollama_cloud` (L3159–3163): "glm-5.3 is NOT in Ollama Cloud's catalog — downgrade to glm-5.2" | The downgrade *mechanism* exists; it is silent and unconditional. Design formalizes + gates it. |
| G6 | Existing hysteresis precedents: `LOCK_THRESHOLDS` (5h: ours 90 / friend 80; weekly: ours 60 / friend 80) hard locks; `.ollama_exhausted_until` paywall flag; `_proactive_switch_state` 60 s TTL cache | Asymmetric friend thresholds already encode "protect the friend key". Bands generalize this. |
| G7 | `ollama_quota_tracker.py` computes regimes `included / extra / exhausted` vs 500M tok/5h + 3.5B/wk | Ollama pressure is already a solved sub-problem; wire it in as dimension 2. |
| G8 | Last-24h traffic: friend ≈ 3.9k calls (1.2k glm-5.3), ollama_cloud 366 glm-5.2, openrouter 29, telnyx 1 | Flat-rate-first is already de-facto; the gap is *no pressure-aware choice among flat-rate paths*. |
| G9 | Token quotas are **model-agnostic** (z.ai quota counts tokens, not dollars) | **Critical:** downgrading 5.3→5.2 *on the friend key* saves ZERO quota. Quota relief requires re-routing to ollama or deferring. "Downgrade" below always means "downgrade AND re-route", unless marked quota-neutral. |
| G10 | External failover already tiers by requester: manager floor glm-5.2, workers cheapest (L3363–3370) | Same classifier can feed Layer 2. |

---

## 1. Goals & non-goals

**Goals (priority order):**
1. **Friend-key protection** — explicit first-class objective. They pay nothing; their key is the only glm-5.3 source. We must not burn their quota on deferrable work.
2. **Flat-rate-first economics** — friend z.ai and ollama_cloud ($100/mo, huge windows) absorb ~100% of load; telnyx/openrouter/deepinfra/ppq remain last resort with caps (unchanged).
3. **Interactive quality floor** — manager turns talking to a human keep 5.3 essentially always.
4. **No flapping, no surprises** — every automatic decision is logged with a reason and is reversible by flag.

**Non-goals:** changing paid-provider pricing/failover logic; touching worker profile pinning (glm-5.2/4.5-flash already sensible); per-token cost optimization on z.ai (flat rate — irrelevant).

---

## 2. Design decisions (numbered, with trade-offs)

### D1 — Two layers with a strict split of knowledge
- **Layer 1 (scheduling-time)** lives *outside* the proxy, at the surfaces that know **task identity**: cron dispatcher, kanban dispatch gate, `delegate_task`. It picks the model *before* a request exists, and can **defer** (skip a cron cycle, hold a card).
- **Layer 2 (request-time)** lives *inside* the proxy. It sees only HTTP and pressure; it can **reroute/downgrade** but never defer (rejecting an in-flight HTTP request breaks callers).

**Trade-off:** policy split across two components risks divergence → mitigated by both layers reading the *same* pressure-band state and the *same* config file (D7). Alternative (all logic in proxy) rejected: the proxy cannot defer cron jobs, and cannot see urgency.

### D2 — Quality tiers: P / S / E, classified per task at scheduling surfaces
- **P (premium/interactive):** manager live conversation turns, handovers, anything with a human waiting. → glm-5.3, never deferred.
- **S (standard):** substantive kanban cards (coding/review/research), important delegate tasks, quality-sensitive cron (deploy verification, review digests). → glm-5.3 when green; glm-5.2-on-ollama under pressure.
- **E (economy):** mechanical cron (sweeps, health checks, log summaries), worker turns, bulk delegate. → glm-4.5-flash / glm-5.2-cheapest by default already.

Classification sources: cron jobs get an additive `quality_tier` field (default **S**, since 91 jobs are currently null/unknown); kanban cards via existing dispatch-gate task_type → tier map; `delegate_task` gains a `tier` argument (default S). One-line overrides per job/card.

**Trade-off:** a default of S for the 91 null jobs is conservative (they'd still get 5.3 when green — matching today's behavior, so no day-1 quality regression); operators promote/demote individual jobs over time. Defaulting to E would save more quota immediately but silently degrades 91 jobs — rejected for v1.

### D3 — Pressure bands with asymmetric hysteresis (the core state)
Computed from `kalman_samples` (friend, 5-hour window primary; monthly as a secondary veto):

| Transition | Condition |
|---|---|
| GREEN → AMBER | `used_pct_5h ≥ 60` OR (`will_exhaust` AND `exhausts_in_hours ≤ 3.0`) |
| AMBER → RED | `used_pct_5h ≥ 75` OR (`will_exhaust` AND `exhausts_in_hours ≤ 1.0`) |
| RED → AMBER | `used_pct_5h ≤ 60` AND dwell ≥ 10 min |
| AMBER → GREEN | `used_pct_5h ≤ 45` OR window-reset detected AND dwell ≥ 10 min |

- **Asymmetry** (escalate at 60/75, de-escalate at 45/60) + **10-min dwell** prevents flapping when usage oscillates near a threshold.
- Use `exhausts_in_hours − uncertainty` (column exists) for the predictive trigger so a noisy Kalman doesn't cry wolf.
- Monthly window ≥ 85% acts as a **floor-raiser**: shifts thresholds down 10pp (amber at 50, red at 65) — the monthly window has no reset escape, so it deserves earlier caution.
- State persisted in `zai_proxy_state.json` (already exists, survives restart).
- "No kalman data" (G2, ours key) ⇒ treat as GREEN but never let it be the *reason* for a downgrade; downgrades require positive evidence of pressure.

**Trade-off:** thresholds are hand-picked v1 constants. The orphaned `adaptive_model_tuner.py` is revived in Phase 2 to recalibrate them from the `exhausts_in_hours` percentile history (it already reads exactly this table/column) — one source of truth, no new tuner.

### D4 — Interactive vs background at the proxy: heuristic first, header second, profile tag rejected
- **v1 heuristic (proxy-only, zero client changes):** a request is **interactive** iff `X-Hermes-Session` is present AND that session had a request in the last ~15 min AND the session is not registered as a cron/worker session. Otherwise **background**. (Batch = background.)
- **v2 explicit:** clients set `X-Task-Class: interactive|background|batch` (cron runner, kanban dispatcher, delegate wrapper send `background`/`batch`; manager REPL sends `interactive`). Heuristic remains the fallback when absent — same loopback trust boundary as `X-Model-Tier` (proxy is localhost-only).
- **Profile tag is rejected as a classifier** — decisive reason: the 91 cron jobs run under the *manager* profile (G1), the same profile as interactive turns. A profile tag would classify all of them interactive and protect exactly the wrong traffic. Classification must be task/session-scoped, not profile-scoped.

**Trade-off:** heuristic can misclassify a slow interactive session (>15 min think time) as background → worst case that one turn gets 5.2-on-ollama under RED. Acceptable; fixed by v2 header.

### D5 — Decision matrix at request time (glm-5.3 arrives)

| Class | Band | Decision | Quota effect |
|---|---|---|---|
| interactive | GREEN/AMBER | **5.3 @ friend** | normal |
| interactive | RED, `exhausts_in_hours > 0.5` | **5.3 @ friend** (rationed, logged `interactive_rationed`) | protected use |
| interactive | RED, `exhausts_in_hours ≤ 0.5` | 5.2 @ ollama if regime=included, else **5.3 @ friend anyway** (a mid-turn 429 is worse than a weaker model; external failover already catches the residual) | last-resort |
| background | GREEN | **5.3 @ friend** (matches today) | normal |
| background | AMBER/RED, ollama=included | **5.2 @ ollama** (formalizing G5's silent rewrite into a *logged, gated* one) | friend quota relieved |
| background | AMBER/RED, ollama=extra | S-tier → 5.2 @ ollama; E-tier → 4.5-flash @ friend | partial |
| background | AMBER/RED, ollama=exhausted | 5.2 @ friend for S (quota-neutral per G9 — buys nothing per token, but flash/5.2 typically *emit fewer tokens* for mechanical tasks); 4.5-flash @ friend for E; batch-class callers that opted in may receive `429 + Retry-After` | no relief — this state is why Layer 1 deferral exists |

"Interactive manager turn = always 5.3?" — **answer: almost.** 5.3 unless the 5h window is within ~30 min of predicted exhaustion; even then 5.3 is preferred over paid providers. The only thing that displaces interactive 5.3 is ollama-included 5.2.

**Trade-off:** under RED with ollama exhausted, background traffic still burns friend quota (no better flat-rate option exists by construction). Layer 1's job is to make this cell rare (defer before it happens, at `exhausts_in_hours ≤ 3`).

### D6 — Ollama session quota as a second pressure dimension
- Reuse `ollama_quota_tracker` regimes (G7): **included** → preferred downgrade target; **extra** → usable only for S-tier/P-tier rescue (overage is metered against the $100 plan); **exhausted / `.ollama_exhausted_until` armed** → skip ollama entirely (price → +inf, already implemented).
- The band computer emits a single composite `flat_rate_capacity: ok | ollama_only | friend_only | none` so Layer 2 does one lookup, not two.

**Trade-off:** none meaningful — this is wiring an existing module into the decision. The one decision *inside* it: treat ollama "extra" as amber-not-red (don't strand the flat-rate path when a little overage is cheaper than paid providers).

### D7 — One shared config + state artifact
- `~/.hermes/bot/pressure_policy.json`: thresholds, tier defaults, flags (`mode: shadow|enforce|off`). Written by hand in v1; rewritten by the revived tuner in Phase 2 (it keeps its `--dry-run` habit).
- Both layers read only this file + the band state exposed by the proxy (`/pressure` endpoint, D8). No layer computes pressure independently.

**Trade-off:** a stale JSON could pin the system in RED → mitigated by band state carrying its own `computed_at`; consumers fall back to GREEN-with-warning if older than 2× the Kalman poll interval.

### D8 — Observability: every auto-decision leaves a trace
- New `pressure_decisions` table (or extend `model_decisions`): `ts, session_id, task_class, requested_model, served_model, provider, band, flat_rate_capacity, reason_code, kalman_snapshot(exhausts_in_hours, used_pct, uncertainty)`.
- Reason codes are stable strings (`bg_downgraded_ollama`, `interactive_rationed`, `interactive_protected`, `cron_deferred_red`, …) so digests are groupable.
- `GET /pressure` on :9099 returns band + inputs + recent decision counts — one curl answers "why am I getting 5.2?".
- Response headers on every rewrite: `X-Served-Model`, `X-Downgrade-Reason` (replaces the *silent* part of G5).
- Daily digest cron (an E-tier job, appropriately): downgrades by reason, minutes spent per band, friend-window saved-token estimate.
- The existing `dq05_health` MCP surface can later surface the same JSON — free dashboarding.

### D9 — Rollback: dark-first, flag-file pattern, per-phase
- Every phase ships in `shadow` mode first (decisions computed and logged, never applied) — the codebase already has this precedent (`ShadowLogger`, `.optimizer_advisor_mode` advisor flag).
- Kill switches: `touch ~/.hermes/bot/.pressure_routing_disabled` → Layer 2 becomes a passthrough (exact current behavior); `pressure_policy.json mode=off` → Layer 1 stops deferring/re-tiering. Both are single-artifact reversals, no code rollback needed.
- Because D5's background-GREEN cell equals today's behavior, "off" and "green" are indistinguishable to callers — rollback is provably clean.

### D10 — Friend-key protection made explicit (not emergent)
- Friend thresholds are strictly tighter than ours everywhere (extends LOCK_THRESHOLDS' existing asymmetry, G6).
- Layer 1 defers **predictively** (`exhausts_in_hours ≤ 3`), i.e. *before* the window is hot — the reactive lock at 80% stays as the backstop, unchanged.
- Weekly/monthly friend windows get their own AMBER (never RED-only) trigger: monthly ≥ 85% forces AMBER even if 5h is green, because there is no reset escape hatch this month.
- A hard invariant asserted in shadow mode: *interactive 5.3 requests served on friend ≥ (total interactive 5.3 requests − those during ollama-included downgrades)* — i.e., friend's key is never bypassed for interactive traffic except to a flat-rate provider.

---

## 3. State machine sketch — request-time routing (Layer 2)

```
            ┌──────────────── request(model, session, headers) ────────────────┐
            ▼                                                                  │
   model == glm-5.3 ?                                                          │
     │no → existing routing (unchanged)                                        │
     ▼yes                                                                      │
   classify: X-Task-Class? ──absent──> heuristic(session recency ≤15min,       │
     │                                  not cron/worker session)               │
     ├── interactive ──┐                                                       │
     └── background ──┤                                                       │
                      ▼                                                       │
              ┌── band (hysteresis FSM, refreshed by Kalman poller) ──┐       │
              │   GREEN ──(5h≥60 or ex_h≤3.0)──> AMBER ──(5h≥75 or     │       │
              │     ▲                                  ex_h≤1.0)──> RED │       │
              │     └──(5h≤45 or reset, dwell≥10m)── AMBER              │       │
              │         (AMBER─(5h≤60, dwell≥10m)──> GREEN)             │       │
              └────────────────────────┬────────────────────────────────┘       │
                                      ▼                                        │
   INTERACTIVE branch:                                                        │
     GREEN|AMBER ──────────────────> SERVE 5.3 @ friend     [interactive_protected]
     RED, ex_h > 0.5 ───────────────> SERVE 5.3 @ friend     [interactive_rationed]
     RED, ex_h ≤ 0.5:                                                        │
        capacity=ollama_ok ─────────> SERVE 5.2 @ ollama     [interactive_downgraded]
        else ───────────────────────> SERVE 5.3 @ friend     [interactive_last_resort]
                                            │ 429 ──> existing external failover
   BACKGROUND branch (tier from Layer 1, default S):
     GREEN ──────────────────────────> SERVE 5.3 @ friend     [bg_kept]
     AMBER|RED:
        capacity=ollama_included ────> SERVE 5.2 @ ollama     [bg_downgraded_ollama]
        capacity=ollama_extra:  S ───> SERVE 5.2 @ ollama     [bg_downgraded_ollama_extra]
                               E ───> SERVE 4.5-flash @ friend[bg_economy_friend]
        capacity=friend_only:   S ───> SERVE 5.2 @ friend     [bg_quota_neutral] *
                               E ───> SERVE 4.5-flash @ friend[bg_economy_friend]
        capacity=none + batch&optedin > 429 + Retry-After     [bg_throttled]
        (every path: log row + X-Served-Model/X-Downgrade-Reason headers)

   * quota-neutral (G9): fewer output tokens is the only real saving; the true
     fix for this cell is Layer 1 deferral, which should make it rare.
```

Layer 1 state machine (scheduling-time, simpler):

```
   task(cron|card|delegate, tier) at dispatch:
     band==GREEN ───────────────> resolve model per tier as requested (null→profile default)
     band==AMBER ───────────────> S: pin glm-5.2@ollama; E: cheap; P: unchanged
     band==RED ─────────────────> defer non-interactive (cron: skip cycle, mark
                                   `deferred_pressure_red`, reschedule after window
                                   reset or band≤AMBER; kanban: hold card in queue;
                                   delegate: reject-with-retryable status)
     P tasks are never deferred, any band.
```

---

## 4. Phased implementation (small steps, each independently shippable & revertible)

| Phase | Scope | Size | Revert |
|---|---|---|---|
| **P0 — Groundwork** | Standalone band-computer module (reads kalman_samples + ollama regime → band + capacity, hysteresis FSM, persisted state). `GET /pressure`. No behavior change anywhere. | ~1 sitting | delete module |
| **P1 — Layer 2 shadow** | Log `pressure_decisions` row for every 5.3 request with the decision it *would* make. Digest: how many downgrades/defers would have fired, last 7 days replay. | ~1 sitting | flag off |
| **P2 — Layer 2 enforce (background only)** | D5 background branch live behind `mode=enforce`; heuristic classifier; formalize G5 rewrite as logged+headered. Interactive branch = always-5.3 passthrough (matrix's interactive cells minus rationing). | ~1 sitting | `.pressure_routing_disabled` |
| **P3 — Layer 1 cron** | Cron dispatcher resolves `model:null` through tier policy (D2 defaults) + defers E/S jobs on RED. Additive `quality_tier` field honored. | ~1 sitting | `mode=off` → null-model resolution reverts to profile inheritance |
| **P4 — Tuner revival** | `adaptive_model_tuner.py` retargeted: writes band thresholds (not the old 10/80/10 mix) into `pressure_policy.json` from percentile history; `--dry-run` first. | ~1 sitting | revert JSON to constants |
| **P5 — Explicit class header** | Clients (cron runner, kanban dispatcher, delegate wrapper) send `X-Task-Class`; heuristic demoted to fallback. Kanban dispatch gate consumes band (replaces legacy TASK_MODELS chains at L4395, kept as fallback). | 2 sittings | clients stop sending header |
| **P6 — Interactive rationing + dashboards** | Enable RED-cell interactive logic; daily digest cron; expose band in `dq05_health`; weekly review of reason-code distribution; consider promoting the best-behaved cron jobs from S to explicit P/E based on digest data. | ~1 sitting | policy JSON |

Sequencing rationale: P0–P2 are proxy-contained and reversible in minutes; P3 (the biggest quota lever, G1) only ships after a week of P1 shadow data validates the classifier; P4–P6 are refinements.

---

## 5. Explicit answers to the posed questions

- **Proxy distinguishing interactive vs background:** heuristic (session recency + non-cron session) in v1; `X-Task-Class` header in v2 with heuristic fallback; profile tag rejected because cron-under-manager-profile makes it structurally wrong (D4).
- **Hysteresis:** asymmetric thresholds (escalate 60/75, de-escalate 45/60) + 10-min dwell + persisted band state + uncertainty-adjusted Kalman trigger (D3).
- **Ollama quota in the decision:** second dimension via regime (included/extra/exhausted) folded into a single `flat_rate_capacity` input; "extra" is amber-grade, "exhausted"/paywall flag removes ollama entirely (D6).
- **Friend-key protection as explicit goal:** tighter thresholds everywhere, predictive deferral at ex_h≤3 before the 80% lock trips, monthly-window AMBER floor-raiser, and a shadow-mode invariant that interactive 5.3 never goes to a paid provider (D10).
- **Observability:** `pressure_decisions` table with stable reason codes, `/pressure` endpoint, response headers on rewrites (killing the silent downgrade), daily digest (D8).
- **Rollback:** shadow-first per phase, flag-file + policy-JSON kill switches, background-GREEN ≡ current behavior so "off" is provably identical (D9).
