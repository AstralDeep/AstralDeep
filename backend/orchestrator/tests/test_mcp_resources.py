"""Tests for the MCP resource endpoints (bounty #322).

These tests exercise the full mcp_endpoint dispatch path for resources/list
and resources/read without depending on FastAPI, so they run in the CI
environment which lacks the FastAPI dependency.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.framework_credentials import FrameworkCaller
from orchestrator.mcp_authz import MCPAuthError
from orchestrator.mcp_server_endpoint import (
    _WORK_RESOURCE_URI_PATTERN,
    _build_bounded_view_model,
    create_mcp_router,
    list_resources,
    read_resource,
)
from orchestrator.work_operations import FrameworkWorkOperations
from persistent_agents.models import AssignmentError

OWNER_ID = "owner-2f1c"
WORK_ID = "3f8a1c2e-9b4d-4f7a-8c1e-2d5b6a7c8e9f"
FOREIGN_ID = "7c2e4a1b-5d3f-4a8b-9e6c-1f2a3b4c5d6e"

_EMPTY_PAGE = {"operations": [], "next_cursor": None, "page_full": False}


def _caller(*, owner_id=OWNER_ID, scopes=("operations.read",)):
    return FrameworkCaller(
        owner_id=owner_id,
        credential_id="cred-2f1c",
        scopes=frozenset(scopes),
        _token_hash="irrelevant",
    )


def _record(work_id=WORK_ID, title="Nightly digest"):
    """A facade record carrying the public fields plus values that must not leak."""
    return {
        "id": work_id,
        "revision": 4,
        "title": title,
        "kind": "chat",
        "disposition": "running",
        "lifecycle": "active",
        "phase": "executing",
        "created_at": "2026-10-15T00:00:00+00:00",
        "updated_at": "2026-10-15T00:05:00+00:00",
        "owner_id": "must-not-leak",
        "usage": {"private": True},
    }


class _Assignments:
    """Minimal stand-in for the facade's assignment registry."""
    store = None


def _new_facade():
    return FrameworkWorkOperations(assignments=_Assignments(), credentials=object())


def _facade(*, page=None, record=None, list_error=None, get_error=None):
    """A real facade whose reads are answered by a deterministic stub."""
    ops = _new_facade()

    async def _list(caller, *, limit=50, after_id=None):
        assert isinstance(caller, FrameworkCaller)
        if list_error is not None:
            raise list_error
        return dict(page if page is not None else _EMPTY_PAGE)

    async def _get(caller, operation_id):
        assert isinstance(caller, FrameworkCaller)
        if get_error is not None:
            raise get_error
        return record

    ops.list = _list
    ops.get = _get
    return ops


def _endpoint(ops):
    class _Endpoint:
        framework_work_operations = ops
    return _Endpoint()


def _claims(caller):
    return {"_framework_caller": caller} if caller is not None else {}


def _make_mock_request(headers=None, body=None):
    """Create a minimal mock Request object for testing mcp_endpoint."""
    req = MagicMock()
    req.headers = MagicMock()
    req.headers.get = MagicMock(side_effect=lambda k, d=None: (headers or {}).get(k, d))
    req.query_params = {}
    req.cookies = {}
    # _read_body consumes the body via `async for chunk in request.stream()`,
    # so the mock must return an *async* iterable, not a plain iterator.
    async def _stream():
        yield body or b'{}'
    req.stream = MagicMock(return_value=_stream())
    req.is_disconnected = AsyncMock(return_value=False)
    return req


def _parse_json_response(resp):
    """Parse the JSON response from mcp_endpoint."""
    import json
    return json.loads(resp.body)


class TestResourceUriContract:
    """The lister and the reader must agree on exactly one URI shape."""

    @pytest.mark.parametrize("kind", ["status", "artifacts"])
    def test_advertised_uris_match_the_reader_contract(self, kind):
        match = _WORK_RESOURCE_URI_PATTERN.match(f"astral://work/{WORK_ID}/{kind}")
        assert match is not None
        assert match.group(1) == WORK_ID
        assert match.group(2) == kind

    @pytest.mark.parametrize("uri", [
        "astral://work/work-1/status",                     # not a canonical UUID4
        "astral://work/work-2/artifacts",                  # not a canonical UUID4
        f"astral://work/{WORK_ID}/metadata",               # unadvertised kind
        f"astral://artifact/{WORK_ID}",                    # retired form, never listed
        f"astral://work/{WORK_ID}",
        f"astral://work/{WORK_ID}/status/../../etc/passwd",
        "astral://work/../../etc/passwd/status",
        "astral://work/3f8a1c2e-9b4d-4f7a-8c1e-2d5b6a7c8e9f/status\n",
        "",
    ])
    def test_uri_outside_the_contract_is_rejected(self, uri):
        assert _WORK_RESOURCE_URI_PATTERN.match(uri) is None

    def test_rejected_uri_reports_400(self):
        with pytest.raises(MCPAuthError) as caught:
            asyncio.run(read_resource(
                _endpoint(_facade()), OWNER_ID, "astral://work/work-1/status", _claims(_caller())))
        assert caught.value.status_code == 400
        assert caught.value.error == "invalid_uri"

    def test_non_string_uri_reports_400(self):
        with pytest.raises(MCPAuthError) as caught:
            asyncio.run(read_resource(
                _endpoint(_facade()), OWNER_ID, None, _claims(_caller())))
        assert caught.value.status_code == 400


