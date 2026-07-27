# Converged-Rate Routing Replay Report

**Generated:** 2026-07-27T22:50:35.861052+00:00
**Decisions replayed:** 50,354
**Total tokens:** 527,017,381

## Rate Comparison

| Provider | Seed $/M | Converged $/M | Delta |
|----------|----------|---------------|-------|
| deepinfra | $1.3000 | $1.300000 | +0.000000 |
| friend | $0.3750 | $0.028983 | -0.346017 |
| ollama_cloud | $0.5000 | $0.023952 | -0.476048 |
| openrouter | $0.1350 | $0.135000 | +0.000000 |
| ours | $0.3100 | $0.001000 | -0.309000 |
| ppq | $0.1400 | $0.140000 | +0.000000 |

## Provider Distribution

| Provider | Live (actual) | Shadow (seed) | Seed Replay | Converged Replay |
|----------|---------------|---------------|-------------|------------------|
| fallback | 0 (0.0%) | 130 (0.3%) | 0 (0.0%) | 0 (0.0%) |
| friend | 34,329 (68.2%) | 15,119 (30.0%) | 0 (0.0%) | 0 (0.0%) |
| ollama_cloud | 0 (0.0%) | 3,840 (7.6%) | 371 (0.7%) | 0 (0.0%) |
| openrouter | 0 (0.0%) | 97 (0.2%) | 97 (0.2%) | 0 (0.0%) |
| ours | 16,025 (31.8%) | 31,168 (61.9%) | 49,886 (99.1%) | 50,354 (100.0%) |

## Cost Comparison

| Metric | Value |
|--------|-------|
| Live cost (actual spend logged) | $10743.9500 |
| Shadow cost (seed-rate estimate) | $11996.6260 |
| Seed replay cost (re-estimated) | $165.0163 |
| Converged replay cost | $0.5489 |

**Converged vs Seed replay savings: 99.7%**

## Agreement Rates

| Comparison | Agreed | Rate |
|------------|--------|------|
| Live vs Seed replay | 15,877 | 31.5% |
| Live vs Converged replay | 16,025 | 31.8% |
| Seed replay vs Converged replay | 49,886 | 99.1% |

## Token Flow Under Converged Rates

| Provider | Tokens (converged routing) | % of total |
|----------|---------------------------|------------|
| ours | 527,017,381 | 100.0% |

## Key Findings
