# PLAN: Sovereign Engineering Demo — CVM-Only Architecture (v3)

**Created:** 2026-07-30 (v3: all-CVM, no direct DB access)
**Demo:** Sovereign Engineering (tomorrow)
**Goal:** Interactive live demo where audience participates via Nostr-addressed infrastructure

---

## CORE PRINCIPLE: EVERYTHING VIA CONTEXTVM

DQ05 (Kalman machine) is remote — different network, only reachable via Nostr. No direct SQLite, no HTTP, no Tailscale fallback. All data flows as CVM tool calls over Nostr relays. Browser JS maintains persistent relay connections for speed.

---

## ARCHITECTURE

```
┌─────────────────────────────────────────────────────────────┐
│ DISPLAY LAPTOP (big screen, at venue)                        │
│                                                              │
│  DISPLAY NSITE (static HTML, deployed via nsyte)             │
│                                                              │
│  ┌──────────────┐  ┌─────────────────────────────────────┐  │
│  │ QR CODE      │  │ SYSTEM DIAGRAM                      │  │
│  │ (scan to     │  │ (live boxes: DQ05, proxy, relays,   │  │
│  │  join demo)  │  │  participant phones — pulsing)      │  │
│  └──────────────┘  └─────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ PER-KEY PRICE CHARTS (4 charts, one per key)          │  │
│  │ Each: cost basis (solid), your price (dashed),        │  │
│  │       margin % (filled area, right axis)              │  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌──────────────┐ ┌──────────────┐ ┌────────────────────┐  │
│  │ QUOTA BARS   │ │ COST METER   │ │ REQUEST FLOW       │  │
│  │ (per key,    │ │ ($ today,    │ │ (live scrolling    │  │
│  │  live)       │ │  ticks up)   │ │  cards, last 20)   │  │
│  └──────────────┘ └──────────────┘ └────────────────────┘  │
│  ┌──────────────┐ ┌──────────────────────────────────────┐ │
│  │ DISPATCH     │ │ TOKEN ECONOMY SUMMARY               │ │
│  │ GATE STATUS  │ │ (total participants, total prompts,  │ │
│  │ (green/red)  │ │  average price, scarcity level)      │ │
│  └──────────────┘ └──────────────────────────────────────┘ │
│                                                              │
│  JS ENGINE: nostr-tools, persistent relay connections       │
│  POLLING: get_snapshot via CVM every 5s (reuses open WS)    │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        │ CVM tool calls (kind 25910 + gift wrap)
                        │ Persistent relay WebSockets to:
                        │   wss://nostr.mom
                        │   wss://relay.primal.net
                        │   wss://nos.lol
                        │
           ┌────────────┴────────────────────────────────────┐
           │                                                 │
           ▼                                                 ▼
┌──────────────────────────────┐    ┌──────────────────────────────────┐
│ DQ05 (Kalman machine, remote) │    │ PARTICIPANT PHONE                │
│                               │    │ (via QR code → participant nsite) │
│ CVM SERVER (bun + nostr-tools)│    │                                   │
│                               │    │ PARTICIPANT NSITE                 │
│ Tools:                        │    │                                   │
│ • get_snapshot → all data     │    │ ┌─────────────┐ ┌──────────────┐ │
│ • send_prompt → route prompt  │◄───┼─│ NIP07/nsec  │ │ TOKEN        │ │
│ • register_participant        │    │ │ bunker login│ │ BALANCE      │ │
│ • get_ledger                  │    │ └─────────────┘ └──────────────┘ │
│ • get_price_history           │    │ ┌──────────────────────────────┐ │
│ • get_whitelist               │    │ │ PROMPT INPUT                 │ │
│                               │    │ │ "Type a prompt..."           │ │
│ Reads from:                   │    │ │ [Send] → CVM send_prompt     │ │
│ • zai_usage.db (direct local) │    │ └──────────────────────────────┘ │
│ • /proc                       │    │ ┌──────────────────────────────┐ │
│ • proxy /quota                │    │ │ YOUR SPEND                   │ │
│ • proxy /dispatch_gate        │    │ │ (tokens used, cost, prompts) │ │
│ • kalman_price_state.json     │    │ └──────────────────────────────┘ │
│                               │    │                                   │
│ Manages:                      │    │ JS: nostr-tools, persistent       │
│ • demo_ledger table           │    │ relay connection                  │
│ • demo-whitelist.json         │    │ CVM calls reuse open WebSocket    │
│ • Scarcity factor calc        │    │                                   │
│                               │    │                                   │
│ PROXY (:9099)                 │    │                                   │
│ Kalman + LiveRouter + z.ai    │    │                                   │
└───────────────────────────────┘    └──────────────────────────────────┘
```

