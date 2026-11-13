"""Resolves a tool a mesh member asked the node to run and runs it through the orchestrator's protected dispatch under machine authority, so every permission, PHI, confirmation and audit gate applies as for any other turn.
node_mesh_chat.py calls it after the kernel has verified the request's ticket, and it never trusts the requesting device's own gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from orchestrator import vessel_executor
from orchestrator.remote_mcp_pins import sanitize_annotations

logger = logging.getLogger("Orchestrator.NodeMeshCalls")

READ_SCOPES = frozenset({"tools:read", "tools:search"})
MAX_CONTENT = 65536
QUALIFIER = "__"


class CallRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ResolvedTool:
    agent_id: str
    tool: str
    scope: str
    definition: str

    @property
    def effect_kind(self) -> str:
        return "outward_read" if self.scope in READ_SCOPES else "outside_effect"


@dataclass(frozen=True)
class CallOutcome:
    status: str
    content: str
    code: Optional[str] = None


def dotted(code: str) -> str:
    cleaned = "".join(c if (c.islower() and c.isascii()) or c.isdigit() or c in "._" else "_" for c in str(code).lower())
    cleaned = cleaned.lstrip("._0123456789") or "failed"
    if "." not in cleaned[:80]:
        cleaned = f"node.{cleaned[:75]}"
    return cleaned[:80]


def _portable(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value if -(2**63) <= value < 2**63 else {"$num": str(value)}
    if isinstance(value, float):
        return {"$num": repr(value)}
    if isinstance(value, (list, tuple)):
        return [_portable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _portable(item) for key, item in value.items()}
    return str(value)


def definition_hash(name: str, description: str, input_schema: Any, annotations: Any) -> str:
    document = {
        "name": name,
        "description": description,
        "input_schema": _portable(input_schema),
        "annotations": _portable(annotations),
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def skill_definition(skill: Any) -> str:
    schema = getattr(skill, "input_schema", None)
    metadata = getattr(skill, "metadata", None) or {}
    return definition_hash(
        str(skill.id),
        str(getattr(skill, "description", "") or ""),
        schema if isinstance(schema, dict) else {},
        sanitize_annotations(metadata.get("annotations") if isinstance(metadata, dict) else None),
    )


def _text(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text[:MAX_CONTENT]


class OrchestratorCalls:
    def __init__(self, orch: Any):
        self._orch = orch

    async def resolve(self, owner_id: str, tool: str) -> ResolvedTool:
        from orchestrator.tool_visibility import eligible_tool_pairs

        pairs: List[Tuple[str, Any]] = await asyncio.to_thread(eligible_tool_pairs, self._orch, owner_id)
        matches = [(agent, skill) for agent, skill in pairs if _matches(tool, agent, skill.id)]
        if not matches:
            raise CallRefused("node.tool_unknown")
        if len(matches) > 1:
            raise CallRefused("node.tool_ambiguous")
        agent_id, skill = matches[0]
        scope = self._orch.tool_permissions.get_tool_scope(agent_id, skill.id)
        pinned = vessel_executor.pinned_definition(self._orch, agent_id, skill.id)
        return ResolvedTool(agent_id, skill.id, scope, pinned or skill_definition(skill))

    async def authorize(self, owner_id: str, resolved: ResolvedTool) -> Any:
        from orchestrator.chain_authority import AuthoritySkip

        authority = await self._orch.derive_machine_authority(
            user_id=owner_id, agent_id=resolved.agent_id, turn_class="mesh_request", consented_scopes=[resolved.scope]
        )
        if isinstance(authority, AuthoritySkip):
            raise CallRefused(dotted(f"node.authority_{authority.reason}"))
        return authority

    async def run(
        self,
        owner_id: str,
        local_chat: str,
        resolved: ResolvedTool,
        authority: Any,
        arguments: Dict[str, Any],
        ticket: Optional[vessel_executor.CallTicket] = None,
    ) -> CallOutcome:
        from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket

        task = BackgroundTask(task_id=f"mesh-call-{int(time.time() * 1000)}", chat_id=local_chat, user_id=owner_id)
        vws = VirtualWebSocket(task)
        self._orch._bind_machine_turn(vws, authority)
        try:
            with vessel_executor.carrying(ticket):
                response = await self._orch.execute_authorized_tool(
                    claims=None,
                    user_id=owner_id,
                    agent_id=resolved.agent_id,
                    tool_name=resolved.tool,
                    arguments=dict(arguments),
                    channel="background",
                    chat_id=local_chat,
                    websocket=vws,
                )
        except Exception as exc:
            logger.warning("node_mesh_call_failed: %s", type(exc).__name__)
            return CallOutcome("error", "The node could not run the tool.", "node.tool_failed")
        finally:
            self._orch._unbind_machine_turn(vws)
            await vws.close()
        if response is None:
            return CallOutcome("error", "The tool returned nothing.", "node.tool_failed")
        if response.error is not None:
            error = response.error
            return CallOutcome("error", _text(error.get("message") or "The tool refused the call."), dotted(error.get("code") or "tool_failed"))
        return CallOutcome("ok", _text(response.result if response.result is not None else ""))


def _matches(requested: str, agent_id: str, skill_id: str) -> bool:
    if QUALIFIER in requested:
        return requested == f"{agent_id}{QUALIFIER}{skill_id}"
    return requested == skill_id
