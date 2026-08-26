"""Workbench connection hub: broadcasts to every open workbench page and
addresses the single bridge page (the one allowed to dial).

Requirement 2.5: broken connections are cleaned up automatically and must
never affect the other pages.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

# A browser tab killed without a TCP FIN leaves a half-open socket whose
# send_json can block forever; cap every send so one zombie connection can
# never stall the scheduler's drive loop.
DEFAULT_SEND_TIMEOUT_SECONDS = 5.0


class WorkbenchSocket(Protocol):
    async def send_json(self, event: dict[str, object]) -> None: ...


class WorkbenchHub:
    def __init__(self, *, send_timeout_seconds: float = DEFAULT_SEND_TIMEOUT_SECONDS) -> None:
        self._connections: set[WorkbenchSocket] = set()
        self._bridge: WorkbenchSocket | None = None
        self._bridge_in_call = False
        self._send_timeout = send_timeout_seconds
        self.on_bridge_change = None  # async (online: bool, in_call: bool) -> None
        self.on_bridge_message = None  # async (message: dict) -> None

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    @property
    def bridge_online(self) -> bool:
        return self._bridge is not None

    @property
    def bridge_in_call(self) -> bool:
        return self._bridge_in_call

    async def register(self, connection: WorkbenchSocket) -> None:
        self._connections.add(connection)

    async def unregister(self, connection: WorkbenchSocket) -> None:
        self._connections.discard(connection)
        if self._bridge is connection:
            self._bridge = None
            self._bridge_in_call = False
            await self._notify_change(online=False)

    async def set_bridge(
        self, connection: WorkbenchSocket | None, *, in_call: bool = False
    ) -> None:
        """A workbench page declared itself the dial bridge (role=bridge)."""
        self._bridge = connection
        self._bridge_in_call = in_call if connection is not None else False
        await self._notify_change(online=connection is not None, in_call=in_call)

    def mark_bridge_call_state(self, in_call: bool) -> None:
        self._bridge_in_call = in_call

    async def broadcast(self, event: dict[str, object]) -> None:
        dead: list[WorkbenchSocket] = []
        for connection in tuple(self._connections):
            try:
                await asyncio.wait_for(
                    connection.send_json(event), timeout=self._send_timeout
                )
            except Exception:
                dead.append(connection)
        for connection in dead:
            await self.unregister(connection)

    async def send_bridge(self, event: dict[str, object]) -> bool:
        if self._bridge is None:
            return False
        bridge = self._bridge
        try:
            await asyncio.wait_for(bridge.send_json(event), timeout=self._send_timeout)
            return True
        except Exception:
            await self.unregister(bridge)
            return False

    async def deliver_bridge_message(self, message: dict[str, object]) -> None:
        if self.on_bridge_message is not None:
            await self.on_bridge_message(message)

    async def _notify_change(self, *, online: bool, in_call: bool = False) -> None:
        if self.on_bridge_change is not None:
            try:
                await self.on_bridge_change(online, in_call)
            except Exception:
                logger.exception("bridge change handler failed")
