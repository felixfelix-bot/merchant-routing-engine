import logging
from typing import Optional

import grpc.aio

logger = logging.getLogger(__name__)

try:
    from tollgate_mint_orchestrator import cdk_mint_rpc_pb2
    from tollgate_mint_orchestrator import cdk_mint_rpc_pb2_grpc

    HAS_GRPC_STUBS = True
except ImportError:
    HAS_GRPC_STUBS = False
    logger.warning("gRPC stubs not generated yet, using stub implementations")


class MintGrpcClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.channel: Optional[grpc.aio.Channel] = None
        self.stub = None

    async def connect(self):
        target = f"{self.host}:{self.port}"
        self.channel = grpc.aio.insecure_channel(target)
        if HAS_GRPC_STUBS:
            self.stub = cdk_mint_rpc_pb2_grpc.CdkMintStub(self.channel)
        logger.info(f"Connected to mint gRPC at {target}")

    async def close(self):
        if self.channel:
            await self.channel.close()
            self.channel = None
            self.stub = None

    async def update_nut04_quote(self, quote_id: str, state: str) -> bool:
        if not self.stub:
            logger.error("gRPC stub not connected")
            return False
        try:
            request = cdk_mint_rpc_pb2.UpdateNut04QuoteRequest(quote_id=quote_id, state=state)
            metadata = [("x-cdk-protocol-version", "1.0.0")]
            await self.stub.UpdateNut04Quote(request, metadata=metadata)
            logger.info(f"Updated quote {quote_id} to state {state}")
            return True
        except grpc.aio.AioRpcError as e:
            logger.error(f"gRPC error updating quote {quote_id}: {e.code()} - {e.details()}")
            return False

    async def get_info(self) -> Optional[dict]:
        if not self.stub:
            return None
        try:
            response = await self.stub.GetInfo(cdk_mint_rpc_pb2.GetInfoRequest(), metadata=[("x-cdk-protocol-version", "1.0.0")])
            return {
                "name": response.name,
                "version": response.version,
                "description": response.description,
                "total_issued": response.total_issued,
                "total_redeemed": response.total_redeemed,
            }
        except grpc.aio.AioRpcError:
            return None