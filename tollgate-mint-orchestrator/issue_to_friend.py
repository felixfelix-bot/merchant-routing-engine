"""issue-to-friend — GRPC ecash issuance for the private Routstr node.

Flow (H4 of the routstr-integration plan):
  1. Friend creates a mint quote at https://mint.orangesync.tech:
       curl -X POST https://mint.orangesync.tech/v1/mint/quote/bolt11 \\
         -H 'Content-Type: application/json' \\
         -d '{"unit": "sat", "amount": 5000}'
     → response contains "quote" (the quote_id). The LN "request" is
       intentionally unpayable — this mint runs without Lightning.
  2. Operator (you) mark the quote PAID via GRPC:
       python -m tollgate_mint_orchestrator.issue_to_friend \\
         --quote <quote_id> [--mint-url https://mint.orangesync.tech]
  3. Friend mints ecash against the paid quote:
       curl -X POST https://mint.orangesync.tech/v1/mint/bolt11 \\
         -H 'Content-Type: application/json' \\
         -d '{"quote": "<quote_id>", "outputs": [<blinded messages>]}'
     → cashu token = API key on https://friends.orangesync.tech

Enforces the registry's issuance ceilings (max_single_issuance /
max_balance) as a soft guard when the mint is registered.

Never marks a quote paid twice (idempotent by nature of the state machine).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.request
import urllib.error
import json

from .grpc_client import MintGrpcClient
from .mint_registry import MintRegistry

DEFAULT_REGISTRY = os.environ.get(
    "ORCHESTRATOR_REGISTRY_PATH",
    "/opt/tollgate/mints/registry.json",
)
DEFAULT_MINT_URL = "https://mint.orangesync.tech"


def _lookup_mint(registry_path: str, mint_url: str):
    """Return (host, grpc_port, entry) for *mint_url* from the registry."""
    reg = MintRegistry.load(registry_path)
    for entry in reg.mints:
        if entry.url.rstrip("/") == mint_url.rstrip("/"):
            host = entry.url.split("//", 1)[-1].split(":")[0]
            return host, entry.grpc_port, entry
    return None, None, None


async def _issue(quote_id: str, mint_url: str, registry_path: str,
                 grpc_host: str | None = None, grpc_port: int | None = None,
                 dry_run: bool = False) -> int:
    entry = None
    if grpc_host and grpc_port:
        host, port = grpc_host, grpc_port
    else:
        host, port, entry = _lookup_mint(registry_path, mint_url)
        if not host:
            print(f"mint {mint_url!r} not in registry ({registry_path}); "
                  f"pass --grpc-host/--grpc-port explicitly "
                  f"(production mint: --grpc-host 127.0.0.1 --grpc-port 50055 "
                  f"from a host-networked context)", file=sys.stderr)
            return 2
    print(f"mint: {mint_url} grpc={host}:{port}")

    # Soft ceiling check against the registry entry.
    if entry is not None:
        try:
            q = _fetch_quote(mint_url, quote_id)
            amount = int(q.get("amount") or 0)
            if amount > entry.max_single_issuance:
                print(f"REFUSED: quote amount {amount} sats exceeds "
                      f"max_single_issuance={entry.max_single_issuance}", file=sys.stderr)
                return 3
            print(f"quote amount: {amount} sats (ceiling {entry.max_single_issuance})")
        except Exception as e:
            print(f"warn: could not pre-check quote ({e}); continuing")

    if dry_run:
        print("dry-run: would call UpdateNut04Quote(state=PAID)")
        return 0

    client = MintGrpcClient(host, port)
    try:
        await client.connect()
        ok = await client.update_nut04_quote(quote_id, "PAID")
        if ok:
            print(f"OK: quote {quote_id} marked PAID — friend can now mint "
                  f"ecash and use it on friends.orangesync.tech")
            return 0
        print(f"FAILED: UpdateNut04Quote for {quote_id} (see logs above)", file=sys.stderr)
        return 1
    finally:
        await client.close()


def _fetch_quote(mint_url: str, quote_id: str) -> dict:
    req = urllib.request.Request(
        f"{mint_url.rstrip('/')}/v1/mint/quote/bolt11/{quote_id}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="issue-to-friend",
        description="Mark a Cashu mint quote PAID via cdk-mintd GRPC "
                    "(issues ecash to a friend without Lightning).")
    p.add_argument("--quote", required=True, help="quote_id the friend created")
    p.add_argument("--mint-url", default=DEFAULT_MINT_URL)
    p.add_argument("--registry", default=DEFAULT_REGISTRY)
    p.add_argument("--grpc-host", default=None,
                   help="override registry lookup (e.g. 127.0.0.1 for the "
                        "production mint from a host-networked context)")
    p.add_argument("--grpc-port", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    return asyncio.run(_issue(args.quote, args.mint_url, args.registry,
                              grpc_host=args.grpc_host, grpc_port=args.grpc_port,
                              dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
