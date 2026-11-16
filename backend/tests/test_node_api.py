"""Covers orchestrator/node_api.py: the public descriptor and JWKS, and the personal-profile gate on the node surface. test_node_mesh_offer.py covers the loopback pairing offer."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator import node_api
from shared import node_identity as ni

OWNER = "oidc.0123456789abcdef.owner-1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/a")
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    monkeypatch.delenv(ni.ISSUERS_ENV, raising=False)
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
    assert body["accepted_issuers"] == ["https://idp.example/realms/a"]
    assert body["owner_hash"] is None
    assert body["transport_addresses"] == []
    assert "mesh-pairing" in body["capabilities"]
    assert "d" not in body["node_key"]


async def test_descriptor_carries_only_a_hash_of_the_bound_owner(env):
    ni.bind_owner(OWNER)
    async with client(env) as http:
        body = (await http.get("/api/node/descriptor")).json()
    assert body["owner_hash"] == ni.owner_principal_hash(OWNER)
    assert OWNER not in json.dumps(body)


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
