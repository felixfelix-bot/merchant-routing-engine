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

**Goal:** Replace `best_key()` with `PrimaryRouter.route()` as the primary routing decision.

**Status:** CODE READY — awaiting 48h shadow soak validation.

### Deliverables (ready)
- `src/primary_router.py` — drop-in replacement for best_key(), same return contract
- `tests/test_primary_router.py` — 16 tests (all pass)
- `scripts/deploy_phase3.py` — automated deploy + revert script with health checks

### Prerequisites (must pass before deploy)
- [ ] 48h shadow soak — no error spikes, no catastrophic misroutes
- [ ] Shadow agreement rate > 50% (optimizer mostly agrees with best_key)
- [ ] Shadow cost ≤ live cost (optimizer is cheaper or equal)
- [ ] Zero fallback picks by optimizer
- [ ] Burn-rate Kalman converged (tokens_used > 10M)

### Deployment

```bash
# Dry run first
cd ~/merchant-routing-engine
python3 scripts/deploy_phase3.py --dry-run

# Deploy (auto-reverts on syntax error or health check failure)
python3 scripts/deploy_phase3.py

# Revert
python3 scripts/deploy_phase3.py --revert
```

### Safety Design
1. **Fallback chain**: PrimaryRouter.route() → best_key() proactive → reactive
2. **Auto-revert**: syntax error or health check failure → restore from backup
3. **Same return contract**: "ours"/"friend"/None — identical to best_key()
4. **Never raises**: on ANY error, falls back to existing best_key() logic
5. **Read-only Kalman**: route() doesn't mutate state during selection

### How it works
```
best_key() called → PrimaryRouter.route() tries first
  ├─ returns "ours" or "friend" → use that z.ai key
  ├─ returns None → optimizer says skip z.ai (go to ollama)
  └─ raises → falls through to existing best_key logic
```

---

## Version Tags

```bash
git tag v1-phase1    # Standalone modules
git tag v2-phase2    # Shadow mode live
git tag v3-phase3    # Real cost feed
git tag v4-phase4    # Optimizer primary
```
