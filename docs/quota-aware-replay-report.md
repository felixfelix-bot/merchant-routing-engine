# Quota-Aware Routing Replay — Full Pricing System Test

**Generated:** 2026-07-27T23:10:11.995624+00:00
**Decisions replayed:** 104,091 (sampled from 520K key_decisions)
**Rates:** CONVERGED Kalman (ours=$0.0010, friend=$0.0290)
**Pricing:** base × peak(3x 6-10UTC) × scarcity(1.0→2.0 ramp) × health(breaker)

## Provider Distribution

| Provider | Live (actual) | Optimizer (converged+scarcity) |
|----------|---------------|--------------------------------|
| deepinfra | 8 (0.0%) | 0 (0.0%) |
| friend | 73,507 (70.6%) | 0 (0.0%) |
| none | 3,766 (3.6%) | 0 (0.0%) |
| ollama_cloud | 3,189 (3.1%) | 69,362 (66.6%) |
| openrouter | 19 (0.0%) | 0 (0.0%) |
| ours | 23,589 (22.7%) | 34,729 (33.4%) |
| ppq | 13 (0.0%) | 0 (0.0%) |

**Agreement:** 21,999/104,091 (21.1%)

## Quota Bracket Analysis — Does Scarcity Smooth the Cliff?

Shows routing behavior at different ours_pct levels.
Live = production proxy (binary: available/exhausted).
Optimizer = price-based with scarcity ramp.

| ours_pct | Total | Live→ours | Live→friend | Opt→ours | Opt→friend | Opt→ollama | Ours eff $/M | Friend eff $/M |
|----------|-------|-----------|-------------|----------|------------|------------|-------------|---------------|
|    0-9% | 25,738 | 28% | 56% | 87% | 0% | 13% | $0.0044 | $0.2017 |
|  10-24% | 2,464 | 99% | 1% | 100% | 0% | 0% | $0.0016 | $0.3833 |
|   100%+ | 43,729 | 6% | 89% | 0% | 0% | 100% | $0.0322 | $0.1940 |
|  25-49% | 2,668 | 100% | 0% | 100% | 0% | 0% | $0.0015 | $0.4200 |
|  50-74% | 12,824 | 40% | 55% | 36% | 0% | 64% | $0.0107 | $0.1140 |
|  75-89% | 13,959 | 21% | 79% | 17% | 0% | 83% | $0.0198 | $0.1319 |
|  90-99% | 2,709 | 21% | 79% | 0% | 0% | 100% | $0.0247 | $0.2059 |

## Cliff Smoothing Analysis

- Optimizer first shifted away from 'ours' at **75% quota** → picked ollama_cloud
  (friend was at 22% at that point)
- Optimizer shifted away from ours **25,633 times** total
- Shift distribution by quota bracket:
  - <50%: 0
  - 50-74%: 0
  - 75-89%: 200
  - 90-99%: 0
  - 100%+: 0

## Findings

### 1. Scarcity ramp effect

- During non-peak (1x): ours crosses friend price at scarcity=29.0x (quota 1449% used)
- During peak (3x): ours crosses friend price at scarcity=9.7x (quota 483% used)

### 2. Production vs optimizer cliff comparison

Production proxy uses a BINARY cliff: ours_available=1 → route to ours, ours_available=0 → route to friend. This causes sudden traffic spikes on friend.

The optimizer with scarcity ramps ours's price gradually as quota fills. At converged rates, ours starts at $0.001/M — even at 2x scarcity + 3x peak = $0.0060/M, still cheaper than friend's $0.0290/M. So scarcity alone does NOT shift traffic at these rates.

The only mechanism that shifts traffic is the HARD exhaustion gate (breaker_tripped when ours_available=0). Scarcity pricing smooths the transition but doesn't cause it — the rates are too far apart.

### 3. What WOULD cause earlier shifting?

With converged rates, ours is ~30x cheaper than friend. Scarcity (2x) + peak (3x) = 6x — not enough. To make the optimizer shift traffic to friend BEFORE exhaustion, we'd need either:

1. **Higher scarcity ceiling** (e.g., 10x at 90% instead of 2x at 100%)
2. **Quota reservation** — reserve last 20% for high-priority only
3. **Pace-based shifting** — if burn rate predicts exhaustion within X hours, gradually raise price to start pre-shifting traffic
4. **Accept the binary cliff** — ours is so cheap that maxing it out before falling back is actually optimal. The cliff IS the right behavior when rates are this far apart.
