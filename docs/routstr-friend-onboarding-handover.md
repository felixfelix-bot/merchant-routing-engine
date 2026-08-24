# Spending AI Credits on Felix's Routstr Node — Friend & Agent Onboarding

Felix runs a private routstr node at `https://friends.orangesync.tech` backed by z.ai GLM models. You receive ecash credits (denominated in sats) and spend them through a standard OpenAI-compatible API. This doc covers the full setup: generating a Nostr identity, requesting credits, and wiring your agent to the node.

---

## The 30-Second Mental Model

| Concept | What it means |
|---------|---------------|
| **npub** | Your Nostr public key — your identity for Felix's whitelist. Send it to Felix via Signal. |
| **Credit request** | You run a wallet command that creates an unpaid Lightning invoice (a "request ticket"). You send Felix the quote ID. Felix marks it paid. |
| **Ecash** | Cashu tokens that land in your local `routstrd` wallet automatically once Felix approves. Denominated in sats. |
| **API key (cashu token)** | A persistent bearer key you exchange ecash for via `/v1/balance/create`. The cashu token string itself becomes your reusable API key (`$ROUTSTR_API_KEY`). |
| **Per-request cashu** | Alternatively, pass a raw cashu token per API call. Token is consumed. |
| **Money** | All amounts are in **sats** (1 sat = 0.00000001 BTC). Balance endpoint returns millisats (1 sat = 1000 msat). |

**Flow:** generate npub → send to Felix → request credits → ecash arrives in wallet → exchange cashu token for persistent API key (`$ROUTSTR_API_KEY`) → use as OpenAI API key.

---

## Prerequisites & Install

You need **Bun** and **routstrd**. Works on Linux and macOS.

```bash
# Install Bun
curl -fsSL https://bun.sh/install | bash
export PATH="$HOME/.bun/bin:$PATH"

# Install routstrd
bun install -g routstrd
routstrd --version   # → 0.4.1 or later
```

For keypair generation, either install `nak` (Nostr CLI) or use the Python fallback in Step 1.

---

## Step 1 — Generate Your Nostr Keypair

Your **npub** is what you send to Felix for whitelisting. Your **nsec** stays on your machine — treat it like a seed phrase.

### Option A: nak CLI

Install `nak` from https://github.com/cmdr-nak/nak/releases/latest (Linux, macOS, Windows binaries). Place the binary for your platform on your `PATH` (e.g. `~/.local/bin` or `/usr/local/bin`), then:

```bash
HEX_SEC=$(nak key generate)          # → 64-char hex private scalar
NSEC=$(nak encode nsec "$HEX_SEC")   # → nsec1...  (SECRET — store securely)
NPUB=$(nak key public "$HEX_SEC" | nak encode npub --stdin)
# or step by step:
#   HEX_PUB=$(nak key public "$HEX_SEC")   # → 64-char hex public key
#   NPUB=$(nak encode npub "$HEX_PUB")     # → npub1abc123...

echo "nsec: $NSEC"   # SECRET — never share this
echo "npub: $NPUB"   # PUBLIC — send this to Felix
```

Tip: stash the nsec where only your agent reads it; you only paste the npub into Signal.

### Option B: Python fallback

Requires `pip install coincurve bech32`:

```python
import coincurve, bech32

priv = coincurve.PrivateKey()
priv_hex = priv.private_key.hex()
pub_hex = priv.public_key.format(compressed=True)[1:].hex()

npub = bech32.encode("npub", bech32.convertbits(bytes.fromhex(pub_hex), 8, 5))
nsec = bech32.encode("nsec", bech32.convertbits(bytes.fromhex(priv_hex), 8, 5))
print(f"npub: {npub}\nnsec: {nsec}")
```

### nsec security rules

- Store in a file with `chmod 600 ~/.nostr-key` or as an environment variable.
- **Never** paste it in chat. **Never** commit it to git. You only share the **npub** with Felix.

### Send your npub to Felix

Message Felix on Signal with your npub (`npub1…`). Felix adds it to his whitelist manually.

---

## Step 2 — Register Felix's Mint

```bash
routstrd wallet mints add https://mint.orangesync.tech
routstrd wallet mints list
# → {"mints": ["https://mint.orangesync.tech"], "activeMint": "https://mint.orangesync.tech"}
```

> `https://mint.minibits.cash/Bitcoin` is also trusted on this node, but `mint.orangesync.tech` is Felix's own and the one you'll use for credit requests.

---

## Step 3 — Request Credits

Create an unpaid invoice (a "request ticket") at the mint, then tell Felix to approve it.

```bash
routstrd wallet receive bolt11 5000 --mint-url https://mint.orangesync.tech
```

The command creates a mint quote and opens a NUT-17 WebSocket subscription that auto-mints ecash when the quote is marked paid. Find the quote ID:

```bash
grep "Mint operation is pending" ~/.cocod/daemon.log | tail -1
# → {"event":"Mint operation is pending","quoteId":"01a0347d-c485-7cc1-9a80-b6e285fb3256",...}
```

### Message Felix on Signal

