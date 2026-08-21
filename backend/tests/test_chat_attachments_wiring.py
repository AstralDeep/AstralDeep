"""Feature 031 — chat-turn attachment wiring (T021).

Exercises Orchestrator._attach_turn_attachments directly with an in-memory DB:
valid attachments are linked (message_attachment) and surfaced to the LLM as a
structured block naming the reader tool (or "pending parser" for uncovered
types); the per-message cap is enforced.
"""

from __future__ import annotations

import os
import sys
import asyncio
import threading
import types

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator.attachments.repository import AttachmentRepository  # noqa: E402
from orchestrator.attachments.message_attachment_repo import (  # noqa: E402
    MessageAttachmentRepository,
)
from orchestrator.attachments.parser_repo import (  # noqa: E402
    AttachmentParserRepository,
)
from tests.attachments._fake_db_031 import FakeDB  # noqa: E402
from tests.attachments.conftest import (  # noqa: E402
    attachment_plane_source,
    seed_attachment_for_test,
)


def _seed(db, *, user_id, attachment_id, category, extension, filename):
    repo = AttachmentRepository.from_plane_source(attachment_plane_source(db))
    seed_attachment_for_test(
        repo,
        attachment_id=attachment_id,
        user_id=user_id,
        filename=filename,
        content_type="application/octet-stream",
        category=category,
        extension=extension,
        size_bytes=10,
        sha256="0" * 64,
    )


def _fake_self(db):
    sent = []

    async def _safe_send(ws, data):
        sent.append(data)

    self_ns = types.SimpleNamespace(
        plane_repository_source=attachment_plane_source(db),
        _safe_send=_safe_send,
        _sent=sent,
    )
    return self_ns


@pytest.mark.asyncio
async def test_valid_attachments_are_linked_and_surfaced():
    db = FakeDB()
    _seed(db, user_id="u1", attachment_id="a-pdf", category="document", extension="pdf", filename="report.pdf")
    _seed(db, user_id="u1", attachment_id="a-pq", category="data", extension="parquet", filename="data.parquet")
    me = _fake_self(db)
    payload = [
        {"attachment_id": "a-pdf", "filename": "report.pdf", "category": "document"},
        {"attachment_id": "a-pq", "filename": "data.parquet", "category": "data"},
    ]
    out = await Orchestrator._attach_turn_attachments(
        me, object(), "summarize these", "c1", "u1", "m1", payload)

    assert "[Attachments on this turn]" in out
    assert 'id=a-pdf name="report.pdf" category=document (readable: read_document)' in out
    # uncovered data type → pending parser (drives US2)
    assert 'id=a-pq name="data.parquet" category=data (readable: pending parser)' in out
    # both linked to the persisted message m1
    links = db.message_attachment
    assert {r.attachment_id for r in links} == {"a-pdf", "a-pq"}
    assert all(r.message_id == "m1" and r.owner_id == "u1" for r in links)


@pytest.mark.asyncio
async def test_per_message_cap_of_ten():
    db = FakeDB()
    payload = []
    for i in range(12):
        aid = f"a{i}"
        _seed(db, user_id="u1", attachment_id=aid, category="text", extension="txt", filename=f"{aid}.txt")
        payload.append({"attachment_id": aid, "filename": f"{aid}.txt", "category": "text"})
    me = _fake_self(db)
    out = await Orchestrator._attach_turn_attachments(me, object(), "hi", "c1", "u1", "m1", payload)
    assert len(db.message_attachment) == 10
    assert out.count("readable: read_text") == 10


@pytest.mark.asyncio
async def test_duplicate_ids_collapse():
    db = FakeDB()
    _seed(db, user_id="u1", attachment_id="a1", category="text", extension="md", filename="n.md")
    me = _fake_self(db)
    payload = [
        {"attachment_id": "a1", "filename": "n.md", "category": "text"},
        {"attachment_id": "a1", "filename": "n.md", "category": "text"},
    ]
    await Orchestrator._attach_turn_attachments(me, object(), "x", "c1", "u1", "m1", payload)
    assert len(db.message_attachment) == 1


@pytest.mark.asyncio
async def test_turn_attachment_plane_reads_and_parser_coverage_stay_off_loop(
    monkeypatch,
):
    loop_thread = threading.get_ident()
    worker_threads = []
    attachment = types.SimpleNamespace(
        attachment_id="a1",
        filename="data.parquet",
        category="data",
        extension="parquet",
    )

    class _Attachments:
        def get_by_id(self, *_args):
            raise AssertionError("sync attachment read reached the event loop")

        async def aget_by_id(self, *_args):
            def _read():
                worker_threads.append(threading.get_ident())
                return attachment

            return await asyncio.to_thread(_read)

    class _Links:
        def insert(self, **_kwargs):
            raise AssertionError("sync attachment link reached the event loop")

        async def ainsert(self, **_kwargs):
            def _write():
                worker_threads.append(threading.get_ident())
                return "link-1"

            return await asyncio.to_thread(_write)

    class _Parsers:
        def get_by_gap(self, _fingerprint):
            worker_threads.append(threading.get_ident())
            return None

    monkeypatch.setattr(
        AttachmentRepository,
        "from_plane_source",
        classmethod(lambda _cls, _source: _Attachments()),
    )
    monkeypatch.setattr(
        MessageAttachmentRepository,
        "from_plane_source",
        classmethod(lambda _cls, _source: _Links()),
    )
    monkeypatch.setattr(
        AttachmentParserRepository,
        "from_plane_source",
        classmethod(lambda _cls, _source: _Parsers()),
    )
    me = types.SimpleNamespace(
        plane_repository_source=types.SimpleNamespace(),
        _safe_send=lambda *_args: None,
    )

    result = await Orchestrator._attach_turn_attachments(
        me,
        object(),
        "inspect",
        "c1",
        "u1",
        "m1",
        [{"attachment_id": "a1"}],
    )

    assert "id=a1" in result
    assert len(worker_threads) == 3
    assert all(thread_id != loop_thread for thread_id in worker_threads)
