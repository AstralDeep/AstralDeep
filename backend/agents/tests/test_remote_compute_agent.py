"""Unit tests for the unified remote-compute agent class (feature 063).

Covers ``agents.remote_compute`` end to end without a real Plane runtime, SSH socket,
or event-loop-bound orchestrator: construction + dependency wiring into both verb
libraries, the unioned 18-verb registry, the agent card the orchestrator registers,
and ``MCPServer``/``handle_mcp_request`` routing into each risk tier.

Hermetic by construction — the Plane binding and ``CredentialManager`` are
replaced with doubles before the agent is built, the ECIES key is written to a
tmp path via ``AGENT_KEY_PATH`` (never the shared ``backend/data/agent_keys`` file),
and the transport seam uses ``FakeTransport``.
"""
from __future__ import annotations

import asyncio
import json
import runpy
import sys
from types import SimpleNamespace

import pytest

from agents.remote_compute import mcp_tools as unified
from agents.remote_compute import remote_compute_agent as agent_module
from agents.remote_compute.mcp_server import MCPServer
from agents.remote_compute.remote_compute_agent import RemoteComputeAgent
from agents.remote_control import mcp_tools as ctl
from agents.remote_observe import mcp_tools as obs
from orchestrator.remote_transport import FakeTransport, MachineTarget, set_transport
from shared.protocol import MCPRequest

USER = "user-1"


class FakeCredentialManager:
    instances: list = []

    def __init__(self, db=None, **kwargs):
        self.db = db
        self.kwargs = kwargs
        FakeCredentialManager.instances.append(self)


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch, tmp_path):
    """Restore both libraries' wired deps + the transport seam after every test.

    ``register_deps`` sets module GLOBALS shared with every other 063 suite, so a
    construction test would otherwise leak its doubles into whatever runs next.
    """
    saved = (obs._DB, obs._CREDMGR, ctl._DB, ctl._CREDMGR, ctl._BLOBS)
    FakeCredentialManager.instances = []
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "remote-compute-1.pem"))
    monkeypatch.setattr("orchestrator.credential_manager.CredentialManager",
                        FakeCredentialManager)
    yield
    obs._DB, obs._CREDMGR, ctl._DB, ctl._CREDMGR, ctl._BLOBS = saved
    set_transport(None)


def _plane():
    repositories = SimpleNamespace()
    return SimpleNamespace(repositories=repositories), repositories


def _agent(port: int = 0) -> RemoteComputeAgent:
    runtime, repositories = _plane()
    return RemoteComputeAgent(
        port=port,
        plane_runtime=runtime,
        plane_repositories=repositories,
        plane_blobs=object(),
    )


def _target():
    return MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                         username="me", cred_type="password", secret="x")


def _call(server: MCPServer, tool: str, **arguments):
    return server.process_request(
        MCPRequest(request_id="r1", method="tools/call",
                   params={"name": tool, "arguments": arguments})
    )


class _FakeWS:
    """Captures the frames ``handle_mcp_request`` writes back to the orchestrator."""

    def __init__(self):
        self.sent: list = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


# ── identity + construction ───────────────────────────────────────────────────

def test_agent_identity_is_the_single_grantable_remote_compute_agent():
    agent = _agent()
    assert agent.agent_id == "remote-compute-1"
    assert agent.service_name == "Remote Compute"
    assert "ssh" in agent.skill_tags and "control" in agent.skill_tags
    assert "confirm" in agent.description


def test_construction_wires_one_plane_binding_into_both_verb_libraries():
    obs.register_deps(None, None)
    ctl.register_deps(None, None, None)
    agent = _agent()
    assert agent.agent_id == "remote-compute-1"
    # The same pool-free binding + CredentialManager reach both risk tiers.
    binding = obs._DB
    assert binding is ctl._DB
    assert ctl._BLOBS is not None
    assert binding.plane_runtime is not None
    assert binding.plane_repositories is binding.plane_runtime.repositories
    assert isinstance(obs._CREDMGR, FakeCredentialManager)
    assert obs._CREDMGR is ctl._CREDMGR
    assert obs._CREDMGR.db is binding
    assert obs._CREDMGR.kwargs == {
        "plane_runtime": binding.plane_runtime,
        "plane_repositories": binding.plane_repositories,
    }


def test_explicit_port_wins_over_the_env_var(monkeypatch):
    monkeypatch.setenv("REMOTE_COMPUTE_AGENT_PORT", "9111")
    assert _agent(port=9222).port == 9222


def test_port_env_var_is_the_agents_own(monkeypatch):
    monkeypatch.setenv("REMOTE_COMPUTE_AGENT_PORT", "9111")
    runtime, repositories = _plane()
    assert RemoteComputeAgent(
        port=None,
        plane_runtime=runtime,
        plane_repositories=repositories,
        plane_blobs=object(),
    ).port == 9111


