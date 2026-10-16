"""Tests for the MCP resource endpoints (bounty #322, partial deliverable).

Testing strategy
----------------
The substantive behaviour -- list/read dispatch, owner isolation, authorization,
and the bounded view models -- is exercised against the **real**
``FrameworkWorkOperations`` facade. The facade is backed by a synthetic
assignment store (``_Repository``) that returns well-formed reads, so the real
``WorkService`` -> ``_public`` transformation runs end to end exactly as in
production. This proves the resource helpers honour the real Work contract
(owner scoping, the ``operations.read`` guard, and the public record shape)
instead of a fabricated mock shape.

A small number of *defensive* guards live in ``list_resources`` /
``read_resource`` that are unreachable through the validating real facade
(every id is a canonical UUID4 and every record is well formed). Those branches
are exercised directly against the facade method in clearly labelled tests.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
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
    """A facade record shape used by the direct _build_bounded_view_model test."""
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


def _make_read(owner_id, assignment_id, *, title="Nightly digest",
               disposition="active", lifecycle="active", phase="executing", revision=4):
    """A well-formed synthetic assignment read that survives the real facade's
    ``_public`` transformation (the same path production uses)."""
    assignment = SimpleNamespace(
        owner_id=owner_id,
        execution_profile="one_shot",
        lifecycle=lifecycle,
        phase=phase,
        usage={},
        safe_error_code=None,
        assignment_id=assignment_id,
        state_version=revision,
        instruction_revision=revision,
        control_epoch=1,
        created_at=datetime(2026, 10, 15, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 10, 15, 0, 5, tzinfo=UTC),
        definition=SimpleNamespace(name=title),
        operation={"kind": "chat", "deadline_at": None},
    )
    return SimpleNamespace(
        assignment=assignment,
        disposition=disposition,
        continuation_supported=True,
    )


class _Repository:
    """Synthetic Work repository: returns real-shaped reads so the real
    ``FrameworkWorkOperations`` / ``WorkService`` / ``_public`` path runs end to end."""

    def __init__(self, reads, *, list_error=None, get_error=None):
        self._reads = dict(reads)
        self.list_error = list_error
        self.get_error = get_error
        self.captured_owner_ids = []

    async def list_operations(self, tx, *, owner_id, limit=50, after_id=None):
        self.captured_owner_ids.append(owner_id)
        if self.list_error is not None:
            raise self.list_error
        items = list(self._reads.values())
        if after_id is not None:
            items = [r for r in items if r.assignment.assignment_id > after_id]
        return items[:limit]

    async def get_operation(self, tx, *, owner_id, assignment_id):
        if self.get_error is not None:
            raise self.get_error
        return self._reads.get(assignment_id)


class _Store:
    async def transaction(self, callback, *, bound_session_waits=False):
        return callback(None, self.repository)


class _Assignments:
    def __init__(self, reads, *, list_error=None, get_error=None):
        self.store = _Store()
        self.store.repository = _Repository(reads, list_error=list_error, get_error=get_error)

    def _owner(self, owner_id, claims):
        return None


def _new_facade(reads=None, *, list_error=None, get_error=None):
    return FrameworkWorkOperations(
        assignments=_Assignments(reads or {}, list_error=list_error, get_error=get_error),
        credentials=object(),
    )


def _facade(reads=None, *, list_error=None, get_error=None):
    return _new_facade(reads, list_error=list_error, get_error=get_error)


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
                _endpoint(_facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID)})), OWNER_ID,
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
        resources = asyncio.run(list_resources(
            _endpoint(_facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID)})),
            OWNER_ID, _claims(_caller())))
        uris = [item["uri"] for item in resources]
        assert uris == [
            f"astral://work/{WORK_ID}/status",
            f"astral://work/{WORK_ID}/artifacts",
        ]
        assert all(item["mimeType"] == "application/json" for item in resources)

    def test_records_without_a_canonical_id_are_skipped(self):
        # Defensive guard in list_resources: records whose id is not a canonical
        # UUID4 are skipped. The real facade validates every id, so this branch is
        # exercised directly against the facade method.
        async def _list(caller, *, limit=50, after_id=None):
            return {"operations": [
                {"id": "work-1", "title": "legacy"},
                {"id": WORK_ID, "title": "canonical"},
                {"title": "missing id"},
            ]}

        ops = _new_facade()
        ops.list = _list
        resources = asyncio.run(
            list_resources(_endpoint(ops), OWNER_ID, _claims(_caller())))
        assert len(resources) == 1
        assert all(WORK_ID in item["uri"] for item in resources)

    def test_unexpected_facade_payload_lists_nothing(self):
        resources = asyncio.run(list_resources(
            _endpoint(_facade()), OWNER_ID, _claims(_caller())))
        assert resources == []


class TestResourceRoundTrip:
    """Anything the lister advertises must be readable with a valid UUID4 id."""

    def test_every_listed_uri_reads_successfully(self):
        ops = _facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID)})
        endpoint = _endpoint(ops)
        claims = _claims(_caller())

        listed = asyncio.run(list_resources(endpoint, OWNER_ID, claims))
        assert listed, "the lister must advertise at least one resource"

        for item in listed:
            read = asyncio.run(read_resource(endpoint, OWNER_ID, item["uri"], claims))
            assert read["uri"] == item["uri"]
            assert read["boundedViewModel"]["id"] == WORK_ID

    def test_listing_contains_no_uri_the_reader_rejects(self):
        ops = _facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID)})
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
            _endpoint(_facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID, disposition="active")})),
            OWNER_ID, f"astral://work/{WORK_ID}/status", _claims(_caller())))

        view = read["boundedViewModel"]
        assert set(view) == {
            "id", "title", "kind", "disposition", "lifecycle", "phase",
            "revision", "createdAt", "updatedAt",
        }
        assert view["id"] == WORK_ID
        assert view["title"] == "Nightly digest"
        assert view["disposition"] == "active"

    def test_status_view_never_carries_owner_or_usage(self):
        view = _build_bounded_view_model(_record())
        assert "owner_id" not in view
        assert "usage" not in view
        assert "must-not-leak" not in repr(view)

    def test_artifact_view_is_bounded(self):
        read = asyncio.run(read_resource(
            _endpoint(_facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID)})), OWNER_ID,
            f"astral://work/{WORK_ID}/artifacts", _claims(_caller())))

        view = read["boundedViewModel"]
        assert set(view) == {"id", "title", "disposition", "revision"}
        assert view["id"] == WORK_ID

    def test_untitled_record_gets_a_stable_label(self):
        read = asyncio.run(read_resource(
            _endpoint(_facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID, title="")})), OWNER_ID,
            f"astral://work/{WORK_ID}/status", _claims(_caller())))
        assert read["boundedViewModel"]["title"] == "Untitled"

    def test_unexpected_read_payload_fails_as_unavailable(self):
        # Defensive guard in read_resource: a non-dict record from the facade is a
        # 503. The real facade always returns a dict (or raises), so this branch is
        # exercised directly against the facade method.
        ops = _new_facade()
        ops.get = AsyncMock(return_value=None)
        with pytest.raises(MCPAuthError) as caught:
            asyncio.run(read_resource(
                _endpoint(ops), OWNER_ID, f"astral://work/{WORK_ID}/status", _claims(_caller())))
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
        """The facade receives the caller's owner_id, never a caller-less request."""
        ops = _facade(reads={WORK_ID: _make_read("owner-distinct", WORK_ID)})
        caller = _caller(owner_id="owner-distinct")
        asyncio.run(list_resources(_endpoint(ops), "ignored-user-id", _claims(caller)))
        assert ops.store.repository.captured_owner_ids == ["owner-distinct"]

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
        ops = _facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID)})
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
        ops = _facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID)})
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
        ops = _facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID)})
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
        ops = _facade(reads={WORK_ID: _make_read(OWNER_ID, WORK_ID)})
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
