"""Starts a draft or generated agent only on a node the owner excluded from the PHI group, a choice the owner signs into the Kernel roster, and stops the ones running when the node joins or rejoins the group.
A node with no mesh, one not enrolled, one never excluded and one whose roster cannot be read are refused, because these agents run unconfined beside the data the node can open.
It reads the roster through node_mesh and is used by agent_lifecycle.start_draft_agent, orchestrator.py (DraftGuard, which re-checks on every mesh change and on a timer and relaunches live generated agents once the node allows them), api.py and node_api.py, and start.py asks node_api before it launches generated agents."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Set

from astral_exec.constants import AGENT_SERVER_REASONS, MESSAGES
from orchestrator import node_mesh

logger = logging.getLogger(__name__)

CODE = "exec.agent_server_unsupported"
PHI_NODE, NOT_EXCLUDED = AGENT_SERVER_REASONS
RECHECK_SECONDS = 30.0


class AgentServerUnsupported(ValueError):
    code = CODE

    def __init__(self, reason: str = PHI_NODE) -> None:
        self.reason = reason
        super().__init__(MESSAGES[reason])

    def detail(self) -> dict:
        return {"code": CODE, "reason": self.reason, "message": MESSAGES[self.reason]}


async def refuse_on_phi_node() -> None:
    mesh = node_mesh.current()
    if mesh is None or not mesh.started:
        raise AgentServerUnsupported(NOT_EXCLUDED)
    try:
        enrolled = bool((await mesh.status())["enrolled"])
        member = enrolled and await mesh.phi_member()
        excluded = enrolled and not member and await mesh.phi_excluded()
    except (mesh.binding.KernelError, node_mesh.NodeMeshError):
        enrolled, member, excluded = True, True, False
    if not enrolled:
        raise AgentServerUnsupported(NOT_EXCLUDED)
    if member:
        raise AgentServerUnsupported(PHI_NODE)
    if not excluded:
        raise AgentServerUnsupported(NOT_EXCLUDED)


class DraftGuard:
    def __init__(self, mesh: Any, lifecycle: Any, recheck_s: float = RECHECK_SECONDS):
        self._mesh = mesh
        self._lifecycle = lifecycle
        self._recheck_s = recheck_s
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: Set[asyncio.Task] = set()
        self._ticker: Optional[asyncio.Task] = None
        self._allowed = False

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._mesh.subscribe(self._on_change)
        self._schedule()
        self._ticker = asyncio.ensure_future(self._recheck())

    def _on_change(self, change: Any) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._schedule)

    def _schedule(self) -> None:
        task = asyncio.ensure_future(self.enforce())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _recheck(self) -> None:
        while True:
            await asyncio.sleep(self._recheck_s)
            await self.enforce()

    async def enforce(self) -> None:
        try:
            await refuse_on_phi_node()
        except Exception as refused:
            if not isinstance(refused, AgentServerUnsupported):
                logger.exception("phi_node_guard_policy_unreadable")
            self._allowed = False
            try:
                stopped = await self._lifecycle.stop_running_drafts()
            except Exception:
                logger.exception("phi_node_guard_failed")
                return
            if stopped:
                logger.warning("phi_node_drafts_stopped: %d", len(stopped))
            return
        if self._allowed:
            return
        self._allowed = True
        try:
            await self._lifecycle.relaunch_live_agents()
        except Exception:
            logger.exception("phi_node_guard_failed")

    async def stop(self) -> None:
        tasks = list(self._tasks) + ([self._ticker] if self._ticker is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._ticker = None
        self._allowed = False
