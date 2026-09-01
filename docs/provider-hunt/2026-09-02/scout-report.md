# Provider Hunt — 2026-09-02

**Seed:** "new flat-rate monthly LLM API subscription providers 2026"
**Queries run:** seed + "cheapest openai compatible API unlimited monthly subscription 2026" + "cheap LLM API provider glm kimi deepseek qwen flat subscription 2026" + "site:reddit.com flat rate unlimited LLM API provider 2026"
**Sources:** Brave Search, live curl probes to API endpoints, OpenRouter marketplace

## Candidates evaluated

### 1. Standard Compute (standardcompute.com)
- **Pricing:** $19-$249/mo flat, free tier with no card. Plans: $39, $249/mo visible.
- **API:** `https://api.stdcmpt.com/v1` — OpenAI-compatible, returns 200 unauthenticated on /v1/models.
- **Catalog:** 6 models, ALL Claude-family (claude-opus-5, claude-sonnet-5, claude-fable-5, etc). Smart routing proxy — you don't pick the model, it routes.
- **Overlap with our mix:** ZERO. No glm, kimi, deepseek, or qwen. Router picks the model for you (like our own router but for frontier labs). We can't pin glm-5.2.
- **Red flags:** "Unlimited" = fixed monthly compute budget, not infinite. No per-token meter but budget runs out. No model pinning (router decides). Has a `/fair-use` page.
- **Verdict:** PARK — zero catalog overlap, no model pinning, can't serve our routing mix.

### 2. Featherless (featherless.ai)
- **Pricing:** 3 tiers:
  - Chat: $25/mo, unlimited tokens, 32K context, 4 concurrent — **"Not for reselling, app/API traffic, background automation, or benchmarking"** — KILLS IT for our use case.
  - Developer: $50 credits/mo, per-token, 256K context, 1 agent env, credits roll over. **Designed for API/automation.**
  - Business: Custom pricing, unlimited concurrent.
- **API:** `https://api.featherless.ai/v1` — OpenAI-compatible (`/v1/chat/completions`, `/v1/models`). Auth via Bearer token. 21,910 models in catalog (unauthenticated listing works).
- **Catalog overlap:** EXCELLENT. Live API returns:
  - `zai-org/GLM-5.2` ($0.75/M in, $2.40/M out, 262K ctx)
  - `zai-org/GLM-5.3-Flash` ($0.15/M in, $0.50/M out, 262K ctx)
  - `zai-org/GLM-4.6` ($0.55/M in, $2.20/M out, 202K ctx)
  - `moonshotai/Kimi-K2-Instruct` ($0.60/M in, $2.50/M out, 32K ctx)
  - `moonshotai/Kimi-K2.5` ($0.77/M in, $3.50/M out, 262K ctx)
  - `moonshotai/Kimi-K2-Thinking` (available)
  - `deepseek-ai/DeepSeek-V4-Pro` ($1.60/M in, $3.20/M out, 262K ctx)
  - `deepseek-ai/DeepSeek-V3.2` ($0.30/M in, $0.45/M out, 131K ctx)
  - `Qwen/Qwen3-235B-A22B` ($0.46/M in, $1.82/M out, 32K ctx)
  - `Qwen/Qwen3-Coder-480B-A35B-Instruct` (available)
- **Effective cost at our volume (2.95B tokens/30d, 99% input):**
  - GLM-5.3-Flash: $0.15/M in → $50 credits buys ~333M input tokens. Effective $0.15/M. Barely beats ppq ($0.1391/M). Loses to ollama_cloud ($0.0083/M).
  - GLM-5.2: $0.75/M in → $50 credits buys ~66M tokens. Effective $0.76/M. Loses to ppq and ollama_cloud.
  - DeepSeek-V3.2: $0.30/M in → $50 credits buys ~166M tokens. Effective $0.30/M. Loses to ppq.
  - Kimi K2: $0.60/M in → $50 credits buys ~83M tokens. Effective $0.60/M. Loses to telnyx ($2.70/M in, but $13.50/M out — for 99% input, telnyx effective is ~$2.84/M, so Featherless is CHEAPER for Kimi).
