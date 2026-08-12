# PLAN: Telnyx Inference — Kimi K3 Provider Integration

**Date:** 2026-08-12
**Author:** Manager (Felix)
**Status:** DRAFT — awaiting consultant review

## 1. Objective

Add Telnyx as a new per-token external provider in the merchant routing engine
and z.ai proxy, enabling Kimi K3 inference via Telnyx's OpenAI-compatible API.
This gives us a second Kimi K3 source (alongside Ollama Cloud) and makes Kimi K3
available to Routstr consumers.

## 2. Verified Provider Facts

**Source:** telnyx.com/products/inference + telnyx.com/pricing/inference-api
( scraped 2026-08-12 )

### API Endpoint
- Base URL: `https://api.telnyx.com/v2/ai`
- Chat completions: `POST /v2/ai/chat/completions`
- OpenAI-compatible (standard OpenAI SDK works with `base_url` override)
- Auth: `Authorization: Bearer $TELNYX_API_KEY`

### Models Available
| Model ID | Label | Languages |
|---------|-------|-----------|
| `moonshotai/Kimi-K3` | Kimi K3 | en |
| `moonshotai/Kimi-K2.5` | Kimi K2.5 | multilingual |
| `zai-org/GLM-5.2` | GLM 5.2 | en |
| `MiniMaxAI/MiniMax-M3-MXFP8` | MiniMax M3 MXFP8 | multilingual |
| `Qwen/Qwen3-235B-A22B` | Qwen3 235B | en |

### Pricing (per 1M tokens, USD)
| Model | Input | Cached Input | Output |
|-------|-------|-------------|--------|
| **Kimi K3** | $2.700 | $0.270 | $13.500 |
| Kimi K2.6 | $0.665 | $0.080 | $4.000 |
| GLM-5.2 | $1.000 | $0.200 | $4.000 |
| MiniMax M3 | $0.270 | $0.080 | $1.100 |

**Prompt caching**: 10x reduction on cached input tokens. Critical for
cost-efficient multi-turn conversations.

**Starting price**: $0.21/1M tokens (cheapest model). Pay-as-you-go, no minimums.
Free trial credits available on signup.

### Key Characteristics
- Per-token pricing (NOT subscription)
- No quota windows (credit balance depletes)
- Global GPU deployment, sub-100ms latency claimed
- Regional AI: inference traffic stays in-region for data compliance

## 3. Current State

### Proxy Architecture (zai_proxy.py)
- Providers: z.ai (ours+friend), ollama_cloud, ppq, openrouter, deepinfra
- Kimi K3 currently Ollama-exclusive (`_OLLAMA_EXCLUSIVE_MODELS` in live_router.py)
- Ollama Kimi K3 rate: ~$7.53/M effective (with quota pressure)
- Model mapping: `get_model("ppq", "coding")` already returns `kimi-k3`

### Merchant Routing Engine
- `providers.yaml`: 6 providers configured
- `model_mapping.py`: maps (provider, task_type) → model_name
- `live_router.py`: handles failover, exclusive model short-circuit
- `real_price_tracker.py`: per-model rate tracking from actual billing
- `pricing_engine.py`: quota pressure curves (exponential, per-endpoint)
- Balance collectors: cron-based, write to `provider_balances` in api_burn.db

### Cost Comparison
| Provider | Kimi K3 Rate | Notes |
|----------|-------------|-------|
| Ollama Cloud | $0.0209/M (blended) | Always extra-usage, metered from prepaid |
| Ollama Cloud (effective) | $7.53/M | With full quota pressure |
| **Telnyx** | **$2.70/M input, $13.50/M output** | Per-token, no quota windows |
| OpenRouter | ~$2-3/M (est) | Per-token, balance-based |

Telnyx is MORE EXPENSIVE than Ollama's base rate but:
1. No quota windows — always available, no 5h/weekly limits
2. Prompt caching ($0.27/M cached input) — 10x cheaper for repeated context
3. Different failure domain — independent of Ollama's GPU availability
4. Makes Kimi K3 available to Routstr consumers as a per-token provider

## 4. Implementation Plan

### Phase 1: Provider Registration & Config

