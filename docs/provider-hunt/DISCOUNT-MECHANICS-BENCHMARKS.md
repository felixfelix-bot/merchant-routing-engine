# Discount Mechanics Benchmarks — Live-Verified 2026-09-02

Reference for consultants and the cheap-provider-hunt cron. Every number below was
checked live against the provider's own pages/API/docs during the 2026-09-02
AI-transcript fact-checks (two consultant passes, manager-verified). Use these as
**ground truth when evaluating any "spot / batch / flex / off-peak" pricing claim** —
AI-mode conversations routinely invent discount mechanics, so verify against this table
first, and only re-probe live if the number is dated >30 days.

## Verified discount mechanisms (the only real ones as of 2026-09-02)

| Mechanism | Provider | Real numbers (live-checked) | Notes |
|---|---|---|---|
| Time-of-day (off-peak) | DeepSeek V4 | Flash $0.22/$0.66 per M off-peak, Pro $0.66/$1.98; **2× during peak 01:00–04:00 + 06:00–10:00 UTC Mon–Fri** (their own API docs) | Peak = Beijing business hours; billing automatic on timestamp, no signup |
| Off-peak on subscription | Z.ai Coding Plan | "Off-peak usage charged at **50% of standard credit rate**; peak = Mon–Fri 14:00–18:00 SGT" (docs.z.ai/devpack/overview) | Lite tier real at 18 USD/mo; Pro/Max $80/$168 JS-rendered — unverified |
| Batch (24h deferred) | OpenAI + Anthropic | **Flat 50% off** both, confirmed live | Not usable in agent loops (24h delay) |
| service_tier:flex | OpenAI | **~50% off**, real parameter | For deferrable work |
| Flex suffix | Neuralwatt | `-flex` model suffixes exist live (6 variants on /v1/models); **0.65× factor + stream:true mechanics NOT verified** — public docs 404 | QS-3 live probe queued (t_fd9bafa4) with <$0.05 spend guard |
| kWh metering | Neuralwatt | $10/kWh PAYG; Pro $100/13.33 kWh; our meter via balance-tracker: **415.7M tok = 6.85 kWh** (16.5 Wh/M blended, 94% cached) | Only kWh-metered provider found; overage $7.50/kWh |
| Spot GPU | Spheron | 8×H200 $20.56/hr spot vs $38.32 on-demand; 8×B200 $31.20/hr (their GLM-5.3 page) | Self-hosting math dies vs our ollama ~$0.0083/M |
| Spot GPU | RunPod | H100 from $1.99/hr (runpod.io/pricing) | FluidStack unverified (JS-blocked) |
| service_tier:flex | DeepInfra | **0.8× base price** (`rate_per_service_tier_flex=0.8`, pricing page + API catalog live-verified 2026-09-04); priority tier 1.5×. Per-model — flex on DS-V4-Flash-0731/GLM-5.2/GLM-5.3, **null for GLM-5.3-Flash/Kimi-K3**. "occasional unavailability" tradeoff | CORRECTION: 09-02 pass listed this as FABRICATED — verdict wrong, overturned 2026-09-04 by two-consultant live re-check (catalog metadata + pricing page both document it). Lesson: 09-02 consultant likely read docs page only, not API catalog metadata |
| Cache metering | DeepInfra | cached input 0.10–0.20× per model (DS-V4-Flash-0731 $0.08/$0.016, Kimi-K3 0.10×, GLM-5.2 0.1867× == z.ai ratio); response exposes `usage.prompt_tokens_details.cached_tokens` + `cache_write_tokens` — matches our zai_proxy parser | Catalog `discount` flags exist but billing effect UNVERIFIED (GLM-5.2 0.35, GLM-5.3-Flash 0.5) — probe before pricing in |

## Fabricated mechanics (never re-verify these — 2× confirmed false)

- **OpenRouter `:floor`** — zero `:floor` variants in 421 models. Real variants: `:batch`, `:nitro`.
  (Invented twice by the same AI-mode source on 2026-09-02.)
- **DeepSeek "50% batch"** — actually off-peak time-of-day pricing, not batch.
- **Azure Foundry DeepSeek "spot-equivalent $0.145/M"** — unverified (JS-blocked pages), likely fiction.
- **DeepSeek "cache-hit $0.003625/M"** — real cache-hit rate is **$0.022/M Pro off-peak** ($0.044 peak), not $0.003625. The "$0.435/$0.87 base" is also wrong — real Pro off-peak is $0.66/$1.98.
- **LMSYS Chatbot Arena anonymous inference** — the Space is a static leaderboard, no inference endpoint.

## Evaluation rules for consultants (from these benchmarks)

1. **Token math must be against OUR volume**: 2.95B tok/30d, 99% input, p99 request 181K.
   Neuralwatt at workhorse volume = ~$600/mo vs ollama $25/mo (loses 24×). Spot-GPU
   self-host = $0.57/M best case (70× worse than ollama effective).
