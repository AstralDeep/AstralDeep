"""Covers the personal-profile delegation path: local EdDSA minting without Keycloak, scope derivation, member-token verification, chain signature checks and the stop gate."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import delegation as dg
from orchestrator import mesh_admission as ma
from orchestrator.emergency_stop import EmergencyStopCoordinator, EmergencyStopRefused
from shared import node_identity as ni

OWNER = ni.canonical_owner_id("https://idp.example/realms/a", "owner-1")
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


def orchestrator(allowed=("read",), stored_scopes=("tools:read", "tools:write")):
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.ui_sessions = {}
    orch.security_flags = {}
    orch.agent_cards = {
        "agent": SimpleNamespace(skills=[SimpleNamespace(id="read"), SimpleNamespace(id="write")])
    }
    scope_of = {"read": "tools:read", "write": "tools:write"}
    orch.tool_permissions = SimpleNamespace(
        is_tool_allowed=MagicMock(side_effect=lambda user, agent, tool: tool in allowed),
        get_tool_scope=MagicMock(side_effect=lambda agent, tool: scope_of[tool]),
        get_enabled_scope_names=MagicMock(return_value=list(stored_scopes)),
    )
    orch.delegation = SimpleNamespace(
        agent_service_client_id=AGENT_AUDIENCE,
        exchange_token_for_agent=AsyncMock(side_effect=AssertionError("Keycloak must not be asked")),
    )
    return orch


async def test_personal_node_mints_delegation_locally_without_keycloak(personal):
    orch = orchestrator()
    socket = object()
    orch.ui_sessions[socket] = {"sub": OWNER}
    token = await orch._get_delegation_token(socket, "agent", OWNER)
    claims = personal.verify_token(token, audience=AGENT_AUDIENCE)
    assert ni.peek_algorithm(token) == "EdDSA"
    assert claims["iss"] == personal.node_id
    assert claims["aud"] == [AGENT_AUDIENCE, personal.node_id]
    assert claims["sub"] == OWNER and claims["act"] == {"sub": "agent:agent"}
    assert claims["delegation"] is True
    assert claims["exp"] - claims["iat"] <= 300
    orch.delegation.exchange_token_for_agent.assert_not_awaited()


async def test_scopes_come_from_allowed_tools_not_stored_rows(personal):
    orch = orchestrator(allowed=("read",), stored_scopes=("tools:read", "tools:write"))
    socket = object()
    orch.ui_sessions[socket] = {"sub": OWNER}
    claims = personal.verify_token(await orch._get_delegation_token(socket, "agent", OWNER))
    assert claims["scope"].split() == ["tools:read", "tool:read"]
    orch.tool_permissions.get_enabled_scope_names.assert_not_called()


async def test_no_allowed_tool_means_no_token(personal):
    orch = orchestrator(allowed=(), stored_scopes=("tools:read",))
    socket = object()
    orch.ui_sessions[socket] = {"sub": OWNER}
    assert await orch._get_delegation_token(socket, "agent", OWNER) is None


async def test_blocked_tools_are_left_out(personal):
    orch = orchestrator(allowed=("read", "write"))
    orch.security_flags = {"agent": {"write": {"blocked": True}}}
    socket = object()
    orch.ui_sessions[socket] = {"sub": OWNER}
    claims = personal.verify_token(await orch._get_delegation_token(socket, "agent", OWNER))
    assert claims["scope"].split() == ["tools:read", "tool:read"]


async def test_mismatched_unknown_and_keyless_requests_get_nothing(personal, tmp_path, monkeypatch):
    orch = orchestrator()
    socket = object()
    orch.ui_sessions[socket] = {"sub": OWNER}
    assert await orch._get_delegation_token(socket, "agent", "oidc.x.someone-else") is None
    assert await orch._get_delegation_token(socket, "missing", OWNER) is None
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(blocker / "sub"))
    ni.reset_cache()
    assert await orch._get_delegation_token(socket, "agent", OWNER) is None
    assert orch.ui_sessions[socket]["_delegation_fault"] == "signing_key_unset"


async def test_a_token_minted_for_this_node_is_refused_by_another(personal, tmp_path, monkeypatch):
    orch = orchestrator()
    socket = object()
    orch.ui_sessions[socket] = {"sub": OWNER}
    token = await orch._get_delegation_token(socket, "agent", OWNER)
    other = other_node(tmp_path, monkeypatch)
    with pytest.raises(ni.NodeTokenError):
        other.verify_token(token, audience=AGENT_AUDIENCE)
    with pytest.raises(dg.RecursiveDelegationError):
        dg.verify_signed_delegation(token)


async def test_machine_turn_scope_ceiling_applies(personal):
    orch = orchestrator(allowed=("read", "write"))
    socket = object()
    orch.ui_sessions[socket] = {
        "sub": OWNER,
        "machine_class": "scheduled_job",
        "_machine_authority_scopes": ("tools:read",),
    }
    claims = personal.verify_token(await orch._get_delegation_token(socket, "agent", OWNER))
    assert claims["scope"].split() == ["tools:read", "tool:read"]
    orch.ui_sessions[socket]["_machine_authority_agent"] = "another-agent"
    assert await orch._get_delegation_token(socket, "agent", OWNER) is None
    orch.ui_sessions[socket] = {"sub": OWNER, "machine_class": "scheduled_job"}
    assert await orch._get_delegation_token(socket, "agent", OWNER) is None


async def test_raising_the_stop_counter_halts_new_delegation(personal):
    orch = orchestrator()
    orch.emergency_stop = EmergencyStopCoordinator()
    socket = object()
    orch.ui_sessions[socket] = {"sub": OWNER}
    assert await orch._get_delegation_token(socket, "agent", OWNER) is not None
    status = await orch.emergency_stop.engage(OWNER, reason="drill")
    assert status["engaged"] is True
    assert await orch._get_delegation_token(socket, "agent", OWNER) is None
    other = ni.canonical_owner_id("https://idp.example/realms/a", "someone-else")
    orch.ui_sessions[socket] = {"sub": other}
    assert await orch._get_delegation_token(socket, "agent", other) is not None
    resumed = await orch.emergency_stop.resume(OWNER, expected_revision=status["revision"])
    assert resumed["engaged"] is False
    orch.ui_sessions[socket] = {"sub": OWNER}
    assert await orch._get_delegation_token(socket, "agent", OWNER) is not None


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


def test_delegation_required_cannot_be_switched_off_on_a_personal_node(personal, monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    monkeypatch.setenv("DELEGATION_REQUIRED", "false")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    assert orch._delegation_required() is True
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert orch._delegation_required() is False
    monkeypatch.setenv(ni.PROFILE_ENV, "institution")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    assert orch._delegation_required() is False


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
    monkeypatch.delenv(ni.PROFILE_ENV)
    monkeypatch.setenv("DELEGATION_CHILD_SIGNING_KEY", "shared-secret")
    now = int(time.time())
    legacy = dg.encode_delegation_payload({"sub": "u", "iat": now, "exp": now + 60, "aud": "x", "iss": "i"})
    assert dg.verify_signed_delegation(legacy, audience="x", issuer="i")["sub"] == "u"
    with pytest.raises(dg.RecursiveDelegationError):
        dg.verify_signed_delegation(legacy.rsplit(".", 1)[0] + ".AAAA", audience="x")
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


def member_claims(personal, **changes):
    now = int(time.time())
    payload = {
        "sub": OWNER,
        "act": {"sub": "mesh-member:m1"},
        "scope": "tools:read",
        "iss": ma.MEMBER_ISSUER,
        "aud": ma.MEMBER_AUDIENCE,
        "iat": now,
        "exp": now + 120,
        "delegation": True,
        "jti": "jti-1",
        "cnf": {"jkt": "thumb"},
        ma.MEMBER_CLAIM: {
            "mesh_id": "mesh-1", "member_id": "m1", "membership_epoch": 1, "revocation_epoch": 0,
            "member_version": 1, "authority_revision": 1, "agent_id": None, "session_incarnation": "inc",
        },
    }
    payload.update(changes)
    return payload


def test_member_tokens_are_signed_by_the_node_and_verified_against_it(personal):
    token = dg.encode_delegation_payload(member_claims(personal))
    claims = ma.verify_member_token(token)
    assert claims["iss"] == personal.node_id
    assert ma.MEMBER_AUDIENCE in claims["aud"] and personal.node_id in claims["aud"]
    assert claims["sub"] == OWNER


def test_member_tokens_from_another_node_or_scheme_are_refused(personal, tmp_path, monkeypatch):
    token = dg.encode_delegation_payload(member_claims(personal))
    other_node(tmp_path, monkeypatch)
    with pytest.raises(ma.MeshAdmissionError):
        ma.verify_member_token(token)
    monkeypatch.delenv(ni.PROFILE_ENV)
    monkeypatch.setenv("ASTRAL_ENV", "development")
    with pytest.raises(ma.MeshAdmissionError):
        ma.verify_member_token(token)


def test_member_token_structure_is_still_enforced(personal):
    for bad in (
        member_claims(personal, scope="admin:everything"),
        member_claims(personal, act={"sub": "mesh-member:other"}),
        member_claims(personal, cnf={}),
        member_claims(personal, delegation=False),
    ):
        with pytest.raises(ma.MeshAdmissionError):
            ma.verify_member_token(dg.encode_delegation_payload(bad))
    with pytest.raises(ma.MeshAdmissionError):
        ma.verify_member_token("garbage")


def test_member_audience_helper_accepts_string_and_list():
    assert ma._has_member_audience({"aud": ma.MEMBER_AUDIENCE})
    assert ma._has_member_audience({"aud": [ma.MEMBER_AUDIENCE, "node"]})
    assert not ma._has_member_audience({"aud": ["other"]})
    assert not ma._has_member_audience({})


async def test_agents_accept_node_delegation_for_their_audience_and_owner_only(personal, tmp_path, monkeypatch):
    from shared.a2a_security import A2ASecurityValidator

    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/a")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("AGENT_SERVICE_CLIENT_ID", raising=False)
    orch = orchestrator()
    socket = object()
    orch.ui_sessions[socket] = {"sub": OWNER}
    token = await orch._get_delegation_token(socket, "agent", OWNER)
    validator = A2ASecurityValidator()
    claims = await validator.validate_token(token)
    assert claims["sub"] == OWNER and validator.is_delegation_token(claims)
    assert validator.get_actor(claims) == "agent:agent"
    assert ni.bound_owner() == OWNER
    wrong_audience = personal.mint_token({"sub": OWNER, "act": {"sub": "agent:agent"}}, audience=["some-other-service"])
    assert await validator.validate_token(wrong_audience) is None
    other_owner = personal.mint_token({"sub": "oidc.0123456789abcdef.intruder"}, audience=[AGENT_AUDIENCE])
    assert await validator.validate_token(other_owner) is None
    strict = A2ASecurityValidator(require_first_party_user=True)
    assert await strict.validate_token(token) is None
    other_node(tmp_path, monkeypatch)
    assert await validator.validate_token(token) is None
