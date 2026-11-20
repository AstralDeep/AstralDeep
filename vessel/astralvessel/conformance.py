# Black-box acceptance checks for a running node over its public HTTP surface: health, descriptor, A2A with a caller
# ticket, and pairing. operate.py runs them as `astralvessel conformance`, and process mode adds the isolation suite
# that `check` runs; each check prints PASS or FAIL.

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from . import caller as callers
from . import node
from .deployment import Deployment
from .probes import Report


@dataclass
class Context:
    deployment: Deployment
    caller: callers.Caller
    offer: Callable[[], dict]


MESSAGE = {"messageId": "c1", "role": "ROLE_USER", "parts": [{"text": "conformance check"}]}
JWT_SHAPED = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJjb25mb3JtYW5jZSJ9.c2ln"

Check = tuple[str, Callable[[Context], str | None]]


def _get(ctx: Context, path: str, **kw) -> httpx.Response:
    return node._call(ctx.deployment.node, "GET", path, **kw)


def ready(ctx: Context) -> str | None:
    state = node.health(ctx.deployment.node)
    return None if state == "ok" else state


def descriptor(ctx: Context) -> str | None:
    return node.descriptor_problem(node.fetch_descriptor(ctx.deployment.node))


def node_key_published(ctx: Context) -> str | None:
    response = _get(ctx, "/.well-known/astral-node/jwks.json")
    keys = response.json().get("keys", []) if response.status_code == 200 else []
    return None if keys and keys[0].get("crv") == "Ed25519" else "the node publishes no Ed25519 key"


def agent_card(ctx: Context) -> str | None:
    response = _get(ctx, "/a2a/.well-known/agent-card.json")
    if response.status_code != 200:
        return f"the agent card answered HTTP {response.status_code}"
    card = response.json()
    interfaces = card.get("supportedInterfaces") or []
    ok = any(i.get("protocolBinding") == "JSONRPC" and i.get("protocolVersion") == "1.0" for i in interfaces)
    return None if ok and (card.get("capabilities") or {}).get("streaming") else "the card lacks JSON-RPC 1.0 streaming"


def a2a_needs_a_ticket(ctx: Context) -> str | None:
    status = node.a2a_call(ctx.deployment.node, None, "SendMessage", MESSAGE).status_code
    return None if status == 401 else f"an anonymous call answered HTTP {status}"


def a2a_refuses_a_bearer_token(ctx: Context) -> str | None:
    status = node.a2a_call(ctx.deployment.node, None, "SendMessage", MESSAGE,
                           headers={"Authorization": f"Bearer {JWT_SHAPED}"}).status_code
    return None if status == 401 else f"a JWT bearer token answered HTTP {status}"


def a2a_refuses_a_forged_proof(ctx: Context) -> str | None:
    forger = callers.Caller(ctx.caller.ticket_text, callers.KEY_TEXT + callers.b64(os.urandom(32)))
    status = node.a2a_call(ctx.deployment.node, forger, "SendMessage", MESSAGE).status_code
    return None if status == 401 else f"a proof from another key answered HTTP {status}"


def a2a_answers_the_caller(ctx: Context) -> str | None:
    response = node.a2a_call(ctx.deployment.node, ctx.caller, "SendMessage", MESSAGE)
    task = (response.json().get("result") or {}).get("task") if response.status_code == 200 else None
    state = (task or {}).get("status", {}).get("state", "")
    return None if state.endswith("COMPLETED") else f"the caller's message ended as {state or response.status_code}"


def a2a_refuses_unknown_methods(ctx: Context) -> str | None:
    response = node.a2a_call(ctx.deployment.node, ctx.caller, "NoSuchMethod", MESSAGE)
    return None if response.status_code == 200 and "error" in response.json() else "an unknown method was accepted"


