"""Tests for /api/download auth (orchestrator/auth.py, web_auth.py): Bearer and
query-token paths stay unchanged, a GET-only session-cookie fallback authenticates
via JWKS, and path-traversal/cross-user access stay blocked.
"""

import os
import shutil
import sys

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import orchestrator.web_auth as web_auth  # noqa: E402
from orchestrator.auth import auth_router, require_download_user_id  # noqa: E402

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SESSION_ID = "dl-auth-test-sess"
FILE_NAME = "report.csv"
FILE_BODY = b"a,b\n1,2\n"
CROSS_USER_FILE = "secret.txt"


@pytest.fixture
def mock_auth_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")


@pytest.fixture
def real_auth_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(auth_router)

    captured = {}
    app.state.captured_audit = captured

    @app.middleware("http")
    async def _capture_audit_claims(request, call_next):
        response = await call_next(request)
        captured["claims"] = getattr(request.state, "audit_claims", None)
        return response

    @app.post("/test/download-auth")
    async def _probe(user_id: str = Depends(require_download_user_id)):
        return {"user_id": user_id}

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def user_file():
    session_dirs = []
    for user, name, body in (
        ("test_user", FILE_NAME, FILE_BODY),
        ("other_user", CROSS_USER_FILE, b"SECRET"),
    ):
        d = os.path.join(BACKEND_DIR, "tmp", user, SESSION_ID)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as f:
            f.write(body)
        session_dirs.append(d)
    yield
    for d in session_dirs:
        shutil.rmtree(d, ignore_errors=True)


def _session(token="dev-token", sub="test_user"):
    async def _ensure(request):
        return {"access_token": token, "refresh_token": "", "sub": sub,
                "created_at": 0, "resumed": True, "sid": "test-sid"}
    return _ensure


def test_bearer_token_still_works(mock_auth_env, client, user_file):
    res = client.get(
        f"/api/download/{SESSION_ID}/{FILE_NAME}",
        headers={"Authorization": "Bearer dev-token"},
    )
    assert res.status_code == 200, res.text
    assert res.content == FILE_BODY


def test_query_token_still_works(mock_auth_env, client, user_file):
    res = client.get(f"/api/download/{SESSION_ID}/{FILE_NAME}?token=dev-token")
    assert res.status_code == 200, res.text
    assert res.content == FILE_BODY


def test_bearer_takes_precedence_over_cookie(mock_auth_env, client, user_file, monkeypatch):
    async def _boom(request):
        raise AssertionError("ensure_session must not be called when a Bearer token exists")
    monkeypatch.setattr(web_auth, "ensure_session", _boom)
    res = client.get(
        f"/api/download/{SESSION_ID}/{FILE_NAME}",
        headers={"Authorization": "Bearer dev-token"},
    )
    assert res.status_code == 200, res.text


def test_cookie_session_serves_file(mock_auth_env, client, user_file, monkeypatch):
    monkeypatch.setattr(web_auth, "ensure_session", _session())
    res = client.get(f"/api/download/{SESSION_ID}/{FILE_NAME}")
    assert res.status_code == 200, res.text
    assert res.content == FILE_BODY


def test_cookie_session_sets_audit_claims(mock_auth_env, app, client, user_file, monkeypatch):
    monkeypatch.setattr(web_auth, "ensure_session", _session())
    res = client.get(f"/api/download/{SESSION_ID}/{FILE_NAME}")
    assert res.status_code == 200, res.text
    claims = app.state.captured_audit.get("claims")
    assert claims is not None, "cookie path must set request.state.audit_claims"
    assert claims.get("sub") == "test_user"


def test_absent_session_is_401(mock_auth_env, client, user_file, monkeypatch):
    async def _none(request):
        return None
    monkeypatch.setattr(web_auth, "ensure_session", _none)
    res = client.get(f"/api/download/{SESSION_ID}/{FILE_NAME}")
    assert res.status_code == 401, res.text
    assert res.headers.get("www-authenticate") == "Bearer"


def test_session_resolution_error_is_401(mock_auth_env, client, user_file, monkeypatch):
    async def _explode(request):
        raise RuntimeError("session store unavailable")
    monkeypatch.setattr(web_auth, "ensure_session", _explode)
    res = client.get(f"/api/download/{SESSION_ID}/{FILE_NAME}")
    assert res.status_code == 401, res.text


def test_cookie_token_failing_jwks_validation_is_401(real_auth_env, client, user_file, monkeypatch):
    monkeypatch.setattr(web_auth, "ensure_session", _session(token="not-a-real-jwt"))

    async def _jwks(url, token=None):
        return {"keys": []}
    monkeypatch.setattr("shared.jwks_cache.get_jwks", _jwks)

    res = client.get(f"/api/download/{SESSION_ID}/{FILE_NAME}")
    assert res.status_code == 401, res.text


def test_cookie_token_valid_via_jwks(real_auth_env, client, user_file, monkeypatch):
    monkeypatch.setattr(web_auth, "ensure_session", _session(token="signed.jwt.token"))

    async def _jwks(url, token=None):
        return {"keys": [{"kid": "k"}]}
    monkeypatch.setattr("shared.jwks_cache.get_jwks", _jwks)
    monkeypatch.setattr(
        "jose.jwt.decode",
        lambda token, key, **kw: {"sub": "test_user", "azp": "astral-frontend"},
    )

    res = client.get(f"/api/download/{SESSION_ID}/{FILE_NAME}")
    assert res.status_code == 200, res.text
    assert res.content == FILE_BODY


def test_cookie_fallback_is_get_only(mock_auth_env, client, monkeypatch):
    monkeypatch.setattr(web_auth, "ensure_session", _session())
    res = client.post("/test/download-auth")
    assert res.status_code == 401, res.text


def test_path_traversal_still_403(mock_auth_env, client, user_file):
    res = client.get(
        f"/api/download/{SESSION_ID}/%2e%2e",
        headers={"Authorization": "Bearer dev-token"},
    )
    assert res.status_code == 403, res.text


def test_cross_user_file_still_404(mock_auth_env, client, user_file):
    res = client.get(
        f"/api/download/{SESSION_ID}/{CROSS_USER_FILE}",
        headers={"Authorization": "Bearer dev-token"},
    )
    assert res.status_code == 404, res.text


def test_cookie_path_keeps_user_scoping(mock_auth_env, client, user_file, monkeypatch):
    monkeypatch.setattr(web_auth, "ensure_session", _session())
    res = client.get(f"/api/download/{SESSION_ID}/{CROSS_USER_FILE}")
    assert res.status_code == 404, res.text
