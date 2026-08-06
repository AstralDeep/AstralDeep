"""
Tests for USE_MOCK_AUTH — verifies the mock auth path resolves to
user `test_user` with roles [admin, user] across all three validators:

1. Orchestrator.validate_token (WebSocket auth)
2. orchestrator.auth.get_current_user_payload (REST auth dependency)
3. shared.a2a_security.A2ASecurityValidator (A2A auth)

The JWT string below is the exact token baked into
frontend/src/contexts/MockAuthContext.tsx — it must decode to test_user.
If this test fails after you change the frontend token, update both in
lockstep.
"""
import asyncio
import base64
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# JWT baked into frontend/src/contexts/MockAuthContext.tsx
FRONTEND_MOCK_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJyZWFsbV9hY2Nlc3MiOnsicm9sZXMiOlsiYWRtaW4iLCJ1c2VyIl19LCJyZXNvdXJjZV9hY2Nlc3MiOnsiYXN0cmFsLWZyb250ZW5kIjp7InJvbGVzIjpbImFkbWluIiwidXNlciJdfX0sInN1YiI6InRlc3RfdXNlciIsInByZWZlcnJlZF91c2VybmFtZSI6InRlc3RfdXNlciIsImVtYWlsIjoidGVzdF91c2VyQGxvY2FsIn0."
    "fake-signature-ignore"
)


@pytest.fixture
def mock_auth_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    yield


@pytest.fixture
def no_mock_auth_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.delenv("KEYCLOAK_AUTHORITY", raising=False)
    monkeypatch.delenv("KEYCLOAK_CLIENT_ID", raising=False)
    yield


def _assert_test_user(payload: dict):
    assert payload is not None, "payload was None — mock auth rejected token"
    assert payload.get("sub") == "test_user", (
        f"expected sub='test_user', got {payload.get('sub')!r}. "
        "Frontend and backend mock identities have drifted."
    )
    realm_roles = payload.get("realm_access", {}).get("roles", [])
    assert "admin" in realm_roles and "user" in realm_roles, (
        f"mock user must have [admin, user] roles, got {realm_roles}"
    )


def test_frontend_jwt_decodes_to_test_user():
    """Baseline: the token baked into MockAuthContext.tsx decodes to test_user."""
    payload_b64 = FRONTEND_MOCK_JWT.split(".")[1]
    payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(payload_b64))
    assert decoded["sub"] == "test_user"
    assert decoded["preferred_username"] == "test_user"
    assert "admin" in decoded["realm_access"]["roles"]


@pytest.mark.asyncio
async def test_orchestrator_validates_frontend_jwt(mock_auth_env):
    from orchestrator.orchestrator import Orchestrator
    orch = await asyncio.to_thread(Orchestrator)
    payload = await orch.validate_token(FRONTEND_MOCK_JWT)
    _assert_test_user(payload)


@pytest.mark.asyncio
async def test_orchestrator_validates_dev_token(mock_auth_env):
    from orchestrator.orchestrator import Orchestrator
    orch = await asyncio.to_thread(Orchestrator)
    payload = await orch.validate_token("dev-token")
    _assert_test_user(payload)
    assert payload.get("email") == "test_user@local"


@pytest.mark.asyncio
async def test_orchestrator_garbage_token_falls_back_to_test_user(mock_auth_env):
    """Mock auth is permissive by design — garbage tokens map to test_user."""
    from orchestrator.orchestrator import Orchestrator
    orch = await asyncio.to_thread(Orchestrator)
    payload = await orch.validate_token("not-a-jwt-at-all")
    _assert_test_user(payload)


@pytest.mark.asyncio
async def test_orchestrator_rejects_token_when_mock_disabled(no_mock_auth_env):
    """With mock off and no Keycloak config, validate_token returns None."""
    from orchestrator.orchestrator import Orchestrator
    orch = await asyncio.to_thread(Orchestrator)
    payload = await orch.validate_token(FRONTEND_MOCK_JWT)
    assert payload is None, "mock disabled + no Keycloak config must not accept tokens"


@pytest.mark.asyncio
async def test_a2a_security_validator_accepts_frontend_jwt(mock_auth_env):
    from shared.a2a_security import A2ASecurityValidator
    validator = A2ASecurityValidator()
    payload = await validator.validate_token(FRONTEND_MOCK_JWT)
    _assert_test_user(payload)


@pytest.mark.asyncio
async def test_a2a_security_validator_dev_token(mock_auth_env):
    from shared.a2a_security import A2ASecurityValidator
    validator = A2ASecurityValidator()
    payload = await validator.validate_token("dev-token")
    _assert_test_user(payload)


@pytest.mark.asyncio
async def test_a2a_security_validator_empty_token_returns_none(mock_auth_env):
    from shared.a2a_security import A2ASecurityValidator
    validator = A2ASecurityValidator()
    assert await validator.validate_token("") is None
    assert await validator.validate_token(None) is None


def test_rest_auth_dependency_accepts_dev_token(mock_auth_env):
    """The REST dependency used by FastAPI routes must accept dev-token."""
    import asyncio
    from fastapi.security import HTTPAuthorizationCredentials
    from orchestrator.auth import get_current_user_payload

    class _Req:
        method = "GET"
        query_params: dict = {}

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="dev-token")
    payload = asyncio.run(get_current_user_payload(_Req(), creds))
    _assert_test_user(payload)


def test_rest_auth_dependency_rejects_missing_token(mock_auth_env):
    """Mock mode still requires *some* token — no token ⇒ 401."""
    import asyncio
    from fastapi import HTTPException
    from orchestrator.auth import get_current_user_payload

    class _Req:
        method = "GET"
        query_params: dict = {}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_current_user_payload(_Req(), None))
    assert exc.value.status_code == 401


def _mock_jwt(payload: dict) -> str:
    body = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.fake-signature-ignore"


@pytest.mark.parametrize("claims", [
    {"sub": "victim", "act": {"sub": "agent:summarizer-1"}, "delegation": True,
     "aud": "astral-agent-service", "realm_access": {"roles": ["user"]}},
    {"sub": "victim", "aud": ["account", "astral-mcp"],
     "realm_access": {"roles": ["user"]}},
])
def test_rest_auth_dependency_rejects_delegated_token_in_mock_mode(mock_auth_env, claims):
    """Mock mode base64-decodes any token verbatim, so the delegated-token hole
    is REAL in dev, not just a production concern — and a refusal must 401
    rather than fall through to the permissive test_user."""
    import asyncio
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from orchestrator.auth import get_current_user_payload

    class _Req:
        method = "GET"
        query_params: dict = {}

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_mock_jwt(claims))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_current_user_payload(_Req(), creds))
    assert exc.value.status_code == 401
