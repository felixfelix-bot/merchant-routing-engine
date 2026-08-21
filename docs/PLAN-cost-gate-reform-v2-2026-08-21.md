# PLAN v2: Quota Gate → Cost Gate Reform (CG-1..CG-9)

**Author:** merchant-routing CW consultant (GLM-5.2, manager-profile session)
**Date:** 2026-08-21 (same day, post-Felix-answers)
**Supersedes:** `docs/PLAN-cost-gate-reform-2026-08-21.md` (v1, commit a772474)
**Absorbs:** `docs/REVIEW-cost-gate-plan-kimi-2026-08-21.md` (commit 03e4714, findings F1–F10)
**Status:** FINAL PRE-IMPLEMENTATION PLAN — Felix answered all 10 blocking questions
2026-08-21; PLAN GATE lifted for scheduling. No production implementation in this commit.

---

## Changelog

**v2.1 — subscription-amortized pricing (2026-08-21, later same day).**
Felix correction: *"zai isn't free. I paid 80 USD for a 1 month subscription.
Make sure prices are calculated correctly using the monthly subscription and
the quota pressure."* v2's implicit $0 zai baseline is wrong; this revision
fixes it throughout:

- **§0.5 (NEW)** — subscription inventory + entitlement-amortized baselines:
  zai friend **$0.0043/M** ($80/mo ÷ ≈18.45B token entitlement), zai ours
  **$0.0084/M** ($155/mo, entitlement provisional), ollama_cloud already
  amortized by tracker ($0.0155/M measured, $100/mo Max plan); all other
  providers pay-per-use. Gate prices at ENTITLEMENT; realized amortized cost
  (friend $0.081/M Aug-to-date) is reported, not gated.
- **§2.1/§2.2** — gate metric is now `effective_zai_price(t) = baseline ×
  pressure(u_5h × u_week × u_month) × peak_mult`, priced at Kalman-FORECAST
  end-state pressure over the task's expected duration.
- **CG-2** — `/v1/pricing` exposes baseline (entitlement + realized),
  per-window usage fractions, pressure multiplier, effective price now and
  forecast (+5/+15/+60 min, `?horizon_min=`); fixes `providers.yaml`
  `friend.monthly_fee_usd: 0 → 80` (the free-tier artifact).
- **CG-6** — paid failover candidates compared against zai OPPORTUNITY price
  (was $0 → never compared). Crossovers: zai friend > ollama at u≈0.89
  single-window (u≈0.71 during peak); zai > routstrd $1/M only at u≈0.998 —
  paid per-token tiers are effectively never price-eligible while any
  subscription route is finite.

---

## 0. Decision Ledger (Felix, 2026-08-21 — all incorporated)

