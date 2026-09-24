# AGENTS.md — Merchant Routing Engine

## Build/Test Commands

```bash
# Run tests
python3 -m pytest tests/ -v

# Run single test
python3 -m pytest tests/test_key_health.py -v

# Check syntax
python3 -c "import src.key_health_tracker; import src.provider_funding_tracker"

# Lint
python3 -m py_compile src/*.py
```

## Architecture

- `src/` — standalone modules (extracted from production zai_proxy.py)
- `docs/` — architecture docs, incident log, migration plan
- `tests/` — pytest tests
- `config/providers.yaml` — provider definitions

## Key Constraints

- NEVER commit API keys (`.env`, `config.yaml` are in .gitignore)
- The LIVE proxy is `~/.hermes/bot/zai_proxy.py`; `production/zai_proxy.py` in this
  repo is a stale snapshot (do not grep it for current behavior). The primary routing
  selector is **`flat_router.select_provider`** (cheapest-first + hysteresis).
  LiveRouter (Kalman-based) is **failover-only**, kill-switched by `.enable_live_routing`
  — corrected 2026-09-24 after a live audit (`docs/provider-hunt/JEV-VERDICT-2026-09-24.md`
  §4): `zai_usage.db` holds 242,524 `flat_router_shadow_decisions` vs 1,587
  `routing_live_decisions`.
- All changes to production must have a revert plan (see `docs/migration-plan.md`)
- All providers are equal (no z.ai preference) — routing picks the cheapest healthy provider (see `flat_router.py`)
