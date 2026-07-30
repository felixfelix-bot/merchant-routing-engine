# Dashboard API Contract

**Owner:** Task A2 (frontend) → defines what A1 (server) must serve.
**Status:** Authoritative for the HTML in `public/index.html`.

The dashboard polls these endpoints. Every field has a graceful fallback
(see DEMO MODE below), so the page renders even if a field is missing or the
server is down. **A1 should aim to fill these, but missing fields never break
the UI.**

---

## `GET /api/snapshot`  (poll every 5s, must be <50ms)

Single JSON blob with everything the read-only panels need.

```jsonc
{
  "ts": 1785430943,                      // server unix seconds
  "pricing": {                           // per-key economics (Panel 1)
    "ours":   { "cost_basis": 0.077, "your_price": 0.120, "margin_pct": 35.8, "effective_rate": 0.120 },
    "friend": { "cost_basis": 0.094, "your_price": 0.130, "margin_pct": 27.7, "effective_rate": 0.130 },
    "ollama": { "cost_basis": 0.050, "your_price": 0.090, "margin_pct": 44.4, "effective_rate": 0.090 },
    "ppq":    { "cost_basis": 0.140, "your_price": 0.200, "margin_pct": 30.0, "effective_rate": 0.200 }
    // units: $/M tokens. margin_pct = (your_price-cost_basis)/your_price*100
  },
  "pricing_history": {                   // OPTIONAL. If absent, client accumulates
                                         // from successive `pricing` snapshots.
    "ours": [ { "t": 1785430000, "cost_basis": 0.080, "your_price": 0.122, "margin_pct": 34.4 }, ... ],
    "friend": [ ... ], "ollama": [ ... ], "ppq": [ ... ]
  },
  "quota": {                             // Panel 3 — animated bars
    "ours":   { "used_pct": 45.0, "remaining": 1100000, "total": 2000000, "healthy": true,  "resets_in_min": 180 },
    "friend": { "used_pct": 30.0, "remaining": 1400000, "total": 2000000, "healthy": true,  "resets_in_min": 180 }
    // remaining/total in tokens. resets_in_min = minutes until 5h window rolls.
  },
  "requests": [                          // Panel 4 — newest first, last ~20
    { "ts": 1785430900, "requester": "npub1abc…", "provider": "ours", "model": "glm-5.2", "tokens": 1234, "cost": 0.00015, "reason": "sufficient headroom" }
  ],
  "provider_distribution": {             // Panel 5 — share of requests, last 1h
    "ours": 0.62, "friend": 0.20, "ollama": 0.12, "ppq": 0.06
    // values are fractions 0..1 summing to ~1
  },
  "dispatch_gate": {                     // Panel 7
    "can_dispatch": true,
    "reason": "sufficient headroom (ours key) with 2x margin",
    "recommended_model": "glm-5.2",
    "effective_price_per_m": 0.003,
    "scarcity_factor": 1.0,
    "safety_margin": 2.0,
    "hours_until_exhaustion": { "ours": 4.5, "friend": 8.2 },
    "downgraded": false
  },
  "cost_today": 2.34,                    // Panel 6 — total $ spent today
  "burn_rate_per_hour": 0.12,            // Panel 6 — $/hour
  "system": { "cpu_pct": 12.0, "mem_pct": 45.0 }  // OPTIONAL
}
```

### Field tolerance
- Any top-level key may be omitted → panel shows last-known or demo value.
- `pricing_history` is OPTIONAL: if omitted, the client builds history by
  appending each `pricing` snapshot it receives (rolling 500-point window).
- `requests` may be empty array.

---

## `GET /ledger`  (poll every 5s — Panel 2 token economy)

```jsonc
{
  "participants": [
    { "npub": "npub1abc…def", "balance": 48000, "spent": 2000, "prompts": 3 }
  ],
  "current_price_per_token": 0.0000034,  // $/token, scarcity-adjusted
  "scarcity_factor": 1.2,                // 1.0..2.0
  "starting_balance": 50000,
  "total_budget": 500000,                // sum across all participants
  "total_consumed": 42000                // tokens spent so far
}
```

---

## `POST /register`  (Panel 2)

Request:  `{ "npub": "npub1…" }`
Response (200): `{ "ok": true, "balance": 50000, "npub": "npub1…" }`
Response (403): `{ "ok": false, "error": "not authorized for this demo" }`
Response (409): `{ "ok": false, "error": "already registered", "balance": 48000 }`

---

## `POST /prompt`  (Panel 2)

Request:  `{ "prompt": "...", "requester_npub": "npub1…" }`
Response (200):
```jsonc
{ "ok": true, "provider": "ours", "model": "glm-5.2", "tokens": 1234,
  "cost": 0.00015, "price_per_m": 0.120, "balance_after": 46766 }
```
Response (402): `{ "ok": false, "error": "insufficient tokens" }`
Response (429): `{ "ok": false, "error": "rate limit: wait Ns" }`

---

## `WS /stream`  (optional live push — Panel 4)

Server pushes one message per new request (polls DB every 2s):
```jsonc
{ "type": "request", "data": { "ts": ..., "requester": "...", "provider": "...", "model": "...", "tokens": ..., "cost": ..., "reason": "..." } }
```
The client also polls `/api/snapshot` every 5s as a fallback, so WS is a
latency optimisation, not a hard requirement.

---

## DEMO MODE (graceful fallback)

When any fetch fails (server down, network error, timeout), the dashboard
switches to DEMO MODE: it generates realistic synthetic data locally so all 7
panels keep animating. A small "DEMO MODE" badge appears top-right. This makes
the dashboard:
- demoable before A1 is wired,
- resilient to proxy/venue-network failure,
- a faithful preview of the real thing.

The moment the server comes back, DEMO MODE turns off silently.
