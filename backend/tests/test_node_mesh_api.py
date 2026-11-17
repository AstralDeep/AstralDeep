"""Tests for the mesh additions to the node descriptor: the iroh address, the mesh and member ids after joining and the mailbox address policy. test_node_mesh_offer.py covers the pairing offer."""

from __future__ import annotations

import pytest

from orchestrator import node_mesh
from tests import fake_astral_kernel as fake
from tests.test_node_api import client, env  # noqa: F401
from tests.test_node_mesh import StubMailbox


@pytest.fixture
async def mesh(tmp_path):
    fake.Kernel.instances.clear()
    instance = node_mesh.NodeMesh(data_dir=tmp_path / "mesh-home", binding=fake, mailbox=StubMailbox())
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
    assert body["mesh"] == {"state": "not_enrolled", "mesh_id": None, "member_id": None, "hardware_backed": False, "requests": True, "owner_binding": {"state": "setup_required", "code": "mesh.not_enrolled"}}
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
