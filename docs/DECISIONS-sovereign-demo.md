# Sovereign Engineering Demo — Architecture Decisions

**Date:** 2026-07-30
**Status:** Approved by Felix
**Demo:** Sovereign Engineering (2026-07-31)

---

## Decision 1: All Communication Via ContextVM (CVM)

**Decision:** Every data path — dashboard refresh, participant prompts, registration, ledger queries — flows as CVM tool calls over Nostr relays. No direct SQLite access, no HTTP fallback, no Tailscale.

**Reasoning:**
- DQ05 (Kalman machine) is remote — different network, only reachable via Nostr
- Cannot assume all components run on the same machine
- CVM is the sovereign transport layer: infrastructure addressable by Nostr public key, not IP
- Demonstrates the actual sovereign engineering story — real Nostr-addressed infrastructure

**Tradeoff accepted:** CVM calls take 2-10s per query (vs <50ms direct SQLite). Mitigated by:
- Persistent relay WebSocket connections (reuses open connection, ~2-3s vs 5-10s cold)
- Display dashboard polls every 5s (not sub-second — acceptable for demo)
- Participant prompts are individual calls (5-10s per prompt is fine for human interaction)

**Rejected alternative:** Direct SQLite read from a local dashboard server. Rejected because:
- Requires dashboard server on same machine as DQ05 — not the deployment topology
- Doesn't demonstrate the sovereign/Nostr story
- Creates a false dependency on co-location

---

## Decision 2: Two Separate Nsites (Display + Participant)

**Decision:** Two static HTML pages deployed as nsites via nsyte:
1. **Display nsite** — big screen, shows all dashboards + QR code
2. **Participant nsite** — phone-optimized, opened via QR code scan

**Reasoning:**
- Display laptop at venue is NOT the same machine as DQ05
- Participants use their own phones, not the display laptop
- QR code provides clean onboarding flow: scan → open → login → participate
- Two separate pages = clean separation of concerns

**QR Code Flow:**
1. Display page shows QR encoding participant nsite URL
2. Participant scans → opens browser → loads participant nsite
3. Nsite asks for Nostr login (NIP07 browser extension or nsec bunker)
4. Nsite calls `register_participant` via CVM
5. If whitelisted → shows token balance + prompt form
6. If not whitelisted → "contact Felix to join"

---

## Decision 3: NIP-78 Not Used — Pure CVM Pull

**Decision:** All data retrieval uses CVM tool calls (pull), not NIP-78 event publishing (push).

**Reasoning:**
- Felix explicitly requested "everything should be a CVM pull"
- CVM pull is simpler to reason about — request/response, no subscription management
- Persistent relay connections make pull fast enough (~2-3s with warm connections)
- NIP-78 push would add complexity (event publishing, browser subscription, deduplication)

**Tradeoff accepted:** Push (NIP-78) would give sub-second updates. Pull (CVM) gives 2-3s. For a demo, 2-3s is fine.

---

## Decision 4: Direct nostr-tools, Not @contextvm/sdk

**Decision:** Browser CVM client and DQ05 CVM server both use direct `nostr-tools` implementation, not the `@contextvm/sdk` transport layer.

**Reasoning (from contextvm skill pitfalls):**
- `NostrServerTransport` from `@contextvm/sdk` silently hangs — connects but produces zero output and never completes `server.connect()`
- This is a known issue documented in the contextvm skill
- Direct nostr-tools implementation is the verified workaround: subscribe to kind 1059 events, decrypt with NIP-44, parse JSON-RPC, respond via gift wrap
- Browser CVM client pattern documented in `references/browser-cvm-client.md`

**Implementation:**
- Server: `bun src/cvm-server.ts` (NOT `bun run` — swallows output)
- Browser: nostr-tools bundled locally (no CDN), persistent WebSocket connections to 3 relays
- Client and server use different Nostr keys (prevents echo loop)

---

## Decision 5: Npub Whitelist + Token Budget for Access Control

**Decision:** Only whitelisted Nostr npubs can participate. Each gets a fixed token budget (50,000 tokens). Rate limited to 1 prompt per 5 seconds per npub.

**Reasoning:**
- Prevents random strangers from wasting API quota
- Felix controls who joins (pre-loads whitelist or adds people live during demo)
- Token budget creates the economic demo: scarcity increases as participants spend
- Rate limiting prevents spam

**Whitelist management:**
- `demo-whitelist.json` — array of authorized npubs
- `POST /admin/whitelist` (password protected) — add npub live during demo

---

## Decision 6: Scarcity-Based Dynamic Pricing

**Decision:** Token price = Kalman base rate × scarcity_factor. Scarcity ramps as aggregate demo budget is consumed:
- <20% consumed: 1.0x
- 20-40%: 1.2x
- 40-60%: 1.5x
- 60-80%: 1.8x
- >80%: 2.0x

