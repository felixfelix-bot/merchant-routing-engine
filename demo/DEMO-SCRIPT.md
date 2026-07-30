# DEMO SCRIPT — Sovereign Engineering (2-minute talking points)

Audience-facing flow. Speak the **bold** lines; point at the named panel. Total ≈ 2 min.
Prerequisite: display nsite open on the big screen; QR encoding the participant nsite URL;
CVM server running; whitelist pre-loaded with the real npubs of people in the room.

---

## 0. SETUP (before the audience looks) — 0:00
- Display nsite loaded, **LIVE · CVM** badge green (not DEMO MODE).
- QR panel shows the participant URL. Cost meter, quota bars, dispatch gate visible.
- If the badge reads DEMO MODE / OFFLINE → the CVM server (DQ05) isn't reachable. Say so
  honestly; pivot to "this is exactly why it's Nostr-addressed — let's see it live."

## 1. "This is my infrastructure. It runs somewhere else, reachable only via Nostr." — 0:00
- Point at the **System Diagram** (Panel 2): DQ05 box, proxy, relays, phone icons.
- "No cloud dashboard. The only way in is a Nostr address."

## 2. "Scan this to join." — 0:15
- Point at the **QR Code** (Panel 1). Audience scans → participant page opens on their phone.

## 3. "Log in with your Nostr identity." — 0:25
- Audience taps **Login with Nostr** (NIP07, e.g. Alby) or pastes their npub.
- *Fallback if NIP07 missing:* the page offers manual npub entry — same flow.

## 4. "You now have 50,000 tokens. Send a prompt." — 0:40
- Audience types a prompt, sees the **cost preview** (tokens → price at current rate), hits Send.
- "Watch it route."

## 5. "Watch the request flow through the system." — 0:55
- Point at **Request Flow** (Panel 6): a new card appears within seconds showing provider,
  model, tokens, cost, and the routing reason. "That's a real API call, routed by the engine."
- The participant's phone shows the **response** and their **balance drops**.

## 6. "Watch the price respond to demand." — 1:15
- As more people send prompts, point at **Token Economy** (Panel 8): scarcity level climbs
  (20%→40%→60%…), the multiplier steps 1.0× → 1.2× → 1.5×, and **cost meter** (Panel 5) ticks up.

## 7. "Here's my cost basis, my price, and my margin — per provider." — 1:35
- Point at the **Per-Key Price Charts** (Panel 3): four charts (ours / friend / ollama / ppq),
  each with cost basis (solid), your price (dashed), margin% (filled).

## 8. CLOSE — 1:50
- "This isn't slides. Real API calls, real money, routing through Nostr-addressed
  infrastructure I control. The economics are live and responding to you right now."

---

## EDGE CASES — what to say if something breaks (don't hide it)
- **A prompt fails / proxy down:** "DQ05 is unreachable for a moment — and that's the point of
  decentralized addressing. It retries over the relays." (The page shows an error, then recovers.)
- **Someone spams:** "One prompt every five seconds per identity — the rate limit just kicked in."
- **NIP07 not installed:** "Paste your npub instead — same identity, no extension needed."
- **Quota exhaustion:** "The dispatch gate just flipped red — the engine refused to overspend."

## RESET (between shows)
- `sqlite3 demo/demo_ledger.db "DELETE FROM demo_participants; DELETE FROM demo_ledger;"`
  then restart the CVM server, or clear via the demo reset flow (integration test covers this).

## LATENCY (measured this run)
- get_snapshot round-trip over 3 relays: **avg ~0.7 s** (max 0.8 s). Dashboard updates are
  effectively instant — well under the 10 s gate.
