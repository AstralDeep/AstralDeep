"""Self-contained A2A agent server for the Windows tools.

Speaks exactly the handshake the orchestrator's `discover_agent` expects:
  GET /.well-known/agent-card.json  -> the AgentCard
  WS  /agent                        -> sends RegisterAgent, then answers
                                       MCPRequest (tools/list, tools/call)
                                       with MCPResponse.

Binds 0.0.0.0 so a Dockerized orchestrator can reach it on the host via
host.docker.internal. Runs on the user's Windows machine, so the tools execute
locally. No dependency on the backend package.

Run standalone:  python -m win_agent.agent --port 8771
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, Optional

from aiohttp import web

from astral_client import __version__
from astral_client.audit_log import AuditLogger
from .tools import TOOL_REGISTRY, set_context

if TYPE_CHECKING:
    from astral_client.deployment import EffectiveDeploymentProfile

logger = logging.getLogger("win_agent")

AGENT_ID = "windows-tools-1"
AGENT_NAME = "Windows Tools (code & system)"
AGENT_DESC = ("Windows tools that run on the user's PC: read/write/edit files and "
              "run commands inside an approved workspace, plus system info, clipboard, "
              "notifications, and open. Every action is permission-gated, PHI-gated "
              "(fail-closed), and audited.")


def _bypass_enabled() -> bool:
    return os.getenv("ASTRAL_DANGEROUS_BYPASS", "0") in ("1", "true", "yes", "on")


def _advertised_tools() -> Dict[str, dict]:
    """The tool registry, minus the dangerous bypass when it isn't enabled.

    ``run_shell`` (full shell) is only advertised — and thus only routable by the
    orchestrator — when the local ``ASTRAL_DANGEROUS_BYPASS`` flag is set. The
    tool also re-checks the flag at call time (defense-in-depth), so a stale
    card can never grant shell access the user hasn't opted into.
    """
    if _bypass_enabled():
        return TOOL_REGISTRY
    return {k: v for k, v in TOOL_REGISTRY.items() if k != "run_shell"}


def build_card(
    deployment_profile: Optional["EffectiveDeploymentProfile"] = None,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "host": "windows-client",
        "platform": "windows",
        "dangerous_bypass": _bypass_enabled(),
    }
    if deployment_profile is not None:
        metadata.update(
            {
                "deployment_profile_sha256": deployment_profile.digest,
                "deployment_release_id": deployment_profile.profile.release_id,
                "deployment_endpoint_sha256": hashlib.sha256(
                    deployment_profile.profile.websocket_endpoint.encode("utf-8")
                ).hexdigest(),
            }
        )
    return {
        "name": AGENT_NAME,
        "description": AGENT_DESC,
        "agent_id": AGENT_ID,
        "version": __version__,
        "skills": [{
            "id": name, "name": name, "description": info["description"],
            "input_schema": info.get("input_schema", {"type": "object", "properties": {}}),
            "output_schema": None, "tags": ["windows", "desktop"],
            "scope": info.get("scope", "tools:system"), "metadata": {},
        } for name, info in _advertised_tools().items()],
        "metadata": metadata,
    }


def _register_message(
    deployment_profile: Optional["EffectiveDeploymentProfile"] = None,
) -> str:
    api_key = (
        deployment_profile.managed_agent_api_key
        if deployment_profile is not None
        else (os.getenv("AGENT_API_KEY") or None)
    )
    return json.dumps({
        "type": "register_agent",
        "agent_card": build_card(deployment_profile),
        "api_key": api_key,
    })


def _actor_from_req(req: Dict[str, Any]) -> str:
    """Best-effort actor identity for the audit trail.

    The MCPRequest carries ``request_id`` (correlation) and an optional ``meta``
    map the orchestrator may forward (user_id / sub). Falls back to the local
    USERNAME so every action is attributable even when no user is forwarded.
    """
    meta = req.get("meta") or {}
    return (meta.get("user_id") or meta.get("sub")
            or os.getenv("USERNAME") or "unknown")


# One audit logger per process; the actor is refined per-dispatch via context.
_AUDIT = AuditLogger(actor=os.getenv("USERNAME") or "unknown")


def dispatch(req: Dict[str, Any]) -> Dict[str, Any]:
    """Process one MCPRequest dict -> MCPResponse dict (mirrors the backend MCPServer)."""
    rid = req.get("request_id", "")
    method = req.get("method", "")
    set_context(actor=_actor_from_req(req), correlation_id=str(rid), audit=_AUDIT)
    tools = _advertised_tools()

    if method == "tools/list":
        return {"type": "mcp_response", "request_id": rid,
                "result_type": "complete",
                "responder_info": {"name": AGENT_ID, "version": "1.0.0"},
                "result": {"tools": [
            {"name": n, "description": i["description"],
             "input_schema": i.get("input_schema", {"type": "object", "properties": {}})}
            for n, i in tools.items()]}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        info = tools.get(name)
        if not info:
            # run_shell with bypass off lands here — audit the refused attempt.
            if name == "run_shell":
                _AUDIT.record(tool="run_shell", args=args, outcome="refused",
                              correlation_id=str(rid), event_class="dangerous_bypass",
                              detail="bypass flag not set (call rejected)")
            return {"type": "mcp_response", "request_id": rid,
                    "result_type": "complete",
                    "responder_info": {"name": AGENT_ID, "version": "1.0.0"},
                    "error": {"code": -32601, "message": f"Unknown tool: {name}", "retryable": False}}
        try:
            fn = info["function"]
            sig = inspect.signature(fn)
            if not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                args = {k: v for k, v in args.items() if k in sig.parameters}
            result = fn(**args)
            comps = result.get("_ui_components") if isinstance(result, dict) else None
            data = result.get("_data") if isinstance(result, dict) else result
            return {"type": "mcp_response", "request_id": rid,
                    "result_type": "complete",
                    "responder_info": {"name": AGENT_ID, "version": "1.0.0"},
                    "result": data, "ui_components": comps}
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s failed", name)
            return {"type": "mcp_response", "request_id": rid,
                    "result_type": "complete",
                    "responder_info": {"name": AGENT_ID, "version": "1.0.0"},
                    "error": {"code": -32603, "message": str(exc), "retryable": True}}

    return {"type": "mcp_response", "request_id": rid,
            "result_type": "complete",
            "responder_info": {"name": AGENT_ID, "version": "1.0.0"},
            "error": {"code": -32601, "message": f"Unknown method: {method}", "retryable": False}}


async def _card(request):
    return web.json_response(build_card(request.app.get("deployment_profile")))


async def _health(request):
    return web.Response(text="ok")


async def _agent_ws(request):
    ws = web.WebSocketResponse(max_msg_size=50 * 1024 * 1024)
    await ws.prepare(request)
    await ws.send_str(_register_message(request.app.get("deployment_profile")))
    logger.info("orchestrator connected; registered %d Windows tools", len(TOOL_REGISTRY))
    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            try:
                req = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            if isinstance(req, dict) and req.get("type") == "mcp_request":
                await ws.send_str(json.dumps(dispatch(req)))
    return ws


def make_app(
    deployment_profile: Optional["EffectiveDeploymentProfile"] = None,
) -> web.Application:
    app = web.Application()
    app["deployment_profile"] = deployment_profile
    app.add_routes([
        web.get("/.well-known/agent-card.json", _card),
        web.get("/health", _health),
        web.get("/agent", _agent_ws),
    ])
    return app


def start_agent_thread(
    host: str = "0.0.0.0",
    port: int = 8771,
    *,
    deployment_profile: Optional["EffectiveDeploymentProfile"] = None,
):
    """Run the agent server in a daemon thread (so the desktop GUI can host it
    in-process). Returns the thread, or None on failure."""
    import asyncio
    import threading

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(make_app(deployment_profile))
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, host, port).start())
        logger.info("Windows tools agent listening on %s:%d", host, port)
        loop.run_forever()

    try:
        t = threading.Thread(target=_run, name="win-agent", daemon=True)
        t.start()
        return t
    except Exception:  # noqa: BLE001
        logger.exception("could not start the Windows tools agent")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="AstralDeep Windows tools agent")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.getenv("WIN_AGENT_PORT", "8771")))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("Windows tools agent on %s:%d (tools: %s)",
                args.host, args.port, ", ".join(TOOL_REGISTRY))
    web.run_app(make_app(), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