def pairing_needs_the_secret(ctx: Context) -> str | None:
    response = node._call(ctx.deployment.node, "POST", "/api/node/mesh/offer", body={})
    status = response.status_code
    return None if status in (401, 403) else f"a pairing call with no secret answered HTTP {status}"


def old_pairing_is_gone(ctx: Context) -> str | None:
    response = node._call(ctx.deployment.node, "POST", "/api/node/pairing", body={})
    status = response.status_code
    return None if status >= 400 else f"the owner-token pairing route answered HTTP {status}"


def pairing_offers_a_mesh_code(ctx: Context) -> str | None:
    reply = ctx.offer()
    if reply.get("status") != 200:
        return node.pairing_refusal(reply.get("status", 0), reply)
    if reply.get("state") == "enrolled":
        return None if reply.get("mesh_id") and reply.get("member_id") else "the node is enrolled with no mesh id"
    if reply.get("state") != "offer":
        return f"no offer was made (state {reply.get('state')})"
    try:
        json.loads(base64.urlsafe_b64decode(reply["payload"] + "=" * (-len(reply["payload"]) % 4)))
    except (KeyError, ValueError):
        return "the payload is not an encoded document"
    return None if reply.get("code") else "the offer has no code"


CHECKS: tuple[Check, ...] = (
    ("the node reports ready with its database up", ready),
    ("the v2 descriptor names the node's key and capabilities", descriptor),
    ("the node publishes its Ed25519 key", node_key_published),
    ("the A2A card advertises JSON-RPC 1.0 with streaming", agent_card),
    ("A2A refuses a call with no ticket", a2a_needs_a_ticket),
    ("A2A refuses a JWT bearer token", a2a_refuses_a_bearer_token),
    ("A2A refuses a proof signed by another key", a2a_refuses_a_forged_proof),
    ("A2A answers the caller", a2a_answers_the_caller),
    ("A2A rejects an unknown method", a2a_refuses_unknown_methods),
    ("pairing refuses a caller without the node's secret", pairing_needs_the_secret),
    ("the owner-token pairing route is gone", old_pairing_is_gone),
    ("pairing offers a code and payload, or names the mesh joined", pairing_offers_a_mesh_code),
)


def run(deployment: Deployment, caller: callers.Caller, offer: Callable[[], dict]) -> list[str]:
    ctx = Context(deployment, caller, offer)
    failed: list[str] = []
    for name, check in CHECKS:
        try:
            problem = check(ctx)
        except (node.NodeError, ValueError, KeyError) as exc:
            problem = str(exc) or type(exc).__name__
        print(f"{'PASS' if problem is None else 'FAIL'}  {name}" + (f" ({problem})" if problem else ""))
        if problem is not None:
            failed.append(name)
    return failed


