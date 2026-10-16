"""Routes MCP requests for the Cresco bridge: enforces the flag/config/authorization gates
at effect time, maps faults to typed public errors, scrubs secrets, renders results with
astralprims, and records fabric identifiers into the hash-chained audit. Registered
in-process by orchestrator/local_agents.py only when FF_CRESCO is on.
"""

from __future__ import annotations

from typing import Any

from astralprims import KeyValue, Table
from shared.feature_flags import flags
from shared.protocol import MCPRequest, MCPResponse

from agents.cresco import mcp_tools
from agents.cresco.mcp_tools import ToolContext
from agents.cresco.targets import TargetRefused
from agents.cresco.wsapi_client import FabricFault

CONTEXT_FIELDS = frozenset({
    "user_id", "session_id", "_runtime", "_credentials", "_credentials_stale",
    "_credentials_encrypted", "_session_llm_credentials", "_session_llm_config",
    "_delegation_token", "_cap_job_id",
})

AUDIT_ACTOR_AGENT = "agent:cresco-1"

_FAULT_CODES = {
    "unavailable": "CRESCO_NOT_CONFIGURED",
    "unreachable": "CRESCO_UNREACHABLE",
    "auth": "CRESCO_AUTH_FAILED",
    "protocol": "CRESCO_INVALID_RESPONSE",
    "tls": "CRESCO_TLS_FAILED",
}


def bind_owner_context(arguments: dict, user_id: Any) -> None:
    arguments["user_id"] = user_id


