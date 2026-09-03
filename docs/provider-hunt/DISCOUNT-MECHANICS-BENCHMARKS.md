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

## Fabricated mechanics (never re-verify these — 2× confirmed false)

- **OpenRouter `:floor`** — zero `:floor` variants in 421 models. Real variants: `:batch`, `:nitro`.
  (Invented twice by the same AI-mode source on 2026-09-02.)
- **DeepInfra "flex 0.8×"** — they have a Batch API, no flex tier.
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
