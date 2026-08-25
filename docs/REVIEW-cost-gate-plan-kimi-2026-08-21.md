# REVIEW: PLAN-cost-gate-reform-2026-08-21.md (kimi cold adversarial review)

**Reviewer:** kimi-family consultant (cold review, no implementation involvement)
**Date:** 2026-08-21
**Under review:** `docs/PLAN-cost-gate-reform-2026-08-21.md` (commit a772474)
**Reference:** `docs/HANDOVER-cost-gate-reform-2026-08-21.md`
**Method:** both docs read in full; every cheaply-checkable factual claim verified against
repo/profile/bot files and live sqlite DBs. Full test suite NOT run (per instructions);
`pytest --collect-only` only.

---

## Verdict: **CHANGES-REQUIRED**

The CG-1..CG-3 architecture is sound and the plan's fact-base is unusually accurate
(every line-number citation I checked is exact — see §3). But the **deployment tasks
(CG-4/CG-5) as written would leave 7 of 31 gated crons on a third, fail-open,
stale-ours-key gate script while the plan's own exit criteria report success**, and the
§3 fail-closed matrix contains one policy decision (infra-down → legacy verdict) that
the plan makes unilaterally and that belongs to Felix. Changes are confined to CG-4/CG-5
scope, the §3 matrix, and the open-question set; no restructuring of CG-1..CG-3 is needed.

---

## 1. Findings

### F1 — BLOCKER — Three gate entry points exist; CG-4's "zero cron changes" claim is false for 7/31 crons

Verified by parsing `~/.hermes/profiles/manager/cron/jobs.json`: the 31 `QUOTA GATE`
crons reference **three different scripts**:

| Script | Cron count | Freeze-marker check | Data source | Missing-data posture |
|---|---|---|---|---|
| `zai-quota-gate.sh` | 17 | ✅ yes (line 13) | zai_state.json → proxy /quota | fail-open (`exit 0`, lines 59–60) |
| `zai-quota-gate.py` | 7 | ❌ **none** | proxy /quota → zai_state.json | fail-open (`no_data_optimistic`, line 64) |
| `quota_gate.py` | 7 | ❌ **none** | zai_state.json ONLY | fail-open **twice** (no file → 0; corrupt file → 0) |

The plan relegates sibling scripts to "Minor note (i): audit whether any cron references
them" — but this is not hypothetical: **23% of the fleet calls `quota_gate.py` today.**
That script is the worst of the three: it never consults the proxy, has no freeze-marker
check, and **still gates on `ours_available`** — the key declared dead 2026-08-15, whose
stale `true` readings are the exact phantom-availability bug class the `.sh` header
explicitly warns against ("Do not re-add ours checks: zai_state.json may emit stale
ours_available=true with empty data").

Consequences for the plan as written:

- CG-4's core deployment claim — "same filename = zero cron changes needed at this
  stage" — covers at most 24/31 crons (17 `.sh` via the new delegation + 7 `.py`).
- CG-5's exit criterion ("grep sweep returns 0 stale `QUOTA GATE` phrasings") matches
  *phrasing*, not *script path*. The 7 `quota_gate.py` lines say "QUOTA GATE: Run
  'python3 …/quota_gate.py' first" — if the new line spec keeps the QUOTA GATE phrasing
  (as CG-5 intends: "semantic, not syntactic"), the sweep can return 0 stale lines while
  7 crons remain ungated by the cost gate and still exposed to the ours-stale bug.
- Go-live would therefore be declared with a split-brain fleet: 24 cost-gated crons,
  7 on a legacy fail-open gate the plan never mentions as a risk.

**Amendment (required):**
1. CG-4 scope must state explicitly that deployment to `zai-quota-gate.py` covers only
   the 7 direct + 17 delegated crons, and that the 7 `quota_gate.py` crons require a
   decision (see new blocking question below).
2. CG-5's audit script must match **script paths** (`zai-quota-gate.sh`, `.py`,
   `quota_gate.py`, and the ~10 other sibling gate scripts), not just phrasings.
