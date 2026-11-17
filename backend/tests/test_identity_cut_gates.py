"""Proves the identity cut's exit criteria that need no live device: a personal node refuses every bearer at every door and serves a mesh device's owner-minted caller ticket, only for its named tools, until it is revoked or its minter is removed.
It drives the real kernel binding, the dev mesh of tests/helpers/dev_mesh.py and the real MCP, A2A and REST doors, reusing the fixtures of test_node_bearer_refusal.py, test_caller_tickets.py and test_node_posture.py, and it never skips."""

from __future__ import annotations

import shutil

import astral_kernel as ak
import pytest

from devtools import dev_mesh
from orchestrator import node_mesh
from shared import node_identity
from tests import fake_astral_kernel
from tests import test_node_bearer_refusal as refusal
from tests import test_node_route_census as census
from tests.helpers.dev_mesh import DevMesh, dev_node, node_settings, publish_catalog, until
from tests.helpers.owner_file import seed_owner
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.test_caller_tickets import AGENT, Host, code_of, code_of_or_none, enrolled, served, tool_names
from tests.test_caller_tickets import test_the_real_dispatch_of_a_ticketed_call_sends_the_agent_a_token_for_that_tool_only as real_dispatch
from tests.test_node_posture import personal, production, refused  # noqa: F401

LEGACY_OWNER = "oidc.0123456789abcdef.owner-1"
OWNER_FILE_PREFIX = "mesh."


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("identity_cut_gates") as runtime:
        yield runtime


node = refusal.node
spies = refusal.spies
server = refusal.server
app = census.app


def test_the_gate_suite_runs_on_the_real_kernel_binding_and_the_node_loads_it():
    assert ak.__name__ == "astral_kernel" and node_mesh.load_binding() is ak
    assert not hasattr(ak, "instances") and ak is not fake_astral_kernel
    assert callable(ak.Kernel.verify_caller_request) and callable(ak.Kernel.mint_caller_ticket)


@pytest.mark.parametrize("method,path,status,body", refusal.REST_DOORS)
def test_gate_a_bearer_is_refused_at_each_rest_door(node, method, path, status, body):
    refusal.test_rest_doors_refuse_a_valid_bearer(node, method, path, status, body)


@pytest.mark.parametrize("method,path", refusal.SIGN_IN_DOORS)
def test_gate_sign_in_the_token_proxy_and_the_device_grant_are_unavailable(node, method, path):
    refusal.test_sign_in_token_proxy_and_device_grant_are_unavailable(node, method, path)


@pytest.mark.parametrize("method,path", refusal.LEGACY_DOORS)
def test_gate_legacy_enrollment_and_owner_token_pairing_are_closed(node, method, path):
    refusal.test_legacy_enrollment_doors_are_closed(node, method, path)


@pytest.mark.parametrize("door", sorted(census.IN_BAND))
def test_gate_a_bearer_is_refused_at_the_websocket_a2a_mcp_and_worker_doors(node, door):
    refusal.test_every_in_band_door_refuses_a_valid_bearer(node, door)


async def test_gate_each_rest_dependency_and_token_choke_point_refuses_before_any_key_fetch(node):
    await refusal.test_each_rest_dependency_refuses_before_parsing(node)
    await refusal.test_every_token_choke_point_refuses_on_the_node(node)


def test_gate_every_route_of_the_served_app_sits_behind_a_refusing_door_or_a_named_public_route(app):
    census.test_every_served_route_refuses_a_bearer_or_is_named(app)
    census.test_the_owner_token_pairing_routes_are_gone(app)


def test_gate_the_same_bearer_still_works_on_the_server_profile(server, monkeypatch):
    refusal.test_the_same_token_still_works_on_the_server_profile(server, monkeypatch)


