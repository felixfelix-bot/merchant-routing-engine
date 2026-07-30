# COLD REVIEW — Sovereign Engineering Demo (A1 + A2 + A3)

**Reviewer:** worker-inspector (cold review, no implementation context)
**Scope:** A1 CVM server (`demo/cvm-server/src/cvm-server.ts`), A2 display nsite (`demo/display/`), A3 participant nsite (`demo/participant/`)
**Method:** Read the committed code only; verified against the live running stack (CVM server pid 201679, proxy :9099).
**Live round-trip measured:** get_snapshot over 3 Nostr relays = **584–809 ms (avg 701 ms, n=3)** — well inside the <10 s dashboard gate.

---

## STATUS: CHANGES_REQUESTED (code) — DEMO-READINESS: BLOCKED

The code is competently built: gift-wrap crypto is correct, ledger writes are transactional,
DB queries are defensive, and the participant page renders model output via `textContent`
(no stored XSS). But there are two issues that must be addressed before the demo, and three
items that are **human-gated** (cannot be resolved by code alone — see Blockers).

---

## ISSUES

### [CRITICAL] Whitelist contains invalid, non-decodable placeholder npubs
`demo/demo-whitelist.json` lists `npub1demo0000felix…`, `…alice…`, `…bob…`. These are **not
valid bech32** — characters `o`, `i`, `b` are outside the bech32 charset (`qpzry9x8gf2tvdw0s3jn54khce6mua7l`),
so they cannot decode to any 32-byte pubkey. No real Nostr identity (NIP07 wallet / nsec)
can ever match them. **Effect: `register_participant` rejects every real participant.** The
end-to-end demo flow (scan QR → login → register → send prompt) is impossible as shipped.
Note: A1's "whitelist rejects non-whitelisted" test *passes* only because `test-client.ts`
passes these same placeholder strings verbatim as the `npub` argument — it never exercises a
real identity. Fix: replace with the real npubs of the people who will join the demo (Felix +
audience), or change the demo to self-register and publish the whitelist as a NIP-78 event.
**This is the single biggest blocker.**

### [HIGH] Token operations are not cryptographically bound to caller identity (spend-on-behalf-of)
`send_prompt` and `register_participant` take `npub` as a **plain tool argument**
(`cvm-server.ts:727-735, 815-819`). The caller is never required to prove ownership of that
npub. Worse, the inner event's signature is **never verified** (`cvm-server.ts:1052-1057` parses
the decrypted inner JSON and trusts `innerEvent.pubkey` outright — a NIP-59 violation). Anyone
who can publish a gift-wrap to the server pubkey can therefore call
`send_prompt({prompt, npub:"<any registered npub>"})` and spend that account's tokens. The
whitelist file is committed to the public repo, so the registered npubs are public knowledge.
In a demo this drains fake tokens, not real funds — but it undercuts the "real economics"
narrative and lets one participant (or an outsider) break the scarcity ramp by draining
accounts. Fix: derive the acting identity from the verified inner-event signature, ignore the
`npub` argument for spend operations, and reject requests whose inner signature doesn't verify.

### [MEDIUM] `cost_hour` is a running average since midnight, not the last hour
`cvm-server.ts:706`: `computeCostToday() / (hours elapsed today)`. This divides the
**day-total** by elapsed hours, yielding an average since 00:00, but it is labelled/exposed as
`cost_hour`. Early in the demo this reads artificially low; the display's hourly cost number
will not match intuition. Fix: sum `api_calls`/`ppq_queries` for the trailing 3600 s window.

### [MEDIUM] No cap on prompt length → real-cost amplification
`send_prompt` forwards an unbounded `prompt` to the proxy (`routeViaProxy`, `cvm-server.ts:609`).
The participant is charged on `estimateTokens(prompt)` (len/4) and the balance check rejects
prompts costing >50 K tokens, but a ~50 K-token prompt still gets routed and billed at real $
through the proxy, while the demo charges only ~50 K fake tokens. A single oversized prompt
can cost the operator real money out of proportion to the demo charge. Fix: cap `prompt.length`
(e.g. 4000 chars) before routing.

