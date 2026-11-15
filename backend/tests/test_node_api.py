"""Covers orchestrator/node_api.py: the public descriptor and JWKS, the owner-authenticated pairing offer, and its one-time short-code lookup."""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator import mesh_api
from orchestrator import mesh_enrollment as me
from orchestrator import node_api
from shared import node_identity as ni

OWNER = "oidc.0123456789abcdef.owner-1"
JSON = {"content-type": "application/json", "authorization": "Bearer synthetic"}


class FakeStore:
    def __init__(self):
        self.calls = []
        self.error = None
        self.expires_ms = int(time.time() * 1000) + 600_000

    def create_invitation(self, owner_id, **kwargs):
        self.calls.append((owner_id, kwargs))
        if self.error:
            raise self.error
        return {
            "invitation": {"invite_id": "inv-1", "expires_at": self.expires_ms, "status": "pending"},
            "payload": "payload_abc-123",
        }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/a")
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    monkeypatch.delenv(ni.ISSUERS_ENV, raising=False)
    ni.reset_cache()
    node_api.reset_state()
    store = FakeStore()
    actor = {"kind": "owner", "id": OWNER, "member": None, "claims": {}}
    app = FastAPI()
    app.include_router(node_api.node_router)
    app.dependency_overrides[mesh_api._read_actor] = lambda: actor
    app.dependency_overrides[mesh_api._store] = lambda: store
    yield SimpleNamespace(app=app, store=store, actor=actor, monkeypatch=monkeypatch)
    node_api.reset_state()
    ni.reset_cache()


def client(env):
    return AsyncClient(transport=ASGITransport(app=env.app), base_url="http://node.test")


OFFER = {"label": "Phone", "device_key": {"kty": "OKP"}, "scopes": ["tools:read"]}


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
        assert (await http.post("/api/node/pairing", json=OFFER, headers=JSON)).status_code == 404
        assert (await http.post("/api/node/pairing/resolve", json={"code": "ABCD-EFGH"})).status_code == 404


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


async def test_pairing_offer_creates_an_invitation_and_binds_the_owner(env):
    async with client(env) as http:
        response = await http.post("/api/node/pairing", json=OFFER, headers=JSON)
    body = response.json()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    owner_id, kwargs = env.store.calls[0]
    assert owner_id == OWNER
    assert kwargs["creator_kind"] == "owner" and kwargs["label"] == "Phone"
    assert kwargs["ttl_seconds"] == node_api.DEFAULT_OFFER_SECONDS
    assert ni.bound_owner() == OWNER
    pairing = body["pairing"]
    assert len(pairing["code"]) == 9 and pairing["code"][4] == "-"
    assert set(pairing["code"].replace("-", "")) <= set(node_api.CODE_ALPHABET)
    assert pairing["qr"].startswith(node_api.QR_PREFIX)
    document = json.loads(base64.urlsafe_b64decode(pairing["qr"][len(node_api.QR_PREFIX):] + "=="))
    identity = ni.load_node_identity()
    assert document == pairing["document"]
    assert document["payload"] == "payload_abc-123"
    assert document["node_id"] == identity.node_id and document["node_key"] == identity.public_jwk()
    assert document["owner_hash"] == ni.owner_principal_hash(OWNER)


async def test_pairing_refuses_a_different_owner_than_the_bound_one(env):
    ni.bind_owner("oidc.0123456789abcdef.someone-else")
    async with client(env) as http:
        response = await http.post("/api/node/pairing", json=OFFER, headers=JSON)
    assert response.status_code == 403
    assert env.store.calls == []


@pytest.mark.parametrize("ttl", [0, 59, 901, "600", True, 1.5])
async def test_pairing_bounds_the_offer_lifetime(env, ttl):
    async with client(env) as http:
        response = await http.post("/api/node/pairing", json={**OFFER, "ttl_seconds": ttl}, headers=JSON)
    assert response.status_code == 400
    assert env.store.calls == []


async def test_pairing_honours_a_valid_lifetime(env):
    async with client(env) as http:
        await http.post("/api/node/pairing", json={**OFFER, "ttl_seconds": 120}, headers=JSON)
    assert env.store.calls[0][1]["ttl_seconds"] == 120


