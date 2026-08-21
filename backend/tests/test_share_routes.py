"""Feature 055 (US5, T044) — share REST routes.

``POST/GET /api/share``, ``DELETE /api/share/{id}`` and the PUBLIC
``GET /share/{token}`` behind ``FF_ARTIFACT_SHARING`` (default OFF,
fail-closed):

* flag off ⇒ every route 404s with FastAPI's route-absent body;
* mint returns ``{id, share_url, created_at, expires_at}`` exactly once —
  no separate token field, no token material in the owner listing;
* the public serve needs NO auth, returns the mint-time snapshot verbatim
  with the contract's noindex / no-store / no-referrer / CSP headers;
* revoke is owner-scoped, idempotent, and immediate (the next public open
  refuses with the uniform 404);
* the PHI gate refusal maps to 403 ``{error: "phi_blocked"}``.

Routes run over a real FastAPI app + TestClient against the REAL
``ShareGrantStore`` and live Postgres ``share_grant`` table (the store's
methods keep all DB work off the event loop, so LOOP_GUARD_ENFORCE=1 holds);
the orchestrator is mocked only as the snapshot source. Each test user is
uuid-unique and purges its own grant rows on teardown.
"""
from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ["USE_MOCK_AUTH"] = "true"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import orchestrator.web_auth as web_auth  # noqa: E402
from orchestrator.api import share_router  # noqa: E402
from orchestrator.artifact_share import (  # noqa: E402
    ShareGrantStore,
    set_share_store,
)
from personalization.phi_gate import PHIGate, set_phi_gate  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_auth(monkeypatch):
    """Keep this module independent of collection-time environment writes."""

    monkeypatch.setenv("USE_MOCK_AUTH", "true")


class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


class _HitAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return [{"entity_type": "PERSON"}]


CHAT_ID = "chat-share-routes"
COMPONENT = {
    "type": "card", "component_id": "wc_shared", "title": "Quarterly revenue",
    "content": "Up and to the right", "provenance": "grounded",
}


