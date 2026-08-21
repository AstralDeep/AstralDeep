"""Feature 031 US3 — attachments library chrome surface (T043).

The surface lists only the caller's live attachments with attach (existing,
no re-upload) + delete controls. Covers FR-020/FR-021.
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
)
from orchestrator.attachments.repository import AttachmentRepository  # noqa: E402
from orchestrator.projection_surfaces import attachments as surface  # noqa: E402


def _seed(repo, *, user_id, attachment_id, filename, category="document", extension="pdf"):
    seed_attachment_for_test(
        repo,
        attachment_id=attachment_id,
        user_id=user_id,
        filename=filename,
        content_type="application/pdf",
        category=category,
        extension=extension,
        size_bytes=2048,
        sha256="0" * 64,
    )


def _orch(repo):
    return types.SimpleNamespace(attachment_repository=repo)


def _repo(database):
    return AttachmentRepository.from_plane_source(attachment_plane_source(database))


@pytest.mark.asyncio
async def test_render_lists_only_callers_attachments():
    db = StubDatabase()
    repo = _repo(db)
    _seed(repo, user_id="u1", attachment_id="a1", filename="report.pdf")
    _seed(repo, user_id="u1", attachment_id="a2", filename="data.parquet",
          category="data", extension="parquet")
    _seed(repo, user_id="u2", attachment_id="b1", filename="secret.pdf")

    html = await surface.render(_orch(repo), "u1", [], {})
    assert "report.pdf" in html and "data.parquet" in html
    assert "secret.pdf" not in html  # another user's file never shown
    # Attach buttons carry the existing id (client stages it — no re-upload).
    assert 'class="astral-attach-existing' in html
    assert 'data-attachment-id="a1"' in html
    # Delete routes through the server handler.
    assert "chrome_attachment_delete" in html


@pytest.mark.asyncio
async def test_render_empty_state():
    db = StubDatabase()
    html = await surface.render(_orch(_repo(db)), "u1", [], {})
    assert "No uploads yet" in html


@pytest.mark.asyncio
async def test_delete_action_reports_durable_cleanup_acceptance():
    class _Purges:
        async def aschedule_attachment(self, *, owner_id, attachment_id):
            assert (owner_id, attachment_id) == ("u1", "a1")
            return types.SimpleNamespace(cleanup_id="opaque-purge-id")

    result = await surface._h_attachment_delete(
        types.SimpleNamespace(attachment_purge_coordinator=_Purges()),
        None,
        "u1",
        [],
        {"attachment_id": "a1"},
    )

    assert result[:2] == ("attachments", {})
    assert "cleanup is pending" in result[2]


@pytest.mark.asyncio
async def test_delete_action_preserves_owner_safe_not_found():
    class _Purges:
        async def aschedule_attachment(self, **_values):
            raise PlaneError("not found", code="purge_object_not_found")

    result = await surface._h_attachment_delete(
        types.SimpleNamespace(attachment_purge_coordinator=_Purges()),
        None,
        "u1",
        [],
        {"attachment_id": "missing"},
    )

    assert "Attachment not found" in result[2]