async def test_gate_a_bearer_only_request_is_refused_and_a_ticketed_request_is_served(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        body, extra = door.mcp("tools/list")
        bearer = await door.client.post(
            "/mcp", content=body, headers={**extra, "Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.e30.c2ln",
                                           "Content-Type": "application/json"})
        assert (bearer.status_code, code_of(bearer)) == (401, "node.bearer_refused")
        a2a = await door.client.post("/a2a", content=b"{}", headers={"Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.e30.c2ln"})
        assert (a2a.status_code, code_of(a2a)) == (401, "node.bearer_refused")
        assert door.host.calls == []
        ticket = door.mint(["read_value"], label="Gate script")
        served_call = await door.call_tool(ticket, "read_value")
        assert served_call.status_code == 200, served_call.text
        assert served_call.json()["result"]["structuredContent"] == {"echo": "v"}
        owner = node_identity.bound_mesh_owner()
        call = door.host.calls[-1]
        assert (call["user_id"], call["agent_id"], call["tool_name"]) == (owner, AGENT, "read_value")
        assert call["claims"]["_caller_id"] == ticket.id and call["claims"]["sub"] == owner


async def test_gate_a_caller_runs_its_named_tool_and_is_refused_another(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        assert tool_names(await door.call_mcp(ticket, "tools/list")) == ["read_value"]
        assert (await door.call_tool(ticket, "read_value")).status_code == 200
        calls = len(door.host.calls)
        other = await door.call_tool(ticket, "write_value")
        assert (other.status_code, code_of(other)) == (403, "caller.tool_not_covered")
        assert len(door.host.calls) == calls


async def test_gate_revoking_a_ticket_refuses_it_and_leaves_the_others(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        revoked, kept = door.mint(["read_value"]), door.mint(["read_value"])
        assert (await door.call_tool(revoked, "read_value")).status_code == 200
        door.mesh.owner.kernel.revoke_caller_ticket(revoked.id)

        async def refused() -> bool:
            return code_of_or_none(await door.call_tool(revoked, "read_value")) == "caller.revoked"

        await until(refused, "the node to see the revoke")
        assert (await door.call_tool(kept, "read_value")).status_code == 200


async def test_gate_a_removed_devices_ticket_is_refused(tmp_path, monkeypatch):
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


async def test_gate_a_ticketed_call_reaches_the_agent_through_the_real_dispatch_with_a_token_for_its_tool_only(orchestrator_factory, monkeypatch):
    await real_dispatch(orchestrator_factory, monkeypatch)


async def test_gate_a_mesh_devices_request_reaches_the_node_with_the_mesh_owner(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        bound = node_identity.bound_owner()
        assert bound == mesh.node.personal_owner_id() == node_identity.owner_id_for_mesh(mesh.owner.mesh_id)
        _, request_id = mesh.owner.send_request(mesh.node_device, "what is on my calendar?")

        async def pending() -> bool:
            return any(item.id == request_id for item in await mesh.node.pending_requests())

        await until(pending, "the request to reach the node")
        request = node_mesh.RequestReference.capture(await mesh.node.request(request_id))
        context = await mesh.node.personal_request_context(request, bound, mesh.node.kernel)
        assert (context.mesh_id, context.member_id) == (mesh.owner.mesh_id, mesh.node_device)
        assert request.from_device == mesh.owner.device_id


@pytest.mark.parametrize("value", [None, "production"])
async def test_gate_a_dev_mesh_is_refused_outside_development_and_serves_no_ticket(tmp_path, monkeypatch, value):
    async with served(tmp_path, monkeypatch) as door:
        ticket = door.mint(["read_value"])
        assert (await door.call_tool(ticket, "read_value")).status_code == 200
        body, extra = door.mcp("tools/list")
        signed = door.mesh.owner.caller_headers(door.holder, ticket, "POST", "/mcp", body)
        if value is None:
            monkeypatch.delenv("ASTRAL_ENV")
        else:
            monkeypatch.setenv("ASTRAL_ENV", value)
        response = await door.post("/mcp", body, ticket, extra, headers=signed)
        assert (response.status_code, code_of(response)) == (503, "node.development_mesh")
        assert await door.mesh.node.refresh_personal_owner() is None
        assert (await door.mesh.node.status())["owner_binding"] == {"state": "refused", "code": "node.development_mesh"}
        with pytest.raises(dev_mesh.DevMeshRefused):
            door.mesh.owner.mint_caller_ticket(door.mesh.node_device, door.holder, ["read_value"])


@pytest.mark.parametrize("name,value", [
    ("AUDIT_HMAC_SECRET", "dev-audit-hmac-secret-change-me-in-prod"),
    ("AUDIT_HMAC_SECRET", "change-me"),
    ("AGENT_API_KEY", "change-me"),
])
def test_gate_a_placeholder_secret_exits_78_on_a_personal_node_with_no_environment(personal, monkeypatch, caplog, name, value):  # noqa: F811
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv(name, value)
    assert f"{name} is" in refused(caplog)


async def test_gate_two_meshes_on_one_machine_stay_isolated(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as door:
        second = dev_mesh.DevOwner("second-owner", tmp_path / "second-owner")
        other_node = node_mesh.NodeMesh(data_dir=tmp_path / "second-node", settings=node_settings())
        await other_node.start()
        try:
            second.create_mesh()
            second.confirm_recovery()
            await second.start()
            await second.approve_offer((await other_node.pairing_offer())["payload"])
            await until(lambda: enrolled(other_node), "the second node to join the second mesh")
            other_device = (await other_node.status())["member_id"]
            await publish_catalog(DevMesh(other_node, second, other_device, door.mesh.opened_ms), Host().published(node_identity.bound_mesh_owner()))
            assert second.mesh_id != door.mesh.owner.mesh_id
            foreign = second.mint_caller_ticket(other_device, door.holder, ["read_value"])
            response = await door.call_tool(foreign, "read_value")
            assert (response.status_code, code_of(response)) == (403, "caller.mesh_mismatch")
            assert door.host.calls == []
            assert (await door.call_tool(door.mint(["read_value"]), "read_value")).status_code == 200
            assert node_identity.bound_owner() == node_identity.owner_id_for_mesh(door.mesh.owner.mesh_id)
            assert {member.device_id for member in door.mesh.node.kernel.roster().members}.isdisjoint(
                member.device_id for member in other_node.kernel.roster().members)
        finally:
            await second.stop()
            await other_node.stop()


async def test_gate_a_legacy_owner_node_keeps_its_owner_and_data_and_serves_no_ticket(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch, bind=False) as door:
        marker = tmp_path / "node" / "legacy-rows.bin"
        marker.write_bytes(b"rows from before the cut")
        stored = seed_owner(LEGACY_OWNER)
        ticket = door.mint(["read_value"])
        assert await door.mesh.node.refresh_personal_owner() is None
        assert (await door.mesh.node.status())["owner_binding"] == {"state": "refused", "code": "node.legacy_owner"}
        response = await door.call_tool(ticket, "read_value")
        assert (response.status_code, code_of(response)) == (503, "node.owner_unbound")
        assert door.host.calls == []
        owner_file = tmp_path / "node" / node_identity.OWNER_FILE
        assert owner_file.read_bytes() == stored and not stored.decode().startswith(OWNER_FILE_PREFIX)
        assert marker.read_bytes() == b"rows from before the cut"


async def test_gate_a_node_keeps_its_binding_and_serves_a_device_restored_from_the_recovery_words(tmp_path, monkeypatch):
    kits = []
    created = dev_mesh.DevOwner.create_mesh

    def record(self):
        kit = created(self)
        kits.append(kit)
        return kit

    monkeypatch.setattr(dev_mesh.DevOwner, "create_mesh", record)
    async with served(tmp_path, monkeypatch) as door:
        owner_id = node_identity.bound_mesh_owner()
        assert len(kits) == 1 and len(kits[0].words.split()) == 24
        phone = dev_mesh.DevDevice("phone", tmp_path / "phone")
        await phone.start()
        await door.mesh.owner.approve_offer(await phone.pairing_offer())
        await until(phone.enrolled, "the phone to join")
        await phone.stop()
        del phone
        shutil.copytree(tmp_path / "phone", tmp_path / "restored")
        fresh = dev_mesh.DevDevice("second-phone", tmp_path / "restored")
        try:
            assert (await fresh.kernel.mesh_status()).state == ak.EnrollmentState.RESTORED
            result = fresh.kernel.recover_with_words(kits[0].words, None)
            assert result.mesh_id == door.mesh.owner.mesh_id
            await fresh.start()
            await until(lambda: any(m.device_id == fresh.device_id for m in door.mesh.node.kernel.roster().members),
                        "the node to learn the restored device")
            assert await door.mesh.node.refresh_personal_owner() == node_identity.bound_owner()
            assert node_identity.bound_mesh_owner() == owner_id
            await until(lambda: {t.tool.name for t in fresh.kernel.peer_tool_catalog()} >= {"read_value"}, "the catalog to reach the restored device")
            ticket = fresh.kernel.mint_caller_ticket(door.mesh.node_device, door.holder.holder_text, "Restored device", ["read_value"], False, 3600)
            response = await door.call_tool(ticket, "read_value")
            assert response.status_code == 200, response.text
            assert door.host.calls[-1]["user_id"] == owner_id
        finally:
            await fresh.stop()
