# Featherless (featherless.ai) — OPS/INTEGRATION/RISK Vetting

**Consultant:** B (Ops Architect) · **Date:** 2026-09-02 · **Pass:** two-consultant vetting, consultant B
**Scope:** Developer tier ($50/mo credits, per-token) as a candidate lane for the flat market-based live router.

---

## 1. LEGITIMACY TRIAGE — verdict: REAL COMPANY, sloppy marketing (NOT Plugsky-class)

**Company signals (all live-verified 2026-09-02):**

| Signal | Finding | Weight |
|---|---|---|
| Legal entity | "Delaware limited liability company" (TOS §1) | + |
| Founding story | Founded 2023 as **Recursal.AI**, rebranded Featherless 2024. Founders **Eugene Cheah, Harrison Vanderbyl, Wesley George** co-lead **RWKV** (Linux Foundation project). | + strong |
| Funding | Claims **$20M Series A (2026)** co-led by AMD Ventures + Airbus Ventures, with BMW i Ventures, Kickstart, Panache, Wavemaker. | + (unverified independently, but specific + falsifiable) |
| Live API | `api.featherless.ai/v1/models` returns **21,911 models** unauthenticated, with real per-model metadata (context_length, pricing, model_class, concurrency_cost, features). | + strong |
| Blog/changelog | Active through **Aug 2026**; "GLM-5.3-Flash is live" dated Aug 27 2026 — matches the model's `created` timestamp (2026-08-27) in the live catalog. | + strong |
| HN footprint | 419 hits, but thin: mostly 1–7 pt posts by `darinver` (founder/employee) announcing model additions; one "Show HN: Run any Llama model finetune" (7 pts, 2 comments, 2024-06-24). | ~ neutral |
| Reddit | Search endpoint returned HTML (blocked), not JSON — could not verify. | ~ neutral |
| GitHub org | `github.com/featherless` exists (id 3107959, created 2012) but is **Jeff Verkoeyen's personal org** (jeffverkoeyen.com, featherless.design) — a name collision, NOT the AI company. The AI company's code is not under this org. | − (sloppy) |
| HuggingFace org | `huggingface.co/api/organizations/featherless` → **404**. They are a HF *inference partner* (re-host HF catalog), not a HF org. Models served under original HF IDs (`zai-org/GLM-5.2`, etc.). | ~ neutral |
| Domain | Cloudflare-fronted (172.67.72.137, nelly/keenan.ns.cloudflare.com). WHOIS unavailable (privacy). | ~ neutral |

**Red-flag triage vs Plugsky:**

| Red flag (scout) | Reality | Severity |
|---|---|---|
| Dead `docs.featherless.ai` subdomain | Confirmed 000. Docs live at `/docs/` on main domain (200, 1MB+). | Cosmetic — stale DNS, not fabrication |
| Template logo wall (Ubisoft/Dropbox/Cisco/VMware/YouTube/Meta/HF) | Confirmed on homepage. These are Webflow template logos. | Marketing sloppiness — but the *actual* "Backed By" section names real VCs (AMD Ventures, Airbus Ventures), which is falsifiable and specific |
| "30,000+ models" vs 21,911 actual | Confirmed inflation (~37%). | Marketing puffery — the *real* number is still enormous and verifiable |
| Fair-use "unlimited" | Only on **Chat** tier (concurrent-unit based). Developer tier is **per-token credit-based** — no "unlimited" to abuse. | Not applicable to Developer |

**Distinguishing test (Plugsky = everything fabricated):** Plugsky's red-flag kit was *unvalidated stats, dead docs, fork-only products, logo walls, fair-use "unlimited"* — with **no live product behind any of it**. Featherless has a **live, working, unauthenticated API serving 21,911 models with internally-consistent metadata**, a real founding team with verifiable RWKV history, a real (if stale) TOS, and an active changelog whose dates match the live catalog. The red flags are all *marketing-layer* sloppiness, not *product-layer* fabrication. **This is a real company with sloppy marketing.**

---

## 2. TOS/AUP RISK — verdict: LOW for our internal use, one stale-TOS ambiguity

**Live pages fetched:** `/legal/terms-of-service` (200), `/legal/privacy-policy` (200), `/docs/plans` (200), `/docs/developer-plan-and-credits` (200), `/docs/concurrency-limits` (200), `/docs/request-pricing-and-credits` (200). Note: `/pricing`, `/tos`, `/aup` are 404 — the real paths are `/legal/*` and `/docs/*`.

