import logging
from typing import Optional

from aiohttp import web

from tollgate_mint_orchestrator.audit_log import AuditLogger
from tollgate_mint_orchestrator.mint_registry import MintRegistry
from dataclasses import asdict

logger = logging.getLogger(__name__)


class OrchestratorAPI:
    def __init__(
        self,
        registry: MintRegistry,
        audit_logger: AuditLogger,
        host: str = "0.0.0.0",
        port: int = 8090,
    ):
        self.registry = registry
        self.audit_logger = audit_logger
        self.host = host
        self.port = port
        self.app = web.Application()
        self._setup_routes()
        self._runner: Optional[web.AppRunner] = None

    def _setup_routes(self):
        self.app.router.add_get("/health", self._health)
        self.app.router.add_get("/mints", self._list_mints)
        self.app.router.add_get("/mints/{subdomain}", self._get_mint)
        self.app.router.add_get("/audit", self._audit)

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "mints": len(self.registry.mints)})

    async def _list_mints(self, request: web.Request) -> web.Response:
        mints = [asdict(m) for m in self.registry.list_mints()]
        return web.json_response({"mints": mints})

    async def _get_mint(self, request: web.Request) -> web.Response:
        subdomain = request.match_info["subdomain"]
        mint = self.registry.get_mint_by_subdomain(subdomain)
        if not mint:
            return web.json_response({"error": "mint not found"}, status=404)
        return web.json_response(asdict(mint))

    async def _audit(self, request: web.Request) -> web.Response:
        count = int(request.query.get("count", "100"))
        entries = self.audit_logger.read_recent(count)
        return web.json_response({"entries": entries})

    async def start(self):
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"API listening on {self.host}:{self.port}")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
