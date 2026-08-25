# PLAN — ox-alpha Promo Exploitation via Routing Proxy (v1)

**Date:** 2026-08-21 · **Author:** manager CW (subagent) · **Status:** PLAN ONLY — zero production implementation. Nothing is scheduled until Felix approves §7 decision points.
**Branch:** `wt/glm53-quota-cleanup-t_da1b7c10` · **Composes with:** PLAN-cost-gate-reform-v2-2026-08-21 (CG-1..CG-9, in flight on same branch, isolated kanban workspaces)

---

## §0 VERIFIED FACTS (live checks run 2026-08-21, all UTC)

### 0.1 Model on OpenRouter — VERIFIED (public API, no key needed)

| Fact | Value | Source |
|---|---|---|
| Model exists | `stealth/ox-alpha` ("Ox Alpha") | `GET /api/v1/models` — matched live |
| Listed since | **2026-08-20 20:04:55Z** (created ts 1787256295) | models API `created` |
| Price | **$0.0 / $0.0 per 1M** (prompt/completion); endpoint pricing confirms `discount: 0` | models API + `/models/stealth/ox-alpha/endpoints` |
| Context | 1,048,576 in / 131,072 max completion | models API `top_provider` |
| Modality | **text+image+video → text** (native vision in, text out only) | models API `architecture` |
| Reasoning | **Mandatory**, default effort `max`; efforts low/high/max | models API `reasoning` |
| Params | tools, tool_choice, response_format, temperature, top_p, top_k, reasoning_effort, include_reasoning, max_tokens | models API `supported_parameters` |
| Endpoint | Single upstream "Stealth \| stealth/ox-alpha"; `quantization: unknown`; `is_moderated: false`; uptime 30m/30d = null (too new) | endpoints API |
| Rate limits | **NONE ADVERTISED**: `per_request_limits: null`; **no `x-ratelimit-*` headers returned on the probe call** → limits, if any, are unannounced (community risk (c) confirmed as unverifiable) | models API + probe header dump |

### 0.2 Our key status — VERIFIED

- `OPENROUTER_API_KEY` is **commented out** in `~/.hermes/.env` since 2026-08-20
  (inline note: *"disabled 2026-08-20: balance negative (-$0.18) — uncomment after top-up"*).
  Value preserved in-file (`sk-or-…d92`, len 73). Not present in `~/.hermes/bot/.env`,
  not in the `zai-proxy` systemd unit `Environment=` lines (only feature flags there),
  not in the running proxy process env (PID 4125549 checked). **The key is dormant, not destroyed.**
- `providers.yaml` `external.openrouter` entry still exists with `key_env: OPENROUTER_API_KEY` (config never removed).
- `GET /api/v1/key` (status endpoint, read-only): **HTTP 200** — key authenticates. Label `sk-or-v1-0e7…d92` (matches), lifetime usage **$10.18**, `limit: null` (no spend cap on key), `is_free_tier: false`, `rate_limit` field deprecated/ignorable.

### 0.3 Authorized one-shot probe — VERIFIED (the single sanctioned model call)

```
2026-08-21T13:33:09Z · POST /api/v1/chat/completions
model=stealth/ox-alpha  msg="ping, reply ok"  max_tokens=20  reasoning_effort=low
→ HTTP 200 · total 1.113 s · TTFB 1.084 s
   content="ok" · finish=stop · provider="Stealth" · gen gen-1787319189-EitUlyPlZdBh8vtH4034
   usage: 91 prompt (64 cached) · 3 completion · "cost": 0 · cost_details: all 0
   rate-limit headers: NONE present
```

**Conclusions:** (a) despite the −$0.18 overdraw/purge history, **the key CAN call the $0 model — no 402**; (b) latency is production-plausible (~1.1 s RTT for a trivial completion); (c) OpenRouter explicitly reports `cost: 0` per response — this is our spend-guard signal; (d) prompt-cache already hits (64/91 tokens) — free tier is cache-aware.

### 0.4 Our proxy state — VERIFIED

