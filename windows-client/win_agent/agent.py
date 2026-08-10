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
import hmac
import inspect
import json
import logging
import os
import sys
import time
from collections import OrderedDict
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


# --------------------------------------------------------------------------- #
# Inbound authentication.
#
# This process listens on a TCP port and its tools read/write files and run
# commands on the user's PC. Until now anything that could reach the port could
# drive them: `_agent_ws` called `ws.prepare()` as its first statement and then
# pushed the register frame — which CONTAINS the shared key — to whoever
# connected, before reading a byte.
#
# The orchestrator now presents the same shared `AGENT_API_KEY` it already holds
# as `X-Astral-Agent-Key` on its outbound connections (see
# backend/orchestrator/agent_peer_auth.py), and every request here must carry a
# matching value. There is deliberately NO development carve-out: the backend's
# ASTRAL_ENV allowance is nested inside "no key configured", and our answer to
# that case is strictly safer — refuse to listen at all (`make_app`).
# --------------------------------------------------------------------------- #

#: Request header carrying the shared agent key. Not ``Authorization``: that is
#: the per-call delegation token on the A2A path, a different credential.
AGENT_KEY_HEADER = "X-Astral-Agent-Key"

#: Minimum key length, mirroring the orchestrator's own boot gate
#: (backend/orchestrator/session_store.py refuses a short or placeholder key).
MIN_KEY_LENGTH = 16

#: Shipped placeholders that must never function as a credential (same list the
#: orchestrator's boot gate refuses).
_PLACEHOLDER_KEYS = frozenset(
    {"change-me", "changeme", "dev-audit-hmac-secret-change-me-in-prod"}
)

#: At most one refusal log line per peer per this many seconds, so a port
#: scanner cannot fill the user's log.
_REFUSAL_LOG_INTERVAL_S = 10.0

#: Bound on the rate-limiter's peer table. Without it, one entry per distinct
#: source address accumulates for the life of a long-running desktop process
#: (a local process can source all of 127.0.0.0/8). Oldest entries are evicted;
#: the worst case is an extra log line from a peer that aged out, never a
#: missed refusal — the gate itself does not consult this table.
_REFUSAL_LOG_MAX_PEERS = 512
_last_refusal_log: "OrderedDict[str, float]" = OrderedDict()

#: How long start_agent_thread waits for the worker thread to actually bind.
BIND_TIMEOUT_S = 5.0


def configured_key(
    deployment_profile: Optional["EffectiveDeploymentProfile"] = None,
) -> Optional[str]:
    """The shared agent key: the managed profile credential, else the env var.

    One resolver for BOTH the outbound register frame and the inbound gate, so
    the two can never disagree about what the key is.
    """
    key = (
        deployment_profile.managed_agent_api_key
        if deployment_profile is not None
        else (os.getenv("AGENT_API_KEY") or None)
    )
    if not isinstance(key, str):
        return None
    key = key.strip()
    return key or None


def key_rejection_reason(key: Optional[str]) -> Optional[str]:
    """Why ``key`` is unusable as a credential, or ``None`` when it is fine.

    A weak key is refused rather than accepted with a warning: a 4-character
    value from the first-run dialog would otherwise install a trivially
    guessable gate in front of file-write and command-exec.
    """
    if not key:
        return "AGENT_API_KEY is not configured"
    if not key.isascii():
        return "AGENT_API_KEY must be ASCII"
    if key.lower() in _PLACEHOLDER_KEYS:
        return "AGENT_API_KEY is a shipped placeholder"
    if len(key) < MIN_KEY_LENGTH:
        return f"AGENT_API_KEY is shorter than {MIN_KEY_LENGTH} characters"
    return None


