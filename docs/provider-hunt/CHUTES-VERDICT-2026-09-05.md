# Chutes.ai (Bittensor SN64) — GO-CONDITIONAL verdict

**Date:** 2026-09-05. **Trigger:** daily provider-hunt find, Felix requested
two-consultant investigation. **Consultants:** 2× glm-5.2 (read-only,
terminal+web, live fetches); all load-bearing claims manager-verified verbatim
against chutes.ai/terms, /pricing, live `/v1/models`, model-guide pages, and
news index.

## Verdict

**CONDITIONAL GO** — internal mid-tier failover lane, and the ONLY evaluated
provider whose ToS has **no resale ban** (routstr-legal). Not a primary:
DeepInfra GO lane stays 4.3× cheaper for the DS-V4-Flash workhorse class.

## Economics (all verified against live catalog 2026-09-05)

Effective rate at our workload (99% input, h=0.9 cache-read):
`eff = 0.99*(0.1*in + 0.9*cache) + 0.01*out` — hunt arithmetic confirmed exact.

| Model | in/out/cache USD | eff $/M | vs DeepInfra ($0.0224) | vs PPQ ($0.1391) |
|---|---|---|---|---|
| DeepSeek-V4-Flash-0731-TEE (fp8, 1M ctx) | 0.44/1.32/0.044 | **0.096** | 4.3× dearer | 1.45× cheaper |
| GLM-5.2-TEE (NVFP4, 1M ctx) | 1.25/3.95/0.125 | 0.275 | — | dearer |
| Kimi-K3-TEE (MXFP4 native, 1M ctx) | 3.0/15.0/0.30 | 0.714 | — | beats Telnyx $2.808 3.9× |
| Qwen3.5-397B-A17B-TEE (fp8, 256k) | 0.45/3.0/0.045 | 0.115 | — | cheaper |

Prices USD-anchored; dual TAO denomination at fixed $227.09/TAO (consistent
across all 14 models — derived conversion, deposit-time applied).

## Why GO (verified)

1. **No resale/competition clause at all** — full-text sweep `resell/resale/
   redistribut/competit` = 0 hits in ToS. Mirror-opposite of DeepInfra
   §11(a)(viii). Routstr node may legally forward Chutes calls.
2. **PAYGO explicitly permits automation**: "Any use cases that require high
   concurrency, high query volume, or other highly automated usage must use
   the PAYGO option" (subscriptions restrict it; we'd be PAYGO anyway).
3. **Catalog = 14 curated TEE models, deliberate pivot not decay** —
   "From Volume to Value: Building a Sustainable AI Inference Platform"
   (news, Mar 26 2026): killed ~$6/user/mo free tier (11k users), 9:1
   cost-revenue private model, subscription abusers; −45% tokens, revenue/Mtok
   +37.7%, revenue/GPU $4.05→$5.89. Sustainability-positive signal.
4. **Reliability surface**: BetterStack status 100% uptime 90d (website+API),
   zero reliability complaints in GitHub issues (15 open, all features).
5. **Hermes-native**: chutes.ai/agents/hermes ships provider config
   (`base_url llm.chutes.ai/v1`, `key_env CHUTES_API_KEY`, cpk_ keys).
   Comma-inline failover aliases (`modelA,modelB,modelC:latency`).
6. **Quality quants are production-grade**: GLM-5.2 = nvidia/GLM-5.2-NVFP4
   (NVIDIA ModelOpt, MIT, "ready for commercial use"); Kimi-K3 MXFP4 native;
   DS-V4-Flash fp8 = same format DeepInfra serves.
7. **TEE is real but irrelevant to us** — Intel TDX + GPU passthrough,
   open-source stack (sek8s). No secrets in our dispatch traffic.

## Risks (verified)

- **TAO-emission subsidy**: prices below raw GPU cost, bridged by SN64
  emissions (~14.4% of Bittensor emissions, #2 subnet; only 17/242 miners
  "earning"). Emission halving/deregistration → price rise or shutdown.
- **No balance protection**: "Any remaining account balance may be forfeited"
  on termination; crypto deposits "final and non-refundable"; liability cap
  $10K; Nevis governing law, binding arbitration, class-action waiver.
- **Pseudonymous collective** (Chutes Global Corp / Rayon Labs origin, ~12
  contributors, no CEO). Funding state unverifiable.
- **Rate limits unpublished** without authenticated `GET /users/me/quotas`.
- Consultant-B nuance: TEE ≠ "zero logging" (compute-side isolation; ToS data
  handling still governs).

## Conditions of GO

1. PAYG only, funded via **Stripe USD** — NEVER TAO/crypto prepay.
2. **Balance ceiling $50** (rolling, below Pro tier cost — forfeiture risk
   capped).
3. Bounded quality probe before routing (<$0.05): 5× GLM-5.2 NVFP4 vs z.ai
   native through quality_floor scoring; DS-V4-Flash cache-hit billing check.
4. Lane order: DeepInfra → Chutes → PPQ (failover), routstr resale optional
   lane.

## Re-open / retract triggers (encoded in provider-hunt-gate chutes_fence)

(a) quotas incompatible with dispatch volume, (b) SN64 emission halve/
deregister, (c) cached tier beats DeepInfra effective, (d) catalog pivot
removes our model families, (e) uptime pattern breaks.

## Full evidence

Consultant reports + verification transcript:
`~/.merchant-routing/reports/provider-hunt/2026-09-05-chutes.md`
