"""Resolves a tool a mesh member asked the node to run and runs it through the orchestrator's protected dispatch under machine authority, so every permission, PHI, confirmation and audit gate applies as for any other turn.
node_mesh_chat.py calls it after the kernel has verified the request's ticket, and it never trusts the requesting device's own gates.
It also lists the node's published catalog and the catalog tools that consented mesh grants and the owner's own enablement both cover, for the node's own turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from orchestrator import vessel_executor
from orchestrator.remote_mcp_pins import sanitize_annotations

logger = logging.getLogger("Orchestrator.NodeMeshCalls")

READ_SCOPES = frozenset({"tools:read", "tools:search"})
MAX_CONTENT = 65536
QUALIFIER = "__"
GRANT_AGENTS = frozenset({"kernel", "*"})


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
class CatalogTool:
    definition: str
    effect: str


@dataclass(frozen=True)
class TurnTool:
    agent_id: str
    tool: str
    definition: str
    scope: str


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


def current_definition(orch: Any, agent_id: str, tool: str) -> Optional[str]:
    pinned = vessel_executor.pinned_definition(orch, agent_id, tool)
    if pinned is not None:
        return pinned
    card = getattr(orch, "agent_cards", {}).get(agent_id)
    skills = [skill for skill in getattr(card, "skills", None) or () if skill.id == tool]
    return skill_definition(skills[0]) if len(skills) == 1 else None


def grant_name(name: str, definition: str) -> str:
    return f"{name}@{definition.removeprefix('sha256:')[:8]}"


def grant_covers(grants: Any, name: str, definition: str, effect: str) -> bool:
    wanted = grant_name(name, definition)
    return any(
        getattr(grant, "consented", False) is True
        and getattr(grant, "agent", None) in GRANT_AGENTS
        and getattr(grant, "tool", None) == wanted
        and getattr(grant, "scope", None) in (effect, "*")
        for grant in grants
    )


def _text(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text[:MAX_CONTENT]


CATALOG_LIMIT = 64
ALLOWLIST_ENV = "ASTRAL_NODE_PUBLISHED_TOOLS"


def published_allowlist() -> Optional[frozenset]:
    raw = os.environ.get(ALLOWLIST_ENV)
    if raw is None or not raw.strip():
        return None
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


class OrchestratorCalls:
    def __init__(self, orch: Any):
        self._orch = orch

    def published_tools(self, owner_id: str) -> List[Dict[str, Any]]:
        return self.published_selection(owner_id)[0]

    def published_selection(self, owner_id: str) -> Tuple[List[Dict[str, Any]], int]:
        allowed = published_allowlist()
        entries = [
            (agent_id, tool) for agent_id, tool in self._entries(owner_id)
            if self._remotely_callable(owner_id, agent_id, tool["name"])
            and (allowed is None or f"{agent_id}__{tool['name']}" in allowed)
        ]
        entries.sort(key=lambda entry: (entry[0], entry[1]["name"]))
        kept = entries[:CATALOG_LIMIT]
        return [tool for _, tool in kept], len(entries) - len(kept)

    def _remotely_callable(self, owner_id: str, agent_id: str, name: str) -> bool:
        try:
            if self._orch._is_draft_agent(agent_id):
                return False
            return self._orch.tool_permissions.is_tool_allowed(owner_id, agent_id, name) is True
        except Exception:  # noqa: BLE001
            return False

    def catalog(self, owner_id: str) -> Dict[Tuple[str, str], CatalogTool]:
        return {
            (agent_id, tool["name"]): CatalogTool(
                definition_hash(tool["name"], tool["description"], tool["input_schema"], tool["annotations"]),
                tool["effect"],
            )
            for agent_id, tool in self._entries(owner_id)
        }

    def turn_tools(self, owner_id: str, grants: Any) -> List[TurnTool]:
        permissions = self._orch.tool_permissions
        found: List[TurnTool] = []
        for (agent_id, name), entry in sorted(self.catalog(owner_id).items()):
            if current_definition(self._orch, agent_id, name) != entry.definition:
                continue
            if not grant_covers(grants, name, entry.definition, entry.effect):
                continue
            if not permissions.is_tool_allowed(owner_id, agent_id, name):
                continue
            found.append(TurnTool(agent_id, name, entry.definition, permissions.get_tool_scope(agent_id, name)))
        return found

    def _entries(self, owner_id: str) -> List[Tuple[str, Dict[str, Any]]]:
        tools = [
            (tool["annotations"][vessel_executor.AGENT_ANNOTATION], tool)
            for tool in vessel_executor.published_tools(self._orch)
        ]
        local_agents = getattr(self._orch, "local_agents", {})
        if type(owner_id) is str and owner_id and isinstance(local_agents, dict):
            from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS
            from orchestrator.tool_visibility import eligible_tool_pairs

            first_party = frozenset(FIRST_PARTY_PUBLIC_AGENT_IDS)
            for agent_id, skill in eligible_tool_pairs(self._orch, owner_id):
                if agent_id not in first_party or agent_id not in local_agents:
                    continue
                name = str(getattr(skill, "id", "") or "")
                if not name:
                    continue
                description = str(getattr(skill, "description", "") or "")
                schema = getattr(skill, "input_schema", None)
                schema = schema if isinstance(schema, dict) else {}
                metadata = getattr(skill, "metadata", None)
                metadata = metadata if isinstance(metadata, dict) else {}
                annotations = sanitize_annotations(metadata.get("annotations"))
                scope = self._orch.tool_permissions.get_tool_scope(agent_id, name)
                tools.append((agent_id, {
                    "name": name,
                    "description": description,
                    "input_schema": schema,
                    "annotations": annotations,
                    "effect": "outward_read" if scope in READ_SCOPES else "outside_effect",
                }))
        counts = Counter(tool.get("name") for _, tool in tools)
        ambiguous = {name for name, count in counts.items() if count > 1}
        return [(agent_id, tool) for agent_id, tool in tools if tool.get("name") not in ambiguous]

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
        from orchestrator import node_mesh_admission
        from orchestrator.node_mesh import NodeMeshError
        from shared import node_identity

        if node_identity.is_personal():
            bound = node_mesh_admission.current_admission()
            if bound is None or bound.resolved != resolved:
                raise CallRefused("request.call_mismatch")
            try:
                return await node_mesh_admission.personal_authority(self._orch, owner_id, agent_id=resolved.agent_id)
            except NodeMeshError as exc:
                raise CallRefused(exc.code) from exc

        bound = None
        if node_identity.is_institution():
            bound = node_mesh_admission.current_admission()
            if (bound is None or bound.resolved != resolved or bound.owner != owner_id
                    or bound.owner_of() != owner_id or bound.request is None or bound.request.call is None):
                raise CallRefused("node.mesh_admission_unavailable")
            try:
                await bound.mesh.personal_authority_context(bound.request, owner_id, bound.kernel)
                if bound.check_current is not None:
                    await bound.check_current()
            except NodeMeshError as exc:
                raise CallRefused(exc.code) from exc

        authority = await self._orch.derive_machine_authority(
            user_id=owner_id, agent_id=resolved.agent_id, turn_class="mesh_request", consented_scopes=[resolved.scope]
        )
        if isinstance(authority, AuthoritySkip):
            raise CallRefused(dotted(f"node.authority_{authority.reason}"))
        if (bound is not None and (node_mesh_admission.current_admission() is not bound
                                   or bound.owner_of() != owner_id)):
            raise CallRefused("node.owner_mismatch")
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
        vws._node_mesh_peer_call = True
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
            return CallOutcome("error", "The node could not confirm the tool outcome.", "node.outcome_unknown")
        finally:
            self._orch._unbind_machine_turn(vws)
            await vws.close()
        if response is None:
            return CallOutcome("error", "The tool returned no confirmed outcome.", "node.outcome_unknown")
        if response.error is not None:
            error = response.error
            return CallOutcome("error", _text(error.get("message") or "The tool refused the call."), dotted(error.get("code") or "tool_failed"))
        return CallOutcome("ok", _text(response.result if response.result is not None else ""))


def _matches(requested: str, agent_id: str, skill_id: str) -> bool:
    if QUALIFIER in requested:
        return requested == f"{agent_id}{QUALIFIER}{skill_id}"
    return requested == skill_id
