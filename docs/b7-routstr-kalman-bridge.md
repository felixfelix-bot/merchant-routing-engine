# B7 — Routstr ⇄ Kalman pricing bridge (contract + open decision)

Bridge between the merchant routing engine's Kalman-smoothed effective prices and
the routstrd daemon's provider selection. Code lives in two repos:

| Side | File | Repo / branch |
|---|---|---|
| Producer | `scripts/export_kalman_pricing.py`, `scripts/kalman_pricing_cron.sh` | `felixfelix-bot/merchant-routing-engine` → `t_f3bc14f1/b7-kalman-pricing-export` |
| Consumer | `src/daemon/kalman-pricing-bridge.ts` (+ `src/daemon/index.ts`, `src/utils/config.ts`, `tests/kalman-pricing*`) | `felixfelix-bot/routstrd` → `churn-fixes-2026-08-22` |

Wire format: `~/.routstrd/kalman_pricing.json` (atomic replace, 5-min cron).
```
{ generated_at, btc_price_usd, sat_per_usd,
  providers: { ours|friend|ollama_cloud|ppq|openrouter|deepinfra:
               { effective_rate_per_m, sat_per_m, sat_per_token, source,
                 is_measured, confidence, ... } } }
```
The daemon overlays `sats_pricing.{prompt,completion,max_prompt_cost,max_completion_cost,max_cost}`
on the SDK model cache so `ProviderManager.getProviderPriceRankingForModel()`
(which sorts by `prompt + completion`, ×1e6) ranks by Kalman price. The overlaid
total per million tokens equals that provider's `sat_per_m`.

## Operational notes

- **Interpreter:** the export needs numpy → run it with `/usr/bin/python3`. An
  interactive `python3` (~/.local/bin/python3) raises `ModuleNotFoundError: numpy`;
  `kalman_pricing_cron.sh` now pins the system interpreter (override `KALMAN_PYTHON`).
- **Freshness guard (mandatory):** the bridge refuses data older than
  `kalmanPricing.maxAgeSeconds` (default 900s = 3 missed cycles) and restores the
  provider-published sats_pricing. Rationale: a 26-day-old file on 2026-09-12 held
  the `cold_start_fallback` rate for `ours` ($0.001/M ≈ 1.05 sat/M) — ~1000× below
  reality — which would have routed all traffic onto our own key.
- **Status:** `GET http://127.0.0.1:8008/kalman-pricing` →
  `{applied, stale, ageSeconds, appliedModels, matchedProviders[], lastError}`.

## OPEN DECISION (operator) — `providerMap`

Default map (routstrd `DEFAULT_PROVIDER_MAP`) matches substrings
`z.ai → ours` (then `friend`, unreachable: same pattern, first match wins),
`ollama.com` + `localhost:9099 → ollama_cloud`, `ppq.ai → ppq`,
`openrouter.ai → openrouter`, `deepinfra.com → deepinfra`, `telnyx.com → telnyx`.

As of 2026-09-12 the live daemon's pool (`GET /providers`, 40 entries) contains
**none** of `z.ai` / `ollama.com` / `ppq.ai` / `openrouter.ai` / `deepinfra.com`,
and no `localhost:9099` entry in the SDK model cache (`sdk_storage` keys in
`~/.routstrd/routstr.db`), so the overlay currently matches **zero** providers —
Kalman pricing is wired and verified but inert in production.

Two questions for the operator, both one-line config changes once answered
(`~/.routstrd/config.json` → `kalmanPricing.providerMap`):

1. **Does the local zai_proxy (`http://localhost:9099/`, the `staticProviders`
   entry) represent `ours` (our z.ai flat-rate key amortized) or `ollama_cloud`
   (its failover upstream)?** `docs/KALMAN-ROUTING-ARCHITECTURE.md` says
   localhost:9099 fronts the two z.ai keys (ours/friend) → suggests `ours`.
   Mapping it to `ollama_cloud` prices our own proxy off a measured rate that the
   current export mislabels anyway (`ollama_cloud` came back with
   `source: zai_amortized` on 2026-09-12 — a rate-attribution bug in
   `RealtimePricing.get_rate("ollama_cloud")`, worth its own card).
2. **Which routstr baseUrl is the friend's node (their z.ai key) and which is
   ours?** Those are the entries where a Kalman price actually changes the
   buy-side decision.

Until then the bridge stays fail-safe: no match → provider-published pricing.

## Verification (2026-09-12, t_f3bc14f1)

- `bun test` → 36 pass / 0 fail (includes `tests/kalman-pricing-integration.test.ts`,
  which drives the real SDK `ProviderManager` over a real sharded discovery
  adapter and asserts the ranking reorders to the Kalman-optimal provider,
  that the deposit estimate follows, and that stale data reverts the ranking).
- `bun run lint` (tsc --noEmit) clean.
- Live daemon `GET /kalman-pricing` before hardening: `hasData:true`,
  `generatedAt: 2026-08-17T13:55:02Z` (26 days stale), no `Overlaid` log line
  since 2026-09-03 → overlay inert.
