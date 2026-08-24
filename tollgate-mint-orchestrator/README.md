# tollgate-mint-orchestrator

Cashu mint orchestrator with Nostr-based approval gating. Issues ecash by
marking fakewallet lightning invoices as paid via gRPC.

## What it does

This daemon watches for Nostr kind 38010 approval events. When a valid
approval is received, it calls the CDK mintd gRPC management interface
to mark the corresponding NUT-04 quote as paid, causing the mint to
issue ecash tokens to the requesting wallet.

## Components

- **daemon.py** — Main event loop: subscribes to Nostr, validates approval
  events, marks quotes paid via gRPC.
- **nostr_subscriber.py** — Nostr relay subscription client (websocket).
- **event_validator.py** — Validates kind 38010 approval event signatures
  and payload structure.
- **grpc_client.py** — Thin gRPC wrapper for CDK mintd management RPCs.
- **issue_to_friend.py** — CLI one-shot tool: mark a single quote as paid
  (used for manual friend onboarding issuance).
- **mint_registry.py** — Loads mint configuration (URLs, ports, limits).
- **audit_log.py** — Append-only audit log of all issuance actions.
- **api.py** — Optional REST API for status/health checks.
- **cdk_mint_rpc_pb2.py / cdk_mint_rpc_pb2_grpc.py** — Auto-generated
  protobuf stubs for CDK mintd gRPC.

## Relationship to routstr node

The mint at `mint.orangesync.tech` is trusted by the friends routstr
proxy at `friends.orangesync.tech`. Ecash issued by this mint can
therefore be spent at the friends node to fund routstr API keys. See
the [friend onboarding handover doc](../docs/routstr-friend-onboarding-handover.md)
for the end-to-end friend-facing flow, and the
[operator guide](../docs/ecash-issuance-operator-guide.md) for
operator-facing issuance instructions.

## Installation

```bash
cd tollgate-mint-orchestrator
pip install -e .
```

## Usage

### Daemon mode

```bash
tollgate-mint-orchestrator --registry /path/to/registry.json
```

### One-shot issuance

```bash
python -m tollgate_mint_orchestrator.issue_to_friend \
  --quote <quote-id> --mint-url http://127.0.0.1:8085 \
  --registry /path/to/registry.json
```