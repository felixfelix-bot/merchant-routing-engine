import asyncio
import json
import logging
import os
import signal

from tollgate_mint_orchestrator.api import OrchestratorAPI
from tollgate_mint_orchestrator.audit_log import AuditLogger
from tollgate_mint_orchestrator.event_validator import EventValidator
from tollgate_mint_orchestrator.grpc_client import MintGrpcClient
from tollgate_mint_orchestrator.mint_registry import MintRegistry
from tollgate_mint_orchestrator.nostr_subscriber import NostrSubscriber

logger = logging.getLogger(__name__)

DEFAULTS = {
    "ORCHESTRATOR_RELAY_URL": "ws://localhost:7777",
    "ORCHESTRATOR_REGISTRY_PATH": "/opt/tollgate/mints/registry.json",
    "ORCHESTRATOR_AUDIT_LOG_PATH": "/var/log/tollgate/mint-approvals.jsonl",
    "ORCHESTRATOR_API_HOST": "0.0.0.0",
    "ORCHESTRATOR_API_PORT": "8090",
    "ORCHESTRATOR_APPROVAL_TTL_SECS": "300",
    "ORCHESTRATOR_LOG_LEVEL": "info",
    "ORCHESTRATOR_AUTHORIZED_APPROVERS": "",
    "ORCHESTRATOR_GRPC_HOST": "127.0.0.1",
}

_grpc_clients: dict[str, MintGrpcClient] = {}


def _env(key: str) -> str:
    return os.environ.get(key, DEFAULTS[key])


async def _get_grpc_client(mint_entry) -> MintGrpcClient:
    key = mint_entry.subdomain
    if key not in _grpc_clients:
        grpc_host = _env("ORCHESTRATOR_GRPC_HOST")
        client = MintGrpcClient(grpc_host, mint_entry.grpc_port)
        await client.connect()
        _grpc_clients[key] = client
    return _grpc_clients[key]


async def _handle_event(
    event: dict,
    validator: EventValidator,
    registry: MintRegistry,
    audit: AuditLogger,
):
    result = validator.validate(event)
    if not result.valid:
        logger.warning(f"Invalid approval event: {result.error}")
        audit.log_approval(
            event_id=event.get("id", ""),
            npub=event.get("pubkey", ""),
            mint_url="",
            quote_id="",
            amount=0,
            unit="",
            success=False,
            error=result.error,
        )
        return

    mint_data = result.mint
    from tollgate_mint_orchestrator.mint_registry import MintEntry
    mint_entry = MintEntry(**mint_data)

    # Forward to the auth payment processor FIRST (settlement authority when
    # the mint runs ln_backend=grpc_processor). Processor maps quote_id ->
    # request_lookup_id via the mint DB and emits the payment event itself.
    processor_ok = False
    try:
        import urllib.request as _ur
        _proc = _env("ORCHESTRATOR_PROCESSOR_APPROVE_URL",
                     "http://127.0.0.1:50057/approve")
        _req = _ur.Request(
            f"{_proc}?quote={result.quote_id}&amount={result.amount}", method="POST")
        with _ur.urlopen(_req, timeout=10) as _resp:
            processor_ok = 200 <= _resp.status < 300
        logger.info(f"Processor approve forwarded: quote={result.quote_id} ok={processor_ok}")
    except Exception as e:
        logger.warning(f"Processor approve forward failed: {e}")

    client = await _get_grpc_client(mint_entry)

    success = await client.update_nut04_quote(result.quote_id, "PAID") or processor_ok
    audit.log_approval(
        event_id=event.get("id", ""),
        npub=event.get("pubkey", ""),
        mint_url=mint_entry.url,
        quote_id=result.quote_id,
        amount=result.amount,
        unit=result.unit,
        success=success,
        error=None if success else "gRPC update failed",
    )
    if success:
        logger.info(
            f"Approved quote {result.quote_id} for {result.amount} {result.unit} on {mint_entry.url}"
        )


async def run_daemon():
    log_level = getattr(logging, _env("ORCHESTRATOR_LOG_LEVEL").upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    registry = MintRegistry.load(_env("ORCHESTRATOR_REGISTRY_PATH"))
    audit = AuditLogger(_env("ORCHESTRATOR_AUDIT_LOG_PATH"))

    authorized_approvers = [
        a.strip() for a in _env("ORCHESTRATOR_AUTHORIZED_APPROVERS").split(",") if a.strip()
    ]
    validator = EventValidator(
        registry,
        int(_env("ORCHESTRATOR_APPROVAL_TTL_SECS")),
        authorized_approvers=authorized_approvers,
    )

    filters = [{"kinds": [38010], "#t": ["mint-approval"]}]
    subscriber = NostrSubscriber(_env("ORCHESTRATOR_RELAY_URL"), filters)

    api = OrchestratorAPI(
        registry,
        audit,
        host=_env("ORCHESTRATOR_API_HOST"),
        port=int(_env("ORCHESTRATOR_API_PORT")),
    )

    await api.start()

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    subscriber_task = asyncio.create_task(
        subscriber.start(
            lambda e: _handle_event(e, validator, registry, audit)
        )
    )

    try:
        await stop_event.wait()
    finally:
        await subscriber.stop()
        subscriber_task.cancel()
        try:
            await subscriber_task
        except asyncio.CancelledError:
            pass
        for client in _grpc_clients.values():
            await client.close()
        _grpc_clients.clear()
        await api.stop()


def main():
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()