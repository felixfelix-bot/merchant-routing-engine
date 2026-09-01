# Consultant A — Cost Economist Vetting: Featherless (featherless.ai)

**Date:** 2026-09-02 · **Role:** Cost economist, price-first flat router
**Provider:** Featherless Developer tier ($50/mo credits, per-token)
**Verdict:** **PARK** — ToS restriction + no price win + speculative context value.

---

## 1. Live re-verification (all curl, no auth, no signup)

### 1a. Catalog endpoint — CONFIRMED
`GET https://api.featherless.ai/v1/models` → HTTP 200, **21,911 models** (scout said 21,910 — off by one, immaterial). Unauthenticated listing works. Each model carries a `pricing` object with `input`/`output` in USD per 1M tokens.

### 1b. Load-bearing prices — CONFIRMED (with one correction)
| Model | Scout claim | Live API `pricing.input` | Live `pricing.output` | ctx | Verdict |
|---|---|---|---|---|---|
| zai-org/GLM-5.3-Flash | $0.15/M in | **$0.15** | $0.50 | 262K | ✓ |
| zai-org/GLM-5.2 | $0.75/M in | **$0.75** | $2.40 | 262K | ✓ |
| moonshotai/Kimi-K2.5 | $0.77/M in | **$0.77** | $3.50 | 262K | ✓ |
| deepseek-ai/DeepSeek-V3.2 | $0.30/M in | **$0.2995** | $0.45 | 131K | ✓ (scout rounded) |
| moonshotai/Kimi-K2-Instruct | $0.60/M in | **$0.60** | $2.50 | 32K | ✓ |
| deepseek-ai/DeepSeek-V4-Flash | (not flagged) | **$0.14** | $0.28 | 262K | NEW — cheapest V4-Flash seen |
| deepseek-ai/DeepSeek-V4-Pro | $1.60/M in | **$1.60** | $3.20 | 262K | ✓ |
| moonshotai/Kimi-K3 | (not flagged) | **$3.00** | $15.00 | 262K | NEW |
| Qwen/Qwen3-235B-A22B | $0.46/M in | **$0.455** | $1.82 | 32K | ✓ |

Scout's prices are **accurate**. The one thing the scout missed: **DeepSeek-V4-Flash at $0.14/M in / $0.28/M out** — the cheapest per-token V4-Flash rate in our provider set. Still loses to ollama_cloud (see §2).

### 1c. Docs pages — CONFIRMED
- `docs.featherless.ai` → **DNS resolution failure (dead)**. Scout correct.
- `featherless.ai/docs/` → HTTP 200, redirects to `/docs/overview` (live). Scout correct.
- `featherless.ai/llms.txt` → live, self-describes as "flat-rate pricing, concurrency-based subscriptions."

### 1d. Tier structure — SCOUT PARTIALLY WRONG (material)
The scout's "Developer $50 / Chat $25" framing is **stale/incomplete**. Live `/docs/plans` + `/docs/request-pricing-and-credits` + `/terms` show:

| Plan | Price | Model | Billing | Context | Concurrency |
|---|---|---|---|---|---|
| **Chat** | $25/mo | concurrent-unit | unlimited requests | 32K | 4 units |
| **Developer** | **$50+/unit/mo** | credit-based | prepaid credits, per-token | 256K | 100 units |
| **Scale** | $75+/mo | business | custom | — | arbitrary |
| Dedicated GPU | contact | — | reserved capacity | — | — |

Key confirmations:
- **Credits do NOT expire** — "Credits do not expire", "Unused credits stay on your organization and do not expire." Rollover confirmed.
- **Per-token** — "Pay per successful request based on model price and token usage." Confirmed.
- **Developer is "designed for API-driven applications … whether agent fleets or other AI applications"** (marketing copy).

### 1e. THE KILLER — ToS §4 (resale/automation restriction)
`/terms` (updated June 10 2024), verbatim:

> "Individual plans are for interactive use or proto-typing and experimentation by the purchaser. **Persons making use of individual plans for other purposes will have their subscription terminated and no refund will be provided.** Scale plans are not subject to said limits and may be used in arbitrary applications, **including inference resale**."

This is the **ROUTSTR lesson (z.ai ToS §4) in a sharper form**. The Developer tier is an *individual* (self-serve) plan. Our use case — a 22-worker automated agent fleet, ~30 cron jobs, background automation — is unambiguously "other purposes," not "interactive use by the purchaser." The marketing copy ("designed for agent fleets") directly contradicts the legal ToS ("individual plans … interactive use or prototyping only"). The ToS is the contract; the marketing is not. Wiring Featherless Developer tier for our fleet risks **account termination with no refund**, and the $50/mo prepaid balance would be forfeited on termination.

This alone is sufficient to PARK. The remaining sections quantify that there is no economic upside worth that legal risk.

---

## 2. Routing win analysis — would Featherless EVER win a routing decision?

