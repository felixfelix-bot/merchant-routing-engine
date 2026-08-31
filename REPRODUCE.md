# Reproducing the flat market-based routing engine

This repo contains everything needed to stand up a working copy of the **flat
market-based routing engine**: a single OpenAI-compatible proxy in which *all
providers are equal market participants*. There are no tiers, no lane pinning,
no hardcoded "primary" — every request is routed to the **cheapest healthy
provider**, as judged by per-provider Kalman filters that continuously measure
real cost ($/M tokens) and token burn rate from live traffic. If the winner
fails, the request fails over to the next-cheapest candidate in the same
ordered list. Price *is* the failover chain.

The reference deployment spans **12 providers** covering five pricing models
(quota / balance / flat / included / per-token). **Bring your own keys** — no
credentials ship with this repo. Every key is read at runtime from a `.env`
file (names below; values are yours).

Companion design document: [`docs/flat-router-design.md`](docs/flat-router-design.md).
The sell-side of this market (merchant inference over Nostr, GPL-3.0) is
[routstr-core](https://github.com/felixfelix-bot/routstr-core); this repo is
the **buy-side market engine** that decides where each request goes.

---

## What ships in this repo

| File | Role |
|---|---|
| `flat_router.py` | The flat router itself: `select_provider()`, 5-tier effective pricing, per-provider Kalman wiring, canonicalization, dispatch |
| `test_flat_router.py` | 73-test pytest suite for the router (model filter, health gate, cost ordering, Kalman update, rollback) |
| `production/zai_proxy.py` | The full production proxy (~7.4k lines, stdlib-only HTTP server on :9099) that imports `flat_router` as a sibling |
| `src/price_kalman.py` | Per-provider price Kalman filter (state: `[base_rate, velocity]`) |
| `src/consumption_kalman.py` | Per-provider token-burn Kalman filter (state: `[burn_rate, velocity, acceleration]`) |
| `src/routing_optimizer.py` | Legacy optimizer — source of the uniform effective-price formula; kept for reference/shadow comparison |
| `config/providers.yaml` | Provider definitions (endpoints, `key_env` names, quota windows, tier notes) |
| `docs/flat-router-design.md` | Full design doc: architecture analysis, provider inventory, migration plan |

## Architecture

```
                 ┌─────────────────────────────────────────────────┐
                 │                client (OpenAI SDK)              │
                 └───────────────────────┬─────────────────────────┘
                                         │  POST /v1/chat/completions
                                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  production/zai_proxy.py  — ThreadingHTTPServer on 127.0.0.1:9099      │
│  ─────────────────────────────────────────────────────────────────────  │
│  request → model name                                                    │
│      │                                                                   │
│      ▼                                                                   │
│  flat_router.select_provider(model)          ← flat_router.py            │
│      1. canonicalize model ID               (short → provider-neutral)   │
│      2. filter by PROVIDER_MODELS           (who serves this model?)     │
│      3. health gate                         (backoff, paywall, breaker,  │
│                                              .key_disabled_<name>)       │
│      4. effective price per survivor        (5-tier, see table below)    │
│      5. sort cheapest-first → ordered candidate list                     │
│      │                                                                   │
│      ▼  dispatch loop: try each candidate via _dispatch_to_provider()    │
│         success → _update_kalman_after_request()  (price + burn update)  │
│         failure → mark failure, next candidate; all fail → 503           │
│                                                                          │
│  per-provider state (sibling modules):                                   │
│      src/price_kalman.py        → smoothed $/M  (PriceKalman)            │
│      src/consumption_kalman.py  → token burn    (ConsumptionKalman)      │
│      config/providers.yaml      → endpoints, key_env, quota windows      │
└─────────────────────────────────────────────────────────────────────────┘
        │ keys from ~/.hermes/profiles/manager/.env, ~/.hermes/.env, or
        │ ~/.hermes/bot/.env (first that exists; never committed)
        ▼
   12 upstream providers: ours+friend (z.ai), ollama_cloud, ollama_cloud_2,
   opencode_go, neuralwatt, deepinfra, ppq, openrouter, telnyx, routstr, routstrd
```

The effective price each candidate is ranked by (uniform formula from
`src/routing_optimizer.py`, tier-specialized in `flat_router.py`):

```
effective_cost = base_rate × peak_mult × scarcity_factor × health_factor × pace_mult
```

- **base_rate** — `PriceKalman.predict()`: smoothed $/M measured from real
  response costs (`cost_usd / tokens × 1e6`)
- **peak_mult** — 3.0 for z.ai keys during peak hours (UTC 06:00–09:59), 1.0
  otherwise
- **scarcity_factor** — ramps 1.0 → ∞ as a provider's quota/balance depletes
  (429/paywall pushes it to ∞, i.e. priced out)
- **health_factor** — graduated pricing on failures: 1.0× (0) → 1.5× (1–2) →
  3.0× (3–5) → 10.0× (6–10) → ∞ (breaker, >10)
- **pace_mult** — burn-rate pace multiplier from per-provider pace windows

## The 5-tier pricing model

Tier classification (`PROVIDER_TIER` in `flat_router.py`) recognizes that
"dollar price" means different things for different billing models:

| Tier | Class | Providers | Effective price |
|---|---|---|---|
| **T1** | quota | `ours`, `friend` (z.ai keys) | `MIN_EFFECTIVE_PRICE × max(0.0001, time_decay) × peak_factor × health_factor` — sunk-cost subscription; unused quota is wasted, so price *decays* toward the $0.001/M floor as the weekly reset approaches. Off-peak hours halve it. |
| **T2** | balance | `neuralwatt` (prepaid) | `base_rate × (1 + depletion_penalty) × NW_CORRECTION_FACTOR` — depletion penalty ramps as the balance drains; a 3.6× token-overcounting correction is applied. |
| **T3** | flat | `opencode_go` ($10/mo) | `MIN_EFFECTIVE_PRICE` floor ($0.001/M) plus scarcity: `0.001 × (1 + scarcity + burn_share × scarcity × BURN_PREMIUM)` — marginal cost ≈ $0 but session-quota scarcity prices it out as it fills. |
| **T4** | included | `ollama_cloud`, `ollama_cloud_2` | Same $0.001/M floor + session/weekly quota scarcity; paywall (429) → ∞. |
| **T5** | per-token | `deepinfra`, `ppq`, `telnyx`, `openrouter`, `routstr`, `routstrd` | `base_rate` straight from the Kalman filter — the market price, discovered by measurement. |

Global floor: $0.001/M for T2–T5 (T1 time-decay may go below it, never to 0).

## Quick start

### 1. Install dependencies

The proxy itself (`flat_router.py` + `production/zai_proxy.py`) uses **only
the Python standard library** (`http.server`, `urllib.request`, `sqlite3`,
`threading`). The Kalman modules in `src/` additionally use `numpy`, and the
test suite needs `pytest`:

```bash
python3 -m pip install --user numpy pytest   # Python 3.10+ recommended
# optional: pyyaml — only needed for the oxalpha promo tier (see Step 3);
# without it that tier is silently disabled, everything else still works.
python3 -m pip install --user pyyaml
```

### 2. Copy the runtime files to a host directory

In production the proxy, router, and config live side by side in one directory
(`~/.hermes/bot/` in the reference deployment), with the repo's `src/` modules
on `PYTHONPATH` (the path bootstrap in `flat_router.py` handles both layouts —
repo checkout or deployed host — automatically):

```bash
mkdir -p ~/.hermes/bot/config
cp flat_router.py ~/.hermes/bot/
cp production/zai_proxy.py ~/.hermes/bot/
cp config/providers.yaml ~/.hermes/bot/config/
# keep this repo checked out at ~/merchant-routing-engine (src/ is imported
# from there), or copy src/price_kalman.py + src/consumption_kalman.py next
# to the proxy and put that dir on PYTHONPATH.
```

> **⚠️ Safety — do NOT overwrite a live proxy.** On the reference machine
> `~/.hermes/bot/` already holds a **running production proxy**; copying over
> it would clobber the live service. If you are reproducing on a machine that
> already runs one, use a **sandbox HOME** instead so nothing running is
> touched:
>
> ```bash
> export HOME="$HOME/sandbox-home"          # isolated HOME for this repro
> mkdir -p "$HOME/.hermes/bot/config"
> cp flat_router.py "$HOME/.hermes/bot/"
> cp production/zai_proxy.py "$HOME/.hermes/bot/"
> cp config/providers.yaml "$HOME/.hermes/bot/config/"
> ```
>
> All subsequent steps in this guide use `~/.hermes/bot/`; if you set a sandbox
> `HOME`, substitute `$HOME/.hermes/bot/` everywhere below (keys, flags, state
> file, and the sqlite DB all live under that directory).

### 3. Provide your keys (names only — values are yours)

The proxy loads keys from the first of `~/.hermes/profiles/manager/.env`,
`~/.hermes/.env`, `~/.hermes/bot/.env` that exists. Provide only the entries
for providers you actually have (all others are simply absent from the
market). Names mirror the `key_env` fields in `config/providers.yaml`:

```bash
cat >> ~/.hermes/bot/.env <<'EOF'
ZAI_OUR_KEY=...            # z.ai coding-plan key ("ours")
ZAI_API_KEY=...            # second z.ai key ("friend")
OLLAMA_CLOUD_API_KEY=...   # ollama.com subscription
OLLAMA_CLOUD_API_KEY_2=...
OPENCODE_GO_API_KEY=...
NEURALWATT_API_KEY=...
DEEPINFRA_API_KEY=...
DEEPINFRA_STARTING_BALANCE=10.0
PPQ_API_KEY=...
OPENROUTER_API_KEY=...
TELNYX_API_KEY=...
TELNYX_STARTING_BALANCE=16.0
ROUTSTR_API_KEY=...        # + ROUTSTR_BASE=... (Nostr merchant endpoint)
ROUTSTRD_API_KEY=...       # + ROUTSTRD_BASE=...
OPENROUTER_OXALPHA_KEY=... # optional promo key
EOF
chmod 600 ~/.hermes/bot/.env
```

Optional tuning flags (booleans, see the systemd unit below):
`ZAI_QUOTA_PRESSURE_ENABLED`, `OLLAMA_QUOTA_PRESSURE_ENABLED`,
`OLLAMA_EXTRA_USAGE_ENABLED`, `LIVE_ROUTER_DYNAMIC_RATES_ENABLED`.

### 4. Run it (systemd user unit)

Reference unit (as deployed):

```ini
# ~/.config/systemd/user/zai-proxy.service
[Unit]
Description=z.ai key-rotation proxy (ContextVM pattern)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
ExecStart=%h/.hermes/hermes-agent/venv/bin/python %h/.hermes/bot/zai_proxy.py
Environment=HOME=%h
Environment=ZAI_QUOTA_PRESSURE_ENABLED=true
Environment=OLLAMA_QUOTA_PRESSURE_ENABLED=true
Environment=OLLAMA_EXTRA_USAGE_ENABLED=true
Environment=LIVE_ROUTER_DYNAMIC_RATES_ENABLED=true
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

> **Note for strangers.** The `ExecStart` above points at the reference
> machine's deployment venv (`%h/.hermes/hermes-agent/venv/bin/python`), which
> you will not have. The **canonical way to run this on a fresh box is the
> direct-run alternative** below (`python3 ~/.hermes/bot/zai_proxy.py`), using
> whatever Python 3.10+ interpreter you installed the deps into. The systemd
> unit is shown for reference-box parity only; adapt the `ExecStart` path to
> your own venv if you want to run it as a service.

```bash
systemctl --user daemon-reload
systemctl --user enable --now zai-proxy.service
# or just run it directly:
python3 ~/.hermes/bot/zai_proxy.py     # listens on 127.0.0.1:9099
```

**Port override.** The proxy listens on `127.0.0.1:9099` by default. To run a
second instance alongside a live one (or any non-default port), set the `PORT`
environment variable:

```bash
PORT=9199 python3 ~/.hermes/bot/zai_proxy.py   # listens on 127.0.0.1:9199
```

The reference systemd unit does not set `PORT`, so it keeps the 9099 default.

### 5. Smoke test

```bash
# models endpoint
curl -s http://localhost:9099/v1/models | head -c 400; echo

# 5-token chat completion through the market
curl -s http://localhost:9099/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm-4.5-flash","max_tokens":5,
       "messages":[{"role":"user","content":"Say hi in one word"}]}' | head -c 600; echo