def _scrub(value: Any, secrets: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 4:
        return "[bounded]"
    if isinstance(value, dict):
        return {_scrub(key, secrets): _scrub(item, secrets, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secrets, depth + 1) for item in value[:64]]
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                return "[REDACTED]"
        return value if len(value) <= 2000 else value[:1997] + "..."
    return value


def _audit_fields(name: str, public: dict, data: dict) -> tuple[dict, dict]:
    inputs_meta: dict[str, Any] = {"cresco_tool": name}
    target = public.get("target")
    if isinstance(target, str) and target:
        inputs_meta["cresco_target"] = target[:192]
    if isinstance(public.get("work_kind"), str):
        inputs_meta["cresco_work_kind"] = public["work_kind"][:32]
    outputs_meta: dict[str, Any] = {}
    for key in ("work_id", "verdict", "fault", "reason"):
        if isinstance(data.get(key), (str, bool)) and data.get(key) not in (None, ""):
            outputs_meta[f"cresco_{key}"] = str(data[key])[:64]
    return inputs_meta, outputs_meta


def _record_effect(user_id: str, name: str, public: dict, data: dict, *,
                   outcome: str, detail: str | None = None) -> bool:
    from audit.recorder import get_recorder, make_correlation_id, now_utc
    from audit.schemas import AuditEventCreate

    recorder = get_recorder()
    if recorder is None:
        return False
    try:
        import os

        secrets = tuple(secret for secret in [os.getenv("CRESCO_SERVICE_KEY", "")] if secret)
        public = _scrub(public, secrets)
        inputs_meta, outputs_meta = _audit_fields(name, public, _scrub(data, secrets))
        return recorder.record_blocking(AuditEventCreate(
            actor_user_id=user_id or "unknown",
            auth_principal=AUDIT_ACTOR_AGENT,
            event_class="agent_tool_call",
            action_type=f"tool.{name}.fabric",
            description=f"Cresco bridge {name} on target {public.get('target', '')}".strip()[:200],
            correlation_id=make_correlation_id(),
            started_at=now_utc(),
            inputs_meta=inputs_meta,
            outputs_meta=outputs_meta,
            outcome=outcome,
            outcome_detail=detail,
        )) is not None
    except Exception:  # noqa: BLE001
        return False


class MCPServer:
    def __init__(self, orchestrator: Any = None):
        self.orchestrator = orchestrator
        self.tools = {name: dict(info) for name, info in mcp_tools.TOOL_REGISTRY.items()}

    def get_tool_list(self) -> list[dict]:
        return [
            {"name": name, "description": info["description"],
             "input_schema": info["input_schema"], "scope": info["scope"]}
            for name, info in self.tools.items()
        ]

    def _fault_response(self, request_id: str, name: str, public: dict,
                        state: str, detail: str, *, retryable: bool,
                        user_id: str, data: dict | None = None) -> MCPResponse:
        code = _FAULT_CODES.get(state, "CRESCO_INVALID_RESPONSE")
        if state == "unavailable" and detail == "audit_unavailable":
            code = "CRESCO_AUDIT_UNAVAILABLE"
        elif state == "unavailable" and detail in ("dispatch_protocol_unavailable", "executor_result_transport_unavailable"):
            code = "CRESCO_OPERATION_UNAVAILABLE"
        payload = {"fault": state, "detail": detail}
        if data:
            payload.update(data)
        _record_effect(user_id, name, public, payload, outcome="failure", detail=detail)
        return MCPResponse(
            request_id=request_id,
            error={"code": code, "message": f"Cresco bridge fault: {state}.",
                   "retryable": retryable, "data": payload},
        )

    def _completed_components(self, name: str, data: dict) -> list[dict]:
        if name == "cresco_list_regions":
            rows = [[str(region.get("name", region.get("region_id", region.get("region", region)))) if isinstance(region, dict) else str(region)]
                    for region in data.get("regions", [])]
            return [Table(title="Cresco regions", headers=["region"], rows=rows).to_dict()]
        if name == "cresco_list_agents":
            rows = [[(f"{agent['region_id']}_{agent['agent_id']}" if "region_id" in agent and "agent_id" in agent
                      else str(agent.get("agent", agent))) if isinstance(agent, dict) else str(agent)]
                    for agent in data.get("agents", [])]
            return [Table(title="Cresco agents", headers=["agent"], rows=rows).to_dict()]
        return [KeyValue(title="Cresco target", items=[
            {"label": key, "value": _scrub(value, ())} for key, value in data.items()
        ]).to_dict()]

    def invoke(self, name: str, **arguments: Any) -> dict:
        public = {key: value for key, value in arguments.items() if key not in CONTEXT_FIELDS}
        user_id = arguments.get("user_id")
        user_id = user_id if isinstance(user_id, str) and user_id else ""
        ctx = ToolContext(
            user_id=user_id,
            orchestrator=self.orchestrator,
        )
        try:
            if not flags.is_enabled("cresco"):
                raise FabricFault("unavailable", retryable=False, detail="feature_flag_off")
            handler = mcp_tools.TOOL_REGISTRY[name]["function"]
            if not _record_effect(user_id, name, public, {}, outcome="in_progress"):
                raise FabricFault("unavailable", retryable=False, detail="audit_unavailable")
            data = handler(ctx, **public)
        except TargetRefused as exc:
            _record_effect(user_id, name, public, {"verdict": "refused", "detail": exc.code},
                           outcome="failure", detail=exc.code)
            return {"_error": {"code": "CRESCO_TARGET_REFUSED" if exc.code == "target_not_approved"
                               else "CRESCO_BAD_REQUEST",
                               "message": str(exc), "retryable": False,
                               "data": {"detail": exc.code}}}
        except FabricFault as exc:
            response = self._fault_response("", name, public, exc.state, exc.detail,
                                            retryable=exc.retryable, user_id=user_id)
            return {"_error": response.error}
        except Exception:
            response = self._fault_response(
                "", name, public, "protocol", "unexpected_handler_failure",
                retryable=False, user_id=user_id,
            )
            return {"_error": response.error}
        import os

        secrets = tuple(secret for secret in [os.getenv("CRESCO_SERVICE_KEY", "")] if secret)
        data = _scrub(data, secrets)
        if not _record_effect(user_id, name, public, data, outcome="success"):
            response = self._fault_response("", name, public, "unavailable", "audit_unavailable",
                                            retryable=False, user_id=user_id)
            return {"_error": response.error}
        return {"_data": data, "_ui_components": self._completed_components(name, data)}

    def process_request(self, request: MCPRequest) -> MCPResponse:
        if request.method == "tools/list":
            return MCPResponse(request_id=request.request_id, result={"tools": self.get_tool_list()})
        if request.method != "tools/call":
            return MCPResponse(
                request_id=request.request_id,
                error={"code": -32601, "message": "Unknown MCP method.", "retryable": False},
            )
        params = request.params or {}
        name, arguments = params.get("name"), params.get("arguments")
        if name not in self.tools or not isinstance(arguments, dict):
            return MCPResponse(
                request_id=request.request_id,
                error={"code": -32602, "message": "Invalid Cresco tool request.", "retryable": False},
            )
        result = self.invoke(name, **arguments)
        if "_error" in result:
            return MCPResponse(
                request_id=request.request_id,
                error=result["_error"],
            )
        return MCPResponse(
            request_id=request.request_id,
            result=result["_data"],
            ui_components=result["_ui_components"],
        )