---

## DATA FLOW — ALL VIA CVM

### Display Dashboard Refresh (every 5s)
1. Display nsite JS calls CVM `get_snapshot` tool
2. DQ05 CVM server reads DB locally, returns JSON snapshot
3. Browser receives response on persistent relay WebSocket
4. With persistent connections: ~2-3s per call (vs 5-10s cold)

### Participant Sends Prompt
1. Participant nsite JS calls CVM `send_prompt` with {prompt, npub}
2. DQ05 CVM server:
   a. Checks whitelist
   b. Checks token balance
   c. Routes through proxy localhost:9099
   d. Deducts tokens at current scarcity-adjusted price
   e. Returns {response, provider, model, cost, tokens, new_balance}
3. Browser receives response, shows it + updates balance
4. Next display dashboard refresh picks up the new routing decision

### Participant Registration
1. Participant nsite JS calls CVM `register_participant` with {npub}
2. DQ05 CVM server checks whitelist, creates participant with 50K tokens
3. Returns {balance, welcome message}

### Whitelist Management
1. Felix calls CVM `add_to_whitelist` with {npub, admin_sig} (or edits file directly on DQ05)
2. Or: whitelist published as NIP-78 event from DQ05, participant nsite checks before showing form

---

## CVM TOOL SPECIFICATION

All tools on DQ05 CVM server, addressed by Nostr npub:

### get_snapshot
Returns everything the display dashboard needs in one call:
```
{
  quota: { ours: {used_pct, locked, resets_at}, friend: {...} },
  pricing: { ours: {cost_basis, your_price, margin_pct}, friend: {...}, ollama: {...} },
  cost_today: 12.34,
  cost_hour: 0.56,
  routing_decisions: [last 20: {ts, provider, model, tokens, cost, reason}],
  provider_dist: { ours: 45, friend: 30, ollama: 25 },
  dispatch_gate: { can_dispatch: true, safety_margin: 4.0, reason: "ok" },
  scarcity: { factor: 1.5, level: "moderate", budget_used_pct: 55 },
  system: { cpu: 12, mem: 34, uptime: "5d" },
  participants: { count: 8, total_prompts: 42, total_tokens: 125000 },
  ledger: [top 10: {npub_short, balance, prompts_sent, tokens_spent}]
}
```

### send_prompt
Input: { prompt: string, npub: string }
Output:
```
{
  response: "generated text...",
  provider: "ours",
  model: "glm-5.2",
  tokens_used: 150,
  cost_usd: 0.002,
  price_per_m: 14.50,
  token_cost: 150,
  new_balance: 48500,
  scarcity_factor: 1.5
}
```

### register_participant
Input: { npub: string }
Output: { success: true, balance: 50000, message: "Welcome to the demo!" }
Error: { success: false, error: "npub not whitelisted" }

### get_price_history
Input: { hours: 24 }
Output: [ { ts, key, cost_basis, your_price, margin_pct } ]

### get_ledger
Output: [ { npub_short, balance, prompts_sent, tokens_spent, joined_at } ]

---

## NSITE PAGES

### 1. Display Nsite (display.nsite.lol/...)
The big screen page. Shows:
- QR code (encodes participant nsite URL)
- System diagram (live, animated)
- Per-key price charts (4 Plotly charts)
- Quota bars
- Cost meter
- Request flow stream
- Dispatch gate status
- Token economy summary

Refresh: polls `get_snapshot` via CVM every 5s. Persistent relay connection.

### 2. Participant Nsite (participant.nsite.lol/...)
The phone page. Shows:
- Login (NIP07 browser extension or nsec bunker scan)
- Token balance (live)
- Prompt input + send button
- Your spend stats (prompts sent, tokens used, avg cost)
- Current token price

Refresh: polls `get_snapshot` for balance/price every 5s. Sends prompts via `send_prompt`.

### QR Code Flow
1. Display page shows QR encoding participant nsite URL
2. Participant scans → opens browser → loads participant nsite
3. Nsite asks for Nostr login (NIP07 or nsec bunker)
4. Nsite calls `register_participant` via CVM
5. If whitelisted → shows token balance + prompt form
6. If not whitelisted → "contact Felix to join"

---

## TASKS

### Task A1: CVM Server on DQ05 (worker-merchant, glm-5.2)