2. **Flex/tier discounts only move dollars on METERED lanes** (ppq, opencode_go, neuralwatt
   overage). Flat subs ($25/mo) and quota keys gain $0 — never propose flex for them.
3. **On subscription lanes, flex = allowance-extension** (same lever class as our
   exhaust_weight): 0.65× burn on a 13.33 kWh allowance ≈ 20.5 effective kWh.
4. **Time-of-day discounts only matter if our cron windows move** — our crons already
   ride the cheap window; the win is re-ordering interactive traffic, which is small.
5. **Wh/energy tables pasted by AI chats are ~17× off** — always use Neuralwatt's own
   meter via the balance-tracker bridge, not published-rate scaling.

## Open items feeding the queue

- QS-3 (t_fd9bafa4): live -flex probe — measures the real 0.65× factor + glm-5.3-flex existence.
- Provider-hunt rotation seeds (commit f984054): kWh metering, deferred queues, off-peak
  tiers, batch discounts — hunt cron now searches for these daily.

## ToS-toxic providers — verified 2026-09-03 (Atlas Cloud consultant pass)

Do NOT re-vet these; the policy clauses kill automated routing regardless of price.
Browser-read of JS-rendered ToS (curl gets MDX shells — always use the browser for these).

| Provider | Killer clause (verbatim) | Price verdict at our mix |
|---|---|---|
| Atlas Cloud (atlascloud.ai) | Privacy §3: "you will not access the Services through automated or non-human means"; AUP §2: "thin wrapper for raw resale is strictly prohibited" | NOT-COMPETITIVE: DS-V4-Flash $0.14/$0.28, eff $0.1414/M = 1.65% ABOVE ppq $0.1391 (break-even only >2% cache-hit); 17× ollama. GLM-5.2@1M ctx $1.40/M in = premium capability, not cost lane |

Verified-real Atlas numbers (for any future price benchmarking only): DS-V4-Flash
$0.14 in/$0.28 out/$0.028 cache-hit, 1M ctx; kimi-k2.5 $0.49/$2.50; GLM-5.2
$1.40/$4.40 (web -33% display price NOT charged on API). $1 trial credit is
card-gated; $25 min top-up. Entity: Atlas Cloud AI Inc (NY/Delaware), domain
2024-04-18, no disclosed funding. Method note: pricing readable from
`api.atlascloud.ai/v1/models` inline `pricing` field — no auth, no signup needed.
## Prompt-caching / warm-context vetting pass (2026-09-04, .bin#4)

Source: pasted AI-conversation "Warm-Context KV Speedup" (llama.cpp slots + cloud
prompt caching). Verdicts below manager-verified or consultant-verified with live
fetches; load-bearing numbers re-checked against live pages.

REAL (verified):
- z.ai GLM-5.3: **Input $1.4 / Cached Input $0.26 / Output $4.4** (0.186× input);
  GLM-5.3-Flash **$0.15/$0.03** (promo to Sep 9: $0.075/$0.015); cached storage
  "Limited-time Free". Source: docs.z.ai/guides/overview/pricing (live fetch).
- z.ai context caching is **automatic** ("no manual configuration required"),
  implicit prefix matching; cached tokens billed at discounted rate; responses
  expose `usage.prompt_tokens_details.cached_tokens`. Source: docs.z.ai/guides/capabilities/cache.md.
- Anthropic: cache write 1.25× (5m) / 2× (1h), cache read **0.1×** base input;
  `cache_control` explicit + new automatic mode. docs.anthropic.com.
- OpenAI: automatic caching, cached reads **0.1× (90% off, not 50%)** on GPT-5.6+;
  threshold 1,024 tok (GPT-5.6+), 2,048 older. platform.openai.com.
- Google Gemini: implicit caching default-on for 2.5+; e.g. 2.5 Flash cached
  $0.075/M (+$0.50/M/hr storage) through 2026-12-31. ai.google.dev.
- llama.cpp `llama-server`: `--slot-save-path`, `POST /slots/{id}?action=save|restore`,
  `cache_prompt` (default TRUE) — all in master README (ggml-org/llama.cpp
  tools/server/README.md :222/:587/:1150-1174). CPU-only, no GPU caveat.
- Caches are per-provider/org, NOT portable (Anthropic+OpenAI docs; no export).
- LiteLLM real OSS gateway (github BerriAI/litellm).

