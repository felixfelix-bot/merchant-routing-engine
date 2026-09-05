# Handover: Inference Market Pricing Models — Implications for routstr

**Audience:** LLM agents working on routstr development (routstrd, routstr-proxy, pricing).
**Author:** Felix's manager agent (Hermes, merchant-routing-engine).
**Date:** 2026-09-04. **Status:** all external claims live-verified 2026-09-04 (verification ledger §5).
**Self-contained:** no prior context needed. No secrets.

---

## 0. Why this document exists

Felix compared inference procurement models (energy-based vs hourly GPU rental vs per-token) and asked how the ideas fit routstr. This doc: (1) states what is verifiably true about each pricing model, (2) locates routstr in that market, (3) lists concrete adoptable ideas, (4) lists traps, (5) leaves a verification ledger. External claims carry source URLs; internal claims carry our DB evidence.

## 1. The three pricing models in the 2026 inference market

| Model | Who | Mechanism | Weakness | Verified |
|---|---|---|---|---|
| **Time-based GPU rental** | RunPod, Vast.ai, TensorDock, Lambda, Hyperstack, Genesis, Paperspace, OCI | $/hr per instance; spot/interruptible 50–80% off (Vast, documented) | Idle waste at bursty traffic; you own ops + weights serving | live 2026-09-04 |
| **Per-token** | Together, DeepInfra, OpenRouter, PPQ | flat $/M input + output, margins baked in | efficiency gains you create (shorter prompts, cache hits) accrue to the provider | our default procurement |
| **Energy-based** | Neuralwatt | $10.00/kWh PAYG headline (subscription tiers $8.50/$8.00/$7.50/kWh); per-token option exists; cached prefix billed at 10–20% of input rate | niche catalog (no GPT/Claude/Gemini) | live + our lane data below |

**Our internal ground truth (zai_usage.db, trailing 7d to 2026-09-04):**

| Lane | Calls | Input M | Output M | Cost | Effective input $/M |
|---|---|---|---|---|---|
| Neuralwatt | 1,068 | 74.2 | 1.6 | **$1.33** | ~$0.018 (94% of cost sits in cache hits) |
| Kimi K3 (model-matched) | 580 | 22.0 | 0.2 | **$0.34** | ~$0.016 |
| Kimi K2.7-code | 205 | 13.0 | 0.04 | $0.16 | ~$0.012 |

Lifetime NW: 9,524 calls, 623.8M tokens, $393.76 ⇒ $0.63/M blended; NW glm-5.3 specifically: $0.017/M in, $0.75/M out — vs z.ai list $1.40/$4.40 that is **~82× cheaper input, ~5.9× cheaper output**. Energy pricing in practice = per-token with aggressive cache pass-through. Their published glm-5.3 benchmark: avg cache hit 93.4%.

**Cache economics (the load-bearing fact):** upstreams bill cached prefix at steep discounts — Kimi K3 $0.30 vs $3.00/M (0.10×, also on Neuralwatt), Neuralwatt ~0.1–0.2×, z.ai $0.26 vs $1.40/M (0.186×). Caching is automatic server-side; customer-side lever = **stable-first prompt layout**. We measured 97.7% cache hit on a repeated 1.6k-token prefix against z.ai live on 2026-09-04.

## 2. Where routstr sits today

- **Model:** per-token, Cashu-settled. Markup chain: `exchange_fee 1.04 × upstream_provider_fee 1.15 × provider_fee 1.43` ≈ **1.71×**. provider_fee 1.43 = Felix's 30%-margin-on-revenue rule (`fee = 1/(1−margin)`).
- **BTC/USD:** live (Kraken/Coinbase/Binance min-of-first-two, ~120s). Sat margin ≠ USD margin — check `upstream_cost_usd < sat_revenue × btc_price`.
- **Dynamic pricing:** Kalman integration (Nostr kind-30315 → provider_fee every 2 min) prices z.ai upstream by real quota state, replacing static litellm cost-map fallback.
- **Upstreams:** public node = PPQ only (346 models incl GPT/Claude/Gemini — the catalog NW lacks). Friends proxy adds zai-coding + ollama. **z.ai coding-plan must NEVER back a public node — ToS §4 forbids subscription resale.**

## 3. Adoptable ideas (ranked)