**Scope:** bun + nostr-tools CVM server exposing 5 tools
**Location:** ~/merchant-routing-engine/demo/cvm-server/ on DQ05

**Deliverables:**
- `src/cvm-server.ts` — direct nostr-tools implementation (NOT @contextvm/sdk — it hangs per skill pitfalls)
- 5 tools: get_snapshot, send_prompt, register_participant, get_price_history, get_ledger
- get_snapshot reads zai_usage.db directly (server-side, local on DQ05)
- send_prompt routes through proxy localhost:9099, deducts tokens
- Token ledger in demo/demo_ledger.db (SQLite)
- Whitelist in demo/demo-whitelist.json
- Scarcity factor calculation
- Rate limiting: 1 prompt per 5s per npub
- Persistent Nostr key for server (generate once, store in demo/cvm-server-key.json)
- Relays: nostr.mom, relay.primal.net, nos.lol

**Critical pitfalls (from contextvm skill):**
- Use `bun src/cvm-server.ts` NOT `bun run src/cvm-server.ts`
- Client and server MUST use different Nostr keys
- `#p` tag filter doesn't work on some relays for kind 1059 — use broad filter + client-side filtering
- Key file truncation: verify 64 hex chars + newline
- nostr-tools direct implementation, NOT SDK transport

**Quality Gates:**
- [ ] Server starts and subscribes on 3 relays
- [ ] `cvmi call <npub> tool:get_snapshot` returns valid JSON
- [ ] `cvmi call <npub> tool:send_prompt prompt="hi"` routes and returns
- [ ] `cvmi call <npub> tool:register_participant` creates participant
- [ ] Whitelist correctly rejects non-whitelisted npubs
- [ ] Rate limiting works
- [ ] Atomic commit + push
- [ ] Cold review (Gate 2.5)

### Task A2: Display Nsite HTML (worker-merchant, glm-5.2)

**Scope:** Static HTML page deployed via nsyte, CVM browser client

**Deliverables:**
- `display/index.html` — self-contained, dark theme
- Plotly.js downloaded locally (bundled in nsite)
- nostr-tools bundled (minified, in-page or separate JS file)
- CVM browser client (direct nostr-tools, per references/browser-cvm-client.md):
  - Generate/derive client Nostr key (ephemeral per session)
  - Connect to 3 relays with persistent WebSocket
  - Gift-wrap requests, subscribe for responses
  - Correlate request/response
- Polls get_snapshot every 5s via CVM
- **Panel 1: QR Code** — generates QR for participant nsite URL
- **Panel 2: System Diagram** — animated boxes showing DQ05, proxy, relays, participants. Pulsing when active.
- **Panel 3: Per-Key Price Charts** — 4 Plotly charts (ours, friend, ollama, ppq), each 3 lines (cost, price, margin)
- **Panel 4: Quota Bars** — animated, green→red
- **Panel 5: Cost Meter** — big number, ticks up
- **Panel 6: Request Flow** — scrolling cards, last 20
- **Panel 7: Dispatch Gate** — green/red indicator
- **Panel 8: Token Economy Summary** — participant count, total prompts, avg price, scarcity

**Quality Gates:**
- [ ] All 8 panels render on first load
- [ ] CVM get_snapshot call works from browser (persistent relay connection)
- [ ] Per-key charts show correct 3 lines
- [ ] QR code generates correctly
- [ ] Data refreshes every 5s
- [ ] Works offline (Plotly bundled, nostr-tools bundled)
- [ ] Cold review (Gate 2.5)

### Task A3: Participant Nsite HTML (worker-merchant, glm-5.2)

**Scope:** Phone-optimized static HTML page for audience participation

**Deliverables:**
- `participant/index.html` — phone-first, dark theme
- Same CVM browser client as A2 (shared JS)
- **NIP07 login** — checks window.nostr object (Alby, etc)
- **nsec bunker fallback** — QR code scan or manual npub entry
- **Token Balance** — live, updates after each prompt
- **Prompt Input** — text field + Send button, shows cost preview at current price
- **Your Spend** — prompts sent, tokens used, avg cost, member since
- **Current Price** — $/M tokens indicator, updates live
- Calls send_prompt via CVM, shows response inline
- Calls get_snapshot for balance/price refresh (every 5s)

**Quality Gates:**
- [ ] NIP07 login works (or npub manual entry)
- [ ] register_participant creates account
- [ ] send_prompt routes and shows response
- [ ] Balance updates after prompt
- [ ] Cost preview shows before sending
- [ ] Phone-responsive
- [ ] Cold review (Gate 2.5)

