# ADR-013: Kalman-Gated Regime-Shift Alerting for Quota↔Metered Transitions

- **Status:** Accepted (2026-08-26)
- **Context:** When subscription/quota-based providers (z.ai, ollama_cloud, opencode_go) exhaust their weekly/session caps, traffic silently cascades to pay-per-token providers (routstrd at $0.53/M, neuralwatt at $0.80/M), causing $19+/day bleeds. Existing alerts fire reactively (after cost is incurred). A trend-detection layer is needed that identifies the *direction* of the transition before it becomes a full cascade, with anti-spam guards to avoid alert fatigue.
- **Decision:**
  - **Metric:** `metered_share` = tokens on T2/T5 providers / total tokens, computed hourly from `api_calls` classified by `PROVIDER_TIER` (quota/flat/included = quota-side; balance/per_token = metered-side). Tier-based classification (not `cost_usd > 0`) is essential because flat subscriptions like opencode_go record estimated $ but are not pay-per-token.
  - **Kalman significance gate:** feed `metered_share` into `ConsumptionKalman` (existing [level, velocity] filter). A trend is significant only when `|velocity| / sqrt(P_vv) ≥ 2.5` (z-score). Noise never passes this gate.
  - **Three gates (ALL required before push):** (a) level crosses band (>25% up / <10% down after being up), (b) Kalman velocity z-score ≥ 2.5, (c) persistence for 2 consecutive 30-min cron windows.
  - **Anti-spam:** hysteresis band (25%/10%), 4h per-direction cooldown, tracked via `escalation_alert_state.json` fingerprints `regime_shift:up` / `regime_shift:down`. One push per regime transition (not per window).
  - **Push payload:** current share %, Kalman z-score, top-3 mover providers (gained/lost share), expected $/day at new mix. Attach heatmap + envelope PNGs on transitions only (rare = valuable, no spam).
- **Consequences:**
  - (+) Proactive: alerts fire when the trend is significant, before full cascade.
  - (+) Anti-spam: hysteresis + Kalman gate + persistence + cooldown = zero false positives on normal traffic fluctuation.
  - (+) Tier classification is the single source of truth (reuses `PROVIDER_TIER` from `flat_router.py`).
  - (-) Kalman state must be persisted across cron runs (30-min cadence; state in `escalation_alert_state.json`).
  - (-) Hourly granularity means detection latency is ~1h (acceptable for cost trends; not for reliability alerts).