3. Add blocking question: *"Q9: quota_gate.py consumers — migrate their 7 cron lines to
   the canonical gate (requires line edits, breaking the 'no cron changes' property),
   redeploy quota_gate.py as a delegating shim, or leave them on the legacy gate
   (accepting a permanently split fleet)?"* This is undecidable by the CW; it changes
   CG-4/CG-5 scope and the rollback story.

### F2 — MAJOR — Freeze-marker backstop is NOT "retained verbatim"; today 14/31 crons have no freeze-marker protection at all

The plan's CG-4 says "freeze-marker check → locked-key check (both retained verbatim as
hard backstops)". Verified: only `zai-quota-gate.sh` checks `.dispatch_frozen`.
`zai-quota-gate.py` and `quota_gate.py` contain **no freeze-marker check** — so for 14
of 31 crons, Felix's emergency freeze marker is *already* ineffective today. The target
design (§3 matrix row 1: freeze → DENY, pre-empts everything) is correct, but:

- "Retained verbatim" is factually wrong for the `.py` path — the check must be
  **added** there (and to whatever happens to the `quota_gate.py` seven, per F1).
- The plan's risk narrative ("backstop preserved in all phases") misses that the
  transition itself is an opportunity to finally make the backstop uniform — and that
  until CG-4 lands, a freeze drill would silently pass 14 crons.

**Amendment:** CG-4 must ADD the freeze-marker check as step 0 of the canonical CLI and
state that pre-CG-4 the backstop is non-uniform (14/31 crons unprotected). CG-6's
"go-live criterion 3: freeze marker still hard-blocks (live drill)" must be defined as
drilling a cron on **each** entry-point path, not one.

### F3 — MAJOR — Shadow-mode fidelity is underspecified; criterion 5 may be unmeasurable as designed

§6 says shadow runs "via the existing shadow-tap pattern (shadow_hook/shadow_logger)"
(both exist in `src/` — verified), while go-live criterion 5 demands "zero shadow
decisions whose provenance shows an 'allow' derived from missing/assumed data; **every
fallback invocation logged**". The fallback invocations that matter (proxy unreachable →
state file → legacy verdict; per §3's last matrix row) live in the **deployed gate
CLI**, not in the pure `evaluate_cost_gate()` function. If the shadow harness calls the
repo module directly with synthetic inputs, it validates the pure function and never
exercises the deployed artifact's fallback chain — the exact fail-open paths A2 warns
about stay untested through the entire campaign.

**Amendment:** §6 must specify that shadow mode instruments the **deployed
`zai-quota-gate.py` CLI end-to-end** (its real /quota fetch, its state-file fallback,
its freeze/locked checks), logging both the cost verdict and which fallback layer
produced it, on every live cron invocation — not a parallel harness around the pure
module. Pure-module shadowing may supplement but not substitute.

### F4 — MAJOR — Infra-down fallback policy is decided unilaterally; it is the single most probable real-world fail-open path

§3's final matrix row: "Gate infrastructure error (proxy down) → Fall back to legacy
quota-gate verdict, log loudly — never 'allow' on absence of data." But the legacy
verdict on proxy-down **is** allow (`no_data_optimistic`). So the plan's own matrix
makes proxy-down → allow for any task, fail-closed in name only. The rationale (a cron
aborting on a dead proxy is itself an outage) is legitimate — which is exactly why this
is a Felix-level tradeoff between two incident classes (fleet-wide cron silence vs.
2026-08-15 under-blocking), not a consultant call. The plan embeds it in the
"for approval" design without flagging it as open.

**Amendment:** promote to blocking question: *"Q10: when gate infrastructure (proxy) is
unreachable, should the cost gate (a) degrade to legacy quota verdict [= allow if state
file absent/stale], (b) DENY after N consecutive infra failures, or (c) DENY with a
time-boxed manual-override window?"* Until answered, §3's row should be marked
PROVISIONAL. Note Q6 (manual override) partially overlaps — merge if Felix prefers.

### F5 — MAJOR — `effective_price` runtime plumbing for the cron-side CLI is unspecified

