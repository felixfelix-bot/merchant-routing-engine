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
- Evidence: ~/reports/consultant-{a,b}-featherless-2026-09-02.md;
  ~/.merchant-routing/reports/provider-hunt/2026-09-02.md

### Standard Compute (standardcompute.com) — PARKED 2026-09-02
- API: `api.stdcmpt.com/v1` (OpenAI-compatible, 200 unauth)
- Type: flat $19–$249/mo, free tier no card
- Catalog: 6 models, ALL Claude-family; smart-routing proxy — you don't pick the
  model, it routes for you. No model pinning.
- Why parked: zero overlap with our mix (no glm/kimi/deepseek/qwen); can't pin
  models so the flat router can't price a call; "unlimited" = fixed budget with
  /fair-use page.
- Revisit triggers: they add model pinning + non-Claude catalog [T4].
- Evidence: ~/.merchant-routing/reports/provider-hunt/2026-09-02.md

### Morph (morphllm.com) — PARKED 2026-09-02
- API: `api.morphllm.com/v1` (endpoint live, auth required — no unauth listing)
- Type: per-token credits (OpenRouter: morph-v3-large $0.90/M in)
- Why parked: pricing page JS-gated (429/308), catalog unverifiable without
  signup — violates our no-signup verification bar. OpenRouter listings are
  narrow code-edit models, not the advertised GLM-5.3/Kimi-K3 catalog.
- Revisit triggers: unauthenticated /v1/models or public flat pricing appears [T3].
- Evidence: ~/.merchant-routing/reports/provider-hunt/2026-09-02.md

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
- Evidence: ~/reports/plugsky-canary-2026-09-02.md

### Yolo-Auto — PARKED 2026-09-02 (from transcript reality-check)
- One model only: Qwen3.8-27B FP8, 256K ctx, $19/mo. No catalog overlap with
  our mix. Real, but useless to the router.

### Dialagram / "Nexum Router" — DEAD 2026-09-02
- Fabricated by an AI transcript — no live service found. Do not re-add without
  independent live verification.

---

## LEAD — surfaced, unvetted (needs consultant pass before surfacing to Felix)

*(hunt cron appends here; entries auto-expire to DEAD after 60d without vetting)*

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