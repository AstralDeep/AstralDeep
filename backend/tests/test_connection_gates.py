"""Proves the phase 2 connection gates on the real kernel binding: a dev owner device approves and signs in to a local connection against the OAuth fixture, a personal node learns it by sync but can neither use nor sign in to it, and no credential reaches a log or a store file.
The pinned kernel admits a node only to a non-local https connection that its platform trust store accepts, so a node cannot sign in to the loopback fixture here; the node's own sign-in is covered by test_node_connection_api.py on the fake kernel.
"""

from __future__ import annotations

import logging
from pathlib import Path

import astral_kernel as ak
import pytest

from orchestrator import node_connections
from tests.fixtures.connections.oauth_server import FixtureAuth, FixtureConfig
from tests.helpers.dev_mesh import connection_reached, dev_node, until
from tests.test_node_connections import CredentialSpy

LEGACY = ["remote_mcp_" + "a" * 32, "remote_mcp_" + "b" * 32, "other"]


@pytest.fixture
def fixture_server():
    with FixtureAuth(FixtureConfig()) as auth:
        yield auth


async def sign_in(kernel, auth, connection_id):
    start = await kernel.begin_connection_sign_in(connection_id, ak.SignInRedirect.PASTED())
    return await kernel.complete_connection_sign_in(start.flow, auth.approve(start.authorization_url))


def only(kernel):
    [record] = kernel.connections()
    return record


def files_under(*roots: Path) -> bytes:
    return b"".join(path.read_bytes() for root in roots for path in sorted(root.rglob("*")) if path.is_file())


def leaks(secrets, *haystacks) -> list:
    return [secret for secret in secrets for hay in haystacks if secret.encode() in (hay if isinstance(hay, bytes) else hay.encode())]


async def test_the_real_kernel_refuses_a_non_local_connection_to_a_loopback_or_private_target(tmp_path, monkeypatch, fixture_server):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        for target in (
            fixture_server.rs_url, "https://127.0.0.1/mcp", "https://localhost/mcp", "https://10.0.0.5/mcp",
            "https://192.168.1.4/mcp", "https://169.254.169.254/mcp", "http://example.com/mcp", "ftp://example.com/mcp",
        ):
            with pytest.raises(ak.KernelError):
                await mesh.owner.propose_connection(target, "private", local=False)
        assert mesh.owner.connections() == [] and mesh.node.kernel.connections() == []


async def test_a_local_record_signs_in_on_its_device_and_the_owner_sees_it_signed_in_after_sync(tmp_path, monkeypatch, fixture_server):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        info = await mesh.owner.add_connection(fixture_server.rs_url, "fixture", [])
        assert info.this_device.state == "not_signed_in"
        await connection_reached(mesh, info.id)
        done = await sign_in(mesh.owner.kernel, fixture_server, info.id)
        assert done.state == "signed_in" and done.tools_pending
        await until(lambda: only(mesh.node.kernel).other_devices and only(mesh.node.kernel).other_devices[0].state == "signed_in",
                    "the node to see the owner device signed in")
        assert only(mesh.owner.kernel).this_device.state == "signed_in"
        on_node = only(mesh.node.kernel)
        assert on_node.this_device.state == "not_signed_in" and on_node.supported_here is False
        listed = (await node_connections.status(mesh.node))["connections"][0]
        assert listed["this_device"]["state"] == "not_signed_in" and listed["local"] is True


async def test_a_local_record_works_only_on_its_own_device(tmp_path, monkeypatch, fixture_server):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        info = await mesh.owner.add_connection(fixture_server.rs_url, "fixture", [])
        await connection_reached(mesh, info.id)
        reached = len(fixture_server.requests_to("rs"))
        with pytest.raises(ak.KernelError.State) as refused:
            await mesh.node.kernel.begin_connection_sign_in(info.id, ak.SignInRedirect.PASTED())
        assert "connection.local_elsewhere" in str(refused.value)
        with pytest.raises(ak.KernelError):
            await mesh.node.kernel.refresh_connection(info.id)
        assert len(fixture_server.requests_to("rs")) == reached