§3's signature takes `effective_price` "from pricing_engine", and the handover says the
pricing engine is "live in dev, partially wired". The plan never says how the deployed
gate CLI obtains a pressure-adjusted price at cron runtime: there is no named queryable
service; the pressure curves need live quota utilization (proxy /quota), session/weekly/
monthly superposition state, and a peak-hour flag. If the CLI must embed this
computation, CG-4 is more than "0.5–1 day" and gains a second fail-closed input
(quota-state fetch failure → price_unknown → DENY, which is correct but must be stated).
The proxy's `/v1/dispatch_gate` endpoint does return `effective_price_per_m` (verified,
including the coarse fallback at `zai_proxy.py:4733`) — if the CLI is meant to consume
that endpoint, say so, and specify its task/model parameters and its own failure mode.

**Amendment:** CG-4 scope must name the effective-price source (endpoint vs embedded
computation), its input contract, and its failure semantics (fetch failure →
`price_unknown` DENY per §3).

### F6 — MINOR — §3 matrix row "predicted_tokens unavailable → DENY" is dead code against CG-2's own design

CG-2 specifies the predictor "must always return a number with a confidence flag"
(cold model → conservative default × penalty). If the predictor always answers, the
`no_token_history` DENY row can never fire. Not dangerous (still fail-closed), but
internally inconsistent — the real trigger should be "predictor infrastructure error"
(predictor module raises/unimportable), not "unavailable".

### F7 — MINOR — Non-z.ai, non-Telnyx provider paths are unspecified

§3 fully specifies z.ai (friend-key) and Telnyx paths. But `daily_spend` (verified live
in `~/.hermes/bot/zai_usage.db`) shows active tiers: routstrd, ollama_cloud ($16.68/7d),
openrouter ($9.17/6d), ppq ($2.05/4d). Which budget does an ollama-routed (glm-5.2,
$0.0155/M) or openrouter-routed task draw from? Today ALL crons gate on the z.ai friend
key regardless of provider; the plan doesn't say whether the cost gate keeps that
coupling or decouples per-provider. Ties into Q1 (cap scope) but deserves an explicit
sentence in §3/CG-3.

### F8 — MINOR — DEFER semantics (Q2) may require infrastructure that exists in no CG task

If Felix chooses DEFER-with-requeue over skip-silently, something must own the queue/
backoff — none of CG-1..CG-6 builds it, and CG-5 is sized (0.5d) assuming a line-only
change. Flag the contingency: Q2's answer can inflate CG-5 or add a CG-7.

### F9 — MINOR — Effort claim is happy-path; amendments add 1–2 days

Sum of per-task estimates ≈ 6.5d with CG-2∥CG-3 parallelism — internally consistent.
But F1 (7-cron migration or shim), F3 (shadow instrumentation of the deployed CLI), and
F5 (price plumbing) are all outside the estimate. Realistic range after amendments:
**6.5–10 implementation-days**; the ≥5-day shadow window is appropriate. "5–8 days"
should be restated or the delta explicitly owned.

### F10 — MINOR — Verification nits (no action beyond noting)

- `api_calls` now 86,545 rows (plan: 86,432 — live DB growth, fine); no `task_type`
  column confirmed; `cost_usd`/`session_id` present.
- Plan quotes "today: telnyx $1.20/17 calls" — cumulative `daily_spend` shows telnyx
  **$1,990.27 across 9 days**. Not a plan error (daily vs cumulative), but that history
  is essential calibration input for Q1: a daily cap sized to routstrd-scale (~$19)
  would have been blown by telnyx repeatedly.
- Untracked files in repo (`docs/HANDOVER-…`, `scripts/routstrd_funding_guard.py`) —
  handover doc referenced by the plan is not committed; commit it with the plan's next
  revision so the review chain is reproducible.

---

## 2. Soundness, safety, blind-spot assessment (summary)

**Soundness (CG-1..CG-6 decomposition/ordering):** SOUND. No circular dependencies
(CG-1 → CG-2∥CG-3 → CG-4 → shadow → CG-5 → go-live is a clean DAG). The reuse-first
bindings are real (verified: `dispatch_gate.py:evaluate_dispatch` + exported
`TASK_PROFILES`/`HARDWARE_SAFETY_MARGIN`, `will_exhaust` at `consumption_kalman.py:182`,
`shadow_hook.py`/`shadow_logger.py`, `routing_shadow_decisions` table, `daily_spend`).
One prerequisite gap: F5 (price plumbing) is an unstated CG-4 prerequisite.

