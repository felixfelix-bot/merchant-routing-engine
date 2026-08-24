# Ecash issuance via routstrd + cocod wallet (verified 2026-08-22)

The zero-hand-crypto operator flow for minting ecash from
mint.orangesync.tech and funding routstr API keys. Supersedes the raw
coincurve minting path as PRIMARY (raw path = fallback only, see
`cashu-raw-minting-2026-08-22.md`).

## Context

- Local machine (DQ05) runs `routstrd` 0.4.1 CLI + `cocod` daemon
  (Bun, `~/.bun/install/global/node_modules/@routstr/cocod`).
- Wallet state: `~/.cocod/coco.db`, logs: `~/.cocod/daemon.log`,
  socket `~/.cocod/cocod.sock`. `routstrd wallet status` shows
  state UNLOCKED + per-mint balances.
- VPS2 (23.182.128.51): `cdk-mintd` (host network, REST 8085,
  gRPC management 50055), `mint-orchestrator` container with
  `issue_to_friend.py`, `routstr-proxy` friends node (127.0.0.1:8009,
  public https://friends.orangesync.tech).
- PROD mint does NOT auto-pay quotes (fakewallet auto-pay is a
  TEST-mint behavior). Operator marks quotes PAID via gRPC.

## Flow (verbatim commands)

```bash
# 1. Register mint in wallet (idempotent)
routstrd wallet mints add https://mint.orangesync.tech

# 2. Create invoice at that mint; wallet creates NUT-04 quote
routstrd wallet receive bolt11 5000 --mint-url https://mint.orangesync.tech
# Invoice prints w/ "testnut-approval-required" marker (harmless).
# Get the quoteId from the daemon log:
grep "Mint operation is pending" ~/.cocod/daemon.log | tail -1
# → {"event":"Mint operation is pending",...,"quoteId":"<uuid>"}

# 3. Operator marks quote PAID via gRPC from mint-orchestrator.
# Registry MUST use 127.0.0.1 URL (tool derives gRPC host from URL;
# public hostname → connection refused). One-off registry:
ssh root@23.182.128.51 'cat > /tmp/reg-prod.json <<EOF
{"mints": [{"url": "http://127.0.0.1:8085", "rest_port": 8085,
  "grpc_port": 50055, "units": "sat", "max_single_issuance": 100000}]}
EOF
docker cp /tmp/reg-prod.json mint-orchestrator:/tmp/reg-prod.json'
ssh root@23.182.128.51 'docker exec mint-orchestrator python -m \
  tollgate_mint_orchestrator.issue_to_friend \
  --quote <quote-id> --mint-url http://127.0.0.1:8085 \
  --registry /tmp/reg-prod.json'

# 4. Wallet auto-mints via NUT-17 websocket subscription (seconds).
routstrd wallet balance
# → balances includes {"https://mint.orangesync.tech": 5000}
```

## Funding a routstr proxy API key

- Send from wallet (`routstrd wallet send cashu <amt> --mint-url ...`)
  or POST token to proxy `/v1/balance/create`
  (`{"initial_balance_token": "<cashuA...>"}`) → returns API key.
- Proxy must TRUST the mint first (see TRUSTED-MINTS PITFALL in
  SKILL.md): friends proxy `cashu_mints` was
  `["https://mint.minibits.cash/Bitcoin"]` only; patched to append
  mint.orangesync.tech via sqlite settings blob + container restart.
  Env alternative: `CASHU_MINTS` (comma list) — settings blob persists
  and env alone does not override an existing blob.
- Key quote on the error: untrusted-mint tokens fail as
  "Token value is too small to cover swap fees" — a CATCH-ALL in
  routstr/wallet.py, not a real fee problem. The mint itself had
  input_fee_ppk = 0.

## Gotchas

- cdk-mintd gRPC = `CDK_MINTD_MANAGEMENT_PORT` (50055), host network
  mode; `docker inspect` Ports is empty `{}`.
- The orchestrator's production registry `/opt/tollgate/mints/registry.json`
  lists only test mints — prod mint needs the 127.0.0.1 one-off above
  (or should be added to the registry properly).
- No gRPC "list quotes" RPC exists — get quote IDs from the client
  daemon log at creation time.
- cashu CLI (nutshell) failed to install both locally (disk quota) and
  on VPS2 (build error exit 7) — routstrd/cocod is the reliable client.
