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
        return {key: _scrub(item, secrets, depth + 1) for key, item in value.items()}
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
                   outcome: str, detail: str | None = None) -> None:
    try:
        from audit.hooks import record_generic

        inputs_meta, outputs_meta = _audit_fields(name, public, data)
        record_coro = record_generic(
            claims={"sub": user_id or "unknown", "act": {"sub": AUDIT_ACTOR_AGENT}},
            event_class="agent_tool_call",
            action_type=f"tool.{name}.fabric",
            description=f"Cresco bridge {name} on target {public.get('target', '')}".strip()[:200],
            inputs_meta=inputs_meta,
            outputs_meta=outputs_meta,
            outcome=outcome,
            outcome_detail=detail,
        )
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(record_coro)
            return
        asyncio.ensure_future(record_coro)
    except Exception:  # noqa: BLE001
        pass


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
            rows = [[str(region.get("region", region)) if isinstance(region, dict) else str(region)]
                    for region in data.get("regions", [])]
            return [Table(title="Cresco regions", headers=["region"], rows=rows).to_dict()]
        if name == "cresco_list_agents":
            rows = [[str(agent.get("agent", agent)) if isinstance(agent, dict) else str(agent)]
                    for agent in data.get("agents", [])]
            return [Table(title="Cresco agents", headers=["agent"], rows=rows).to_dict()]
        if name in ("cresco_dispatch_work", "cresco_run_command"):
            return [KeyValue(title="Dispatch result", items=[
                {"label": label, "value": _scrub(value, ())}
                for label, value in (("target", data.get("target", "")),
                                     ("work_id", data.get("work_id", "")),
                                     ("verdict", data.get("verdict", "")),
                                     ("result", data.get("result", {})))
            ]).to_dict()]
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
        import os

        secrets = tuple(secret for secret in [os.getenv("CRESCO_SERVICE_KEY", "")] if secret)
        data = _scrub(data, secrets)
        if name in ("cresco_dispatch_work", "cresco_run_command") and data.get("verdict") == "unconfirmed":
            _record_effect(user_id, name, public, data, outcome="failure",
                           detail=str(data.get("reason") or data.get("detail") or "unconfirmed"))
            return {"_error": {"code": "CRESCO_RESULT_UNCONFIRMED",
                               "message": ("The work order was dispatched but no confirmed result "
                                           "arrived in the window; check native fabric state "
                                           "before retrying. Retain the work id."),
                               "retryable": False,
                               "data": {"work_id": data.get("work_id"), "target": data.get("target"),
                                        "reason": data.get("reason") or data.get("detail")}}}
        if data.get("verdict") == "faulted":
            response = self._fault_response("", name, public, str(data.get("fault", "protocol")),
                                            str(data.get("detail", "")),
                                            retryable=bool(data.get("retryable")), user_id=user_id,
                                            data={"work_id": data.get("work_id"),
                                                  "target": data.get("target")})
            return {"_error": response.error}
        _record_effect(user_id, name, public, data, outcome="success")
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