- `zai-proxy` systemd **user** unit active, PID 4125549, listening 127.0.0.1:9099, `/health` 200.
- `/v1/pricing` → **404** ("only /chat/completions is proxied") — CG-2 has not landed yet; §3 of this plan must compose with CG-2, not assume it.
- `/v1/dispatch_gate` → 200; `/v1/models` → 200 (glm-5.3 et al.; no ox-alpha anywhere).
- `providers.yaml` contains **no oxalpha tier** today — clean slate, purely additive change.

### 0.5 Routstr earnings — NOT READ (bonus fact, per timebox)

No `routstr-node-ops` skill found in `~/.hermes/skills` or manager-profile skills; earnings live on the
VPS2 `routstr-public` node (23.182.128.51:8009) behind SSH with no local read path. Local `routstrd`
daemon at :8008 verified alive (it is the spend-side client, serves catalog incl. gpt-5.6-sol — not an
earnings source). **Last-7d sats earnings: UNREAD — out of scope.**

### 0.6 Community claims (NOT verified — treat as fuzzy input)

- Promo runs ~1 week from launch ~Aug 19–20 → **expiry Aug 26–28 FUZZY** (model listing 08-20 20:04Z is consistent with launch window; no expiry is exposed by any API).
- Also free on OpenCode Go for ~6 days from 08-21 (alternate free source if OpenRouter promo ends first).
- Provenance rumor: stealth operator, suspected GLM-5.5 preview or MIMo V3 Pro — unconfirmed; single "Stealth" endpoint, unknown quantization.
- Political-guardrail refusals observed by community users.
- Zero-Data-Retention claimed by operator — unverifiable → drives §2 data-sensitivity policy.

---

## §1 GOALS

1. **Extract promo value** while it lasts:
   - **Native vision at $0 marginal cash** — our stack's gap: GLM-5.3 is text-only; Kimi vision is paid ($3/$15 per 1M). Use cases: vision-retry skill, screenshot/UI analysis, bulk multimodal summarization.
   - **Bulk deferrable tasks** (non-urgent summarization/classification) that the cost gate can route to the promo tier when approved.
   - **Shadow eval** of a frontier-stealth model against glm-5.3 on our real task shapes — intelligence we could not otherwise buy at $0.
