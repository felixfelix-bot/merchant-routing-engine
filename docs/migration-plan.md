# Migration Plan — From zai_proxy.py to merchant-routing-engine

## Current State (Phase 1)

- Standalone module copies in `src/` mirror the logic in `zai_proxy.py`
- `zai_proxy.py` is production, untouched
- Tests validate the standalone modules
- Felix works in this repo, documents, improves

## Phase 2: Import Bridge

**Goal**: `zai_proxy.py` imports from the merchant-routing-engine package with automatic fallback to inline code.

### Step 1: Install the package
```bash
cd ~/merchant-routing-engine
pip install -e .  # editable install
```

### Step 2: Add import bridge to zai_proxy.py
```python
# At top of zai_proxy.py, replace inline implementations:
try:
    from merchant_routing_engine.key_health_tracker import (
        is_key_healthy, mark_key_exhausted, mark_key_healthy, select_healthy_key
    )
    from merchant_routing_engine.provider_funding_tracker import (
        is_provider_funded, mark_unfunded, mark_funded
    )
    from merchant_routing_engine.reasoning_handler import check_and_inject_reasoning
    _USE_MRE = True
except ImportError:
    _USE_MRE = False
    # Inline fallback (current code continues to work)
```

### Step 3: Deploy
```bash
# Backup current version
cp ~/.hermes/bot/zai_proxy.py ~/.hermes/bot/zai_proxy.py.bak

# Copy new version
cp ~/hermes-orchestration/scripts/engine/zai_proxy.py ~/.hermes/bot/zai_proxy.py

# Restart
systemctl --user restart zai-proxy

# Verify
curl http://localhost:9099/health
```

### Revert if broken
```bash
cp ~/.hermes/bot/zai_proxy.py.bak ~/.hermes/bot/zai_proxy.py
systemctl --user restart zai-proxy
```

### Validation checklist
- [ ] Proxy starts without import errors
- [ ] `/health` returns OK
- [ ] Test request returns content
- [ ] 429 handling works (watch journalctl for backoff)
- [ ] Key health tracker marks/unmarks correctly
- [ ] No TypeError crashes in journal

---

## Phase 3: Full Migration

**Goal**: `zai_proxy.py` is a thin HTTP handler. All routing logic lives in this repo.

### Step 1: Move all logic to merchant-routing-engine
- `best_key()` → `merchant_routing_engine.key_selector.best_key()`
- `_attempt_retry()` → `merchant_routing_engine.backoff.attempt_retry()`
- `_try_external_failover()` → `merchant_routing_engine.external_failover.try_external_failover()`
- Response handling → `merchant_routing_engine.response_handler`

### Step 2: Thin proxy
```python
# zai_proxy.py (future — ~100 lines)
from merchant_routing_engine import ProxyServer
server = ProxyServer(port=9099, config="config/providers.yaml")
server.run()
```

### Step 3: CI validation
- Unit tests for each module
- Integration test: send request, verify routing decision
- Load test: 100 concurrent requests, verify no crashes
- Chaos test: kill z.ai endpoint, verify failover

### Revert
```bash
# Full revert to Phase 1
cp ~/hermes-orchestration/scripts/engine/zai_proxy.py ~/.hermes/bot/zai_proxy.py
pip uninstall merchant-routing-engine
systemctl --user restart zai-proxy
```

---

## Version Tags

Each phase gets a git tag for easy rollback:

```bash
# Phase 1 (current)
git tag v1-phase1-standalone

# Phase 2
git tag v2-phase2-import-bridge

# Phase 3
git tag v3-phase3-full-migration
```

Revert to any phase:
```bash
cd ~/merchant-routing-engine
git checkout v1-phase1-standalone
pip install -e .
systemctl --user restart zai-proxy
```
