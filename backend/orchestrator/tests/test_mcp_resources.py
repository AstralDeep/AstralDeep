"""Tests for the MCP resource methods (bounty #322).

These drive the real ``FrameworkWorkOperations`` facade and a real
``FrameworkCaller`` so that the authorization guard cannot be satisfied by a
stub that invents a record shape the facade never returns, and they assert that
every URI the lister advertises round-trips through the reader.

Async coroutines are driven with ``asyncio.run`` to match the other backend
suites, which run without an async pytest plugin.
"""

import asyncio

import pytest

from orchestrator.framework_credentials import FrameworkCaller
from orchestrator.mcp_authz import MCPAuthError
from orchestrator.mcp_server_endpoint import (
    _WORK_RESOURCE_URI_PATTERN,
    _build_bounded_view_model,
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


def _facade(*, page=None, record=None, list_error=None, get_error=None):
    """A real facade whose reads are answered by a deterministic stub."""
    ops = FrameworkWorkOperations(assignments=object(), credentials=object())

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
            _endpoint(_facade(page=None)), OWNER_ID, _claims(_caller())))
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
        ops = FrameworkWorkOperations(assignments=object(), credentials=object())

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