```
npub: npub1yourkey...
amount: 5000 sats
quoteId: 01a0347d-c485-7cc1-9a80-b6e285fb3256
```

### ⚠️ Do NOT pay the invoice over Lightning

The Lightning invoice (`lntb5000p1...testnut-approval-required`) is a **request ticket from a fake wallet**. The mint does not accept real Lightning payments. Paying it will just lose you sats — it does nothing. Only Felix's approval triggers ecash issuance.

Felix verifies your npub is on the whitelist and marks the invoice as paid from the mint's operator interface. Your wallet auto-mints the ecash via the NUT-17 WebSocket — usually within seconds.

---

## Step 4 — Confirm the Ecash Arrived

```bash
routstrd wallet balance
# → {"https://mint.orangesync.tech": 5000}
```

If it's still 0 after a minute, ask Felix to confirm he approved the right quote ID.

---

## Step 5 — Convert Ecash into a Persistent API Key

After the ecash lands in your wallet, spend it as a **persistent bearer token** for the node. The flow is: send a cashu token to `/v1/balance/create`; the node validates it at the mint and registers a balance record keyed by your token's hash.

```bash
# 1. Create a cashu token from your wallet (debits your wallet, returns a spending token)
TOKEN=$(routstrd wallet send cashu 5000 --mint-url https://mint.orangesync.tech)
# TOKEN now holds a "cashuA..." string

# 2. Register it with the node — the token is settled at the mint and becomes a persistent API key
curl -s -X POST https://friends.orangesync.tech/v1/balance/create \
  -H 'Content-Type: application/json' \
  -d "{\"initial_balance_token\": \"$TOKEN\"}"
# → {"api_key":"***<masked-hash>","balance":5000}
```

**Key things to understand:**

- The response masks the key (`***` + hash) for verification only.
- **Your usable API key is the cashu token you just sent.** After `/v1/balance/create`, that `cashu...` string works as a persistent bearer credential — the node looks it up by hash on every subsequent request, exactly like an `sk-` key. Do not discard it.
- `balance` is in sats (not millisats here). The same key persists across top-ups — no new key needed.

Store the token like any other API key:

```bash
# Save the cashu token (this IS your reusable API key after step 5)
echo "$TOKEN" > ~/.routstr-api-key
chmod 600 ~/.routstr-api-key
# Or as an env var:
echo "ROUTSTR_API_KEY=$TOKEN" >> ~/.env.routstr
chmod 600 ~/.env.routstr
```

> **Per-request alternative:** if you skip `/v1/balance/create` and just send `Authorization: Bearer cashu...` directly on `/v1/chat/completions`, the token is consumed by that single request (xcashu mode) and cannot be reused. Use `/v1/balance/create` first for reusable, balance-tracked access.

---

## Step 6 — Use It

The node is fully OpenAI-compatible. This is the section your agent cares about most.

### List models (no auth needed)

The `/v1/models` and `/v1/info` endpoints are public — you can call them without an API key to check the node is reachable and see what is on offer:

```bash
curl -s https://friends.orangesync.tech/v1/models | python3 -m json.tool
# or squeeze it down:
curl -s https://friends.orangesync.tech/v1/models | jq -r '.data[].id'
```

Common models include `glm-5.2`, `glm-4.6`, `kimi-k3` — always verify live via `/v1/models`; the catalogue rotates.

### curl

```bash
curl -s https://friends.orangesync.tech/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ROUTSTR_API_KEY" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"Hello!"}],"max_tokens":100}'
```

### Python (OpenAI client)

```python
from openai import OpenAI

# export OPENAI_BASE_URL=https://friends.orangesync.tech/v1
# export OPENAI_API_KEY=$ROUTSTR_API_KEY   # your cashu token from Step 5
client = OpenAI()  # picks up OPENAI_BASE_URL + OPENAI_API_KEY from env
r = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "Hello!"}],
    max_tokens=100,
)
print(r.choices[0].message.content)
```

### Node (OpenAI client)

```javascript
import OpenAI from "openai";
// picks up OPENAI_BASE_URL + OPENAI_API_KEY from env
const client = new OpenAI();
const r = await client.chat.completions.create({ model: "glm-5.2", messages: [{ role: "user", content: "Hello!" }], max_tokens: 100 });
console.log(r.choices[0].message.content);
```

### Other tools

Any OpenAI-compatible tool works — set `OPENAI_API_KEY` env var to your cashu token from Step 5 and `OPENAI_BASE_URL` to `https://friends.orangesync.tech/v1`:

- **LiteLLM:** `model: openai/glm-5.2`, `api_base: https://friends.orangesync.tech/v1`, `api_key: $ROUTSTR_API_KEY`
- **LangChain:** `ChatOpenAI()` with `OPENAI_BASE_URL` + `OPENAI_API_KEY` set in env
- **Agent env vars:** `OPENAI_API_KEY=$ROUTSTR_API_KEY` (your cashu token), `OPENAI_BASE_URL=https://friends.orangesync.tech/v1`

---

## Checking Balance & Topping Up

