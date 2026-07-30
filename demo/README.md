# Sovereign Engineering Demo — Dashboard Server (Task A1)

Node.js 22 + `node:sqlite` HTTP + WebSocket server for the Sovereign Engineering
demo. Serves live Kalman token-pricing + demand-economics data by reading the
z.ai usage DB directly (fast, <50ms), the Kalman price-state JSON, `/proc`, and
the running z.ai proxy (`:9099`) for live quota + dispatch-gate signals. Demo
prompts route through the proxy and are token-charged via the shared
token-ledger (Task A3, `src/token-ledger.mjs`).

**Spec:** `docs/PLAN-sovereign-demo.md` §A1
**API contract:** `demo/API-CONTRACT.md` (authoritative for shapes)

## Quick start

```bash
# Prerequisites: Node.js ≥22, the z.ai proxy running on :9099
node demo/src/dashboard-server.mjs                 # → http://localhost:3001

# Run the gate suite (server must be running)
node demo/tests/verify-a1-gates.mjs                # A1 gates (39 checks)
node demo/tests/verify-gates.mjs                   # A3 token-ledger gates (53 checks)
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Server health + proxy status |
| GET | `/api/snapshot` | All dashboard data in one JSON (<50ms) |
| GET | `/ledger` | Token-economy participants + scarcity |
| GET | `/ledger/recent` | Recent ledger transactions |
| POST | `/register` | Register a whitelisted npub → token budget |
| POST | `/prompt` | Route a prompt via the proxy + charge tokens |
| POST | `/admin/whitelist` | Add an npub to the whitelist (password) |
| POST | `/reset` | Clear all participants (demo restart) |
| WS | `/stream` | Live request-event push (polls DB every 2s) |

## Configuration (env vars)

| Var | Default | Description |
|-----|---------|-------------|
| `PORT` | `3001` | Listen port |
| `HOST` | `0.0.0.0` | Listen address |
| `ZAI_USAGE_DB` | `/home/c03rad0r/.hermes/bot/zai_usage.db` | z.ai usage DB |
| `BURN_DB` | `/home/c03rad0r/.hermes/bot/api_burn.db` | Balance/spend DB |
| `KALMAN_STATE` | `/home/c03rad0r/.hermes/bot/kalman_price_state.json` | Kalman state |
| `PROXY_URL` | `http://localhost:9099` | z.ai proxy base URL |
| `DEMO_MODEL` | `glm-4.5-flash` | Model for demo prompts |
| `FLAT_KEY_COST_PER_M` | `0.02` | $/M cost basis for flat-rate keys |
| `OLLAMA_MONTHLY_USD` | `100.0` | Monthly flat fee for ollama_cloud |
| `MARGIN` | `0.30` | Fractional markup → your_price |
| `STARTING_BALANCE` | `50000` | Tokens granted on registration |
| `RATE_LIMIT_MS` | `5000` | 1 prompt / 5s / npub |

## Architecture

```
Browser ──HTTP/WS──→ Dashboard Server (:3001)
                       ├── zai_usage.db (read-only, direct SQLite)
                       ├── api_burn.db (read-only, ppq spend)
                       ├── kalman_price_state.json (converged rates)
                       ├── /proc (system stats fallback)
                       └── z.ai proxy :9099 (/quota, /v1/dispatch_gate)
```

The snapshot endpoint serves from an in-memory aggregate cache refreshed every
8s in the background, keeping request-path latency well under the 50ms gate.
Expensive 1h/7d/30d scans run off the request path. The WebSocket stream polls
the DB every 2s and pushes new `key_decisions` rows as they arrive.

## Known constraint: port 3001

A stale `kalman-dashboard.service` (systemd --user) may hold :3001. Before
starting this server on :3001, stop it:

```bash
systemctl --user stop kalman-dashboard.service
```

Until then, run on an alternate port: `PORT=3003 node demo/src/dashboard-server.mjs`