# ADR-001: Price-First Routing

## Status

Proposed

## Date

2025-07-25

## Related

- `zai_proxy.py` lines 1061-1162 (`best_key()` — current Kalman-based key selector)
- `burn_predictor.py` lines 524-691 (`route_request()` — price-first routing, exists but unused for key selection)
- `model_matrix.json` (646 models with live pricing)
- ADR-002 (multi-Kalman separation), ADR-003 (deterministic peak multiplier)

## Context

The live proxy routes API requests through a hardcoded cascade:

1. Peak-hour check → try Ollama Cloud first (line 1439, hardcoded hour check)
2. `best_key()` → Kalman predicts ours-vs-friend exhaustion (binary, no price)
3. Ollama fallback → external failover (PPQ/OpenRouter)

Price is not the primary routing signal. Peak hours are a routing directive, not a cost input. A `route_request()` function that uses effective cost (base_rate × peak_multiplier × penalties) as the primary signal already exists but is only called for model tier downgrade, not provider/key selection.

This means the proxy misses cost-optimal decisions. Example: during peak hours when z.ai costs triple, Ollama Cloud at flat $0.069/M is cheaper — but the proxy only tries Ollama because of a hardcoded hour check, not because of a cost comparison.

## Decision

**Price is the primary routing signal.** All provider selection decisions are driven by effective cost minimization.

The routing optimizer (Layer 3) receives effective prices from all providers, filters out unavailable/low-quality ones, and picks the cheapest viable. No hardcoded cascade checks. No peak-hour routing directive. Peak hours are a cost multiplier, not an if-statement.

`best_key()` is replaced by `routing_optimizer.route()` which calls the price Kalman for each provider, applies deterministic multipliers, and sorts by effective cost.

## Invariants

1. Provider selection is ALWAYS determined by `argmin(effective_price)` among viable providers.
2. No hardcoded provider ordering (no "always try z.ai first").
3. Peak hours affect routing ONLY through the price multiplier, never through a direct routing check.
4. `route_request()` in `burn_predictor.py` (which already implements this logic) is the reference implementation for the routing optimizer.

## Consequences

### Positive
- Optimal cost decisions automatically (cheapest provider always chosen)
- No manual rule updates when adding providers — they compete on price
- Transparent routing reasons ("chose ollama at $0.069/M over friend at $0.246/M")
- Forward-looking: Kalman predicts price trajectory, enables proactive switching

### Costs
- Requires accurate pricing data for all providers (config maintenance)
- Cold-start period where Kalman has no data (mitigated by shadow mode validation)
- Must handle edge cases: provider with no quota data, pricing model changes