### 3.1 Cache pass-through pricing (build-worthy)
**Problem:** routstrd's pricing_resolver multiplies a static per-token price. It cannot see that a warm-prefix request costs us 0.10–0.21× on input. Cache-friendly customers are overcharged (demand suppressed); cold-prefix heavy requests may be underpriced.
**Build:** parse upstream `usage.prompt_tokens_details.cached_tokens` per request; price input as `p_full×(1−h) + p_cached×h` at invoice time (h = that request's cached fraction). Keep provider_fee on top. Expose "cached: N tokens, saved: X sats" in response metadata — visible savings sell.
**Precedent:** Neuralwatt meters `cache_savings_usd` per request; our NW lane's $0.018/M effective input IS this mechanism working.

### 3.2 Off-peak / weekend pricing (cheap to formalize)
Our internal z.ai cost basis drops off-peak (coding-plan credits 0.5× off-peak + weekends; verified). Kalman already encodes scarcity/peak. A customer-visible off-peak multiplier captures margin we leave on the table when our cost falls but customer price doesn't. Low effort: time-of-day term in the same pricing path.

### 3.3 Reasoning-effort exposure (VERIFIED — build-ready)
z.ai API: `reasoning_effort` accepts `max`/`high`/`low`, **default `max`** (docs.z.ai/guides/overview/concept-param.md). GLM-5.3/5.3-FLASH: thinking is FORCED (cannot disable, docs.z.ai/guides/capabilities/thinking-mode.md) but effort CAN be lowered to `low`. Billed output = thinking + answer, so effort is a direct price knob. routstr can pass it through per-request and price tiers accordingly ("reasoning: low" below "max"). Same for our internal workers: routine kanban tasks on `low` cuts billed completion tokens materially (Hermes side: task QS-11 candidate).

### 3.4 Portable warm context (watch, don't build)
A standard where compatible providers accept exported KV/prefix state would let customers carry warm context across nodes. routstr — multi-provider Cashu router — is positioned to broker it (restore-fee < prefill-fee). No standard exists yet (verified 2026-09-04: llama.cpp slot save/restore local-only; no cloud provider exports cache state). Provider-hunt cron watches for it.

### 3.5 Failover economics (Hermes-side, verified numbers)
When z.ai quota exhausts, internal fallback candidates: Neuralwatt glm-5.3 ($0.017/$0.75) ≪ PPQ passthrough rates — for the GLM/DeepSeek/Kimi model class NW should sit ahead of PPQ in failover order (PPQ remains sole option for GPT/Claude/Gemini). Eval folded into cache-aware dispatch work (QS-9).

## 4. Do NOT adopt (with reasons)

- **Energy metering as our pricing axis:** we own no inference hardware; upstream energy is their cost axis. Decorating a per-token resale.
- **Spot-GPU self-hosting now:** frontier MoE weights are 2.8T-param class needing multi-node serving; our whole NW spend is $1.33/wk — nothing to undercut. **License note:** GLM-5.3 and Kimi K3 weights are downloadable under custom MIT-style licenses with a MaaS clause (restricts >$10B-revenue resellers) — NOT plain MIT, but fine for us. Self-host is license-viable, just uneconomic at current volume. Revisit at ~1000× volume or if portable-warm-context standardizes.
- **z.ai coding plan as public upstream:** ToS §4. PPQ is the legit public upstream.
- **Hourly rental for spiky traffic:** idle-waste trap; only wins at sustained 90%+ util (training/fine-tune), not routstr's workload.

## 5. Verification ledger (all live-verified 2026-09-04)

| Claim | Verdict | Evidence |
|---|---|---|
| Neuralwatt $10/kWh | REAL (PAYG headline; tiers $7.50–8.50/kWh) | neuralwatt.com/technology, portal.neuralwatt.com |
| NW prefix caching reduces billed cost | REAL (cached billed 10–20% of input; `cache_savings_usd`, `cachedInputDiscount: 0.1`) | portal.neuralwatt.com/docs/energy-methodology |
| NW serves frontier MoE | REAL (GLM-5.2/5.3, Kimi K2.7-code/K3, DeepSeek V4, Qwen; NO GPT/Claude/Gemini/Mixtral) | portal model catalog |
| "Renting wastes 50–70% on idle" | FALSE (not NW's claim; theirs: "40%+ idle power reduction 125W→73W") | Crusoe case study on neuralwatt.com |
| Kimi K3 cache 90% off ($0.30 vs $3.00/M) | REAL — and on Neuralwatt too | platform.moonshot.ai/docs/pricing/chat-k3.md |
| KDA = K3 architecture | REAL (2.8T params, 69 KDA + 24 Gated MLA layers) | huggingface.co/moonshotai/Kimi-K3 README |
| GLM-5.3 weights "MIT" | PARTIAL — custom MIT-style + MaaS clause (>$10B rev), `license: other`, gated:false | huggingface.co/zai-org/GLM-5.3 |
| Kimi K3 "1.56 TB model card" | PARTIAL — 2.8T params; no 1.56TB figure in card; custom Kimi K3 License | HF model card |
| z.ai `reasoning_effort` | REAL — `max`/`high`/`low`, default `max`, GLM-5.2+ | docs.z.ai/guides/overview/concept-param.md |
| GLM-5.3 thinking can be disabled | FALSE — forced thinking; effort CAN go `low` | docs.z.ai/guides/capabilities/thinking-mode.md |
| Spot GPU 60–80% off | REAL on Vast ("Saves 50–80%", documented); TensorDock "80%" = homepage marketing | docs.vast.ai rental-types |
| Self-host fits (live HF cards, 2026-09-04) | VERIFIED: GLM-5.3 = 753B (FP8 751GB → 8×H200); Kimi K3 = 2.78T/104B-act (MXFP4 1.36TB → 8×B200 tight); DS-V4-Flash = 284B/13B-act (8-bit 283GB → 8×H100 or 2×H200); DS-V4-Pro 1.6T needs B300 class | HF safetensors; portal.neuralwatt.com/pricing |
| GPU rental vs NW (8×H100/H200 spot $10–16/hr, live) | HARD NO at spiky util — needs ≥5.5k tok/s sustained to beat our $0.75/M energy-leg rate; only Vast + Hyperstack even offer spot+API; 9–10 min TB-weight cold starts fatal to dispatcher spin-up | consultant fetches 2026-09-04 |
| NW token-leg vs energy-leg | VERIFIED: token-leg GLM-5.3 $4.5/M out ≈ 45× the $10/kWh energy leg (~$0.10/M); our lane rides the cheap leg — margin collapses if NW converges pricing | portal.neuralwatt.com/pricing |
| z.ai cached input 0.186×, automatic, `cached_tokens` reported | VERIFIED-LIVE | docs.z.ai pricing; our probe 97.7% hit |
| z.ai coding-plan credits discount cached tokens | OPEN — inline probe inconclusive (integer-credit quota API); 16k-prefix re-probe queued (QS-3 Part 1D) | — |
| z.ai off-peak 50% credits (weekends) | VERIFIED (coding-plan terms) | z.ai coding-plan page |
| **Our NW cache_hit telemetry** | **DARK since 2026-08-25** (meter bridge stopped writing `cache_hit`; 7d all-zero vs lifetime 81%) | zai_usage.db — fix queued (QS-6 scope) |
| **Chutes.ai (SN64) GO-CONDITIONAL 2026-09-05** | 14 TEE models, USD-anchored ($227.09/TAO fixed); DS-V4-Flash $0.096/M eff (DeepInfra 4.3× cheaper, PPQ 1.45× dearer); **NO ToS resale ban** (routstr-legal, unlike DeepInfra §11(a)(viii)); PAYGO explicitly permits high-volume automation; 100% uptime 90d; NVFP4 GLM-5.2 = NVIDIA production quant; TAO-emission subsidy + balance-forfeiture/Nevis/$10K-cap risk | llm.chutes.ai/v1/models (authless), chutes.ai/terms verbatim, status.chutes.ai, 2-consultant pass — docs/provider-hunt/CHUTES-VERDICT-2026-09-05.md |

## 6. Pointers

- Kalman pricing integration: `skills/devops/routstr-node-ops/references/kalman-pricing-integration.md`
- Routing telemetry dataset: `datasets/routing-telemetry/` + `docs/HANDOVER-routing-telemetry-friend.md`
- Internal cache-work queue (Hermes side, same pattern as §3.1): router-maintenance board QS-3/QS-6/QS-9/QS-10
- Friend onboarding handover: `docs/routstr-friend-onboarding-handover.md`

*End of handover.*
