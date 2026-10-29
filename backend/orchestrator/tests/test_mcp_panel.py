"""Checks the default-off external-host Work panel view models, metadata, and URI scope,
including the fail-closed panel branches of the Work resource authority.
Real framework authority, owner isolation, PHI, and HTTP delivery are covered by the
PostgreSQL resource tests.
"""

import asyncio
import json
from types import SimpleNamespace
import types
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.mcp_authz import MCPAuthError
from orchestrator.mcp_panel import (
    PANEL_KIND,
    PANEL_RESOURCE_META,
    panel_descriptor,
    panel_enabled,
    panel_read_result,
    panel_state,
    panel_view,
)
from orchestrator.mcp_server_endpoint import create_mcp_router
from orchestrator.mcp_work_resources import WorkResourceAuthority, resource_scope
from shared.feature_flags import flags
from tests.test_mcp_endpoint_064 import META, _Orchestrator

OWNER_ID = "owner-2f1c"
WORK_ID = "3f8a1c2e-9b4d-4f7a-8c1e-2d5b6a7c8e9f"
PANEL_URI = f"astral://work/{WORK_ID}/panel"
STATUS_URI = f"astral://work/{WORK_ID}/status"


def _record(**changes):
    value = {
        "id": WORK_ID, "revision": 4, "title": "Nightly digest", "kind": "chat",
        "disposition": "active", "lifecycle": "active", "phase": "executing",
        "created_at": "2026-10-15T00:00:00+00:00", "updated_at": "2026-10-15T00:05:00+00:00",
        "owner_id": "must-not-leak", "usage": {"private": True}, "safe_error_code": None,
    }
    return {**value, **changes}


@pytest.mark.parametrize("server,panel,expected", [
    (False, False, False), (False, True, False), (True, False, False), (True, True, True),
])
def test_panel_enabled_requires_both_flags(monkeypatch, server, panel, expected):
    monkeypatch.setitem(flags._flags, "mcp_server", server)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", panel)
    assert panel_enabled() is expected


@pytest.mark.parametrize("disposition,state", [
    ("queued", "waiting"), ("active", "running"), ("awaiting_event", "running"),
    ("retry_eligible", "running"), ("paused", "running"), ("budget_blocked", "running"),
    ("awaiting_approval", "input_required"), ("awaiting_authority", "input_required"),
    ("reconciliation_required", "input_required"), ("completed", "succeeded"),
    ("failed", "failed"), ("cancelled", "cancelled"), ("unsupported_version", "cancelled"),
])
def test_every_disposition_maps_to_a_truthful_state(disposition, state):
    assert panel_state(disposition) == state
    assert panel_view(_record(disposition=disposition))["state"] == state


def test_view_is_bounded_and_never_carries_private_record_fields():
    view = panel_view(_record())
    assert set(view) == {"kind", "state", "title", "detail"}
    assert view["kind"] == PANEL_KIND
    blob = json.dumps(view)
    assert "must-not-leak" not in blob and "private" not in blob
    assert "http" not in blob and "token" not in blob and "Bearer" not in blob


def test_failed_view_carries_only_the_safe_error_code():
    view = panel_view(_record(disposition="failed", safe_error_code="assignment_failed"))
    assert view["status_message"] == "assignment_failed"
    assert panel_view(_record(disposition="failed")) == {
        "kind": PANEL_KIND, "state": "failed", "title": "Nightly digest", "detail": "executing",
    }


def test_safe_error_code_is_omitted_outside_failure():
    view = panel_view(_record(safe_error_code="assignment_failed"))
    assert "status_message" not in view


def test_long_title_is_truncated_to_the_panel_bound():
    assert len(panel_view(_record(title="x" * 512))["title"]) == 200
    assert len(panel_view(_record(phase="y" * 900))["detail"]) == 500


@pytest.mark.parametrize("change", [
    {"title": ""}, {"title": "private\ntext"}, {"title": "\ud800"}, {"title": None},
    {"phase": "\ud800"}, {"disposition": "mystery"}, {"disposition": None},
])
def test_unmappable_records_fail_closed(change):
    with pytest.raises(ValueError):
        panel_view(_record(**change))


