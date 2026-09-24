"""Per-request bridge from a synchronous tool back to the agent's event loop, built once
per call by shared/base_agent.py; call_agent_tool() mediates a peer-agent hop, and
start_long_running_job() schedules a JobPoller.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from shared.protocol import MCPRequest


logger = logging.getLogger("AgentRuntime")


class AgentRuntime:
    def __init__(
        self,
        ws: Any,
        msg: "MCPRequest",
        agent_id: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.ws = ws
        self.request_id = msg.request_id
        self.agent_id = agent_id
        self.tool_name = (msg.params or {}).get("name", "")
        args = (msg.params or {}).get("arguments") or {}
        self.cap_job_id: Optional[str] = args.get("_cap_job_id")
        self.user_id: Optional[str] = args.get("user_id")
        self.loop = loop

    async def call_agent_tool(
        self,
        callee_agent_id: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 30.0,
    ):
        import uuid as _uuid
        from shared.protocol import AgentHopRequest, MCPResponse

        hop_id = f"hop_{_uuid.uuid4().hex[:12]}"
        fut: "asyncio.Future" = asyncio.get_running_loop().create_future()
        futures = getattr(self.ws, "_hop_futures", None)
        if futures is None:
            futures = {}
            try:
                self.ws._hop_futures = futures
            except Exception:
                return MCPResponse(
                    request_id=hop_id,
                    error={"message": "agent transport cannot correlate hop responses",
                           "retryable": False})
        futures[hop_id] = fut

        frame = AgentHopRequest(
            request_id=hop_id,
            parent_request_id=self.request_id,
            initiator_agent_id=self.agent_id,
            callee_agent_id=callee_agent_id,
            tool_name=tool_name,
            arguments=dict(arguments or {}),
        )
        try:
            await self.ws.send_text(frame.to_json())
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return MCPResponse(
                request_id=hop_id,
                error={"message": f"hop to '{callee_agent_id}.{tool_name}' timed out",
                       "retryable": True})
        except Exception as exc:
            logger.warning("call_agent_tool failed: %s", exc)
            return MCPResponse(
                request_id=hop_id,
                error={"message": f"hop request failed: {exc}", "retryable": False})
        finally:
            futures.pop(hop_id, None)

    def start_long_running_job(
        self,
        poll_fn: Callable[[], Dict[str, Any]],
        *,
        poll_interval: float = 5.0,
        failure_threshold: int = 5,
    ) -> None:
        from shared.job_poller import JobPoller
        poller = JobPoller(
            ws=self.ws,
            request_id=self.request_id,
            agent_id=self.agent_id,
            tool_name=self.tool_name,
            cap_job_id=self.cap_job_id,
            poll_fn=poll_fn,
            poll_interval=poll_interval,
            failure_threshold=failure_threshold,
        )
        asyncio.run_coroutine_threadsafe(poller.run(), self.loop)
