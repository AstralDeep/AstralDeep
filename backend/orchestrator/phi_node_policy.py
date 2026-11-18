"""Refuses to start a draft agent on a node that is a member of the owner's PHI group, and stops the ones already running when the node joins or rejoins the group, because draft agents run unconfined beside PHI bodies the node can open.
It reads membership from the Kernel through node_mesh and is used by agent_lifecycle.start_draft_agent, orchestrator.py (DraftGuard), api.py and node_api.py, and start.py asks node_api before it launches generated agents."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Set

from astral_exec.constants import AGENT_SERVER_REASONS, MESSAGES
from orchestrator import node_mesh

logger = logging.getLogger(__name__)

CODE = "exec.agent_server_unsupported"
REASON = AGENT_SERVER_REASONS[0]
MESSAGE = MESSAGES[REASON]


class AgentServerUnsupported(ValueError):
    code = CODE
    reason = REASON

    def __init__(self) -> None:
        super().__init__(MESSAGE)

    def detail(self) -> dict:
        return {"code": CODE, "reason": REASON, "message": MESSAGE}


async def refuse_on_phi_node() -> None:
    mesh = node_mesh.current()
    if mesh is None or not mesh.started:
        return
    if not (await mesh.status())["enrolled"]:
        return
    try:
        member = await mesh.phi_member()
    except mesh.binding.KernelError:
        member = True
    if member:
        raise AgentServerUnsupported()


class DraftGuard:
    def __init__(self, mesh: Any, lifecycle: Any):
        self._mesh = mesh
        self._lifecycle = lifecycle
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: Set[asyncio.Task] = set()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._mesh.subscribe(self._on_change)
        self._schedule()

    def _on_change(self, change: Any) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._schedule)

    def _schedule(self) -> None:
        task = asyncio.ensure_future(self.enforce())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def enforce(self) -> None:
        try:
            try:
                await refuse_on_phi_node()
            except AgentServerUnsupported:
                stopped = await self._lifecycle.stop_running_drafts()
                if stopped:
                    logger.warning("phi_node_drafts_stopped: %d", len(stopped))
        except Exception:
            logger.exception("phi_node_guard_failed")

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