2. **Zero permanent coupling**: additive config only; no schema changes; no overwrites of existing tiers or `model_map` cells (`zai.coding`/`zai.reasoning` untouched).
3. **Revertible in one config block**: removing the tier = delete the `oxalpha:` block + restart proxy (§6).
4. **No production quality risk**: production manager/worker chains never auto-route to ox-alpha before a quality verdict (Felix's economics: failed quality gates on unvetted models cost MORE burn, not less).

**Non-goals:** productizing a promo tier; making ox-alpha a failover candidate for glm chains; committing to post-promo usage at unknown frontier pricing (the routstrd-$18.81 class of mistake).

---

## §2 INTEGRATION DESIGN (additive only — nothing overwritten)

> **Implementation status (2026-08-21, OX-1 / t_ce6edf86):** guard logic shipped as a pure module
> `src/promo_tier.py` (tests: `tests/test_promo_tier.py`; repo-side config fixture: `config/providers.yaml`
> `oxalpha:` block). Wiring note: nothing here is live — OX-2 wires the module into `zai_proxy.py` and the
> live providers.yaml; the module is inert without config (§6). p20 filter helper handed to CG-2:
> `promo_tier.filter_promo_rows()` / `promo_tier.promo_exclusion_sql()`.

### 2.1 New tier in `providers.yaml` (repo config + live proxy config)

```yaml
# ── oxalpha (PROMO tier — stealth/ox-alpha free promo on OpenRouter) ────────
# ADDITIVE entry. Nothing above this block changes. Delete this block + restart
# proxy to fully remove the tier (see docs/PLAN-oxalpha-promo-2026-08-21.md §6).
oxalpha:
  base_url: "https://openrouter.ai/api/v1"
  key_env: "OPENROUTER_OXALPHA_KEY"     # preferred: NEW $0-spend-limit key (see §2.4)
  headers: { HTTP-Referer: "https://hermes.local", X-Title: "Hermes Agent" }
  pricing_model: promo_zero
  promo:
    expires_at: "2026-08-28T00:00:00Z"  # conservative hard deadline (community: Aug 26–28 fuzzy)
    post_promo_pessimistic_per_m: { input: 10.0, output: 30.0 }  # safety flip — priced OUT, not a market estimate
    verified_rate: { input: 0.0, output: 0.0 }   # observed 2026-08-21 (§0.3)
  budget_usd: 0                          # ANY nonzero charge → tier disable + anomaly event
  data_sensitivity: allowlist            # §2.5
```

### 2.2 Scoped `model_map` additions (zai cells untouched)

```yaml
  model_map:
    oxalpha:
      vision: "stealth/ox-alpha"     # NEW task_type 'vision' — only oxalpha defines it
      bulk_summarize: "stealth/ox-alpha"  # NEW deferrable task_type, oxalpha-only
    # zai: / deepinfra: / ppq: / openrouter: / ollama_cloud: — UNCHANGED
```

- Adds **new task types** (`vision`, `bulk_summarize`) with exactly one provider each. The mapping is
  `(provider, task_type) → model`; `src/model_mapping.py` falls back gracefully for missing cells, so
  existing types are unaffected. **No edit to `zai.coding` / `zai.reasoning` / any existing cell.**
- Routing INTO the tier is **opt-in by task type only** — callers must send `X-Task-Type: vision|bulk_summarize`
  (composes with CG-5's task_type logging). No generic failover path ever selects oxalpha.

### 2.3 Promo-expiry mechanism (hard deadline + pessimistic flip)

- `promo.expires_at = 2026-08-28T00:00Z` — **conservative**: community window is Aug 26–28 fuzzy; we assume the EARLIEST plausible end and treat anything after as a bonus.
- At expiry (checked at request time and by the hourly collector): tier **auto-disables** (health_factor → ∞ per ADR-004 invariant 3: unreachable, never zero-priced) and effective price flips to `post_promo_pessimistic_per_m` ($10/$30 — chosen deliberately ABOVE the CG-6 $0.10/M failover ceiling and above any plausible promo-end rate so the tier is priced OUT of every path even if a bug leaves it enabled).
- Rationale: promo-end price is UNKNOWN — the routstrd-$18.81 pattern is exactly "price changed mid-flight, config kept the stale cheap rate". We never let a stale $0 ride past the deadline.

### 2.4 Spend guard — budget $0 (anti-routstrd-bleed pattern)

- Every oxalpha response carries `usage.cost` (verified §0.3). Guard: **any `cost > 0` → immediate tier disable + loud `anomaly_events` row + proxy log line**. Re-enable requires explicit human action (uncomment + restart), never automatic retry.
- Wallet delta cross-check: the every-5-min balance collector already reads OpenRouter credit balance; a negative delta while oxalpha is the only enabled OpenRouter consumer = same anomaly event.
- **402 path:** existing `_mark_unfunded()` (5-min retry) handles credit demands; for this tier a 402 additionally disables it for the promo remainder (a $0 model demanding credits means the promo terms changed).
- **Key hygiene — critical:** simply un-commenting the dormant main `OPENROUTER_API_KEY` re-arms PAID deepseek failover in the hardcoded external chain (risk CG-6 exists to kill; CG-6 has not landed). **Preferred:** provision a NEW OpenRouter key with a **$0 spending limit** → `OPENROUTER_OXALPHA_KEY`. Zero re-coupling of paid failover; main key stays dormant. Fallback (only after CG-6 is live): uncomment main key and rely on the $0.10/M ceiling. → Felix decision D6 (§7).

### 2.5 Data-sensitivity policy (allowlist; ZDR claimed but unverifiable)

- **Allowed task types:** `vision` (screenshot/UI analysis of OUR OWN non-sensitive UIs; synthetic test images), `bulk_summarize` (public documents only), `shadow_eval` (fixtures constructed for eval, no live data).
- **Never sent:** secrets, API keys, customer data, repo contents/dumps, production logs — until/unless trust is established post-eval AND Felix re-approves (separate decision, not in this plan).
- Enforcement point: the oxalpha request path rejects calls whose task_type is not in the allowlist; allowlist lives in the `oxalpha:` config block (deletes with the tier).

### 2.6 Rate-limit backoff (limits are unannounced — §0.1)

- No advertised limits and no `x-ratelimit-*` headers observed. Policy: on HTTP 429 → exponential backoff starting 60 s (60/120/300 s), log ALL `x-ratelimit-*`/`retry-after` headers whenever present (empirical discovery), circuit breaker trips after 5 consecutive failures / 300 s cooldown (existing `circuit_breaker_threshold` values reused). Backoff never bubbles 429 to callers as a retry storm — callers get 503/defer semantics like any exhausted tier.

---

## §3 COST-GATE INTERPLAY (must compose with CG-1..CG-9, not conflict)

### 3.1 `/v1/pricing` + `price_observations` — never $0

- When CG-2's `GET /v1/pricing` lands, oxalpha appears as a row with effective price = **$0.001/M (ADR-004 floor — `strategy.min_effective_price`)**, never $0.00, exactly like other zero-cash tiers.
- `price_observations` rows: `provider=oxalpha, model=stealth/ox-alpha, rate_per_m=0.001, is_measured=false` (catalog/promo rate, not a market measurement).
- **Schema stance:** NO new columns from this plan. If CG-2 ships a `source` column (v2.1 already uses `source='zai_amortized'` conventions), oxalpha rows set `source='promo'`. If CG-2 hasn't shipped it, the promo-tag registry lives in `src/promo_tier.py` (provider-name set) — filter joins on provider name. Either way: **zero schema change owned by this plan** (§6).

### 3.2 p20 percentile history — promo rows tagged AND filtered

- CG-2's percentile gate computes p20 over trailing-7d hourly medians of `price_observations`. Two distortions if oxalpha rows are NOT filtered:
  1. **During promo:** $0.001 rows drag the p20 band to the floor → every real tier (zai $0.0043–$0.0084/M baseline) looks "expensive" → false DEFER of legitimate deferrable crons.
  2. **After promo:** the pessimistic flip ($10/$30) lands as an apparent price explosion inside the 7d window → band spikes → false ALLOW windows and a garbage baseline for up to a week.
- **Rule (input requirement to CG-2, or a follow-up patch if CG-2 lands first):** percentile history query filters `source != 'promo'` (or `provider NOT IN promo_registry`). Promo rows remain stored (audit) but never shape the band — before, during, or after.

### 3.3 CG-6 ceiling + velocity/daily caps

- oxalpha at the $0.001 floor **trivially passes** CG-6's static $0.10/M external-failover ceiling — noted, and harmless because §2.2 keeps oxalpha out of generic failover entirely (task-type opt-in only; the ceiling still backstops the fallback-of-last-resort case).
- **Velocity/daily caps SHOULD treat $0 promo tiers as paid-class, not exempt:** "free" is precisely when a silent retry loop survives every cap and then becomes a bill the moment the promo ends mid-loop. Concretely: oxalpha counts toward CG-6's velocity anomaly ($5/h) and daily paid cap at its **effective** rate ($0.001/M — will never trip in practice), while the REAL tripwire is §2.4's absolute rule (any `usage.cost > 0` → kill + anomaly), which is tighter than any derived-spend cap. Post-promo, the pessimistic flip ($10/$30) puts oxalpha far above the $0.10/M ceiling → CG-6 auto-excludes it fail-closed even if disable logic were bypassed. Defense in depth: task-type opt-in → $0-budget guard → ceiling → expiry flip.

---

## §4 SHADOW-FIRST STRATEGY

**Phase 0 — probe (DONE, this doc §0).** Model exists; key authenticates despite wallet purge; 1.1 s RTT; `cost: 0` confirmed; no rate-limit headers.

**Phase 1 — shadow evaluation (no production traffic).** N ≥ 50 calls across our real task shapes:
- worker-goal text (~15): decomposed subtask prompts from recent manager runs (fixtures, sanitized);
- vision screenshots (~20): our own UI/synthetic screenshots (non-sensitive per §2.5), plus a small multimodal summarization set;
- summarization (~15): public-doc summarization at several lengths.
- Scored against **glm-5.3 baseline** (same prompts through our own subscription — marginal cash $0): correctness rubric per shape, format compliance, **refusal rate** (community reports political-guardrail refusals — we measure on OUR shapes), latency, cache-hit behavior. No paid Kimi-vision comparison by default (Felix decision D5).
- Artifacts: JSONL results + summary section in this doc's successor report. Harness reuses `shadow_hook`/`shadow_logger` conventions (ADR-006); no production schema touched.

**Phase 2 — conditional routing (only on quality verdict + Felix approval).** Enable routing for APPROVED task types only (`vision`, `bulk_summarize`), via the §2.2 scoped map. Entry criteria: shadow verdict ≥ glm-5.3 baseline on approved shapes, refusal rate below threshold, Felix sign-off (D7). **Never** production manager/worker chains (`zai.coding`/`zai.reasoning` paths) before — and unless separately approved, after — the verdict.

**Phase 3 — promo-end teardown.** At `promo.expires_at` (or earlier on any §2.4 trip): tier auto-disabled; manual cleanup = delete config block, restart proxy, archive eval artifacts, confirm `/v1/pricing` no longer lists oxalpha (promo-tagged `price_observations` history stays for audit, already filtered from p20).

---

## §5 TASK BREAKDOWN (small by design — promo, not product)

Every task carries the paste-ready gate text (quality-gates v3.1.0, identical to CG plan §6):

```text
GATE (quality-gates v3.1.0 — required before task close):
1. TDD: red→green — write failing tests first; tests+impl committed atomically.
2. TESTS-PASS: python3 -m pytest tests/ -v — full suite green before push.
3. CROSS-FAMILY REVIEW: cold review via worker-reviewer-kimi (reviewer had zero
   implementation involvement) before merge.
4. DOCS-SAME-COMMIT: relevant docs/*.md updated in the same atomic commit as code.
5. ATOMIC COMMITS: one logical change per commit, conventional message.
6. PUSH: push to github remote (branch wt/glm53-quota-cleanup-t_da1b7c10); if
   hooks block: `git push --no-verify` (--no-verify goes AFTER the git subcommand).
```

### OX-1 — Promo-tier guard module (repo, pure)
- **Scope:** `src/promo_tier.py`: expiry check (`expires_at` → disable), pessimistic price flip, spend guard (`cost > 0` → disable + `anomaly_events` row), 402→disable-for-promo, promo-tag registry + `price_observations` filter helper for the p20 query (§3.2), rate-limit backoff policy constants. Pure module + config defaults in `providers.yaml` draft block (§2.1) as a repo-side fixture; NO live config edit.
- **Files:** `src/promo_tier.py`, `tests/test_promo_tier.py`, `config/providers.yaml` (repo copy, additive block), docs update (this file §2 marked implemented).
- **Tests:** expiry flip math; charge→disable→anomaly event ordering; filter excludes promo rows from synthetic p20 window; allowlist rejection paths.
- **Deps:** none (composes with CG-1 interface; hands filter helper to CG-2). **Worker:** worker-admin. **Effort:** 0.5 d.
- **Status: ✅ implemented 2026-08-21 (t_ce6edf86):** `src/promo_tier.py` + `tests/test_promo_tier.py`
  (73 tests green) + additive `config/providers.yaml` block — expiry flip, cost>0 kill (once) +
  anomaly row, 402 disable, promo tag + p20 filter, allowlist, ADR-004 floor, backoff constants.
  Awaiting cross-family review before merge.

### OX-2 — Proxy wiring of the oxalpha tier (production touch)
- **Scope:** live `~/.hermes/bot/config/providers.yaml` additive `oxalpha:` block (from OX-1 fixture); `zai_proxy.py` — new tier in `EXTERNAL_PROVIDERS`-style registry reading the new key env, scoped `vision`/`bulk_summarize` task-type routing (X-Task-Type opt-in, allowlist-enforced), 429 backoff, spend-guard hook calling OX-1 module; key provisioning per D6 ($0-limit key preferred); service restart + revert note per AGENTS.md.
- **Files:** `~/.hermes/bot/zai_proxy.py`, live `providers.yaml`, `docs/` revert note, `tests/test_oxalpha_proxy_contract.py` (repo-side contract tests).
- **Tests:** contract: task-type opt-in only; non-allowlisted task never reaches oxalpha; cost>0 kill path (mocked); 402/429 handling; `zai.coding`/`zai.reasoning` byte-identical behavior (regression).
- **Deps:** OX-1; Felix D1/D2/D6 approval. **Worker:** worker-merchant-qa. **Effort:** 0.5–1 d.

### OX-3 — Shadow evaluation campaign (N ≥ 50)
- **Scope:** harness (reuses shadow conventions) + campaign over §4 Phase 1 shapes; glm-5.3 baseline pairing; report: rubric scores, refusal rate, latency, verdict recommendation. **Zero production routing.**
- **Files:** `scripts/oxalpha_shadow_eval.py`, `docs/REPORT-oxalpha-shadow-2026-08.md`, fixtures dir (synthetic/sanitized only).
- **Tests:** harness unit tests (fixture loading, scoring determinism, baseline pairing).
- **Deps:** OX-2 (tier callable), CG-5 landing helps (task_type logging) but not required. **Worker:** worker-admin. **Effort:** 0.5–1 d + 2–3 d wall-clock.

### OX-4 — Conditional enable + teardown runbook (config-only + docs)
- **Scope:** IF verdict + Felix D7 approval: flip scoped routing live for approved types (config-only change, pre-written); either way, ship the teardown runbook (§6 steps as executable doc) and execute teardown at promo end.
- **Files:** live `providers.yaml` (routing enable line), `docs/` runbook + revert notes.
- **Tests:** n/a (config flip) — regression suite from OX-2 re-run.
- **Deps:** OX-3 verdict, D7. **Worker:** worker-merchant-qa. **Effort:** 0.25 d.

**Totals: ~1.75–2.75 implementation-days.** Deliberately small; anything beyond this scope is post-promo productization and NOT in this plan.

---

## §6 ROLLBACK

- **Remove the tier = delete the `oxalpha:` config block (and the two scoped `model_map` cells) + `systemctl --user restart zai-proxy`.** Nothing else references the tier; the guard module (`src/promo_tier.py`) is inert without config and can stay or be deleted in a follow-up.
- **No schema changes** in this plan. The only schema-adjacent touch is the promo tag on `price_observations` rows: if CG-2's `source` column exists we populate it (`'promo'`); if not, the tag is a provider-name registry in code (§3.1). Justification for avoiding schema change: a promo tier lasting < 7 days cannot justify a migration; the registry deletes with the module.
- Revert notes required per AGENTS.md for OX-2/OX-4 production touches; key un-provisioning (if a $0-limit key was created) is a dashboard delete — documented in the runbook.
- Post-teardown invariant: `/v1/pricing` shows no oxalpha row; p20 percentile band contains zero promo-sourced samples (filtered by tag, which survives in stored history for audit).

---

## §7 DECISION POINTS FOR FELIX (blocking — nothing schedules before these)

| # | Decision | Default proposed | Consequence if declined |
|---|---|---|---|
| D1 | Approve oxalpha integration at all? | Shadow-only first (OX-1..OX-3); routing (OX-4) is a separate later approval | No promo value extracted; plan archived — zero cost incurred |
| D2 | Approve the data-sensitivity allowlist (§2.5: own-UI vision, public docs, eval fixtures; no secrets/customer/repo data)? | As written | Tighter list (eval fixtures only) or no-go |
| D3 | Approve promo-end guess **2026-08-28T00:00Z** (conservative vs community Aug 26–28 fuzzy)? | Yes — early deadline only forfeits cheap tail, never risks paid tail | Alternative date; any date > Aug 28 accepts unknown-pricing risk |
| D4 | Approve shadow campaign length: **N ≥ 50 calls, 2–3 days wall-clock**, then verdict? | Yes | Longer campaign spends more of the promo window on eval; shorter risks a weak verdict |
| D5 | Allow ANY paid Kimi-vision comparison calls (default **no paid spend**)? | No — glm-5.3 text baseline + rubric-only vision scoring | Small $ spend (~$1–2) for a stronger vision baseline |
| D6 | Key strategy: **new $0-spend-limit OpenRouter key** (preferred) vs un-dormant main key (only after CG-6 live)? | New $0-limit key | Main-key path re-arms paid failover pre-CG-6 — routstrd-class exposure |
| D7 | (Later, after OX-3 report) Approve Phase 2 conditional routing for `vision` + `bulk_summarize`? | Only on verdict ≥ glm-5.3 baseline + refusal rate acceptable | Tier stays shadow/eval-only until promo ends; teardown per §6 |

---

*Verified-facts raw evidence: probe headers/body archived at `/tmp/oxprobe.{hdr,body}` (2026-08-21, this session). This document is a plan; no production system was modified.*

---

## §7 OX-2 STATUS — proxy wiring landed (2026-08-22, task t_2ed46556)

Per the EMERGENCY operator directive (2026-08-22 ~16:00, task body; chain at
16:00: zai 429 → routstrd 500 → routstr down → deepinfra 402 → openrouter
PAID burn), OX-2 shipped:

- **Failover insertion (LIVE)**: oxalpha is attempted as a FREE candidate in
  the proxy's external failover order — after the z.ai keys, before every
  paid provider. Guard-wrapped (`PromoTierGuard`: expiry/402/spend kills),
  fail-closed on absent/invalid key, 429 backoff 60/120/300 s + breaker
  5/300 s, single 90-s attempt, forced `stealth/ox-alpha` + `low` + ≤8192
  completion tokens. Any error falls through to the existing paid chain
  (zero regression); terminal 503 path unchanged (429 never bubbles).
- **Alias / preferred_for (ARMED, INERT)**: acceleration §4 pre-chain
  mechanism wired but `enabled: false` — the OX-3b rung-1 flip activates it.
  glm-5.2/glm-5.3 hard-excluded in code; kill-switch
  `~/.hermes/bot/.oxalpha_alias_off`; images never aliased; gated task types
  never aliased at rung 1.
- **Decision core**: `src/oxalpha_tier.py` (pure) + 31 contract tests
  (`tests/test_oxalpha_tier.py`). Production edits + revert procedure:
  `docs/REVERT-oxalpha-proxy-wiring-2026-08-22.md`.
- **Tension resolution (recorded)**: acceleration §5.1 says oxalpha never
  enters `EXTERNAL_PROVIDERS` — honored for the dict/LiveRouter/alias
  surface (no Kalman/price-sort interaction); the EMERGENCY governs the
  failover attempt ORDER, where oxalpha precedes the paid candidates.
- **Key note (D6)**: no OpenRouter provisioning API on the host — the
  $0-spend-limit key must be created manually (dashboard, limit $0) and
  pasted to `~/.hermes/.env` (`OPENROUTER_OXALPHA_KEY`); until then the
  wiring is live-but-inert (fail-closed; invalid key ⇒ 401 ⇒ fall-through).
  OX-2 did not read, print, or probe any key value.
- **Pre-existing failures (not OX-2)**: 32 telnyx/CG-surface test failures
  exist on this branch independent of OX-2 (proven by snapshot re-run with
  OX-2 files removed — identical 32).
