"""``validate_token`` must refuse tokens minted for a non-user purpose.

A delegated agent token carries the human's ``sub``, the user's realm roles, the
realm ``iss`` and the requesting client's ``azp``, so it satisfies every gate
``validate_token`` applied before this check and ``register_ui`` would promote it
to a full interactive session. The production Keycloak RFC 8693 exchange emits
neither ``act`` nor ``delegation``, so the audience is the only discriminator —
which is why the predicate is a denylist rather than a positive ``aud`` check
(real logins carry ``aud="account"``).
"""

import base64
import json
import os

import pytest

from orchestrator.orchestrator import Orchestrator


def _token(payload: dict) -> str:
    """A JWT-shaped token the mock branch will base64-decode."""
    body = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


_USER = {
    "sub": "user-1",
    "preferred_username": "user-1",
    "realm_access": {"roles": ["user"]},
}


@pytest.fixture
def orch(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    return Orchestrator.__new__(Orchestrator)


@pytest.mark.asyncio
async def test_ordinary_user_token_is_accepted(orch):
    claims = await orch.validate_token(_token(dict(_USER, aud="account")))
    assert claims is not None
    assert claims["sub"] == "user-1"


@pytest.mark.asyncio
async def test_live_realm_user_token_is_accepted(orch):
    """The deployment realm grants the agent-service audience to ordinary
    interactive tokens, so that audience must NOT be treated as a refusal —
    doing so locks every real user out of the WebSocket."""
    agent_aud = os.getenv("AGENT_SERVICE_CLIENT_ID", "astral-agent-service")
    payload = dict(_USER, aud=[agent_aud, "realm-management", "account"])
    claims = await orch.validate_token(_token(payload))
    assert claims is not None and claims["sub"] == "user-1"


@pytest.mark.asyncio
async def test_mcp_audience_is_refused(orch):
    assert await orch.validate_token(_token(dict(_USER, aud="astral-mcp"))) is None


@pytest.mark.asyncio
async def test_rfc8693_actor_claim_is_refused(orch):
    payload = dict(_USER, aud="account", act={"sub": "agent:web-research-1"})
    assert await orch.validate_token(_token(payload)) is None


@pytest.mark.asyncio
async def test_delegation_flag_is_refused(orch):
    payload = dict(_USER, aud="account", delegation=True)
    assert await orch.validate_token(_token(payload)) is None
