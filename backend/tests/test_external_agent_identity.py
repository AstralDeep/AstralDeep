"""Authorization and transport coverage for identity-bound external agents."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from orchestrator.agent_identity import (
    identity_requirement_satisfied,
    normalize_orcid,
    required_identity_claims,
    verified_identity_for,
)
from orchestrator.mcp_projection import project_tools
from orchestrator.orchestrator import GateRefusal, Orchestrator
from orchestrator.tool_visibility import eligible_tool_pairs
from shared.protocol import AgentCard, AgentSkill, MCPResponse

SAM_ORCID = "0009-0003-6606-0831"
CODY_ORCID = "0000-0001-9588-3501"
PANATLAS_ID = "panatlas-1"


@pytest.fixture(autouse=True)
def _trust_panatlas_identity_projection(monkeypatch):
    monkeypatch.setenv("IDENTITY_CLAIM_TRUSTED_AGENTS", PANATLAS_ID)


def _card(metadata=None) -> AgentCard:
    return AgentCard(
        name="PanAtlas",
        description="atlas",
        agent_id=PANATLAS_ID,
        skills=[AgentSkill(
            name="Summarize",
            description="summarize",
            id="summarize_atlas_state",
            input_schema={"type": "object", "properties": {}},
            scope="tools:read",
        )],
        metadata=metadata or {"required_identity_claims": ["orcid"]},
    )


@pytest.mark.parametrize("value", [SAM_ORCID, CODY_ORCID])
def test_known_orcids_are_canonical_and_checksum_valid(value):
    assert normalize_orcid(value) == value


@pytest.mark.parametrize("value", [
    "0009-0003-6606-0832",
    "https://orcid.org/0009-0003-6606-0831",
    None,
    123,
])
def test_malformed_orcid_claims_are_rejected(value):
    assert normalize_orcid(value) is None
    assert not identity_requirement_satisfied(_card(), {"orcid": value})


def test_projection_includes_only_declared_verified_claims():
    claims = {
        "sub": "keycloak-subject",
        "email": "private@example.test",
        "orcid": SAM_ORCID,
        "_raw_token": "must-never-cross-agent-boundary",
    }
    assert verified_identity_for(_card(), claims) == {"orcid": SAM_ORCID}


def test_saved_external_identity_links_a_verified_astral_subject():
    assert verified_identity_for(
        _card(),
        {
            "sub": "sam-keycloak-subject",
            "_verified_external_identities": {
                "orcid": {
                    "subject": SAM_ORCID,
                    "issuer": "https://orcid.org",
                    "verified_by_agent": PANATLAS_ID,
                }
            },
        },
    ) == {"orcid": SAM_ORCID}


def test_saved_external_identity_is_scoped_to_the_verifying_agent():
    claims = {
        "_verified_external_identities": {
            "orcid": {
                "subject": SAM_ORCID,
                "issuer": "https://orcid.org",
                "verified_by_agent": "different-agent",
            }
        }
    }
    assert verified_identity_for(_card(), claims) is None


def test_untrusted_agent_cannot_opt_itself_into_identity_disclosure(monkeypatch):
    monkeypatch.setenv("IDENTITY_CLAIM_TRUSTED_AGENTS", "some-other-agent")
    assert verified_identity_for(_card(), {"orcid": SAM_ORCID}) is None


@pytest.mark.parametrize("metadata", [
    {"required_identity_claims": "orcid"},
    {"required_identity_claims": []},
    {"required_identity_claims": ["email"]},
    {"required_identity_claims": ["bad claim"]},
])
def test_malformed_or_unsupported_card_requirements_fail_closed(metadata):
    card = _card(metadata)
    assert required_identity_claims(card)
    assert verified_identity_for(card, {"orcid": SAM_ORCID, "email": "x@y.test"}) is None


class _AllowAllPermissions:
    def is_tool_allowed(self, *_args):
        return True


def _visibility_orchestrator(card: AgentCard):
    return SimpleNamespace(
        agent_cards={PANATLAS_ID: card},
        agents={PANATLAS_ID: object()},
        local_agents={},
        security_flags={},
        tool_permissions=_AllowAllPermissions(),
        _is_draft_agent=lambda _agent_id: False,
        history=SimpleNamespace(
            db=SimpleNamespace(get_user_disabled_agents=lambda _user_id: [])
        ),
    )


def test_restricted_tools_are_hidden_without_a_verified_orcid():
    orch = _visibility_orchestrator(_card())
    reasons = []
    assert eligible_tool_pairs(
        orch,
        "sam",
        identity_claims={"sub": "sam"},
        log_exclusion=lambda agent, skill, reason: reasons.append((agent, skill, reason)),
    ) == []
    assert reasons == [(PANATLAS_ID, None, "missing_required_identity")]


def test_restricted_tools_are_visible_with_a_verified_orcid():
    orch = _visibility_orchestrator(_card())
    pairs = eligible_tool_pairs(
        orch,
        "sam",
        identity_claims={"sub": "sam", "orcid": SAM_ORCID},
    )
    assert [(agent, skill.id) for agent, skill in pairs] == [
        (PANATLAS_ID, "summarize_atlas_state")
    ]


def test_external_mcp_projection_uses_the_same_identity_gate():
    orch = _visibility_orchestrator(_card())
    assert project_tools(orch, "sam", {"sub": "sam"}) == ()
    projected = project_tools(
        orch,
        "sam",
        {"sub": "sam", "orcid": SAM_ORCID},
    )
    assert [tool.name for tool in projected] == ["summarize_atlas_state"]


def test_shared_dispatch_gate_denies_a_missing_identity_before_permissions():
    websocket = object()
    orch = SimpleNamespace(
        security_flags={},
        agent_cards={PANATLAS_ID: _card()},
        ui_sessions={websocket: {"sub": "sam"}},
    )
    result = asyncio.run(Orchestrator._run_gate_stack(
        orch,
        websocket,
        PANATLAS_ID,
        "summarize_atlas_state",
        {},
        user_id="sam",
    ))
    assert isinstance(result, GateRefusal)
    assert result.response.error == {
        "message": (
            "PanAtlas requires a linked ORCID iD. Open Agents & permissions, "
            "select this agent, and choose Connect ORCID."
        ),
        "retryable": False,
    }


class _CapturingAgentSocket:
    def __init__(self):
        self.orchestrator = None
        self.payloads = []

    async def send(self, payload: str):
        decoded = json.loads(payload)
        self.payloads.append(decoded)
        future = self.orchestrator.pending_requests[decoded["request_id"]]
        future.set_result(MCPResponse(request_id=decoded["request_id"], result={"ok": True}))


def _dispatch_orchestrator(claims):
    ui_websocket = object()
    agent_socket = _CapturingAgentSocket()
    orch = SimpleNamespace(
        agent_cards={PANATLAS_ID: _card()},
        agents={PANATLAS_ID: agent_socket},
        ui_sessions={ui_websocket: claims},
        pending_requests={},
        _pending_request_agent={},
        pending_ui_sockets={},
        _dispatch_context={},
        _register_dispatch_context=lambda *_args: None,
    )
    agent_socket.orchestrator = orch
    return orch, ui_websocket, agent_socket


def test_websocket_envelope_projects_only_the_verified_orcid():
    orch, ui_websocket, agent_socket = _dispatch_orchestrator({
        "sub": "sam",
        "email": "private@example.test",
        "orcid": SAM_ORCID,
        "_raw_token": "secret-token",
    })
    response = asyncio.run(Orchestrator._execute_via_websocket(
        orch,
        PANATLAS_ID,
        "summarize_atlas_state",
        {},
        ui_websocket=ui_websocket,
    ))
    assert response.error is None
    caller_info = agent_socket.payloads[0]["caller_info"]
    assert caller_info["verified_identity"] == {"orcid": SAM_ORCID}
    serialized = json.dumps(caller_info)
    assert "secret-token" not in serialized
    assert "private@example.test" not in serialized
    assert "keycloak-subject" not in serialized


def test_websocket_dispatch_defense_in_depth_refuses_missing_claim():
    orch, ui_websocket, agent_socket = _dispatch_orchestrator({"sub": "sam"})
    response = asyncio.run(Orchestrator._execute_via_websocket(
        orch,
        PANATLAS_ID,
        "summarize_atlas_state",
        {},
        ui_websocket=ui_websocket,
    ))
    assert response.error["retryable"] is False
    assert "requires a linked ORCID iD" in response.error["message"]
    assert agent_socket.payloads == []
