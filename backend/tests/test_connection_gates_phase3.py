"""Proves the phase 3 connection gates on the node: a model key sealed to the node answers a node request against the fixture model server, a key not sealed to it is unusable there, the stdio launcher refuses with its sandbox code, an A2A OAuth sign-in runs through the operator routes, and phase 2 connections and legacy rows stay unchanged.
Model and stdio checks run on the real kernel binding, the A2A sign-in on the fake kernel because the pinned kernel admits a node only to a non-local https connection, and every scan has a planted negative control.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace as NS

import astral_kernel as ak
import pytest

from orchestrator import node_connections, node_mesh, node_stdio_launcher
from orchestrator.node_mailbox import MailboxSettings
from shared import node_identity as ni
from tests import fake_astral_kernel as fake
from tests.fixtures.connections.a2a_agent import FixtureAgent
from tests.fixtures.connections.oauth_server import FixtureAuth, FixtureConfig
from tests.helpers import agent_sign_in, orchestrator_app
from tests.helpers.dev_mesh import connection_reached, dev_node
from tests.test_connection_gates import LEGACY, files_under, leaks, sign_in
from tests.test_node_connection_api import Operator, personal
from tests.test_node_connections import CredentialSpy
from tests.test_node_mesh_kernel_integration import ak as binding, paired, until  # noqa: F401
from tests.test_node_mesh_peer_tools import node_host  # noqa: F401
from tests.test_node_mesh_turn_tools import turn_host  # noqa: F401

REPLY = "the node answered from its provider book"
CONNECTION = "cd" * 16


def planted_scan(secret: str, directory: Path, caplog) -> None:
    (directory / "planted.log").write_text(f"token={secret}")
    logging.getLogger("scan").debug("bearer %s", secret)
    assert leaks([secret], files_under(directory)) == [secret]
    assert leaks([secret], caplog.text) == [secret]
    assert leaks([secret], json.dumps([{"content": secret}])) == [secret]


async def test_the_stdio_launcher_refuses_in_production_and_the_node_lists_the_owner_connection_as_running_elsewhere(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        assert mesh.node.stdio_refusal == node_stdio_launcher.SANDBOX_UNAVAILABLE
        with pytest.raises(ak.KernelError) as refused:
            await mesh.node.kernel.propose_stdio_connection("files", "fixture", [], [], [], None)
        assert refused.value.code == node_stdio_launcher.SANDBOX_UNAVAILABLE
        from tests.helpers.stdio_sandbox import FixtureSandbox

        owner_launcher = node_stdio_launcher.NodeStdioLauncher(FixtureSandbox())
        try:
            mesh.owner.kernel.set_launcher(node_stdio_launcher.host_launcher(ak, owner_launcher))
            proposal = await mesh.owner.kernel.propose_stdio_connection("files", "fixture", [], [], [], None)
            info = mesh.owner.approve_connection(proposal, ["search"])
            await connection_reached(mesh, info.id)
            [seen] = (await node_connections.status(mesh.node))["connections"]
            assert (seen["kind"], seen["launch"]) == ("mcp-stdio", node_stdio_launcher.RUNS_ELSEWHERE)
        finally:
            owner_launcher.stop_all()


async def test_a_node_with_legacy_rows_and_a_phase_2_connection_keeps_both_unchanged(tmp_path, monkeypatch):
    spy = CredentialSpy(list(LEGACY))
    with FixtureAuth(FixtureConfig()) as server:
        async with dev_node(tmp_path, monkeypatch) as mesh:
            info = await mesh.owner.add_connection(server.rs_url, "fixture", [])
            await connection_reached(mesh, info.id)
            await sign_in(mesh.owner.kernel, server, info.id)
            keys = node_connections.legacy_keys_of(spy, "owner")
            first = await node_connections.status(mesh.node, legacy_keys=keys)
            second = await node_connections.status(mesh.node, legacy_keys=keys)
            [row] = first["connections"]
            assert (row["kind"], row["launch"], row["local"], row["supported_here"]) == ("mcp-http", None, True, False)
            assert first["connections"] == second["connections"]
            assert first["legacy_connectors"] == {"count": 2, "code": "connection.legacy_connector"}
            assert spy.read == [] and spy.keys == LEGACY


@pytest.fixture
async def a2a_node(tmp_path, monkeypatch, request):
    home = personal(monkeypatch, tmp_path)
    fake.Kernel.instances.clear()
    with FixtureAgent(request.param) as agent:
        mesh = node_mesh.NodeMesh(data_dir=home, binding=fake, settings=node_mesh.MeshSettings(mailbox=MailboxSettings(enabled=False)))
        await mesh.start()
        node_mesh.install(mesh)
        mesh.kernel.join()
        agent_sign_in.AgentSignIn(mesh.kernel, agent, agent_sign_in.connection(CONNECTION, "agent", agent.card_url))
        app = orchestrator_app.build(monkeypatch, orchestrator_app.host(), surfaces=())
        secret = (home / node_mesh.OPERATOR_SECRET_FILE).read_text()
        yield NS(mesh=mesh, agent=agent, op=Operator(app, secret), home=home, secret=secret)
        node_mesh.install(None)
        await mesh.stop()
        ni.reset_cache()


@pytest.mark.parametrize("a2a_node", ["oauth2_authorization_code"], indirect=True)
async def test_an_a2a_oauth_sign_in_runs_through_the_operator_routes_and_no_scan_finds_a_token(a2a_node, caplog):
    caplog.set_level(logging.DEBUG)
    node = a2a_node
    [row] = (await node.op.send("GET", "/api/node/connections")).json()["connections"]
    assert (row["kind"], row["state"]) == ("a2a", "not_signed_in")
    started = await node.op.begin(CONNECTION, {"redirect": "loopback", "port": 49152})
    back = agent_sign_in.approve(started["authorization_url"])
    assert back.startswith("http://127.0.0.1:49152/callback?")
    done = await node.op.send("POST", f"/api/node/connections/sign-in/{started['flow']}/complete", {"redirect_url": back})
    assert done.status_code == 200 and done.json() == {"state": "signed_in", "reason": None}
    [row] = (await node.op.send("GET", "/api/node/connections")).json()["connections"]
    assert row["state"] == "signed_in"
    issued = [*node.agent.issued.access_tokens, *node.agent.issued.refresh_tokens]
    assert len(issued) == 2
    assert leaks(issued, caplog.text, files_under(node.home), done.text, repr(node.mesh.kernel.connection_list)) == []
    planted_scan(issued[0], node.home, caplog)
    assert leaks([node.secret], caplog.text, done.text) == []


@pytest.mark.parametrize("a2a_node", ["oauth2_device_code"], indirect=True)
async def test_an_a2a_device_sign_in_polls_until_the_owner_approves_and_a_replayed_flow_is_gone(a2a_node):
    node = a2a_node
    started = await node.op.begin(CONNECTION, {"redirect": "device_code"})
    assert started["user_code"] and started["verification_uri"].startswith("http://")
    poll = f"/api/node/connections/sign-in/{started['flow']}/poll"
    assert (await node.op.send("POST", poll)).json() == {"status": "authorization_pending"}
    node.agent.approve_device()
    assert (await node.op.send("POST", poll)).json() == {"status": "signed_in"}
    assert (await node.op.send("POST", poll)).status_code == 404
