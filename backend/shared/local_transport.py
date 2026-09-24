"""In-process loopback so a BaseA2AAgent subclass runs inside the orchestrator with no
network hop: LoopbackSocket feeds frames back into Orchestrator.handle_agent_message;
TunnelSocket carries frames over a user's own UI WebSocket instead.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("LoopbackSocket")


class LoopbackSocket:
    def __init__(self, orchestrator: Any, agent_id: str) -> None:
        self._orch = orchestrator
        self.agent_id = agent_id
        self.client = ("inprocess", 0)

    async def send_text(self, text: str) -> None:
        await self._orch.handle_agent_message(self, text)

    async def send_json(self, obj: Any) -> None:
        await self._orch.handle_agent_message(self, json.dumps(obj))

    async def accept(self) -> None:
        return None

    async def close(self, *args: Any, **kwargs: Any) -> None:
        return None


class TunnelSocket:
    def __init__(self, ui_websocket: Any, owner_sub: str, agent_id: str,
                 send_fn: Any) -> None:
        self.ui_websocket = ui_websocket
        self.owner_sub = owner_sub
        self.agent_id = agent_id
        self._send_fn = send_fn
        self.client = ("tunnel", 0)
        self.is_user_agent_tunnel = True

    async def send(self, text: str) -> None:
        await self._send_fn(self.ui_websocket, json.dumps({
            "type": "agent_tunnel", "agent_id": self.agent_id, "frame": text,
        }))

    async def send_text(self, text: str) -> None:
        await self.send(text)

    async def close(self, *args: Any, **kwargs: Any) -> None:
        return None


class FencedTunnelSocket:
    def __init__(
        self,
        ui_websocket: Any,
        owner_sub: str,
        runtime_fence: Any,
        send_fn: Any,
    ) -> None:
        self.ui_websocket = ui_websocket
        self.owner_sub = owner_sub
        self.runtime_fence = runtime_fence
        self.agent_id = runtime_fence.agent_id
        self.host_session_id = runtime_fence.host_session_id
        self._send_fn = send_fn
        self.client = ("fenced-tunnel", 0)
        self.is_user_agent_tunnel = True
        self.is_fenced_user_agent_tunnel = True

    async def send_fenced(self, frame: dict[str, Any]) -> None:
        if frame.get("fence") != self.runtime_fence.to_dict():
            raise ValueError("personal-agent request runtime fence is stale")
        await self._send_fn(
            self.ui_websocket,
            json.dumps(
                {
                    "type": "agent_tunnel",
                    "fence": self.runtime_fence.to_dict(),
                    "frame": frame,
                },
                separators=(",", ":"),
            ),
        )

    async def send(self, _text: str) -> None:
        raise RuntimeError("v2 personal-agent calls require a durable request fence")

    async def send_text(self, text: str) -> None:
        await self.send(text)

    async def close(self, *args: Any, **kwargs: Any) -> None:
        return None
