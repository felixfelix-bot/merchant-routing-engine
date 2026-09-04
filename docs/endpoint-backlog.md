# Endpoint Backlog — Escape-Hatch Provider Tracker

Living list of potential API endpoints, tracked so we can surface options FAST when
we need more quota. Every entry carries a verdict, its evidence, and the exact
trigger that would make us look again.

**Maintainers:** the cheap-provider-hunt cron (a89bd61e3b68, daily 04:30 IST)
appends entries; consultants add verdicts; manager commits. Verdicts older than
~30 days are STALE — prices and ToS change; always re-verify live before acting.

## Surfacing protocol (when we need quota)

QUOTA-PRESSURE TRIGGERS — any of these means "open this backlog":

- **T1 — Gate-blocked:** quota gate BLOCK with no cheap lane serving (proxy dead
  or all providers exhausted).
- **T2 — Weekly pressure:** friend key weekly window >70% (refill trigger per
  zai-quota-gate skill fallback-capacity policy).
- **T3 — Lane death/pricing spike:** an incumbent lane dies (sub cancelled,
  provider lockout) or its effective rate rises above a backlog entry's.
- **T4 — Demand shift:** traffic pattern changes (e.g. >128K-context requests
  materialize at volume, new model family needed).

STEPS:

1. Match the active trigger(s) against each entry's `revisit triggers`.
2. Live re-verify matched entries (curl pricing page + /v1/models + ToS; cached
   verdicts expire ~30d). No signup, no spend.
3. Still viable → two-consultant pass (cost economist + ops architect, glm-5.2)
   → update verdict here.
4. **Felix gates any signup, spend, or wiring. Never auto-wire from this list.**

Status legend: `PARKED` = evaluated, rejected, triggers listed. `LEAD` = surfaced
but unvetted (no verdict yet — needs consultant pass before anything else).
`DEAD` = verified nonexistent/fabricated.

---

## PARKED — evaluated, rejected (with revisit triggers)

### Featherless (featherless.ai) — PARKED 2026-09-02
- API: `api.featherless.ai/v1` (OpenAI-compatible, 21,911 models, unauth listing works)
- Type: per-token, Developer tier $50/mo credits (rollover); Scale $75+/mo
- Prices: GLM-5.3-Flash $0.15/M in, GLM-5.2 $0.75/M in (262K ctx), Kimi-K2.5 $0.77/M,
  DeepSeek-V4-Flash $0.14/M, DeepSeek-V3.2 $0.30/M, Qwen3-235B $0.46/M
- Overlap: EXCELLENT (glm/kimi/deepseek/qwen all live)
- ToS class: **KILLS IT** — Developer tier is legally an "individual plan" ("interactive
  use or proto-typing... terminated and no refund"); only Scale permits
  automation/resale. Our 22-worker fleet = banned use.
- Why parked: ToS §4 kill + zero routing wins (18–90× worse than ollama_cloud
  $0.0083/M; 9× worse than ppq fallback $0.0155/M) + Kimi-vs-telnyx win is dead
  demand (telnyx ~0 tokens since Aug 25) + 262K-ctx value speculative (p99 req
  181,688 tok; 0.08% of requests >256K). Expected savings if wired: ~$0.
- Revisit triggers: (a) >128K-context demand materializes at volume [T4];
  (b) ollama flat subs die → need per-token fallback catalog [T3];
  (c) Scale-tier pricing drops below lane economics [T3].
- Evidence: docs/provider-hunt/2026-09-02/ (consultant-a-cost-featherless.md,
  consultant-b-ops-featherless.md, scout-report.md)

### Standard Compute (standardcompute.com) — PARKED 2026-09-02
- API: `api.stdcmpt.com/v1` (OpenAI-compatible, 200 unauth)
- Type: flat $19–$249/mo, free tier no card
- Catalog: 6 models, ALL Claude-family; smart-routing proxy — you don't pick the
  model, it routes for you. No model pinning.
- Why parked: zero overlap with our mix (no glm/kimi/deepseek/qwen); can't pin
  models so the flat router can't price a call; "unlimited" = fixed budget with
  /fair-use page.
- Revisit triggers: they add model pinning + non-Claude catalog [T4].
- Evidence: docs/provider-hunt/2026-09-02/scout-report.md

### Morph (morphllm.com) — PARKED 2026-09-02
- API: `api.morphllm.com/v1` (endpoint live, auth required — no unauth listing)
- Type: per-token credits (OpenRouter: morph-v3-large $0.90/M in)
- Why parked: pricing page JS-gated (429/308), catalog unverifiable without
  signup — violates our no-signup verification bar. OpenRouter listings are
  narrow code-edit models, not the advertised GLM-5.3/Kimi-K3 catalog.
- Revisit triggers: unauthenticated /v1/models or public flat pricing appears [T3].
- Evidence: docs/provider-hunt/2026-09-02/scout-report.md

### Plugsky (plugsky.com) — PARKED 2026-09-02
- API: `api.plugsky.com/v1` (OpenAI-compatible, unauth listing works)
- Type: flat Free/$5.60 Hobby/$14 Starter/$42 Builder/$84 Scale (NOT the
  "$20–120 unlimited" an AI transcript claimed)
- Catalog: 100% rebranded NVIDIA NIM resell (nemotron-nano-9b, nemotron-3-ultra-550b)
  — zero overlap; NIM directly accessible without the middleman.
- ToS/trust class: fabrication-adjacent — stats footnote "pending independent
  validation", logo wall (NVIDIA+MS+OpenAI+AWS+Google+PayPal+Meta+Shopify
  simultaneously), docs subdomain dead, first-party products = opencode/Jan forks.
