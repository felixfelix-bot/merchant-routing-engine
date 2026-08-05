# Activation Risk Assessment — Pressure System + Per-Model Pricing

**Date:** 2026-08-05
**Consultant plan:** docs/plan-per-model-pricing.md (1112 lines)
**Priority:** Zero interruption to live traffic

---

## Architecture Facts

1. Proxy reads from `~/merchant-routing-engine/src/live_router.py` directly (sys.path)
2. Kill switches are module-level `os.environ.get()` — require `systemctl --user restart zai-proxy` to change
3. Restart takes ~3 seconds (Type=simple, Restart=always, RestartSec=3)
4. During restart, requests fail with connection refused (~3s window)
5. Two independent systems: pressure (P8) and per-model (PM-T3) — separate kill switches

## Risk: P8 + PM-T3 Interaction

THE KEY QUESTION: Should we enable pressure and per-model pricing simultaneously or sequentially?

**Consultant recommendation (from plan):** SEQUENTIAL. Enable per-model pricing FIRST, validate 48h, THEN enable pressure.

**My recommendation: REVERSED.** Enable pressure FIRST, validate, THEN per-model. Reasoning:

1. Pressure system is FULLY TESTED (P7 cascade test passed, 96 tests)
2. Per-model pricing (PM-T3) is NOT YET WIRED — _resolve_model_rate() exists but isn't called in failover
3. Pressure only changes multipliers (1.0x → 1.5x near exhaustion) — small effect on routing
4. Per-model changes BASE RATES (up to 10x for DeepInfra, 485x for kimi) — large effect

**Risk if both enabled simultaneously:** If per-model pricing makes a provider look 10x cheaper (DeepInfra flash $1.30→$0.13) AND pressure is also active, the compounding effect could route to DeepInfra prematurely. We'd be unable to tell which system caused the change.

## Recommended Activation Sequence

```
PHASE A: Pressure Only (P8)
  Day 1: OLLAMA_QUOTA_PRESSURE_ENABLED=true → restart → 24h monitor
  Day 2: ZAI_QUOTA_PRESSURE_ENABLED=true → restart → 24h monitor
  Day 3: PPQ_QUOTA_PRESSURE_ENABLED=true → restart → 24h monitor
  Day 4: OPENROUTER_CREDIT_PRESSURE_ENABLED=true → restart → 24h monitor
  Day 5: DEEPINFRA_CREDIT_PRESSURE_ENABLED=true → restart → 24h monitor

PHASE B: Per-Model Wiring (PM-T3)
  Day 6: Wire PM-T3 + tests + shadow mode → PER_MODEL_PRICING_ENABLED=true
  Day 7-8: 48h shadow validation (per-model rates logged, routing NOT changed)
  Day 9: Enable per-model pricing live → monitor 24h

ROLLBACK for any phase:
  Set env var back to false
  systemctl --user restart zai-proxy
  ~3 second interruption window
```

## Risk Analysis

### R1: PPQ/OpenRouter dead endpoints (MEDIUM RISK)
PPQ and OpenRouter are at $0 balance. Their pressure should be infinity → excluded from routing.
- WITH pressure: safe (infinity pressure = excluded)
- WITHOUT pressure: router sees PPQ at $0.14/M base rate → could route to dead endpoint
- **Mitigation:** Pressure must be enabled for PPQ/OpenRouter BEFORE anything else changes. If pressure somehow fails to make them infinite, the dead-endpoint guard in the health tracker should still exclude them (429/connection errors mark them unhealthy).

### R2: Cold-start window (LOW RISK)
First ~5 minutes after restart, balance collectors haven't run. Cold-start fix (P4) seeds conservative 0.5 pressure.
- **Mitigation:** The 0.5 seed biases AWAY from unknown-balance endpoints. Worst case: router prefers z.ai (known quota) slightly more for 5 minutes. Not dangerous.

### R3: Per-model base rate shock (MEDIUM RISK)
DeepInfra flash goes from $1.30/M to $0.13/M (10x cheaper). This could cause a sudden traffic shift to DeepInfra.
- **Mitigation:** Enable per-model in SHADOW MODE first (48h). Verify routing decisions match expectations. The kill switch allows instant rollback.

### R4: Kimi bypass unchanged (NO RISK)
kimi models short-circuit before pricing comparison. Per-model pricing doesn't affect this path.

### R5: All-∞ deadlock (LOW RISK)
If all quota endpoints exhaust simultaneously AND all credit endpoints are empty, every endpoint is at infinity.
- **Mitigation:** Deadlock fallback (P7 cascade test verified) picks least-bad endpoint (lowest base rate). z.ai ours ($0.001/M) wins. 429s auto-retry.

### R6: Proxy restart interruption (LOW RISK)
Each kill switch change requires a 3-second restart.
- **Mitigation:** Schedule restarts during low-traffic periods. Restart is automatic (Restart=always, RestartSec=3).

## Rollback Plan

For ANY anomaly:

```bash
# Instant rollback — set ALL flags to false
# Create a drop-in that overrides all pricing env vars
cat > ~/.config/systemd/user/zai-proxy.service.d/pricing-rollback.conf << 'EOF'
[Service]
Environment=OLLAMA_QUOTA_PRESSURE_ENABLED=false
Environment=ZAI_QUOTA_PRESSURE_ENABLED=false
Environment=PPQ_QUOTA_PRESSURE_ENABLED=false
Environment=OPENROUTER_CREDIT_PRESSURE_ENABLED=false
Environment=DEEPINFRA_CREDIT_PRESSURE_ENABLED=false
Environment=PER_MODEL_PRICING_ENABLED=false
EOF

systemctl --user daemon-reload
systemctl --user restart zai-proxy
```

This single drop-in file disables ALL pricing features in one restart. Keep it ready.

## Pre-Activation Checklist

Before starting Phase A:
- [ ] Confirm rollback drop-in file is ready
- [ ] Confirm test suite passes (python3 -m pytest tests/ -x -q)
- [ ] Confirm proxy is healthy (curl localhost:9099/v1/models returns 200)
- [ ] Confirm z.ai quota is not near exhaustion (check dashboard)
- [ ] Confirm balance collectors are running (api_burn.db has recent entries)
- [ ] Verify PPQ/OpenRouter pressure returns infinity (they're at $0)

## Monitoring During Activation

After each kill switch flip:
1. Check proxy logs: `journalctl --user -u zai-proxy --since "5 min ago" | grep -i "error\|fail\|429"`
2. Check routing decisions: `sqlite3 routing_live_decisions "SELECT * FROM routing_live_decisions ORDER BY ts DESC LIMIT 10;"`
3. Check 429 rate: compare before/after
4. Confirm no traffic to dead endpoints (PPQ/OpenRouter)