**Task 1.1: Add Telnyx to providers.yaml**
- File: `~/merchant-routing-engine/config/providers.yaml`
- Add under `external:`:
  ```yaml
  telnyx:
    base_url: "https://api.telnyx.com/v2/ai"
    key_env: "TELNYX_API_KEY"
    starting_balance_env: "TELNYX_STARTING_BALANCE"
    pricing_model: per_token
    quota_pressure:
      onset: 0.80
      asymptote: 1.5
      hard_limit: true
    models:
      kimi-k3:
        model_id: "moonshotai/Kimi-K3"
        cost_per_1m_input: 2.70
        cost_per_1m_cached_input: 0.27
        cost_per_1m_output: 13.50
      kimi-k2.5:
        model_id: "moonshotai/Kimi-K2.5"
        cost_per_1m_input: 0.665
        cost_per_1m_output: 4.00
      glm-5.2:
        model_id: "zai-org/GLM-5.2"
        cost_per_1m_input: 1.00
        cost_per_1m_output: 4.00
      minimax-m3:
        model_id: "MiniMaxAI/MiniMax-M3-MXFP8"
        cost_per_1m_input: 0.27
        cost_per_1m_output: 1.10
  ```
- Gate: YAML validates, no existing provider overwritten

**Task 1.2: Add Telnyx to model_mapping.py**
- File: `~/merchant-routing-engine/src/model_mapping.py`
- Add `telnyx` to `_KNOWN_PROVIDERS` frozenset
- Add model map entries:
  ```python
  "telnyx": {
      "coding": "kimi-k3",
      "reasoning": "kimi-k3",
      "simple": "kimi-k2.5",
  }
  ```
- Gate: `get_model("telnyx", "coding")` returns `"kimi-k3"` (test)

**Task 1.3: Add Telnyx to provider_names.py**
- File: `~/merchant-routing-engine/src/provider_names.py`
- Add `"telnyx"` to canonical provider names
- Add any aliases (none expected — new provider)
- Gate: `normalize_provider_name("telnyx")` returns `"telnyx"` (test)

### Phase 2: Proxy Integration

**Task 2.1: Add Telnyx API key loading to zai_proxy.py**
- File: `~/.hermes/bot/zai_proxy.py`
- Add `TELNYX_API_KEY` loading alongside existing PPQ/OPENROUTER/DEEPINFRA
- Add `TELNYX_STARTING_BALANCE` loading
- Gate: Key loads from .env, print on startup like other providers

**Task 2.2: Add Telnyx to failover chain**
- File: `~/.hermes/bot/zai_proxy.py`
- Add Telnyx as a failover provider after DeepInfra
- Request formatting: standard OpenAI chat completions (same as DeepInfra/OpenRouter)
- Model name mapping: `kimi-k3` → `moonshotai/Kimi-K3` in request body
- Gate: Failover chain includes telnyx, request body has correct model ID

**Task 2.3: Add Telnyx to shadow optimizer**
- File: `~/.hermes/bot/zai_proxy.py`
- Add `_shadow_optimizer.add_provider("telnyx", ...)` with per-model rates
- Seed rates: $2.70/M input, $13.50/M output (blended ~$5.00/M for typical 3:1 input:output)
- Gate: Shadow optimizer logs telnyx decisions

**Task 2.4: Remove kimi-k3 from Ollama-exclusive list (CONDITIONAL)**
- File: `~/merchant-routing-engine/src/live_router.py`
- CRITICAL: Only do this AFTER Phase 3 validates Telnyx actually serves kimi-k3
- Remove `"kimi-k3:cloud"` from `_OLLAMA_EXCLUSIVE_MODELS`
- This allows the router to failover kimi-k3 requests to Telnyx when Ollama is exhausted
- Gate: kimi-k3 requests can route to telnyx (integration test)

### Phase 3: Balance Tracking & Cost Capture

