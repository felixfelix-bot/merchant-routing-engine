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

---

## 8. Price-driven routing replaces hardcoded cascade

**Decision**: All routing decisions are made by minimizing `effective_price` across all providers. Hardcoded cascade (peak-hour Ollama-first, z.ai-primary, external-failover) is removed.

**Rationale**: Price captures all routing-relevant information (quota state, peak hours, health) in one comparable metric. A cascade requires N! code paths for N providers; price-based routing requires one comparison regardless of provider count.

**Trade-off**: Requires accurate price computation. Bad prices = bad routing. Mitigated by shadow-mode validation before deployment.

**SUPERSEDES ADR-2** (z.ai flat rate always primary).

---

## 9. Multiplier decomposition — peak/scarcity/health outside Kalman

**Decision**: The effective price formula decomposes into `base_rate` (Kalman-smoothed) × `peak_mult` (deterministic) × `scarcity_mult` (deterministic) × `health_mult` (deterministic). Only `base_rate` passes through a Kalman filter.

**Rationale**: Peak hours cause instantaneous step changes (1× → 3×). Kalman filters smooth transitions by design — putting peak inside the filter would lag the step by several update cycles. The operator explicitly wants immediate routing response to peak transitions, not gradual shifts. Scarcity and health have the same requirement.

**Trade-off**: `base_rate` alone doesn't capture all price dynamics. But `base_rate` genuinely IS smooth (subscription_fee / cumulative_tokens amortizes monotonically), making it the ideal Kalman target.

---

## 10. Two Kalman types — base-rate and consumption

**Decision**: Two distinct Kalman filter types, each with a narrow state vector. Base-Rate Kalman: `[amortized_cost, cost_velocity]`. Consumption Kalman: `[burn_rate, burn_acceleration]`. No combined filter.

**Rationale**: These estimate fundamentally different quantities. Base-rate is a financial amortization (deterministic formula smoothed for noise). Consumption is a physical burn rate (stochastic, bursty). Combining them creates a non-linear state space requiring an Extended Kalman Filter (harder to tune, more fragile). Separation allows independent tuning, independent failure, and clean extensibility.

**Trade-off**: More filter instances to manage. But each is simple, well-understood, and independently testable.

---

## 11. Consumer vs Merchant system separation

**Decision**: The module has two entry points. `RoutingOptimizer` (consumer: "which provider do I use?") and `ProfitOptimizer` (merchant: "what do I charge?"). These can be used by different entities.

**Rationale**: A merchant running a Routster node needs to set prices for customers. A customer (e.g., another Hermes agent) needs to choose between merchants. These are different optimization problems with different objectives (cost minimization vs profit maximization). Coupling them into one system conflates roles.

**Trade-off**: Slight code duplication (merchant uses consumer internally). But the abstraction is clean: merchant = consumer + demand_estimation + margin.

---

## 12. Profit = traffic × margin, not just margin

**Decision**: The merchant pricing optimizer maximizes `profit = traffic(price) × (price - cost)`, not just margin per request.

**Rationale**: Lowering price below competitors increases traffic volume, which can increase total profit despite lower per-unit margin. A demand Kalman estimates price elasticity from observed traffic patterns. The optimal price is where marginal revenue equals marginal cost, not where margin is maximized.

**Trade-off**: Requires demand data to estimate elasticity. Cold-start uses a default elasticity assumption. Over time, the Kalman converges to the true demand curve.

---

## 13. Dynamic amortized pricing — price decreases as usage increases

**Decision**: For flat-rate subscriptions (z.ai, Ollama Cloud), `base_rate = subscription_fee / cumulative_tokens_this_cycle × 1e6`. This means per-token cost DECREASES as more tokens are consumed.

**Rationale**: A flat €144/mo subscription that processes 1B tokens costs €0.144/M. The same subscription processing 5B tokens costs €0.029/M. The true cost per token is the amortized rate, not a static estimate. As usage increases, flat-rate providers become cheaper relative to per-token providers — the system naturally prefers them more as the billing cycle progresses.

