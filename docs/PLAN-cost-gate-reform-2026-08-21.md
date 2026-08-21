# PLAN: Quota Gate → Cost Gate Reform (CG-1..CG-6)

> **SUPERSEDED (2026-08-21)** by `docs/PLAN-cost-gate-reform-v2-2026-08-21.md`
> (Felix answered Q1–Q10; percentile gate design; routstrd anomaly; CG-1..CG-9).

**Author:** merchant-routing CW consultant (GLM-5.2, manager-profile session)
**Date:** 2026-08-21
**Input:** `docs/HANDOVER-cost-gate-reform-2026-08-21.md` (read in full)
**Status:** PRE-IMPLEMENTATION PLAN — awaiting Felix's approval per PLAN GATE. No implementation code written or committed.

---

## 0. Executive Summary

**Verdict on the handover's 6-task decomposition: SOUND, with 5 amendments.**
The CG-1..CG-6 shape is correct and the reuse-first philosophy is right. But
ground-truth verification of every referenced asset surfaced facts the handover
got partially stale or missed entirely (details in §2). The most consequential:

1. **The Kalman dispatch gate is already implemented and live.**
   `src/dispatch_gate.py` (P5.1, pure `evaluate_dispatch()`) exists, is tested
   (`tests/test_dispatch_gate.py`), and is wired into the production proxy at
   `zai_proxy.py:4617` (`GET /v1/dispatch_gate`). It already computes
   `predicted_cost` — but as an *informational* field, not a gating dimension.
   CG-1 is an **extension** of this module, not a greenfield build.
2. **Kalman convergence is STILL RED today** (verified live, 2026-08-21:
   `Kalman Convergence: ✗ unhealthy | mean error: n/a | keys: friend ?`).
   Per the handover's own pitfall, CG-2 must not lean on ConsumptionKalman
   predictions until this is green — plan accordingly (§4, CG-2).
3. **The proxy's spend caps were deliberately DEACTIVATED 2026-08-20**
   (`_check_spend_cap` / `_check_global_spend_cap` in `zai_proxy.py` now always
   allow, docstring: "merchant module markets and wallet balance decide
   routing"). CG-3 reintroduces a cap at *dispatch* level — this is a partial
   reversal of a fresh Felix decision and needs his explicit sign-off (Q3).
4. **`api_calls` has no `task_type` column** — the CG-2 join key
   `(model, task_type)` cannot be seeded from history as written (§4, CG-2).
5. **The approved IMPL spec is fail-OPEN; the handover demands fail-CLOSED.**
   The 2026-07-29 spec's Safety Properties §2–3 ("fails open") predate the
   2026-08-15 phantom-availability incident. The cost gate must invert this —
   and the proxy's existing fallback chain (coarse-check fallback at
   `zai_proxy.py:4733`, flat-rate override of gate holds) contains fail-open
   paths that CG-4/CG-6 must explicitly reconcile, not inherit.

Recommended order: **CG-1 → (CG-2 ∥ CG-3) → CG-4 → CG-5 → CG-6(go-live gate)**,
with shadow-mode operation between CG-4 and cron cutover (§6). Estimated total:
**5–8 working days** of implementation + a ≥5-day shadow campaign.

---

## 1. Reference Verification (every asset the handover cites)

