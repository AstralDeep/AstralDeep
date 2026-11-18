"""Covers orchestrator/node_api.py: the public v2 descriptor with a hash of the mesh owner and no accepted issuers, the JWKS, and the personal-profile gate on the node surface. test_node_mesh_offer.py covers the loopback pairing offer."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator import node_api
from shared import node_identity as ni
from tests.helpers.owner_file import seed_owner

MESH = "sha256:" + "4" * 64
STORED = "mesh." + "4" * 64
LEGACY = "oidc.0123456789abcdef.owner-1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/a")
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    ni.reset_cache()
    app = FastAPI()
    app.include_router(node_api.node_router)
    yield SimpleNamespace(app=app, monkeypatch=monkeypatch)
    ni.reset_cache()


def client(env):
    return AsyncClient(transport=ASGITransport(app=env.app), base_url="http://node.test")


async def test_descriptor_is_public_and_describes_the_node(env):
    async with client(env) as http:
        response = await http.get("/api/node/descriptor")
    body = response.json()
    identity = ni.load_node_identity()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert body["node_id"] == identity.node_id
    assert body["node_key"] == identity.public_jwk()
    assert body["format"] == "astral.node.descriptor/v2"
    assert "issuers" not in json.dumps(body) and "idp.example" not in json.dumps(body)
    assert body["owner_hash"] is None
    assert body["transport_addresses"] == []
    assert "mesh-pairing" in body["capabilities"]
    assert "d" not in body["node_key"]


@pytest.mark.parametrize("binding", ["bound", "wire_file", "env_wire", "env_stored"])
async def test_descriptor_carries_only_a_hash_of_the_bound_mesh_id(env, binding):
    if binding == "bound":
        ni.bind_mesh_owner(MESH)
    elif binding == "wire_file":
        seed_owner(MESH)
    else:
        env.monkeypatch.setenv(ni.OWNER_ENV, MESH if binding == "env_wire" else STORED)
    async with client(env) as http:
        body = (await http.get("/api/node/descriptor")).json()
    assert body["owner_hash"] == ni.owner_principal_hash(MESH) == "sha256:" + hashlib.sha256(MESH.encode()).hexdigest()
    assert MESH not in json.dumps(body) and STORED not in json.dumps(body)


@pytest.mark.parametrize("source", ["file", "env"])
async def test_a_legacy_owner_is_neither_hashed_nor_shown(env, source):
    if source == "file":
        stored = seed_owner(LEGACY)
    else:
        env.monkeypatch.setenv(ni.OWNER_ENV, LEGACY)
    async with client(env) as http:
        response = await http.get("/api/node/descriptor")
    assert response.status_code == 200
    assert response.json()["owner_hash"] is None and LEGACY not in response.text
    if source == "file":
        assert not (ni.data_dir() / ni.OWNER_FILE).exists()
        (kept,) = ni.data_dir().glob(ni.OWNER_FILE + ".legacy-*")
        assert kept.read_bytes() == stored


@pytest.mark.parametrize("stored", ["mesh." + "5" * 64])
async def test_a_configured_mesh_owner_over_a_different_owner_file_hashes_nothing(env, stored):
    before = seed_owner(stored)
    env.monkeypatch.setenv(ni.OWNER_ENV, MESH)
    async with client(env) as http:
        response = await http.get("/api/node/descriptor")
    assert response.status_code == 200 and response.json()["owner_hash"] is None
    assert (ni.data_dir() / ni.OWNER_FILE).read_bytes() == before


async def test_an_unreadable_owner_binding_is_reported_not_hidden(env):
    ni.data_dir().mkdir(parents=True)
    (ni.data_dir() / ni.OWNER_FILE).write_text("")
    async with client(env) as http:
        response = await http.get("/api/node/descriptor")
    assert (response.status_code, response.json()) == (503, {"detail": "node.owner_binding_unavailable"})
    assert response.headers["cache-control"] == "no-store"


async def test_node_surface_is_absent_outside_the_personal_profile(env):
    env.monkeypatch.delenv(ni.PROFILE_ENV)
    async with client(env) as http:
        assert (await http.get("/api/node/descriptor")).status_code == 404
        assert (await http.get("/.well-known/astral-node/jwks.json")).status_code == 404
        assert (await http.post("/api/node/mesh/offer")).status_code == 404


async def test_unusable_node_key_is_reported(env, tmp_path):
    env.monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "file" / "sub"))
    (tmp_path / "file").write_text("x")
    ni.reset_cache()
    async with client(env) as http:
        assert (await http.get("/api/node/descriptor")).status_code == 503


async def test_jwks_publishes_only_the_public_key(env):
    async with client(env) as http:
        response = await http.get("/.well-known/astral-node/jwks.json")
    assert response.json() == ni.load_node_identity().jwks()
