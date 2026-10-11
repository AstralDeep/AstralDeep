"""Projects the caller's live tool catalog into MCP tool definitions, gating each by the
normal chat visibility and permission checks, or, for a resolved framework
credential, by that credential's own scopes alone. Tools whose schemas carry an
x-mcp-header annotation outside the bounded supported form are withheld, and
supported annotations are returned as validated mirror parameters for
mcp_server_endpoint.py to match against Mcp-Param mirror headers before dispatch.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass
from typing import Any

from shared.schema_validation import JSON_SCHEMA_2020_12

from orchestrator.tool_visibility import eligible_tool_pairs

logger = logging.getLogger("MCPProjection")

MIRROR_ANNOTATION = "x-mcp-header"
MIRROR_HEADER_PREFIX = "mcp-param-"
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})

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
    "astral_emergency_status": {
        "scope": "operations.read",
        "description": "Read the credential owner's emergency stop status: running, "
                       "stopped, partially acknowledged, or unreachable responders.",
        "readOnly": True,
        "input_schema": {"type": "object", "properties": {}},
    },
    "astral_emergency_stop": {
        "scope": "operations.control",
        "description": "Engage the owner's emergency stop: refuse new tool admission, "
                       "agent dispatch, scheduled turns, and remote execution until an "
                       "explicit resume.",
        "readOnly": False,
        "input_schema": {"type": "object",
                         "properties": {"reason": {"type": "string", "maxLength": 280}},
                         "additionalProperties": False},
    },
    "astral_emergency_resume": {
        "scope": "operations.control",
        "description": "Explicitly resume after an emergency stop; requires the current "
                       "stop revision and the engaging owner's credential.",
        "readOnly": False,
        "input_schema": {"type": "object", "required": ["expected_revision"],
                         "properties": {"expected_revision": {"type": "integer", "minimum": 1}},
                         "additionalProperties": False},
    },
}


def _annotation_paths(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    found: list[tuple[Any, ...]] = []
    if isinstance(value, dict):
        for key, member in value.items():
            if key == MIRROR_ANNOTATION:
                found.append((*path, key))
            found.extend(_annotation_paths(member, (*path, key)))
    elif isinstance(value, list):
        for index, member in enumerate(value):
            found.extend(_annotation_paths(member, (*path, index)))
    return found


def _scalar_annotation(property_schema: Any) -> bool:
    declared = property_schema.get("type") if isinstance(property_schema, dict) else None
    if isinstance(declared, str):
        return declared in _SCALAR_TYPES
    if isinstance(declared, list) and declared:
        members = [member for member in declared if isinstance(member, str)]
        return (
            len(members) == len(declared)
            and any(member in _SCALAR_TYPES for member in members)
            and all(member in _SCALAR_TYPES or member == "null" for member in members)
        )
    return False


def _header_mirror_params(schema: Any) -> tuple[MirrorParam, ...] | None:
    occurrences = _annotation_paths(schema)
    if not occurrences:
        return ()
    properties = schema.get("properties") if isinstance(schema, dict) else None
    params: list[MirrorParam] = []
    for path in occurrences:
        if len(path) != 3 or path[0] != "properties" or not isinstance(path[1], str):
            return None
        property_schema = properties.get(path[1]) if isinstance(properties, dict) else None
        header = (
            property_schema.get(MIRROR_ANNOTATION)
            if isinstance(property_schema, dict)
            else None
        )
        if not _scalar_annotation(property_schema):
            return None
        if not isinstance(header, str) or not _HEADER_NAME.fullmatch(header):
            return None
        if not header.lower().startswith(MIRROR_HEADER_PREFIX):
            return None
        if len(header) == len(MIRROR_HEADER_PREFIX):
            return None
        params.append(MirrorParam(argument=path[1], header=header))
    if len({param.header.lower() for param in params}) != len(params):
        return None
    return tuple(params)


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
                                       skill_id=name, descriptor=descriptor,
                                       mirror_params=_header_mirror_params(spec["input_schema"]) or ()))
    return tuple(projected)


@dataclass(frozen=True)
class MirrorParam:
    argument: str
    header: str


@dataclass(frozen=True)
class ProjectedTool:
    name: str
    agent_id: str
    skill_id: str
    descriptor: dict[str, Any]
    mirror_params: tuple[MirrorParam, ...] = ()
    mirrors_supported: bool = True


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


def _projected_tools(
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
        mirror_params = _header_mirror_params(skill.input_schema)
        output_annotated = isinstance(skill.output_schema, dict) and bool(
            _annotation_paths(skill.output_schema)
        )
        mirrors_supported = mirror_params is not None and not output_annotated
        if not mirrors_supported:
            logger.warning(
                "mcp projection withheld tool agent=%s skill=%s "
                "reason=unsupported_x_mcp_header_annotation",
                agent_id,
                skill.id,
            )
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
                mirror_params=mirror_params or (),
                mirrors_supported=mirrors_supported,
            )
        )
    return tuple(sorted(projected, key=lambda item: (item.name, item.agent_id)))


def project_tools(
    orchestrator: Any,
    user_id: str,
    claims: dict[str, Any] | None = None,
) -> tuple[ProjectedTool, ...]:
    return tuple(
        tool
        for tool in _projected_tools(orchestrator, user_id, claims)
        if tool.mirrors_supported
    )


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


def has_unsupported_mirrors(
    orchestrator: Any,
    user_id: str,
    name: str,
    claims: dict[str, Any] | None = None,
) -> bool:
    return any(
        tool.name == name and not tool.mirrors_supported
        for tool in _projected_tools(orchestrator, user_id, claims)
    )


__all__ = [
    "MirrorParam",
    "ProjectedTool",
    "has_unsupported_mirrors",
    "project_tools",
    "resolve_projected_tool",
]
