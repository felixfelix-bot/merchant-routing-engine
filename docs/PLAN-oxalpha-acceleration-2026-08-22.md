# PLAN — oxalpha Acceleration: Compressed Path to Preferred Tier (v2)

**Date:** 2026-08-22 · **Author:** manager CW (consultant subagent) · **Status:** PLAN + kanban deltas — no production change until OX-2 lands.
**Branch:** `wt/glm53-quota-cleanup-t_da1b7c10` · **Supersedes the timeline of:** `PLAN-oxalpha-promo-2026-08-21.md` (v1, commit f997745 — §2 design, §2.4/§2.5/§2.6 safety policy and §6 teardown all remain normative; this doc compresses §4 phasing and §5 task chain only).

**Trigger:** operator directive 2026-08-22 — *"Make sure this key is in our live router and use it as MAIN DRIVER while free to save cost IF good enough. Shortest path to SAFELY using it live + getting workers to run on it."* Felix's D1–D6 approvals stand; D7 remains his verdict, on compressed evidence.

**What changed since v1:** (a) `OPENROUTER_OXALPHA_KEY` staged in `~/.hermes/.env` (KEYGATE t_025f6b5a done: `/api/v1/key` 200, usage $0.00 — but **`limit: null`, the $0 spend cap is NOT set**); (b) per-response `usage.cost` now returns **`None`** (was `0` on the 08-21 probe) → per-response spend verification is gone; spend truth moves to `/api/v1/key` **usage delta**; (c) 6 of 6 promo days remain (hard end 2026-08-28T00:00Z).

---

## §0 VERDICT UP FRONT

**The compressed timeline is sound** — conditionally. It is sound *because* the OX-2 alias design (§5) makes live canary traffic **strictly no worse than today** on the failure axis (any oxalpha error/timeout falls through to the untouched zai-first chain), which converts "shadow vs live" from a safety question into a **quality-audit** question. The 2–3-day natural-traffic shadow of v1 is replaced by: ~6 h scripted eval on real task shapes (OX-3a, starts today, zero proxy dependency) → 12 h live canary on non-gated digesters with fallback (OX-3b rung 1) → pre-authorized widen to the full low-stakes worker lane (rung 2) → auto-teardown at expiry. Two HARD preconditions gate every live rung: **key spend-limit $0 verified** (Felix, 30-second dashboard action — currently MISSING) and **OX-3a pass criteria** (§3.4). Without the $0 limit, nothing goes live — no exceptions, because `cost=None` removed our per-response tripwire and the in-process kills are best-effort detectors, not guarantees.

What compression gives up: distribution coverage (60 scripted items ≠ 2–3 days of natural traffic mix) and multi-day drift observation. Mitigated by: deterministic ground-truth checks on half the eval set, a daily N=20 live-output spot audit during canary (§5.4), and the fallback semantics above.

---

## §1 HONEST SAVINGS MATH (do not oversell)

z.ai is a **fixed-$ subscription** — routing zai-bound traffic to oxalpha saves **$0.00** there. Real, quantifiable value, in order of size:

| # | Source | Mechanism | Honest magnitude (6-day window) |
|---|---|---|---|
| 1 | **Quota headroom** (largest) | Flash-lane traffic moved off the ours+friend zai keys frees 5-hour/weekly quota windows for gated + manager work; fewer AMBER/RED lockouts, less DEFER of gated work | Not directly cash. Upper bound = the sub's value; realistic framing: extends gated-work runway by whatever fraction of request volume the flash lane carries (~40 worker profiles + ~60 crons; flash/simple/mechanical/chat classes are the bulk by count). Expect noticeably fewer friend-key lock events. |
| 2 | **Ollama-gap coverage** | Ollama Cloud 429-exhausted until **Monday 2026-08-24 00:00 UTC** (~1.5 d away). Until then, zai overflow lands on **paid** PPQ/routstrd failover. oxalpha absorbs flash-lane overflow at $0 | Event-driven. Incident-class reference: routstrd burned **$18.81/day** under dual-exhaustion. If the weekend produces even one pressure spike, avoided burn is plausibly **$2–20**; quiet weekend → ~$0. |
| 3 | **Paid-failover burn avoided (rest of promo)** | Same mechanism Mon–Thu whenever both zai keys are hot and gates would fail work out to paid tiers | **$0–10/day** typical, spiky. Live PPQ balance probe was unreachable from this box today (no `PPQ_API_KEY` on this host; DQ05 context VM unreachable) — pull `dq05_ppq` + `api_burn.db` on the manager box to tighten these bounds before the rung-2 decision. |
| 4 | **Tier-upgrade avoidance** | Research/review-high work that would justify glm-5.2/5.3 externals can defer the upgrade if oxalpha evals well | $0 unless quota pressure would otherwise have forced it. |
| 5 | **Free frontier-model eval** | 60-item rubric comparison vs glm-5.3 on OUR task shapes, $0 | Intelligence we could not otherwise buy. |

