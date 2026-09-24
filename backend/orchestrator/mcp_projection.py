"""Projects the caller's live tool catalog into MCP tool definitions, gating each by the
normal chat visibility and permission checks, or, for a resolved framework
credential, by that credential's own scopes alone. Used by mcp_server_endpoint.py.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from shared.schema_validation import JSON_SCHEMA_2020_12

from orchestrator.tool_visibility import eligible_tool_pairs

FRAMEWORK_WORK_AGENT_ID = "__work__"

_OPERATION_ID = {"type": "string", "description": "The operation id returned by astral_submit_operation."}
_WORK_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "astral_submit_operation": {
        "scope": "operations.submit",
        "description": "Submit one chat-kind Work operation for the credential's owner.",
        "readOnly": False,
        "input_schema": {
            "type": "object",
            "required": ["idempotency_key", "name", "instructions"],
            "properties": {
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256,
                                    "description": "Client-chosen key; a repeat call with the SAME key "
                                                   "and content returns the original operation unchanged."},
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "instructions": {"type": "string", "minLength": 1, "maxLength": 4096},
                "conversation_id": {"type": ["string", "null"]},
                "deadline_in_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
            },
        },
    },
    "astral_get_operation": {
        "scope": "operations.read",
        "description": "Read one Work operation's current status.",
        "readOnly": True,
        "input_schema": {"type": "object", "required": ["operation_id"],
                         "properties": {"operation_id": _OPERATION_ID}},
    },
    "astral_list_operations": {
        "scope": "operations.read",
        "description": "List the credential owner's recent Work operations.",
        "readOnly": True,
        "input_schema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "after_id": {"type": ["string", "null"]},
        }},
    },
    "astral_get_operation_events": {
        "scope": "operations.read",
        "description": "Poll one Work operation for a revision change.",
        "readOnly": True,
        "input_schema": {"type": "object", "required": ["operation_id"], "properties": {
            "operation_id": _OPERATION_ID,
            "after_revision": {"type": ["integer", "null"]},
        }},
    },
    "astral_cancel_operation": {
        "scope": "operations.control",
        "description": "Cancel one Work operation.",
        "readOnly": False,
        "input_schema": {"type": "object",
                         "required": ["operation_id", "submission_id", "expected_revision"],
                         "properties": {
                             "operation_id": _OPERATION_ID,
                             "submission_id": {"type": "string", "description": "A fresh UUID4 per attempt."},
                             "expected_revision": {"type": "integer", "minimum": 1},
                         }},
    },
    "astral_pause_operation": {
        "scope": "operations.control",
        "description": "Pause one Work operation.",
        "readOnly": False,
        "input_schema": {"type": "object",
                         "required": ["operation_id", "submission_id", "expected_revision"],
                         "properties": {
                             "operation_id": _OPERATION_ID,
                             "submission_id": {"type": "string", "description": "A fresh UUID4 per attempt."},
                             "expected_revision": {"type": "integer", "minimum": 1},
                         }},
    },
    "astral_get_artifact": {
        "scope": "artifacts.read",
        "description": "Read one Work operation's retained result.",
        "readOnly": True,
        "input_schema": {"type": "object", "required": ["operation_id"],
                         "properties": {"operation_id": _OPERATION_ID}},
    },
}


def _framework_tools(claims: dict[str, Any] | None) -> tuple["ProjectedTool", ...]:
    scopes = set((claims or {}).get("_framework_scopes") or ())
    if not scopes:
        return ()
    from orchestrator.work_operations import DISPATCHABLE_TOOL_NAMES

    projected: list[ProjectedTool] = []
    for name in DISPATCHABLE_TOOL_NAMES:
        spec = _WORK_TOOL_SPECS.get(name)
        if spec is None or spec["scope"] not in scopes:
            continue
        descriptor: dict[str, Any] = {
            "name": name,
            "description": spec["description"],
            "inputSchema": _schema(spec["input_schema"]),
            "annotations": {"readOnlyHint": spec["readOnly"], "destructiveHint": False},
            "_meta": {"astral/agentId": FRAMEWORK_WORK_AGENT_ID, "astral/requiredScope": spec["scope"]},
        }
        projected.append(ProjectedTool(name=name, agent_id=FRAMEWORK_WORK_AGENT_ID,
                                       skill_id=name, descriptor=descriptor))
    return tuple(projected)


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
    if isinstance(claims, dict) and claims.get("_framework_scopes"):
        return _framework_tools(claims)
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