| Referenced asset | Status | Notes |
|---|---|---|
| `skills/devops/zai-quota-gate/references/kalman-dispatch-gate-design.md` | ✅ EXISTS | But header says "Not yet implemented" — **STALE**; P5.1 implemented it |
| `docs/IMPL-SPEC-kalman-dispatch-gate.md` | ✅ EXISTS (529 lines) | v1+v2 approved 2026-07-29. **STALE on safety posture**: §Safety Properties 2–3 specify fail-open; conflicts with the 2026-08-15 fail-closed lesson |
| Pressure curves in `src/` | ✅ EXISTS | `src/pricing_engine.py`: RP-EXP `1 + K·t/(1-t)`, uniform asymptote 1.5 (Felix 2026-08-05 decision), staggered onsets. Matches handover exactly |
| `ConsumptionKalman.will_exhaust()` | ✅ EXISTS | `src/consumption_kal.py` → actually `src/consumption_kalman.py:182` |
| `PriceKalman`, `LiveRouter` | ✅ EXIST | `src/price_kalman.py`, `src/live_router.py` (kill-switch machinery present; failover wiring disabled per handover — consistent) |
| `src/dispatch_gate.py` (NOT cited — the handover missed it) | ✅ EXISTS, tested, wired into proxy `:4617` | The strongest reuse asset for CG-1 |
| Gate scripts `zai-quota-gate.{sh,py}` | ✅ EXIST | Both currently **fail OPEN** on no data (`.sh` line 59–60 `exit 0`; `.py` `no_data_optimistic`) — the exact anti-pattern the pitfall section forbids |
| `zai_usage.db` (`api_calls` w/ `cost_usd`) | ✅ EXISTS | 86,432 rows; cols incl. model, total_tokens, cost_usd, cost_source, session_id. **NO `task_type` column** |
| `api_burn.db` (`provider_balances`) | ✅ EXISTS | + `balance_snapshots`, `ppq_queries` |
| Measured rates (glm-5.2 $0.0101–0.0155/M, PPQ $0.0197/M) | ✅ CORROBORATED | `docs/extra-usage-evidence-and-seed-plan.md`: glm-5.2 $0.01554/M measured (2.07B tokens); PPQ figure appears only in handover — treat as plausible, re-verify from `ppq_queries` at CG-2 time |
| Telnyx kimi-k3 rates ($2.70/M in, $13.50/M out) | ✅ EXIST in proxy | `zai_proxy.py:2034` price table + blended 5.40/M seed at `:1307` + `telnyx_quota_entry` balance bridge (TELNYX-3.2) |
| 27+ LLM crons with QUOTA GATE line | ✅ CONFIRMED | 31 `QUOTA GATE` matches in `cron/jobs.json` (manager profile), in ≥3 phrasings |
| `~/plans/liverouter-wiring-plan-2026-08-19.md` | ✅ EXISTS (38 KB) | Not needed for CG scope; context only |
| Skill `zai-quota-gate` | ✅ EXISTS | `skills/devops/zai-quota-gate/` |
| Skill `zai-pricing-reform` | ✅ EXISTS | `skills/zai-pricing-reform/` |
| Skill `kalman-convergence-check` | ✅ EXISTS | v1.1.0, current; its `--short` check was run for this plan |
| Skill `price-first-api-routing` | ❌ **NOT FOUND** | No such skill dir anywhere under `~/.hermes` (only incidental session-dump mentions). The "63+67 pitfalls" reference is unusable as cited — ask orchestrator where it lives or drop it |
| "1740+ tests pass" | ✅ (understated) | `pytest --collect-only`: **2019 tests** collected in 0.7 s on this branch |
| Kalman convergence | ⚠️ **STILL RED** | Live check 2026-08-21: `✗ unhealthy`, friend key `?` (insufficient-data flavored). Handover's conditional triggers: CG-2 backtest prerequisite |

---

## 2. Consultant Review of the Decomposition

### 2.1 What's right

- **Cost as the unified gating dimension** is architecturally correct: the
  pressure curves already fold quota depletion into price, so
  `est_cost = predicted_tokens × effective_price` subsumes the three-window
  × two-key boolean logic Felix wants gone.
- **Reuse-first task list**: pointing CG-1 at the Kalman dispatch-gate design,
  CG-2 at `zai_usage.db` historicals, CG-3 at existing spend data — correct
  instincts, and the assets are real.
- **Backstop preservation** (freeze marker + locked-key hard block) in CG-4 and
  the Telnyx bypass test in CG-6 show the handover absorbed the right incidents.

### 2.2 Gaps and boundary problems (amendments)

