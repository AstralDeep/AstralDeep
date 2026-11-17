"""Proves a personal node lets a script holding an owner-minted caller ticket and holder proof call only the named tools through the real MCP endpoint and /a2a, verified by the real kernel binding, and refuses every other request with the contract's code and status.
It uses the seeded dev mesh from tests/helpers/dev_mesh.py, orchestrator/caller_tickets.py, mcp_authz.py, mcp_server_endpoint.py and a2a_orchestrator_executor.py, and scripts/dev_mesh.py drives the same flow from the command line."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock

import astral_kernel as ak
import httpx
import pytest
from fastapi import FastAPI

from audit import hooks as audit_hooks
from audit.hooks import actor_principal_from_claims
from devtools import dev_mesh
from orchestrator import caller_tickets, node_mesh
from orchestrator.a2a_orchestrator_executor import public_catalog_agent_ids, setup_orchestrator_a2a
from orchestrator.a2a_publication_wiring import registered_audiences
from orchestrator.mcp_server_endpoint import install_mcp_server
from orchestrator.node_mesh_calls import OrchestratorCalls
from orchestrator.orchestrator import Orchestrator
from shared import node_identity
from shared.a2a_publication import A2APublicationRegistry
from shared.protocol import AgentCard, AgentSkill, MCPResponse
from tests.helpers.dev_mesh import DevMesh, dev_node, node_settings, now_ms, publish_catalog, until, wait_for_caller_pause
from tests.test_mcp_endpoint_064 import META, _Orchestrator

BASE = "http://mcp.test"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev_mesh.py"
AGENT = "weather-1"
OWNER_ID = node_identity.owner_id_for_mesh("sha256:" + "0" * 64)
UNSUPPORTED_MIRROR = {"type": "object", "properties": {"bad": {"type": "object", "x-mcp-header": "Mcp-Param-Bad"}}}


def skill(name: str, scope: str = "tools:read", schema: Optional[dict] = None) -> AgentSkill:
    return AgentSkill(
        id=name, name=name, description=f"{name} description", scope=scope,
        input_schema=schema or {"type": "object", "properties": {"value": {"type": "string"}}},
    )


class Permissions:
    def __init__(self, host: "Host"):
        self.host = host

    def is_tool_allowed(self, user_id, agent_id, tool_name):
        return True

    def get_tool_scope(self, agent_id, tool_name):
        card = self.host.agent_cards[agent_id]
        return next(item.scope for item in card.skills if item.id == tool_name)

    def get_enabled_scope_names(self, user_id, agent_id):
        return ["tools:read"]

    def _is_safe_agent(self, agent_id):
        return True

    def list_disabled_agents(self, _user_id):
        return ()


class Host(_Orchestrator):
    def __init__(self):
        super().__init__()
        self.agents = {AGENT: object()}
        self.local_agents = {AGENT: object()}
        self.agent_cards = {AGENT: AgentCard(
            name="Weather", description="reads", agent_id=AGENT,
            skills=[skill("read_value"), skill("write_value", "tools:write"), skill("ghost_tool", schema=UNSUPPORTED_MIRROR)],
        )}
        self.tool_permissions = Permissions(self)
        self.a2a_agent_owners = {}
        self.a2a_publications = A2APublicationRegistry(
            catalog=lambda: public_catalog_agent_ids(self), audiences=lambda agent_id: registered_audiences(self, agent_id),
        )
        self.execute_authorized_tool = AsyncMock(return_value=MCPResponse(result={"echo": "a2a"}))

    def published(self, owner: str) -> List[dict]:
        return OrchestratorCalls(self).published_tools(owner)

    async def execute_mcp_tool(self, **kwargs):
        self.calls.append(kwargs)
        return MCPResponse(result={"echo": kwargs["arguments"].get("value", "")})


class Audit:
    def __init__(self):
        self.events: List[Any] = []

    async def record(self, event):
        self.events.append(event)


@pytest.fixture
def audit(monkeypatch):
    recorder = Audit()
    monkeypatch.setattr(audit_hooks, "get_recorder", lambda: recorder)
    return recorder


class Door:
    def __init__(self, mesh: DevMesh, client: httpx.AsyncClient, host: Host):
        self.mesh, self.client, self.host = mesh, client, host
        self.holder = dev_mesh.CallerHolder.generate()

    def mint(self, tools, *, label="Dev script", a2a=False, lifetime_s=dev_mesh.DEFAULT_LIFETIME_S, node=None):
        return self.mesh.owner.mint_caller_ticket(
            node or self.mesh.node_device, self.holder, tools, label=label, a2a=a2a, lifetime_s=lifetime_s)

    async def post(self, path: str, body: bytes, ticket, extra: Optional[Dict[str, str]] = None, *,
                   signed_body: Optional[bytes] = None, signed_target: Optional[str] = None, holder=None,
                   headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        proof = headers if headers is not None else self.mesh.owner.caller_headers(
            holder or self.holder, ticket, "POST", signed_target or path, body if signed_body is None else signed_body)
        merged = {"Content-Type": "application/json", **(extra or {}), **proof}
        return await self.client.post(path, content=body, headers=merged)

    def mcp(self, method: str, params: Optional[dict] = None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {**(params or {}), "_meta": dict(META)}})
        extra = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
        if method == "tools/call":
            extra["Mcp-Name"] = (params or {})["name"]
        return body.encode(), extra

    async def call_mcp(self, ticket, method: str, params: Optional[dict] = None, **options) -> httpx.Response:
        body, extra = self.mcp(method, params)
        return await self.post("/mcp", body, ticket, extra, **options)

    async def call_tool(self, ticket, name: str, **options) -> httpx.Response:
        return await self.call_mcp(ticket, "tools/call", {"name": name, "arguments": {"value": "v"}}, **options)

    async def send_a2a(self, ticket, parts: Optional[list] = None, **options) -> httpx.Response:
        message = {"message_id": "m1", "role": "ROLE_USER", "parts": parts or [{"text": ""}]}
        body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": message}})
        return await self.post("/a2a", body.encode(), ticket, {"A2A-Version": "1.0"}, **options)


def code_of(response: httpx.Response) -> str:
    challenge = response.headers["www-authenticate"]
    return challenge.rpartition('error="')[2].rstrip('"')


@asynccontextmanager
async def served(tmp_path, monkeypatch, *, bind: bool = True) -> AsyncIterator[Door]:
    async with dev_node(tmp_path, monkeypatch, bind=bind) as mesh:
        previous = node_mesh.current()
        node_mesh.install(mesh.node)
        try:
            node_identity.load_node_identity()
            host = Host()
            owner = node_identity.owner_id_for_mesh((await mesh.node.status())["mesh_id"])
            await publish_catalog(mesh, host.published(owner))
            await wait_for_caller_pause(mesh)
            app = FastAPI()
            install_mcp_server(app, host, public_base_url=BASE)
            setup_orchestrator_a2a(app, host)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
                yield Door(mesh, client, host)
        finally:
            node_mesh.install(previous)


def tool_names(response: httpx.Response) -> List[str]:
    return [tool["name"] for tool in response.json()["result"]["tools"]]


def a2a_tools(response: httpx.Response) -> set:
    result = response.json()["result"]
    parts = (result.get("task", result).get("status", {}).get("message") or result).get("parts", [])
    return {tool["name"] for part in parts if "data" in part for tool in part["data"].get("tools", [])}


async def test_a_covered_tool_runs_and_the_list_shows_only_covered_tools(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value", "ghost_tool"], label="Nightly job")
        listed = await door.call_mcp(ticket, "tools/list")
        assert listed.status_code == 200, listed.text
        assert tool_names(listed) == ["read_value"]
        response = await door.call_tool(ticket, "read_value")
        assert response.status_code == 200, response.text
        assert response.json()["result"]["structuredContent"] == {"echo": "v"}
        call = door.host.calls[-1]
        owner = node_identity.bound_mesh_owner()
        assert (call["user_id"], call["agent_id"], call["tool_name"]) == (owner, AGENT, "read_value")
        assert call["claims"]["sub"] == owner and call["claims"]["_caller_id"] == ticket.id
        assert (await door.call_mcp(ticket, "server/discover")).status_code == 200


async def test_the_audited_dispatch_records_the_caller_id_and_label(tmp_path, monkeypatch, audit):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"], label="Nightly job")
        assert (await door.call_tool(ticket, "read_value")).status_code == 200
        claims = door.host.calls[-1]["claims"]
    owner = claims["sub"]
    orchestrator = Orchestrator.__new__(Orchestrator)
    invocation = object()
    orchestrator.ui_sessions = {invocation: {**claims, "_invocation_channel": "mcp"}}

    async def emit_hook(*_args, **_kwargs):
        return None

    async def dispatch(*_args, **_kwargs):
        return MCPResponse(result={"ok": True})

    orchestrator.hooks = NS(emit=emit_hook)
    orchestrator._execute_with_retry = dispatch
    await Orchestrator._execute_with_retry_audited(
        orchestrator, invocation, AGENT, "read_value", {"value": "v"}, user_id=owner,
    )
    start, end = audit.events
    for event in (start, end):
        assert (event.actor_user_id, event.auth_principal) == (owner, f"caller:{ticket.id}")
        assert event.inputs_meta["caller_label"] == "Nightly job"
        assert event.inputs_meta["invocation_channel"] == "mcp"
    assert end.outcome == "success"


async def test_a_covered_tool_the_endpoint_cannot_dispatch_is_unavailable(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["ghost_tool"])
        response = await door.call_tool(ticket, "ghost_tool")
        assert (response.status_code, code_of(response)) == (403, "caller.tool_unavailable")
        assert door.host.calls == []
        listed = await door.call_mcp(ticket, "tools/list")
        assert tool_names(listed) == []


async def test_requests_without_a_valid_presentation_are_refused_before_dispatch(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        body, extra = door.mcp("tools/list")
        proof = door.mesh.owner.caller_headers(door.holder, ticket, "POST", "/mcp", body)
        bearer = {"Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.e30.c2ln"}
        cases = [
            ({}, 401, "node.bearer_refused"),
            (bearer, 401, "node.bearer_refused"),
            ({"Authorization": proof["Authorization"].replace("AstralCaller", "Basic")}, 401, "caller.malformed"),
            ({"Authorization": "AstralCaller " + "A" * 40, "Astral-Caller-Proof": proof["Astral-Caller-Proof"]}, 401, "caller.malformed"),
            ({"Authorization": proof["Authorization"]}, 401, "caller.proof_malformed"),
            ({**proof, "Astral-Caller-Proof": "A" * 40}, 401, "caller.proof_malformed"),
        ]
        for headers, status, code in cases:
            response = await door.post("/mcp", body, ticket, extra, headers=headers)
            assert (response.status_code, code_of(response)) == (status, code), headers
        query = await door.client.post("/mcp?token=x", content=body, headers={**extra, **proof, "Content-Type": "application/json"})
        assert (query.status_code, code_of(query)) == (401, "node.bearer_refused")
        assert door.host.calls == []


async def test_each_denial_carries_its_code_and_status(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        other = dev_mesh.CallerHolder.generate()
        body, _ = door.mcp("tools/call", {"name": "read_value", "arguments": {"value": "v"}})
        changed = body.replace(b'"v"', b'"w"')
        cases = {
            "other tool": (await door.call_tool(ticket, "write_value"), 403, "caller.tool_not_covered"),
            "body changed": (await door.call_tool(ticket, "read_value", signed_body=changed), 403, "caller.proof_mismatch"),
            "target changed": (await door.call_tool(ticket, "read_value", signed_target="/other"), 403, "caller.proof_mismatch"),
            "wrong holder": (await door.call_tool(ticket, "read_value", holder=other), 401, "caller.proof_bad_signature"),
        }
        for name, (response, status, code) in cases.items():
            assert (response.status_code, code_of(response)) == (status, code), name
        assert door.host.calls == []
        resources = await door.call_mcp(ticket, "resources/list")
        assert (resources.status_code, code_of(resources)) == (403, "caller.tool_not_covered")


async def test_a_replayed_proof_is_refused(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        body, extra = door.mcp("tools/list")
        headers = door.mesh.owner.caller_headers(door.holder, ticket, "POST", "/mcp", body)
        first = await door.post("/mcp", body, ticket, extra, headers=headers)
        again = await door.post("/mcp", body, ticket, extra, headers=headers)
        assert first.status_code == 200
        assert (again.status_code, code_of(again)) == (403, "caller.replayed")


async def test_an_expired_ticket_is_refused(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"], lifetime_s=1)
        await asyncio.sleep(1.5)
        response = await door.call_tool(ticket, "read_value")
        assert (response.status_code, code_of(response)) == (403, "caller.expired")


async def test_a_revoked_ticket_is_refused_and_others_still_work(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        revoked, kept = door.mint(["read_value"]), door.mint(["read_value"])
        assert (await door.call_tool(revoked, "read_value")).status_code == 200
        door.mesh.owner.kernel.revoke_caller_ticket(revoked.id)

        async def refused() -> bool:
            return code_of_or_none(await door.call_tool(revoked, "read_value")) == "caller.revoked"

        await until(refused, "the node to see the revoke")
        assert (await door.call_tool(kept, "read_value")).status_code == 200


def code_of_or_none(response: httpx.Response) -> Optional[str]:
    return None if response.status_code == 200 else code_of(response)


async def test_a_raised_stop_refuses_tickets_minted_before_it(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        assert (await door.call_tool(ticket, "read_value")).status_code == 200
        door.mesh.owner.kernel.raise_stop()

        async def refused() -> bool:
            return code_of_or_none(await door.call_tool(ticket, "read_value")) is not None

        await until(refused, "the node to see the stop")
        response = await door.call_tool(ticket, "read_value")
        assert (response.status_code, code_of(response)) == (403, "caller.stopped")


async def test_a_changed_tool_definition_is_refused_before_and_after_the_node_republishes(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        assert (await door.call_tool(ticket, "read_value")).status_code == 200
        door.host.agent_cards[AGENT].skills[0].description = "returns a different value"
        calls = len(door.host.calls)
        stale = await door.call_tool(ticket, "read_value")
        assert (stale.status_code, code_of(stale)) == (403, "caller.tool_changed")
        owner = node_identity.bound_mesh_owner()
        await door.mesh.node.publish_tool_catalog(door.host.published(owner))
        republished = await door.call_tool(ticket, "read_value")
        assert (republished.status_code, code_of(republished)) == (403, "caller.tool_changed")
        assert len(door.host.calls) == calls


async def test_a_name_that_now_resolves_to_an_agent_outside_the_published_catalog_is_unavailable(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        door.host.agent_cards["custom-1"] = AgentCard(name="Custom", description="x", agent_id="custom-1", skills=[skill("read_value")])
        door.host.agents = {"custom-1": object()}
        door.host.local_agents = {}
        response = await door.call_tool(ticket, "read_value")
        assert (response.status_code, code_of(response)) == (403, "caller.tool_unavailable")
        assert door.host.calls == []


async def test_a_removed_minting_device_refuses_its_tickets(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        phone = dev_mesh.DevDevice("phone", tmp_path / "phone")
        await phone.start()
        try:
            await door.mesh.owner.approve_offer(await phone.pairing_offer())
            await until(phone.enrolled, "the phone to join")
            await until(lambda: {t.tool.name for t in phone.kernel.peer_tool_catalog()} >= {"read_value"}, "the catalog to reach the phone")
            ticket = phone.kernel.mint_caller_ticket(door.mesh.node_device, door.holder.holder_text, "Phone job", ["read_value"], False, 3600)
            assert (await door.call_tool(ticket, "read_value")).status_code == 200
            door.mesh.owner.kernel.remove_device(phone.device_id)

            async def refused() -> bool:
                return code_of_or_none(await door.call_tool(ticket, "read_value")) is not None

            await until(refused, "the node to see the removal")
            response = await door.call_tool(ticket, "read_value")
            assert (response.status_code, code_of(response)) == (403, "caller.device_removed")
        finally:
            await phone.stop()


async def test_a_ticket_for_another_node_is_refused(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        second = node_mesh.NodeMesh(data_dir=tmp_path / "second-node", settings=node_settings())
        await second.start()
        try:
            offer = await second.pairing_offer()
            await door.mesh.owner.approve_offer(offer["payload"])
            await until(lambda: enrolled(second), "the second node to join")
            device = (await second.status())["member_id"]
            await publish_catalog(DevMesh(second, door.mesh.owner, device, door.mesh.opened_ms), Host().published(OWNER_ID))
            ticket = door.mint(["read_value"], node=device)
            response = await door.call_tool(ticket, "read_value")
            assert (response.status_code, code_of(response)) == (403, "caller.wrong_node")
        finally:
            await second.stop()


async def enrolled(node: node_mesh.NodeMesh) -> bool:
    return bool((await node.status())["enrolled"])


async def test_a2a_needs_a2a_message_and_lists_only_covered_tools(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        tools_only = door.mint(["read_value"])
        refused = await door.send_a2a(tools_only)
        assert (refused.status_code, code_of(refused)) == (403, "caller.tool_not_covered")
        owner = node_identity.bound_mesh_owner()
        registry = door.host.a2a_publications
        intent = registry.create_intent(agent_id=AGENT, audiences=["tools:read"], lifetime="open", actor=owner)
        registry.confirm(intent_id=intent.intent_id, confirmation_token=intent.confirmation_token, actor=owner)
        ticket = door.mint(["read_value", "ghost_tool"], a2a=True)
        listed = await door.send_a2a(ticket)
        assert listed.status_code == 200, listed.text
        assert a2a_tools(listed) == {"read_value"}
        direct = await door.send_a2a(ticket, [{"data": {"method": "tools/call", "name": "read_value", "arguments": {}}}])
        assert direct.status_code == 200
        assert "unavailable" in json.dumps(direct.json())
        door.host.execute_authorized_tool.assert_not_called()
        bearer = await door.client.post("/a2a", content=b"{}", headers={"Authorization": "Bearer x"})
        assert (bearer.status_code, code_of(bearer)) == (401, "node.bearer_refused")
        card = await door.client.get("/a2a/.well-known/agent-card.json")
        assert card.status_code == 200


async def test_a2a_refuses_a_body_over_the_limit_declared_or_streamed(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"], a2a=True)
        headers = door.mesh.owner.caller_headers(door.holder, ticket, "POST", "/a2a", b"")

        async def chunks():
            for _ in range(2):
                yield b"x" * (600 * 1024)

        declared = await door.client.post("/a2a", content=b"x" * (1024 * 1024 + 1), headers=headers)
        streamed = await door.client.post("/a2a", content=chunks(), headers=headers)
        garbled = await door.client.post("/a2a", content=b"{}", headers={**headers, "Content-Length": "abc"})
        for response in (declared, streamed, garbled):
            assert (response.status_code, code_of(response)) == (401, "caller.malformed")


async def test_an_unbound_node_refuses_every_ticket(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch, bind=False) as door:
        ticket = door.mint(["read_value"])
        response = await door.call_tool(ticket, "read_value")
        assert (response.status_code, code_of(response)) == (503, "node.owner_unbound")
        assert door.host.calls == []


async def test_a_node_with_no_mesh_refuses(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        node_mesh.install(None)
        response = await door.call_tool(ticket, "read_value")
        assert (response.status_code, code_of(response)) == (503, "node.mesh_unavailable")


async def test_a_mesh_that_cannot_verify_refuses_as_unavailable(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])

        async def unavailable(**_request):
            raise node_mesh.MeshUnavailable("mesh_not_started")

        monkeypatch.setattr(door.mesh.node, "verify_caller", unavailable)
        response = await door.call_tool(ticket, "read_value")
        assert (response.status_code, code_of(response)) == (503, "node.mesh_unavailable")
        binding = door.mesh.node._binding
        door.mesh.node._binding = NS(KernelError=ak.KernelError)
        try:
            with pytest.raises(node_mesh.MeshUnavailable):
                await node_mesh.NodeMesh.verify_caller(
                    door.mesh.node,
                    authorization="x", proof=None, method="POST", target="/mcp", body_sha256="sha256:" + "0" * 64, tool=None,
                )
        finally:
            door.mesh.node._binding = binding


async def test_a_legacy_owner_binding_refuses(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        monkeypatch.setenv(node_identity.OWNER_ENV, "oidc." + "a" * 64)
        response = await door.call_tool(ticket, "read_value")
        assert (response.status_code, code_of(response)) == (503, "node.owner_unbound")


async def test_a_production_node_holding_a_dev_mesh_serves_no_ticket(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        assert (await door.call_tool(ticket, "read_value")).status_code == 200
        body, extra = door.mcp("tools/list")
        signed = [door.mesh.owner.caller_headers(door.holder, ticket, "POST", "/mcp", body) for _ in range(2)]
        monkeypatch.setenv("ASTRAL_ENV", "production")
        for headers in signed:
            response = await door.post("/mcp", body, ticket, extra, headers=headers)
            assert (response.status_code, code_of(response)) == (503, "node.development_mesh")


async def test_a_verified_mesh_that_is_not_the_bound_owners_mesh_is_refused(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        body, extra = door.mcp("tools/list")
        proof = door.mesh.owner.caller_headers(door.holder, ticket, "POST", "/mcp", body)
        monkeypatch.setattr(node_identity, "mesh_id_for_owner", lambda owner: "sha256:" + "0" * 64)
        response = await door.post("/mcp", body, ticket, extra, headers=proof)
        assert (response.status_code, code_of(response)) == (403, "caller.mesh_mismatch")


@pytest.mark.parametrize("code,status", [
    ("caller.malformed", 401), ("caller.proof_malformed", 401), ("caller.bad_signature", 401),
    ("caller.proof_bad_signature", 401), ("node.bearer_refused", 401),
    ("caller.mesh_mismatch", 403), ("caller.wrong_node", 403), ("caller.device_removed", 403),
    ("caller.wrong_minter", 403), ("caller.stopped", 403), ("caller.expired", 403), ("caller.not_yet_valid", 403),
    ("caller.revoked", 403), ("caller.proof_mismatch", 403), ("caller.proof_stale", 403), ("caller.replayed", 403),
    ("caller.tool_not_covered", 403), ("caller.tool_changed", 403), ("caller.tool_unavailable", 403),
    ("caller.replay_full", 503), ("mesh.not_enrolled", 503), ("gate.device_not_on_roster", 503),
    ("gate.own_removal_pending", 503), ("gate.recovery_pending", 503), ("node.development_mesh", 503),
    ("node.mesh_unavailable", 503), ("node.owner_unbound", 503),
    ("log.key_missing", 500), ("store.error", 500), ("canonical.invalid", 500), ("anything.else", 500),
])
def test_the_status_table_follows_the_kernel_readme(code, status):
    assert caller_tickets.status_for(code) == status
    assert caller_tickets.refusal(code).status == status


def test_the_request_target_is_the_path_and_query_as_sent():
    request = NS(headers={"authorization": "AstralCaller x"}, query_params={}, method="POST",
                 scope={"raw_path": b"/mcp", "query_string": b"a=b"}, url=NS(path="/mcp"))
    assert caller_tickets.request_target(request) == "/mcp?a=b"
    request.scope = {}
    assert caller_tickets.request_target(request) == "/mcp"


def test_claims_carry_the_owner_as_subject_and_the_caller_separately():
    verified = NS(id="abc", label="Job", tools=["a", "b"])
    claims = caller_tickets.claims_for("mesh.owner", verified, "astral-mcp")
    assert claims["sub"] == "mesh.owner" and claims["_caller_id"] == "abc" and claims["_caller_label"] == "Job"
    assert caller_tickets.covered(claims) == ("a", "b")
    assert caller_tickets.covered({"sub": "x"}) is None
    assert actor_principal_from_claims(claims) == ("mesh.owner", "caller:abc")
    assert actor_principal_from_claims({"sub": "u", "act": {"sub": "agent"}}) == ("u", "agent")


def load_script():
    spec = importlib.util.spec_from_file_location("scripts_dev_mesh", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_script_creates_pairs_and_mints_a_ticket_the_node_accepts(tmp_path, monkeypatch, capsys):
    script = load_script()
    monkeypatch.setenv(dev_mesh.ENV, dev_mesh.DEVELOPMENT)
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.delenv(node_identity.OWNER_ENV, raising=False)
    node_identity.reset_cache()
    owner_dir = str(tmp_path / "script-owner")
    assert await asyncio.to_thread(script.main, ["--dir", owner_dir, "create"]) == 0
    created = capsys.readouterr().out
    assert created.startswith("mesh sha256:")
    node = node_mesh.NodeMesh(data_dir=tmp_path / "node", settings=node_settings())
    await node.start()
    previous = node_mesh.current()
    node_mesh.install(node)
    try:
        offer = await node.pairing_offer()
        assert await asyncio.to_thread(script.main, ["--dir", owner_dir, "pair", "--offer", offer["payload"]]) == 0
        await until(lambda: enrolled(node), "the node to join")
        assert await node.refresh_personal_owner() is not None
        device = (await node.status())["member_id"]
        opened = now_ms()
        owner = dev_mesh.DevOwner("owner", Path(owner_dir))
        await owner.start()
        try:
            mesh = DevMesh(node, owner, device, opened)
            await publish_catalog(mesh, Host().published(OWNER_ID))
        finally:
            await owner.stop()
            del owner
        capsys.readouterr()
        code = await asyncio.to_thread(script.main, [
            "--dir", owner_dir, "ticket", "--node", device, "--tool", "read_value", "--a2a", "--label", "Cli job",
            "--lifetime-s", "3600",
        ])
        assert code == 0
        printed = dict(line.split(" ", 1) for line in capsys.readouterr().out.splitlines())
        assert printed["ticket"].startswith(dev_mesh.TICKET_TEXT) and printed["holder-key"].startswith(dev_mesh.KEY_TEXT)
        holder = dev_mesh.CallerHolder.from_key_text(printed["holder-key"])
        assert holder.key_text == printed["holder-key"]
        with pytest.raises(ValueError):
            dev_mesh.CallerHolder.from_key_text(holder.holder_text)
    finally:
        node_mesh.install(previous)
        await node.stop()


def test_the_script_refuses_outside_development(tmp_path, monkeypatch, capsys):
    script = load_script()
    monkeypatch.setenv(dev_mesh.ENV, "production")
    assert script.main(["--dir", str(tmp_path), "create"]) == 2
    assert "development" in capsys.readouterr().err


def test_an_unreadable_owner_binding_is_no_owner_for_the_admission_check(monkeypatch):
    from orchestrator import node_mesh_admission

    def unreadable():
        raise node_identity.LegacyOwner("legacy")

    monkeypatch.setattr(node_identity, "bound_mesh_owner", unreadable)
    assert node_mesh_admission._bound_mesh_owner() is None


def test_a2a_tasks_are_scoped_to_the_ticket_and_not_shared_across_tickets():
    from orchestrator.a2a_orchestrator_executor import A2APrincipal

    first = A2APrincipal(caller_tickets.claims_for("mesh.owner", NS(id="a", label="x", tools=[]), "astral-a2a"), "")
    second = A2APrincipal(caller_tickets.claims_for("mesh.owner", NS(id="b", label="y", tools=[]), "astral-a2a"), "")
    assert (first.user_name, second.user_name) == ("caller:a", "caller:b")
    assert A2APrincipal({"sub": "u"}, "t").user_name == "sub:u"


async def test_a_name_two_agents_share_cannot_be_covered_and_runs_nothing(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        door.host.agent_cards["other-1"] = AgentCard(name="Other", description="x", agent_id="other-1", skills=[skill("read_value")])
        door.host.agents["other-1"] = door.host.local_agents["other-1"] = object()
        ticket = door.mint(["read_value"])
        for name, code in (("read_value", "caller.tool_unavailable"), ("weather-1__read_value", "caller.tool_not_covered")):
            response = await door.call_tool(ticket, name)
            assert (response.status_code, code_of(response)) == (403, code), name
        with pytest.raises(ak.KernelError):
            door.mint(["weather-1__read_value"])
        assert door.host.calls == []


async def test_a_covered_call_after_a_restart_is_refused_until_the_catalog_is_republished(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        door.mesh.node._published_definitions = {}
        refused = await door.call_tool(ticket, "read_value")
        assert (refused.status_code, code_of(refused)) == (403, "caller.tool_changed")
        assert door.host.calls == []
        await door.mesh.node.publish_tool_catalog(door.host.published(node_identity.bound_mesh_owner()))
        assert (await door.call_tool(ticket, "read_value")).status_code == 200


async def test_the_ticketed_principal_carries_no_scope_claim_and_the_tool_list_decides(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["write_value"])
        assert tool_names(await door.call_mcp(ticket, "tools/list")) == ["write_value"]
        assert (await door.call_tool(ticket, "write_value")).status_code == 200
        claims = door.host.calls[-1]["claims"]
        assert not {"scope", "scp", "_framework_scopes"} & set(claims)
        refused = await door.call_tool(ticket, "read_value")
        assert (refused.status_code, code_of(refused)) == (403, "caller.tool_not_covered")


def ticketed_orchestrator(orchestrator_factory, covered: str = "read_value") -> Orchestrator:
    orch = orchestrator_factory()
    orch.tool_permissions.is_tool_allowed = lambda *_: True
    orch.tool_permissions.get_enabled_scope_names = lambda *_: ["tools:read", "tools:write"]
    orch.tool_permissions.get_tool_scope = lambda _agent, tool: "tools:write" if tool == "write_value" else "tools:read"
    orch.tool_permissions.get_tool_scope_map = lambda _agent: {"read_value": "tools:read", "write_value": "tools:write", "peer_tool": "tools:read"}
    orch.local_agents[AGENT] = object()
    orch.agent_cards[AGENT] = AgentCard(
        name="Weather", description="reads", agent_id=AGENT,
        skills=[skill("read_value"), skill("write_value", "tools:write")],
    )
    orch.local_agents["peer-1"] = object()
    orch.agent_cards["peer-1"] = AgentCard(name="Peer", description="x", agent_id="peer-1", skills=[skill("peer_tool")])
    return orch


def ticket_claims() -> Dict[str, Any]:
    return caller_tickets.claims_for(OWNER_ID, NS(id="c1", label="Nightly", tools=["read_value"]), "astral-mcp")


async def test_the_delegation_token_of_a_ticketed_call_names_only_that_tool(orchestrator_factory):
    from orchestrator import delegation as dg

    orch = ticketed_orchestrator(orchestrator_factory)
    session = {**ticket_claims(), "_invocation_channel": "mcp"}
    await orch._bind_caller_call(session, AGENT, "read_value")
    token = await orch._mint_mcp_delegation_token(AGENT, OWNER_ID, session)
    scopes = dg.decode_token_payload(token)["scope"].split()
    assert scopes == ["tools:read", "tool:read_value"]


async def test_a_ticketed_call_is_bound_to_its_tool_scope_and_definition(orchestrator_factory):
    from orchestrator.chain_authority import machine_scope_ceiling, machine_tool_admitted

    orch = ticketed_orchestrator(orchestrator_factory)
    session = {**ticket_claims(), "_invocation_channel": "mcp"}
    await orch._bind_caller_call(session, AGENT, "read_value")
    assert machine_scope_ceiling(session) == frozenset({"tools:read"})
    assert machine_tool_admitted(orch, session, AGENT, "read_value") is True
    assert machine_tool_admitted(orch, session, AGENT, "write_value") is False
    assert machine_tool_admitted(orch, session, "peer-1", "read_value") is False
    orch.agent_cards[AGENT].skills[0].description = "changed"
    assert machine_tool_admitted(orch, session, AGENT, "read_value") is False
    unscoped = {**ticket_claims(), "_caller_bound_tool": ("a", "b")}
    assert machine_scope_ceiling(unscoped) == frozenset()
    assert machine_tool_admitted(orch, unscoped, "a", "b") is False
    orch.tool_permissions.get_tool_scope = lambda *_: (_ for _ in ()).throw(RuntimeError("scope unavailable"))
    failed = {**ticket_claims()}
    await orch._bind_caller_call(failed, AGENT, "read_value")
    assert machine_scope_ceiling(failed) == frozenset()


async def test_a_chained_hop_from_a_ticketed_call_is_refused_and_the_uncovered_tool_never_runs(orchestrator_factory):
    from orchestrator import delegation as dg
    from shared.feature_flags import flags
    from shared.protocol import AgentHopRequest

    orch = ticketed_orchestrator(orchestrator_factory)
    orch._execute_with_retry = AsyncMock(return_value=MCPResponse(result="ran"))
    session = {**ticket_claims(), "_invocation_channel": "mcp"}
    await orch._bind_caller_call(session, AGENT, "read_value")
    invocation = object()
    orch.ui_sessions[invocation] = session
    token = await orch._mint_mcp_delegation_token(AGENT, OWNER_ID, session)
    orch._register_dispatch_context(
        "req-a", AGENT, {"user_id": OWNER_ID, "session_id": "c1", "_delegation_token": token}, invocation)
    prior = flags._flags.get("recursive_delegation")
    flags._flags["recursive_delegation"] = True
    try:
        future = asyncio.get_running_loop().create_future()
        ws = NS(_hop_futures={"hop-1": future})
        await orch._handle_agent_hop_request(ws, AgentHopRequest(
            request_id="hop-1", parent_request_id="req-a", initiator_agent_id=AGENT,
            callee_agent_id="peer-1", tool_name="peer_tool", arguments={}))
        response = await asyncio.wait_for(future, timeout=2)
    finally:
        flags._flags["recursive_delegation"] = prior
    assert response.result is None and "caller ticket" in response.error["message"]
    orch._execute_with_retry.assert_not_awaited()
    assert dg.decode_token_payload(token)["scope"].split() == ["tools:read", "tool:read_value"]


async def test_the_real_dispatch_of_a_ticketed_call_sends_the_agent_a_token_for_that_tool_only(orchestrator_factory, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "true")
    orch = ticketed_orchestrator(orchestrator_factory)
    orch.delegation.agent_service_client_id = "agent-service"
    orch._map_file_paths = lambda _chat, arguments, **_: arguments
    orch.credential_manager.get_agent_credentials_encrypted = lambda *_: None
    orch._execute_with_retry = AsyncMock(return_value=MCPResponse(result={"ok": True}))
    ran = await orch.execute_mcp_tool(
        claims=ticket_claims(), user_id=OWNER_ID, agent_id=AGENT, tool_name="read_value", arguments={})
    assert ran.error is None and orch._execute_with_retry.await_count == 1
    sent = orch._execute_with_retry.await_args.args[3]["_delegation_token"]
    from orchestrator import delegation as dg
    assert dg.decode_token_payload(sent)["scope"].split() == ["tools:read", "tool:read_value"]
