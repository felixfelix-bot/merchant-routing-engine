import asyncio
import json
import logging
import uuid
from typing import Awaitable, Callable

import websockets

logger = logging.getLogger(__name__)


class NostrSubscriber:
    def __init__(self, relay_url: str, filters: list[dict]):
        self.relay_url = relay_url
        self.filters = filters
        self.sub_id = str(uuid.uuid4())[:8]
        self._running = False
        self._ws = None
        self._backoff = 1
        self._max_backoff = 60

    async def start(self, on_event: Callable[[dict], Awaitable[None]]):
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.relay_url) as ws:
                    self._ws = ws
                    self._backoff = 1
                    subscribe_msg = json.dumps(["REQ", self.sub_id] + self.filters)
                    await ws.send(subscribe_msg)
                    logger.info(f"Subscribed to {self.relay_url} with sub_id={self.sub_id}")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if isinstance(msg, list) and len(msg) >= 2:
                            msg_type = msg[0]
                            if msg_type == "EVENT" and len(msg) >= 3:
                                event = msg[2]
                                try:
                                    await on_event(event)
                                except Exception as e:
                                    logger.error(f"Error processing event: {e}")
                            elif msg_type == "EOSE":
                                logger.debug(f"EOSE received for {msg[1]}")
                            elif msg_type == "NOTICE":
                                logger.debug(f"Relay notice: {msg[1]}")
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.InvalidStatusCode,
                OSError,
            ) as e:
                if not self._running:
                    break
                logger.warning(f"WebSocket disconnected: {e}, reconnecting in {self._backoff}s")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self._max_backoff)
            except Exception as e:
                if not self._running:
                    break
                logger.error(f"Unexpected error: {e}, reconnecting in {self._backoff}s")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self._max_backoff)

    async def stop(self):
        self._running = False
        if self._ws:
            try:
                close_msg = json.dumps(["CLOSE", self.sub_id])
                await self._ws.send(close_msg)
            except Exception:
                pass
            self._ws = None
