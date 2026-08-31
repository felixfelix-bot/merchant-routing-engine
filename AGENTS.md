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
- The production proxy (`production/zai_proxy.py`) is the source of truth — it imports `flat_router.select_provider`; LiveRouter (Kalman-based) runs as the primary
- All changes to production must have a revert plan (see `docs/migration-plan.md`)
- All providers are equal (no z.ai preference) — routing picks the cheapest healthy provider (see `flat_router.py`)