async def test_invitation_failures_surface_without_binding_the_owner(env):
    env.store.error = ValueError("label must be 1-64 characters")
    async with client(env) as http:
        assert (await http.post("/api/node/pairing", json=OFFER, headers=JSON)).status_code == 400
        env.store.error = me.IdentityAmbiguous("holder")
        response = await http.post("/api/node/pairing", json=OFFER, headers=JSON)
    assert response.status_code == 409 and response.json()["detail"] == "mesh_identity_ambiguous"
    assert ni.bound_owner() is None


async def test_member_without_confirm_scope_cannot_offer_pairing(env):
    env.actor.update(kind="member", member={"owner_id": OWNER, "scopes": ["tools:read"], "updated_at": 1})
    async with client(env) as http:
        response = await http.post("/api/node/pairing", json=OFFER, headers=JSON)
    assert response.status_code == 403
    assert env.store.calls == []


async def test_member_with_confirm_scope_offers_pairing_for_the_owner(env):
    env.actor.update(
        kind="member", id="member-1", member={"owner_id": OWNER, "scopes": [me.CONFIRM_SCOPE], "updated_at": 7}
    )
    async with client(env) as http:
        response = await http.post("/api/node/pairing", json=OFFER, headers=JSON)
    assert response.status_code == 200
    owner_id, kwargs = env.store.calls[0]
    assert owner_id == OWNER and kwargs["creator_kind"] == "member" and kwargs["creator_revision"] == 7


async def test_pairing_writes_keep_the_mesh_write_guard(env):
    async with client(env) as http:
        query_token = await http.post("/api/node/pairing?token=abc", json=OFFER, headers=JSON)
        no_origin = await http.post("/api/node/pairing", json=OFFER, headers={"content-type": "application/json"})
        wrong_type = await http.post("/api/node/pairing", content="x", headers={"authorization": "Bearer t", "content-type": "text/plain"})
    assert query_token.status_code == 403
    assert no_origin.status_code == 403
    assert wrong_type.status_code == 415


async def test_pairing_capacity_is_bounded(env):
    env.monkeypatch.setattr(node_api, "MAX_OUTSTANDING_OFFERS", 2)
    async with client(env) as http:
        statuses = [(await http.post("/api/node/pairing", json=OFFER, headers=JSON)).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


async def offer_code(http):
    return (await http.post("/api/node/pairing", json=OFFER, headers=JSON)).json()["pairing"]["code"]


async def test_code_resolves_once_to_the_pairing_document(env):
    async with client(env) as http:
        code = await offer_code(http)
        first = await http.post("/api/node/pairing/resolve", json={"code": code.lower().replace("-", " ")})
        second = await http.post("/api/node/pairing/resolve", json={"code": code})
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert first.json()["pairing"]["document"]["payload"] == "payload_abc-123"
    assert first.json()["pairing"]["qr"].startswith(node_api.QR_PREFIX)
    assert second.status_code == 404


@pytest.mark.parametrize("code", [None, 5, "", "SHORT", "ABCD-EFGH-IJKL", "ABCD-EFG!", "x" * 40])
async def test_malformed_codes_are_refused(env, code):
    async with client(env) as http:
        response = await http.post("/api/node/pairing/resolve", json={"code": code})
    assert response.status_code == 400


async def test_unknown_and_expired_codes_are_refused(env):
    async with client(env) as http:
        assert (await http.post("/api/node/pairing/resolve", json={"code": "ABCD-EFGH"})).status_code == 404
        code = await offer_code(http)
        for offer in node_api._offers.values():
            offer["expires_at"] = time.time() - 1
        assert (await http.post("/api/node/pairing/resolve", json={"code": code})).status_code == 404


async def test_code_guessing_is_rate_limited(env):
    async with client(env) as http:
        statuses = [
            (await http.post("/api/node/pairing/resolve", json={"code": "ABCD-EFGH"})).status_code
            for _ in range(node_api.LOOKUP_MAX_PER_WINDOW + 1)
        ]
    assert statuses[: node_api.LOOKUP_MAX_PER_WINDOW] == [404] * node_api.LOOKUP_MAX_PER_WINDOW
    assert statuses[-1] == 429


async def test_rate_limit_table_is_swept_when_it_grows(env):
    env.monkeypatch.setattr(node_api, "_MAX_TRACKED_CLIENTS", 1)
    node_api._lookups.update({"a": [time.time() - 1000], "b": [], "c": [time.time()]})
    async with client(env) as http:
        await http.post("/api/node/pairing/resolve", json={"code": "ABCD-EFGH"})
    assert "a" not in node_api._lookups and "b" not in node_api._lookups