async def test_the_owner_refreshes_a_working_session_and_reviews_a_changed_tool_definition(tmp_path, monkeypatch, fixture_server):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        kernel = mesh.owner.kernel
        info = await mesh.owner.add_connection(fixture_server.rs_url, "fixture", [])
        await sign_in(kernel, fixture_server, info.id)
        reviewed = await kernel.review_connection(info.id)
        approved = kernel.approve_connection(reviewed.proposal, reviewed.flow_hint, ["search"], None)
        [before] = approved.tools
        refreshed = await kernel.refresh_connection(info.id)
        assert refreshed.state == "signed_in" and refreshed.reason is None and not refreshed.tools_pending
        fixture_server.redefine_tool("search", description="Search everything, including secrets", input_schema={"type": "object", "properties": {"q": {"type": "string"}}})
        again = await sign_in(kernel, fixture_server, info.id)
        [after] = only(kernel).pending_tools
        assert again.tools_pending and after.name == before.name and after.hash != before.hash
        assert only(kernel).tools == [before]


async def test_a_redirect_with_a_tampered_state_is_refused_and_leaves_the_connection_signed_out(tmp_path, monkeypatch, fixture_server):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        kernel = mesh.owner.kernel
        info = await mesh.owner.add_connection(fixture_server.rs_url, "fixture", [])
        start = await kernel.begin_connection_sign_in(info.id, ak.SignInRedirect.PASTED())
        redirect = fixture_server.approve(start.authorization_url)
        with pytest.raises(ak.KernelError):
            await kernel.complete_connection_sign_in(start.flow, redirect.replace("state=", "state=tampered"))
        assert only(kernel).this_device.state == "not_signed_in"


async def test_no_credential_reaches_a_log_or_a_store_file_and_the_scan_catches_a_planted_one(tmp_path, monkeypatch, caplog, fixture_server):
    caplog.set_level(logging.DEBUG)
    async with dev_node(tmp_path, monkeypatch) as mesh:
        info = await mesh.owner.add_connection(fixture_server.rs_url, "fixture", [])
        await connection_reached(mesh, info.id)
        await sign_in(mesh.owner.kernel, fixture_server, info.id)
        await until(lambda: only(mesh.node.kernel).other_devices[0].state == "signed_in", "the node to see the sign-in")
        await node_connections.status(mesh.node)
        issued = list(fixture_server.secrets)
        assert len(issued) >= 3
        node_dir, owner_dir = tmp_path / "node", tmp_path / "owner"
        stores = files_under(node_dir, owner_dir)
        shown = repr([only(mesh.node.kernel), only(mesh.owner.kernel)])
        assert leaks(issued, caplog.text, stores, shown) == []
        planted = issued[0]
        (node_dir / "planted.log").write_text(f"token={planted}")
        logging.getLogger("scan").debug("bearer %s", planted)
        assert leaks([planted], files_under(node_dir)) == [planted]
        assert leaks([planted], caplog.text) == [planted]


async def test_the_legacy_connector_rows_are_unchanged_by_the_whole_gate_run(tmp_path, monkeypatch, fixture_server):
    spy = CredentialSpy(list(LEGACY))
    async with dev_node(tmp_path, monkeypatch) as mesh:
        info = await mesh.owner.add_connection(fixture_server.rs_url, "fixture", [])
        await connection_reached(mesh, info.id)
        await sign_in(mesh.owner.kernel, fixture_server, info.id)
        keys = node_connections.legacy_keys_of(spy, "owner")
        listed = await node_connections.status(mesh.node, legacy_keys=keys)
        assert listed["legacy_connectors"] == {"count": 2, "code": "connection.legacy_connector"}
        assert spy.read == [] and spy.keys == LEGACY