**Reasoning:**
- This is the core demo mechanic — audience sees price respond to demand in real-time
- Uses the REAL Kalman pricing engine already running on DQ05
- Scarcity factor already exists in the proxy (ramps 1.0x → 2.0x as quota fills)
- Demo scarcity is a separate calculation based on demo budget consumption (not API quota)
- This makes the economic story visible and interactive

---

## Decision 7: Per-Key Price Charts (One Chart Per Key)

**Decision:** Separate Plotly chart per provider key (ours, friend, ollama_cloud, ppq). Each chart shows 3 lines over time:
- Cost basis (what Felix pays upstream) — solid line
- Your price (what Felix charges, with margin) — dashed line
- Margin % — filled area on right axis

**Reasoning:**
- Felix explicitly requested this: "Lets do one plot per key if the dashboard is cluttered"
- Shows the economic story per provider: different costs, different margins
- Makes the "merchant routing" concept visible — you're routing between providers with different economics
- 4 separate charts avoid clutter on a single combined chart

---

## Decision 8: CVM Invisible to Audience

**Decision:** The audience never sees CVM internals (cvmi CLI, kind 25910, gift wrap, NIP-44). They interact via web forms. CVM is mentioned verbally only if asked.

**Reasoning:**
- Felix: "Lets not expose the internal workings of contextvm to the audience. They already know about contextvm, we can mention that as a side note during the demo"
- The demo surface is a web dashboard — clean, professional
- The sovereignty story is in the architecture, not the protocol details
- If someone asks "how is this sovereign?", Felix mentions Nostr as the transport layer

---

## Decision 9: Plotly.js + nostr-tools Bundled Locally

**Decision:** Both Plotly.js and nostr-tools are downloaded and bundled in the nsite, not loaded from CDN.

**Reasoning:**
- Venue network may be unreliable
- Offline capability = demo reliability
- No dependency on external CDNs at demo time

---

## Decision 10: Playwright Tests + Video Recording Mandatory

**Decision:** Every task includes:
1. Playwright test suite with full functional coverage
2. Playwright video recording (`--video=on`)
3. Commit + push of code, tests, and videos
4. A4 integration video sent to Felix via MEDIA: before he tests

**Reasoning:**
- Felix must see evidence that it works before testing
- Video proof = confidence, no surprises during live demo
- Tests survive as regression suite for future iterations
- Mobile emulation (375px) for participant nsite tests

---

## Decision 11: Option B (VPS + Cashu) Strictly Gated

**Decision:** Option B (real Cashu ecash payments on VPS) only starts after:
1. Option A is demo-ready
2. Felix has personally tested it
3. Felix has explicitly approved

**Reasoning:**
- Felix: "Only after A is demo-ready and after we have iterated till the point where I have deemed it ready to demo"
- Option A (simulated token economy) is the fallback — works without Cashu
- Option B adds real ecash payments but is more complex
- If time runs out, Option A is sufficient for the demo

---

## Decision 12: Node.js 22 (not Bun) for Broader Compatibility

**Decision:** Use Node.js 22 with built-in `node:sqlite` for the dashboard server. Bun is used for the CVM server only (needs `bun:sqlite`).

**Reasoning:**
- Node.js is the standard runtime — broader compatibility
- Node 22 has built-in SQLite (no better-sqlite3 compile issues)
- Bun is newer/faster but has compatibility issues in some environments
- CVM server uses bun because it needs `bun:sqlite` and the contextvm skill documents bun patterns

**Note:** After the architecture change to CVM-only, the dashboard server (Node.js) is no longer needed — the CVM server (bun) reads the DB directly on DQ05. Node.js may still be used for nsite build tooling.

---

## Architecture Summary

```
Display Laptop (venue)          DQ05 (remote, different network)        Participant Phones
─────────────────              ──────────────────────────              ─────────────────
Display Nsite                  CVM Server (bun + nostr-tools)          Participant Nsite
  │                            │                                        │
  ├─ QR Code                   ├─ get_snapshot (reads DB local)         ├─ NIP07/nsec login
  ├─ System Diagram            ├─ send_prompt (routes via proxy)        ├─ Token Balance
  ├─ 4 Per-Key Charts          ├─ register_participant                   ├─ Prompt Input
  ├─ Quota Bars                ├─ get_price_history                     ├─ Your Spend
  ├─ Cost Meter                ├─ get_ledger                            └─ Current Price
  ├─ Request Flow              │
  ├─ Dispatch Gate             ├─ Proxy :9099 (Kalman + LiveRouter)
  └─ Token Economy Summary     ├─ zai_usage.db
                               ├─ demo_ledger.db
                               └─ demo-whitelist.json
         │                              │                                        │
         └────────── Nostr Relays ──────┴────────────────────────────────────────┘
                    (nostr.mom, relay.primal.net, nos.lol)
                    CVM: kind 25910 + NIP-59 gift wrap
                    Persistent WebSocket connections
```

All arrows are CVM tool calls over Nostr. No direct network paths. No HTTP. No SQLite access from browser. Sovereign by design.