Our router picks the cheapest healthy lane per call. Effective rates at our measured volume:

| Lane | Effective $/M (input) | Notes |
|---|---|---|
| ollama_cloud | **$0.0083** | grandfathered $25/mo flat, 2.95B tok/30d — the bar |
| z.ai friend | ~$0 (free, quota-windowed) | 5h/weekly windows |
| ppq | $0.1391 | pay-per-use, fallback |
| telnyx kimi-k3 | ~$2.74 blended | $2.70 in / $13.50 out, 99.6% input |
| neuralwatt | kWh-based, ~$0.19/M on V4-Flash | variable |

Per model family:

| Family | Featherless $/M in | Cheapest incumbent | Wins? |
|---|---|---|---|
| GLM-5.3-Flash | $0.15 | ollama_cloud $0.0083 | **NO** (18× worse) |
| GLM-5.3-Flash | $0.15 | ppq $0.1391 | barely (7%) — but ppq is fallback, z.ai friend is free |
| GLM-5.2 | $0.75 | ollama_cloud $0.0083 | **NO** (90× worse) |
| DeepSeek-V4-Flash | $0.14 | ollama_cloud $0.0155 | **NO** (9× worse) |
| DeepSeek-V3.2 | $0.30 | ppq $0.1391 | **NO** |
| Kimi-K2.5 | $0.77 | telnyx $2.74 | **YES on paper** (3.5× cheaper) |
| Qwen3-235B | $0.455 | (thin coverage) | marginal |

**The only theoretical win is Kimi vs telnyx.** But §3 shows telnyx Kimi traffic is a dead lane — a one-time Aug 13–14 burst, ~0 since Aug 25. There is no ongoing telnyx Kimi overflow to displace.

**The GLM-5.3-Flash "barely beats ppq" case never fires in practice:** ppq is itself a fallback lane (only 623 calls / 22.9M tokens all month), and z.ai friend (free) sits above it. For Featherless to win a GLM-5.3-Flash call, ollama_cloud AND z.ai friend AND ppq would all have to be simultaneously unhealthy — a triple-failure that the $50/mo pool (≈333M GLM-5.3-Flash input tokens) would survive for roughly one burst before going to zero.

**Conclusion: Featherless wins zero routing decisions under normal operation, and only wins under a rare triple-failure that would exhaust its credit pool immediately.**

---

## 3. Empirical demand check (zai_usage.db, read-only)

### 3a. Per-request token distribution (input+output, n=124,285)
| Percentile | total_tokens | input_tokens | output_tokens |
|---|---|---|---|
| p50 | 53,397 | 52,632 | 193 |
| p95 | 127,088 | 126,379 | 2,411 |
| p99 | 181,688 | 180,614 | 7,017 |
| p99.9 | 251,055 | — | — |
| max | 338,004 | 337,069 | 113,163 |

### 3b. Context-length tail (the 262K value proposition)
| Threshold | Requests | % of all |
|---|---|---|
| >128K input | 5,109 | 4.11% |
| >200K input | 657 | 0.53% |
| >256K input | 102 | **0.08%** |

**The 262K-context value proposition is speculative.** Only 0.08% of requests exceed 256K context. The p99 (180K) sits between 128K and 256K, but those 4.11% of >128K requests are *already being served* by ollama_cloud (1,946), z.ai ours (1,029), neuralwatt (580), friend (483), etc. There is **no context-length gap** that Featherless would fill — our existing lanes already carry the long-context tail.

### 3c. Telnyx Kimi traffic — is the "Kimi win" real?
Telnyx daily token volume (Kimi-K3 + Kimi-K2.5 + glm-5.2):
```
2026-08-13  189.9M   ← burst
2026-08-14  255.6M   ← burst (the "one bad night" incident)
2026-08-15    3.1M
2026-08-16..24  ~1–23M/day (trickle)
2026-08-25..09-01  ~0
```
**90% of all telnyx traffic (444M of 490M tokens) happened in a 2-day burst on Aug 13–14.** Since Aug 25, telnyx has served effectively zero tokens. Kimi-K3 is now served by ollama_cloud (490 req / 19.9M tok) and ollama_cloud_2.

**The Kimi win is moot.** There is no ongoing telnyx Kimi overflow to displace. The 250M tokens of Kimi-K3 that went through telnyx was a one-time incident, not recurring demand. Even if it recurred, the $50/mo Featherless pool buys only ~64M Kimi-K2.5 input tokens (at $0.77/M) — it could not absorb a 250M-token burst anyway.

### 3d. Telnyx real cost (for the record)
- daily_spend (L1 real money): telnyx = **$1,994.63** over 490.3M tokens = **$4.07/M blended** (burst-inflated; the Aug 14 day alone was $1,524 on 431M tokens).
- Kimi-K3 effective at 99.6% input: ~$2.74/M. Featherless Kimi-K2.5 at same mix: ~$0.78/M → 3.5× cheaper *if* the demand existed. It doesn't.

