"""Enrolls a device on a personal node through real PostgreSQL and REST proofs: member and agent tokens are signed by the node key, no Keycloak exchange happens, and other nodes refuse the result."""

from __future__ import annotations

import time

import pytest
from jose import jwt

from orchestrator import delegation, mesh_admission as ma
from shared import node_identity as ni
from tests.helpers.mesh_runtime import MeshRuntime
from tests.helpers.voice_plane_runtime import isolated_plane_runtime


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("node_mesh_personal") as runtime:
        yield runtime


@pytest.fixture
def node(plane_runtime, monkeypatch, tmp_path):
    fixture = MeshRuntime(plane_runtime, monkeypatch)
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    monkeypatch.delenv("DELEGATION_CHILD_SIGNING_KEY", raising=False)
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)
    ni.reset_cache()
    try:
        yield fixture
    finally:
        fixture.close()
        ni.reset_cache()


def test_enrollment_issues_node_signed_tokens_without_a_keycloak_exchange(node):
    device, _ = node.enroll()
    identity = ni.load_node_identity()
    claims = ma.verify_member_token(device._token)
    assert node.exchanges == [] and node.exchange_forms == []
    assert ni.peek_algorithm(device._token) == "EdDSA"
    assert claims["iss"] == identity.node_id
    assert claims["sub"] == node.owner
    assert claims["scope"].split() == ["mesh:confirm", "tools:read"]
    with node.runtime.transaction() as transaction:
        stored = node.store._get_in_transaction(
            transaction, node.owner, ma.SESSION_NAMESPACE, device.device.member_id
        )
    agent_claims = identity.verify_token(stored["delegation_token"], audience="astral-agent-service")
    assert agent_claims["sub"] == node.owner
    assert agent_claims["act"] == {"sub": "agent:reader"}
    assert agent_claims["scope"].split() == ["tools:read", "tool:read"]
    assert agent_claims["exp"] - agent_claims["iat"] <= 300
    assert device.request("GET", "/api/mesh/me")["member"]["member_id"] == device.device.member_id


def test_member_sessions_keep_working_with_the_issuer_offline(node):
    device, _ = node.enroll()
    node.refresh_mode = "offline"
    device.session(agent_id="reader")
    claims = ma.verify_member_token(device._token)
    ma.assert_dispatch_current(node.host, claims, "reader", "tools:read")
    assert node.exchanges == []


def test_a_member_token_from_another_node_is_refused(node, tmp_path, monkeypatch):
    device, _ = node.enroll()
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "second-node"))
    ni.reset_cache()
    with pytest.raises(ma.MeshAdmissionError):
        ma.verify_member_token(device._token)
    response = node.client.get("/api/mesh/me", headers={"Authorization": "Bearer " + device._token})
    assert response.status_code in {401, 403}


def test_unconfirmed_or_expired_custody_still_refuses_activation(node):
    device, invitation = node.invitation()
    response = node.client.post(
        "/api/mesh/enrollment/redeem",
        json=device.redemption(invitation["payload"], agent_id="reader"),
    )
    assert response.status_code in {400, 403, 409}
    assert node.store.list_members(node.owner) == []


def test_delegation_stays_least_privilege_for_a_narrow_member(node):
    device, _ = node.enroll(scopes=["tools:read"])
    claims = ma.verify_member_token(device._token)
    assert claims["scope"].split() == ["tools:read"]
    assert "tools:write" not in claims["scope"]
    unsigned = jwt.get_unverified_claims(device._token)
    assert unsigned["exp"] - unsigned["iat"] <= ma.MAX_TOKEN_SECONDS
    assert unsigned["exp"] > int(time.time())
    assert delegation.decode_token_payload(device._token)["delegation"] is True