**Trade-off**: At start of billing cycle (few tokens), flat-rate appears expensive. This may route to per-token providers early. Corrects naturally as data accumulates.

---

## 14. Shadow-mode incremental deployment

**Decision**: The price-first engine runs in shadow mode for 48h before any routing changes. It logs every decision alongside the live system's decision for comparison.

**Rationale**: The engine needs real production traffic to validate price estimates, Kalman convergence, and routing quality. Shadow mode collects this data without risking live operations. Validation criteria: >99% decision coverage, >70% agreement with live, disagreement cases must show lower effective price.

**Trade-off**: 48h delay before any improvement. But the alternative (deploying untested routing) risks production outages.

---

## 15. Three operation modes — consumer, merchant, arbiter

**Decision**: The module supports three distinct operation modes:
1. **Consumer** — operator owns API keys, routes between them to minimize cost.
2. **Merchant** — operator runs a Routster node, sells LLM access to maximize profit.
3. **Arbiter** — operator does BOTH simultaneously: buys from Routster network when cheaper than own keys, sells to Routster network when own keys are cheaper than competitors. Maximizes profit on sell side while minimizing cost on buy side.

**Rationale**: A Routster node operator isn't just a seller. They also consume LLM access — for their own Hermes agents, cron jobs, and workers. When network providers offer cheaper rates than their own keys, they should buy from the network. When their own keys are cheaper, they serve their own traffic internally and sell excess capacity to the network. The routing optimizer treats own keys and network providers as competing upstream candidates. The profit optimizer handles the sell side. Both run simultaneously.

**Trade-off**: Mode 3 (arbiter) requires scraping Routster network for competitor prices and maintaining provider reliability tracking. Near-term: use a whitelist/web-of-trust for trusted network providers. Long-term roadmap: automated quality verification (did provider deliver expected model quality?) and malicious-provider detection.

---

## 16. Provider reliability tracking via web of trust (near-term) with quality verification (roadmap)

**Decision**: Network providers (Routster) are initially filtered by a manually-maintained whitelist (web of trust). A reliability tracker records delivery outcomes per provider. Future roadmap: automated quality verification comparing delivered responses against expected model benchmarks, and malicious-provider detection (bait-and-switch: advertising one model but serving a cheaper one).

**Rationale**: When buying from the Routster network, price alone is insufficient. A provider advertising $0.02/M might be serving a cheaper, lower-quality model. Near-term, a whitelist of trusted npubs provides safety. Long-term, statistical quality verification (response latency distribution, token count distribution, content quality scoring) detects providers systematically underperforming.

**Trade-off**: Whitelist limits network liquidity early on. Quality verification requires calibration data and adds latency. Phased approach: whitelist first, verification later.

---

## 17. Retired 'ours' z.ai key removed from shadow provider set (S3b, 2026-08-17)

**Decision**: The 'ours' z.ai key is no longer a candidate in any shadow-routing provider set — removed from `src/shadow_hook.py` `_SEED_COSTS`/`_QUOTA_TOTALS` (MRE) and from the proxy's `_shadow_optimizer` tap registration (`~/.hermes/bot/zai_proxy.py`). Live key handling (`src/live_router.py`, `config/providers.yaml`) is untouched: the live path is health-gated by design and already excludes the disabled key via `.key_disabled_ours`.

**Rationale**: The 'ours' key was disabled 2026-08-15 and permanently retired (friend-only policy, per Felix — never re-add). Shadow taps are read-only and NOT health-gated, so the dead key kept winning price-first comparisons at its historical $0.068/M rate, producing ~4.8k disagreeing shadow decisions/24h that polluted divergence analytics with un-actionable 'ours' proposals. Legacy `live_provider="ours"` reports (historical rows) still log gracefully — all Kalman dict lookups are membership-guarded.

**Trade-off**: Shadow analytics lose the counterfactual "what if ours existed" data point. Accepted: the key can never come back, so the counterfactual is meaningless by policy.
