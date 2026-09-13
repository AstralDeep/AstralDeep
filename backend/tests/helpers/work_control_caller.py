"""Actual signed caller and exact Plane composition for safe-control PG tests."""
from contextlib import asynccontextmanager
import time
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import FastAPI
from jose import jwt

from orchestrator.work_control_authority import authenticate_work_control_request
from tests.helpers.session_plane_runtime import web_session_store
from tests.test_request_session_authority_088 import request


@asynccontextmanager
async def current_control_caller(assignments, monkeypatch, signing_key):
    """Capture real cookie/Bearer IAM once before each test's adverse mutation.

    Only the JWKS reply is synthetic. No refresh, endpoint or provider runs; the
    cookie row, observation, transaction checks and delivery verification are real.
    """
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEB_SESSION_SECRET", "synthetic-control-cookie-key")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://request-authority.invalid/realm")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-web")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "synthetic-secret")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-native")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.invalid")
    claims = {"sub": "owner", "iss": "https://request-authority.invalid/realm",
              "azp": "astral-web", "aud": "account", "exp": int(time.time()) + 300,
              "realm_access": {"roles": ["user"]}}
    token = jwt.encode(claims, signing_key[0], algorithm="RS256",
                       headers={"kid": "synthetic-request-authority"})

    async def keys(*_args, **_kwargs):
        return signing_key[1]

    async def no_refresh(*_args, **_kwargs):
        raise AssertionError("safe control must not refresh execution authority")

    monkeypatch.setattr("shared.jwks_cache.get_jwks", keys)
    monkeypatch.setattr("orchestrator.web_auth._exchange_session_refresh", no_refresh)
    runtime = assignments.store.plane_runtime
    sessions = web_session_store(runtime)
    sid = uuid4().hex
    sessions.create(sid, user_id="owner", access_token=token,
                    refresh_token="synthetic-control-refresh", hard_max_seconds=3600)
    orch = assignments.orch
    orch.persistent_assignments = assignments
    orch.web_sessions = sessions
    orch.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(runtime=runtime, repositories=runtime.repositories))
    app = FastAPI()
    app.state.orchestrator = orch
    incoming = request(sid, headers=[(b"authorization", ("Bearer " + token).encode()),
        (b"content-type", b"application/json"), (b"origin", b"https://app.invalid")])
    incoming.scope["app"] = app
    try:
        yield await authenticate_work_control_request(
            incoming, assignments=assignments, sessions=sessions)
    finally:
        sessions.delete(sid)