# Each container probe and where its claim is checked in process mode: a named astral_exec case, a named test file, or
# a weaker guarantee the docs state. A new container probe without an entry here fails the mapping test.
CASE, TESTED, WEAKER = "case", "tested", "weaker"
PROBE_MAP: dict[str, tuple[str, str]] = {
    "has no route to the internet": (CASE, "net.public_address_denied"),
    "cannot resolve or reach the egress": (CASE, "net.loopback_ports_denied"),
    "cannot reach the model host directly": (CASE, "net.public_address_denied"),
    "cannot reach the node API": (CASE, "net.loopback_ports_denied"),
    "cannot reach the node's mailbox": (CASE, "net.loopback_ports_denied"),
    "cannot reach {other}": (CASE, "proxy.other_sandbox_ports_unreachable"),
    "holds no task or conversation store": (CASE, "fs.read_node_data_dir_denied"),
    "environment holds no node secret or model key": (CASE, "env.cleared"),
    "holds its own token, not the master": (CASE, "env.secret_not_in_process_listing"),
    "root filesystem is read-only": (CASE, "fs.write_outside_workspace_denied"),
    "scratch space is noexec": (CASE, "fs.workspace_exec_script_denied"),
    "{target} is mounted read-only": (CASE, "fs.write_read_root_denied"),
    "a background process does not outlive its call": (CASE, "proc.no_strays"),
    "a turn's workspace is gone once it ends": (TESTED, "test_process_sandbox.py"),
    "a call can start a background process (for the next probe)": (CASE, "proc.limit_enforced"),
    "a call cannot read another call's scratch": (CASE, "fs.read_outside_policy_denied"),
    "a call cannot list processes": (CASE, "signal.outside_process_denied"),
    "a call cannot open TCP connections": (CASE, "net.loopback_ports_denied"),
    "a call cannot create a socket file": (CASE, "net.unix_socket_denied"),
    "a call cannot use shared memory": (
        WEAKER, "Linux blocks System V and POSIX IPC with seccomp. On macOS the profile denies everything it does "
                "not allow, and no separate case names it."),
    "a call cannot open UDP sockets": (CASE, "net.udp_denied"),
    "glob cannot climb out of the agent's data": (TESTED, "test_deploy.py"),
    "calls in one turn share its workspace": (CASE, "fs.write_workspace_allowed"),
    "a call cannot read another turn's workspace": (CASE, "fs.read_outside_policy_denied"),
    "a file a call writes to its outbox reaches the caller": (TESTED, "test_files.py"),
    "a file type the agent does not allow is not attached": (TESTED, "test_files.py"),
    "the outbox refuses other types, big files and links": (TESTED, "test_files.py"),
    "an outbox swapped for a link exposes nothing": (TESTED, "test_files.py"),
    "a turn over its disk limit is stopped and emptied": (TESTED, "test_process_sandbox.py"),
    "the sandbox refuses a malformed workspace key": (TESTED, "test_units.py"),
    "the executor has no direct route to the internet": (WEAKER, "The executor is a host process on loopback. It is "
                                                                  "not confined, and it holds no model key."),
    "the executor refuses a caller without its token": (TESTED, "executor: refuses a caller without its token"),
    "the executor lists what its agents may run": (TESTED, "executor: lists what its agents may run"),
    "the executor's keys are readable only by it": (TESTED, "executor: keeps its keys private"),
    "the executor refuses a forged ticket with a dotted code and runs nothing": (
        TESTED, "executor: refuses a forged ticket"),
    "the executor refuses a tool the agent does not offer or whose arguments differ from the action": (
        TESTED, "executor: refuses an unknown agent"),
    "the executor refuses an agent it does not serve": (TESTED, "executor: refuses an unknown agent"),
    "the executor's operator token cannot run a call": (TESTED, "executor: operator token cannot run a call"),
    "the executor refuses one agent's token for another agent": (TESTED, "test_executor.py"),
    "the egress has no way to the node API": (WEAKER, "The egress is a host process on loopback and is not confined."),
    "the egress proxy refuses the cloud metadata address": (TESTED, "test_egress.py"),
    "the egress proxy refuses private addresses": (TESTED, "test_egress.py"),
    "the egress proxy refuses Tailscale addresses": (TESTED, "test_egress.py"),
    "the egress proxy refuses odd ports": (TESTED, "test_egress.py"),
    "the egress proxy allows a public site on 443": (TESTED, "test_egress.py"),
    "the model passthrough refuses a caller without the token": (TESTED, "test_egress.py"),
    "the model passthrough accepts its own token": (TESTED, "test_egress.py"),
}