### 2a. Does Developer tier permit API/automation? — YES (confirmed, with one caveat)

- **Homepage pricing** (live): Chat plan is footnoted *"Not for reselling, app/API traffic, background automation, or benchmarking. Misuse may lead to cancellation without refund."* The **Developer** plan has **no such restriction** — it is positioned as *"Build production AI with the fastest usage-based inference."*
- **Docs `/docs/plans`** (live): *"Feather Developer plans are subscription and credit based… **Developer plans are designed for API-driven applications and workloads** where usage may vary over time."*
- **Docs `/docs/developer-plan-and-credits`** (live): *"Request pricing lets your organization pay for API usage with prepaid credits… power production applications — whether agent fleets or other AI applications."*

**Caveat (the one real risk):** The **TOS** (`/legal/terms-of-service`, last updated **June 10, 2024**) is *stale* and predates the current Developer-tier positioning. It says: *"Individual plans are for interactive use or proto-typing and experimentation by the purchaser. Persons making use of individual plans for other purposes will have their subscription terminated and no refund will be provided. **Scale plans are not subject to said limits and may be used in arbitrary applications, including inference resale.**"*

The TOS's "individual plan" vs "Scale plan" split does not cleanly map to today's "Chat" vs "Developer" naming. The docs call Developer plans "scalable," which would put them in the "Scale" bucket (arbitrary use permitted). But the TOS language is 2024-era and ambiguous. **This is the single biggest contractual risk** — a hostile reading could classify Developer as an "individual plan" and terminate us for automation. Mitigation: it is low-probability (the entire Developer tier is *marketed* for API/automation, and terminating paying API customers for API use would be self-defeating), but it is non-zero and should be flagged.

### 2b. Resale restriction binding our internal use? — NO

- TOS permits "inference resale" on Scale plans. Our use is **internal** (routing our own fleet through the local proxy), which is *less* than resale.
- **ROUTSTR rule** already governs this: internal router use is fine; the public routstr node is **never** backed by internal quota keys. Featherless would be an internal lane only. No conflict.

### 2c. Fair-use clause that could cut us off mid-month? — NO (structurally impossible)

- Developer tier is **per-token credit-based**. There is no "unlimited" to abuse. You pay per token; when credits hit $0, **API calls are blocked** (not terminated) until you top up. Credits **do not expire** and **roll over**.
- **Volume ceiling is self-imposed by the credit stipend.** At $50/mo credits:
  - GLM-5.3-Flash ($0.15/M in) → ~333M input tokens
  - GLM-5.2 ($0.75/M in) → ~66M input tokens
  - DeepSeek-V3.2 ($0.30/M in) → ~166M input tokens
  - Kimi-K2.5 ($0.77/M in) → ~65M input tokens
- Our fleet total is 2.95B tokens/30d across all lanes. Featherless would be a **T5 per-token fallback for long-context GLM/Kimi overflow**, not a primary lane — plausible monthly volume is **~$50 worth (66M–333M tokens)**, i.e. **~2–11% of fleet volume**, and it *cannot* exceed that because it's credit-capped. **No fair-use cutoff risk exists at any volume we could send.**

### 2d. Rate limits / concurrency — "1 agent env" ≠ API concurrency cap

- **Homepage** says Developer = "1 agent environment included." **Docs `/docs/plans`** says Developer = "**100 concurrent units**."
- These are **different things**. "Agent environment" = their hosted agent runtime/sandbox (Nemo Claw, Open WebUI, Open Claw, Hermes Agent — the "Agents" section). "Concurrent units" = API concurrency.
- **Concurrency model** (`/docs/concurrency-limits`): each model has a `concurrency_cost` (1–4 units by size). A request reserves that many units while in flight; released on completion. Our target models all have `concurrency_cost=4` (GLM-5.2, Kimi-K2.5, DeepSeek-V3.2, Qwen3-235B). **100 units ÷ 4 = 25 concurrent in-flight requests** for a 4-unit model. That is ample for our fleet (which is single-proxy, mostly serialized).
- **No RPM/TPM rate limit** documented on Developer tier. The only hard limit is the credit balance (out-of-credits → 402/blocked).

---

## 3. SERVING REALITY CHECK — verdict: all 5 target models live, no staleness signals

**Unauthenticated `GET /v1/models` → HTTP 200, 21,911 models** (scout said 21,910 — off by one, trivial). Metadata is rich and internally consistent.