def _make_mock_token(payload: dict) -> str:
    import base64
    import json as _json
    body = base64.b64encode(_json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {_make_mock_token({'sub': user_id})}"}


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("share_routes") as runtime:
        yield runtime


@pytest.fixture()
def user(plane_runtime):
    uid = f"pytest-shareroutes-{uuid.uuid4().hex[:12]}"
    yield uid
    with plane_runtime.transaction() as transaction:
        transaction.execute("DELETE FROM share_grant WHERE user_id = %s", (uid,))


@pytest.fixture(autouse=True)
def real_store(plane_runtime):
    set_share_store(
        ShareGrantStore(
            plane_runtime=plane_runtime,
            plane_repositories=plane_runtime.repositories,
        )
    )
    yield
    set_share_store(None)


@pytest.fixture(autouse=True)
def clean_phi_gate():
    set_phi_gate(PHIGate(analyzer=_CleanAnalyzer(), build_if_missing=False))
    yield
    set_phi_gate(None)


@pytest.fixture()
def sharing_on():
    prior = flags._flags.get("artifact_sharing")
    flags._flags["artifact_sharing"] = True
    yield
    flags._flags["artifact_sharing"] = prior


@pytest.fixture()
def orch():
    m = MagicMock()
    m.workspace = MagicMock()
    m.workspace.aget_by_component_id = AsyncMock(
        return_value={"chat_id": CHAT_ID, "component_id": "wc_shared",
                      "component_data": dict(COMPONENT)})
    m._canvas_components = MagicMock(return_value=[dict(COMPONENT)])
    return m


@pytest.fixture()
def client(orch):
    app = FastAPI()
    app.include_router(share_router)
    app.state.orchestrator = orch
    return TestClient(app)


def _mint(client, user_id, scope="component", **over):
    body = {"chat_id": CHAT_ID, "scope": scope}
    if scope == "component":
        body["component_id"] = "wc_shared"
    body.update(over)
    return client.post("/api/share", json=body, headers=_auth(user_id))


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _grant_rows(plane_runtime, user_id):
    with plane_runtime.transaction() as transaction:
        rows = transaction.fetch_all(
            "SELECT * FROM share_grant WHERE user_id = %s ORDER BY id ASC",
            (user_id,),
        )
    return [_plain(row) for row in rows]


# ---------------------------------------------------------------------------
# Flag off — fail-closed 404 everywhere
# ---------------------------------------------------------------------------


def test_flag_off_all_routes_404(plane_runtime, client, user):
    prior = flags._flags.get("artifact_sharing")
    flags._flags["artifact_sharing"] = False
    try:
        absent = {"detail": "Not Found"}
        r = _mint(client, user)
        assert (r.status_code, r.json()) == (404, absent)
        r = client.get("/api/share", headers=_auth(user))
        assert (r.status_code, r.json()) == (404, absent)
        r = client.delete("/api/share/1", headers=_auth(user))
        assert (r.status_code, r.json()) == (404, absent)
        r = client.get("/share/any-token-at-all")
        assert (r.status_code, r.json()) == (404, absent)
        assert _grant_rows(plane_runtime, user) == []
    finally:
        flags._flags["artifact_sharing"] = prior


# ---------------------------------------------------------------------------
# Mint + public serve
# ---------------------------------------------------------------------------


def test_mint_returns_url_once_and_serves_unauthenticated(
    plane_runtime, client, user, sharing_on
):
    r = _mint(client, user)
    assert r.status_code == 201
    body = r.json()
    # The raw token appears exactly once, inside share_url — no token field.
    assert set(body) == {"id", "share_url", "created_at", "expires_at"}
    assert body["share_url"].startswith("/share/")

    # PUBLIC serve: no Authorization header at all.
    pub = client.get(body["share_url"])
    assert pub.status_code == 200
    assert pub.headers["content-type"].startswith("text/html")
    assert pub.text.startswith("<!DOCTYPE html>")
    assert "Quarterly revenue" in pub.text
    assert "<script" not in pub.text
    # Contract headers (rest-endpoints.md §GET /share/{token}).
    assert pub.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert pub.headers["Cache-Control"] == "no-store"
    assert pub.headers["Referrer-Policy"] == "no-referrer"
    assert pub.headers["Content-Security-Policy"] == \
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:"

    row = _grant_rows(plane_runtime, user)[0]
    assert row["open_count"] == 1
    assert row["scope"] == "component" and row["component_id"] == "wc_shared"


def test_serve_is_the_mint_time_snapshot_not_live(client, orch, user, sharing_on):
    r = _mint(client, user)
    # The workspace changes after mint — the link must keep serving the snapshot.
    orch.workspace.aget_by_component_id.return_value = None
    pub = client.get(r.json()["share_url"])
    assert pub.status_code == 200
    assert "Quarterly revenue" in pub.text


def test_canvas_scope_mint_and_serve(plane_runtime, client, user, sharing_on):
    r = _mint(client, user, scope="canvas", component_id=None)
    assert r.status_code == 201
    pub = client.get(r.json()["share_url"])
    assert pub.status_code == 200
    assert "Quarterly revenue" in pub.text
    assert _grant_rows(plane_runtime, user)[0]["scope"] == "canvas"


def test_unknown_token_uniform_404(client, user, sharing_on):
    r = client.get("/share/definitely-not-a-token")
    assert (r.status_code, r.json()) == (404, {"detail": "Not Found"})


# ---------------------------------------------------------------------------
# Revoke — immediate, owner-scoped, idempotent
# ---------------------------------------------------------------------------


def test_revoke_immediately_stops_public_serving(client, user, sharing_on):
    minted = _mint(client, user).json()
    assert client.get(minted["share_url"]).status_code == 200

    r = client.delete(f"/api/share/{minted['id']}", headers=_auth(user))
    assert r.status_code == 200
    # The very next public open refuses with the uniform body.
    pub = client.get(minted["share_url"])
    assert (pub.status_code, pub.json()) == (404, {"detail": "Not Found"})

    # Idempotent second revoke; unknown id → 404.
    assert client.delete(f"/api/share/{minted['id']}", headers=_auth(user)).status_code == 200
    assert client.delete("/api/share/999999999", headers=_auth(user)).status_code == 404


def test_stranger_cannot_revoke(client, user, sharing_on):
    minted = _mint(client, user).json()
    stranger = f"pytest-shareroutes-{uuid.uuid4().hex[:12]}"
    r = client.delete(f"/api/share/{minted['id']}", headers=_auth(stranger))
    assert r.status_code == 404
    assert client.get(minted["share_url"]).status_code == 200


# ---------------------------------------------------------------------------
# Owner listing
# ---------------------------------------------------------------------------


def test_list_owner_metadata_never_token_material(client, user, sharing_on):
    a = _mint(client, user).json()
    b = _mint(client, user, scope="canvas", component_id=None).json()

    r = client.get("/api/share", headers=_auth(user))
    assert r.status_code == 200
    shares = r.json()["shares"]
    assert [s["id"] for s in shares] == sorted([a["id"], b["id"]], reverse=True)
    for s in shares:
        assert "token_sha256" not in s
        assert "snapshot_html" not in s and "snapshot_json" not in s
    # Neither raw token ever appears in the listing payload.
    for minted in (a, b):
        assert minted["share_url"].split("/share/")[1] not in r.text


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_phi_refusal_is_403_phi_blocked(plane_runtime, client, user, sharing_on):
    set_phi_gate(PHIGate(analyzer=_HitAnalyzer(), build_if_missing=False))
    r = _mint(client, user)
    assert r.status_code == 403
    assert r.json() == {"error": "phi_blocked"}
    assert _grant_rows(plane_runtime, user) == []


def test_invalid_scope_and_missing_component_id_are_422(client, user, sharing_on):
    r = _mint(client, user, scope="everything")
    assert r.status_code == 422 and r.json()["error"] == "invalid_scope"
    r = _mint(client, user, component_id=None)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


def test_component_not_found_is_404(client, orch, user, sharing_on):
    orch.workspace.aget_by_component_id.return_value = None
    assert _mint(client, user).status_code == 404


def test_empty_canvas_is_404(client, orch, user, sharing_on):
    orch._canvas_components.return_value = []
    assert _mint(client, user, scope="canvas", component_id=None).status_code == 404


def _no_session(monkeypatch):
    """No astral_session cookie resolves (real deployments without a login)."""
    async def _none(request):
        return None
    monkeypatch.setattr(web_auth, "ensure_session", _none)


def test_api_routes_require_auth(client, user, sharing_on, monkeypatch):
    _no_session(monkeypatch)
    assert client.post("/api/share", json={"chat_id": CHAT_ID, "scope": "canvas"}).status_code == 401
    assert client.get("/api/share").status_code == 401
    assert client.delete("/api/share/1").status_code == 401


def test_unauthenticated_browser_navigation_redirects(client, sharing_on, monkeypatch):
    """GET navigation (Accept prefers text/html) with no session: 302 to
    login; non-GET stays 401 even when the browser asks for HTML."""
    _no_session(monkeypatch)
    accept = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    r = client.get("/api/share", headers=accept, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/login?next=%2Fapi%2Fshare"
    r = client.post("/api/share", json={"chat_id": CHAT_ID, "scope": "canvas"},
                    headers=accept, follow_redirects=False)
    assert r.status_code == 401


def test_cookie_session_mock_mode_mints_and_lists(plane_runtime, client, sharing_on):
    """No Authorization header at all — the astral_session cookie path
    (USE_MOCK_AUTH=true makes ensure_session return the test_user session)."""
    r = client.post("/api/share", json={"chat_id": CHAT_ID, "scope": "component",
                                        "component_id": "wc_shared"})
    try:
        assert r.status_code == 201
        minted_id = r.json()["id"]
        rows = _grant_rows(plane_runtime, "test_user")
        assert minted_id in [row["id"] for row in rows]
        listed = client.get("/api/share")
        assert listed.status_code == 200
        assert minted_id in [s["id"] for s in listed.json()["shares"]]
    finally:
        # Delete exactly the cookie-session grant without broad fixture cleanup.
        if r.status_code == 201:
            with plane_runtime.transaction() as transaction:
                transaction.execute(
                    "DELETE FROM share_grant WHERE id = %s",
                    (r.json()["id"],),
                )


def test_cookie_session_real_mode_mints(
    plane_runtime, client, user, sharing_on, monkeypatch
):
    """Non-mock: the faked session's access token flows through the SAME JWKS
    verification path as a Bearer token (test_download_auth.py pattern)."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")

    async def _sess(request):
        return {"access_token": "signed.jwt.token", "refresh_token": "",
                "sub": user, "created_at": 0, "resumed": True, "sid": "s"}
    monkeypatch.setattr(web_auth, "ensure_session", _sess)

    async def _jwks(url, token=None):
        return {"keys": [{"kid": "k"}]}
    monkeypatch.setattr("shared.jwks_cache.get_jwks", _jwks)
    monkeypatch.setattr(
        "jose.jwt.decode",
        lambda token, key, **kw: {"sub": user, "azp": "astral-frontend"},
    )

    r = client.post("/api/share", json={"chat_id": CHAT_ID, "scope": "component",
                                        "component_id": "wc_shared"})
    assert r.status_code == 201
    assert _grant_rows(plane_runtime, user)[0]["id"] == r.json()["id"]