class TestAuthorization:
    """Every resource request must carry the framework Work authority."""

    def test_listing_without_a_verified_caller_fails_closed(self):
        for claims in ({}, {"_framework_caller": None}, {"_framework_caller": object()}):
            with pytest.raises(MCPAuthError) as caught:
                asyncio.run(list_resources(_endpoint(_facade()), OWNER_ID, claims))
            assert caught.value.status_code == 403

    def test_reading_without_a_verified_caller_fails_closed(self):
        with pytest.raises(MCPAuthError) as caught:
            asyncio.run(read_resource(
                _endpoint(_facade(record=_record())), OWNER_ID,
                f"astral://work/{WORK_ID}/status", {}))
        assert caught.value.status_code == 403

    def test_caller_without_the_read_scope_fails_closed(self):
        caller = _caller(scopes=("operations.submit",))
        with pytest.raises(MCPAuthError) as caught:
            asyncio.run(list_resources(_endpoint(_facade()), OWNER_ID, _claims(caller)))
        assert caught.value.status_code == 403

    def test_missing_facade_fails_closed(self):
        class _Endpoint:
            framework_work_operations = None

        with pytest.raises(MCPAuthError) as caught:
            asyncio.run(list_resources(_Endpoint(), OWNER_ID, _claims(_caller())))
        assert caught.value.status_code == 403

    def test_facade_denial_is_passed_through_unchanged(self):
        ops = _facade(list_error=AssignmentError("framework_scope_required", 403))
        with pytest.raises(MCPAuthError) as caught:
            asyncio.run(list_resources(_endpoint(ops), OWNER_ID, _claims(_caller())))
        assert caught.value.status_code == 403


class TestResourceListing:
    def test_empty_page_lists_nothing(self):
        resources = asyncio.run(
            list_resources(_endpoint(_facade()), OWNER_ID, _claims(_caller())))
        assert resources == []

    def test_each_visible_work_exposes_status_and_artifacts(self):
        page = {"operations": [_record()], "next_cursor": None, "page_full": False}
        resources = asyncio.run(
            list_resources(_endpoint(_facade(page=page)), OWNER_ID, _claims(_caller())))
        uris = [item["uri"] for item in resources]
        assert uris == [
            f"astral://work/{WORK_ID}/status",
            f"astral://work/{WORK_ID}/artifacts",
        ]
        assert all(item["mimeType"] == "application/json" for item in resources)

    def test_records_without_a_canonical_id_are_skipped(self):
        page = {"operations": [
            {"id": "work-1", "title": "legacy"},
            {"title": "missing id"},
            "not-a-record",
            _record(),
        ], "next_cursor": None, "page_full": False}
        resources = asyncio.run(
            list_resources(_endpoint(_facade(page=page)), OWNER_ID, _claims(_caller())))
        assert len(resources) == 2
        assert all(WORK_ID in item["uri"] for item in resources)

    def test_unexpected_facade_payload_lists_nothing(self):
        resources = asyncio.run(list_resources(
            _endpoint(_facade(page={"unexpected": True})), OWNER_ID, _claims(_caller())))
        assert resources == []


class TestResourceRoundTrip:
    """Anything the lister advertises must be readable."""

    def test_every_listed_uri_reads_successfully(self):
        page = {"operations": [_record()], "next_cursor": None, "page_full": False}
        ops = _facade(page=page, record=_record())
        endpoint = _endpoint(ops)
        claims = _claims(_caller())

        listed = asyncio.run(list_resources(endpoint, OWNER_ID, claims))
        assert listed, "the lister must advertise at least one resource"

        for item in listed:
            read = asyncio.run(read_resource(endpoint, OWNER_ID, item["uri"], claims))
            assert read["uri"] == item["uri"]
            assert read["boundedViewModel"]["id"] == WORK_ID

    def test_listing_contains_no_uri_the_reader_rejects(self):
        page = {"operations": [_record()], "next_cursor": None, "page_full": False}
        ops = _facade(page=page, record=_record())
        claims = _claims(_caller())
        listed = asyncio.run(list_resources(_endpoint(ops), OWNER_ID, claims))

        rejected = []
        for item in listed:
            try:
                asyncio.run(read_resource(_endpoint(ops), OWNER_ID, item["uri"], claims))
            except MCPAuthError as exc:
                rejected.append((item["uri"], exc.status_code))
        assert rejected == []