- Why parked: no catalog overlap + trust insufficient for production dispatch.
- Revisit trigger: ONLY as $0 free-tier gateway to eval Nemotron 3 Ultra 550B
  (model-eval question, not router) [T4].
- Evidence: docs/provider-hunt/2026-09-02/plugsky-canary.md

### Yolo-Auto — PARKED 2026-09-02 (from transcript reality-check)
- One model only: Qwen3.8-27B FP8, 256K ctx, $19/mo. No catalog overlap with
  our mix. Real, but useless to the router.

### Dialagram / "Nexum Router" — DEAD 2026-09-02
- Fabricated by an AI transcript — no live service found. Do not re-add without
  independent live verification.

---

## LEAD — surfaced, unvetted (needs consultant pass before surfacing to Felix)

*(hunt cron appends here; entries auto-expire to DEAD after 60d without vetting)*

### Atlas Cloud (atlascloud.ai) — LEAD→DEAD 2026-09-03 (consultant pass: ToS-toxic)
- API: `https://api.atlascloud.ai/v1` (OpenAI-compatible, 117 models, unauth /v1/models works)
- Type: per-token pay-as-you-go, $1 free credit w/ payment method (card-gated), $25 min top-up, credits expire 365d
- Prices: DeepSeek-V4-Flash $0.14/$0.28 per M, cache-hit $0.028/M (eff $0.1414/M — 1.65% ABOVE ppq $0.1391), GLM-5.3-Flash $0.15/$0.50, GLM-5.2 $1.40/$4.40 (web shows -33% but API charges official), Kimi-K3 $3.00/$15.00, kimi-k2.5 $0.49/$2.50. No flat sub.
- Overlap: 36 models in glm/kimi/deepseek/qwen families incl. GLM-5.2 @ 1M ctx — best overlap seen, moot
- DEAD REASON (ops-architect consultant, browser-read ToS 2026-09-03): Privacy §3 verbatim "you will not access the Services through automated or non-human means" — Featherless-class killer; AUP §2 "thin wrapper for raw resale is strictly prohibited" + §7 "build a competitive product". AUP grants API access but Privacy boilerplate bans automated use — docs contradict, unusable for a routing fleet either way.
- Entity ATLAS CLOUD AI INC (NY addr / Delaware law), domain 2024-04-18, no disclosed funding, media-gen aggregator first (video/image), SOC2/HIPAA claims unvalidated. "400+ models" = 417 across ALL modalities, only 117 LLM on /v1/models — not inflated.
- Verdict: DEAD — ToS kills automated routing even at internal scale; price never beat ppq anyway (needs >2% cache-hit to break even).