**A1 — CG-1 boundary vs `src/dispatch_gate.py` is undefined.** The handover
says "nothing connects the pricing engine's output to dispatch decisions" —
false since P5.1. CG-1 must be scoped as: *add a Dimension-4 (budget) to
`evaluate_dispatch()` or a sibling `evaluate_cost()`*, reusing its pure-function
pattern, TASK_PROFILES, and margins. Building a parallel module would duplicate
task-profile/margin logic and create drift. Decision needed: extend
`dispatch_gate.py` vs new `cost_gate.py` that composes with it (plan assumes
**new `src/cost_gate.py` composing `dispatch_gate` outputs** — keeps P5.1
tests untouched; see §4).

**A2 — Fail-closed vs the approved spec's fail-open.** The IMPL spec (still the
approved design of record) mandates fail-open twice; the proxy implements it
(module-unavailable → coarse check → allow; flat-rate candidates override gate
holds). The cost gate inverts this. CG-4/CG-6 must specify exactly which
fallback layers become fail-closed and which stay fail-open (a cron aborting on
a dead proxy is itself an outage). Proposal in §4 (CG-4): freeze marker and
locked-key remain hard-closed; cost-gate *evaluation* failures degrade to the
OLD quota-gate verdict (fail-closed relative to cost, fail-open relative to
nothing) with a loud log line — NOT to "allow".

