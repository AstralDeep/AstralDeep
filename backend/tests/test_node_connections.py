"""Proves a personal node lists the connections its owner's devices add, keeps the credentials in its encrypted keystore, never approves a connection itself, and offers no remote MCP tool on the personal profile.
It drives the real kernel binding through the seeded dev mesh with the fixture MCP server, and covers the node keystore, the development mesh gate, the legacy connector count and the caller ticket path to the withdrawn tool.
"""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace as NS

import astral_kernel as ak
import pytest

from orchestrator import node_connection_keystore as keystore
from orchestrator import node_connections, node_mesh, node_mesh_keys
from orchestrator.node_mesh_calls import OrchestratorCalls, grant_name
from shared import node_identity
from shared.protocol import AgentCard
from tests.fixtures.connections.oauth_server import FixtureAuth, FixtureConfig
from tests.helpers.dev_mesh import connection_reached, dev_node, until
from tests.test_caller_tickets import AGENT, Host, code_of, served, skill

REMOTE = "remote-mcp-1"
REMOTE_TOOLS = ("call_remote_tool", "list_remote_tools")


@pytest.fixture
def fixture_server():
    with FixtureAuth(FixtureConfig(open=True)) as auth:
        yield auth


def remote_host() -> Host:
    host = Host()
    host.agents[REMOTE] = object()
    host.local_agents[REMOTE] = object()
    host.agent_cards[REMOTE] = AgentCard(
        name="Remote", description="remote", agent_id=REMOTE,
        skills=[skill("call_remote_tool", "tools:execute"), skill("list_remote_tools")],
    )
    return host


def names(tools) -> set:
    return {tool["name"] for tool in tools}


async def test_a_dev_owner_adds_a_local_connection_and_the_node_lists_it_after_sync(tmp_path, monkeypatch, fixture_server):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        before = await node_connections.status(mesh.node)
        assert before["available"] and before["connections"] == []
        info = await mesh.owner.add_connection(fixture_server.rs_url, "fixture", ["search"])
        await connection_reached(mesh, info.id)
        listed = await node_connections.status(mesh.node)
        [seen] = listed["connections"]
        assert (seen["id"], seen["name"], seen["local"], seen["tools"]) == (info.id, "fixture", True, ["search"])
        assert seen["endpoint"] == fixture_server.rs_url and seen["scheme"] == "none"
        assert seen["supported_here"] is False and seen["this_device"]["state"] != "signed_in"
        assert listed["this_node"]["connections"] == 1
        assert listed["legacy_connectors"] == {"count": 0, "code": None}
        mesh.owner.remove_connection(info.id)
        await until(lambda: info.id not in [item.id for item in mesh.node.kernel.connections()], "the removal to reach the node")


async def test_the_node_listing_carries_no_credential_material(tmp_path, monkeypatch, fixture_server):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        info = await mesh.owner.add_connection(fixture_server.rs_url, "fixture", ["search"])
        await connection_reached(mesh, info.id)
        text = json.dumps(await node_connections.status(mesh.node))
        assert "token" not in text.lower() and "secret" not in text.lower()


async def test_the_node_signer_refuses_connection_consent(tmp_path):
    keys = node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=lambda: bytes(ak.new_operations_secret()))
    signer = node_mesh._signer(ak, keys)
    for kind in (kind for kind in ak.ConsentKind if kind != ak.ConsentKind.OTHER):
        with pytest.raises(ak.HostSignerError.Denied):
            signer.presence_sign(NS(kind=kind), b"message")


async def test_a_node_without_a_mesh_or_a_started_mesh_lists_nothing(tmp_path, monkeypatch):
    assert (await node_connections.status(None))["available"] is False
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    idle = node_mesh.NodeMesh(data_dir=tmp_path / "node", settings=node_mesh.MeshSettings(), binding=ak)
    assert (await node_connections.status(idle))["available"] is False