- **Red flags:**
  1. `docs.featherless.ai` → connection failure (000). Dead docs subdomain. Docs live at `/docs/` on main domain (200, works).
  2. Homepage logo wall claims Ubisoft, Dropbox, Cisco, VMware, YouTube, Meta, Hugging Face as clients — these are Webflow template logos, not verified customers.
  3. "30,000+ models" claim — API returns 21,910. Inflated.
  4. Chat plan ($25/mo) has explicit fair-use clause banning API/automation use — but Developer tier ($50/mo) explicitly allows it.
  5. Per-token pricing on Developer tier — NOT flat-rate. It's a monthly credit stipend with per-token billing. Credits roll over (good).
  6. Status page returns 200 but is a single-page JS app (not independently verified uptime).
- **Verdict:** NEEDS-CONSULTANTS — strongest catalog overlap found (GLM-5.2, GLM-5.3-Flash, Kimi K2/K2.5, DeepSeek V4, Qwen3-235B all live), OpenAI-compatible API confirmed, Developer tier allows automation. But per-token pricing with $50/mo credit stipend doesn't beat our cheapest lanes at volume. The value proposition is CATALOG DIVERSITY (262K context on GLM-5.2 vs z.ai's limit, Kimi K2.5 262K) not price. Could serve as a T5 per-token fallback provider for models where we have thin coverage.

### 3. Morph (morphllm.com)
- **Pricing:** Not directly accessible (JS-rendered pricing page, HTTP 308 redirect on /pricing). Via OpenRouter: morph-v3-large $0.90/M in, $1.90/M out; morph-v3-fast $0.80/M in, $1.20/M out.
- **API:** `https://api.morphllm.com/v1` — OpenAI-compatible, requires API key. Returns auth error (not 404) — endpoint is live.
- **Catalog:** Homepage advertises GLM-5.3 (1M context), Kimi K3, Qwen, DeepSeek, MiniMax. But on OpenRouter, only sells `morph-v3-large` and `morph-v3-fast` (apply models for code edits, not chat models). Direct API may serve the full catalog — can't verify without signup.
- **Overlap:** Claims GLM-5.3 and Kimi K3 — but can't verify serving without a key. The OpenRouter listings are specialized code-edit models, not general chat.
- **Red flags:**
  1. Pricing page inaccessible (JS-only, /pricing returns 429 rate-limited).
  2. Cannot verify catalog or pricing without account signup. No unauthenticated /v1/models.
  3. OpenRouter models are narrow code-edit tools, not the broad catalog advertised on homepage.
  4. "$5K in API credits for startups" — indicates per-token credit model, not flat-rate.
- **Verdict:** PARK — can't verify catalog or pricing without signup. OpenRouter presence suggests per-token, not flat-rate. Homepage claims impressive catalog (GLM-5.3 1M ctx, Kimi K3) but unprovable from outside.

## Summary

| Provider | Type | Cheapest effective $/M | Catalog overlap | Verdict |
|----------|------|----------------------|-----------------|---------|
| Standard Compute | Flat $19-$249/mo | N/A (no model pinning) | Zero (Claude-only) | PARK |
| Featherless | Per-token, $50/mo credits | $0.15/M (GLM-5.3-Flash) | Excellent (GLM, Kimi, DeepSeek, Qwen) | NEEDS-CONSULTANTS |
| Morph | Per-token (credits) | $0.80/M (OpenRouter) | Claims GLM-5.3/Kimi K3, unverifiable | PARK |

## Recommendation

Featherless is the only candidate worth deeper vetting. It doesn't beat our cheapest lanes on price, but it offers 262K context on GLM-5.2 (vs z.ai's ~128K) and Kimi K2.5 at 262K — useful for long-context agent sessions. The $50/mo Developer tier is per-token, not flat, but credits roll over. The two-consultant vetting pass should evaluate: (1) whether Featherless Developer tier at $50/mo credits adds enough routing value as a T5 per-token fallback for long-context GLM/Kimi requests, and (2) whether their per-token rates are stable enough to Kalman-track alongside existing PPQ/Telnyx lanes.