**Bottom line:** direct cash savings are **small — order $10–40 across the whole window, event-driven**. The real prizes are (1) quota headroom during gated-work crunches, (2) not paying for the ollama gap this weekend, (3) the eval. The plan is still worth it — but nobody should book $100s of savings.

---

## §2 COMPRESSED TIMELINE (T = OX-3a dispatch, today Sat 2026-08-22)

| When | Event | Gate that must be green |
|---|---|---|
| T+0 | Dispatch **OX-3a** (eval, §3) in parallel with **OX-2** (starts when CG-2 frees the worker, same day). Felix sets **key spend limit $0** (30 s, dashboard). | KEYGATE done ✓ |
| T+2 h | OX-3a fixtures built; staged ramp fires (5 → 60 items → burst probe) | §3.6 staged ramp |
| T+6 h | **OX-3a report**: rubric verdict vs glm-5.3, refusal rate, latency p50/p95 at effort low & max, 429 behavior, usage-delta $0 check | §3.4 pass criteria |
| T+8 h | OX-2 landed + smoke (task-type opt-in path live, shadow only) | OX-2 gates (v1 §5) |
| T+8 h | **Rung 1 flip** — alias ON for `bulk_summarize` on the flash lane (non-gated digesters only). Live main-driver traffic begins. | §7 invariants: limit==0 VERIFIED + OX-3a PASS + probe OK |
| T+20 h (Sun) | Rung-1 12 h watch report → **Rung 2** widen (pre-authorized, §5.3) if criteria met | §5.3 auto-widen criteria |
| Mon 08-24 00:00 Z | Ollama Cloud resets; oxalpha **stays preferred** for the aliased lane (free > flat-sub quota burn); ollama re-enters the normal fallback chain behind zai as before | — |
| Mon–Thu | Steady state; daily spot audit N=20 (§5.4); rung 3 only via Felix D7 amendment (default: never) | — |
| **Fri 08-28 00:00 Z** | **Auto-flip**: guard expiry disables tier + pessimistic pricing ($10/$30). Worst case if humans asleep: traffic silently reverts to today's chain. | Already built (OX-1, 66f1652) |
| Fri daytime | Manual teardown per v1 §6 (delete block + restart; archive artifacts) | v1 §6 unchanged |

v1 timeline for comparison: eval alone was 2–3 days, live routing "maybe, after D7, before 08-28". Compression: eval 6 h, live rung 1 ~T+8 h, full low-stakes lane by Sunday, teardown unchanged.

---

## §3 OX-3a — SCRIPTED RUBRIC EVAL (starts TODAY; no proxy dependency)

Replaces the first half of v1 OX-3. Fires **directly at OpenRouter** with `OPENROUTER_OXALPHA_KEY` (read from `~/.hermes/.env` at runtime — key never committed, never logged). glm-5.3 baseline runs the same prompts through our own zai sub via `localhost:9099` (`model: "glm-5.3"`, marginal cash $0). OX-2 is NOT a dependency — this runs whether or not the proxy wiring has landed.

### 3.1 Eval set — derived from REAL worker task types (N=60 primary)

