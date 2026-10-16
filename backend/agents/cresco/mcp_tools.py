"""Cresco bridge tool handlers and TOOL_REGISTRY: read-only topology reads, the
target-bound work-dispatch effect, and the hard-gated executor wrapper. mcp_server.py
routes MCP requests here and owns admission errors, audit, and rendering; handlers stay
transport-thin over wsapi_client.WsapiClient.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from agents.cresco.targets import TargetRefused, require_approved
from agents.cresco.wsapi_client import (
    FabricConfig,
    FabricFault,
    UnconfirmedResult,
    WsapiClient,
    _MAX_WORK_CHARS,
)

MAX_ROWS = 50
MAX_TIMEOUT_SECONDS = 120
EXECUTOR_AGENT_ID = "cresco-1"
EXECUTOR_TOOL_NAME = "cresco_run_command"
_WORK_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class ToolContext:
    user_id: str
    orchestrator: Any = None
    config: FabricConfig | None = None
    client_factory: Callable[[FabricConfig], WsapiClient] = WsapiClient

    def client(self) -> WsapiClient:
        return self.client_factory(self.config or FabricConfig.from_env())


def _require_flag(arguments: dict) -> None:
    from shared.feature_flags import flags

    if not flags.is_enabled("cresco"):
        raise FabricFault("unavailable", retryable=False, detail="feature_flag_off")


def _require_configured(ctx: ToolContext) -> WsapiClient:
    _require_flag({})
    client = ctx.client()
    if not (ctx.config or FabricConfig.from_env()).is_configured:
        raise FabricFault("unavailable", retryable=False, detail="fabric_not_configured")
    return client


def _rows(payload: dict, key: str) -> list:
    rows = payload.get(key)
    if not isinstance(rows, list):
        raise FabricFault("protocol", retryable=False, detail="unexpected_topology_shape")
    return rows[:MAX_ROWS]


def list_regions(_ctx: ToolContext, **_arguments) -> dict:
    client = _require_configured(_ctx)
    payload = client.call("listregions", {})
    return {"regions": _rows(payload, "regions")}


def list_agents(_ctx: ToolContext, region: str | None = None, **_arguments) -> dict:
    client = _require_configured(_ctx)
    if region is not None and (not isinstance(region, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", region)):
        raise TargetRefused("invalid_target", "The region is not a bounded identifier.")
    payload = client.call("listagents", {"region": region} if region else {})
    return {"region": region, "agents": _rows(payload, "agents")}


def agent_info(ctx: ToolContext, target: str = "", **_arguments) -> dict:
    client = _require_configured(ctx)
    parsed = require_approved(target)
    payload = client.call("agentinfo", {"region_agent": "_".join(parsed)})
    return {"target": "_".join(parsed), "info": payload.get("info") if isinstance(payload.get("info"), dict) else payload}


def dispatch_work(ctx: ToolContext, target: str = "", work_kind: str = "",
                  work_payload: str | None = None, timeout_seconds: float = 30.0,
                  **_arguments) -> dict:
    client = _require_configured(ctx)
    parsed = require_approved(target)
    target_text = "_".join(parsed)
    if not isinstance(work_kind, str) or not _WORK_KIND.match(work_kind):
        raise TargetRefused("invalid_work_kind", "The work kind must be a short lowercase identifier.")
    if work_payload is not None and (not isinstance(work_payload, str) or len(work_payload) > _MAX_WORK_CHARS):
        raise TargetRefused("invalid_work_payload", "The work payload must be bounded text.")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) \
            or not 1.0 <= float(timeout_seconds) <= MAX_TIMEOUT_SECONDS:
        raise TargetRefused("invalid_timeout", "The result window must be between 1 and 120 seconds.")
    work_id = uuid.uuid4().hex
    work = {
        "kind": work_kind,
        "payload_text": work_payload or "",
        "requested_by": ctx.user_id,
        "created_at_epoch": int(time.time()),
    }
    try:
        outcome = client.submit_work(target_text, work_id, work,
                                     timeout=float(timeout_seconds))
    except FabricFault as exc:
        return {"verdict": "unconfirmed" if exc.after_send else "faulted",
                "fault": exc.state, "detail": exc.detail,
                "target": target_text, "work_id": work_id,
                "retryable": exc.retryable}
    except UnconfirmedResult as exc:
        return {"verdict": "unconfirmed", "reason": exc.reason,
                "target": target_text, "work_id": exc.work_id}
    return {"verdict": "completed", "target": target_text,
            "work_id": outcome["work_id"], "result": outcome.get("result", {})}


def run_command(ctx: ToolContext, target: str = "", command: str = "",
                timeout_seconds: float = 30.0, **_arguments) -> dict:
    import os

    _require_flag({})
    if os.getenv("CRESCO_EXECUTOR_ENABLED", "").strip().lower() not in ("true", "1", "yes"):
        raise FabricFault("unavailable", retryable=False, detail="executor_disabled_by_operator")
    permissions = getattr(ctx.orchestrator, "tool_permissions", None)
    if not ctx.user_id or permissions is None or not permissions.is_tool_allowed(
            ctx.user_id, EXECUTOR_AGENT_ID, EXECUTOR_TOOL_NAME):
        raise TargetRefused(
            "executor_not_authorized",
            "The executor requires an explicit per-user permission override; the request was refused.",
        )
    return run_command_authorized(ctx, target=target, command=command,
                                  timeout_seconds=timeout_seconds)


def run_command_authorized(ctx: ToolContext, target: str = "", command: str = "",
                           timeout_seconds: float = 30.0, **_arguments) -> dict:
    client = _require_configured(ctx)
    parsed = require_approved(target)
    if not isinstance(command, str) or not command.strip() or len(command) > 2000:
        raise TargetRefused("invalid_command", "A bounded command line is required.")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) \
            or not 1.0 <= float(timeout_seconds) <= MAX_TIMEOUT_SECONDS:
        raise TargetRefused("invalid_timeout", "The result window must be between 1 and 120 seconds.")
    command_id = uuid.uuid4().hex
    try:
        payload = client.call("command", {
            "region_agent": "_".join(parsed),
            "command_id": command_id,
            "command": command,
        }, timeout=float(timeout_seconds))
    except FabricFault as exc:
        return {"verdict": "unconfirmed" if exc.after_send else "faulted",
                "fault": exc.state, "detail": exc.detail,
                "target": "_".join(parsed), "work_id": command_id,
                "retryable": exc.retryable}
    except UnconfirmedResult as exc:
        return {"verdict": "unconfirmed", "reason": exc.reason,
                "target": "_".join(parsed), "work_id": exc.work_id}
    return {"verdict": "completed", "target": "_".join(parsed),
            "work_id": str(payload.get("command_id") or command_id),
            "result": payload.get("result") if isinstance(payload.get("result"), dict) else {}}


TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "cresco_list_regions": {
        "function": list_regions,
        "scope": "tools:read",
        "description": ("Lists the regions known to the operator-configured Cresco fabric: "
                        "bounded read-only topology for approved-fabric awareness."),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "cresco_list_agents": {
        "function": list_agents,
        "scope": "tools:read",
        "description": ("Lists the agents of one region, or of every region, on the "
                        "operator-configured Cresco fabric. Bounded read-only topology."),
        "input_schema": {
            "type": "object",
            "properties": {
                "region": {"type": "string", "description": "Optional region to scope the listing."},
            },
            "additionalProperties": False,
        },
    },
    "cresco_agent_info": {
        "function": agent_info,
        "scope": "tools:read",
        "description": ("Reads status and plugin information for one operator-approved "
                        "Cresco target (region_agent[_plugin]). Bounded read-only telemetry."),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Approved target as region_agent[_plugin]."},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    "cresco_dispatch_work": {
        "function": dispatch_work,
        "scope": "tools:write",
        "description": ("Dispatches a bounded work order to one operator-approved Cresco "
                        "target and waits for its correlated result within a bounded window. "
                        "The target must already be operator-approved; approval is never "
                        "granted by discovery."),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Approved target as region_agent[_plugin]."},
                "work_kind": {"type": "string", "description": "Short lowercase work-kind identifier."},
                "work_payload": {"type": "string", "description": "Optional bounded work text."},
                "timeout_seconds": {"type": "number", "description": "Result window, 1-120 seconds."},
            },
            "required": ["target", "work_kind"],
            "additionalProperties": False,
        },
    },
    "cresco_run_command": {
        "function": run_command,
        "scope": "tools:system",
        "description": ("Runs an operating-system command on a Cresco fabric node through "
                        "the fabric executor plugin: remote arbitrary command execution. "
                        "System-scoped, hard security-flagged and default-deny."),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Approved executor target as region_agent_plugin."},
                "command": {"type": "string", "description": "Bounded command line for the fabric executor."},
                "timeout_seconds": {"type": "number", "description": "Result window, 1-120 seconds."},
            },
            "required": ["target", "command"],
            "additionalProperties": False,
        },
    },
}