@pytest.mark.parametrize("value", [None, [], "record", 7])
def test_non_record_views_are_unavailable(value):
    with pytest.raises(ValueError):
        panel_view(value)


def test_panel_descriptor_shape_and_meta_isolation():
    descriptor = panel_descriptor(WORK_ID, "Nightly digest")
    assert descriptor == {
        "uri": PANEL_URI, "name": "Panel: Nightly digest", "mimeType": "application/json",
        "description": "External-host panel view for this work item",
        "_meta": PANEL_RESOURCE_META,
    }
    descriptor["_meta"]["astral/panelKind"] = "mutated"
    assert PANEL_RESOURCE_META["astral/panelKind"] == PANEL_KIND
    assert panel_descriptor(WORK_ID, "")["name"] == "Panel: Panel"


def test_panel_read_result_keeps_the_view_and_a_meta_copy():
    view = panel_view(_record())
    result = panel_read_result(PANEL_URI, WORK_ID, view)
    assert result["boundedViewModel"] is view
    assert result["_meta"] == PANEL_RESOURCE_META
    assert result["_meta"] is not PANEL_RESOURCE_META


def test_panel_uri_maps_to_the_status_scope():
    assert resource_scope(PANEL_URI) == "operations.read"
    assert resource_scope(STATUS_URI) == "operations.read"
    assert resource_scope(f"astral://work/{WORK_ID}/result") == "artifacts.read"


@pytest.mark.parametrize("uri", [
    f"astral://work/{WORK_ID}/panels", f"astral://work/{WORK_ID}/panel/extra",
    f"astral://work/{WORK_ID}/PANEL", f"astral://work/{WORK_ID}/Panel",
    "astral://work/not-a-uuid/panel", f"astral://work/{WORK_ID}/panel?x=1",
    "ui://astralprojection/astral-work-status", f"astral://work/{WORK_ID}", None,
])
def test_unsupported_panel_uri_shapes_are_rejected(uri):
    with pytest.raises(MCPAuthError) as error:
        resource_scope(uri)
    assert error.value.status_code == 400


class _Reader:
    def __init__(self, scope="operations.read"):
        self.scope = scope
        self.rechecked = False

    async def recheck(self):
        self.rechecked = True


class _Gate:
    def contains_phi_for_sharing(self, payload):
        return False


def _authority(record, *, scope="operations.read", service=None, gate=None):
    authority = SimpleNamespace(
        reader=_Reader(scope), caller=SimpleNamespace(owner_id=OWNER_ID),
        gate=gate or _Gate(),
        service=service or SimpleNamespace(
            get=AsyncMock(return_value=record), result=AsyncMock(return_value=record),
        ),
    )
    authority._bound = lambda: None
    authority.recheck = types.MethodType(WorkResourceAuthority.recheck, authority)
    authority._share = types.MethodType(WorkResourceAuthority._share, authority)
    return authority


@pytest.mark.parametrize("disposition", ["active", "queued", "completed", "failed", "cancelled"])
def test_panel_read_serves_the_bounded_view(monkeypatch, disposition):
    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", True)
    authority = _authority(_record(disposition=disposition))
    resource = asyncio.run(WorkResourceAuthority.read(authority, PANEL_URI))
    assert resource["uri"] == PANEL_URI
    assert resource["mimeType"] == "application/json"
    assert resource["boundedViewModel"]["state"] == panel_state(disposition)
    assert resource["_meta"]["astral/dataFlow"] == "owner-scoped; no third-party data flow"
    assert "must-not-leak" not in json.dumps(resource)
    assert authority.reader.rechecked is True


def test_panel_read_is_refused_while_disabled(monkeypatch):
    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", False)
    authority = _authority(_record())
    with pytest.raises(MCPAuthError) as error:
        asyncio.run(WorkResourceAuthority.read(authority, PANEL_URI))
    assert error.value.status_code == 503
    authority.service.get.assert_not_called()


def test_panel_read_without_the_status_scope_is_refused():
    authority = _authority(_record(), scope="artifacts.read")
    with pytest.raises(MCPAuthError) as error:
        asyncio.run(WorkResourceAuthority.read(authority, PANEL_URI))
    assert error.value.status_code == 403