| Model (our mix) | Present | context_length | input $/M | output $/M | `created` (UTC) | concurrency_cost |
|---|---|---|---|---|---|---|
| `zai-org/GLM-5.2` | ✅ | 262,144 | 0.75 | 2.40 | 2026-06-16 | 4 |
| `zai-org/GLM-5.3-Flash` | ✅ | 262,144 | 0.15 | 0.50 | **2026-08-27** | 4 |
| `moonshotai/Kimi-K2.5` | ✅ | 262,144 | 0.77 | 3.50 | 2026-01-27 | 4 |
| `deepseek-ai/DeepSeek-V3.2` | ✅ | 131,072 | 0.2995 | 0.45 | 2026-01-26 | 4 |
| `Qwen/Qwen3-235B-A22B` | ✅ | 32,768 | 0.455 | 1.82 | 2026-02-04 | 4 |

**Staleness signals: NONE.** The catalog has **no `last_updated` field** — only `created` (model-card creation date). All five `created` timestamps are 2026, and GLM-5.3-Flash is from **4 days ago** (2026-08-27), matching the blog's "GLM-5.3-Flash is live" post. No model shows a stale/abandoned date. `owned_by` is uniformly `"Feather"` (they re-host under their own namespace but preserve original HF model IDs — good for our `_PROVIDER_MODEL_NAMES` translation).

**Additional catalog notes:**
- `zai-org/GLM-5.3` (non-Flash) is also present: 32K ctx, $1.40/$4.40 — note the **non-Flash GLM-5.3 is 32K, but GLM-5.3-Flash is 262K** (counter-intuitive; Flash has the bigger window).
- `deepseek-ai/DeepSeek-V4-Pro` ($1.60/$3.20, 262K) and `DeepSeek-V4-Flash` ($0.14/$0.28, 262K) are present — newer than our V3.2 target.
- `moonshotai/Kimi-K2.6`, `Kimi-K2.7-Code`, `Kimi-K3` all present (K3 at $3.00/$15.00).