def _authorized(request) -> bool:
    """True iff the request carries exactly one matching key header."""
    expected = request.app.get("inbound_key")
    if not expected:
        # Unreachable in practice: make_app refuses to build without a key.
        return False
    # `getall`, not `get`: duplicate headers survive to the handler and `.get()`
    # returns only the FIRST of them (and `getone()` does not raise on
    # duplicates either), so a caller could pair a wrong value with the real one
    # and have the gate read whichever it prefers. Exactly one value, or no.
    values = request.headers.getall(AGENT_KEY_HEADER, [])
    if len(values) != 1:
        return False
    presented = values[0]
    # compare_digest raises TypeError on non-ASCII str, and this value is fully
    # attacker-controlled — inside a handler that is a 500, not a refusal.
    if not presented or not presented.isascii():
        return False
    return hmac.compare_digest(presented.encode("ascii"), expected.encode("ascii"))


def _log_refusal(request, route: str) -> None:
    """Rate-limited refusal log. NEVER records the configured key, the presented
    value, or any length/prefix/hash of either — the key can be low-entropy
    (it may come from the first-run dialog), so even a digest is brute-forceable
    offline."""
    peer = str(getattr(request, "remote", "") or "unknown")
    now = time.monotonic()
    last = _last_refusal_log.get(peer)
    if last is not None and (now - last) < _REFUSAL_LOG_INTERVAL_S:
        return
    _last_refusal_log[peer] = now
    _last_refusal_log.move_to_end(peer)
    while len(_last_refusal_log) > _REFUSAL_LOG_MAX_PEERS:
        _last_refusal_log.popitem(last=False)
    logger.warning("refused unauthenticated %s request from %s", route, peer)


def _print_console(text: str) -> None:
    """Write to stderr when a console exists.

    In a windowed frozen build ``sys.stderr`` is ``None`` (and can be a closed
    handle), so an unguarded ``print(..., file=sys.stderr)`` raises. Everything
    important is already on the logger; this is only the courtesy copy for
    someone running the module from a terminal.
    """
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return
    try:
        print(text, file=stream)
    except (AttributeError, ValueError, OSError):  # closed/detached handle
        pass


def _log_bind(host: str, port: int) -> None:
    """Announce the bind. A non-loopback bind says plainly that the open port is
    guarded, so nobody reads it as unauthenticated. ``0.0.0.0`` remains the
    default because a containerized orchestrator reaches the desktop at
    ``host.docker.internal``, which resolves to the bridge address and can never
    reach a loopback bind."""
    if host in ("127.0.0.1", "::1", "localhost"):
        logger.info("Windows tools agent listening on %s:%d", host, port)
        return
    logger.warning(
        "Windows tools agent listening on %s:%d — reachable from the local "
        "network; every request requires the %s header",
        host, port, AGENT_KEY_HEADER,
    )


def _unauthorized() -> web.HTTPUnauthorized:
    """A 401 that says nothing about the key or the configuration state — the
    body is identical whether the key was absent, wrong, duplicated or
    non-ASCII. The scheme is deliberately not ``Bearer``: we do not read
    ``Authorization`` at all, and advertising Bearer would invite callers to put
    the credential where nothing reads it."""
    return web.HTTPUnauthorized(
        text=json.dumps({"error": "agent_auth_required"}),
        content_type="application/json",
        headers={"WWW-Authenticate": 'AstralAgentKey realm="win-agent"'},
    )


