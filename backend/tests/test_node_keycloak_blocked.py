"""Exit criterion for a personal node: after sign-in with the issuer unreachable, verification still works on the last good keys and tool calls still dispatch with locally minted delegation."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared import jwks_cache
from shared import node_identity as ni
from shared.token_verify import TokenRefused, verify_idp_token
from tests.helpers.idp_tokens import AUTHORITY, CLIENT_ID, FakeIssuer

JWKS_URL = f"{AUTHORITY}/protocol/openid-connect/certs"


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", AUTHORITY)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", CLIENT_ID)
    for name in (ni.OWNER_ENV, ni.ISSUERS_ENV, "KEYCLOAK_ALLOWED_AZP", "JWKS_STALE_GRACE_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    ni.reset_cache()
    jwks_cache.clear()
    state = {"now": 1_000_000.0, "up": True}
    monkeypatch.setattr(jwks_cache, "time", types.SimpleNamespace(time=lambda: state["now"]))
    issuer = FakeIssuer()

    async def fetch(url):
        if not state["up"]:
            raise OSError("issuer blocked")
        jwks_cache._cache[url] = {"jwks": issuer.jwks, "fetched_at": state["now"]}
        return issuer.jwks

    monkeypatch.setattr(jwks_cache, "_fetch", fetch)
    yield types.SimpleNamespace(issuer=issuer, clock=state)
    jwks_cache.clear()
    ni.reset_cache()


def orchestrator(owner):
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.ui_sessions = {}
    orch.security_flags = {}
    orch.agent_cards = {"agent": types.SimpleNamespace(skills=[types.SimpleNamespace(id="read")])}
    orch.tool_permissions = types.SimpleNamespace(
        is_tool_allowed=MagicMock(return_value=True),
        get_tool_scope=MagicMock(return_value="tools:read"),
    )
    orch.delegation = types.SimpleNamespace(
        agent_service_client_id="astral-agent-service",
        exchange_token_for_agent=AsyncMock(side_effect=AssertionError("Keycloak must not be asked")),
    )
    return orch


async def test_session_keeps_working_and_tools_dispatch_with_the_issuer_blocked(node):
    from orchestrator.orchestrator import Orchestrator

    token = node.issuer.token("owner-1")
    claims = await Orchestrator.validate_token(object(), token)
    owner = claims["sub"]
    orch = orchestrator(owner)
    socket = object()
    orch.ui_sessions[socket] = dict(claims)

    node.clock["up"] = False
    node.clock["now"] += jwks_cache._TTL_SECONDS + 60

    again = await Orchestrator.validate_token(object(), token)
    assert again is not None and again["sub"] == owner
    delegation = await orch._get_delegation_token(socket, "agent", owner)
    assert delegation is not None
    verified = ni.load_node_identity().verify_token(delegation)
    assert verified["sub"] == owner and "tool:read" in verified["scope"].split()
    orch.delegation.exchange_token_for_agent.assert_not_awaited()


async def test_verification_stops_when_the_grace_runs_out(node):
    token = node.issuer.token("owner-1")
    await verify_idp_token(token)
    node.clock["up"] = False
    node.clock["now"] += jwks_cache._TTL_SECONDS + jwks_cache._DEFAULT_GRACE_SECONDS + 1
    with pytest.raises(TokenRefused):
        await verify_idp_token(token)


async def test_a_new_sign_in_needs_the_issuer(node):
    node.clock["up"] = False
    with pytest.raises(TokenRefused):
        await verify_idp_token(node.issuer.token("owner-1"))
