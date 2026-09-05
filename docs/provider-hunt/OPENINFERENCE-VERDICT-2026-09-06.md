# OpenInference (openinference.ai) — VERDICT: WATCH (no deposit, no routing)

**Date:** 2026-09-06 · **Pass:** 3-consultant (ToS/viability, technical/authenticity, pricing/sustainability) + manager self-verification (catalog, ToS bundle, OR cross-check, TPU rates, Wayback) · **Trigger:** provider-hunt FIND #1 (2026-09-06.md)

## Identity (verified)

- Legal entity: **Open Research, Inc., a Delaware corporation dba Open Inference** (June-2026 ToS, archived bundle). Contact: markian@openinference.ai. US entity per OR record.
- Domain history: **openinference.xyz (Mar 2025)** = research org, "We do not plan to offer any services via this website. We will solely provide access to our hosted models through OpenRouter." → June 2026: direct TPU API (gpt-oss-120b/20b + gemma-4) → Sept 2026: **DeepSeek-V4-Flash-only** catalog. Catalog churned twice in 3 months.
- Rebrand ex-"Inducta": reason UNVERIFIABLE — inducta.ai has zero Wayback captures, zero HN/press footprint. Logo path `/inducta_logos/` survives in current bundle.

## Pricing (live /v1/models, authless — manager-verified)

| SKU | in | out | cache-read | eff in @h=0.9 |
|---|---|---|---|---|
| DeepSeek-V4-Flash (direct only) | $0.03/M | $0.075/M | $0.007/M | **$0.0093/M** |
| DeepSeek-V4-Flash-0731 (direct = OR, identical) | $0.05 | $0.16 | $0.013 | $0.0167 |

- Cheapest DS-V4-Flash input ever found (2.4× under DeepInfra eff $0.0224; 15× under PPQ $0.1391). Cheap SKU is **direct-only** — OI absent from OR base-0423 variant (15 providers).
- OR cross-check: Baidu 0731 ($0.05/$0.10/$0.01) **undercuts OI 0731** — OI's only edge is the direct base SKU.

## Consultant findings (load-bearing, spot-verified)

**A — legal/viability:** Internal single-key agent-fleet use compatible with ToS ("service bureau" + "automated use" clauses = resale/scraping boilerplate; own §7/§13 contemplate org agents). Featherless mass-ban precedent UNVERIFIABLE — weighted zero. **Hard traps: §5 "All transactions are final, and no refunds are issued" + §19 sole-discretion termination "for any reason or no reason" ⇒ prepaid balance = unsecured, confiscatable.** Viability FRAGILE (sub-year pivot, no traction evidence, no rate limits). De-risk: one-line email to markian@ confirming business-key use before any deposit.

**B — technical:** Plausible-real weights (GA MoE shape, MIT, deepseek-ai HF ids, TPU-DeepSeek public precedent) but **unproven — logprobs/top_logprobs EXCLUDED on OR**, blocking the cheapest authenticity probe; direct-API logprob support unknown (no key). OR provider stats UNVERIFIABLE-STATIC (client-fetched; CDP timed out). 1M ctx on 32GB/chip = marketing-leaning. Designed 24h <$5 probe suite (uptime/TTFT/concurrency/cache-billing-honesty/logprob-fingerprint/quality-drift; pass bar ≥99.5% uptime, p95 TTFT <3s, cached-token billing within 10%).

**C — pricing:** **$0.075/M output is 2.4–5× BELOW published TPU v6e cost floor** (v6e $2.70/chip-hr on-demand, $1.22 3Y-CUD, cited; 284GB fp8 ≈ 16 chips → floor $0.179–$0.396/M output even 3Y-CUD saturated; break-even needs $0.512/chip-hr). ⇒ loss-leader or below-market capacity arbitrage; DO NOT load-bear on price persisting. Direct-vs-OR 0731 price identical ⇒ OR-fee hypothesis falsified ⇒ deliberate aggregator exclusion. **Our dollars: OI-base saves $6.48/wk vs DeepInfra = $84/qtr — immaterial; real value = quota-outage insurance (~700M eff-M/mo flat-lane tail ≈ $98k/mo if forced to PPQ).** Decision rule: deposit $50 only after (i) 14d sustained availability at listed price, (ii) status page/incident channel appears, (iii) price holds one monthly recheck, (iv) probe suite passes.

## Verdict: WATCH — fenced with re-open triggers

No deposit, no routing, no registry entry. DeepInfra stays cheap internal metered anchor; Chutes stays resale-conditional lane. Lane value = optionality.

**Re-open triggers (encoded in gate `openinference_fence`):**
1. Status page or public incident channel appears (reliability signal).
2. 14+ days sustained availability at the $0.03/$0.075/$0.007 price (probe: authless /v1/models price check — zero-cost).
3. Price drops further OR catalog adds a family we route (GLM/Kimi) — re-evaluate immediately.
4. Company-identity resolution (funding, team page, press) changes viability read.
5. Quota-outage event makes any cheap metered DS lane immediately valuable → run B's 24h probe suite ($5) + deposit bottom-of-range with auto-top-up.

**Follow-ups queued:** (a) traceroute/IP check api.openinference.ai vs GCP front-ends — confirms/kills TPU claim; (b) markian@ email IF we ever approach deposit; (c) OR stats need rendered fetch someday (static dead).

## Files

- Hunt report: `2026-09-06.md` · Consultant A artifacts: `consultantA/` (bundles, archives, extracted ToS ×3)
- Consultant C math: `2026-09-06-openinference-consultant-c-pricing.md`
- Gate fence: `openinference_fence` in provider-hunt-gate.py + 2 seeds
