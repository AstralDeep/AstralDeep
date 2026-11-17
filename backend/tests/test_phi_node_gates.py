"""Proves the node's PHI gates on the real kernel binding through the seeded dev mesh: group adoption, destination approval by name, uncertain and oversized input, an excluded node, and the draft agent refusal on a PHI member node.
Only the model and MCP servers are loopback fixtures, and every refusal comes from the Kernel or from orchestrator/phi_node_policy.py through the normal request and lifecycle paths."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import astral_kernel as ak
import pytest

from orchestrator import api, node_mesh, phi_node_policy
from orchestrator.agent_lifecycle import AgentLifecycleManager
from orchestrator.session_store import assert_production_posture
from tests.fixtures.connections import FixtureAuth, FixtureConfig, ModelServer
from tests.helpers.dev_mesh import connection_reached, dev_node
from tests.helpers.dev_mesh import until as wait_until
from tests.test_fail_closed_boot import _configure_production_secrets

CLEAR = "Patient SSN 900-12-3456 was verified"
UNCERTAIN = "Appointment on 2024-03-15 was moved"
PLAIN = "Please summarise the weekly report"
REMOTE = "https://model.invalid/v1"
OVER_CAP = 1_048_577


async def until(condition, what):
    def settled():
        try:
            return condition()
        except ak.KernelError:
            return False

    await wait_until(settled, what)


def provider(name, base_url, devices, *, remote):
    return ak.ModelProviderConfig(
        name=name, base_url=base_url, models=["model-1"], api_keys=["fixture-key"], fallback_order=0,
        self_hosted=not remote, strict_tools=False, recipients=list(devices),
    )


async def serve_provider(mesh, name, base_url, *, remote):
    owner_device = (await mesh.owner.kernel.mesh_status()).device_id
    mesh.owner.kernel.set_model_provider(provider(name, base_url, [owner_device, mesh.node_device], remote=remote))
    await until(lambda: any(item.name == name for item in mesh.node.kernel.model_providers()), "the provider to reach the node")


async def ask_node(mesh, text, chat=None):
    chat_id, request = mesh.owner.send_request(mesh.node_device, text, chat_id=chat)
    await until(lambda: mesh.node.kernel.request(request), "the request to reach the node")
    return chat_id, request


def labelled(kernel, chat, text):
    return [message.labels.confidentiality for message in kernel.chat(chat).view.messages if message.text == text]


async def test_the_node_is_a_phi_member_only_after_the_owner_adopts_the_group(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch, adopt=False) as mesh:
        assert await mesh.node.phi_member() is False
        await until(mesh.owner.phi_markers_synced, "the PHI markers to sync")
        group = mesh.owner.adopt_phi_group()
        assert {member.label: member.state for member in group.members} == {"node": "included", "owner": "included"}
        await until(mesh.node.phi_member, "the node to join the PHI group")
        local = next(member for member in mesh.node.kernel.phi_group().members if member.is_local)
        assert local.state == "included" and mesh.node.kernel.phi_group().local_code is None
        assert (await mesh.node.peer_standing(mesh.owner.device_id)).phi is True


async def test_clear_phi_to_a_remote_model_is_refused_until_the_owner_approves_it_by_name_and_again_after_revocation(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        await serve_provider(mesh, "remote", REMOTE, remote=True)
        assert mesh.node.kernel.phi_destination_allowed("model", REMOTE) is False
        _, refused = await ask_node(mesh, CLEAR)
        result = await asyncio.wait_for(mesh.node.kernel.answer_request(refused), 30)
        assert (result.status, result.code) == ("failed", "flow.phi_to_model")
        proposal = mesh.owner.approve_provider_destination("remote")
        assert (proposal.kind, proposal.target, proposal.needs_approval) == ("model", REMOTE, True)
        await until(lambda: mesh.node.kernel.phi_destination_allowed("model", REMOTE), "the approval to reach the node")
        mesh.owner.revoke_destination(REMOTE)
        await until(lambda: not mesh.node.kernel.phi_destination_allowed("model", REMOTE), "the revocation to reach the node")
        _, again = await ask_node(mesh, CLEAR)
        result = await asyncio.wait_for(mesh.node.kernel.answer_request(again), 30)
        assert (result.status, result.code) == ("failed", "flow.phi_to_model")


async def test_a_loopback_model_is_on_the_device_so_a_member_node_sends_it_phi_and_labels_every_message_phi(tmp_path, monkeypatch):
    with ModelServer(reply="noted") as model:
        async with dev_node(tmp_path, monkeypatch) as mesh:
            await serve_provider(mesh, "local", model.url + "/v1", remote=False)
            chat, request = await ask_node(mesh, CLEAR)
            result = await asyncio.wait_for(mesh.node.kernel.answer_request(request), 30)
            assert (result.status, result.text) == ("completed", "noted")
            assert any(CLEAR in str(sent["messages"]) for sent in model.requests)
            assert labelled(mesh.node.kernel, chat, CLEAR) == ["phi"] and labelled(mesh.node.kernel, chat, "noted") == ["phi"]


async def test_uncertain_content_written_on_the_node_is_phi(tmp_path, monkeypatch):
    with ModelServer(reply="noted") as model:
        async with dev_node(tmp_path, monkeypatch) as mesh:
            await serve_provider(mesh, "local", model.url + "/v1", remote=False)
            chat, request = await ask_node(mesh, UNCERTAIN)
            assert (await asyncio.wait_for(mesh.node.kernel.answer_request(request), 30)).status == "completed"
            assert labelled(mesh.node.kernel, chat, UNCERTAIN) == ["phi"]


async def test_an_attachment_over_the_size_cap_is_phi_and_a_small_plain_one_is_not(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        kernel = mesh.owner.kernel
        quiet = kernel.create_chat("plain")
        small = kernel.add_attachment_attended(quiet, b"lorem ipsum " * 100, "text/plain", "small.txt", True)
        assert small.labels.confidentiality == "private"
        chat = kernel.create_chat("large")
        large = kernel.add_attachment_attended(chat, b"a" * OVER_CAP, "text/plain", "large.txt", True)
        assert large.labels.confidentiality == "phi"
        assert kernel.phi_status().failed >= 1
        await until(lambda: any(item.chat_id == chat for item in mesh.node.kernel.list_chats()), "the chat to reach the node")
        await until(lambda: mesh.node.kernel.chat(chat).attachments, "the attachment to reach the node")
        assert [item.labels.confidentiality for item in mesh.node.kernel.chat(chat).attachments] == ["phi"]


async def test_a_connection_tool_is_not_offered_to_a_phi_turn_on_the_node_and_nothing_reaches_the_server(tmp_path, monkeypatch):
    with FixtureAuth(FixtureConfig(open=True)) as auth, ModelServer(tool="search", arguments={"q": "x"}, reply="done") as model:
        async with dev_node(tmp_path, monkeypatch) as mesh:
            await serve_provider(mesh, "local", model.url + "/v1", remote=False)
            info = await mesh.owner.add_connection(auth.rs_url, "fixture", ["search"])
            await connection_reached(mesh, info.id)
            assert mesh.node.kernel.phi_destination_allowed("service", auth.rs_url) is False
            _, request = await ask_node(mesh, CLEAR)
            result = await asyncio.wait_for(mesh.node.kernel.answer_request(request), 30)
            assert result.status == "completed"
            offered = [tool["function"]["name"] for tool in model.requests[0].get("tools", [])]
            assert "search" not in offered
            assert "The tool 'search' is not available." in str(model.requests[-1]["messages"])
            assert [seen for seen in auth.mcp.seen if seen.method == "tools/call"] == []


async def test_an_excluded_node_gets_no_phi_body_and_regains_access_when_included_again(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        chat = mesh.owner.kernel.create_chat("clinic")
        mesh.owner.kernel.send_message(chat, PLAIN, None)
        await until(lambda: any(item.chat_id == chat for item in mesh.node.kernel.list_chats()), "the chat to reach the node")
        states = {member.label: member.state for member in mesh.owner.exclude_phi_device(mesh.node_device).members}
        assert states == {"node": "excluded", "owner": "included"}
        await until(lambda: mesh.node.kernel.phi_group().local_code == "phi.device_excluded", "the exclusion to reach the node")
        assert await mesh.node.phi_member() is False
        mesh.owner.kernel.send_message(chat, CLEAR, None)
        await until(lambda: mesh.node.kernel.chat(chat).view.unreadable, "the sealed PHI event to reach the node")
        view = mesh.node.kernel.chat(chat).view
        assert [message.text for message in view.messages] == [PLAIN]
        assert CLEAR not in repr(view)
        mesh.owner.include_phi_device(mesh.node_device)
        await until(mesh.node.phi_member, "the node to rejoin the PHI group")
        assert mesh.node.kernel.phi_group().local_code is None


async def test_a_draft_agent_is_refused_on_a_phi_member_node_and_runs_where_the_owner_excluded_it(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        node_mesh.install(mesh.node)
        try:
            with pytest.raises(phi_node_policy.AgentServerUnsupported) as refused:
                await phi_node_policy.refuse_on_phi_node()
            assert refused.value.detail() == {"code": "exec.agent_server_unsupported", "reason": "phi_node", "message": phi_node_policy.MESSAGE}
            mesh.owner.exclude_phi_device(mesh.node_device)
            await until(lambda: mesh.node.kernel.phi_group().local_code == "phi.device_excluded", "the exclusion to reach the node")
            await phi_node_policy.refuse_on_phi_node()
            mesh.owner.include_phi_device(mesh.node_device)
            await until(mesh.node.phi_member, "the node to rejoin the PHI group")
            with pytest.raises(phi_node_policy.AgentServerUnsupported):
                await phi_node_policy.refuse_on_phi_node()
        finally:
            node_mesh.install(None)


async def test_the_draft_agent_policy_allows_a_node_with_no_mesh_and_one_that_is_not_enrolled(tmp_path, monkeypatch):
    node_mesh.install(None)
    await phi_node_policy.refuse_on_phi_node()
    stopped = SimpleNamespace(started=False)
    node_mesh.install(stopped)
    try:
        await phi_node_policy.refuse_on_phi_node()
        async def status():
            return {"enrolled": False}

        node_mesh.install(SimpleNamespace(started=True, status=status))
        await phi_node_policy.refuse_on_phi_node()
    finally:
        node_mesh.install(None)


async def test_the_draft_agent_policy_fails_closed_when_membership_cannot_be_read():
    class Broken(Exception):
        pass

    async def status():
        return {"enrolled": True}

    async def member():
        raise Broken("roster unreadable")

    node_mesh.install(SimpleNamespace(started=True, status=status, phi_member=member, binding=SimpleNamespace(KernelError=Broken)))
    try:
        with pytest.raises(phi_node_policy.AgentServerUnsupported):
            await phi_node_policy.refuse_on_phi_node()
    finally:
        node_mesh.install(None)


async def test_start_draft_agent_stops_before_any_process_on_a_phi_node(monkeypatch):
    manager = object.__new__(AgentLifecycleManager)
    manager.db = SimpleNamespace(get_draft_agent=lambda draft_id: {"id": draft_id, "status": "generated", "agent_slug": "x", "origin": None})

    async def refuse():
        raise phi_node_policy.AgentServerUnsupported()

    monkeypatch.setattr("orchestrator.agent_lifecycle.refuse_on_phi_node", refuse)
    with pytest.raises(phi_node_policy.AgentServerUnsupported):
        await manager.start_draft_agent("draft-1")


async def test_the_start_route_reports_the_refusal_with_its_code_and_reason(monkeypatch):
    class Lifecycle:
        async def start_draft_agent(self, draft_id, websocket=None):
            raise phi_node_policy.AgentServerUnsupported()

    orch = SimpleNamespace()
    monkeypatch.setattr(api, "_get_orchestrator", lambda request: orch)
    monkeypatch.setattr(api, "_draft_store", lambda orchestrator: SimpleNamespace(get_owned_draft_agent=lambda user, draft: {"id": draft}))
    monkeypatch.setattr(api, "_get_lifecycle", lambda request: Lifecycle())
    monkeypatch.setattr(api, "_find_user_websocket", lambda orchestrator, user: None)
    start = api.test_draft
    with pytest.raises(api.HTTPException) as caught:
        await start(SimpleNamespace(), "draft-1", user_id="owner")
    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "exec.agent_server_unsupported", "reason": "phi_node", "message": phi_node_policy.MESSAGE}


@pytest.mark.parametrize("name,value", [("AUDIT_HMAC_SECRET", "change-me"), ("AUDIT_HMAC_SECRET", "dev-audit-hmac-secret-change-me-in-prod")])
def test_a_production_boot_with_a_placeholder_secret_still_exits_78(monkeypatch, name, value):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    _configure_production_secrets(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as exit_info:
        assert_production_posture()
    assert exit_info.value.code == 78


def test_the_pinned_kernel_names_the_phi_consent_kinds_and_the_attended_calls():
    assert {"GRANT_PHI", "REVOKE_PHI", "APPROVE_PHI_DESTINATION", "REGISTER_PHI_APPROVER"} <= set(ak.ConsentKind.__members__)
    assert all(hasattr(ak.Kernel, name) for name in ("send_message_attended", "add_attachment_attended", "phi_destination_allowed", "set_phi_prompt"))
