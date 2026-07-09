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
- The production proxy (`~/.hermes/bot/zai_proxy.py`) is the source of truth until Phase 2
- All changes to production must have a revert plan (see `docs/migration-plan.md`)
- z.ai flat rate is always the primary provider — paid providers are last resort only
