# Migration Plan — zai_proxy.py → merchant-routing-engine

## Phase 1: Standalone Modules ✅ COMPLETE

Built and tested the price-first routing engine as standalone modules.

**Deliverables:**
- `src/price_kalman.py` — 2-state Kalman, deterministic peak/scarcity/health multipliers
- `src/consumption_kalman.py` — 3-state Kalman, burn rate + exhaustion prediction
- `src/routing_optimizer.py` — per-provider peak hours, 5-stage filter pipeline
- `src/shadow_logger.py` — thread-safe SQLite dual-decision logger
- `config/providers.yaml` — full pricing for 5 providers

**Tests:** 190/190 pass (including 10 e2e integration scenarios)
**Coverage:** 90-98% on new modules
**Pushed:** GitHub + ngit (tag: `v1-phase1`)

---

## Phase 2: Shadow Mode ✅ COMPLETE

Live shadow comparison running in production. The optimizer's decision is logged alongside `best_key()` for every API call. Read-only — never affects routing.

**Deliverables:**
- `src/shadow_hook.py` — persistent singleton bridging proxy to optimizer
- `tests/test_shadow_hook.py` — 16 tests
- `zai_proxy.py` patched (3 surgical changes):
  1. Import bridge (try/except guarded)
  2. `_snapshot_quota()` + `_snapshot_health()` helpers
  3. Shadow hook call in `_proxy()` finally block

**Deployed:** Proxy restarted, healthy, collecting data.
**Backup:** `~/.hermes/bot/zai_proxy.py.bak-phase2`

### Early Shadow Data (first 2h, 111 decisions)

| Metric | Value |
|--------|-------|
| Decisions logged | 111 |
| Agreement rate | 21.6% |
| Avg live cost | $0.361/M |
| Avg shadow cost | $0.435/M |

**Decision breakdown:**
- `friend → ollama_cloud` (73x) — optimizer avoids unhealthy z.ai keys
- `ours → ours` (24x) — agreement when both z.ai keys healthy + off-peak
- `friend → ours` (14x) — optimizer correctly prefers cheaper ours key

**Key insight:** Low agreement is EXPECTED — the optimizer makes different (smarter) decisions:
1. Filters out unhealthy keys entirely (best_key retries them)
2. Applies peak multiplier (best_key ignores peak pricing)
3. Prefers ours over friend (ours is 21% cheaper)

**Cost caveat:** Shadow cost appears higher because PriceKalman seeds haven't converged. Need real spend feed (Phase 3) for accurate cost comparison.

### Revert

```bash
cp ~/.hermes/bot/zai_proxy.py.bak-phase2 ~/.hermes/bot/zai_proxy.py
systemctl --user restart zai-proxy
```

---

## Phase 3: Feed Real Cost Data → Converge Kalman (NEXT)

**Goal:** Feed actual $/M observations into PriceKalman so the cost comparison becomes meaningful.

### Problem
PriceKalman instances use static seed rates. They need real cost observations to converge:
- z.ai ours: `€155/mo / actual_monthly_tokens` (amortization)
- z.ai friend: ours × 1.21
- ollama_cloud: `$100/mo / actual_monthly_tokens`
- ppq/openrouter: fixed per-token pricing

### Step 1: Daily cost feed
Query `daily_spend` table (already populated by `_record_spend()`) once per refresh cycle:
```python
# In shadow_hook.py, add price update logic:
SELECT spend_usd, token_count FROM daily_spend WHERE date = today AND tier = 'ours'
effective_rate = spend_usd / (token_count / 1e6)  # $/M
price_kalman.update(effective_rate)
```

### Step 2: Validate convergence
After 24-48h with real cost data, re-check:
- Agreement rate (should stabilize)
- Cost comparison (should reflect real economics)
- Peak-hour switching (should show optimizer saving money during peak)

---

## Phase 4: Make Optimizer Primary

**Goal:** Replace `best_key()` with `optimizer.route()` as the primary routing decision.

### Prerequisites
- [ ] Phase 3 complete — Kalman filters converged with real cost data
- [ ] 48h shadow soak showing optimizer is safe (no crashes, no bad decisions)
- [ ] Agreement rate analysis showing optimizer is equal or better

### Migration path
1. Add feature flag: `~/.hermes/bot/.optimizer_primary`
2. When flag exists: `chosen = optimizer.route().chosen_provider`
3. When absent: `chosen = best_key()` (current behavior)
4. Monitor for 24h with flag on
5. Remove flag + old code once stable

### Safety
- Optimizer never returns None (always has fallback model)
- Optimizer respects health state (filters tripped breakers)
- Optimizer respects peak hours (avoids z.ai during UTC 6-10)
- Revert: `rm ~/.hermes/bot/.optimizer_primary && systemctl --user restart zai-proxy`

---

## Version Tags

```bash
git tag v1-phase1    # Standalone modules
git tag v2-phase2    # Shadow mode live
git tag v3-phase3    # Real cost feed
git tag v4-phase4    # Optimizer primary
```
