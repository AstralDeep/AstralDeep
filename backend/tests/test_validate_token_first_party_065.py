"""Tests for orchestrator.py's validate_token: ordinary and realm-granted user tokens
are accepted, while MCP-audience, RFC 8693 actor-claim, and delegation-flagged tokens
are refused.
"""

import os

import pytest

from orchestrator.orchestrator import Orchestrator


@pytest.fixture
def orch(server_identity):
    return Orchestrator.__new__(Orchestrator)


@pytest.mark.asyncio
async def test_ordinary_user_token_is_accepted(orch, server_identity):
    claims = await orch.validate_token(server_identity.token("user-1", aud="account"))
    assert claims is not None
    assert claims["sub"] == server_identity.owner("user-1")


# Real user logins share this audience — must not refuse
@pytest.mark.asyncio
async def test_live_realm_user_token_is_accepted(orch, server_identity):
    agent_aud = os.getenv("AGENT_SERVICE_CLIENT_ID", "astral-agent-service")
    token = server_identity.token("user-1", aud=[agent_aud, "realm-management", "account"])
    claims = await orch.validate_token(token)
    assert claims is not None and claims["sub"] == server_identity.owner("user-1")


@pytest.mark.asyncio
async def test_mcp_audience_is_refused(orch, server_identity):
    assert await orch.validate_token(server_identity.token("user-1", aud="astral-mcp")) is None


@pytest.mark.asyncio
async def test_rfc8693_actor_claim_is_refused(orch, server_identity):
    token = server_identity.token("user-1", aud="account", act={"sub": "agent:web-research-1"})
    assert await orch.validate_token(token) is None


@pytest.mark.asyncio
async def test_delegation_flag_is_refused(orch, server_identity):
    assert await orch.validate_token(server_identity.token("user-1", aud="account", delegation=True)) is None


@pytest.mark.asyncio
async def test_unsigned_token_is_refused(orch):
    assert await orch.validate_token("header.e30.signature") is None