### LLM7 (llm7.io) — LEAD→PARK 2026-09-03
- API: `https://api.llm7.io/v1` (45 models on /v1/models, but chat requires API key)
- Type: unknown — /pricing and /terms both 404
- Overlap: glm-5.3, glm-5.3-flash, deepseek-v4-flash, kimi-k3, gpt-5.x (suspicious)
- Red flags: keyless claim false (auth error on chat completions); no public pricing or ToS; GPT-5.x listings suspicious
- Verdict: PARK — cannot verify pricing or ToS without signup; dead policy pages

### Novita AI (novita.ai) — LEAD→PARK 2026-09-03
- API: `api.novita.ai/v1` (endpoint unverified — JSON parse fail)
- Type: per-token, referral credits ($10 signup)
- Prices: DeepSeek V4 Flash $0.14/M in, V3.2 $0.269/M, R1 $0.70/M
- Overlap: DeepSeek family on pricing page
- Red flags: API endpoint not verified; GPU marketplace not API-focused; referral credit model
- Verdict: PARK — DeepSeek pricing matches ppq but API unverified and referral model unsuitable for production

### DeepInfra (deepinfra.com) — LEAD 2026-09-04 (scout-approve + TWO-CONSULTANT PASS = GO w/ conditions)
- Two-consultant verdict (cost economist + ops architect, both live-fetched 2026-09-04): ALL scout pricing claims REAL from DeepInfra's own catalog/pricing page. **Flex tier 0.8× REAL** (`rate_per_service_tier_flex=0.8` in live API catalog + pricing page "0.8x base price") — per-model: applies to DS-V4-Flash-0731/GLM-5.2/GLM-5.3, null for GLM-5.3-Flash/Kimi-K3. **09-02 "fabricated flex" verdict OVERTURNED** (correction logged in DISCOUNT-MECHANICS-BENCHMARKS.md).
- New find scout missed: GLM-5.3-Flash carries `discount:0.5` → $0.075/$0.25/$0.015 = matches z.ai promo. GLM-5.2 `discount:0.35` (billing effect unverified — probe before pricing in).
- Ops: NO automation/anti-agent clause (unlike Atlas). Internal 22-worker fleet = normal use; **routstr public upstream = BLOCKED by §11(a)(viii) resale clause**. 200 concurrent req/model default; 100% 90d API uptime (status.deepinfra.com); founded Sep 2022; $107M Series B verified (500 Global et al). Card or prepay required before API use. Cache telemetry exposes `usage.prompt_tokens_details.cached_tokens` — EXACT field our zai_proxy parser (:3842/:5341) expects, zero parser changes. `reasoning_effort` enum none…max + `reasoning.enabled`; `service_tier` param; model strings `vendor/model`.
- Economics vs our 2.95B tok/30d (99% in): DS-V4-Flash-0731 beats ppq at ALL h (even h=0: $0.08 < $0.1391) — $57/mo flex @h=0.9 vs ppq $406/mo. Kimi-K3 $4,998/mo, GLM-5.2 $1,370/mo at same volume = non-starters for bulk, but Kimi-K3 cached eff $0.5415 beats telnyx $2.70 in for input-heavy. $25 ollama flat lane unbeatable by ANY metered provider.
- GO conditions: (1) internal-fleet-only, hard-block routstr upstream; (2) bounded billing probe for discount flags + 181K-ctx truncation + flex 429 behavior under spiky load; (3) Felix creates account (virtual CC, min top-up) — buy step is his.
- Evidence: ~/.merchant-routing/reports/provider-hunt/2026-09-04.md (scout + consultant verdicts appended)

## INCUMBENTS (reference — not candidates)

ollama_cloud (grandfathered $25/mo flat, ~$0.0083/M eff — THE bar to beat) ·
z.ai friend key (free, 5h/weekly windows) · ppq.ai (~$0.1391/M pay-per-use) ·
telnyx kimi-k3 direct ($2.70/$13.50) · neuralwatt (kWh) · opencode_go (unlimited
slow) · ollama_cloud_3 (credit-pool, T4 included tier, $0.001 floor, 90% monthly
delist guard — see plans/ollama3-burn-reduction-2026-09-02.md)

## Maintenance

- Hunt cron appends new finds to LEAD with date; verdicts move entries to
  PARKED/DEAD with evidence links.
- Manager commits this doc weekly (doc-only commits go straight to main, dual-push
  github + origin ngit).
- Surfacing action always: re-verify live first — cached verdicts expire ~30d.