---

## 4. Credit-pool semantics — how the flat router would price it

Featherless Developer is a **prepaid credit pool with monthly top-up + rollover**. It is NOT a flat "included" tier like ollama_cloud_3 (T4, $0.001 floor). Every token burns real, non-refundable credit.

**Correct tier assignment: T5 (per-token), with a balance-depletion penalty** — the same shape as NeuralWatt (T2 balance), not the T4 "included" floor.

- **Base rate** = the per-token rate (e.g., $0.15/M GLM-5.3-Flash, $0.14/M V4-Flash, $0.77/M Kimi-K2.5), seeded from the live catalog `pricing` field, then Kalman-measured from balance depletion (`Δbalance / tokens`) — the PPQ/NeuralWatt pattern.
- **Depletion penalty** = linear in depleted fraction, capped ~2.0× at zero balance (mirror `NW_MAX_DEPLETION_PENALTY`). The $50/mo pool is finite; as it drains toward zero, the lane must price itself out of the market *before* it hard-blocks ("balance reaches zero → API calls blocked").
- **No $0.001 floor.** Unlike ollama_cloud_3, there is no "included" headroom — the $50 is a real consumable, so a $0.001 floor would misprice it as free and drain the pool in one burst.
- **Rollover changes the depletion curve, not the tier.** Because credits don't expire, the "use-it-or-lose-it" decay that drives T1 (z.ai) pricing does NOT apply. The pool is a true balance, not a quota window. This makes it *simpler* than z.ai but *more expensive* than ollama_cloud_3.

**If wired at all:** T5, seed $0.15/M (GLM-5.3-Flash) / $0.14/M (V4-Flash) / $0.77/M (Kimi-K2.5), depletion penalty onset ~80%, cap 2.0×. It would sit at the *bottom* of the T5 sort, below ppq ($0.1391) only for V4-Flash, and above ollama_cloud/z.ai always.

---

## 5. Verdict

### **PARK.**

**The number that kills it (primary):** ToS §4 — "Individual plans are for interactive use or proto-typing and experimentation by the purchaser. Persons making use of individual plans for other purposes will have their subscription terminated and no refund will be provided." Our automated 22-worker fleet is "other purposes." The Developer tier is an individual plan; only Scale ($75+/mo, custom) permits "arbitrary applications, including inference resale." This is the ROUTSTR/z.ai-§4 lesson, and it's *worse* here because the restriction is on automation generally, not just resale, and termination forfeits the prepaid balance.

**The number that kills it (economic):** Featherless wins **zero** routing decisions under normal operation. Every model family loses to ollama_cloud ($0.0083/M) by 9–90×. The only theoretical win (Kimi vs telnyx, 3.5×) is moot — telnyx Kimi traffic is a dead lane (~0 tokens since Aug 25; 90% of its lifetime volume was a 2-day Aug 13–14 burst).

**The number that kills it (context):** 0.08% of requests exceed 256K context. The 262K value proposition is speculative; our existing lanes already serve the 4.11% of >128K requests.

### What would change my mind (CONDITIONAL → WIRE):
1. **ToS clarification** — written confirmation from Featherless that the Developer tier permits background/agent-fleet automation (not just "interactive use"). Without this, the legal risk is disqualifying regardless of price.
2. **A real, recurring telnyx-Kimi overflow** — if telnyx Kimi traffic returned as steady demand (not a one-off burst), Featherless Kimi-K2.5 at $0.78/M effective vs telnyx $2.74/M would be a genuine 3.5× win worth ~$1.96/M on that lane.
3. **A cheaper-than-ollama_cloud rate** — impossible at current pricing; would require a flat/included tier, which Featherless does not offer below Scale.

### Expected monthly savings if wired anyway (for completeness): **~$0.**
The only displaceable spend is telnyx Kimi (~$0/mo ongoing) and ppq fallback (~$2.05/mo, 22.9M tokens). Featherless would displace neither at its price points, and the $50/mo credit stipend would be a net *cost*, not a saving.

---

## Appendix — key numbers at a glance
- Catalog: 21,911 models, unauthenticated listing confirmed.
- GLM-5.3-Flash $0.15/M in (vs ollama_cloud $0.0083 → 18× worse).
- DeepSeek-V4-Flash $0.14/M in (vs ollama_cloud $0.0155 → 9× worse).
- Kimi-K2.5 $0.77/M in (vs telnyx $2.74 → 3.5× cheaper, but telnyx lane is dead).
- p99 request = 181,688 tokens; >256K = 0.08% of requests.
- Telnyx lifetime: $1,994.63 / 490M tokens, 90% in a 2-day burst; ~0 since Aug 25.
- ToS §4: individual plans = interactive/prototyping only; automation → termination, no refund.