async def test_a_kernel_error_reading_connections_reports_unavailable_with_its_code(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        kernel = mesh.node.kernel
        await kernel.close()
        result = await node_connections.status(mesh.node)
        assert result["available"] is False and result["code"] in {"kernel.closed", "store.closed"}


def stored(tmp_path):
    return keystore.NodeKeystore(tmp_path, b"w" * 32)


def test_the_keystore_round_trips_and_seals_the_file(tmp_path):
    store = stored(tmp_path)
    assert store.read("astral.connection/v1/a/b") is None
    store.write("astral.connection/v1/a/b", b"top-secret")
    assert store.read("astral.connection/v1/a/b") == b"top-secret"
    [item] = (tmp_path / keystore.KEYSTORE_DIR).iterdir()
    assert stat.S_IMODE(item.stat().st_mode) == 0o600 and b"top-secret" not in item.read_bytes()
    assert store.accounts_present() == 1
    store.delete("astral.connection/v1/a/b")
    store.delete("astral.connection/v1/a/b")
    assert store.read("astral.connection/v1/a/b") is None and store.accounts_present() == 0


def test_the_keystore_never_reads_an_empty_or_foreign_value_as_a_credential(tmp_path):
    store = stored(tmp_path)
    with pytest.raises(keystore.KeystoreFailure):
        store.write("account", b"")
    with pytest.raises(keystore.KeystoreFailure):
        store.write("", b"x")
    with pytest.raises(keystore.KeystoreFailure):
        store.write("a" * 300, b"x")
    store.write("one", b"first")
    store.write("two", b"second")
    one, two = (store._path(name) for name in ("one", "two"))
    two.write_bytes(one.read_bytes())
    with pytest.raises(keystore.KeystoreFailure):
        store.read("two")
    one.write_bytes(b"not a token")
    with pytest.raises(keystore.KeystoreFailure):
        store.read("one")
    other = keystore.NodeKeystore(tmp_path, b"x" * 32)
    store.write("three", b"third")
    with pytest.raises(keystore.KeystoreFailure):
        other.read("three")
    store._path("three").chmod(0o644)
    with pytest.raises(keystore.KeystoreFailure):
        store.read("three")


def test_the_host_keystore_turns_every_failure_into_a_refusal(tmp_path):
    host = keystore.host_keystore(ak, stored(tmp_path))
    host.put("account", b"value")
    assert host.get("account") == b"value" and host.get("missing") is None
    with pytest.raises(ak.HostKeystoreError.Failed):
        host.put("account", b"")
    stored(tmp_path)._path("account").write_bytes(b"garbage")
    stored(tmp_path)._path("account").chmod(0o600)
    with pytest.raises(ak.HostKeystoreError.Failed):
        host.get("account")
    host.remove("account")
    with pytest.raises(ak.HostKeystoreError.Failed):
        host.remove("")


async def test_the_node_opens_its_kernel_with_the_keystore_and_closes_it_on_stop(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        kernel = mesh.node.kernel
        assert kernel.connections() == []
    with pytest.raises(ak.KernelError) as closed:
        kernel.connections()
    assert closed.value.code in {"kernel.closed", "store.closed"}


def test_a_production_gate_refuses_a_candidate_listing_a_development_device(monkeypatch):
    gate = node_mesh._mesh_gate(ak)
    from shared import dev_mesh_keys

    dev = dev_mesh_keys.device_ids()["owner"]
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    assert gate.accept(NS(mesh_id="sha256:" + "1" * 64, device_ids=["a" * 64])) is True
    assert gate.accept(NS(mesh_id="sha256:" + "1" * 64, device_ids=[dev])) is False
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert gate.accept(NS(mesh_id="sha256:" + "1" * 64, device_ids=[dev])) is True


async def test_an_accessor_error_is_not_a_supported_request_runtime(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        kernel = mesh.node.kernel
        await kernel.close()
        mesh.node._kernel = kernel
        with pytest.raises(node_mesh.MeshUnavailable) as refused:
            mesh.node.require_request_runtime(kernel)
        assert refused.value.code == "node.request_runtime_unsupported"


def test_the_personal_catalog_has_no_remote_mcp_tool_and_the_server_profile_keeps_it(monkeypatch):
    host = remote_host()
    owner = node_identity.owner_id_for_mesh("sha256:" + "0" * 64)
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    personal = names(OrchestratorCalls(host).published_tools(owner))
    assert AGENT and "read_value" in personal and not personal & set(REMOTE_TOOLS)
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.INSTITUTION)
    assert set(REMOTE_TOOLS) <= names(OrchestratorCalls(host).published_tools(owner))


async def test_a_personal_node_resolves_no_withdrawn_tool(monkeypatch):
    from orchestrator.node_mesh_calls import CallRefused

    host = remote_host()
    host.tool_permissions.is_tool_allowed = lambda *args: True
    owner = node_identity.owner_id_for_mesh("sha256:" + "0" * 64)
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    with pytest.raises(CallRefused) as refused:
        await OrchestratorCalls(host).resolve(owner, "call_remote_tool")
    assert refused.value.code == "node.tool_unknown"


def test_existing_grants_naming_the_withdrawn_tools_are_unchanged(monkeypatch):
    host = remote_host()
    owner = node_identity.owner_id_for_mesh("sha256:" + "0" * 64)
    grant = NS(agent="kernel", tool=grant_name("call_remote_tool", "sha256:" + "a" * 64), scope="*", consented=True)
    grants = [grant]
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    turn = OrchestratorCalls(host).turn_tools(owner, grants)
    assert grants == [grant] and grant.consented is True
    assert not {tool.tool for tool in turn} & set(REMOTE_TOOLS)


async def test_a_caller_ticket_naming_the_remote_tool_is_refused_and_so_is_a_call_through_it(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch, host=remote_host()) as door:
        with pytest.raises(ak.KernelError):
            door.mint(["call_remote_tool"])
        ticket = door.mint(["read_value"])
        response = await door.call_tool(ticket, "call_remote_tool")
        assert response.status_code == 403 and code_of(response) == "caller.tool_not_covered"
        assert not [call for call in door.host.calls if call.get("agent_id") == REMOTE]


class CredentialSpy:
    def __init__(self, keys):
        self.keys = keys
        self.read = []

    def list_credential_keys(self, owner, agent):
        assert agent == node_connections.LEGACY_AGENT_ID
        return list(self.keys)

    def get_credential(self, *args):
        self.read.append(args)
        raise AssertionError("a legacy connector value was read")


async def test_legacy_connector_rows_are_counted_by_key_name_and_their_bytes_are_never_read(tmp_path, monkeypatch):
    spy = CredentialSpy(["remote_mcp_" + "a" * 32, "remote_mcp_" + "b" * 32, "other"])
    async with dev_node(tmp_path, monkeypatch) as mesh:
        result = await node_connections.status(mesh.node, legacy_keys=node_connections.legacy_keys_of(spy, "owner"))
        assert result["legacy_connectors"] == {"count": 2, "code": "connection.legacy_connector"}
        assert spy.read == []
        assert node_connections.legacy_count(["remote_mcp_x", 3, None]) == 1


def test_the_binding_pin_names_the_phase_five_kernel():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "config"
    binding = json.loads((root / "astral-kernel-binding.json").read_text())
    composition = json.loads((root / "astral-composition.json").read_text())
    assert binding["source"]["commit"] == composition["components"]["astral-kernel"]["commit"]
    assert binding["source"]["commit"] == "ac2360b4f9fd8856fd7896677a80c80261e56833"
