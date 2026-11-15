"""Tests for the mesh additions to the node descriptor and pairing response: the iroh address, the mesh and member ids after joining, the mailbox address policy and the node's mesh pairing offer."""

from __future__ import annotations

import pytest

from orchestrator import node_mesh
from tests import fake_astral_kernel as fake
from tests.test_node_api import JSON, OFFER, client, env  # noqa: F401
from tests.test_node_mesh import StubMailbox, no_issuers


@pytest.fixture
async def mesh(tmp_path):
    fake.Kernel.instances.clear()
    instance = node_mesh.NodeMesh(data_dir=tmp_path / "mesh-home", binding=fake, issuers=no_issuers, mailbox=StubMailbox())
    await instance.start()
    node_mesh.install(instance)
    yield instance
    node_mesh.install(None)


async def test_descriptor_without_a_mesh_is_unchanged(env):  # noqa: F811
    node_mesh.install(None)
    async with client(env) as http:
        body = (await http.get("/api/node/descriptor")).json()
    assert "mesh" not in body and body["transport_addresses"] == [] and "mailbox" not in body


async def test_descriptor_reports_the_endpoint_before_joining_but_not_the_mailbox(env, mesh):  # noqa: F811
    async with client(env) as http:
        body = (await http.get("/api/node/descriptor")).json()
    assert body["transport_addresses"] == [{"kind": "iroh", "address": '{"id":"endpoint-1","addrs":["127.0.0.1:1"]}'}]
    assert body["mesh"] == {"state": "not_enrolled", "mesh_id": None, "member_id": None, "hardware_backed": False, "requests": True}
    assert "mailbox" not in body and "mesh-peer" in body["capabilities"] and "mesh-mailbox" not in body["capabilities"]


async def test_descriptor_reports_the_mesh_member_and_mailbox_after_joining(env, mesh):  # noqa: F811
    fake.Kernel.instances[0].join("mesh-42")
    async with client(env) as http:
        body = (await http.get("/api/node/descriptor")).json()
    assert body["mesh"]["mesh_id"] == "mesh-42" and body["mesh"]["member_id"] == "dev-node"
    assert body["mailbox"] == {"kind": "iroh-mailbox", "address": '{"id":"mb"}'}
    assert "mesh-mailbox" in body["capabilities"]


async def test_the_descriptor_never_exposes_key_material(env, mesh):  # noqa: F811
    fake.Kernel.instances[0].join()
    async with client(env) as http:
        text = (await http.get("/api/node/descriptor")).text
    assert "secret" not in text and "BEGIN" not in text


async def test_pairing_includes_the_nodes_mesh_offer_and_mailbox(env, mesh):  # noqa: F811
    async with client(env) as http:
        response = await http.post("/api/node/pairing", json=OFFER, headers=JSON)
    body = response.json()
    assert response.status_code == 200 and "pairing" in body
    offer = body["mesh"]
    assert offer["state"] == "offer" and offer["code"] == "123-456-789" and offer["payload"] == "payload-1"
    assert offer["endpoint_address"] and offer["mailbox"]["address"] == '{"id":"mb"}'


async def test_pairing_after_joining_reports_the_membership_and_makes_no_offer(env, mesh):  # noqa: F811
    fake.Kernel.instances[0].join("mesh-7")
    async with client(env) as http:
        body = (await http.post("/api/node/pairing", json=OFFER, headers=JSON)).json()
    assert body["mesh"] == {"state": "enrolled", "mesh_id": "mesh-7", "member_id": "dev-node"}
    assert fake.Kernel.instances[0].offers == 0


async def test_a_mesh_offer_failure_is_reported_without_failing_the_device_invitation(env, mesh):  # noqa: F811
    async def refuse():
        raise fake.KernelError.State("pairing.busy")

    fake.Kernel.instances[0].create_pairing_offer = refuse
    async with client(env) as http:
        response = await http.post("/api/node/pairing", json=OFFER, headers=JSON)
    assert response.status_code == 200
    assert response.json()["mesh"] == {"state": "not_enrolled", "error": "pairing.busy"}


async def test_pairing_without_a_mesh_has_no_mesh_block(env):  # noqa: F811
    node_mesh.install(None)
    async with client(env) as http:
        body = (await http.post("/api/node/pairing", json=OFFER, headers=JSON)).json()
    assert "mesh" not in body