**A3 — CG-2's join key doesn't exist.** `(model, task_type)` has no
`task_type` in `api_calls`. Seed per-model p50/p90 and derive the task
dimension from `TASK_PROFILES.budget_mult` (already the spec's own model);
optionally backfill coarse labels via `session_id` → cron-name heuristics later.
Also: the handover's "if still red, CG-2 needs the backtest first" condition IS
TRIGGERED — convergence is red today. CG-2 must run on measured historicals
only; Kalman inputs are optional, added when green.

**A4 — CG-3 collides with a fresh Felix decision.** Proxy per-request spend
caps were deactivated 2026-08-20 with an explicit rationale. CG-3's budget cap
is dispatch-time, not request-time, but Felix must confirm this is a layering
he intends (Q3). Data side is easy: the `daily_spend` table is live (today:
telnyx $1.20/17 calls, routstrd $18.75/155 calls, ours $0/6310 calls) — rolling
spend needs no new tracker, just a reader. Note routstrd's $18.75/day: a global
budget cap that ignores routstrd/telnyx tiers is meaningless; cap scope must
cover all paid tiers (Q1).

**A5 — CG-5 crosses a profile boundary.** The 31 QUOTA GATE lines live in the
**manager profile's** `cron/jobs.json` (and its .bak files — leave baks alone).
The merchant-routing CW should not edit another profile's crons directly
(cross-profile write guard); CG-5 is a *coordinated handoff*: merchant-routing
ships the new gate-line spec + verification script; the orchestrator CW applies
it to manager crons. Plan reflects this.

**Minor notes:** (i) the scripts dir holds ~10 sibling gate scripts
(`pressure_model_gate.py`, `quota_gate.py`, `dispatch-quota-gate.py`, …) — out
of CG scope, but audit whether any cron references them instead of
`zai-quota-gate.*` during the CG-5 sweep; (ii) `.dispatch_frozen` currently
absent — correct (it's an emergency marker, not a permanent file).

### 2.3 Ordering

Proposed order is sound as given (CG-1..CG-6). Two adjustments: CG-2 and CG-3
are mutually independent — run in parallel after CG-1's interface freeze; CG-6
splits into *continuous test suite* (ships with every task) and *shadow
validation campaign* (the actual go-live gate, §6).

---

## 3. Target Design (one page, for approval)

```
evaluate_cost_gate(
    model, task_type, estimated_tokens,        # task identity
    effective_price,                            # $/M, from pricing_engine
                                               #   pressure(u) × base rate
    rolling_spend_usd, budget_cap_usd,          # from budget_tracker (CG-3)
    predicted_tokens,                           # from token_predictor (CG-2)
    hardware_req,                               # margin: 2x/4x/6x/3x (reuse)
) -> {decision: ALLOW | DENY | DEFER,
      est_cost_usd, headroom_usd, reason_json,
      inputs_confidence: {...}}                 # provenance of every input
```

Decision rule: `allowed = (rolling_spend + est_cost × margin) < budget_cap`
with `est_cost = predicted_tokens/1M × effective_price`. Telnyx-routed models
(kimi-k3 et al.): skip z.ai quota inputs entirely; cost logic applies with
Telnyx's own blended rate and Telnyx balance as the rolling-spend source.
Fail-closed matrix:

| Missing input | Decision |
|---|---|
| Freeze marker present | DENY (hard, pre-empts everything) |
| Friend key locked (z.ai path) | DENY (hard, backstop retained) |
| effective_price unknown/inf | DENY + `reason: price_unknown` (+ manual override escape, Q6) |
| predicted_tokens unavailable | DENY + `reason: no_token_history` (seed table ships with CG-2, so rare) |
| budget config missing/unparsable | DENY + `reason: budget_unconfigured` (CG-3's whole point) |
| Gate infrastructure error (proxy down) | Fall back to legacy quota-gate verdict, log loudly — never "allow" on absence of data |

---

## 4. Implementation Plan, per task

### CG-1 — Cost-gate module (pure, tested)
- **Scope:** `src/cost_gate.py`: `evaluate_cost_gate()` per §3; decision enum;
  fail-closed matrix; reason-JSON schema with input provenance. Composes with
  `src/dispatch_gate.py` (imports TASK_PROFILES, HARDWARE_SAFETY_MARGIN) — no
  duplication. `MIN_EFFECTIVE_PRICE` floor respected (ADR-004).
- **Files:** `src/cost_gate.py` (new), `tests/test_cost_gate.py` (new).
- **Tests:** table-driven unit tests — every fail-closed row of §3's matrix;
  the handover's two acceptance cases (expensive task blocked at simulated 95%
  budget; cheap task allowed at same state); Telnyx bypass applies cost but not
  z.ai quota; margin compounding (board 4x on cost, not just tokens).
- **Depends on:** nothing. **Blocks:** CG-2/CG-3 interface, CG-4.
- **Effort:** 1–1.5 days.

### CG-2 — Token predictor (model → p50/p90)
- **Scope:** `src/token_predictor.py`: seed script + reader. Seed = p50/p90 of
  `total_tokens` per model from `api_calls` (86K rows, status_code=200,
  recent window). Task dimension via `TASK_PROFILES.budget_mult` (A3); no
  `(model, task_type)` historical join until the proxy logs `task_type`
  (flagged as follow-up, Q5). Emit confidence bucket by sample count
  (n<30 → low). **Prerequisite per handover:** Kalman convergence is red →
  ship WITHOUT Kalman inputs; add `kalman_convergence-check --short` as an
  optional accuracy input only when verdict ∈ {healthy, improving}.
- **Files:** `src/token_predictor.py` (new), `tests/test_token_predictor.py`
  (new, fixture DB), optionally `scripts/seed_token_stats.py`.
- **Tests:** synthetic sqlite fixture; percentile correctness; cold-model
  behavior (unknown model → conservative default × penalty, still answers —
  gate fails closed on *price*, predictor must always return a number with a
  confidence flag); drift re-seed (seed-then-replace pattern per Felix).
- **Depends on:** CG-1 (interface). **Effort:** 1–2 days.

### CG-3 — Budget config + rolling-spend reader
- **Scope:** `config/budget.yaml` (cap value(s), window, scope — values TBD,
  Q1–Q3); `src/budget_tracker.py`: rolling spend from the LIVE `daily_spend`
  table (all tiers incl. routstrd/telnyx) + `api_burn.db` provider balances as
  cross-check; staleness check (no rows today → `budget_unconfigured`-style
  fail-closed). No new collection infrastructure.
- **Files:** `config/budget.yaml` (new), `src/budget_tracker.py` (new),
  `tests/test_budget_tracker.py` (new).
- **Tests:** rolling-window sums; multi-tier aggregation; stale-data →
  fail-closed; cap parse errors → fail-closed.
- **Depends on:** CG-1 (interface); **Felix answers Q1–Q3 before merge**.
- **Effort:** 0.5–1 day.

### CG-4 — Rewrite production gate scripts
- **Scope:** canonical gate CLI in repo (`scripts/zai-cost-gate.py`) that:
  freeze-marker check → locked-key check (both retained verbatim as hard
  backstops) → cost-gate evaluation via the repo modules. Deployed to
  `~/.hermes/profiles/manager/scripts/zai-quota-gate.py` (same filename =
  zero cron changes needed at this stage; CG-5 only changes the *line*, and
  only if we adopt new semantics). Missing-data posture flipped from
  `no_data_optimistic` to §3's matrix. `.sh` variant delegates to the `.py`.
  Revert plan per AGENTS.md: keep `zai-quota-gate.py.legacy-20260821`.
- **Files:** `scripts/zai-cost-gate.py` (repo), deploy target
  `~/.hermes/profiles/manager/scripts/zai-quota-gate.py` (profile —
  coordinated, not silently); `docs/` revert note.
- **Tests:** CLI exit-code contract tests in repo; manual verify items 1–3 of
  the handover's "Verify Before Declaring Done" against a local proxy stub.
- **Depends on:** CG-1..3. **Effort:** 0.5–1 day + deploy window.

### CG-5 — Cron prompt sweep (31 lines, manager profile)
- **Scope:** New gate-line spec (keeps the existing two-call phrasing so the
  change is semantic, not syntactic, wherever possible); sweep script that
  audits `cron/jobs.json` for stale `QUOTA GATE` phrasings; **executed by the
  orchestrator CW** in the manager profile (A5). Verify = grep sweep returns
  0 stale lines; spot-check 3 crons end-to-end.
- **Files:** `scripts/cron_gate_line_audit.py` (repo); manager `jobs.json`
  (orchestrator-side).
- **Depends on:** CG-4 deployed. **Effort:** 0.5 day + coordination.

### CG-6 — Validation & go-live gate
- **Scope:** two halves.
  (a) *Test suite (continuous):* bidirectional fail-closed tests (deny when
  over-budget, allow when under, deny on every missing-input row), Telnyx
  both-ways test (bypasses z.ai quota; does NOT bypass cost), freeze-marker
  precedence test, proxy fallback-chain audit test (no path reaches "allow"
  without a positive cost verdict — this is where A2's reconciliation is
  enforced).
  (b) *Shadow campaign (go-live gate):* §6.
- **Files:** `tests/test_cost_gate_validation.py` (new); shadow harness reuses
  `src/shadow_hook.py` / `shadow_logger.py` and the `routing_shadow_decisions`
  table pattern.
- **Effort:** 1 day (suite) + campaign wall-clock.

### Dependency graph & timeline

```
CG-1 (1.5d) ──┬─ CG-2 (1.5d, parallel) ──┐
              └─ CG-3 (1d, parallel) ────┴─ CG-4 (1d) ── shadow (≥5d) ── CG-5 (0.5d) ── go-live
CG-6a tests ship with every task; CG-6b = shadow exit criteria
```
Total: **5–8 implementation-days** + ≥5-day shadow window. Two PRs minimum
(repo modules CG-1..3+6a; gate rewrite CG-4+6b), each with kimi↔glm
cross-family review (quality-gates Gate 2.5).

---

## 5. Open Questions for Felix (blocking, in priority order)

1. **Budget cap value, window, owner, scope.** Daily? Weekly rolling? Global
   fleet cap vs per-CW vs per-workstream? Today's `daily_spend` already shows
   routstrd $18.75 — a cap that excludes paid tiers is decorative. Who can
   change it, and how fast during an incident?
2. **Deny vs defer semantics.** When over/near cap: hard DENY (cron skips
   silently, current quota-gate behavior) or DEFER (task re-queued with
   backoff)? For crons "defer" ≈ "skip and reschedule" — confirm. And what
   does DEFER mean for kanban dispatch (hold vs downgrade-to-flash first,
   per the existing Dimension-2 cascade)?
3. **Reconciliation with the 2026-08-20 cap deactivation.** The proxy's
   per-request spend caps were switched off with the rationale "markets and
   wallet balance decide routing". Is the dispatch-time cost gate a deliberate
   new layer on top, and should the per-request caps stay dead?
4. **GLM-5.3 exclusivity interaction.** Exclusive/free access tier — does its
   effective price enter the cost gate as measured $/M (≈0) or at opportunity
   cost? This decides whether the gate ever blocks GLM-5.3 work.
5. **`task_type` logging.** Approve per-model-only seeding for CG-2 (A3), and
   adding `task_type` to `api_calls` in the proxy so the predictor can
   graduate to (model, task_type)?
6. **Manual override escape.** Fail-closed needs a human escape hatch for
   `price_unknown`/`budget_unconfigured` denies — who (Felix only? any CW?),
   what mechanism (env var, marker file, CLI flag), and is it audited?
7. **Peak-hour policy.** The formula bakes peak multiplier into effective
   price; should peak merely price tasks higher (deferring only via budget) or
   actively DEFER non-urgent work off-peak (old Dimension-3 behavior)?
8. **Non-blocking:** where does the `price-first-api-routing` skill actually
   live? (Not found under `~/.hermes` — reference is currently unusable.)

---

## 6. Rollout Recommendation

**Shadow mode first. Direct cutover is not acceptable.** Rationale: three
live unknowns — token-prediction accuracy on real task mix (never measured),
budget-cap calibration (value not yet chosen, Q1), and a gate family whose
existing fallback chain is fail-open in at least two places (A2). A direct
cutover risks either fleet-wide cron silence (over-blocking) or a replay of
the 2026-08-15 phantom-availability class of bug (under-blocking).

**Mechanics:** after CG-4 deploys, the gate runs in shadow (decision computed
+ logged, legacy quota verdict still enforced) via the existing shadow-tap
pattern (`shadow_hook`/`shadow_logger`, `routing_shadow_decisions`-style
table). Minimum **5 full days** covering ≥1 z.ai 5h-window rollover and ≥1
weekly-window boundary.

**CG-6 bidirectional validation = the go-live gate.** Cutover to enforcement
is permitted only when ALL of:
1. **Under-budget direction:** 0 false denies of tasks the legacy gate
   allowed AND post-hoc actual cost confirms headroom existed (≥95% of shadow
   ALLOWs were genuinely affordable at realized token counts).
2. **Over-budget direction:** synthetic expensive task blocked at simulated
   95% budget; cheap task allowed at same state (handover verify items 1–2);
   shadow data shows the deny threshold firing on the top-decile real tasks.
3. **Backstops intact:** freeze marker still hard-blocks (live drill);
   locked-key block fires before cost logic.
4. **Telnyx both ways:** kimi-k3 dispatches while z.ai is locked (quota
   bypass) but is denied when Telnyx balance/budget is exhausted (cost
   enforced) — both demonstrated in shadow.
5. **Fail-closed audit:** zero shadow decisions whose provenance shows an
   "allow" derived from missing/assumed data; every fallback invocation logged.
6. **Cron sweep verified:** 0 stale `QUOTA GATE` phrasings post-CG-5; 3
   end-to-end cron spot-checks green.

Rollback: gate CLI ships with `--legacy` flag + retained legacy script; cron
lines reference the same filename so rollback = flip flag, no cron edits.

---

## 7. Verdict Recap

Decomposition **sound and approved-with-amendments A1–A5**; all load-bearing
assets verified real except `price-first-api-routing` (missing) and the IMPL
spec's fail-open posture (stale vs. the 2026-08-15 lesson — must be
superseded by this plan's §3 matrix upon approval). Kalman convergence being
red today does NOT block the plan — it constrains CG-2 to measured
historicals, which the plan adopts. No implementation begins until Felix
answers §5 Q1–Q3 (the rest can proceed in parallel with answers).