### Task A4: Integration + Nsite Deploy + Polish (worker-inspector, glm-5.2)

**Depends on:** A1+A2+A3

**Scope:** Deploy nsites, test E2E, write demo script

**Deliverables:**
- Deploy display + participant nsites via nsyte
- Test full flow: scan QR → login → register → send prompt → see on display
- Test from phone (real device)
- Performance: measure CVM round-trip with persistent connections
- Edge cases: relay down (retry), DQ05 unreachable (error message), spam (rate limit)
- Demo script: 2-minute talking points
- Whitelist pre-loaded with test npubs

**Quality Gates:**
- [ ] Full demo flow works E2E (phone → QR → login → prompt → display)
- [ ] Display dashboard updates within 10s of prompt
- [ ] Participant balance updates within 10s
- [ ] Per-key charts show correct data
- [ ] Scarcity ramp visible
- [ ] Graceful errors
- [ ] Demo script written
- [ ] Cold review (Gate 2.5)

---

## EXECUTION ORDER

```
Phase 1 (Option A — parallel):
  A1 (CVM server on DQ05)  ──┐
  A2 (display nsite HTML)  ──┼── A4 (integration + deploy) ── FELIX APPROVAL
  A3 (participant nsite)   ──┘

A1+A2+A3 dispatched in parallel.
A2+A3 can mock CVM calls during development (A1 provides real endpoint).
A4 waits for all three.
```

**Estimated time:** 4-6 hours

---

## OPTION B: VPS ROUTER NODE (STRETCH)

**Gate:** Option A demo-ready AND Felix personally tested + approved.

Same as v2 plan — Cashu mint on VPS, real ecash payments for API access.

---

## DEMO NARRATIVE (for Felix)

1. "This is my infrastructure. It runs in a different location, reachable only via Nostr." → show system diagram, point to DQ05 box
2. "Scan this QR code to join." → audience scans, opens participant page on phone
3. "Log in with your Nostr identity." → NIP07/nsec bunker
4. "You now have 50,000 tokens. Send a prompt." → audience types, hits send
5. "Watch the request flow through the system." → display shows routing decision
6. "Watch the price respond to demand." → as more people send prompts, scarcity ramps, price ticks up
7. "Here's my cost basis, my price, and my margin per provider." → per-key charts
8. "This isn't slides. These are real API calls, real money, routing through Nostr-addressed infrastructure." → cost meter climbing

## QUALITY GATE: PLAYWRIGHT TESTS + VIDEOS (MANDATORY FOR ALL TASKS)

Every task MUST include:
1. **Playwright test suite** — full coverage of all functionality
2. **Playwright video recording** — `--video=on`, saved to `demo/test-videos/`
3. **Commit + push** — code, tests, and videos pushed to repo (dr remote = felixfelix-bot fork)
4. **Video sent to Felix** — for A4 (integration), the video MUST be delivered via MEDIA: before Felix is asked to test

Test files:
- `test/cvm-server.spec.ts` (A1) — all 5 CVM tools
- `test/display-nsite.spec.ts` (A2) — all 8 panels
- `test/participant-nsite.spec.ts` (A3) — full participant flow
- `test/integration.spec.ts` (A4) — end-to-end multi-page

Video files:
- `demo/test-videos/cvm-server-test.mp4`
- `demo/test-videos/display-nsite-test.mp4`
- `demo/test-videos/participant-nsite-test.mp4`
- `demo/test-videos/integration-test.mp4` (THE money shot — Felix reviews this)

Playwright config:
- `playwright.config.ts` in demo/ directory
- `--video=on` for all tests
- Mobile emulation for participant tests (375px viewport)
- Headless mode for CI, headed mode for video recording

No task is complete until: tests pass + video recorded + code pushed.
A4 not complete until: integration video sent to Felix via MEDIA:.

---

## RISKS

1. **Relay latency** — mitigated: 3 relays, persistent connections, measure during A4
2. **DQ05 unreachable** — mitigated: graceful error, mention this is why decentralization matters
3. **Venue network** — mitigated: everything over Nostr relays (public internet), phone hotspot backup
4. **NIP07 not available** — mitigated: npub manual entry fallback
5. **Quota exhaustion** — mitigated: whitelist + budget + rate limiting
6. **CVM protocol bugs** — mitigated: direct nostr-tools (proven pattern from skill), A4 cold review
