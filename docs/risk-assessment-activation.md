# Risk Assessment: Production Activation

> **Date:** 2026-08-05
> **Verdict:** GO (with conditions)
> **Assessor:** Manager profile (consultant timed out, verified manually)

## Test Suite: PASS
- 1740 tests, 0 skipped, 0 failures
- Full suite covers pressure formulas, per-model lookup, cold-start, integration paths

## Kill Switches: ALL OFF (default)
- OLLAMA_QUOTA_PRESSURE_ENABLED=false
- ZAI_QUOTA_PRESSURE_ENABLED=false
- PPQ_QUOTA_PRESSURE_ENABLED=false
- DEEPINFRA_QUOTA_PRESSURE_ENABLED=false
- OPENROUTER_QUOTA_PRESSURE_ENABLED=false
- PER_MODEL_PRICING_ENABLED=false

## PM-T3 Hot Path: SAFE
- Line 1283: `if model and _PER_MODEL_PRICING_ENABLED:` gates the new path
- When off: byte-for-byte identical to legacy (verified by code reading)
- Fallback chain: per-model rate → _default → $1.0/M (conservative, never optimistic)
- Unknown model → $1.0/M floor (prevents kimi-k3 blindspot on cold start)
- Model not served by provider → healthy=False (unreachable)

## Interaction Risk: LOW
Both systems multiply base_rate independently:
  effective = base_rate × pressure_factor × other_multipliers
- Per-model sets base_rate correctly (kimi-k3=$7.53 vs glm-5.2=$0.014)
- Pressure multiplies on top (asymptote=1.5)
- Example: kimi-k3 at 90% quota = $7.53 × 2.5 = $18.8/M — expensive but CORRECT
- No compounding bug: each system operates on a different dimension

## Rollback Speed: ~5 seconds
- Set env var to false + restart proxy
- In-flight requests complete on old code path

## Recommended Activation Order

### Step 1: Per-Model Pricing (safest first)
- Rationale: fixes wrong routing without changing any routing logic — just makes prices accurate
- Enable: `PER_MODEL_PRICING_ENABLED=true`
- Monitor: 30 min — check that glm-5.2 routing unchanged, kimi-k3 routing changes
- Risk if wrong: optimizer sees different prices, might route differently — but it's routing to MORE accurate prices, so even "wrong" is better than current

### Step 2: Ollama Pressure (24h)
- Rationale: Ollama has extra-usage safety net (won't 429, just costs more)
- Enable: `OLLAMA_QUOTA_PRESSURE_ENABLED=true`
- Monitor: 24h — check no 429 increase, no unexpected routing changes at low load
- Risk if wrong: Ollama over/under-used (correctable, not catastrophic)

### Step 3: z.ai Pressure (24h each key)
- Rationale: most critical endpoints (primary traffic)
- Enable: `ZAI_QUOTA_PRESSURE_ENABLED=true`
- Monitor: 24h — check 429 rate stable, breaker trips at 100% correctly
- Risk if wrong: z.ai 429s if breaker doesn't trip correctly

### Step 4: Paid endpoints (24h each)
- PPQ, DeepInfra, OpenRouter — enable individually
- Monitor: 24h each — check balance tracking works, pressure ramps correctly

## ABORT CONDITIONS
- Any 429 rate increase >2x baseline → rollback immediately
- Any routing loop or deadlock → rollback immediately
- Any cost anomaly >20% of expected → rollback immediately
- Any test that was passing starts failing → investigate before proceeding
