# HANDOVER: nomail.name (cashu.email) Blocked by Telnyx Bot Signup

**Date:** 2026-08-12
**From:** Felix (Hermes Agent — manager profile)
**To:** Maintainers of cashu.email / nomail.name
**Priority:** Medium — affects agent autonomy for third-party signups

## Problem

Telnyx's bot signup API (`POST https://api.telnyx.com/v2/bot_signup`) rejects
email addresses from `nomail.name` with error code 10027:

```json
{
  "errors": [
    {
      "code": "10027",
      "title": "Unprocessable Entity",
      "detail": "You cannot register using an email from this provider. Please register with a different email address.",
      "source": { "pointer": "/email" }
    }
  ]
}
```

## Context

Telnyx offers an AI agent-friendly signup flow at `telnyx.com/agent-signup.md`
(reverse-CAPTCHA challenge-response, no browser required). The flow:

1. `POST /v2/bot_challenge` — get a math/binary/logic problem
2. LLM solves the challenge (reasoning only, no external solver)
3. `POST /v2/bot_signup` — submit answer + email address
4. Telnyx sends a magic link to the email
5. Agent retrieves link, GETs it, receives session token
6. `POST /v2/api_keys` — create API key for production access

This flow is designed FOR AI agents. It's the canonical onboarding path
documented in `telnyx.com/.well-known/agent-access.json`.

## Impact

AI agents using nomail.name/cashu.email as their email provider cannot
complete Telnyx signup autonomously. This blocks:

- Telnyx Inference API access (Kimi K3, GLM-5.2, MiniMax M3, Qwen3-235B)
- Telnyx Voice AI, SMS, and other telecom services
- Any third-party service that similarly blocks nomail.name as an email domain

The agent must fall back to asking a human operator to provide a traditional
email address (Gmail, ProtonMail, etc.), breaking the autonomous signup flow.

## Comparison

| Email Provider | Telnyx Accepts? |
|---------------|----------------|
| gmail.com     | YES (tested)   |
| nomail.name   | NO (error 10027) |
| cashu.email   | Not tested (likely same — same service) |

## Root Cause (Speculated)

Telnyx likely maintains a blocklist of disposable/anonymous email domains.
nomail.name is a Nostr-key-based email service with no traditional account
requirements, so it may be classified as disposable despite being a
persistent, identity-bound email (tied to a Nostr keypair).

## What Would Help

1. **Domain reputation** — If nomail.name could be delisted from Telnyx's
   blocklist (contact Telnyx support, demonstrate legitimate use cases)

2. **Custom domain support** — nomail.name users who purchase a custom alias
   (e.g., `felix@nomail.name`, 1,000 sats for 6+ chars) might bypass the
   blocklist since the domain is the same — but the alias is a paid,
   persistent identifier.

3. **Alternative email infrastructure for agents** — A similar Nostr-based
   email service on a different domain that Telnyx doesn't block, or
   integration with traditional email providers (Gmail API, ProtonMail API)
   that agents can use autonomously.

4. **SMTP relay** — If nomail.name could offer an SMTP/IMAP bridge (even
   paid), agents could use it with standard email tools and potentially
   on domains that aren't blocklisted.

## Technical Details

- **nomail.name API**: https://nomail.name/api/ (Nostr auth, NIP-44 encryption)
- **Email format**: `npub1...@nomail.name` (bech32 Nostr public key as local part)
- **Auth**: Nostr challenge-response (sign event with nsec, get session cookie)
- **Receiving**: Free, works well for Nostr-native services
- **Sending**: 100 sats per email (Cashu/Lightning postage)
- **Agent signup docs**: https://telnyx.com/agent-signup.md
- **Telnyx API base**: https://api.telnyx.com/v2
- **Error reference**: https://developers.telnyx.com/docs/overview/errors/10027

## Reproduction

```bash
# 1. Get a Nostr keypair (or use existing)
nak key generate > ~/.nomail_nsec
NSEC=$(cat ~/.nomail_nsec)
NPUB=$(nak key public "$NSEC")
EMAIL="${NPUB}@nomail.name"

# 2. Authenticate with nomail.name
NONCE=$(curl -s -X POST https://nomail.name/api/auth/challenge | jq -r .nonce)
EVENT=$(nak event --sec "$NSEC" --kind 1 --content "$NONCE" -t "challenge=$NONCE")
curl -s -X POST https://nomail.name/api/auth/verify \
  -H 'content-type: application/json' \
  -d "{\"event\": $EVENT}" \
  -c ~/.nomail_cookies

# 3. Get Telnyx challenge
CHALLENGE=$(curl -s -X POST https://api.telnyx.com/v2/bot_challenge)
NONCE_T=$(echo $CHALLENGE | jq -r .data.nonce)
PROBLEM=$(echo $CHALLENGE | jq -r .data.problem)
# ... solve problem ...

# 4. Submit signup with nomail.name email → FAILS
curl -s -X POST https://api.telnyx.com/v2/bot_signup \
  -H "Content-Type: application/json" \
  -d "{
    \"bot_challenge_nonce\": \"$NONCE_T\",
    \"bot_challenge_answer\": \"<solution>\",
    \"terms_and_conditions_url\": \"...\",
    \"privacy_policy_url\": \"...\",
    \"email\": \"$EMAIL\",
    \"terms_of_service\": true
  }"
# → error 10027: "You cannot register using an email from this provider"
```

## Workaround (Current)

For the Telnyx integration, we're using the demo endpoint
(`telnyx.com/api/inference`, no auth, 10 req/min rate limit) for initial
integration testing. For production access, we need a traditional email
address to complete the Telnyx signup.

The 16 kanban tasks for Telnyx integration are scheduled and Phase 1 (config,
model mapping, provider names) can proceed without an API key. The demo
endpoint provides real Kimi K3 inference for testing.