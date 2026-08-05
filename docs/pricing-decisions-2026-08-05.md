# Pricing Architecture Decisions — 2026-08-05

## Decision Summary

All quota-based AND credit-based endpoints get exponential price pressure. The price the router sees depends on ALL factors: base cost, remaining quota/credits, peak hours, health, and scarcity.

## Architecture: Two-Layer Pricing

### Layer 1: base_rate = PREFERENCE (always active)
The fundamental cost-per-token for each endpoint. Lower = preferred. Determined by:
- z.ai (ours): amortized from $155/mo subscription ÷ trailing token volume (~$0.001-0.015/M depending on usage)
- z.ai (friend): $0 fee, floored at min_effective_price ($0.001/M)
- ollama_cloud: $0.024/M (measured from prepaid burn data)
- PPQ: $0.14/M (per-token, from ledger)
- OpenRouter: $0.135/M (per-token)
- DeepInfra: $1.30/M (per-token)

Preference ranking at normal load:
ours ($0.001) > ollama ($0.024) > friend ($0.029) > openrouter ($0.135) > ppq ($0.140) > deepinfra ($1.30)

### Layer 2: asymptote = URGENCY (active near exhaustion)
Controls how aggressively price rises as quota/credits deplete. 

## Decision 1: Exponential Curve (1/(1-x) asymptote)

Formula: pressure(u) = 1 + K·t/(1-t)
  where t = (u - onset) / (1 - onset), K = asymptote - 1
  u < onset: pressure = 1.0 (no penalty)
  u >= 1.0: pressure = infinity (unreachable — router always finds alternative)

Rationale: Price approaches infinity as usage approaches 100%. The router ALWAYS finds a cheaper alternative before quota actually exhausts. No thresholds, no special-casing — pure price-based routing.

## Decision 2: Superposition (Multiply Windows)

Each quota window gets its own exponential. Combined pressure = product of all windows.

For z.ai: session_factor × weekly_factor × monthly_factor
For ollama: session_factor × weekly_factor
For credit-based: single factor

Example: session=90% AND weekly=90%:
- Old (max): 2.0x (single curve on worse window)
- New (superposition): 2.0 × 2.0 = 4.0x (both depleting = worst case, steeper)

Rationale: When multiple windows deplete simultaneously, pressure should compound — that's genuinely worse than a single window depleting.

## Decision 3: ALL Endpoints Get Pressure (Universal)

Every endpoint with a finite resource gets exponential pressure:

Quota-based (time windows):
- z.ai (ours + friend): 5h session × weekly × monthly windows
- ollama_cloud: 5h session × 7d weekly

Credit-based (balance depletion):
- PPQ: u = 1 - (credits_remaining / credits_start), API at /credits/balance
- OpenRouter: u = 1 - (credits_remaining / credits_start), API at /credits endpoint
- DeepInfra: u = 1 - (remaining / $5.00 starting), self-tracked from proxy logs (no balance API)

## Decision 4: Uniform Low Asymptote (1.5)

ALL endpoints: asymptote = 1.5 (K = 0.5)

Rationale: Squeeze every token from sunk-cost subscriptions before fleeing to expensive per-token endpoints. Low asymptote = gentle ramp = keys stay usable until ~95%+ quota.

Price table at asymptote=1.5 (single window, onset=0.70, base=$0.024/M):
  70% usage: 1.0x → $0.024/M (onset — no penalty)
  80%: 1.25x → $0.030/M
  85%: 1.5x → $0.036/M
  90%: 2.0x → $0.048/M
  95%: 3.5x → $0.084/M
  99%: 15.5x → $0.372/M
  100%: infinity

Reversed from earlier decision (5.0 → 1.5). Felix: "make the asymptote really low so that the keys flee as late as possible."

## Decision 5: Onset Staggering

Different onset points stagger pressure activation:
- z.ai: onset = 0.60 (5h window is tiny, fills fast)
- ollama_cloud: onset = 0.70
- PPQ/OpenRouter/DeepInfra: onset = 0.80 (credits deplete slowly)

Rationale: Sequential activation distributes load. z.ai enters ramp first, ollama second, credit-based last. DeepInfra/OpenRouter (infinite quota... wait, they're credit-based) catch overflow only when everything else is near exhaustion.

## Decision 6: Monthly Window for z.ai

z.ai gets 3 superimposed windows: 5h × weekly × monthly.
Monthly window is NOT just a billing period — it's a real quota limit that triggers 429s.

## Decision 7: Trailing 365d Base Rate

z.ai base rate computed from ALL available trailing data (currently ~40 days), not month-to-date.
Formula: annual_fee / (trailing_tokens × (365/trailing_days) / 1e6)
Uses whatever data is available, annualized.

## Decision 8: hard_limit Parameter

- ollama_cloud: hard_limit=False (has extra-usage path, caps at asymptote at 100%)
- z.ai: hard_limit=True (no extra-usage, price → infinity at 100%)
- PPQ/OpenRouter/DeepInfra: hard_limit=True (credits exhausted = no service)

## Current Endpoint Balances (2026-08-05)

| Endpoint | Balance | Status |
|---|---|---|
| z.ai (ours) | $155/mo subscription | Active |
| z.ai (friend) | $0 (shared) | Active |
| ollama_cloud | ~$2 remaining | Nearly exhausted |
| PPQ | $0.00 | EXHAUSTED (price=inf) |
| OpenRouter | $0.00 | EXHAUSTED ($10 used) |
| DeepInfra | $5.00 starting | Self-tracked |

## Future: Routstr Vision

Each endpoint's pricing model → Routstr node (publishes effective price via Nostr).
Router → Routstr client (subscribes to prices, picks cheapest).
Scoped JWT with spending_limit = prepaid envelope (hard cap at API level).

Migration path:
1. NOW: Universal pressure in existing architecture
2. NEXT: Wrap into self-contained EndpointPriceModel classes
3. FUTURE: Each model → Routstr node, optimizer → pure client