| Shape | N | Source | Deterministic ground truth? |
|---|---|---|---|
| Code-review verdicts | 15 | Diff + changed-file excerpt from recent REAL worker PRs on this repo (sanitized: no keys, no customer data — v1 §2.5 applies); ask for verdict {approve / request-changes / block} + 3 bullet reasons | **Yes** — seeded with the verdict the merged decision actually took |
| Firmware/build summaries | 15 | Truncated real build/log digests (our own non-sensitive logs) → 5-bullet executive summary with failure/no-failure call | **Yes** — call matches known outcome |
| Doc writing | 15 | Real worker-task doc prompts (runbook section, changelog entry from a real commit message set) | Rubric-only |
| Structured JSON extraction | 15 | Real-ish payloads (invoice/alert/config text) → strict-schema JSON (`response_format` supported per models API) | **Yes** — `json.loads` + schema-key check |

Plus: **10 refusal probes** (work-plausible but politically-adjacent, e.g. summarize a sanctions-news paragraph — measures the community-reported guardrail on OUR content class) and a **10-item latency micro-set** (1–3 k-token digest prompts, the cron-digester shape).

### 3.2 Reasoning-effort handling (mandatory reasoning, default max)

Mandatory reasoning is the biggest behavioral risk: effort=max can mean minutes of latency and thousands of hidden tokens on a cron digest. Every primary item runs at **`reasoning_effort: "low"`** (the lane we would actually route). A **20-item subset runs BOTH low and max** to quantify the quality/latency tradeoff — if max is materially better AND low fails, that is a rung-1 blocker (we will not put effort=max latency on digester crons). `max_completion_tokens: 8192` everywhere so reasoning cannot truncate content (`finish_reason == "length"` → item scored 1 and flagged; it is a real failure mode, not noise).

### 3.3 Scoring — blind rubric + deterministic checks

- **Blind rubric 1–5** per item: responses stripped of model labels, shuffled, graded by the manager (glm-5.3) against a per-shape 4-criterion rubric (correctness, completeness, format compliance, actionability). Same grader, same rubric, both models → paired comparison.
- **Deterministic half:** verdict-accuracy vs seed, outcome-call accuracy, JSON schema validity — no LLM judgment involved.
- **Measured per item:** latency, TTFB if stream, `usage` tokens (prompt/completion), finish_reason, refusal (hard-refusal classifier: explicit refusal or empty content).
- **Spend truth = `/api/v1/key` usage delta** (§3.6) — the `cost: None` field is recorded but trusted for NOTHING.

### 3.4 PASS CRITERIA (all must hold for rung 1; any miss → no live traffic, teardown default)

1. **Quality non-inferiority:** per-shape blind mean rubric oxalpha@low ≥ glm-5.3 mean − **0.3** (1–5 scale), AND ≥ **90%** of items within ±1 of their baseline pair.
2. **Deterministic floor:** JSON schema validity ≥ **95%**; code-review verdict accuracy ≥ **85% of glm-5.3's accuracy**; summary outcome-call accuracy ≥ **90%**.
3. **Refusal:** **0 hard refusals** on the 60 work items; ≤ 1/10 on the sensitive probes (flag, not necessarily fail).
4. **Latency @low, 1–3 k-token prompts:** p50 ≤ **25 s**, p95 ≤ **60 s** (glm-5.3 baseline recorded alongside; non-inferiority not required — digests are deferrable — but the cap is).
5. **Effort A/B:** low must not lose > 0.5 mean rubric to max on the 20-item subset.
6. **Spend:** `/api/v1/key` usage delta == **$0.000** across the entire campaign INCLUDING the burst probe.

### 3.5 Rate-limit discovery probe (gate for any rung-2 widen)

No advertised limits, no `x-ratelimit-*` headers observed (v1 §0.1). Before widening beyond rung 1: **10 rps × 30 s = 300 requests** (tiny prompts, `max_tokens 16`, effort low), concurrency 10. Record: 429 count, any `x-ratelimit-*`/`retry-after` headers (log ALL — empirical discovery), p95 latency under load, circuit behavior after the burst. **Probe gate:** ≥10% 429 or p95 > 60 s under burst → rung 2 must ship with an in-flight concurrency cap (start 4) and staggered cron alignment; clean probe → cap 8.

