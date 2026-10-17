"""Checks bounded resource projection and the signed JWT denial boundary.
Real framework authority, owner isolation, PHI, and HTTP delivery are covered by PostgreSQL resource tests.
"""

import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from orchestrator.mcp_authz import MCPAuthError
from orchestrator.mcp_server_endpoint import (
    _WORK_RESOURCE_URI_PATTERN,
    create_mcp_router,
    list_resources,
    read_resource,
)
from orchestrator.mcp_work_resources import bounded_status as _build_bounded_view_model
from orchestrator.mcp_work_resources import bounded_result
from tests.test_mcp_endpoint_064 import _Orchestrator as AdmissionOrchestrator
from tests.test_request_session_authority_088 import signing_key as signing_key

OWNER_ID = "owner-2f1c"
WORK_ID = "3f8a1c2e-9b4d-4f7a-8c1e-2d5b6a7c8e9f"


def _record(**changes):
    value = {
        "id": WORK_ID, "revision": 4, "title": "Nightly digest", "kind": "chat",
        "disposition": "active", "lifecycle": "active", "phase": "executing",
        "created_at": "2026-10-15T00:00:00+00:00", "updated_at": "2026-10-15T00:05:00+00:00",
        "owner_id": "must-not-leak", "usage": {"private": True},
    }
    return {**value, **changes}


def test_status_uri_round_trips():
    match = _WORK_RESOURCE_URI_PATTERN.fullmatch(f"astral://work/{WORK_ID}/status")
    assert match is not None and match.group(1) == WORK_ID


@pytest.mark.parametrize("uri", [
    "astral://work/work-1/status", f"astral://work/{WORK_ID}/artifacts",
    f"astral://work/{WORK_ID}/metadata", f"astral://artifact/{WORK_ID}",
    f"astral://work/{WORK_ID}", f"astral://work/{WORK_ID}/status/../../etc/passwd",
    "astral://work/../../etc/passwd/status", f"astral://work/{WORK_ID}/status\n", "", None,
])
def test_unsupported_uri_is_rejected_before_read(uri):
    with pytest.raises(MCPAuthError) as error:
        asyncio.run(read_resource(object(), OWNER_ID, uri, {}))
    assert error.value.status_code == 400


@pytest.mark.parametrize("claims", [{}, {"sub": OWNER_ID}, {"_framework_caller": object()}])
def test_no_supported_framework_authority_fails_closed(claims):
    with pytest.raises(MCPAuthError) as error:
        asyncio.run(list_resources(SimpleNamespace(), OWNER_ID, claims))
    assert error.value.status_code == 403


def test_projection_is_a_scalar_allowlist():
    view = _build_bounded_view_model(_record())
    assert view == {
        "id": WORK_ID, "revision": 4, "title": "Nightly digest", "kind": "chat",
        "disposition": "active", "lifecycle": "active", "phase": "executing",
        "createdAt": "2026-10-15T00:00:00+00:00", "updatedAt": "2026-10-15T00:05:00+00:00",
    }
    assert "must-not-leak" not in json.dumps(view)


@pytest.mark.parametrize("change", [
    {"id": "work-1"}, {"id": None}, {"revision": True}, {"revision": 0},
    {"revision": 2**63}, {"kind": "internal"}, {"kind": {"private": "nested"}},
    {"title": ""}, {"title": {"private": "nested"}}, {"title": "x" * 513},
    {"title": "private\ntext"}, {"title": "\ud800"}, {"phase": ["private"]},
    {"lifecycle": None}, {"disposition": False}, {"created_at": "bad-date"},
    {"created_at": "2026-10-15T00:00:00"}, {"updated_at": None},
])
def test_malformed_view_is_unavailable(change):
    with pytest.raises(MCPAuthError) as error:
        _build_bounded_view_model(_record(**change))
    assert error.value.status_code == 503


@pytest.mark.parametrize("value", [None, [], "private"])
def test_non_record_view_is_unavailable(value):
    with pytest.raises(MCPAuthError):
        _build_bounded_view_model(value)


def _retained_result():
    return {"id": WORK_ID, "revision": 4, "result": {
        "version": 1, "available": True, "reason": None, "content": {
            "version": 1, "scope": "one_page_excerpts", "disposition": "evidence",
            "source": {"requested_url": "https://source.invalid/private?token=hidden",
                       "final_url": "https://source.invalid/private", "title": "Public release",
                       "retrieved_at": "2026-10-16T00:00:00+00:00", "media_type": "text/plain",
                       "extraction_profile": "plain_text_v1", "body_complete": True,
                       "extraction_complete": True, "excerpt_complete": True, "redacted": False,
                       "action_id": WORK_ID, "result_digest": "a" * 64, "revision_digest": "b" * 64},
            "passages": [{"id": "p001", "text": "Release summary.\nSecond line."}],
        },
    }}


def test_result_projection_carries_text_and_receipt_identity_without_links():
    value = _retained_result()
    projected = bounded_result(value)
    assert projected["result"]["content"]["passages"] == value["result"]["content"]["passages"]
    assert set(projected["result"]["content"]["source"]) == {"title", "action_id", "result_digest", "revision_digest"}
    assert "hidden" not in json.dumps(projected) and "source.invalid" not in json.dumps(projected)
    assert projected["result"]["content"]["source"] is not value["result"]["content"]["source"]
    projected["result"]["content"]["passages"][0]["text"] = "Changed projection"
    assert value["result"]["content"]["passages"][0]["text"] == "Release summary.\nSecond line."