**Status page:** `featherless.ai/status` → HTTP 200, but it is a **self-hosted React SPA** (7 `<script>` tags, 1.2MB, `id="root"`, no `__NEXT_DATA__`). It renders a per-model "72 hours ago → Now" uptime list. **This is self-reported and unverifiable** — no independent third-party uptime source. Treat as marketing, not telemetry. (Scout's note confirmed.)

**Auth reality:** `/v1/chat/completions`, `/v1/plan`, `/account/concurrency` all return **401 "You must be signed in"** unauthenticated — the API is live and gated, not a stub. `/v1/models` is the only unauthenticated endpoint (catalog listing).

---

## 4. INTEGRATION SKETCH (paper only — no code changes made)

### 4a. `_extract_cost()` — explicit provider branch (MANDATORY, avoid oc2/oc3 fall-through bug)

Featherless is OpenAI-compatible and (like PPQ/OpenRouter/NeuralWatt) does **not** return a `usage.cost` field — it returns `usage.prompt_tokens`/`completion_tokens`. So step 1 (`_extract_cost_module`) will miss it, and without an explicit branch it falls through to the **`rate_derived_fallback`** catch-all at line 3899–3910, which uses `_rpt_rate(provider)` (Kalman-measured blended $/M) × total_tokens. That is the **exact oc2/oc3 bug pattern** — a blended rate that ignores per-model input/output split and prompt-cache discounts.

**Required wiring** (mirror the `neuralwatt` branch at lines 3854–3898, which is the closest analog — per-model rate table + cached-token handling):

1. Add a `FEATHERLESS_MODEL_RATES` dict keyed by provider-native model ID (`zai-org/GLM-5.2`, `moonshotai/Kimi-K2.5`, etc.) with `input`/`cached_input`/`output` from the live catalog pricing.
2. Add an explicit `if provider == "featherless":` branch that parses `usage` (prompt/completion/cached tokens), looks up per-model rates, computes `(uncached×input + cached×cached_input + completion×output)/1e6`, and returns `("cached_rate_derived" | "rate_derived")`.
3. **Do NOT** rely on the catch-all. The catch-all's blended `_rpt_rate` would mis-price GLM-5.2 ($0.75 in) vs GLM-5.3-Flash ($0.15 in) by 5×, and would ignore the ~99% prompt-cache hit ratio that makes our repeated-context workload cheap.

### 4b. Auth env var pattern

Add to `_load_external_keys()` (line 552): `elif line.startswith("FEATHERLESS_API_KEY=") and "featherless" not in keys:` → `keys["featherless"]`. Then `FEATHERLESS_KEY = _EXTERNAL_KEYS.get("featherless", "")` and `FEATHERLESS_BASE = "https://api.featherless.ai/v1"`. Matches the existing PPQ/OpenRouter/DeepInfra pattern exactly.

### 4c. Lane registration (three surfaces, all required)

1. **`PROVIDER_MODELS`** (flat_router.py ~L131): add `"featherless": {"glm-5.2", "glm-5.3-flash", "kimi-k2.5", "deepseek/deepseek-v3.2", "qwen3-235b"}` — canonical IDs only (FR-2 rule). Note: our canonical set uses `kimi-k2.5` and `deepseek/deepseek-v3.2`; verify these canonical forms exist (the registry currently has `kimi-k2.5` under telnyx, and `deepseek/deepseek-v4-*` slashed forms — V3.2 may need a new canonical ID).
2. **`_PROVIDER_MODEL_NAMES`** (zai_proxy.py ~L661): add `"featherless": {"glm-5.2": "zai-org/GLM-5.2", "glm-5.3-flash": "zai-org/GLM-5.3-Flash", "kimi-k2.5": "moonshotai/Kimi-K2.5", "deepseek/deepseek-v3.2": "deepseek-ai/DeepSeek-V3.2", "qwen3-235b": "Qwen/Qwen3-235B-A22B"}`.
3. **`PROVIDER_TIER`** (flat_router.py ~L274): `"featherless": "per_token"` (T5).

### 4d. Price floor / tier placement

- **Tier T5 (per-token)** — Kalman-measured real $/M, no time decay, no balance factor. Correct tier: it's a prepaid credit balance, but unlike NeuralWatt (T2 "balance" with depletion penalty), Featherless credits **roll over and don't expire**, so there's no use-it-or-lose-it pressure — plain T5 is right.
- **Seed rate** (`_SEED_RATES` ~L245): seed conservatively. GLM-5.3-Flash is the cheapest target at $0.15/M in; seed `"featherless": 0.30` (blended, conservative — the Kalman will measure down to ~$0.15–0.20/M for Flash-heavy traffic, mirroring the PPQ $0.80→$0.1391 convergence). Do NOT seed at $0.15 (would over-route before measurement).
- **Floor:** T5 providers get the global `MIN_EFFECTIVE_PRICE = 0.001` floor. Featherless at $0.15/M will **never** beat the T1/T3/T4 sunk-cost lanes ($0.001 floor) or even PPQ ($0.1391 measured) on price — it only wins when those are exhausted (∞) or for **long-context** requests where cheaper lanes can't serve 262K. This is the correct market outcome: Featherless is a **catalog-diversity / long-context fallback**, not a price leader.

### 4e. Health / delist canary (adapt the ollama usage-shape canary)

The `scripts/ollama_usage_shape_canary.py` pattern (fingerprint → diff → silent-unless-drift) adapts cleanly:

- **New script** `scripts/featherless_catalog_canary.py`: unauthenticated `GET /v1/models`, fingerprint = `{http_status, model_count, presence of our 5 target IDs, their context_length + pricing}`. Diff against state file `~/.merchant-routing/featherless-catalog-state.json`.
- **Drift signals to catch:** (a) a target model **delisted** (the TOS explicitly reserves the right to de-list models — §1 "may result in the de-listing of models that were previously operable"), (b) a **context_length shrink** (e.g. GLM-5.2 262K→128K), (c) a **price change** (regime shift — feeds ADR-013 alerting).
- **Why this matters more than for ollama:** Featherless's TOS *explicitly* warns of model de-listing, and their catalog is 21,911 models of which our 5 are a rounding error. A silent de-list of `zai-org/GLM-5.2` would otherwise surface as a 503 cascade (the Aug 25 72h-outage lesson). The canary is the early-warning.
- **Health gate** already handles the rest: 401/402/429 → backoff windows, `_mark_unfunded` on 402 (out-of-credits), circuit breaker on >10 failures. No new health code needed — the existing `_provider_health` / `_is_provider_funded` machinery covers it.

### 4f. Peak-window (06:00–10:59 UTC) interaction — NONE (by design)

`peak_mult` (3.0×) applies **only to z.ai T1 quota keys** (`peak_multiplier()` in `src/price_kalman.py`, `peak_hours_utc=(6,10)`). Featherless is **T5 per-token** — it carries no peak multiplier, no time decay, no scarcity factor. Its effective price is flat at the Kalman-measured rate 24/7.

**Consequence:** during peak (06:00–10:59 UTC = 11:30–16:29 IST), z.ai's effective price triples (from ~$0.001 floor to ~$0.003), but that is still ~50× cheaper than Featherless's $0.15/M. So Featherless does **not** become attractive during peak — it only becomes attractive when z.ai is *exhausted* (∞) AND ollama/opencode/ppq are exhausted or can't serve the context length. **No peak-window code interaction is required.** (The only subtlety: if we ever add a peak multiplier to T5 providers, Featherless would need to be excluded — but that's not the current design.)