### 3.6 Staged ramp (spend exposure control — because `limit` may still be null)

Ordered, each stage checks `/api/v1/key` usage delta == 0 before the next fires:
1. **5-call canary** (~15 k tokens total). If promo has silently ended at pessimistic $10/$30 per 1M, worst-case exposure ≈ **$0.20** — the price of finding out. Also re-verify `GET /api/v1/models` still lists `$0.00/$0.00` before stage 1.
2. **Full 60 + 20 + 10 + 10 set** (~0.25 M in / 0.08 M out ≈ **$4.90** worst case if stage-1 detection somehow failed — it won't).
3. **Burst probe** (§3.5, ~0.15 M in ≈ **$1.70** worst case).

With Felix's $0 limit set, all three numbers collapse to a hard **$0** and the ramp is pure belt-and-suspenders. Script aborts permanently on any usage delta > 0 and writes an `anomaly_events`-shaped row via the OX-1 helper.

---

## §4 OX-2 — SAME TASK, THREE ADDITIVE ITEMS

OX-2 (t_2ed46556, ready) proceeds exactly per v1 §5 plus these additive wiring items (same task, same worker, still no existing cell touched):

1. **`preferred_for` alias block** (§5.1–§5.2) — the proxy-side main-driver mechanism.
2. **Kill-switch file** `~/.hermes/bot/.oxalpha_alias_off` (mirrors the `.pressure_routing_disabled` pattern already in the proxy): present → alias skipped, tier still usable for opt-in task types. One `rm` to re-arm after human review.
3. **5-min usage-delta poller**: `GET /api/v1/key` (free, read-only) → cumulative `usage` delta > 0 → OX-1 `_kill_nonzero` via `observe_charge(delta)` semantics (note: for a fresh key with usage 0, the kill direction is **usage UP**, not wallet down — wire accordingly; the existing `observe_wallet_delta(negative)` path stays for balance-based checks).

---

## §5 MAIN-DRIVER SEMANTICS + CANARY LADDER

### 5.1 What "preferred tier while free" means mechanically

A **pre-chain preferred attempt**, NOT a member of the generic failover chain:

```
request(model, task_type, has_images)
  └─ if promo_guard.enabled && in_promo && alias_on && model ∈ preferred_for.models
        && task_type ∈ preferred_for.task_types && !has_images:
         1. try oxalpha: stealth/ox-alpha, effort=low, max_completion_tokens=8192,
            upstream timeout 90 s, ONE attempt, no retry
         2. ANY failure (4xx/5xx/timeout/circuit-open/guard-kill) → fall through
  └─ existing zai-first chain, byte-identical to today (ours → friend → ollama →
     deepinfra → ppq → … Kalman/LiveRouter untouched)
```

- **Workers change NOTHING.** They keep sending `model: "glm-4.5-flash"` etc. The rewrite is proxy-side. Rollback = kill-switch file or delete config block (v1 §6).
- **Worst case = today's behavior + ≤90 s once** on the aliased lane (deferrable digests tolerate this; that is why the ladder starts there).
- oxalpha **never enters `EXTERNAL_PROVIDERS`**, so no Kalman/price interaction, no CG-2/CG-3 dependency, and no generic-failover path can ever select it (v1 §2.2 invariant preserved). CG-6 ceiling still backstops as designed.
- **Expiry auto-flip (already built, OX-1):** at 08-28T00:00Z the guard disables the tier and flips effective pricing to $10/$30 — the alias condition `guard.enabled && in_promo` goes false mid-request safely; the next request just takes the normal chain. No cron needs to notice.

### 5.2 Alias map (which lanes, which rungs)

| Lane (model name callers send) | Rung | Alias target | Notes |
|---|---|---|---|
| `glm-4.5-flash`, `task_type=bulk_summarize`, **non-gated digester crons** | **1** (T+8 h) | `stealth/ox-alpha` @ low | ~6–10 lowest-stakes always-on digesters, named in config |
| `glm-4.5-flash` + `glm-4.5-air`, task_types `{simple, mechanical, chat, bulk_summarize}` | **2** (pre-authorized, Sun) | same | The "output gets vetted" cheap classes (proxy line-comment: workers' output gets vetted) |
| Gated / quality-gated lanes (CG-1-gated deferrable work, `coding`/`review`/`research` high-tier types) | **NEVER by default** — operator economics: cheap models failing gates cause rework burn (DEFER > downgrade) | — | Only via explicit D7 amendment after ≥36 h clean rung 2 |
| `glm-5.2` (manager standard/research) | excluded by default (rung-3 candidate ONLY via D7 amendment) | — | Manager quality floor lives here (`MANAGER_FALLBACK_MODEL`) |
| `glm-5.3` (manager heavy) | **NEVER during promo** | — | Non-negotiable |
| Image-bearing requests | excluded at rungs 1–2 (D2 vision allowlist unchanged; vision stays opt-in `X-Task-Type: vision` exactly as v1 §2.5) | — | oxalpha's native vision remains a separate, already-approved opt-in |

### 5.3 Canary ladder + ABORT TRIGGERS (per rung; all auto-revert via kill-switch file, all log an anomaly row)

| Rung | Scope | Duration | Abort triggers (ANY → alias off, traffic silently reverts) |
|---|---|---|---|
| 0 | OX-3a scripted eval (direct, no prod) | ~6 h | §3.4 criteria miss → no rung 1 at all |
| 1 | Flash lane, `bulk_summarize`, non-gated digesters | 12 h | • oxalpha error rate ≥ **5%** over any 15-min window • p95 latency > **60 s** over 30 min • circuit breaker (5 consec / 300 s) trips **3×** in 1 h • **any** usage delta > 0 (OX-1 kill, tier dead, human-only re-enable) • spot-audit mean rubric drop > **0.5** vs OX-3a baseline • promo guard expiry/402/kill fires |
| 2 | Full flash + air low classes (§5.2) | rest of promo, audited | Same as rung 1, PLUS: aggregate zai-fallback rate > **30%** over 6 h (alias is net-negative — off) • daily spot audit fails (§5.4) |
| 3 | (default NEVER) glm-5.2 lane / gated lanes | — | Requires Felix D7 amendment in writing |

**Rung-2 auto-widen pre-authorization** (so widening doesn't wait on chat latency): rung 2 fires WITHOUT further Felix input iff ALL of: rung 1 ran ≥ 12 h with zero abort triggers; rung-1 live volume ≥ **50** oxalpha-served requests (natural-traffic N replaces v1's shadow N); OX-3a pass criteria all held; burst probe clean per §3.5. Otherwise escalate to D7 with the rung-1 report.

### 5.4 Live quality audit (the compensation for compressed eval)

Quality regressions do NOT raise HTTP errors — the fallback cannot catch them. Daily during canary: sample **N=20** oxalpha-served responses from `api_calls` (task_type-tagged, CG-5), blind-rubric grade against the same per-shape rubric, compare to OX-3a baseline. Mean drop > 0.5 or any deterministic-shape failure streak (3 consecutive bad JSON/verdicts) → kill-switch + D7 escalation. This is the one task where the compression genuinely spends human/manager attention; budget 30 min/day.

---

## §6 SAFETY INVARIANTS (hard, in order)

1. **`limit == 0` on the key is a HARD precondition for ANY live rung** (rung 1 included — not just shadow). Verify `GET /api/v1/key` returns a numeric `0` limit (not `null`) at rung-1 flip time and re-check at each widen. It is currently `null` — **Felix owes this 30-second action before Sunday.** Rationale: with `cost=None`, the $0 limit is the ONLY guaranteed spend cap; every in-process kill is a best-effort detector with a 5-min worst-case blind window.
2. **Belt-and-suspenders stays fully armed regardless:** OX-1 cost>0 kill (any nonzero observed charge), 5-min usage-delta kill (§4.3), 402 → disable-for-promo-remainder, expiry auto-flip to $10/$30 pricing (priced out of every path even if a bug leaves the alias on).
3. **No uncapped paid exposure at any point:** oxalpha never joins `EXTERNAL_PROVIDERS`; main `OPENROUTER_API_KEY` stays dormant (D6); staged-ramp exposure caps (§3.6) bound the pre-limit worst case at cents-to-dollars, and at $0 once the limit lands.
4. **Zero prod changes before OX-2 lands** — OX-3a is read-only against OpenRouter + our own proxy as a *client*; it touches no proxy internals.
5. **p20 baselines stay clean:** promo-tagged `price_observations` rows excluded (OX-1 filter, shipped); canary `api_calls` rows carry `provider=oxalpha` + task_type and never feed the percentile band.
6. **Teardown runbook (v1 §6) remains the default outcome** and stays valid at every rung: delete the `oxalpha:` block (+ alias sub-block) + restart. The auto-flip covers the deadline even if nobody shows up Friday.

---

## §7 REVISED TASK CHAIN (kanban deltas vs OX-2 → OX-3 → D7 → OX-4)

```
v1:  OX-1 ──► OX-2 ──► OX-3 (2–3 d shadow) ──► D7 ──► OX-4
v2:  OX-1 ✓   OX-2 (today, +alias/+kill-switch/+usage-poll)
                │
                ├─► OX-3a (NEW, parallel, ready NOW — no OX-2 dep): eval + burst probe + spend check
                │
                └─► OX-3b (retitled OX-3): rung-1 flip + 12 h watch ──► [auto-widen rung 2 per §5.3]
                              │
                              └─► D7-GATE (reframed: compressed evidence; only for beyond-rung-2) ──► OX-4 (enable-runbook execution + teardown default)
```

Also update `~/.hermes/scripts/ox_pipeline_advance.py`: OX-3a parallel to OX-2; OX-3b (t_55d38878 retitle) deps = OX-2 + OX-3a; FINAL stays OX-4.

### Paste-ready task bodies

**OX-3a (NEW — dispatch today):**

```text
TITLE: OX-3a: scripted rubric eval + rate-limit probe (direct OpenRouter, no proxy dep)
REPO: ~/merchant-routing-engine (branch wt/glm53-quota-cleanup-t_da1b7c10). READ docs/PLAN-oxalpha-acceleration-2026-08-22.md §3 (your spec) + v1 plan §2.5 (data policy). AGENTS.md applies. NO key material in commits/logs — read OPENROUTER_OXALPHA_KEY from ~/.hermes/.env at runtime.

TASK: scripted eval per acceleration plan §3, ~6 h total, ZERO production change:
(a) fixtures: 60 primary items across 4 REAL shapes (15 code-review verdicts w/ seeded ground truth from recent merged PRs — sanitized; 15 build/firmware summaries w/ known outcome; 15 doc-writing prompts from real worker tasks; 15 strict-schema JSON extractions) + 10 refusal probes + 10 latency micro-set (1–3k-token digests). Sanitize per v1 §2.5: no secrets/customer/repo-dump data.
(b) harness scripts/oxalpha_eval.py + scripts/ox_eval_report.py: paired runs — oxalpha (reasoning_effort=low, max_completion_tokens=8192) vs glm-5.3 baseline via localhost:9099 (model glm-5.3, own sub, $0 marginal). 20-item subset ALSO at effort=max (A/B). Blind rubric grading (labels stripped, shuffled) by manager glm-5.3, 4 criteria, 1–5 scale; deterministic checks for verdict/outcome/JSON-schema shapes.
(c) STAGED RAMP with spend gate (plan §3.6): re-verify GET /api/v1/models shows $0.00/$0.00 → 5-call canary → check /api/v1/key usage delta == 0 → full set → delta check → burst probe 10 rps × 30 s (300 calls, max_tokens 16, effort low, concurrency 10) recording 429 count + ALL x-ratelimit-*/retry-after headers + p95 under load. ANY usage delta > 0 → abort campaign, write anomaly_events-shaped row via src/promo_tier.py helper, report immediately.
(d) report docs/OX3a-eval-report-2026-08-22.md: per-shape rubric means (ox vs baseline, low vs max), deterministic accuracies, refusal counts, latency p50/p95 per shape, 429/burst findings, usage-delta evidence, and an explicit PASS/FAIL verdict against plan §3.4 criteria 1–6. This report feeds rung-1 flip and Felix D7.

TESTS: harness unit tests (fixture loading, blind-shuffle determinism, schema checker, ramp-abort path with mocked usage delta). No live calls in CI.
DEPS: none (parallel to OX-2; KEYGATE done).
QUALITY GATES: standard v3.1.0 block (TDD, TESTS-PASS, REVIEW, DOCS-SAME-COMMIT, ATOMIC, PUSH --no-verify after subcommand if hooks block).
```

**OX-2 (AMEND existing t_2ed46556 — append):**

```text
APPEND TO SCOPE (acceleration plan §4): in addition to v1 §5 OX-2 scope, wire:
(1) preferred_for alias block in the oxalpha config: {models, task_types, reasoning_effort: low, max_completion_tokens: 8192, upstream_timeout_s: 90, single-attempt no-retry}; pre-chain preferred attempt ONLY while PromoTierGuard.enabled && in_promo && alias conditions match && request has no images; ANY failure falls through to the existing zai-first chain byte-identically. oxalpha NEVER enters EXTERNAL_PROVIDERS / generic failover / LiveRouter candidates.
(2) kill-switch file ~/.hermes/bot/.oxalpha_alias_off (pattern of .pressure_routing_disabled): present → alias skipped.
(3) 5-min usage-delta poller: GET /api/v1/key cumulative usage delta > 0 → OX-1 kill path (note direction: fresh key usage starts 0; kill on usage INCREASE) + anomaly row.
Contract tests to add: alias-off file respected; fall-through on 429/timeout/guard-kill; glm-5.2/glm-5.3 models NEVER aliased; image-bearing requests NEVER aliased; gated task types never aliased at rung 1 config; zai chain behavior byte-identical with tier disabled.
```

**OX-3b (RETITLE t_55d38878, replace body):**

```text
TITLE: OX-3b: canary rung 1 flip + 12 h watch (live, fallback-protected)
REPO: ~/merchant-routing-engine (branch wt/glm53-quota-cleanup-t_da1b7c10). READ docs/PLAN-oxalpha-acceleration-2026-08-22.md §5–§6 (your spec). AGENTS.md applies.

ENTRY CHECKLIST (ALL must be green or DO NOT FLIP): (1) OX-2 merged+restarted, /health 200, oxalpha tier visible shadow-only; (2) OX-3a report PASS on §3.4 criteria 1–6; (3) GET /api/v1/key limit == 0 (numeric, not null) — if null, STOP and ping manager (Felix owes the $0 cap); (4) kill-switch file absent by deliberate choice.
TASK: flip rung 1 — alias config: models [glm-4.5-flash], task_types [bulk_summarize], non-gated digester cron list (~6–10 named crons). Then 12 h watch with abort automation per plan §5.3 rung-1 triggers (error ≥5%/15 min, p95 >60 s/30 min, breaker 3×/h, ANY usage delta > 0, spot-audit drop >0.5): implement as a watch script polling api_calls + /api/v1/key that touches .oxalpha_alias_off on trigger + writes an anomaly row. Report at +12 h: volume served, error rate, latency p50/p95, fallback rate, usage-delta evidence, spot-audit N=20 rubric vs OX-3a baseline. If §5.3 auto-widen criteria ALL hold → execute rung 2 config widen in the same task (pre-authorized; document the checklist evidence), else leave rung 1 and escalate to D7-GATE.
FILES: config alias flip, scripts/ox_canary_watch.py, docs/OX3b-canary-report-2026-08-2X.md.
DEPS: OX-2 merged, OX-3a done.
QUALITY GATES: standard v3.1.0 block.
```

**D7-GATE (t_165dea27 — reframe body):**

```text
REFRAMED (acceleration plan §7): D7 verdict now = (a) ratify rung-2 widen AFTER THE FACT if auto-widen already fired on §5.3 criteria (evidence pasted from OX-3b report), or halt it; (b) approve/deny anything BEYOND rung 2 (glm-5.2 lane, gated lanes, live vision traffic) — default deny; (c) if OX-3a FAILED or rung 1 aborted → confirm teardown default (v1 §6). Manager completes only with Felix's chat verdict. Compressed evidence = OX-3a report + OX-3b 12 h watch; no 2–3 day campaign exists anymore.
```

**OX-4 (t_aa11cc50 — minor reframe):**

```text
REFRAMED: the enable runbook is now PRE-WRITTEN and executed incrementally by OX-3b (rung flips are config-only + restart). OX-4 becomes: (a) verify end-state at whatever rung was reached (alias serving, guards armed, p20 clean of promo rows); (b) execute teardown at/after 2026-08-28T00:00Z (auto-flip makes this safe even if late): delete oxalpha block + alias sub-block, restart, verify /v1/pricing + /v1/models show no oxalpha, zai chain regression-green, archive OX-3a/3b reports; (c) if Felix D7 approved beyond-rung-2 scope, execute that flip with the same checklist discipline.
```

---

## §8 WHAT FELIX STILL OWNS (nothing else)

1. **Set key spend limit $0** — openrouter.ai dashboard → key `sk-or-v1-08c…2af` → spend limit 0. 30 seconds. **Hard precondition for rung 1 (Sun ~T+8 h).** Until set, everything else can proceed (OX-3a staged ramp bounds exposure at ~$0.20 lead-item), but nothing goes live.
2. **D7 verdict on compressed evidence** (§7 reframed gate) — only if he wants beyond-rung-2 scope, or to ratify/halt an auto-widen, or on any abort. Default path (rungs 1–2, teardown Friday) needs no further Felix action once the limit is set.

## §9 RISKS (top 5)

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | **Promo terms change silently mid-window** (price flips paid; `cost=None` hides it per-response) | Low–Med (promos of this class do end early) | High (uncapped paid burn — the routstrd-$18.81 class) | $0 key limit = structural cap (hard precondition #1); 5-min usage-delta kill; staged ramp; expiry pessimistic flip. Residual: ~5 min × burst-rate spend IF the limit were somehow bypassed — with limit=0, OpenRouter 402s instead. |
| 2 | **Silent quality regression on worker lane** (success-status garbage → rework burn, the exact operator-economics failure) | Med (compressed N) | Med–High | Non-inferiority gate vs glm-5.3 BEFORE rung 1; deterministic ground-truth on half the eval; daily N=20 blind spot audit; abort on mean drop >0.5; blast radius capped at low-stakes vetted-output classes; gated/manager lanes excluded. |
| 3 | **Unannounced rate limits at fleet concurrency** (60 crons; 429 storms or queue-forever latency) | Med–High | Med | 10 rps burst probe BEFORE rung 2; per-tier in-flight cap (4–8); circuit breaker 5/300 s; 90 s single-attempt timeout → fall-through; alias-off kill switch; ladder starts at 6–10 crons not 60. |
| 4 | Model vanishes / vendor pulls promo early | Med (anonymous "stealth" vendor, 30d uptime null) | Low (with fallback) | Fallback chain = today's behavior; breaker + error-rate aborts handle it; teardown runbook default. |
| 5 | OX-2 slips past today (CG-2 contention on the shared worker) | Med | Med (compresses canary window) | OX-3a is fully parallel (no proxy dep) so evidence lands tonight regardless; rung 1 can flip as late as Tue and still give 3 days of main-driver value; promo math degrades gracefully. |

---

## §10 UNCHANGED FROM v1 (still normative)

§2.1 config shape, §2.4 spend-guard philosophy, §2.5 data-sensitivity allowlist (D2), §2.6 backoff policy, §3 cost-gate interplay (p20 promo-row exclusion — shipped), §6 teardown runbook. This doc adds §4/§5 of OX-2 only as explicitly scoped in §4 above.

*No production system was modified by this document. The only artifact is this file.*