def _register_message(
    deployment_profile: Optional["EffectiveDeploymentProfile"] = None,
) -> str:
    return json.dumps({
        "type": "register_agent",
        "agent_card": build_card(deployment_profile),
        "api_key": configured_key(deployment_profile),
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
    # Gated too: the card publishes every advertised tool with its full input
    # schema, the dangerous_bypass flag state, and the deployment digests.
    # Gating /agent alone would leave that disclosure open to any LAN scanner.
    if not _authorized(request):
        _log_refusal(request, "card")
        raise _unauthorized()
    return web.json_response(build_card(request.app.get("deployment_profile")))


async def _health(request):
    # Deliberately ungated and deliberately content-free: a liveness probe must
    # work without a credential, so this must never grow a disclosure surface.
    return web.Response(text="ok")


async def _agent_ws(request):
    # The check MUST precede ws.prepare(): the register frame sent immediately
    # after the upgrade carries the shared key, so a gate that ran after prepare
    # would hand the credential to the very caller it then rejected.
    if not _authorized(request):
        _log_refusal(request, "agent")
        raise _unauthorized()
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


class AgentKeyUnavailable(RuntimeError):
    """No usable ``AGENT_API_KEY`` — the listener must not exist.

    Refusing to serve is the fail-closed answer to a missing key: an
    unauthenticated file-write / command-exec listener should not be reachable
    at all, and this is the single choke point covering every caller
    (``python -m win_agent.agent``, the GUI, and the screenshot/verify
    harnesses, none of which can conjure a key).
    """


def make_app(
    deployment_profile: Optional["EffectiveDeploymentProfile"] = None,
) -> web.Application:
    key = configured_key(deployment_profile)
    reason = key_rejection_reason(key)
    if reason:
        raise AgentKeyUnavailable(
            f"{reason} — refusing to serve the Windows tools agent"
        )
    app = web.Application()
    app["deployment_profile"] = deployment_profile
    app["inbound_key"] = key
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
    in-process). Returns the thread, or None on failure — including the
    deliberate refusal when no usable key is configured."""
    import asyncio
    import threading

    try:
        app = make_app(deployment_profile)
    except AgentKeyUnavailable as exc:
        # Build the app on THIS thread so the refusal is synchronous and the
        # caller can react (the GUI turns off the feature and tells the user).
        logger.error("Windows tools agent not started: %s", exc)
        return None

    # The bind happens on the worker thread, so a failure there (port in use,
    # permission denied) would surface AFTER this function already returned a
    # live thread — leaving the caller convinced a listener exists. Wait for the
    # bind to actually succeed or fail before reporting.
    bound = threading.Event()
    outcome = {}

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        outcome["loop"] = loop
        runner = web.AppRunner(app)
        try:
            loop.run_until_complete(runner.setup())
            loop.run_until_complete(web.TCPSite(runner, host, port).start())
        except Exception as exc:  # noqa: BLE001 — reported to the caller below
            outcome["error"] = exc
            bound.set()
            return
        _log_bind(host, port)
        bound.set()
        try:
            loop.run_forever()
        finally:
            # Reachable when a caller stops the loop (tests do; the desktop app
            # runs it for the process lifetime). Release the port rather than
            # leaving a bound socket behind.
            try:
                loop.run_until_complete(runner.cleanup())
            except Exception:  # noqa: BLE001
                pass
            loop.close()

    try:
        t = threading.Thread(target=_run, name="win-agent", daemon=True)
        t.start()
    except Exception:  # noqa: BLE001
        logger.exception("could not start the Windows tools agent")
        return None
    if not bound.wait(BIND_TIMEOUT_S):
        logger.error("Windows tools agent did not bind %s:%d within %.0fs",
                     host, port, BIND_TIMEOUT_S)
        return None
    if "error" in outcome:
        logger.error("Windows tools agent could not bind %s:%d: %s",
                     host, port, outcome["error"])
        return None
    # Expose the loop so a caller can shut the listener down deterministically
    # (tests need the port released; the app runs it for the process lifetime).
    t._astral_loop = outcome.get("loop")
    return t


def main() -> int:
    ap = argparse.ArgumentParser(description="AstralDeep Windows tools agent")
    ap.add_argument("--host", default=os.getenv("ASTRAL_AGENT_BIND", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.getenv("WIN_AGENT_PORT", "8771")))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        app = make_app()
    except AgentKeyUnavailable as exc:
        # A windowed PyInstaller build has NO console: sys.stderr is None, and
        # print(file=None) raises AttributeError — turning a clean exit 78 into
        # a crash on the one path that is supposed to fail politely. Log first
        # (that always works), then write to the console only if there is one.
        logger.error("%s", exc)
        _print_console(f"AstralDeep Windows tools agent: {exc}")
        return 78
    logger.info("Windows tools agent on %s:%d (tools: %s)",
                args.host, args.port, ", ".join(TOOL_REGISTRY))
    # The standalone path binds through run_app, not start_agent_thread, so it
    # must announce the bind itself or an operator running `python -m
    # win_agent.agent` never sees the non-loopback warning.
    _log_bind(args.host, args.port)
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