```

A 200 with a `choices` array means a provider won the market and served the
request. `curl -sD-` (dump headers) shows `X-Provider: <name>` — which
provider actually served it.

## How it works

**`select_provider()` flow** (`flat_router.py`):

1. **Canonicalize** the requested model ID (short/aliased forms like
   `deepseek-v4-flash` resolve to the provider-neutral canonical ID so *all*
   capable providers compete).
2. **Model filter** — `PROVIDER_MODELS` maps each of the 12 providers to the
   set of models it actually serves (catalog drift is re-verified live; a
   provider that 400s a model is simply never a candidate for it).
3. **Health gate** — excludes providers in backoff, paywalled, circuit-broken
   (>10 consecutive failures), unfunded, or manually disabled
   (`.key_disabled_<name>` flag).
4. **Effective price** — for each survivor, compute the tier-specific
   effective $/M (formula above + tier table).
5. **Sort cheapest-first** and return the ordered candidate list (never
   empty — worst case a `fallback` candidate with cost ∞).

**Dispatch loop** (`production/zai_proxy.py`, flat-router path): iterate the
candidates, call each one's `dispatch_fn`; on success, update that provider's
price + burn Kalman filters (`_update_kalman_after_request()`) and log the
spend; on failure, mark the failure (health factor rises) and try the next
candidate; if all fail, 503.

## Rollback / kill switches

Flag files in the host directory (`~/.hermes/bot/`) — create to activate,
delete to restore:

| Flag | Effect |
|---|---|
| `.disable_flat_router` | Master rollback: reverts the proxy to the legacy `best_key()` + hardcoded failover-chain path (kept intact in `zai_proxy.py` for exactly this purpose). |
| `.disable_time_decay` | Turns off T1 time-decay (z.ai keys priced flat at the floor instead of decaying toward reset). |
| `.disable_depletion_penalty` | Turns off the T2 NeuralWatt balance-depletion penalty. |
| `.key_disabled_<name>` | Manually removes one provider from the market (e.g. `.key_disabled_ours`) without touching any other. |
| `.enable_live_routing` | Legacy flag for the pre-flat external-failover path; no longer required — the flat router is always live when `.disable_flat_router` is absent. |

The planned `.disable_live_routing` / `.disable_intake_overlay` switches from
the model-intake staging plan (2026-08-31) are **not** part of this snapshot —
they land with a later phase.

## Verification

**Run the router's test suite in-repo** (73 tests: model filtering, health
gating, cost ordering, Kalman updates, canonicalization, rollback):

```bash
cd merchant-routing-engine
python3 -m pytest test_flat_router.py -q     # → 73 passed
```

**Failure-injection recipe** — prove the market actually re-routes when the
cheapest provider is removed:

```bash
# 0. baseline: who serves glm-4.5-flash right now?
curl -sD- -o /dev/null http://localhost:9099/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm-4.5-flash","max_tokens":5,
       "messages":[{"role":"user","content":"hi"}]}' | grep -i x-provider