class TestBoundedViewModels:
    def test_status_view_keeps_only_allowlisted_fields(self):
        read = asyncio.run(read_resource(
            _endpoint(_facade(record=_record())), OWNER_ID,
            f"astral://work/{WORK_ID}/status", _claims(_caller())))

        view = read["boundedViewModel"]
        assert set(view) == {
            "id", "title", "kind", "disposition", "lifecycle", "phase",
            "revision", "createdAt", "updatedAt",
        }
        assert view["id"] == WORK_ID
        assert view["title"] == "Nightly digest"
        assert view["disposition"] == "running"

    def test_status_view_never_carries_owner_or_usage(self):
        view = _build_bounded_view_model(_record())
        assert "owner_id" not in view
        assert "usage" not in view
        assert "must-not-leak" not in repr(view)

    def test_artifact_view_is_bounded(self):
        read = asyncio.run(read_resource(
            _endpoint(_facade(record=_record())), OWNER_ID,
            f"astral://work/{WORK_ID}/artifacts", _claims(_caller())))

        view = read["boundedViewModel"]
        assert set(view) == {"id", "title", "disposition", "revision"}
        assert view["id"] == WORK_ID

    def test_untitled_record_gets_a_stable_label(self):
        read = asyncio.run(read_resource(
            _endpoint(_facade(record=_record(title=""))), OWNER_ID,
            f"astral://work/{WORK_ID}/status", _claims(_caller())))
        assert read["boundedViewModel"]["title"] == "Untitled"

    def test_unexpected_read_payload_fails_as_unavailable(self):
        with pytest.raises(MCPAuthError) as caught:
            asyncio.run(read_resource(
                _endpoint(_facade(record=None)), OWNER_ID,
                f"astral://work/{WORK_ID}/status", _claims(_caller())))
        assert caught.value.status_code == 503


class TestOwnerIsolation:
    def test_foreign_work_surfaces_the_facade_not_found(self):
        """A work item outside the caller's owner reads as absent, not as a leak."""
        ops = _facade(get_error=AssignmentError("work_not_found", 404))
        with pytest.raises(MCPAuthError) as caught:
            asyncio.run(read_resource(
                _endpoint(ops), OWNER_ID, f"astral://work/{FOREIGN_ID}/status", _claims(_caller())))
        assert caught.value.status_code == 404

    def test_reader_authenticates_with_the_caller_owner(self):
        """The facade receives the caller itself, never a caller-less request."""
        seen = []
        ops = _new_facade()

        async def _list(caller, *, limit=50, after_id=None):
            seen.append(caller)
            return dict(_EMPTY_PAGE)

        ops.list = _list
        caller = _caller(owner_id="owner-distinct")
        asyncio.run(list_resources(_endpoint(ops), "ignored-user-id", _claims(caller)))
        assert seen == [caller]
        assert seen[0].owner_id == "owner-distinct"

    def test_revoked_caller_is_refused_by_the_facade(self):
        ops = _facade(get_error=AssignmentError("work_authentication_required", 401))
        with pytest.raises(MCPAuthError) as caught:
            asyncio.run(read_resource(
                _endpoint(ops), OWNER_ID, f"astral://work/{WORK_ID}/status", _claims(_caller())))
        assert caught.value.status_code == 401


class _Orchestrator:
    """Minimal orchestrator stand-in; only a real Work facade is required.

    mcp_endpoint reads ``orchestrator.framework_work_operations`` (a real
    ``FrameworkWorkOperations``) and optionally ``orchestrator.runtime_observability``
    (absent here, so metric recording is a no-op). Admission is mocked away.
    """
    history = None


