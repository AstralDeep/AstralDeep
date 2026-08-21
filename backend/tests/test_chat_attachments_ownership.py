"""Feature 031 — attachment ownership enforcement on the chat turn (T022).

A reference to an attachment the sender does not own is dropped (and never
appears in the LLM-facing block or as a message_attachment link), and the user
is told some attachments were skipped. Covers FR-007 / SC-004.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator.attachments.repository import AttachmentRepository  # noqa: E402
from tests.attachments._fake_db_031 import FakeDB  # noqa: E402
from tests.attachments.conftest import (  # noqa: E402
    attachment_plane_source,
    seed_attachment_for_test,
)


def _seed(db, *, user_id, attachment_id, category="document", extension="pdf"):
    repo = AttachmentRepository.from_plane_source(attachment_plane_source(db))
    seed_attachment_for_test(
        repo,
        attachment_id=attachment_id,
        user_id=user_id,
        filename=f"{attachment_id}.{extension}",
        content_type="application/pdf",
        category=category,
        extension=extension,
        size_bytes=10,
        sha256="0" * 64,
    )


def _fake_self(db):
    sent = []

    async def _safe_send(ws, data):
        sent.append(data)

    return types.SimpleNamespace(
        plane_repository_source=attachment_plane_source(db),
        _safe_send=_safe_send,
        _sent=sent,
    )


@pytest.mark.asyncio
async def test_foreign_attachment_is_dropped_and_user_notified():
    db = FakeDB()
    _seed(db, user_id="u1", attachment_id="mine")
    _seed(db, user_id="u2", attachment_id="theirs")  # owned by someone else
    me = _fake_self(db)
    payload = [
        {"attachment_id": "mine", "filename": "mine.pdf", "category": "document"},
        {"attachment_id": "theirs", "filename": "theirs.pdf", "category": "document"},
    ]
    out = await Orchestrator._attach_turn_attachments(me, object(), "read these", "c1", "u1", "m1", payload)

    # Only the owned attachment is surfaced + linked.
    assert "id=mine" in out
    assert "theirs" not in out
    assert {r.attachment_id for r in db.message_attachment} == {"mine"}
    # The user is told something was skipped.
    assert any("skipped" in s for s in me._sent)


@pytest.mark.asyncio
async def test_foreign_attachment_denial_is_audited(monkeypatch):
    """Regression: a dropped cross-user reference MUST be recorded in the audit
    trail. ``_audit_drop`` previously built an AuditEventCreate without the
    required ``correlation_id``/``started_at`` fields, so construction raised and
    the bare except swallowed it — the denial was never audited.
    """
    from audit import recorder as recorder_mod

    captured = []

    class _CapturingRecorder:
        async def record(self, event):
            captured.append(event)

    old = recorder_mod.get_recorder()
    recorder_mod.set_recorder(_CapturingRecorder())
    try:
        db = FakeDB()
        _seed(db, user_id="u1", attachment_id="mine")
        _seed(db, user_id="u2", attachment_id="theirs")  # owned by someone else
        me = _fake_self(db)
        payload = [
            {"attachment_id": "mine", "filename": "mine.pdf", "category": "document"},
            {"attachment_id": "theirs", "filename": "theirs.pdf", "category": "document"},
        ]
        await Orchestrator._attach_turn_attachments(
            me, object(), "read these", "c1", "u1", "m1", payload
        )
    finally:
        recorder_mod.set_recorder(old)

    denials = [e for e in captured if getattr(e, "action_type", None) == "attachment_reference_denied"]
    assert len(denials) == 1, "the foreign reference must produce exactly one audit denial"
    ev = denials[0]
    assert ev.outcome == "failure"
    assert ev.event_class == "file"
    assert ev.actor_user_id == "u1"
    assert ev.correlation_id, "correlation_id must be populated (required field)"
    assert ev.started_at is not None, "started_at must be populated (required field)"


@pytest.mark.asyncio
async def test_unknown_attachment_id_is_dropped():
    db = FakeDB()
    _seed(db, user_id="u1", attachment_id="real")
    me = _fake_self(db)
    payload = [
        {"attachment_id": "real", "filename": "real.pdf", "category": "document"},
        {"attachment_id": "ghost", "filename": "ghost.pdf", "category": "document"},
    ]
    out = await Orchestrator._attach_turn_attachments(me, object(), "x", "c1", "u1", "m1", payload)
    assert "id=real" in out and "ghost" not in out
    assert {r.attachment_id for r in db.message_attachment} == {"real"}
