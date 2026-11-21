"""Covers the personal-profile delegation path: local EdDSA minting without Keycloak, scope derivation, member-token verification, chain signature checks and the stop gate."""

from __future__ import annotations

import time

import pytest

from orchestrator import agent_authority as dg
from orchestrator.emergency_stop import EmergencyStopCoordinator, EmergencyStopRefused
from shared import node_identity as ni

MESH = "sha256:" + "1" * 64
OWNER = ni.owner_id_for_mesh(MESH)
AGENT_AUDIENCE = "astral-agent-service"


@pytest.fixture
def personal(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    monkeypatch.delenv("DELEGATION_CHILD_SIGNING_KEY", raising=False)
    monkeypatch.delenv("MEMORY_HMAC_KEY", raising=False)
    ni.reset_cache()
    yield ni.load_node_identity()
    ni.reset_cache()


def other_node(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "other"))
    return ni.load_node_identity()


async def test_stop_counter_is_durable_and_only_rises(personal):
    events = []

    async def audit(action, outcome, *, owner_id, claims=None, detail=None):
        events.append({"action": action, "detail": dict(detail or {})})

    def loader(owner_id):
        for event in reversed(events):
            if event["action"] == "emergency_stop.engage":
                return {"engaged": True, "revision": event["detail"]["revision"],
                        "engaged_at": 1.0, "engaged_by": owner_id, "reason": ""}
            if event["action"] == "emergency_stop.resume" and not event["detail"].get("denial"):
                return {"engaged": False, "revision": event["detail"]["current_revision"]}
        return None

    def node_process():
        return EmergencyStopCoordinator(audit=audit, rearm_loader=loader)

    first = await node_process().engage(OWNER)
    restarted = node_process()
    assert restarted.admission_allowed(OWNER) is False
    await restarted.resume(OWNER, expected_revision=first["revision"], actor_id=OWNER)
    second = await restarted.engage(OWNER)
    assert second["revision"] == first["revision"] + 1
    with pytest.raises(EmergencyStopRefused) as stale:
        await node_process().resume(OWNER, expected_revision=first["revision"], actor_id=OWNER)
    assert stale.value.code == "emergency_stop_stale_revision"


def test_encoded_delegation_is_eddsa_and_child_tokens_keep_the_chain(personal):
    now = int(time.time())
    parent = {
        "sub": OWNER,
        "act": {"sub": "agent:a"},
        "scope": "tools:read tool:read",
        "iss": "x",
        "aud": AGENT_AUDIENCE,
        "iat": now,
        "exp": now + 900,
        "delegation": True,
    }
    token = dg.encode_delegation_payload(parent)
    claims = dg.verify_signed_delegation(token, audience=AGENT_AUDIENCE, issuer=personal.node_id)
    assert claims["exp"] - claims["iat"] == 300
    child = dg.mint_child_delegation(claims, "b", ["tools:read"], now=now)
    child_claims = dg.verify_signed_delegation(dg.encode_delegation_payload(child))
    assert dg.actor_chain(child_claims) == ["agent:b", "agent:a"]


def test_chain_verification_checks_the_signature_when_a_token_is_given(personal):
    now = int(time.time())
    payload = {
        "sub": OWNER, "act": {"sub": "agent:a"}, "scope": "tools:read", "iat": now,
        "exp": now + 100, "delegation": True, "aud": AGENT_AUDIENCE,
    }
    token = dg.encode_delegation_payload(payload)
    claims = dg.decode_token_payload(token)
    assert dg.verify_delegation_chain(claims, raw_token=token)[0] is True
    assert dg.verify_delegation_chain(dict(claims, scope="tools:write"), raw_token=token) == (
        False, "claims do not match the signed delegation")
    head, body, sig = token.split(".")
    forged = ".".join([head, body[:-2] + ("AA" if body[-2:] != "AA" else "BB"), sig])
    ok, reason = dg.verify_delegation_chain(dg.decode_token_payload(forged), raw_token=forged)
    assert ok is False and "refused" in reason
    assert dg.verify_delegation_chain(claims)[0] is True


def test_hmac_tokens_are_not_accepted_on_a_personal_node(personal, monkeypatch):
    from jose import jwt as jose_jwt

    now = int(time.time())
    claims = {"sub": "u", "iat": now, "exp": now + 60, "aud": "x", "iss": "i"}
    legacy = jose_jwt.encode(claims, b"shared-secret", algorithm="HS256")
    with pytest.raises(dg.RecursiveDelegationError):
        dg.verify_signed_delegation(legacy, audience="x", issuer="i")
    monkeypatch.delenv(ni.PROFILE_ENV)
    with pytest.raises(dg.RecursiveDelegationError):
        dg.verify_signed_delegation(legacy, audience="x", issuer="i")
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    with pytest.raises(dg.RecursiveDelegationError):
        dg.verify_signed_delegation(legacy)


def test_node_signed_tokens_are_refused_outside_the_personal_profile(personal, monkeypatch):
    now = int(time.time())
    token = dg.encode_delegation_payload({"sub": "u", "iat": now, "exp": now + 60, "aud": "x"})
    monkeypatch.delenv(ni.PROFILE_ENV)
    with pytest.raises(dg.RecursiveDelegationError):
        dg.verify_signed_delegation(token)
    with pytest.raises(dg.RecursiveDelegationError):
        dg.verify_signed_delegation("not-a-token")


def test_derived_hmac_key_keeps_hop_gating_working_without_configuration(personal):
    assert len(dg._child_signing_key()) == 32
    assert dg._child_signing_key() == dg._child_signing_key()


def test_unavailable_node_key_is_a_delegation_config_error(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(blocker / "sub"))
    ni.reset_cache()
    with pytest.raises(dg.DelegationConfigError):
        dg.encode_delegation_payload({"sub": "u"})
    with pytest.raises(dg.DelegationConfigError):
        dg._child_signing_key()


