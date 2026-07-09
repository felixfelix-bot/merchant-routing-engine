# Design Decisions — Merchant Routing Engine

## 1. All requests through the proxy (no agent-level fallback)

**Decision**: `fallback_providers: null` in config.yaml. The proxy is the sole decision-maker.

**Rationale**: The proxy has the Kalman filter, cost matrix, model tier router, and key health tracker. When the agent bypasses the proxy, none of these optimizations apply. Brief proxy hiccups (1,152/hour during Incident 1) caused direct OpenRouter calls that burned the daily budget.

**Trade-off**: If the proxy itself crashes, the gateway can't fall back to anything. This is acceptable because the proxy is a simple Python HTTP server that rarely crashes (the crashes in Incident 1 were a code bug, now fixed).

---

## 2. z.ai flat rate is always primary

**Decision**: z.ai keys ("ours" + "friend") are tried before any paid provider.

**Rationale**: z.ai costs $155/month regardless of usage. PPQ and OpenRouter charge per-token. Every request to a paid provider costs real money. The proxy should exhaust all flat-rate options first.

**Trade-off**: When z.ai quota is exhausted, there's a delay while the proxy cycles through keys + backoff before reaching external providers. This delay (seconds, not minutes) is acceptable.

---

## 3. Empty content ≠ key exhausted

**Decision**: When z.ai returns `"content":""` (reasoning model), do NOT mark the key as exhausted. Only error responses (quota/auth) trigger exhaustion.

**Rationale**: The key still has quota — the model just didn't produce output for that specific request (reasoning consumed all tokens). Marking it exhausted would prevent future requests from using a perfectly good key.

**Trade-off**: The current request might need external failover if reasoning injection also fails. But the key stays available for the next request.

---

## 4. Reasoning injection

**Decision**: If `content` is empty but `reasoning_content` has data, inject reasoning as content.

**Rationale**: The reasoning IS the model's analysis of the request. It contains useful information. Injecting it means the tokens spent on reasoning aren't wasted, and the gateway gets text to work with. No external failover needed.

**Trade-off**: The reasoning text isn't a polished response — it's the model's internal thinking. The gateway receives raw analysis instead of a clean answer. This is better than an empty response or burning PPQ credits.

---

## 5. Dynamic cheapest-funded selection (no hardcoded order)

**Decision**: External failover sorts providers by cost and tries cheapest first. No hardcoded PPQ-before-OpenRouter order.

**Rationale**: PPQ and OpenRouter have the same model (deepseek-v4-flash) at nearly identical prices ($0.28 vs $0.27 per 1M). The choice should be based on which is funded and healthier, not a hardcoded preference.

**Trade-off**: Slightly more complex than a fixed order. But the cost matrix and funding tracker already exist — using them is cleaner than maintaining an arbitrary ordering.

---

## 6. Binary exponential backoff (not fixed delay)

**Decision**: Between key switches: 1-2s jitter. Full cycle: 2s, 4s, 8s, 16s, 32s, 60s cap. Kalman predictor overrides when available.

**Rationale**: Fixed delays either recover too slowly (wasting time when the rate limit clears quickly) or too fast (hammering the endpoint). Exponential backoff adapts: quick first retry, then increasingly patient. The 25-75% jitter prevents thundering herd when multiple workers retry simultaneously.

**Trade-off**: Under sustained rate limiting, retries can take up to 60s. The gateway's 30s HTTP timeout may trigger first. This is acceptable — the gateway retries the proxy, and the next request benefits from the key being marked exhausted (skipped immediately).

---

## 7. deepseek-v4-flash as fallback model (not glm-5.2)

**Decision**: External failover uses `deepseek/deepseek-v4-flash`, not `z-ai/glm-5.2`.

**Rationale**: deepseek-v4-flash costs $0.09/1M on both PPQ and OpenRouter. glm-5.2 costs $0.42/1M on OpenRouter and $1.00/1M on PPQ. For a fallback (when the primary provider is down), the cheaper model is better — quality difference is marginal (85 vs 92 on coding benchmarks).

**Trade-off**: deepseek-v4-flash is slightly lower quality than glm-5.2. For a fallback scenario (z.ai is down), this is acceptable — the alternative is no response at all.