```bash
curl -s https://friends.orangesync.tech/v1/balance/info \
  -H "Authorization: Bearer $ROUTSTR_API_KEY"
# → {"api_key":"***","balance":4950000,"reserved":0,"total_requests":12,"total_spent":50000,...}
```

`balance` is in millisats. Divide by 1000 for sats.

### Top up

1. Repeat [Step 3](#step-3--request-credits) to get more ecash from Felix.
2. Create a cashu token: `routstrd wallet send cashu 5000 --mint-url https://mint.orangesync.tech`
3. Top up your existing key:

```bash
curl -s -X POST https://friends.orangesync.tech/v1/balance/topup \
  -H "Authorization: Bearer $ROUTSTR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"cashu_token": "cashuA..."}'
# → {"msats": 5000000}
```

> **Your cashu token (used as `$ROUTSTR_API_KEY`) persists across top-ups.** No new key needed — just add balance.

---

## Alternative: Per-Request Ecash

Pass a raw cashu token directly on each API call. The token is consumed.

```bash
curl -s https://friends.orangesync.tech/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cashuA..." \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"Quick!"}],"max_tokens":50}'
```

**Use when:** one-off requests, testing, or programmatic per-call payment.
**Avoid when:** regular usage (wasteful — needs a fresh token every call) or when you want balance tracking.

---

## Error Reference

| Error string | HTTP | Cause | Fix |
|-------------|------|-------|-----|
| `{"detail":"Unauthorized"}` | 401 | No `Authorization` header | Add `Authorization: Bearer ***` or `Bearer cashu...` |
| `"API key or Cashu token required"` (`missing_api_key`) | 401 | Non-Bearer header (e.g., NIP-98 `Nostr`) | Use `Authorization: Bearer ***` — Nostr headers not supported |
| `"Invalid API key format. Expected an 'sk-...' API key or a 'cashu...' token."` (`invalid_api_key`) | 401 | `sk-` key not registered or malformed | Verify key came from `/v1/balance/create` and is copied correctly |
| `"Invalid Cashu token"` (`invalid_cashu_token`) | 400/401 | Token malformed, spent, or from untrusted mint | Use a fresh token from `mint.orangesync.tech` or `mint.minibits.cash/Bitcoin` |
| `"Token value is too small to cover swap fees"` | 400 | Untrusted mint (catch-all — not a real fee issue) | Use only trusted mints: `mint.orangesync.tech` or `mint.minibits.cash/Bitcoin` |

---

## Pitfalls

- **Trusted mints only.** The node accepts ecash from `https://mint.orangesync.tech` and `https://mint.minibits.cash/Bitcoin`. Any other mint fails with a misleading "swap fees" error.
- **NIP-98 / Nostr auth NOT supported.** Do not send `Authorization: Nostr <event>` headers. The node only accepts `Bearer` with your cashu token (as `$ROUTSTR_API_KEY`) or a raw `cashu...` token. Your npub is for Felix's whitelist only — it never enters the API auth flow.
- **routstrd remote mode is NOT for this node.** The daemon's `--provider` flag is for routstr network nodes (Nostr-based), not this friends proxy. Use the HTTP API directly with your `$ROUTSTR_API_KEY` (cashu token).
- **nsec security.** If someone gets your nsec, they can impersonate you to Felix. Store securely, never chat it, never git it.
- **Sats vs millisats.** Wallet commands use sats. API balance returns millisats. 1 sat = 1000 msat.
- **Check `/v1/models` before hardcoding.** Model availability changes — verify live.
- **The invoice is a request ticket.** It contains `testnut-approval-required` and is NOT payable over Lightning. Never try to pay it — that just loses you sats.

---

## Quick Reference Card

```
Node URL:     https://friends.orangesync.tech
Mint URL:     https://mint.orangesync.tech
Auth:         Authorization: Bearer $ROUTSTR_API_KEY (cashu token from Step 5, persistent)
              Authorization: Bearer cashuA... (per-request, alternative)

Endpoints:
  POST /v1/chat/completions     — OpenAI-compatible chat
  GET  /v1/models               — list models
  POST /v1/balance/create       — exchange cashu token → persistent API key ($ROUTSTR_API_KEY)
  GET  /v1/balance/info         — check balance ($ROUTSTR_API_KEY)
  POST /v1/balance/topup        — add ecash to existing key

Wallet:
  routstrd wallet mints add <url>
  routstrd wallet receive bolt11 <sats> --mint-url <url>
  routstrd wallet balance
  routstrd wallet send cashu <sats> --mint-url <url>

Credit flow:
  1. routstrd wallet receive bolt11 <sats> --mint-url https://mint.orangesync.tech
  2. Get quoteId from ~/.cocod/daemon.log
  3. Signal Felix: npub + amount + quoteId
  4. Felix approves → wallet auto-mints
  5. routstrd wallet send cashu <sats> --mint-url https://mint.orangesync.tech → token
  6. POST /v1/balance/create {"initial_balance_token":"cashuA..."} → cashu token = $ROUTSTR_API_KEY
  7. Use $ROUTSTR_API_KEY with base_url https://friends.orangesync.tech/v1
```