def test_missing_plane_dependency_fails_closed_without_rebinding():
    sentinel = object()
    obs.register_deps(sentinel, sentinel)
    ctl.register_deps(sentinel, sentinel, sentinel)

    with pytest.raises(RuntimeError, match="initialized AstralPlane"):
        RemoteComputeAgent(port=0, plane_runtime=None)
    assert obs._DB is sentinel and ctl._DB is sentinel


# ── the union (FR-024) ────────────────────────────────────────────────────────

def test_registry_is_exactly_nine_plus_nine_with_no_key_collision():
    assert len(obs.TOOL_REGISTRY) == 9 and len(ctl.TOOL_REGISTRY) == 9
    assert set(obs.TOOL_REGISTRY) & set(ctl.TOOL_REGISTRY) == set()
    assert len(unified.TOOL_REGISTRY) == 18
    assert set(unified.TOOL_REGISTRY) == set(obs.TOOL_REGISTRY) | set(ctl.TOOL_REGISTRY)


def test_register_deps_propagates_to_both_libraries():
    source, cm, blobs = object(), object(), object()
    unified.register_deps(source, cm, blobs)
    assert (obs._DB, obs._CREDMGR) == (source, cm)
    assert (ctl._DB, ctl._CREDMGR, ctl._BLOBS) == (source, cm, blobs)


def test_agent_serves_the_unified_registry_by_reference():
    # Not a copy: the entry dicts the agent serves ARE the ones the source
    # modules built, so destructive/scope metadata cannot drift.
    server = _agent().mcp_server
    assert server.tools is unified.TOOL_REGISTRY
    for name, entry in {**obs.TOOL_REGISTRY, **ctl.TOOL_REGISTRY}.items():
        assert server.tools[name] is entry


# ── agent card ────────────────────────────────────────────────────────────────

def test_card_publishes_all_eighteen_skills_with_their_scopes():
    card = _agent().card
    assert card.agent_id == "remote-compute-1"
    by_id = {s.id: s for s in card.skills}
    assert set(by_id) == set(unified.TOOL_REGISTRY)
    for name, entry in unified.TOOL_REGISTRY.items():
        assert by_id[name].scope == entry["scope"]
        assert by_id[name].input_schema == entry["input_schema"]
    assert {s.scope for s in card.skills} == {"tools:read", "tools:write", "tools:system"}


def test_card_carries_destructive_metadata_only_for_mutating_verbs():
    by_id = {s.id: s for s in _agent().card.skills}
    for name in ctl.TOOL_REGISTRY:
        assert by_id[name].metadata["destructive"] is ctl.TOOL_REGISTRY[name]["destructive"]
    for name in obs.TOOL_REGISTRY:
        assert "destructive" not in by_id[name].metadata


def test_card_metadata_carries_the_public_key_and_tags():
    card = _agent().card
    assert card.metadata["public_key_jwk"]["kty"] == "EC"
    assert all(set(s.tags) == set(RemoteComputeAgent.skill_tags) for s in card.skills)


# ── MCPServer routing ─────────────────────────────────────────────────────────

def test_tools_list_returns_every_verb_with_a_schema():
    server = _agent().mcp_server
    resp = server.process_request(MCPRequest(request_id="r0", method="tools/list"))
    tools = resp.result["tools"]
    assert len(tools) == 18
    assert {t["name"] for t in tools} == set(unified.TOOL_REGISTRY)
    assert all(t["description"] and t["input_schema"]["type"] == "object" for t in tools)


def test_unknown_tool_is_method_not_found():
    resp = _call(_agent().mcp_server, "rm_rf_everything")
    assert resp.error["code"] == -32601 and resp.error["retryable"] is False
    assert "rm_rf_everything" in resp.error["message"]


def test_unknown_method_is_rejected():
    resp = _agent().mcp_server.process_request(
        MCPRequest(request_id="r1", method="tools/teleport"))
    assert resp.error["code"] == -32601 and "tools/teleport" in resp.error["message"]


def test_tools_call_routes_into_the_read_library(monkeypatch):
    rows = [{"machine_id": "m1", "label": "dgx", "address": "10.0.0.5", "port": 22,
             "os_family": "linux", "role": "cluster", "last_verdict": "ok"}]
    monkeypatch.setattr("orchestrator.remote_machines.list_machines",
                        lambda db, uid: rows)
    resp = _call(_agent().mcp_server, "list_machines", user_id=USER)
    assert resp.error is None
    assert resp.result == {"count": 1, "machines": [
        {"machine_id": "m1", "label": "dgx", "address": "10.0.0.5",
         "role": "cluster", "last_verdict": "ok"}]}
    assert resp.ui_components[0]["type"] == "card"