def run_process(runtime) -> Report:
    # Process mode: the backend suite shipped with the pack's astral_exec, each agent's own policy, the executor over
    # loopback and one line for every weaker guarantee. Every line is printed, and a FAIL ends `check` with status 1.
    report = Report()
    python, code = str(runtime.bundle.python), runtime.layout.code
    suite = [python, "-I", "-B", str(code / "astral_exec" / "run.py"), "conformance", "--json"]
    for line in _lines(suite):
        label = "isolation"
        if line.get("result") == "REFUSED":
            report.add(label, {"name": line["case"], "skip": line.get("detail") or "no backend"})
        else:
            report.add(label, {"name": line["case"], "ok": line.get("result") == "PASS"})
    for agent_id in runtime.deployment.agents:
        probe = [python, "-I", "-B", str(code / "run.py"), "probe", "--env-file",
                 str(runtime.env_file(f"sandbox-{agent_id}"))]
        for line in _lines(probe):
            report.add(f"agent {agent_id}", line)
    for line in executor_lines(runtime):
        report.add("executor", line)
    for name, (kind, detail) in PROBE_MAP.items():
        if kind == WEAKER and "{" not in name:
            print(f"WEAK  {name}: {detail}")
    return report


def _lines(command: list[str]) -> list[dict]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return [{"case": "the check ran", "result": "FAIL", "name": "the check ran", "ok": False, "detail": str(exc)}]
    found = []
    for raw in done.stdout.splitlines():
        try:
            found.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    if not found:
        found.append({"case": "the check ran", "result": "FAIL", "name": "the check ran", "ok": False})
    return found


def executor_lines(runtime) -> list[dict]:
    # The same claims the container probes make about the executor, asked over its loopback port.
    deployment = runtime.deployment
    if not deployment.agents:
        return []
    gen = runtime.layout.generated
    try:
        operator = next(line.partition("=")[2] for line in (gen / "executor.env").read_text().splitlines()
                        if line.startswith("VESSEL_EXECUTOR_TOKEN="))
        agent_tokens = json.loads((gen / "executor-agents.json").read_text())
    except (OSError, StopIteration, ValueError):
        return [{"name": "the executor's token files are readable", "ok": False}]
    base = f"http://127.0.0.1:{runtime.ports['executor']}"

    def call(path, body=None, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = httpx.request("POST" if body is not None else "GET", base + path, json=body, headers=headers,
                                     timeout=10)
            return response.status_code, response.json()
        except (httpx.HTTPError, ValueError):
            return 0, {}

    lines = [{"name": "refuses a caller without its token",
              "ok": call("/tools")[0] == 401 and call("/tools", token="x")[0] == 401}]
    status, listing = call("/tools", token=operator)
    ids = list(deployment.agents)
    listed = set(listing.get("agents", {}))
    lines.append({"name": "lists what its agents may run", "ok": status == 200 and listed == set(ids)})
    agent = ids[0]
    token = agent_tokens.get(agent, "")
    tools = (listing.get("agents", {}).get(agent) or {}).get("tools") or [{}]
    tool = tools[0]
    arguments = "{}"
    action = {"v": 1, "mesh": "m", "device": "d", "turn": "t", "agent": "kernel", "tool": tool.get("name"),
              "tool_def": tool.get("definition"),
              "args_digest": "sha256:" + hashlib.sha256(arguments.encode()).hexdigest(), "influence": []}
    forged = {"agent": agent, "tool": tool.get("name"), "arguments_json": arguments,
              "action_json": json.dumps(action), "ticket_json": json.dumps({"v": 1, "capability": "AAAA"})}
    status, body = call("/run", forged, token)
    lines.append({"name": "refuses a forged ticket", "ok": status == 403 and body.get("error") is True
                  and "." in body.get("code", "") and "files" not in body})
    status, _ = call("/run", {**forged, "agent": "no-such-agent"}, token)
    lines.append({"name": "refuses an unknown agent", "ok": status in (403, 404)})
    status, body = call("/run", forged, operator)
    lines.append({"name": "operator token cannot run a call",
                  "ok": status == 403 and body.get("code") == "executor.token_scope"})
    keys = runtime.layout.executor_data / "keys"
    modes = [stat.S_IMODE(p.stat().st_mode) for p in keys.iterdir()] if keys.is_dir() else []
    lines.append({"name": "keeps its keys private", "ok": all(mode == 0o600 for mode in modes)})
    return lines