@pytest.mark.parametrize("reason", ["unsupported", "not_completed", "not_retained", "unavailable"])
def test_result_projection_preserves_unavailable_reasons(reason):
    value = _retained_result()
    value["result"] = {"version": 1, "available": False, "reason": reason, "content": None}
    assert bounded_result(value) == value


def test_empty_selection_is_insufficient_evidence():
    value = _retained_result()
    value["result"]["content"].update(disposition="insufficient_evidence", passages=[])
    value["result"]["content"]["source"]["title"] = ""
    assert bounded_result(value)["result"]["content"]["passages"] == []


@pytest.mark.parametrize("path,replacement", [
    ((), None), ((), []), (("id",), "foreign"), (("revision",), True), (("revision",), 0),
    (("result",), None), (("result", "version"), True), (("result", "available"), 1),
    (("result", "available"), False), (("result", "reason"), "private-error"),
    (("result", "content"), None), (("result", "content", "version"), True),
    (("result", "content", "scope"), "generic_html"), (("result", "content", "disposition"), "succeeded"),
    (("result", "content", "source"), []), (("result", "content", "source", "title"), "x" * 513),
    (("result", "content", "source", "title"), "\ud800"),
    (("result", "content", "source", "action_id"), "unbound"),
    (("result", "content", "source", "result_digest"), "bad"),
    (("result", "content", "passages"), {}), (("result", "content", "passages"), []),
    (("result", "content", "passages"), [{"id": "p001", "text": "x"}] * 9),
    (("result", "content", "passages"), [None]),
    (("result", "content", "passages"), [{"id": "p001", "text": "x", "html": "<script>private</script>"}]),
    (("result", "content", "passages"), [{"id": "p001", "text": "x"}] * 2),
    (("result", "content", "passages"), [{"id": "bad", "text": "x"}]),
    (("result", "content", "passages"), [{"id": "p001", "text": " "}]),
    (("result", "content", "passages"), [{"id": "p001", "text": "x" * 513}]),
    (("result", "content", "passages"), [{"id": "p001", "text": "private\x00value"}]),
    (("result", "content", "passages"), [{"id": "p001", "text": "\ud800"}]),
])
def test_malformed_result_projection_never_passes_unknown_data(path, replacement):
    value = _retained_result()
    if not path:
        value = replacement
    else:
        parent = value
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = deepcopy(replacement)
    with pytest.raises(MCPAuthError) as error:
        bounded_result(value)
    assert error.value.status_code == 503


@pytest.mark.parametrize("path", [(), ("result",), ("result", "content"), ("result", "content", "source")])
def test_result_projection_rejects_unrecognized_fields(path):
    value = _retained_result()
    parent = value
    for key in path:
        parent = parent[key]
    parent["private_html"] = "<script>do_not_expose()</script>"
    with pytest.raises(MCPAuthError):
        bounded_result(value)


@pytest.mark.parametrize("reason,content", [("private-internal-error", None), ("unavailable", {"private": "value"})])
def test_unavailable_result_never_carries_private_error_or_content(reason, content):
    value = _retained_result()
    value["result"].update(available=False, reason=reason, content=content)
    with pytest.raises(MCPAuthError):
        bounded_result(value)


@pytest.mark.parametrize("enabled", [True, False])
def test_resource_capability_tracks_existing_mcp_gate(monkeypatch, enabled):
    from orchestrator.mcp_server_endpoint import _discover_result
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "mcp_server", enabled)
    capabilities = _discover_result()["capabilities"]
    assert capabilities == ({"tools": {}, "resources": {}} if enabled else {"tools": {}})


def resource_request(method, token):
    params = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {}}}
    if method == "resources/read":
        params["uri"] = f"astral://work/{WORK_ID}/status"
    return {"json": {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            "headers": {"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2026-07-28",
                        "Mcp-Method": method}}


@pytest.mark.parametrize("method", ["resources/list", "resources/read"])
def test_verified_mcp_jwt_does_not_manufacture_framework_authority(monkeypatch, signing_key, method):
    from orchestrator import mcp_authz

    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    authority = "https://synthetic-iam.invalid/realm"
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", authority)
    claims = {"sub": OWNER_ID, "iss": authority, "aud": "astral-mcp", "exp": int(time.time()) + 300,
              "scope": "mcp:tools:read", "realm_access": {"roles": ["user"]}}
    token = jwt.encode(claims, signing_key[0], algorithm="RS256", headers={"kid": "synthetic-request-authority"})
    requested = []

    async def keys(url, *, token):
        requested.append(url)
        return signing_key[1]

    monkeypatch.setattr(mcp_authz, "get_jwks", keys)
    orch = AdmissionOrchestrator()
    app = FastAPI()
    app.include_router(create_mcp_router(orch, public_base_url="https://mcp.invalid"))
    response = TestClient(app).post("/mcp", **resource_request(method, token))
    assert response.status_code == 403
    assert requested == [f"{authority}/protocol/openid-connect/certs"]
    assert "result" not in response.json()
    assert orch.calls == []
