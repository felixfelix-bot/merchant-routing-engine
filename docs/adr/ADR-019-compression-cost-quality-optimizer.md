# ADR-019: Compression Cost/Quality Optimizer (Explicit Sampled LLM Judge)

- **Status:** Accepted (operator-ratified 2026-09-15)
- **Related:** `docs/kalman-compaction-frequency-design.md`,
  `docs/DESIGN-two-layer-pressure-routing.md`.

## Context

Context compression is a **cost/quality tradeoff**: a large context window costs more
per call (input tokens), but **premature compaction loses context**, causing retries,
tool-loop recurrence, and lower answer quality. An optimum exists.

The existing stack already governs this:

- `compression_growth_governor.py` — 1-D Kalman on **context growth rate** → adjusts
  `compression.threshold`, plus a bounded price-aware nudge and a pressure-relief
  integrator.
- `compression_cost_governor.py` — 1-D Kalman on the **compression-cost ratio** + PI
  control → threshold override and a **compression-model budget**.
- `dynamic_context_length_governor.py` — sets `model.context_length` from a registry.
- `compression_model_router.py` — selects the compression model within budget.

Gaps: there is **no explicit quality term**, and the cost model ignores **prompt
caching** (`prompt_caching.cache_ttl: 5m`). Compaction rewrites the prefix, which can
**bust a warm cache** and *increase* cost — so "compact sooner" is not always cheaper.

## Decision

1. **Explicit objective.** Choose the threshold `τ*` minimizing
   `E[cost(τ)] + λ·E[quality_loss(τ)]` over the session horizon:
   - `cost(τ) = ctx_tokens(τ)·lane_$/M − cache_hit_savings(τ) + re-prefill/cache-bust penalty`
   - compact only when projected savings exceed re-prefill + quality risk.
2. **Quality signal = explicit sampled LLM judge.** Sample post-compaction outputs and
   score quality with an LLM judge, in addition to retry/tool-loop-recurrence/kanban
   outcome telemetry. The judge is the authoritative quality estimate.
3. **Quality-first λ.** Default λ favours preserving context (compact later); tune via
   the existing Kalman retune loop.
4. **Keep** the growth-rate Kalman, the price nudge, the pressure-relief integrator,
   `protect_first_n`/`protect_last_n`, and hysteresis. Add the quality + cache terms.
5. **Observability.** Publish `τ*`, the judge score, and the rationale to the digest.
6. Run under the fleet's single dispatcher; deploy via Ansible.

## Consequences

- (+) Compaction decisions are justified by an explicit objective, not a proxy alone.
- (+) Cache-aware cost prevents "compact → cache miss → pay more" mistakes.
- (+) Quality is measured, not assumed; λ is the single cost/quality knob.
- (−) The sampled LLM judge costs tokens (bounded by sampling rate) and adds latency;
  sampling rate must be tuned.
- (−) Judge bias/variance must be monitored (spot audits vs. human review).
- **Risk if ignored:** premature compaction degrades agent quality while appearing
  "cheap" on the cost ratio.

## References

- `scripts/engine/compression_growth_governor.py`,
  `scripts/engine/compression_cost_governor.py`,
  `scripts/engine/dynamic_context_length_governor.py`,
  `scripts/engine/compression_model_router.py`,
  `scripts/engine/compression_growth_health.py`; hermes `config.yaml` `compression:`.
- plan §35; hermes `DECISIONS.md` D-145.