**Task 3.1: Create Telnyx balance collector**
- File: `~/merchant-routing-engine/src/telnyx_balance_collector.py`
- Pattern: same as `ppq_balance_collector.py` and `openrouter_balance_collector.py`
- Check if Telnyx has a balance API endpoint (TODO: verify during implementation)
- If no balance API: self-track from proxy logs (like DeepInfra pattern, pitfall #47)
- Write to `provider_balances` table in api_burn.db
- Gate: Collector runs, writes valid balance row

**Task 3.2: Wire Telnyx balance bridge into proxy**
- File: `~/.hermes/bot/zai_proxy.py`
- Add `_telnyx_quota_entry_fn` following the PPQ/OpenRouter pattern
- Import from `src.telnyx_balance_collector`
- Gate: `quota_state['telnyx']` reads real balance (or self-tracked)

**Task 3.3: Wire cost extraction from Telnyx responses**
- File: `~/.hermes/bot/zai_proxy.py`
- Telnyx may return usage/cost in API response (check during implementation)
- If yes: extract and store in `cost_usd` column of `api_calls` table
- If no: compute from token count × published rate (mark as `rate_derived`)
- Gate: `cost_usd` populated for telnyx calls in api_calls table

### Phase 4: Pricing Engine Integration

**Task 4.1: Add Telnyx quota pressure curve**
- File: `~/merchant-routing-engine/src/pricing_engine.py`
- Add Telnyx to the universal endpoint pressure system
- Parameters: onset=0.80, asymptote=1.5, hard_limit=true (credit-based, no extra usage)
- Balance tracking: `u = 1 - (remaining / starting_balance)` (same as PPQ)
- Gate: `quota_pressure_factor(0.9, provider='telnyx')` returns correct multiplier (test)

**Task 4.2: Add Telnyx per-model rates to real_price_tracker**
- File: `~/merchant-routing-engine/src/real_price_tracker.py`
- Add Telnyx model rates to `LAST_RESORT_RATES`:
  ```python
  "telnyx": {
      "kimi-k3": {"input": 2.70, "output": 13.50, "cached_input": 0.27},
      "kimi-k2.5": {"input": 0.665, "output": 4.00},
      "glm-5.2": {"input": 1.00, "output": 4.00},
      "minimax-m3": {"input": 0.27, "output": 1.10},
  }
  ```
- Gate: `get_rate_with_fallback("telnyx", "kimi-k3")` returns $2.70 (test)

**Task 4.3: Add Telnyx to LiveRouter failover**
- File: `~/merchant-routing-engine/src/live_router.py`
- Add `"telnyx"` to the provider list in `_do_select_failover`
- Wire per-model rate resolution for telnyx
- Ensure kimi-k3 requests can failover to telnyx
- Gate: LiveRouter includes telnyx in failover candidates (test)

### Phase 5: Systemd Kill Switch

**Task 5.1: Create pricing-phase-e.conf**
- File: `~/.config/systemd/user/zai-proxy.service.d/pricing-phase-e.conf`
- Content:
  ```ini
  [Service]
  Environment=TELNYX_ENABLED=true
  Environment=TELNYX_QUOTA_PRESSURE_ENABLED=true
  ```
- Gate: `systemctl --user daemon-reload && systemctl --user restart zai-proxy` succeeds
- Kill switch: `rm pricing-phase-e.conf && daemon-reload && restart`

### Phase 6: Testing & Validation

**Task 6.1: Unit tests for Telnyx provider**
- Tests for: provider name normalization, model mapping, pricing engine integration
- Gate: All new tests pass, existing 1740+ tests still pass

**Task 6.2: Integration test — live API call**
- Make a real API call to Telnyx with kimi-k3
- Verify response format, token counts, latency
- Gate: Real response received, valid content, token count > 0

**Task 6.3: Shadow mode validation (48h)**
- Telnyx runs in shadow mode alongside live routing
- Every kimi-k3 request also evaluated for telnyx routing
- Gate: >99% decision coverage, <5% divergence at low load

## 5. Account Setup Requirements

Before implementation can begin:
1. Sign up at telnyx.com
2. Get API key (TELNYX_API_KEY)
3. Add starting balance (suggest $10 for testing)
4. Record starting balance for self-tracking (TELNYX_STARTING_BALANCE)
5. Add both to `~/.hermes/bot/.env`

## 6. Risk Assessment

### Low Risk
- Adding a new provider is additive — no changes to existing routing
- Kill switch via systemd drop-in (instant rollback)
- Shadow mode validates before going live

### Medium Risk
- Telnyx API compatibility: claimed OpenAI-compatible but edge cases may exist
  - Mitigation: integration test in Phase 6 before shadow mode
- Balance tracking: no known balance API (may need self-tracking like DeepInfra)
  - Mitigation: self-track from proxy logs, same pattern as DeepInfra
- Kimi K3 model ID: Telnyx uses `moonshotai/Kimi-K3`, not `kimi-k3`
  - Mitigation: model_mapping.py handles the translation

### Not a Risk
- Cost: Telnyx is per-token, no subscription. Zero cost when not used.
- Quota: No quota windows — credit balance only. Simpler than z.ai/ollama.

## 7. Worker Assignment

| Task | Worker Profile | Model | Timeout |
|------|----------------|-------|---------|
| 1.1 providers.yaml | worker-merchant | glm-5.2 | 120s |
| 1.2 model_mapping.py | worker-merchant | glm-5.2 | 120s |
| 1.3 provider_names.py | worker-merchant | glm-4.5-flash | 90s |
| 2.1 key loading | worker-merchant | glm-4.5-flash | 90s |
| 2.2 failover chain | worker-merchant | glm-5.2 | 180s |
| 2.3 shadow optimizer | worker-merchant | glm-5.2 | 120s |
| 2.4 exclusive list | worker-merchant | glm-5.2 | 90s |
| 3.1 balance collector | worker-merchant | glm-5.2 | 180s |
| 3.2 balance bridge | worker-merchant | glm-5.2 | 120s |
| 3.3 cost extraction | worker-merchant | glm-5.2 | 180s |
| 4.1 quota pressure | worker-merchant | glm-5.2 | 180s |
| 4.2 price tracker | worker-merchant | glm-4.5-flash | 120s |
| 4.3 LiveRouter | worker-merchant | glm-5.2 | 180s |
| 5.1 systemd conf | worker-merchant | glm-4.5-flash | 60s |
| 6.1 unit tests | worker-merchant | glm-5.2 | 180s |
| 6.2 integration test | worker-merchant | glm-5.2 | 180s |
| 6.3 shadow mode | (manager manual) | — | 48h |

**Dependencies:**
- Phase 1 (1.1-1.3) → Phase 2 (2.1-2.3) → Phase 3 (3.1-3.3) → Phase 4 (4.1-4.3)
- Task 2.4 depends on Phase 6.2 (integration test confirming Telnyx serves kimi-k3)
- Phase 5 (systemd) after Phase 4 confirmed working
- Phase 6.3 (shadow mode) after all phases deployed

**Parallelizable:** Tasks within each phase can be parallelized if they touch different files.

## 8. Consultant Review Fixes (Pre-Schedule)

Issues found by manager review against actual code (2026-08-12):

### FIX 1: Model name translation in zai_proxy.py
Add to `_PROVIDER_MODEL_NAMES` dict:
```python
"telnyx": {
    "kimi-k3":     "moonshotai/Kimi-K3",
    "kimi-k2.5":   "moonshotai/Kimi-K2.5",
    "glm-5.2":     "zai-org/GLM-5.2",
    "minimax-m3":  "MiniMaxAI/MiniMax-M3-MXFP8",
    "qwen3-235b":  "Qwen/Qwen3-235B-A22B",
},
```
→ Added to Task 2.2 scope.

### FIX 2: Provider priority for failover
Add telnyx to `_PROVIDER_PRIORITY`:
```python
_PROVIDER_PRIORITY = {"deepinfra": 0, "ppq": 1, "openrouter": 2, "telnyx": 3}
```
Telnyx is LAST resort for general tasks (expensive). For kimi-k3 specifically,
it's the ONLY alternative to Ollama — handled by exclusive model removal (Task 2.4).
→ Added to Task 2.2 scope.

### FIX 3: EXTERNAL_PROVIDERS dict entry
```python
"telnyx": {
    "base_url": "https://api.telnyx.com/v2/ai",
    "key": _EXTERNAL_KEYS.get("telnyx", ""),
},
```
→ Added to Task 2.2 scope.

### FIX 4: kimi-k3 model alias resolution
The exclusive list has `kimi-k3:cloud` (Ollama naming). Telnyx uses
`moonshotai/Kimi-K3`. The proxy must:
1. Recognize `kimi-k3` as a request that can go to EITHER ollama OR telnyx
2. When routing to ollama: use `kimi-k3:cloud`
3. When routing to telnyx: use `moonshotai/Kimi-K3`
This is handled by `_PROVIDER_MODEL_NAMES` (FIX 1) — the model map translates
the canonical name to the provider-specific ID.
→ No new task, but Task 2.4 must verify the alias chain works end-to-end.

### FIX 5: BALANCE_DELTA_PROVIDERS
Add `"telnyx"` to `BALANCE_DELTA_PROVIDERS` in `real_price_tracker.py`:
```python
BALANCE_DELTA_PROVIDERS = ("ppq", "openrouter", "telnyx")
```
→ Added to Task 4.2 scope.

### FIX 6: No balance API — self-track from cost_usd
Same as DeepInfra (pitfall #47). `telnyx_balance_collector.py` must:
1. Query `SELECT SUM(cost_usd) FROM api_calls WHERE key_name='telnyx'`
2. `remaining = TELNYX_STARTING_BALANCE - sum_spent`
3. `usage_fraction = 1 - (remaining / TELNYX_STARTING_BALANCE)`
4. Write to `provider_balances` table
→ Task 3.1 already covers this, but must follow DeepInfra pattern exactly.

### FIX 7: Prompt caching cost extraction (NEW)
Telnyx returns `usage.prompt_tokens_details.cached_tokens` (OpenAI-compatible).
Cost = `(input_tokens - cached_tokens) × $2.70/M + cached_tokens × $0.27/M + output_tokens × $13.50/M`.
The `_extract_cost()` function in zai_proxy.py must handle this.
→ Added as Task 3.3a (new task).

### FIX 8: Shadow optimizer seed rate
Telnyx blended rate for kimi-k3 (assuming 3:1 input:output ratio):
`($2.70×3 + $13.50×1) / 4 = $5.40/M` — seed shadow optimizer at $5.40/M.
→ Task 2.3 must use this seed rate, not $2.70.

## 9. Updated Task List (with fixes applied)

| Task | File(s) | Worker | Model | Timeout |
|------|---------|--------|-------|---------|
| 1.1 providers.yaml | config/providers.yaml | worker-merchant | glm-5.2 | 120s |
| 1.2 model_mapping.py | src/model_mapping.py | worker-merchant | glm-5.2 | 120s |
| 1.3 provider_names.py | src/provider_names.py | worker-merchant | glm-4.5-flash | 90s |
| 2.1 key loading | zai_proxy.py | worker-merchant | glm-4.5-flash | 90s |
| 2.2 failover+model names+priority | zai_proxy.py | worker-merchant | glm-5.2 | 180s |
| 2.3 shadow optimizer | zai_proxy.py | worker-merchant | glm-5.2 | 120s |
| 2.4 exclusive list removal | live_router.py | worker-merchant | glm-5.2 | 90s |
| 3.1 balance collector | src/telnyx_balance_collector.py | worker-merchant | glm-5.2 | 180s |
| 3.2 balance bridge | zai_proxy.py | worker-merchant | glm-5.2 | 120s |
| 3.3 cost extraction | zai_proxy.py | worker-merchant | glm-5.2 | 180s |
| 3.3a cached token cost | zai_proxy.py | worker-merchant | glm-5.2 | 120s |
| 4.1 quota pressure | src/pricing_engine.py | worker-merchant | glm-5.2 | 180s |
| 4.2 price tracker | src/real_price_tracker.py | worker-merchant | glm-4.5-flash | 120s |
| 4.3 LiveRouter | src/live_router.py | worker-merchant | glm-5.2 | 180s |
| 5.1 systemd conf | systemd drop-in | worker-merchant | glm-4.5-flash | 60s |
| 6.1 unit tests | tests/test_telnyx*.py | worker-merchant | glm-5.2 | 180s |
| 6.2 integration test | (live API call) | worker-merchant | glm-5.2 | 180s |

**Dependencies:**
- Phase 1 (1.1→1.2→1.3) sequential (config → mapping → names)
- Phase 2 (2.1→2.2→2.3) sequential, 2.4 after 6.2
- Phase 3 (3.1→3.2→3.3→3.3a) sequential
- Phase 4 (4.1→4.2→4.3) sequential, after Phase 3
- Phase 5 after Phase 4
- Phase 6.1 after Phase 1+2, 6.2 after Phase 2, 6.3 after all

## 10. Routstr Integration (Future)

Once Telnyx is validated as a provider:
1. Telnyx becomes a per-token source in the Routstr marketplace
2. Consumers can buy Kimi K3 access via Routstr at Telnyx rates + margin
3. Telnyx's prompt caching makes it competitive for multi-turn workloads
4. Merchant mode: `customer_price = telnyx_cost × (1 + margin_pct / 100)`

This is the path to making Kimi K3 available to external users over Routstr —
which is the original ask.