**Safety (fail-closed):** The §3 matrix is genuinely fail-closed at the *pure-function*
level — with two exceptions: F4 (infra-down row is allow-in-disguise) and the deployment
gaps F1/F2 (crons that never reach the matrix at all). Freeze-marker backstop: preserved
in the target design, NOT preserved uniformly in the transition (F2). Silent-allow bug
paths: the quota_gate.py seven (F1) is a live one today.

**Blind spots covered:** migration/rollback (plan's `--legacy` flag + same-filename
deploy is good, but see F1 rollback gap for the unmigrated seven); shadow fidelity (F3);
quota-window interaction during transition (correct: legacy verdict enforced through
shadow — good); multi-key (Q4 raised; per-provider budget coupling missed, F7);
cron-prompt partial-migration risk (F1 — this is the big one).

**Open questions:** Q1–Q3 is the right blocking core but **incomplete**: F1 (Q9,
canonical script / the seven crons) and F4 (Q10, infra-down policy) must be added as
blocking. Q4–Q8 correctly non-blocking. The plan's unilateral spec-supersession of the
2026-07-29 IMPL-SPEC fail-open properties is acceptable *if* Felix approves this plan
explicitly against that supersession (plan §7 already frames it this way — keep).

**Effort:** see F9.

---

## 3. Fact-check table (plan claims vs. verification)

| Plan claim | Result |
|---|---|
| `evaluate_dispatch()` exists, tested, wired at `zai_proxy.py:4617` | ✅ exact (def at `dispatch_gate.py:222`; `GET /v1/dispatch_gate` at proxy :4617) |
| Coarse-check fallback at `zai_proxy.py:4733` | ✅ exact ("dispatch_gate module unavailable; coarse check") |
| `ConsumptionKalman.will_exhaust()` at `consumption_kalman.py:182` | ✅ exact |
| Spend caps deactivated 2026-08-20 (`_check_spend_cap` "always allows") | ✅ confirmed (proxy :2811) |
| Telnyx blended 5.40/M seed at proxy `:1307` | ✅ exact |
| `api_calls` has no `task_type` | ✅ confirmed |
| 2019 tests collected | ✅ exact (0.58s) |
| 31 `QUOTA GATE` lines in manager `cron/jobs.json` | ✅ exact — but splits 17/7/7 across three scripts (F1) |
| Gate scripts fail-open (`.sh` :59–60; `.py` `no_data_optimistic`) | ✅ exact — and worse for `quota_gate.py` (F1) |
| IMPL-SPEC 529 lines; Safety Properties §2–3 "Fails open" | ✅ exact (§254–258) |
| `price-first-api-routing` skill not found | ✅ confirmed (0 hits under `~/.hermes`) |
| `daily_spend` live (routstrd ≈ $18.8 today) | ✅ confirmed in `~/.hermes/bot/zai_usage.db` |
| Design doc "Not yet implemented" header stale | ✅ confirmed (line 5) |
| Kalman convergence red today | ⚠️ not re-probed by this review (plan ran it same-day; plausible) |

---

## 4. Required amendments before Felix sign-off (consolidated)

1. **F1:** Rescope CG-4/CG-5 for the three-entry-point reality; audit by script path;
   add blocking Q9.
2. **F2:** CG-4 adds freeze-marker check to canonical CLI (all entry points); go-live
   drill covers every path.
3. **F3:** §6: shadow instruments the deployed CLI end-to-end incl. fallback logging.
4. **F4:** §3 infra-down row marked PROVISIONAL; add blocking Q10.
5. **F5:** CG-4 names the effective-price source and its failure semantics.
6. **F6–F9:** Minor text fixes per findings.

---

## 5. One-line recommendation to Felix

Approve the CG-1..CG-3 architecture now, but hold CG-4/CG-5 until the plan absorbs
amendments F1–F5 and you answer Q1–Q3 **plus** the two new blocking questions (Q9
quota_gate.py fleet split, Q10 infra-down fail-open allowance) — the plan's facts are
solid, but as written it would declare go-live with 7 of 31 crons still on a fail-open
gate that trusts a dead key.
