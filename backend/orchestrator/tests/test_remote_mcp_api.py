"""Covers generic OAuth callback outcomes and browser safety headers."""

from __future__ import annotations

import asyncio

from orchestrator import remote_mcp_api


class Request:
    def __init__(self):
        self.scope = {"type": "http", "method": "GET", "path": "/api/remote-mcp/oauth/callback",
                      "raw_path": b"/api/remote-mcp/oauth/callback", "query_string": b"",
                      "headers": [], "client": ("127.0.0.1", 1), "server": ("test", 443),
                      "scheme": "https", "http_version": "1.1"}


def test_callback_success_is_generic_and_has_no_store_headers(monkeypatch):
    async def complete(orch, code, state):
        assert orch is not None
        assert (code, state) == ("code", "state")

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.complete_authorization", complete)
    monkeypatch.setattr(remote_mcp_api, "_get_orchestrator", lambda request: object())
    response = asyncio.run(remote_mcp_api.remote_mcp_oauth_callback(Request(), code="code", state="state"))

    assert response.status_code == 200
    assert "completed" in response.body.decode()
    assert b"code" not in response.body and b"state" not in response.body
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_callback_provider_denial_and_internal_failure_are_generic(monkeypatch):
    denied = asyncio.run(remote_mcp_api.remote_mcp_oauth_callback(
        Request(), code="sensitive-code", state="sensitive-state", error="access_denied"))
    assert denied.status_code == 400
    assert b"sensitive" not in denied.body
    assert denied.headers["cache-control"] == "no-store"

    async def fail(*_args):
        raise ValueError("secret provider error")

    monkeypatch.setattr("orchestrator.remote_mcp_oauth.complete_authorization", fail)
    monkeypatch.setattr(remote_mcp_api, "_get_orchestrator", lambda request: object())
    failed = asyncio.run(remote_mcp_api.remote_mcp_oauth_callback(
        Request(), code="sensitive-code", state="sensitive-state"))
    assert failed.status_code == 400
    assert b"secret provider error" not in failed.body
    assert b"sensitive" not in failed.body
    assert failed.headers["referrer-policy"] == "no-referrer"
