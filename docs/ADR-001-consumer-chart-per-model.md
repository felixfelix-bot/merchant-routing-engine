# ADR-001: Consumer Chart Shows Price Per Model (Not Per Provider)

**Status:** ACCEPTED  
**Date:** 2026-08-07  
**Decision Maker:** Felix (operator)  

## Context

The merchant routing engine dashboard has a consumer chart at the bottom showing price/M over time. The question arose: should this chart show one line per **provider** (FRIEND, OLLAMA, PPQ, OPENROUTER) or one line per **model** (glm-5.2, kimi-k2.7-code, deepseek-v4-flash, etc.)?

## Decision

**The consumer chart MUST show one line per MODEL, not per provider.**

Each line represents the cheapest available price for that model across all providers at each point in time.

## Rationale

1. **Consumer perspective**: The consumer (Hermes proxy) decides which provider to use on a per-model basis. It doesn't care about providers — it cares about "what's the cheapest way to get glm-5.2 right now?"

2. **Model-level routing**: The routing engine picks the cheapest provider FOR A GIVEN MODEL. Multiple providers can serve the same model (e.g., glm-5.2 is available on both FRIEND and OLLAMA). Showing per-provider lines conflates models.

3. **Ollama subscription distinction**: Ollama is a flat $25/mo subscription (not per-token). Models served cheapest by Ollama should be styled with **dashed lines** to indicate subscription pricing. Models served by pay-per-use providers use **solid lines**.

4. **Colors per model**: Each model gets a distinct color from `MODEL_COLORS`:
   - glm-5.2: `#58a6ff` (blue)
   - glm-4.5-flash: `#bc8cff` (purple)
   - glm-4.5-air: `#d2a8ff` (light purple)
   - kimi-k2.7-code: `#39d2c0` (teal)
   - kimi-k3:cloud: `#7ee8d8` (light teal)
   - deepseek-v4-flash: `#f0883e` (orange)

## Implementation Details

- `renderConsumer()` in `display-deploy/index.html` iterates over all models (from `PROVIDER_MODELS`), not over `PROVIDERS`
- For each model, finds the cheapest provider using `getBaseRate()` + `ollama_model_rates` from CVM server
- Builds time series from `state.priceHist[cheapestProv]` mapped to model's base rate
- Ollama-sourced rates styled as `dash: 'dash'` in Plotly line config
- Annotation: "dashed = subscription (Ollama)"

## Constraints

- **NEVER revert** `renderConsumer()` to per-provider mode
- If refactoring, the chart MUST still show per-model lines
- The `PROVIDER_MODELS` mapping is the source of truth for which models each provider serves
- The `MODEL_COLORS` map is the source of truth for model colors

## Related

- CVM server `computeOllamaModelRates()` — per-model Ollama rates via duration-allocated hybrid
- `PROVIDER_MODELS` constant — maps providers to their models with base rates