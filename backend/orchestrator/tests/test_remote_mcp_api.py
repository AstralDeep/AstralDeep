"""Covers the remote MCP OAuth browser start and callback boundaries: owner web-session
gating, binding cookie attributes, generic outcomes, and browser safety headers.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote, urlencode

from starlette.requests import Request

from orchestrator import auth, remote_mcp_api, web_auth
from orchestrator.remote_mcp_oauth import RemoteMCPOAuthError

STATE = "s" * 40
COOKIE = remote_mcp_api._binding_cookie(STATE)


def _request(path, query, cookies=None):
    headers = [(b"host", b"deep.example"), (b"accept", b"text/html")]
    if cookies:
        headers.append((b"cookie", "; ".join(f"{key}={value}" for key, value in cookies.items()).encode()))
    return Request({"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
                    "query_string": urlencode(query).encode(), "headers": headers, "root_path": "",
                    "client": ("127.0.0.1", 1), "server": ("deep.example", 443),
                    "scheme": "https", "http_version": "1.1"})


def _set_cookies(response):
    return [value.decode() for name, value in response.raw_headers if name == b"set-cookie"]


def _sessions(monkeypatch, session, claims=None):
    async def ensure_session(_request):
        return session

    async def payload(_request, credentials):
        assert credentials.credentials == session["access_token"]
        return claims if claims is not None else {"sub": session["sub"], "realm_access": {"roles": ["user"]}}

    monkeypatch.setattr(web_auth, "ensure_session", ensure_session)
    monkeypatch.setattr(web_auth, "aget_session", ensure_session)
    monkeypatch.setattr(auth, "get_current_user_payload", payload)
    monkeypatch.setattr(remote_mcp_api, "_get_orchestrator", lambda request: object())


def _start(query, cookies=None):
    return asyncio.run(remote_mcp_api.remote_mcp_oauth_start(
        _request("/api/remote-mcp/oauth/start", query, cookies), state=query.get("state", "")))


def _callback(query, cookies=None):
    return asyncio.run(remote_mcp_api.remote_mcp_oauth_callback(
        _request("/api/remote-mcp/oauth/callback", query, cookies), **query))


def test_start_without_web_session_sends_browser_to_sign_in_and_drops_other_parameters(monkeypatch):
    _sessions(monkeypatch, None)

    async def bind(*_args):
        raise AssertionError("an unauthenticated browser must not bind the flow")

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.bind_authorization", bind)
    response = _start({"state": STATE, "token": "attacker-bearer"})

    assert response.status_code == 303
    target = "/api/remote-mcp/oauth/start?" + urlencode({"state": STATE})
    assert response.headers["location"] == "/auth/login?next=" + quote(target, safe="")
    assert "attacker-bearer" not in response.headers["location"]
    assert _set_cookies(response) == []
    assert response.headers["cache-control"] == "no-store"


def test_start_binds_owner_session_and_sets_browser_binding_cookie(monkeypatch):
    _sessions(monkeypatch, {"access_token": "owner-token", "sub": "owner-1"})
    calls = []

    async def bind(orch, owner_id, state):
        calls.append((owner_id, state))
        return "https://login.example/authorize?state=" + state, "b" * 43

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.bind_authorization", bind)
    response = _start({"state": STATE})

    assert calls == [("owner-1", STATE)]
    assert response.status_code == 303
    assert response.headers["location"] == "https://login.example/authorize?state=" + STATE
    assert response.headers["referrer-policy"] == "no-referrer"
    [cookie] = _set_cookies(response)
    attributes = {part.strip().lower() for part in cookie.split(";")}
    assert cookie.startswith(f"{COOKIE}={'b' * 43};")
    assert {"httponly", "secure", "samesite=lax", "path=/api/remote-mcp/oauth", "max-age=600"} <= attributes


def test_start_refusals_are_generic_logged_and_set_no_cookie(monkeypatch, caplog):
    async def deny(*_args):
        raise ValueError("private detail")

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.bind_authorization", deny)
    _sessions(monkeypatch, {"access_token": "owner-token", "sub": "owner-1"})
    failed = _start({"state": STATE})
    _sessions(monkeypatch, {"access_token": "guest-token", "sub": "guest"},
              claims={"sub": "guest", "realm_access": {"roles": []}})
    unprivileged = _start({"state": STATE})

    async def broken(_request):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(web_auth, "ensure_session", broken)
    unavailable = _start({"state": STATE})
    assert [record.getMessage() for record in caplog.records] == [
        "Remote MCP OAuth start refused: ValueError", "Remote MCP OAuth start refused: HTTPException",
        "Remote MCP OAuth start refused: RuntimeError"]
    for response in (failed, unprivileged, unavailable):
        assert response.status_code == 400
        assert b"private detail" not in response.body and STATE.encode() not in response.body
        assert _set_cookies(response) == []
        assert response.headers["cache-control"] == "no-store"


def test_callback_passes_browser_binding_and_session_owner_then_clears_cookie(monkeypatch):
    seen = []

    async def complete(orch, code, state, *, binding, session_owner):
        assert orch is not None
        seen.append((code, state, binding, session_owner))

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.complete_authorization", complete)
    _sessions(monkeypatch, None)
    anonymous = _callback({"code": "code", "state": STATE}, {COOKIE: "binding-value"})
    _sessions(monkeypatch, {"access_token": "owner-token", "sub": "owner-1"})
    signed_in = _callback({"code": "code", "state": STATE}, {COOKIE: "binding-value"})
    _sessions(monkeypatch, {"access_token": "odd-token"})
    subjectless = _callback({"code": "code", "state": STATE})

    assert seen == [("code", STATE, "binding-value", None), ("code", STATE, "binding-value", "owner-1"),
                    ("code", STATE, None, "")]
    for response in (anonymous, signed_in, subjectless):
        assert response.status_code == 200
        assert "completed" in response.body.decode()
        assert b"code" not in response.body and STATE.encode() not in response.body
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        [cleared] = _set_cookies(response)
        assert cleared.startswith(COOKIE + '="";') and "max-age=0" in cleared.lower()
        assert "path=/api/remote-mcp/oauth" in cleared.lower()


def test_callback_provider_denial_and_internal_failure_are_generic_logged_and_clear_cookie(monkeypatch, caplog):
    async def unreachable(*_args, **_kwargs):
        raise AssertionError("a provider denial must not reach the exchange")

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.complete_authorization", unreachable)
    denied = _callback({"code": "sensitive-code", "state": STATE, "error": "access_denied"},
                       {COOKIE: "binding-value"})
    assert denied.status_code == 400
    assert b"sensitive" not in denied.body
    assert denied.headers["cache-control"] == "no-store"
    assert _set_cookies(denied)[0].startswith(COOKIE + '="";')

    async def fail(*_args, **_kwargs):
        raise ValueError("secret provider error")

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.complete_authorization", fail)
    _sessions(monkeypatch, None)
    failed = _callback({"code": "sensitive-code", "state": STATE}, {COOKIE: "binding-value"})
    assert failed.status_code == 400
    assert b"secret provider error" not in failed.body
    assert b"sensitive" not in failed.body
    assert failed.headers["referrer-policy"] == "no-referrer"
    assert _set_cookies(failed)[0].startswith(COOKIE + '="";')

    async def unbound(*_args, **_kwargs):
        raise RemoteMCPOAuthError("The OAuth sign-in was not started in this browser.")

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.complete_authorization", unbound)
    assert _callback({"code": "sensitive-code", "state": STATE}).status_code == 400
    assert [record.getMessage() for record in caplog.records] == [
        "Remote MCP OAuth callback refused: ValueError",
        "Remote MCP OAuth callback refused: The OAuth sign-in was not started in this browser."]
    assert "sensitive" not in caplog.text and STATE not in caplog.text


def test_router_round_trip_returns_scoped_binding_cookie_only_to_the_callback(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _sessions(monkeypatch, {"access_token": "owner-token", "sub": "owner-1"})
    seen = []

    async def bind(_orch, _owner_id, state):
        return "https://login.example/authorize?state=" + state, "b" * 43

    async def complete(_orch, code, state, *, binding, session_owner):
        seen.append((code, state, binding, session_owner))

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.bind_authorization", bind)
    monkeypatch.setattr("orchestrator.remote_mcp_oauth.complete_authorization", complete)
    app = FastAPI()
    app.include_router(remote_mcp_api.remote_mcp_router)
    client = TestClient(app, base_url="https://deep.example")

    started = client.get("/api/remote-mcp/oauth/start", params={"state": STATE}, follow_redirects=False)
    assert started.status_code == 303
    assert client.cookies.get(COOKIE) == "b" * 43
    done = client.get("/api/remote-mcp/oauth/callback", params={"code": "code", "state": STATE})

    assert done.status_code == 200
    assert seen == [("code", STATE, "b" * 43, "owner-1")]
    assert client.cookies.get(COOKIE) is None