#   → e.g. X-Provider: zai:ours   (cheapest healthy candidate)

# 1. take the winner off the market
touch ~/.hermes/bot/.key_disabled_ours

# 2. same request — market re-routes to the next-cheapest healthy provider
curl -sD- -o /dev/null http://localhost:9099/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm-4.5-flash","max_tokens":5,
       "messages":[{"role":"user","content":"hi"}]}' | grep -i x-provider
#   → X-Provider changes (e.g. zai:friend), request still 200

# 3. the ordered candidate list is in the key_decisions table (sqlite):
python3 - <<'EOF'
import sqlite3, time, os
db = os.path.expanduser("~/.hermes/bot/zai_usage.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
for ts, key, reason in con.execute(
        "SELECT ts, chosen_key, reason FROM key_decisions "
        "ORDER BY ts DESC LIMIT 6"):
    print(f"{time.strftime('%H:%M:%S', time.localtime(ts))}  "
          f"chosen={key:8s}  {reason}")
con.close()
EOF
#   → reason="flat_router: friend -> ollama_cloud -> ..." (ours absent)

# 4. restore
rm ~/.hermes/bot/.key_disabled_ours
```

The response body's `model` field echoes the model the winning provider
actually served; the `X-Provider` header and the `flat_router: a -> b -> c`
row in the `key_decisions` table are the authoritative routing record.

## Relationship to routstr

[routstr-core](https://github.com/felixfelix-bot/routstr-core) (GPL-3.0) is
the **sell side** of this market: merchant LLM inference advertised and paid
over Nostr (sats per token, Cashu ecash). This repo is the **buy side**: the
market engine that measures every provider's true price — routstr endpoints
(`routstr`, `routstrd`) compete in the same candidate list as z.ai, ollama
cloud, and the per-token aggregators, priced by the same Kalman filters, and
win traffic only when they are actually the cheapest healthy option.

---

*API keys are never committed to this repo; they are read from `.env` files at
runtime. See `.gitignore` — `.env`, `*.key`, `secrets.yaml` are excluded.*