### [MEDIUM] Failed routing still consumes the rate-limit window
`ledgerCharge` sets `lastPromptAt` (`cvm-server.ts:564`) **before** `routeViaProxy` runs. If the
proxy is down, the charge is refunded (`cvm-server.ts:756-766`) but the rate-limit timestamp is
**not** cleared, so the participant is locked out for 5 s after every failure and cannot retry
quickly. This directly fights the "graceful error when DQ05 unreachable" gate. (The participant
page tries to mitigate client-side by resetting `state.lastPromptTs` on failure, but the
**server** rate-limit still holds.) Fix: clear `lastPromptAt` for the npub inside the refund path.

### [LOW] Refund ledger row records the pre-charge balance as `balance_after`
`cvm-server.ts:761` inserts the refund row with `balance_after = participant.balance` (the value
read before the charge), not the actual post-refund balance. Cosmetic ledger inconsistency only.

### [LOW] `get_price_history` bucketing assumes integer `ts`
`cvm-server.ts:835` computes `(ts / bucketSize) * bucketSize`. If `api_calls.ts` is stored as
REAL (the code formats it with `round(r.ts,3)`), SQLite real division can produce fuzzy bucket
boundaries and slightly off charts. Low risk; worth a `CAST(ts AS INTEGER)` if charts look
misaligned.

### [LOW] Broad subscription `kinds:[1059,21059]` with `limit:0`
The server subscribes to ALL gift-wrap events on three busy relays and filters by `p`-tag
client-side (`cvm-server.ts:1031-1050`). The cheap `p`-tag check gates decryption (good), but
the dedup `Set` admits every 1059 event and the `MAX_SEEN` clearing logic can evict a legit id
before it's seen on all relays, allowing rare reprocessing. Acceptable for a demo; would not
ship to production.

---

## THINGS DONE WELL
- Gift-wrap send path (`sendResponse`, `cvm-server.ts:973-1005`): random one-time wrapper key,
  NIP-44 v2 conversation key, publishes to all relays with per-relay try/catch. Correct.
- Ledger writes are wrapped in BEGIN/COMMIT with ROLLBACK on throw (`cvm-server.ts:526-538,
  578-589`). Atomic.
- DB helpers `qall`/`qone` swallow errors and return empty — no uncaught throws crash the relay
  handler.
- Key file written `mode 0o600`, hex re-read after write to detect truncation, gitignored.
- Participant page renders LLM output via `textContent` (`participant/index.html:800`) —
  model output cannot inject HTML/JS. innerHTML is used only for static templates / operator data.
- Demo-mode fallback in both nsites: if no CVM server is configured, they synthesise data so the
  UI is never blank. Good for resilience.

---

## BLOCKERS (human-gated — cannot resolve by editing code)

1. **Real participant npubs** (CRITICAL, see above). The whitelist must contain the real npubs of
   the people who will scan the QR code, OR the registration model must change. I cannot fabricate
   identities the audience actually holds.
2. **Real-device test.** "Test from real phone" is a stated gate and cannot be performed headless.
   Mobile-emulated Playwright (375 px) is covered by A3 + the integration test; a human must do
   the physical scan/login on a phone.
3. **nsyte deploy credentials.** `nsyte` (v0.27.2) is installed but has **no identity configured**
   (no `~/.config/nsyte`, no `~/.nsyte`, no `nsec` env that nsyte reads). Deploying the display +
   participant nsites requires a decision on which Nostr key/bunker to publish under. Running
   `nsyte init` would mint a new throwaway key on disk — a credential decision I should not make
   autonomously, and a random key means the QR/URLs won't be Felix's stable identity.

---

## RECOMMENDATIONS
- Replace the placeholder whitelist with real npubs (or self-registration + NIP-78 publish) — **unblocks E2E**.
- Bind spend operations to the verified inner-event signature; drop the `npub` argument — **closes the spend-on-behalf-of hole**.
- Fix `cost_hour` to a trailing-1h window; cap prompt length; clear rate-limit on refund.
- Provide nsyte credentials (nsec / nbunksec / bunker), then deploy both nsites and bake the resulting URLs into the display's QR config + participant defaults.
- Re-run `test/integration.spec.ts` (this task) live once the whitelist is real, to prove true E2E.