### 4g. Effort estimate (worker-hours)

| Task | Hours |
|---|---|
| `_extract_cost()` explicit branch + `FEATHERLESS_MODEL_RATES` table | 2.5 |
| Auth env var (`_load_external_keys` + key constant + base URL) | 0.5 |
| Lane registration (PROVIDER_MODELS + _PROVIDER_MODEL_NAMES + PROVIDER_TIER + _SEED_RATES) | 1.5 |
| Canonical-ID reconciliation (verify `kimi-k2.5` / `deepseek/deepseek-v3.2` / `qwen3-235b` canonical forms exist) | 1.0 |
| `featherless_catalog_canary.py` + state file + cron wiring | 2.5 |
| Tests (cost-extraction unit tests, canary tests, router candidate tests) | 2.0 |
| **Total** | **~10 worker-hours** |

---

## 5. VERDICT

### **CONDITIONAL — WIRE as a T5 per-token long-context fallback, gated on one TOS clarification.**

**Rationale:** Featherless is a **real, funded, live-serving company** (not Plugsky-class). Its Developer tier **explicitly permits API/automation**, is **per-token credit-capped** (so no fair-use cutoff risk and no runaway-spend risk — the $50/mo stipend is a hard ceiling), and serves **all 5 of our target models live with 262K context on GLM-5.2/Kimi-K2.5** — a genuine catalog-diversity win over our current lanes (z.ai caps GLM-5.2 context, and no current lane serves Kimi-K2.5 at 262K). It will **never win on price** (T5 at $0.15/M+ vs T1/T3/T4 sunk-cost $0.001 floor), so it slots in as a **long-context / thin-coverage fallback**, exactly where the scout placed it.

**The single biggest integration risk:** the **stale TOS ambiguity** — the June-2024 TOS's "individual plans are for interactive use only" clause does not cleanly map to today's Developer tier, and a hostile reading could terminate us for automation. This is *low-probability* (the entire Developer tier is marketed for API/automation) but *high-impact* (mid-month termination with no refund). **Condition:** confirm in writing (Discord/support) that the Developer credit plan is a "Scale plan" for TOS purposes before wiring it as a lane — or accept the risk explicitly and keep it as a non-critical fallback (which it already is, by price).

**Secondary risks (lower):** (1) model de-listing (TOS §1 explicitly reserves it) — mitigated by the catalog canary; (2) self-reported status page (unverifiable uptime) — mitigated by the existing health-gate backoff/circuit-breaker; (3) the `_extract_cost` fall-through bug if the explicit branch is skipped — mitigated by making the branch mandatory in the integration checklist.

---

## Appendix — live evidence log (all 2026-09-02)

- `GET https://api.featherless.ai/v1/models` → 200, 21,911 models, 7.0MB JSON.
- `GET https://featherless.ai/legal/terms-of-service` → 200 (TOS, "Last updated June 10th, 2024").
- `GET https://featherless.ai/docs/plans` → 200 (Developer = "$50+", "100 concurrent units", "designed for API-driven applications").
- `GET https://featherless.ai/docs/developer-plan-and-credits` → 200 (per-token, credits roll over, don't expire, out-of-credits = blocked).
- `GET https://featherless.ai/docs/concurrency-limits` → 200 (concurrency_cost model, 1–4 units).
- `GET https://featherless.ai/status` → 200, React SPA (self-reported, unverifiable).
- `GET https://featherless.ai/about` → 200 (Recursal.AI origin, RWKV founders, $20M Series A).
- `GET https://docs.featherless.ai` → 000 (dead subdomain, confirmed).
- `GET https://featherless.ai/pricing`, `/tos`, `/aup` → 404 (real paths are `/legal/*`, `/docs/*`).
- `POST /v1/chat/completions` (no key) → 401 "You must be signed in".
- `GET https://api.github.com/orgs/featherless` → exists (2012) but is Jeff Verkoeyen's personal org (name collision).
- `GET https://huggingface.co/api/organizations/featherless` → 404.
- HN Algolia: 419 hits, thin (1–7 pt posts by `darinver`).