def test_panel_read_refuses_mismatched_or_unmappable_records(monkeypatch):
    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", True)
    for record in (_record(id="3f8a1c2e-9b4d-4f7a-8c1e-2d5b6a7c8e9a"), _record(disposition="mystery"), None):
        authority = _authority(record)
        with pytest.raises(MCPAuthError) as error:
            asyncio.run(WorkResourceAuthority.read(authority, PANEL_URI))
        assert error.value.status_code == 503


def test_phi_screened_delivery_blocks_the_panel(monkeypatch):
    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", True)

    class UnsafeGate:
        def contains_phi_for_sharing(self, payload):
            return True

    authority = _authority(_record(), gate=UnsafeGate())
    with pytest.raises(MCPAuthError):
        asyncio.run(WorkResourceAuthority.read(authority, PANEL_URI))


@pytest.mark.parametrize("enabled", [True, False])
def test_listing_appends_panel_descriptors_only_when_enabled(monkeypatch, enabled):
    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", enabled)
    service = SimpleNamespace(list=AsyncMock(return_value={"operations": [_record()]}))
    authority = _authority(_record(), service=service)
    resources = asyncio.run(WorkResourceAuthority.list(authority))
    uris = [item["uri"] for item in resources]
    assert uris == [STATUS_URI] + ([PANEL_URI] if enabled else [])
    if enabled:
        assert resources[1]["_meta"]["astral/panelKind"] == PANEL_KIND


def test_status_and_result_reads_keep_their_existing_envelopes(monkeypatch):
    monkeypatch.setitem(flags._flags, "mcp_server", True)
    monkeypatch.setitem(flags._flags, "mcp_work_panel", True)
    authority = _authority(_record())
    status = asyncio.run(WorkResourceAuthority.read(authority, STATUS_URI))
    assert set(status) == {"uri", "name", "mimeType", "boundedViewModel"}
    retained = {
        "id": WORK_ID, "revision": 4,
        "result": {"version": 1, "available": False, "reason": "not_retained", "content": None},
    }
    authority = _authority(retained, scope="artifacts.read")
    result = asyncio.run(WorkResourceAuthority.read(authority, f"astral://work/{WORK_ID}/result"))
    assert result["name"] == f"Retained result: {WORK_ID}"
    assert result["boundedViewModel"] == retained


def _panel_request(method, uri):
    params = {"_meta": dict(META)}
    if uri is not None:
        params["uri"] = uri
    return {
        "json": {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        "headers": {"Authorization": "Bearer dev-mcp-token",
                    "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method},
    }


class _StubAuthority:
    def __init__(self, orchestrator, user_id, claims, *, scope="operations.read"):
        self.scope = scope

    async def read(self, uri):
        if uri == PANEL_URI:
            return panel_read_result(uri, WORK_ID, panel_view(_record()))
        return {
            "uri": uri, "name": "Status: Nightly digest", "mimeType": "application/json",
            "boundedViewModel": {"id": WORK_ID, "title": "Nightly digest"},
        }

    async def recheck(self):
        return None


@pytest.mark.parametrize("uri,with_meta", [(PANEL_URI, True), (STATUS_URI, False)])
def test_resources_read_carries_meta_only_for_panel_resources(monkeypatch, uri, with_meta):
    from orchestrator import mcp_server_endpoint

    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setattr(mcp_server_endpoint, "WorkResourceAuthority", _StubAuthority)
    app = FastAPI()
    app.include_router(create_mcp_router(_Orchestrator(), public_base_url="https://mcp.invalid"))
    response = TestClient(app).post("/mcp", **_panel_request("resources/read", uri))
    assert response.status_code == 200
    result = response.json()["result"]
    assert response.headers["cache-control"] == "no-store"
    if with_meta:
        assert result["_meta"]["astral/panelKind"] == PANEL_KIND
        assert result["_meta"]["astral/dataFlow"] == "owner-scoped; no third-party data flow"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == {
            "name": "AstralDeep", "version": "1.0.0",
        }
        assert "astral/panelKind" not in result["contents"][0]
    else:
        assert "_meta" not in result
