"""Per-principal projection of Astral's live tool catalog into MCP tools."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from shared.schema_validation import JSON_SCHEMA_2020_12

from orchestrator.tool_visibility import eligible_tool_pairs


@dataclass(frozen=True)
class ProjectedTool:
    name: str
    agent_id: str
    skill_id: str
    descriptor: dict[str, Any]


def _destructive_hint(agent_id: str, skill: Any) -> bool:
    metadata = getattr(skill, "metadata", None) or {}
    classification = metadata.get("destructive")
    if agent_id == "remote-compute-1":
        from orchestrator.remote_confirmation import classification_for

        declared = classification_for(str(getattr(skill, "id", "")))
        if declared is not None:
            classification = declared
    return classification not in (None, False, "never")


def _schema(value: Any) -> dict[str, Any]:
    projected = copy.deepcopy(value) if isinstance(value, dict) else {
        "type": "object",
        "properties": {},
    }
    projected.setdefault("$schema", JSON_SCHEMA_2020_12)
    return projected


def _eligible_pairs(
    orchestrator: Any,
    user_id: str,
    claims: dict[str, Any] | None = None,
) -> list[tuple[str, Any]]:
    """Mirror the normal-chat visibility and permission gates, fail closed."""

    disabled = set(orchestrator.tool_permissions.list_disabled_agents(user_id))
    return eligible_tool_pairs(
        orchestrator,
        user_id,
        disabled_agents=disabled,
        identity_claims=claims,
    )


def project_tools(
    orchestrator: Any,
    user_id: str,
    claims: dict[str, Any] | None = None,
) -> tuple[ProjectedTool, ...]:
    pairs = _eligible_pairs(orchestrator, user_id, claims)
    owners: dict[str, set[str]] = {}
    for agent_id, skill in pairs:
        owners.setdefault(skill.id, set()).add(agent_id)

    projected: list[ProjectedTool] = []
    for agent_id, skill in pairs:
        collides = len(owners.get(skill.id, ())) > 1
        name = f"{agent_id}__{skill.id}" if collides else skill.id
        description = str(skill.description or "")
        if collides:
            description = f"[Provider: {agent_id}] {description}"
        descriptor: dict[str, Any] = {
            "name": name,
            "description": description,
            "inputSchema": _schema(skill.input_schema),
            "annotations": {
                "readOnlyHint": skill.scope in {"tools:read", "tools:search"},
                "destructiveHint": _destructive_hint(agent_id, skill),
            },
            "_meta": {
                "astral/agentId": agent_id,
                "astral/requiredScope": skill.scope or "",
            },
        }
        if isinstance(skill.output_schema, dict):
            descriptor["outputSchema"] = _schema(skill.output_schema)
        projected.append(
            ProjectedTool(
                name=name,
                agent_id=agent_id,
                skill_id=skill.id,
                descriptor=descriptor,
            )
        )
    return tuple(sorted(projected, key=lambda item: (item.name, item.agent_id)))


def resolve_projected_tool(
    orchestrator: Any,
    user_id: str,
    name: str,
    claims: dict[str, Any] | None = None,
) -> ProjectedTool | None:
    return next(
        (tool for tool in project_tools(orchestrator, user_id, claims) if tool.name == name),
        None,
    )


__all__ = ["ProjectedTool", "project_tools", "resolve_projected_tool"]
