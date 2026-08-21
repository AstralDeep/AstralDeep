"""Feature 031 US3 — attachment deletion via the library surface (T044).

Deleting removes the attachment from the list and makes it unreferenceable;
a non-owner delete is refused. Covers FR-022.
"""

from __future__ import annotations

import os
import sys
import types

import pytest
from astralplane.errors import PlaneError

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_TESTS = os.path.join(_BACKEND, "tests")
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from attachments.conftest import (  # noqa: E402
    StubDatabase,
    attachment_plane_source,
    seed_attachment_for_test,
    soft_delete_attachment_for_test,
)
from orchestrator.attachments.repository import AttachmentRepository  # noqa: E402
from orchestrator.projection_surfaces import attachments as surface  # noqa: E402


def _seed(repo, *, user_id, attachment_id):
    seed_attachment_for_test(
        repo,
        attachment_id=attachment_id,
        user_id=user_id,
        filename=f"{attachment_id}.pdf",
        content_type="application/pdf",
        category="document",
        extension="pdf",
        size_bytes=10,
        sha256="0" * 64,
    )


def _orch(repo):
    class Purges:
        async def aschedule_attachment(self, *, owner_id, attachment_id):
            if repo.get_by_id(attachment_id, owner_id) is None:
                raise PlaneError("not found", code="purge_object_not_found")
            assert soft_delete_attachment_for_test(
                repo,
                attachment_id=attachment_id,
                user_id=owner_id,
            )
            return types.SimpleNamespace(cleanup_id="purge-test")

    return types.SimpleNamespace(
        attachment_repository=repo,
        attachment_purge_coordinator=Purges(),
    )


def _repo(database):
    return AttachmentRepository.from_plane_source(attachment_plane_source(database))


@pytest.mark.asyncio
async def test_delete_removes_attachment_and_unreferenceable():
    db = StubDatabase()
    repo = _repo(db)
    _seed(repo, user_id="u1", attachment_id="a1")
    orch = _orch(repo)

    result = await surface._h_attachment_delete(orch, object(), "u1", [], {"attachment_id": "a1"})
    assert result[0] == "attachments"  # re-render the surface
    # Gone from the owner's view, and no longer resolvable.
    assert repo.get_by_id("a1", "u1") is None
    html = await surface.render(orch, "u1", [], {})
    assert "a1.pdf" not in html


@pytest.mark.asyncio
async def test_delete_foreign_attachment_is_refused():
    db = StubDatabase()
    repo = _repo(db)
    _seed(repo, user_id="owner", attachment_id="a1")
    orch = _orch(repo)
    # A different user cannot delete it.
    result = await surface._h_attachment_delete(orch, object(), "mallory", [], {"attachment_id": "a1"})
    assert result[0] == "attachments"
    assert "not found" in result[2].lower()
    # Still present for the real owner.
    assert repo.get_by_id("a1", "owner") is not None


@pytest.mark.asyncio
async def test_delete_missing_id_is_handled():
    db = StubDatabase()
    orch = _orch(_repo(db))
    result = await surface._h_attachment_delete(orch, object(), "u1", [], {})
    assert result[0] == "attachments"
    assert "no attachment" in result[2].lower()


@pytest.mark.asyncio
async def test_delete_blob_failure_is_visible_after_metadata_is_durably_hidden():
    db = StubDatabase()
    repo = _repo(db)
    _seed(repo, user_id="u1", attachment_id="a1")

    class PendingPurges:
        async def aschedule_attachment(self, *, owner_id, attachment_id):
            assert soft_delete_attachment_for_test(
                repo,
                attachment_id=attachment_id,
                user_id=owner_id,
            )
            return types.SimpleNamespace(cleanup_id="purge-test")

    orch = types.SimpleNamespace(
        attachment_repository=repo,
        attachment_purge_coordinator=PendingPurges(),
    )
    result = await surface._h_attachment_delete(
        orch,
        object(),
        "u1",
        [],
        {"attachment_id": "a1"},
    )
    assert "cleanup is pending" in result[2].lower()
    assert repo.get_by_id("a1", "u1") is None