def test_tools_call_routes_into_the_mutating_library(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.resolve_machine",
                        lambda db, uid, ref: {"machine_id": "m1", "label": "dgx"})
    monkeypatch.setattr("orchestrator.remote_machines.build_target",
                        lambda db, cm, uid, mid: _target())
    transport = FakeTransport(command_exit=0)
    set_transport(transport)

    resp = _call(_agent().mcp_server, "make_directory",
                 user_id=USER, machine_id="m1", path="/scratch/out")

    assert resp.error is None and resp.result["path"] == "/scratch/out"
    assert ["mkdir", "-p", "/scratch/out"] in [c["argv"] for c in transport.calls
                                               if c["op"] == "run"]


def test_error_variant_component_becomes_an_error_response():
    # No principal on the call → the verb's honest refusal alert, which the
    # dispatch contract must surface as an MCP error (not a success payload).
    resp = _call(_agent().mcp_server, "list_machines")
    assert resp.error["code"] == -32603 and resp.error["retryable"] is False
    assert "unattended_refused" in resp.error["message"]
    assert resp.ui_components is None


def test_arguments_are_filtered_to_the_functions_signature():
    server = _agent().mcp_server
    seen = {}

    def narrow(machine_id):
        seen["machine_id"] = machine_id
        return {"ok": True}

    server.tools = {"narrow": {"description": "d", "function": narrow,
                               "input_schema": {"type": "object", "properties": {}}}}
    resp = _call(server, "narrow", machine_id="m1", _runtime=object(), stray="drop me")
    assert resp.result == {"ok": True} and seen == {"machine_id": "m1"}


def test_var_keyword_tools_receive_every_argument():
    server = _agent().mcp_server
    seen = {}

    def wide(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    server.tools = {"wide": {"description": "d", "function": wide}}
    resp = _call(server, "wide", machine_id="m1", extra=7)
    assert resp.result == {"ok": True} and seen == {"machine_id": "m1", "extra": 7}


@pytest.mark.parametrize("exc,retryable", [
    (OSError("ssh died"), True),
    (TimeoutError("slow"), True),
    (ConnectionError("refused"), True),
    (ValueError("bad arg"), False),
    (KeyError("missing"), False),
    (TypeError("wrong"), False),
    (RuntimeError("unknown"), True),
])
def test_exception_retry_classification(exc, retryable):
    server = _agent().mcp_server

    def boom():
        raise exc

    server.tools = {"boom": {"description": "d", "function": boom}}
    resp = _call(server, "boom")
    assert resp.error["code"] == -32603 and resp.error["retryable"] is retryable


def test_plain_dict_result_passes_through_without_ui_components():
    server = _agent().mcp_server
    server.tools = {"plain": {"description": "d", "function": lambda: {"n": 1}}}
    resp = _call(server, "plain")
    assert resp.result == {"n": 1} and resp.ui_components is None


# ── handle_mcp_request (the transport-facing entry point) ─────────────────────

async def test_handle_mcp_request_dispatches_and_replies(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.list_machines",
                        lambda db, uid: [])
    agent = _agent()
    ws = _FakeWS()
    msg = MCPRequest(request_id="req-7", method="tools/call",
                     params={"name": "list_machines", "arguments": {"user_id": USER}})

    await agent.handle_mcp_request(ws, msg)

    (frame,) = ws.sent
    payload = json.loads(frame)
    assert payload["type"] == "mcp_response" and payload["request_id"] == "req-7"
    assert payload["error"] is None and payload["result"] == {"machines": []}
    assert payload["ui_components"][0]["type"] == "card"
    # The runtime bridge is injected for every tools/call (verbs take **kwargs).
    assert msg.params["arguments"]["_runtime"].agent_id == "remote-compute-1"


async def test_handle_mcp_request_serves_tools_list():
    agent = _agent()
    ws = _FakeWS()
    await agent.handle_mcp_request(ws, MCPRequest(request_id="req-8", method="tools/list"))
    payload = json.loads(ws.sent[0])
    assert len(payload["result"]["tools"]) == 18


# ── CLI entry point ───────────────────────────────────────────────────────────

def test_module_main_builds_the_agent_and_runs_it(monkeypatch):
    started = []
    closed = []
    runtime, repositories = _plane()

    composition = SimpleNamespace(
        runtime=runtime,
        repositories=repositories,
        blobs=object(),
        close=lambda: closed.append(True),
    )

    def _fake_asyncio_run(coro):
        started.append(coro)
        coro.close()  # never actually serve — the coroutine is inert until awaited

    monkeypatch.setattr(asyncio, "run", _fake_asyncio_run)
    monkeypatch.setattr(
        "orchestrator.plane_composition.compose_plane_from_environment",
        lambda _manifest: composition,
    )
    monkeypatch.setattr(sys, "argv", ["remote_compute_agent.py", "--port", "9333"])
    ns = runpy.run_path(agent_module.__file__, run_name="__main__")
    assert len(started) == 1
    assert ns["agent"].port == 9333 and ns["agent"].agent_id == "remote-compute-1"
    assert closed == [True]