class TestIntegration:
    """Exercise the real mcp_endpoint dispatch path for resources/*.

    ``mcp_endpoint`` is a route handler nested inside ``create_mcp_router``
    (decorated with ``@router.post("/mcp")``), so it is not importable at module
    level. The faithful, FastAPI-free way to reach it -- and to cover its
    resources/list and resources/read branches -- is to build the router and
    pull the ``/mcp`` handler out, then drive it with a mock Request. Admission
    is mocked so we only exercise the resource dispatch logic.
    """

    @asynccontextmanager
    async def _fake_admission(self, *args, **kwargs):
        yield MagicMock()

    def _mcp_endpoint(self, ops):
        """Build the router with a real Work facade and return the /mcp handler."""
        orch = _Orchestrator()
        orch.framework_work_operations = ops
        router = create_mcp_router(orch, public_base_url="https://example.com")
        return next(
            r.endpoint
            for r in router.routes
            if getattr(r, "path", None) == "/mcp"
        )

    def _claims_with_sub(self):
        claims = _claims(_caller())
        claims.setdefault("sub", OWNER_ID)
        return claims

    def _dispatch(self, ops, *, method, body, mcp_method=None):
        mcp_endpoint = self._mcp_endpoint(ops)
        mock_request = _make_mock_request(
            headers={
                "content-type": "application/json",
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": mcp_method or method,
            },
            body=body,
        )
        with patch(
            "orchestrator.mcp_server_endpoint.authorize_mcp_request",
            new_callable=lambda: AsyncMock(return_value=self._claims_with_sub()),
        ), patch(
            "orchestrator.mcp_server_endpoint._admitted_request", self._fake_admission
        ):
            return asyncio.run(mcp_endpoint(mock_request))

    def test_resources_list_dispatch_via_endpoint(self):
        """resources/list is served through the main dispatcher, not just the helper."""
        ops = _facade(page={"operations": [_record()], "next_cursor": None, "page_full": False})
        resp = self._dispatch(
            ops,
            method="resources/list",
            body=b'{"jsonrpc":"2.0","id":1,"method":"resources/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}',
        )
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["result"]["resultType"] == "complete"
        resources = body["result"]["resources"]
        assert len(resources) == 2  # status + artifacts
        uris = {r["uri"] for r in resources}
        assert f"astral://work/{WORK_ID}/status" in uris
        assert f"astral://work/{WORK_ID}/artifacts" in uris

    def test_resources_read_dispatch_via_endpoint(self):
        """resources/read is served through the main dispatcher, not just the helper."""
        ops = _facade(record=_record())
        resp = self._dispatch(
            ops,
            method="resources/read",
            body=b'{"jsonrpc":"2.0","id":2,"method":"resources/read","params":{"uri":"astral://work/3f8a1c2e-9b4d-4f7a-8c1e-2d5b6a7c8e9f/status","_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}',
        )
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["result"]["resultType"] == "complete"
        resource = body["result"]["resource"]
        assert resource["uri"] == f"astral://work/{WORK_ID}/status"
        assert resource["boundedViewModel"]["id"] == WORK_ID
        assert "owner_id" not in resource["boundedViewModel"]

    def test_invalid_uri_rejected_via_endpoint(self):
        """A malformed URI reaches the reader and returns a 400 error via the dispatcher."""
        ops = _facade(record=_record())
        resp = self._dispatch(
            ops,
            method="resources/read",
            body=b'{"jsonrpc":"2.0","id":3,"method":"resources/read","params":{"uri":"astral://work/bad-id/status","_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}',
        )
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "error" in body

    def test_missing_uri_param_rejected_via_endpoint(self):
        """A resources/read request without a uri param returns a 400 error."""
        ops = _facade(record=_record())
        resp = self._dispatch(
            ops,
            method="resources/read",
            body=b'{"jsonrpc":"2.0","id":4,"method":"resources/read","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}',
        )
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "error" in body

    def test_auth_failure_propagates_via_endpoint(self):
        """An auth failure raises MCPAuthError that the dispatcher turns into 403."""
        ops = _facade()
        mcp_endpoint = self._mcp_endpoint(ops)
        mock_request = _make_mock_request(
            headers={
                "content-type": "application/json",
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "resources/list",
            },
            body=b'{"jsonrpc":"2.0","id":5,"method":"resources/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}',
        )

        def raise_auth(*a, **kw):
            raise MCPAuthError(403, "forbidden", "no scope", ("mcp:resources:read",))

        with patch(
            "orchestrator.mcp_server_endpoint.authorize_mcp_request", side_effect=raise_auth
        ), patch(
            "orchestrator.mcp_server_endpoint._admitted_request", self._fake_admission
        ):
            resp = asyncio.run(mcp_endpoint(mock_request))
        assert resp.status_code == 403

    def test_resources_list_auth_error_propagates_via_endpoint(self):
        """A facade denial on resources/list propagates as a 403 error."""
        ops = _facade(list_error=AssignmentError("framework_scope_required", 403))
        resp = self._dispatch(
            ops,
            method="resources/list",
            body=b'{"jsonrpc":"2.0","id":6,"method":"resources/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}',
        )
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert "error" in body

    def test_resources_read_auth_error_propagates_via_endpoint(self):
        """A facade denial on resources/read propagates as a 404 error."""
        ops = _facade(get_error=AssignmentError("work_not_found", 404))
        resp = self._dispatch(
            ops,
            method="resources/read",
            body=b'{"jsonrpc":"2.0","id":7,"method":"resources/read","params":{"uri":"astral://work/7c2e4a1b-5d3f-4a8b-9e6c-1f2a3b4c5d6e/status","_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}',
        )
        assert resp.status_code == 404
        body = json.loads(resp.body)
        assert "error" in body
