# The executor in front of the sandboxes: it verifies a ticket and the action it covers before a sandbox runs a tool.
# The node's tool adapter calls POST /run on it; it speaks to each sandbox over HTTP with that sandbox's own token.

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .deployment import Deployment, load_deployment, sandbox_token
from .manifest import BUILTIN_SCOPES
from .mesh import Refused, Verified
from .sandbox import RemoteRunner
from .tools import BUILTIN_SCHEMAS, WORKSPACE_KEY, clip

READ_SCOPES = frozenset({"tools:read", "tools:search"})
SANDBOX_PORT = 8710
SEEN_LIMIT = 65536
RETENTION_SECONDS = 3600


class Verifier(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def verify(self, ticket_json: str, action_json: str, effect: str) -> Verified: ...

    async def state(self) -> str: ...

    async def mesh_id(self) -> str | None: ...

    async def pairing_offer(self) -> dict[str, Any]: ...


def definition_hash(agent: str, name: str, description: str, input_schema: Any, annotations: Any) -> str:
    # The kernel hashes a published tool as name, description, input schema and annotations, so putting the agent id in
    # the annotations makes this the hash a device's ticket carries for a tool published with that annotation.
    document = {"annotations": {**annotations, "agent": agent}, "description": description,
                "input_schema": input_schema, "name": name}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def args_digest(arguments_json: str) -> str:
    return "sha256:" + hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()


def executor_token(master: str) -> str:
    # The operator's token: it lists the catalog and asks for a pairing offer, and it can run nothing.
    return hmac.new(master.encode(), b"executor:operator", hashlib.sha256).hexdigest()


def agent_token(master: str, agent_id: str) -> str:
    # The token that may run and end calls for one agent. It is derived from the master secret, so no two tokens match.
    return hmac.new(master.encode(), f"executor:agent:{agent_id}".encode(), hashlib.sha256).hexdigest()


def catalog(deployment: Deployment) -> dict[str, dict[str, Any]]:
    # What each agent offers: the tools the manifest lists, each with its schema, scope and definition hash.
    found: dict[str, dict[str, Any]] = {}
    for agent_id, agent in deployment.agents.items():
        tools: dict[str, dict[str, Any]] = {}
        for name in agent.package.manifest.tools.builtin:
            schema = BUILTIN_SCHEMAS[name]
            tools[name] = {"description": schema["description"], "input_schema": schema["parameters"],
                           "scope": BUILTIN_SCOPES[name]}
        for tool in agent.package.declared:
            tools[tool.name] = {"description": tool.description, "input_schema": tool.parameters, "scope": tool.scope}
        for name, tool in tools.items():
            tool["name"] = name
            tool["definition"] = definition_hash(agent_id, name, tool["description"], tool["input_schema"], {})
        found[agent_id] = {"description": agent.package.manifest.agent.description,
                           "scope": agent.package.strongest_scope(), "tools": list(tools.values())}
    return found


def workspace_for(mesh: str, turn: str) -> str:
    # A turn's working folder comes from the mesh and turn the ticket proves, never from the request.
    return hashlib.sha256(f"{mesh}\0{turn}".encode()).hexdigest()[:32]


def effect_kind(scope: str) -> str:
    return "outward_read" if scope in READ_SCOPES else "outside_effect"


def _refusal(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": True, "code": code, "output": f"refused: {code}"}, status_code=status)


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": True, "output": "unauthorized"}, status_code=401)


class ReplayMemoryFull(Exception):
    pass


class Replays:
    # A ticket pays for one effect. The kernel verifies each ticket afresh, so the executor keeps the ids it has run
    # in a file that survives a restart. An id is kept for the retention window, which must cover a ticket's life.
    def __init__(self, path: Path, retention_s: int = RETENTION_SECONDS, limit: int = SEEN_LIMIT,
                 clock: Callable[[], float] = time.time):
        self._retention, self._limit, self._clock = retention_s, limit, clock
        self._lock = threading.Lock()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS spent (id TEXT PRIMARY KEY, expires REAL NOT NULL)")
        path.chmod(0o600)

    def first_use(self, key: str) -> bool:
        with self._lock:
            now = self._clock()
            self._db.execute("DELETE FROM spent WHERE expires <= ?", (now,))
            if self._db.execute("SELECT COUNT(*) FROM spent").fetchone()[0] >= self._limit:
                raise ReplayMemoryFull
            return self._db.execute("INSERT OR IGNORE INTO spent VALUES (?, ?)",
                                    (key, now + self._retention)).rowcount == 1

    def close(self) -> None:
        self._db.close()


def executor_app(deployment: Deployment, verifier: Verifier, runners: dict[str, RemoteRunner], operator_token: str,
                 agent_tokens: dict[str, str], replays: Replays) -> Starlette:
    tools = catalog(deployment)

    def caller(request: Request) -> tuple[str, str | None] | None:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").encode()
        found: tuple[str, str | None] | None = None
        for role, agent_id, token in [("operator", None, operator_token),
                                      *(("agent", a, t) for a, t in agent_tokens.items())]:
            if hmac.compare_digest(supplied, token.encode()):
                found = (role, agent_id)
        return found

    async def health(_request: Request) -> JSONResponse:
        ready = {agent_id: ((await runner.health()) or {}).get("confined") is True
                 for agent_id, runner in runners.items()}
        return JSONResponse({"ok": True, "agents": ready, "mesh": await verifier.state()})

    async def listing(request: Request) -> JSONResponse:
        who = caller(request)
        if who is None:
            return _unauthorized()
        return JSONResponse({"agents": tools if who[1] is None else {who[1]: tools[who[1]]}})

    async def pairing(request: Request) -> JSONResponse:
        who = caller(request)
        if who is None:
            return _unauthorized()
        if who[1] is not None:
            return _refusal("executor.token_scope", 403)
        try:
            return JSONResponse(await verifier.pairing_offer())
        except Refused as exc:
            return _refusal(exc.code, 409)

    async def run(request: Request) -> JSONResponse:
        who = caller(request)
        if who is None:
            return _unauthorized()
        try:
            body = await request.json()
            agent_id, name = body["agent"], body["tool"]
            arguments_json, ticket_json, action_json = body["arguments_json"], body["ticket_json"], body["action_json"]
            workspace, owner = body.get("workspace"), body.get("owner") or ""
            if not all(isinstance(v, str) for v in (agent_id, name, arguments_json, ticket_json, action_json, owner)):
                raise ValueError
            arguments, action = json.loads(arguments_json), json.loads(action_json)
            if not isinstance(arguments, dict) or not isinstance(action, dict):
                raise ValueError
            if workspace is not None and not (isinstance(workspace, str) and WORKSPACE_KEY.fullmatch(workspace)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return _refusal("request.malformed", 400)
        if who[1] is None:
            return _refusal("executor.token_scope", 403)
        if who[1] != agent_id:
            return _refusal("executor.token_agent_mismatch", 403)
        entry = next((t for t in tools.get(agent_id, {}).get("tools", []) if t["name"] == name), None)
        if entry is None:
            return _refusal("executor.tool_unknown", 404)
        if action.get("tool") != name or action.get("args_digest") != args_digest(arguments_json):
            return _refusal("request.call_mismatch", 403)
        if action.get("tool_def") != entry["definition"]:
            return _refusal("gate.tool_def_changed", 403)
        try:
            verified = await verifier.verify(ticket_json, action_json, effect_kind(entry["scope"]))
        except Refused as exc:
            return _refusal(exc.code, 403)
        if verified.mesh != action.get("mesh") or verified.turn != action.get("turn") \
                or verified.device != action.get("device"):
            return _refusal("ticket.unauthorized", 403)
        if verified.mesh != await verifier.mesh_id():
            return _refusal("ticket.wrong_mesh", 403)
        bound_owner = deployment.owner.owner_id
        if bound_owner is None:
            return _refusal("executor.owner_unbound", 403)
        bound_workspace = workspace_for(verified.mesh, verified.turn)
        if verified.agents and verified.agents[-1] != action.get("agent"):
            return _refusal("ticket.unauthorized", 403)
        if owner not in ("", bound_owner) or workspace not in (None, bound_workspace):
            return _refusal("request.call_mismatch", 403)
        try:
            if not replays.first_use(verified.action_digest):
                return _refusal("ticket.replayed", 403)
        except ReplayMemoryFull:
            return _refusal("executor.replay_memory_full", 429)
        output, error = await runners[agent_id].run(name, arguments, bound_workspace, owner=bound_owner)
        limit = deployment.agents[agent_id].package.manifest.sandbox.tool_output_chars
        files = [f.wire() for f in runners[agent_id].take_files(bound_workspace)]
        return JSONResponse({"output": clip(output, limit)[0], "error": error, "files": files,
                             "workspace": bound_workspace,
                             "ticket": {"mesh": verified.mesh, "device": verified.device, "turn": verified.turn}})

    async def end(request: Request) -> JSONResponse:
        who = caller(request)
        if who is None:
            return _unauthorized()
        try:
            body = await request.json()
            agent_id, workspace = body["agent"], body["workspace"]
            if agent_id not in runners or not (isinstance(workspace, str) and WORKSPACE_KEY.fullmatch(workspace)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return _refusal("request.malformed", 400)
        if who[1] is None:
            return _refusal("executor.token_scope", 403)
        if who[1] != agent_id:
            return _refusal("executor.token_agent_mismatch", 403)
        await runners[agent_id].end_turn(workspace)
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        await verifier.start()
        try:
            yield
        finally:
            await verifier.stop()
            replays.close()
            await asyncio.gather(*(r.close() for r in runners.values()))

    return Starlette(routes=[Route("/health", health), Route("/tools", listing), Route("/pairing", pairing),
                             Route("/run", run, methods=["POST"]), Route("/end", end, methods=["POST"])],
                     lifespan=lifespan)


def build(deployment: Deployment | None = None) -> Starlette:
    from .mesh import KernelVerifier
    deployment = deployment or load_deployment(os.environ["VESSEL_DEPLOYMENT"],
                                               os.environ.get("VESSEL_PACKAGES_ROOT"))
    master = os.environ["VESSEL_SANDBOX_TOKEN"]
    template = os.environ.get("VESSEL_SANDBOX_URL_TEMPLATE", f"http://sandbox-{{id}}:{SANDBOX_PORT}")
    runners = {agent_id: RemoteRunner(template.format(id=agent_id), sandbox_token(master, agent_id),
                                      agent.package.manifest.sandbox.tool_timeout_seconds,
                                      list(agent.package.manifest.output.files))
               for agent_id, agent in deployment.agents.items()}
    node_url = os.environ.get("VESSEL_NODE_URL", "http://node:8001").rstrip("/")

    async def mailbox() -> str | None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                document = (await client.get(f"{node_url}/api/node/descriptor")).json()
            return (document.get("mailbox") or {}).get("address")
        except (httpx.HTTPError, ValueError):
            return None

    data = Path(os.environ.get("VESSEL_EXECUTOR_DIR", "/executor"))
    verifier = KernelVerifier(data, mailbox=mailbox)
    replays = Replays(data / "spent.db", int(os.environ.get("VESSEL_REPLAY_RETENTION_SECONDS", RETENTION_SECONDS)))
    return executor_app(deployment, verifier, runners, os.environ["VESSEL_EXECUTOR_TOKEN"],
                        {agent_id: agent_token(master, agent_id) for agent_id in deployment.agents}, replays)

