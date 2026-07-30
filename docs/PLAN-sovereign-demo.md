# PLAN: Sovereign Engineering Demo Dashboard (v2)

**Created:** 2026-07-30 (v2: incorporates Felix's feedback)
**Demo:** Sovereign Engineering (tomorrow)
**Goal:** Interactive live demo showing Kalman token pricing + demand economics

---

## WHAT THE AUDIENCE SEES

A web dashboard (big screen + accessible on phones). They register with their npub, get a token budget, send prompts, watch the price respond to demand in real-time. CVM/Nostr runs underneath but is invisible — mentioned verbally only.

---

## ARCHITECTURE

```
┌─────────────────────────────────────────────────────────┐
│                    DEMO BROWSER                          │
│  (Big screen + audience phones via shared URL)           │
│                                                          │
│  ┌───────────────┐  ┌─────────────────────────────────┐ │
│  │ PER-KEY       │  │ TOKEN ECONOMY (main demo focus) │ │
│  │ PRICE CHARTS  │  │                                 │ │
│  │ (one per key) │  │ Register (npub) → get budget    │ │
│  │               │  │ Send prompt → spend tokens      │ │
│  │ Shows:        │  │ Watch price tick up with demand │ │
│  │ - cost basis  │  │ See your balance drain          │ │
│  │ - your price  │  │ Leaderboard: who spent what     │ │
│  │ - margin %    │  │                                 │ │
│  └───────────────┘  └─────────────────────────────────┘ │
│  ┌───────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ QUOTA BARS    │  │ REQUEST FLOW │  │ COST METER   │ │
│  │ (per key)     │  │ (live cards) │  │ ($ today)    │ │
│  └───────────────┘  └──────────────┘  └──────────────┘ │
│  ┌───────────────┐  ┌──────────────┐                   │
│  │ DISPATCH GATE │  │ PROVIDER     │                   │
│  │ (green/red)   │  │ DISTRIBUTION │                   │
│  └───────────────┘  └──────────────┘                   │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP/WebSocket (direct, <50ms)
                       ▼
┌─────────────────────────────────────────────────────────┐
│              DASHBOARD SERVER (:3001)                     │
│  Node.js 22 + node:sqlite (reads DB file directly)       │
│  No Nostr round-trips for dashboard data                 │
│                                                          │
│  Reads directly from:                                    │
│  ├── zai_usage.db (routing, telemetry, cost)            │
│  ├── /proc (live system stats)                          │
│  ├── proxy /quota (live key status, every 2s)           │
│  ├── proxy /v1/dispatch_gate (gate decisions)           │
│  └── kalman_price_state.json (converged rates)          │
│                                                          │
│  Serves:                                                 │
│  ├── GET / → dashboard HTML                             │
│  ├── GET /api/snapshot → all data in one JSON (<50ms)   │
│  ├── WS /stream → live request events (2s poll)         │
│  ├── POST /register → whitelist + create participant    │
│  └── POST /prompt → route prompt, deduct tokens         │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                 Z.AI PROXY (:9099)                       │
│  (already running — no changes needed)                   │
│  LiveRouter + ShadowHook + PriceKalman                   │
└─────────────────────────────────────────────────────────┘
```

---

## OPTION A: LOCAL DASHBOARD (MUST-HAVE)

### Task A1: Dashboard Server (worker-merchant, glm-5.2)

**Scope:** Node.js HTTP + WebSocket server on localhost:3001

**Deliverables:**
- `src/dashboard-server.mjs` — serves HTML + API + WebSocket
- `GET /api/snapshot` returns single JSON with:
  - Live quota status per key (from proxy /quota)
  - Recent routing decisions (last 50 from zai_usage.db)
  - Provider distribution (last 1h aggregate)
  - Kalman converged rates per provider (from kalman_price_state.json)
  - Per-key pricing: cost_basis, your_price, margin_pct, effective_rate
  - System stats (from /proc)
  - Scarcity factor (from dispatch gate endpoint)
  - Cost today / total (from daily_spend table)
- `POST /prompt` — accepts {prompt, requester_npub}, routes through proxy, returns {provider, model, cost, tokens, price_per_m}
- `WS /stream` — pushes new routing events as they arrive (poll DB every 2s)
- Demo ledger stored in new SQLite table `demo_ledger`

**Quality Gates:**
- [ ] Server starts without errors on localhost:3001
- [ ] /api/snapshot returns valid JSON in <50ms
- [ ] POST /prompt routes through proxy and returns provider/model/cost
- [ ] WS /stream pushes events within 3s of DB insert
- [ ] Cold review (Gate 2.5)

### Task A2: Dashboard HTML + Charts (worker-merchant, glm-5.2)

**Scope:** Single-page dark-themed dashboard with live charts

**Deliverables:**
- `public/index.html` — self-contained, local Plotly.js (downloaded, not CDN — works offline), dark theme
- Layout: responsive grid, works on phone (Bootstrap or simple CSS grid)

**Panels:**

**Panel 1: Per-Key Price Charts (PRIORITY)**
- ONE separate chart per key (ours, friend, ollama_cloud, ppq)
- Each chart shows 3 lines over time:
  - Cost basis (what Felix pays upstream): solid line
  - Your price (what you charge, with margin): dashed line
  - Margin % (right axis): filled area
- X-axis: last 24h (historical) + live updates
- Y-axis: $/M tokens
- This makes the economic story visible: "Here's what I pay, here's what I charge, here's my margin"

**Panel 2: Token Economy (MAIN DEMO FOCUS)**
- Registration form: npub input + "Join" button
- Current participant list with balances
- Prompt input: text field + "Send" button (shows cost preview at current price)
- Leaderboard: who sent most prompts, who spent most tokens
- Live balance display for logged-in participant
- "Token price" indicator: current $/M tokens, updated live as demand shifts

**Panel 3: Quota Bars**
- Two bars (ours + friend key), animated fill
- Color: green (<50%) → yellow (50-80%) → red (>80%)
- Shows: exact %, tokens remaining, time to reset

**Panel 4: Request Flow (live stream)**
- Vertical scrolling cards, newest on top
- Each card: timestamp, requester (if demo), provider, model, tokens, cost, reason
- Last 20 visible, older scroll away

**Panel 5: Provider Distribution**
- Donut chart, last 1h
- ours vs friend vs ollama vs ppq vs openrouter
- Updates every 10s

**Panel 6: Cost Meter**
- Big number: total $ spent today
- Subtext: $/hour burn rate
- Ticks up live with each request

**Panel 7: Dispatch Gate Status**
- Visual indicator: green light (can dispatch) or red light (hold)
- Shows: safety_margin, recommended_model, reason

**Quality Gates:**
- [ ] All 7 panels render correctly on first load
- [ ] Per-key price charts show correct 3 lines (cost/price/margin)
- [ ] Token economy: register → send prompt → balance updates → price responds
- [ ] Data refreshes every 5s without page reload
- [ ] Works on phone screen (responsive)
- [ ] Plotly.js loaded locally (offline-capable)
- [ ] Cold review (Gate 2.5)

### Task A3: Token Economy + Access Control (worker-merchant, glm-5.2)

**Scope:** The core demo mechanic — npub-gated participants with token budgets

**Deliverables:**
- `src/token-ledger.mjs` — participant management

**Access control:**
- `POST /register` — accepts {npub}
  - Checks npub against whitelist file (`demo-whitelist.json`)
  - If whitelisted: creates participant with starting balance (configurable, default 50,000 tokens)
  - If not whitelisted: returns error "not authorized for this demo"
  - Prevents re-registration (one balance per npub)

- Whitelist management:
  - `demo-whitelist.json` — array of npubs allowed to participate
  - `POST /admin/whitelist` — add npub to whitelist (password protected)
  - Felix pre-loads whitelist before demo or adds people live during demo

**Token economy:**
- Each prompt deducts: `token_cost = estimated_tokens × current_price_per_token`
- Current price derived from: Kalman base rate × scarcity_factor
- Scarcity factor ramps as aggregate demo consumption increases:
  - <20% of total demo budget consumed: scarcity = 1.0x
  - 20-40%: scarcity = 1.2x
  - 40-60%: scarcity = 1.5x
  - 60-80%: scarcity = 1.8x
  - >80%: scarcity = 2.0x
- Insufficient balance → returns error "insufficient tokens"
- `GET /ledger` — returns all participants sorted by spend
- `POST /reset` — clears all participants (demo restart)

**Quality Gates:**
- [ ] Whitelist correctly rejects non-whitelisted npubs
- [ ] Register creates participant with correct starting balance
- [ ] Prompt deducts correct token amount at current scarcity-adjusted price
- [ ] Scarcity factor visibly increases as more prompts sent (chart shows this)
- [ ] Insufficient balance correctly refused
- [ ] Cold review (Gate 2.5)

### Task A4: Integration Test + Demo Polish (worker-inspector, glm-5.2)

**Scope:** End-to-end verification + demo readiness

**Deliverables:**
- Verify full flow: whitelist npub → register → send prompt → see in request flow → see cost → see quota drop → see price tick up → see balance drain
- Test from phone (shared URL via local network or Tailscale)
- Performance test: ensure <100ms API response, <5s prompt round-trip
- Edge cases:
  - What if proxy down? (graceful error in UI)
  - What if quota exhausted mid-demo? (price maxes out, prompts still route to flat-rate)
  - What if someone spams prompts? (rate limit: 1 prompt per 5s per npub)
- Demo script: 2-minute talking points for Felix
- Plotly.js downloaded locally for offline reliability

**Quality Gates:**
- [ ] Full demo flow works end-to-end from phone
- [ ] Per-key price charts show correct data
- [ ] Scarcity ramp visible when sending multiple prompts in sequence
- [ ] Rate limiting works
- [ ] Graceful error handling for proxy down
- [ ] Demo script written
- [ ] Cold review (Gate 2.5)

---

## OPTION B: VPS ROUTER NODE (STRETCH — ONLY AFTER FELIX APPROVES A)

**Gate:** Option A must be demo-ready AND Felix has personally tested it and deemed it ready. Only then start B.

### Task B1: VPS Provisioning (worker-admin, glm-5.2)
- Provision VPS
- Install Node.js 22
- Setup Tailscale for access
- Copy dashboard server

### Task B2: Cashu Mint + Token Gateway (worker-merchant, glm-5.2)
- Setup Cashu mint on VPS
- Wrap proxy endpoint with Cashu verification
- Each API request requires valid ecash token
- Token amount = current Kalman price × estimated tokens
- Mint verifies + burns token, forwards request, returns response
- Dashboard shows Cashu payments in the ledger

### Task B3: Public Dashboard + Pricing Feed (worker-merchant, glm-5.2)
- Deploy dashboard on VPS (accessible URL)
- NIP-78 events publishing live pricing data (public,任何人 can subscribe)
- Historic price chart from NIP-78 data
- CVM server running underneath (invisible, mentioned verbally)

### Task B4: Integration + Demo (worker-inspector, glm-5.2)
- End-to-end: pay Cashu → get tokens → send prompt → see response
- Verify from separate machine
- Performance test
- Fallback: if B fails, use A

---

## EXECUTION ORDER

```
Phase 1 (Option A — parallel):
  A1 (server)  ──┐
  A2 (html+charts) ──┤
  A3 (token econ) ──┼── A4 (integration + polish) ── FELIX APPROVAL GATE
  A4 depends on A1+A2+A3          ↑
                         Option A demo-ready

Phase 2 (Option B — sequential, ONLY after Felix approves A):
  B1 → B2 → B3 → B4
```

A1+A2+A3 dispatched in parallel. A4 waits for all three.

**Estimated time:**
- Phase 1: 3-4 hours (parallel dispatch)
- Phase 2: 3-4 hours (sequential, only if approved)

---

## TECH DECISIONS

1. **Node.js 22 + node:sqlite** — standard runtime, built-in SQLite, no compile issues
2. **Plotly.js downloaded locally** — works offline at venue (no CDN dependency)
3. **Direct SQLite read for dashboard** — dashboard server on same machine as DB, reads file directly (<50ms). No Nostr round-trips needed for dashboard data. CVM runs underneath but is invisible to audience.
4. **Access via npub whitelist** — only authorized npubs can participate. Prevents resource waste.
5. **Rate limiting** — 1 prompt per 5s per npub to prevent spam
6. **CVM invisible** — audience never sees cvmi or CVM internals. Felix mentions ContextVM/Nostr verbally as the transport layer if asked.

## DEMO NARRATIVE (for Felix)

1. "This is my API gateway. Every request routes through it." → show request flow
2. "The Kalman filter learns real costs per provider and prices accordingly." → show per-key price charts
3. "Here's my cost basis, my price, and my margin per key." → point to the 3 lines on each chart
4. "You can participate — register your npub and send a prompt." → audience joins, sends prompts
5. "Watch what happens to the price as demand increases." → scarcity factor ramps, price ticks up
6. "This isn't slides. These are real API calls, real money, real routing decisions." → cost meter climbing
7. (If asked about architecture): "The whole thing runs over Nostr. My infrastructure is addressable by public key, not IP."

## RISKS

1. **z.ai quota exhaustion mid-demo** — mitigated: npub whitelist + limited budget + rate limiting
2. **Proxy crash** — mitigated: restart script ready, dashboard shows graceful error
3. **Venue network down** — mitigated: Plotly local, phone hotspot backup, dashboard works on LAN
4. **Random strangers** — mitigated: npub whitelist, no open registration