PARTIAL:
- "Warm context is NOT an ollama feature": ollama HAS in-memory prefix reuse while
  warm (`prompt_eval_cached_count` in API response; keep_alive) — but NO disk
  persistence (PR #16836 OLLAMA_SLOT_SAVE_PATH closed UNMERGED 2026-06; follow-ups
  #17247/#17278 open). Accurate only for the persistence half.

OUR-STACK FIT (economics, 7d lane volumes from zai_usage.db):
- ours (z.ai coding plan) 443.6M in — cached tier exists on PAYG, but our lanes
  are CREDIT-metered subscription: whether credits burn at cached rate = UNKNOWN →
  probe queued (extends QS-3 Part 1B harness). If yes at ~0.19× and ~50-70%
  prefix-hit → effective quota ~1.8-2.3× on biggest lane.
- neuralwatt 74.2M in — cached already banked (94% lifetime via own meter).
- ollama flat lanes ~1.07B in — flat rate, caching moves $0.
- opencode_go 12.1M in — trivial exposure.
- Proxy gap: `cache_hit` column 0 for ALL lanes 7d despite live parsing of
  `prompt_tokens_details.cached_tokens` (zai_proxy.py :3842/:5341) — upstreams
  may not report per-call, or field mismatch; NW billing corrected via meter
  bridge. Visibility task queued with probe.
- Local llama-server warm-KV: NOT a cost lever (no local lane; T470-class CPU
  prefill hopeless at p99 181K). Offline-resilience only.

## Vetting pass 2026-09-21 — "cheapest provider" AI-mode paste (cheap-provider list)

Source: German AI-mode (Google) summary pasted by the operator: DeepInfra/Novita
price leaders, Groq/Together/Fireworks mid-tier, OpenRouter + HF Inference
Providers as smart routers, "free Kimi-K3 via Zenmux", FreeLLMAPI keyless
pooling, Pollinations keyless. Every price claim re-checked LIVE against
authless `/v1/models` catalogs (skill rule 11) — no consultant passes spent.

### Verified REAL (authless catalogs, 2026-09-21) — $/M in / out / cache-read

| Provider | Probed (no auth) | Rates for models we route |
|---|---|---|
| DeepInfra | api.deepinfra.com/v1/openai/models (192 models) | DS-V4-Flash-0731 0.06/0.18/0.015 · DS-V4-Flash 0.09/0.18/0.018 · DS-V4-Flash-Vision-Exp 0.44/1.32/0.014 · GLM-5.3 1.20/4.00/0.20 · GLM-5.3-Flash 0.15/0.50/0.03 · GLM-5.2 0.75/2.40/0.14 · Kimi-K3 2.85/14.25/0.285 |
| inference.net | api.inference.net/v1/models (60 models) | DS-V4-Flash 0.23/0.62/**0.0028** (cache ratio 0.0122× = cheapest cache tier measured anywhere) · DS-V4-Flash-0731 0.53/1.58/0.017 · DS-V4.1-Flash 0.30/1.00/0.007 · GLM-5.3 0.90/3.00/0.15 · GLM-5.3-Flash 0.09/0.28/0.02 · Kimi-K3 2.10/10.95/0.23 |
| HF Inference Providers | router.huggingface.co/v1/models (134 models, per-provider price + throughput + `is_free`) | cheapest live DS-V4-Flash-0731 = deepinfra 0.06/0.18; GLM-5.3-Flash = novita 0.15/0.50; `is_free` count = **0** (no free capacity at all) |
| Zenmux | zenmux.ai/api/v1/models (194 models) + /llms.txt | explicit pass-through: GLM-5.3 1.4/4.4/0.26 (= z.ai list) · Kimi-K3 3/15/0.3 (= Moonshot list) · DS-V4.1-Flash 0.075/0.30/0.0015 · zero-price ids: z-ai/glm-4.7-flash-free, z-ai/glm-4.6v-flash-free, ling-3.0-* |
| Novita | api.novita.ai/v3/openai/models | GLM-5.3-Flash 0.15/0.50/0.03 |
| OneInfer | oneinfer.ai (no catalog API; prices in JS bundle, 450 provider/model rows) | own DeepSeek lane DS-V4-Flash 0.44/1.32/0.014 (cents-per-M scale calibrated against gpt-5 1.25/10 and claude-sonnet-5 2/4 in the same bundle) |
| FreeLLMAPI | github tashfeenahmed/freellmapi (27.8k★, MIT) | BYO-key pooling router — ships NO capacity; 34 providers / 635 endpoints |
| Pollinations | text.pollinations.ai/models | anonymous tier = exactly ONE model: openai-fast (GPT-OSS 20B, OVH) |
| z.ai | docs.z.ai pricing (re-confirmed) | GLM-5.3 PAYG 1.4/0.26/4.4 and GLM-5.3-Flash 0.15/0.03 EXISTS, alongside the $18/mo Coding Plan Lite we run |

### FABRICATED / EXPIRED / WRONG

- **"Zenmux offers Kimi K3 free (Moonshot/Kimmy.K3-free promo)"** — no such id in
  the live 194-model catalog. Zero-price ids are glm-4.7-flash-free,
  glm-4.6v-flash-free and ling-3.0-* tiny models. Same fabrication class as
  OpenRouter `:floor`.
- **"GLM-5.3 has no token-based public API at Z.ai — subscription only"** — FALSE.
  z.ai PAYG rates are live (table above); we run the $18/mo Lite plan as the
  `ours` lane *and* hold PAYG rates.
- **"Novita is the GLM-5.3-Flash price leader (~$0.50/M out)"** — real number,
  wrong conclusion: 16.7× z.ai's $0.03/M flash output; inference.net lists the
  same model at $0.28/M out.
- **"Kimi-K3 $3.00/$15.00 direct is the most economical reference"** — irrelevant
  to us: our neuralwatt kimi-k3 lane measures $0.0587/M.
- **"Keyless endpoints (Pollinations) usable in code"** — the live anonymous tier
  is a single 20B model; below our quality bar by construction.

### Economics vs our lanes (eff input $/M at h=0.90, our ~99%-input mix)

- Measured 7d (zai_usage.db): neuralwatt DS-flash 0.0065–0.0162 ·
  ollama_cloud_2 glm-5.3 0.0155 · **deepseek direct 0.0355 blended (dominant cost:
  5.24B tok / $185.98 in 7d)** · ppq ~0.1391 · `ours` (coding plan) $0 marginal.
- DeepInfra DS-V4-Flash-0731 → 0.006 + 0.9×0.015 = **$0.0195/M** (1.8× under the
  DeepSeek-direct lane we are actually paying for).
- inference.net DS-V4-Flash → 0.023 + 0.9×0.0028 = **$0.0255/M**.
- DeepInfra DS-V4-Flash 0.0252 · inference.net DS-V4.1-Flash 0.0363 ·
  Novita DS-V4-Flash 0.0392 · OneInfer own lane 0.0566.
- GLM-5.3-Flash: z.ai PAYG 0.042 (promo 0.021) vs inference.net 0.027 vs
  DeepInfra 0.042 vs Novita 0.043 — nothing beats the flat `ours` lane for models
  already served there.

### Verdicts

- **DeepInfra — GO stands, but the GO is INERT.** Key is present in
  `~/.hermes/.env`; the disable marker was renamed to
  `.key_disabled_deepinfra.disabled-bak-20260915-211907`, which is NOT the exact
  path the gate checks (`router_state.py:138` / `zai_proxy.py:1329`) → lane
  ENABLED. Yet 0 calls in 7d. Cause: `flat_router.py` prices the lane with a
  single stale seed (`deepinfra: 1.30`, PROVIDER_SEED) and no per-model rates, so
  it never wins and only surfaces in the D-138 last-resort dial after `deepseek`.
  Fix = per-model rates for DS-V4-Flash-0731 / GLM-5.3 + canary — not another
  evaluation.
- **inference.net — WATCH (new; cheapest cache tier ever measured, 0.0122×).**
  Authless priced catalog, 60 models, not yet in KNOWN_PROVIDERS; ToS/resale
  UNVERIFIED; no quality evidence. Probe before wiring.
- **Zenmux — WATCH (free-lane candidate).** Real gateway, pass-through prices (no
  discount on our set). Only interesting feature: 4 zero-price ids → an ADR-014
  internal-only, $0-exposure canary candidate.
- **HF Inference Providers — not a lane, a PRICE ORACLE.** Authless multi-provider
  catalog with per-provider pricing + throughput; `:cheapest` / `:fastest` /
  `:preferred` suffixes are REAL (HF docs). Nothing in it beats our lanes, so its
  value is a free market index for the hunt gate.
- **Novita / Groq / Together / Fireworks / OpenRouter / Moonshot-direct — NO NEW
  ACTION.** Novita loses on output (16.7×); Groq/Together/Fireworks already in
  KNOWN_PROVIDERS; OpenRouter already fenced (§7 no-resale); Moonshot direct
  already go_conditional and beaten by neuralwatt.
- **FreeLLMAPI / Pollinations — NO.** FreeLLMAPI ships no capacity (BYO keys; free
  tiers are RPM/context-limited against p99 181K-token requests and ToS-hostile to
  pooled automation, even though 34×~1.5k req/day nominally covers our ~13.7k
  calls/day). Pollinations anonymous = one 20B model.

### Live-system finding (2026-09-21 23:10–23:16, not in the paste)

`journalctl --user -u zai-proxy.service` shows **171×**
`[flat-router] pool priced out (all ∞) — dialing last-resort ['deepseek','deepinfra']`
inside 60 minutes: the whole flat pool was priced unusable and the metered
DeepSeek-direct lane carried everything. That condition is what makes the
DeepInfra seed bug expensive rather than cosmetic.