| Q | Decision | Design consequence |
|---|---|---|
| Q1 | NO fixed primary cap. **Percentile gating**: run deferrable crons only when current effective price is in the **lower 20%** of its rolling average. Budget cap retained as **hard backstop ceiling** (secondary; consultant proposes value) | §2 percentile gate is the primary gate; §2.5 budget backstop |
| Q2 | **DEFER, never auto-downgrade.** Model choice happens at schedule time (manager/consultant job). Under price pressure, deferrable work is simply not dispatched (cheap models risk quality-gate failure → token burn) | No downgrade cascade in gate; DEFER = skip-and-reschedule for crons |
| Q3 | **Deliberate layering CONFIRMED.** Dispatch gate = consumer behavior (buy more when live-router price is low). Per-request caps stay dead — "too granular, difficult to price individual requests, easy to slow dispatches as live router prices go up" | Proxy `_check_spend_cap` stays deactivated; no per-request logic anywhere in CG tasks |
| Q4 | Use the live router's **own exposed cost** (remaining quota + subscription amortization) in decisions. Verify what the proxy exposes; spec what's missing | §4 verification results; CG-2 builds the missing `/v1/pricing` exposure |
| Q5 | **APPROVED** `task_type` logging in proxy | CG-5, strict quality gates |
| Q6 | Override mechanism to be proposed (who/how/audit) | §3 proposal; CG-4 |
| Q7 | Peak pricing only via live router — no separate deferral logic. Verify pressure/peak already affects exposed prices | §4: peak ✅ done by construction; pressure ❌ not exposed → folded into CG-2 |
| Q8 | RESOLVED — skill lives at `~/.hermes/profiles/manager/skills/mlops/price-first-api-routing/` (verified: SKILL.md + 25 references). v1's reference was a wrong path; **no recreation** | Reference fixed throughout; root-skill list unchanged |
| Q9 | **Consolidate ALL 31 crons** onto the new gate, embracing live router + dynamic prices | CG-8 sweeps every entry point (fixes review F1's 7-cron split) |
| Q10 | **Strict fail-closed on infra-down**: block + loud log. Escape hatch via Q6 override | v1 §3's "degrade to legacy verdict" row DELETED (review F4 resolved the other way) |
| Q11 | **zai is a PAID subscription** — "I paid 80 USD for a 1 month subscription" (friend key); prices must use the monthly fee × quota pressure | §0.5 baselines; §2.1 gate metric; CG-2 exposure; CG-6 opportunity-cost comparator |

---

## 0.5 Subscription Inventory & Amortized Baselines (v2.1 — verified 2026-08-21)

**Inventory** (checked against `config/providers.yaml`, live `GET /quota`, and
`zai_usage.db`):

| Provider | Plan | Fee | Quota entitlement | Baseline $/M (gate) |
|---|---|---|---|---|
| zai **friend** | GLM coding plan | **$80/mo** (Felix, 2026-08-21) | ≈**18.45B tokens/mo** (estimated, below) | **$0.0043** |
| zai **ours** | GLM coding plan | $155/mo (`providers.yaml`) | unmeasured; 18.45B assumed until CG-2 logs capacity | $0.0084 (provisional) |
| ollama_cloud | Max plan | $100/mo | 500M/5h + 3.5B/wk included; $0.15/M above (tracker-sourced, RP-4) | already subscription-amortized by tracker — measured **$0.0155/M**, unchanged |
| routstrd / telnyx / ppq / openrouter / deepinfra | — | none (ecash/credit wallets) | pay-per-use | catalog/measured — unchanged |

**Entitlement estimate (friend):** the z.ai quota API exposes only `used_pct`,
never absolute tokens. `kalman_samples` recorded the friend **monthly window
at 5.0% used** on 2026-08-18 12:18, by which point `api_calls` shows 922.7M
tokens served on friend → entitlement ≈ 922.7M ÷ 0.05 ≈ **18.45B tokens/mo**.
Single-observation estimate: CG-2 logs `estimated_capacity_tokens`
(burn_predictor's capacity estimator: `burn_rate × elapsed ÷ used_pct`)
hourly and Kalman-smooths it. Until confidence is established the denominator
falls back to trailing-30d usage — conservative (pricier zai).

**Entitlement vs realized — the gate uses ENTITLEMENT; realized is reported.**

- Realized Aug-to-date: friend $80 ÷ 983.9M = **$0.081/M** (≈19× entitlement);
  ours $155 ÷ 209.0M = **$0.74/M**. We are on pace for <10% of the friend
  plan's monthly entitlement — the binding constraint is the **5h window**
  (observed at 100% on 08-18), not monthly volume.
- Why entitlement gates: (1) **stability** — a usage-amortized baseline starts
  at ∞ on day 1 and decays all month, inverting incentives (defer early,
  binge late) and smearing the p20 band with baseline drift; (2) **no
  double-counting** — pressure(u_month) already rises with monthly usage while
  a usage-based baseline falls; the two signals fight; (3) **architecture** —
  realized pricing ($0.081/M) would rank friend ~5× ABOVE ollama_cloud,
  inverting the fleet contra AGENTS.md ("z.ai flat rate is always the
  primary") and Felix's squeeze-cheap-keys asymptote decision; (4)
  **economics** — dispatch prices marginal opportunity cost: an unused
  entitled token costs its entitlement rate, a depleting window costs more
  (pressure), an exhausted window costs ∞.
- Realized $/M + entitlement utilization % ship in `/v1/pricing` and a monthly
  finance note (subscription-value audit — at ~8% utilization the friend plan
  is over-sized or under-used; Felix's call, informed by data). Key renewals
  ($80/$155/$100) are finance line items, not gate inputs; the budget backstop
  (§2.5) still scopes per-token paid tiers only.

**Effective price model (per zai key; this is exactly the composition
`src/pricing_engine.compute_effective_price` already performs — v2.1 changes
only the base rate fed in):**

```
effective_zai_price(t) = baseline_$per_M (entitlement, this section)
                       × P(u_5h, u_week, u_month)      # RP-EXP superposition
                       × peak_mult(t)                  # 3.0, UTC 06–09, zai only

P = Π_windows 1 + 0.5·t/(1−t),  t = (u − 0.60)/0.40    # onset 0.60, K=0.5
    → 1.5× at u=0.80, 2.5× at 0.90, +∞ at u ≥ 1.0 (hard limit)
```

**Worked table (friend baseline $0.0043/M):**

| u_5h | u_week | u_month | pressure | $/M | peak $/M | vs ollama $0.0155 | vs routstrd $1.00 |
|---|---|---|---|---|---|---|---|
| 0.60 | 0.60 | 0.05 | ×1.0 | $0.0043 | $0.013 | cheaper | cheaper |
| 0.80 | 0.60 | 0.05 | ×1.5 | $0.0065 | $0.020 | cheaper / pricier at peak | cheaper |
| 0.90 | 0.70 | 0.10 | ×2.9 | $0.013 | $0.038 | ~parity / pricier | cheaper |
| 0.95 | 0.80 | 0.10 | ×6.7 | $0.029 | $0.087 | **pricier** | cheaper |
| 0.98 | 0.90 | 0.20 | ×26 | $0.113 | $0.34 | pricier | cheaper |
| 0.99 | 0.95 | 0.30 | ×92 | $0.40 | $1.19 | pricier | cheaper / pricier at peak |
| 0.995 | 0.98 | 0.50 | ×425 | $1.83 | $5.5 | pricier | **pricier** |

**Crossover points (single window unless noted):**

| Comparison | friend ($0.0043/M) | ours ($0.0084/M) |
|---|---|---|
| > ollama_cloud $0.0155/M | **u ≈ 0.89** | u ≈ 0.78 |
| > ollama during peak (×3) | **u ≈ 0.71** | any u (base×3 = $0.025 > $0.0155) |
| > routstrd $1.00/M | **u ≈ 0.998** | u ≈ 0.997 |
| > routstrd, 5h×weekly superposed | u_5h ≈ 0.995 ∧ u_week ≈ 0.97 | u_5h ≈ 0.99 ∧ u_week ≈ 0.97 |

Reading: with real baselines zai stays cheapest until deep depletion — the
pressure curves (not the baseline) do the diverting, per Felix's "squeeze
cheap keys as long as possible" asymptote decision. routstrd beats friend only
in the last ~0.2% of a window, i.e. effectively only when a zai window is
hard-exhausted (+∞) — and then it must still beat ollama ($0.0155–$0.15/M),
which it never does.

---

## 1. Routstrd Anomaly (2026-08-21): Root Cause + Guard

**Incident:** 156 glm-5.2 calls / 18.8M tokens / **$18.81** hit `routstrd` (paid ecash,
~$1/M catalog) while z.ai served 7,155 glm-5.2 calls at recorded $0.00 (true
amortized cost ≈ $0.004–0.08/M — §0.5) and ollama_cloud
took 1,269 calls at $1.19. Historical routstrd spend: $0.15–4/day.

### 1.1 Root cause (verified against `zai_usage.db` today)

Mechanism: **simultaneous degradation of all three cheap tiers + a request burst +
no price ceiling on paid failover for subscription-covered models.**

Evidence chain:

1. **`friend` key DEAD since 2026-08-20 ~19:44** — `key_health`: failure_count=37,
   `last_error_type='dead'`, 3600 s backoff. (Not "locked" — hard-dead.)
2. **`ours` key intermittent 5h-window exhaustion** all day — anomaly_events
   `ours exhausted failure #27–#37` 23:52→00:06 and recurrences; 2 s backoffs
   (recovers, but each spell opens the failover window).
3. **`ollama_cloud` quota-exhausted 42× today** (`key_backoff/exhausted`, 60 s
   backoffs) — the usual cheap external floor was repeatedly unavailable.
4. **At 16:16 a 154-request glm-5.2 burst** (all `session_id=None` — a headless
   non-Hermes client, attribution impossible pre-CG-5) hit while ours was
   exhausted, friend dead, and ollama_cloud in backoff → every call fell through
   to `_try_external_failover` (`zai_proxy.py:3641`).
5. **Candidate filter left exactly one funded provider**: telnyx skipped (kimi-only
   guard), ppq skipped (D6 policy + $0.00 balance), openrouter unfunded (−$0.18),
   deepinfra unfunded today → **routstrd** (ecash wallet topped up ≥5,000 sats by
   the funding-guard cron, catalog ~$1.0/M). 156 ×
   `key_decisions.reason='zai_exhausted_routstrd_failover'` — exact match to the
   156 routstrd rows in `api_calls`.
6. `.enable_live_routing` was created 13:42 today (LiveRouter now live) but is
   **correlated, not causal**: the spill decisions carry the hardcoded-chain
   reason, not `live_kalman_failover_*`.

Why it burned money: the chain sorts candidates *by price among providers that
are available right now* — it never compares a paid route against the
**opportunity cost of the subscription route** (glm-5.2 marginal cash $0 but
opportunity price ≈ $0.0043/M × pressure, §0.5, back in ~2 s) or against the
cheapest flat-rate alternative (ollama_cloud $0.0155/M ≈
65× cheaper). 18.6M tokens on ollama_cloud would have cost ~$0.29; waiting out
the 2 s ours backoff, $0. The correct behavior was 503/defer-at-source.

### 1.2 Guard (CG-6 — prevents this CLASS, not just this instance)

1. **Opportunity-price comparator (v2.1)** in `_try_external_failover`: for any
   model with an active subscription route (glm-5.2/glm-5.3 on z.ai keys,
   glm-5.2 on ollama_cloud), a paid external candidate is price-eligible only
   if its rate beats the subscription routes' CURRENT effective prices (§0.5).
   The crossovers are extreme (routstrd $1/M beats friend only at u ≈ 0.998
   single-window, or u_5h ≈ 0.995 ∧ u_week ≈ 0.97 superposed — and it never
   beats ollama at $0.0155–$0.15/M), so paid per-token tiers are effectively
   never price-eligible while any subscription route is finite; they exist for
   ollama-exclusive models and total fleet outage. The static **$0.10/M
   ceiling** (≈23× friend baseline, ≈6× ollama flat) stays as a
   belt-and-braces backstop: today it alone zeroes the $18.81 — glm-5.2 would
   503 → callers defer/retry instead of silently paying 65×.
2. **Paid-tier velocity anomaly + hard daily cap** (the Q1 backstop): any
   paid-tier hour > $5 or day > cap → `anomaly_events` row (loud, alerted) and
   paid tiers fail-closed out of the candidate list for the rest of the window.
3. **Attribution** (CG-5): `session_id=None` bursts are currently unattributable;
   `task_type` + source logging makes the next burst forensically traceable.

---

## 2. Percentile Cost Gate — Primary Design (Q1)

> Felix's words: "run the cron jobs when we are in the lower 20% of our average
> cost." Interpretation (per Q1 decision): the gate is **anomaly/percentile
> gating on effective price**, not a fixed budget.

### 2.1 Metric

`effective_price_usd_per_M(model)` = the **cheapest ELIGIBLE provider's**
pressure- and peak-adjusted price for the model the job is scheduled to use:

- z.ai keys: **entitlement-amortized baseline (§0.5: friend $0.0043/M, ours
  $0.0084/M provisional) × pressure(u) superposition** (5h×weekly×monthly,
  RP-EXP, `src/pricing_engine.py`) × peak multiplier — i.e.
  `effective_zai_price(t)` per §0.5; zai is NOT free (Q11)
- flat/paid tiers (ollama_cloud, routstrd, telnyx…): measured/catalog rate
  (the same `_get_provider_cost()` values the failover chain already uses)
  × peak multiplier
- **subscription amortization (Q4, v2.1):** the baseline is the
  entitlement-amortized subscription rate (§0.5) and pressure(u) supplies the
  depletion dynamics — as quota depletes, effective price rises from
  $0.0043/M past ollama ($0.0155/M at u ≈ 0.89; u ≈ 0.71 at peak) toward
  paid tiers, moving the current price OUT of the lower-20% band exactly when
  deferral is right. Realized amortized $/M and entitlement utilization are
  exposed alongside for audit — never gated on.
- **forecast pricing (v2.1):** the gate prices a task at the PREDICTED
  end-state pressure, not just current: u_end per window from burn_predictor's
  Kalman projection (`projected_total_pct`, `exhausts_in_hours`) evaluated
  over the task's expected duration (CG-3 p90 duration; default horizon
  30 min). A 20-min job launched at u_5h = 0.93 projected to end at 0.97 is
  priced at the 0.97 curve point. Both variants come from CG-2; the gate
  consumes the forecast.

**Per-model, not blended.** Manager crons gate on glm-5.2's own distribution.
Blended fleet price is reported in the verdict JSON (visibility) but never gates.
Models with <N observations fall back to their family's blended distribution.

### 2.2 Rolling window + threshold

- Window: **trailing 7 days** of **hourly median** observations per model
  (hourly median de-weights burst noise; ≈168 samples).
- Source: `price_observations` table (right shape: provider, model, rate_per_m,
  is_measured, ts — but **stale since 2026-08-17**; CG-2 resumes the collector).
- Rule: deferrable cron **ALLOW iff current effective price ≤ p20(window)**
  (the price sits in the cheapest 20% of the trailing week).
- v2.1: zai's contribution to the per-model distribution is now dynamic
  (baseline × pressure × peak): mostly cheap, spiking near 5h-window
  exhaustion and during UTC 06–09. The p20 band therefore tracks
  "subscription-abundant, off-peak" hours — precisely the window Felix wants
  crons to run in — and under pressure zai itself exits the band, which is
  when DEFER (or ollama reroute) is the right call.

### 2.3 Hysteresis (anti-flapping)

- Enter CHEAP: `price ≤ p20`
- Exit CHEAP: `price > p20 × 1.20` (20% exit band) — once cheap, stays cheap
  until price clearly leaves the band
- Min dwell 30 min between state flips
- **Job-burst stickiness:** the verdict is snapshotted at dispatch time; an
  ALLOW covers the job's entire run (no mid-job revoke — crons are not
  interruptible mid-flight anyway)

### 2.4 Cold start / no history

- **< 48 hourly samples**: deferrable crons get DEFER
  (`reason: price_history_insufficient`) — fail-closed posture per Q10 — while
  the legacy quota-gate verdict still applies as the floor. Interactive/urgent
  work is never deferred on price-history grounds (Q2: not dispatched ≠
  downgraded; urgent work uses the budget backstop only).
- Cutover requires ≥5 days of fresh hourly data (shadow window doubles as
  warm-up; the 7,346 existing rows only reach 08-17).

### 2.5 Budget backstop (secondary, hard ceiling — consultant-proposed values)

- Scope: **all paid tiers** (routstrd, telnyx, openrouter, ppq, deepinfra,
  ollama_cloud above-quota) — a cap excluding them is decorative (v1 A4).
- Proposed: **$15/day fleet-wide**, WARN at 50%, **hard DENY of paid-tier
  dispatch at 100%**. Subscription routes (z.ai keys) are never blocked by the
  backstop. Owner: Felix; changed via `config/budget.yaml` + Q6 override.
- This backstop is exactly the CG-6 velocity cap — one implementation, two
  call sites (gate CLI + proxy failover filter).

### 2.6 Decision semantics (Q2)

`ALLOW` (cheap band or non-deferrable under backstop) / `DEFER` (deferrable
work outside the band — cron skips and reschedules; **no model downgrade, no
auto-substitution**) / `DENY` (backstop hit, freeze marker, locked key, missing
price data). Model choice remains a schedule-time human/manager decision.

---

## 3. Override Mechanism — Proposal (Q6)

- **Who:** Felix, the merchant-routing CW, and the orchestrator CW (manager
  profile). Workers: no.
- **How:** marker file `~/.hermes/bot/.cost_gate_override` (JSON:
  `{scope: "budget|price_history|infra_down|paid_ceiling", expires_ts, issued_by,
  reason}`) + CLI `zai-cost-gate.py --override <scope> --ttl 4h --reason "…"` —
  no raw file editing; TTL mandatory (default 4 h, max 24 h), single scope per
  grant so an override never globally disables the gate.
- **Audit:** every gate invocation that consumed an override logs a
  `cost_gate_overrides` table row (issued_by, scope, ttl, consumed_at, task) +
  an `anomaly_events` INFO entry. Overrides never bypass the freeze marker or
  the locked/dead-key hard block (those are hardware-incident backstops).
- Also serves as the **Q10 escape hatch** for infra-down strict blocks.

---

## 4. Q4/Q7 Verification — What the Proxy Exposes Today

Checked `zai_proxy.py` (production, 4,938 lines) on 2026-08-21:

| Endpoint / mechanism | Exposes | Verdict |
|---|---|---|
| `GET /v1/models` (:4830) | `sats_pricing` fields | **STUB** — near-zero values so the Routstr SDK accepts our models; explicitly NOT a cost signal |
| `GET /v1/dispatch_gate` (:4617) | `effective_price_per_m` for best of {ours, friend, ollama_cloud} | Real, **includes peak** (`_is_peak_hour()` → ×3.0); **pressure NOT included** — `_KEY_COST_MULTIPLIER` (:440) is a STATIC dict {ours 1.0, friend 1.21, ollama 1.0}; no external paid-tier prices |
| `GET /quota` (:4499) | quota_cache: remaining/used_pct per key + ollama_cloud/openrouter/telnyx balance snapshots | Good for quota state; no prices |
| `_get_provider_cost()` (:952–969) | live per-provider rates (routstrd catalog 10-min cache, PPQ/OpenRouter tables) | **Internal only** — used for failover sort, exposed nowhere |
| LiveRouter (`_consult_live_router`) | Kalman-priced picks; `routing_live_decisions` (12,405 rows: live/shadow cost) | Logged, not queryable |
| `price_observations` table | provider, model, rate_per_m, is_measured | Right shape, **stale since 2026-08-17** |

**Q4 verdict:** the raw ingredients exist (peak, quota, live catalog rates,
subscription amortization via pressure curves in `src/pricing_engine.py`) but
**no endpoint exposes a decision-grade effective price**. Missing (→ CG-2):
(1) `GET /v1/pricing?model=…` returning per-provider effective price = base ×
pressure(u) × peak_mult + remaining-quota/amortization fields for every funded
provider; (2) resumed `price_observations` collector at hourly cadence;
(3) exposure of the already-computed routstrd/PPQ/OpenRouter catalog rates.
**v2.1 adds:** the only zai "rate" observable today is the **$0.001/M floor**
(`price_observations`: provider `friend`, source `zai_amortized`, rate 0.001 —
because `providers.yaml` sets `friend.monthly_fee_usd: 0`). That is the
free-tier artifact this revision deletes (§0.5, CG-2).

**Q7 verdict:** peak is **done by construction** — peak_mult 3.0 is already in
`/v1/dispatch_gate`'s exposed price and `peak_hour_ollama_primary` behavior
fired 1,201 times today. Pressure curves are NOT yet in exposed prices (static
multiplier instead) — closing that is part of CG-2's `/v1/pricing`; **no
separate peak/deferral logic task is created** (per Felix: pricing only, no
separate deferral).

---

## 5. Target Design (supersedes v1 §3)

```
evaluate_cost_gate(
    model, task_type, deferrable,                  # task identity
    effective_price_usd_per_m,                     # FORECAST variant — GET /v1/pricing?horizon_min= (CG-2, v2.1)
    price_history,                                 # price_observations window (CG-2)
    rolling_paid_spend_usd, budget_cap_usd,        # daily_spend reader (CG-6/CG-7)
    override,                                      # from .cost_gate_override (CG-4)
) -> {decision: ALLOW | DEFER | DENY,
      percentile_rank, threshold_p20, headroom_usd,
      provenance: {price_source, history_n, window_days},
      reason_code, reason_json}
```

Fail-closed matrix (Q10 posture — v1's "degrade to legacy verdict on infra-down"
row is DELETED):

| Condition | Decision |
|---|---|
| Freeze marker present | DENY (hard; overrides never apply) |
| Dead/locked z.ai key on the z.ai path | DENY (hard; overrides never apply) |
| Price endpoint unreachable / stale >15 min | **DENY + loud log** (`infra_down`); escape = Q6 override (Q10) |
| Effective price unknown | DENY, `price_unknown` |
| <48 h price history (deferrable task) | DEFER, `price_history_insufficient` |
| Budget config missing/unparsable | DENY, `budget_unconfigured` |
| Paid-tier backstop exceeded | DENY for paid tiers only (subscription routes unaffected) |
| Valid scoped override active | Override only its scope; everything else still enforced |

---

## 6. Implementation Schedule — CG-1..CG-9

Every task carries this paste-ready gate text (quality-gates v3.1.0):

```text
GATE (quality-gates v3.1.0 — required before task close):
1. TDD: red→green — write failing tests first; tests+impl committed atomically.
2. TESTS-PASS: python3 -m pytest tests/ -v — full suite green (2019+ tests) before push.
3. CROSS-FAMILY REVIEW: cold review via worker-reviewer-kimi (kimi↔glm; reviewer
   had zero implementation involvement) before merge.
4. DOCS-SAME-COMMIT: relevant docs/*.md updated in the same atomic commit as code.
5. ATOMIC COMMITS: one logical change per commit, conventional message
   (feat|fix|docs|test(scope): ...).
6. PUSH: push to github remote (branch wt/glm53-quota-cleanup-t_da1b7c10); if
   hooks block: `git push --no-verify` (--no-verify goes AFTER the git
   subcommand, never before).
```

### CG-1 — Percentile cost-gate module (pure, tested)
- **Scope:** `src/cost_gate.py`: `evaluate_cost_gate()` per §5; p20 threshold,
  hysteresis state machine, cold-start rule, backstop check, override
  consumption, reason codes with provenance. Composes `src/dispatch_gate.py`
  (TASK_PROFILES, margins) — no duplication. `config/budget.yaml` defaults
  ($15/day, tiers list).
- **Files:** `src/cost_gate.py`, `config/budget.yaml`, `tests/test_cost_gate.py`.
- **Tests:** every §5 matrix row; band-entry/exit with 20% hysteresis; dwell
  timing; <48-sample DEFER; backstop denial frees subscription routes; override
  scope isolation; freeze-marker/dead-key override immunity.
- **Deps:** none. **Blocks:** CG-2/3/4/7. **Effort:** 1.5 d.

### CG-2 — Price exposure: collector + `GET /v1/pricing` (Q4/Q7 gap)
- **Scope:** resume `price_observations` collector (hourly, all funded
  providers, measured flags); add proxy `GET /v1/pricing?model=` returning
  per-provider effective price = base × pressure(u) superposition × peak_mult +
  remaining-quota/amortization fields; expose `_get_provider_cost()` catalog
  rates (routstrd cache) in the same payload. Repo module
  `src/pricing_exposure.py` + proxy wiring (production edit per AGENTS.md
  revert-plan rule).
- **Files:** `src/pricing_exposure.py`, `scripts/collect_price_observations.py`,
  `tests/test_pricing_exposure.py`, `~/.hermes/bot/zai_proxy.py` (endpoint).
- **v2.1 payload spec (zai rows):** `baseline_entitlement_usd_per_m` (§0.5),
  `baseline_realized_usd_per_m` (trailing 30d), `entitlement_utilization_pct`,
  `windows: {u_5h, u_week, u_month, estimated_capacity_tokens, confidence}`,
  `pressure_mult`, `peak: {active, mult}`, `effective_price_usd_per_m` (now),
  and `forecast`: effective price at +5/+15/+60 min plus `?horizon_min=` for
  task-duration pricing — computed from burn_predictor `projected_total_pct`
  per window. Forecast variant is gated on `kalman-convergence-check` green
  (same condition as CG-3); until then forecast = current price + staleness
  flag. Config fix in the same task: `providers.yaml`
  `zai.keys.friend.monthly_fee_usd: 0 → 80`, and `_measure_zai_amortized` /
  realtime_pricing switches to the entitlement denominator
  `max(smoothed capacity estimate, trailing-30d usage)` (§0.5 fallback rule);
  log `estimated_capacity_tokens` hourly until the ours-key entitlement is
  measured with confidence.
- **Tests:** pressure applied on z.ai rows and NOT on flat tiers; peak flag;
  staleness marker >15 min; fee=0 never again yields the $0.001 floor;
  forecast variant matches closed-form pressure at projected u; fixture
  price history for CG-1 integration.
- **Deps:** CG-1 (input shape). **Effort:** 1.5 d.

### CG-3 — Token predictor (v1 CG-2, unchanged core)
- **Scope:** `src/token_predictor.py`: per-model p50/p90 from `api_calls`
  (status_code=200, recent window); task dimension via TASK_PROFILES until
  task_type data matures (CG-5); confidence by sample count; NO Kalman inputs
  until `kalman-convergence-check --short` is green (still red 2026-08-21).
- **Files:** `src/token_predictor.py`, `scripts/seed_token_stats.py`,
  `tests/test_token_predictor.py`.
- **Tests:** percentile correctness on synthetic sqlite; cold-model conservative
  default + flag; drift re-seed (seed-then-replace).
- **Deps:** CG-1. **Effort:** 1 d.

### CG-4 — Override mechanism (Q6, §3)
- **Scope:** `zai-cost-gate.py --override` CLI (TTL, scope, issued_by);
  `.cost_gate_override` marker handling; `cost_gate_overrides` audit table;
  `anomaly_events` INFO row on issue + on consumption. Allowed principals:
  Felix, merchant-routing CW, orchestrator CW.
- **Files:** `src/override_store.py`, `tests/test_override_store.py`.
- **Tests:** TTL expiry; single-scope grants; audit rows; freeze/dead-key
  immunity; corrupt-marker fail-closed.
- **Deps:** CG-1. **Effort:** 0.5 d.

### CG-5 — `task_type` logging in proxy (Q5, approved)
- **Scope:** `ALTER TABLE api_calls ADD COLUMN task_type` (nullable, backfill
  NONE — history stays as-is); accept `X-Task-Type` header and body
  `task_type` field (header wins); log on every call incl. external failovers;
  unknown/unset → NULL (never guessed); document the field for all CW callers.
- **Files:** proxy schema + request logging path; `docs/task-type-logging.md`;
  `tests/test_task_type_logging.py` (repo-side contract tests).
- **Tests:** header/body/column round-trip; NULL when absent; survives
  failover hop; no behavior change when field absent.
- **Deps:** none (parallel-friendly). **Effort:** 0.5–1 d.

### CG-6 — Routstrd-class guard (§1.2)
- **Scope:** in `_try_external_failover`: **opportunity-price comparator
  (v2.1)** — paid candidates must beat the subscription routes' live
  effective prices (§0.5 crossovers: routstrd $1/M eligible only at
  u ≈ 0.998+, i.e. effectively only when zai windows are hard-exhausted AND
  ollama is pricier/unavailable) — plus the static $0.10/M ceiling as
  backstop;
  paid-tier velocity anomaly (>$5/h → loud anomaly_events) + hard daily paid
  cap (shared `config/budget.yaml`) fail-closed for the rest of the window.
  Ceiling bypass = CG-4 override scope `paid_ceiling` only.
- **Files:** `src/paid_tier_guard.py`, `tests/test_paid_tier_guard.py`, proxy
  wiring at the failover candidate filter.
- **Tests:** replay of today's exact state (ours exhausted, friend dead,
  ollama backoff, routstrd $1.0/M funded) → routstrd EXCLUDED, 503 path taken;
  ollama_cloud at $0.0155/M still eligible; override unlocks with audit row.
  v2.1: comparator excludes routstrd whenever effective zai price < $1/M
  (replay at u_5h=0.95 → zai ≈ $0.029/M → EXCLUDED); admits it only at the
  §0.5 crossover state (u_5h ≥ 0.998) with an audit row.
- **Deps:** CG-2 (price data; static ceiling can land first). **Effort:** 1 d.

### CG-7 — Canonical gate CLI (deploy)
- **Scope:** `scripts/zai-cost-gate.py`: step 0 freeze-marker check (uniform
  across ALL entry points — fixes review F2), dead/locked-key block, percentile
  gate via `GET /v1/pricing`, override check, Q10 strict infra-down DENY +
  loud log, `--legacy` flag for rollback. Deploy to
  `~/.hermes/profiles/manager/scripts/zai-quota-gate.py` (same filename); convert
  `quota_gate.py` and `zai-quota-gate.sh` into delegating shims (review F1) so
  all 31 crons physically reach the new gate. Keep legacy script as
  `zai-quota-gate.py.legacy-20260821` (revert plan per AGENTS.md).
- **Files:** `scripts/zai-cost-gate.py`, shim targets, `docs/` revert note,
  `tests/test_cost_gate_cli.py` (exit-code contract).
- **Tests:** exit codes per matrix row; each of the three legacy entry points
  now hits freeze-marker step 0; `--legacy` parity.
- **Deps:** CG-1..CG-4 (+CG-2 endpoint live). **Effort:** 1 d + deploy window.

### CG-8 — Cron consolidation: all 31 (Q9)
- **Scope:** new COST GATE line spec (dispatch: run `zai-cost-gate.py`, DEFER on
  hold — Q2 semantics); audit script matching **script paths**, not phrasings
  (review F1); sweep all 31 `QUOTA GATE` lines (17 `.sh`, 7 `.py`, 7
  `quota_gate.py`) in manager `cron/jobs.json` — **executed by the orchestrator
  CW** in the manager profile (cross-profile guard; A5). Verify: 0 stale lines
  AND 0 non-canonical script paths; 3 end-to-end cron spot checks.
- **Files:** `scripts/cron_gate_line_audit.py`; manager `jobs.json`
  (orchestrator-side).
- **Deps:** CG-7 deployed. **Effort:** 0.5 d + coordination.

### CG-9 — Validation suite + shadow campaign (go-live gate)
- **Scope:** (a) continuous suite: bidirectional fail-closed, Telnyx both-ways
  (quota bypass ≠ cost bypass), freeze drill **per entry-point path** (F2),
  no-allow-without-positive-cost-verdict audit (F2/A2); (b) shadow ≥5 days
  instrumenting the **deployed CLI end-to-end** (real /v1/pricing fetch,
  fallback layers logged — review F3), doubling as percentile-history warm-up
  (§2.4). Exit criteria: v1 §6 items 1–6 with percentile stats added
  (p20 band hit-rate, DEFER volume, false-DEFER audit).
- **Files:** `tests/test_cost_gate_validation.py`, shadow harness reusing
  `shadow_hook`/`shadow_logger`.
- **Deps:** CG-7 (CG-8 after shadow exit). **Effort:** 1 d + ≥5 d wall-clock.

### Dependency graph & totals

```
CG-1 (1.5d) ──┬─ CG-2 (1.5d) ──┐
              ├─ CG-3 (1d) ────┤
              ├─ CG-4 (0.5d) ──┼─ CG-7 (1d) ── shadow ≥5d ── CG-8 (0.5d) ── go-live
CG-5 (0.5-1d) ┘                │
CG-6 (1d, after CG-2) ─────────┘        CG-9 suite ships with every task
```

**Total: 8–9.5 implementation-days** (+ ≥5-day shadow window; +0.5–1 d if
friend-key resurrection work is pulled in — out of CG scope). Minimum two PRs
(modules CG-1..4+6; deploy CG-5/7+8+9), each with cross-family review.

---

## 7. Rollout

Shadow-first (v1 §6 retained): after CG-7 deploys, the gate computes + logs
while the legacy quota verdict enforces; cutover only on CG-9 exit criteria;
rollback = `--legacy` flip (cron lines unchanged post-CG-8). The Q1 backstop
(CG-6) is the one piece that enforces from day one — it is fail-closed against
exactly today's $18.81 class, and its blast radius (paid tiers only) cannot
silence subscription-routed crons.
