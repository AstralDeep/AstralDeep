"""Feature 031 — message_attachment + attachment_parser repositories (T023).

Covers turn→attachment linking + the dedup-safe global parser registry DAO.
"""

from __future__ import annotations

from orchestrator.attachments.message_attachment_repo import MessageAttachmentRepository
from orchestrator.attachments.parser_repo import (
    AttachmentParserRepository, STATUS_PENDING, STATUS_LIVE, STATUS_FAILED,
)
from tests.attachments._fake_db_031 import FakeDB


def test_message_attachment_insert_and_list_for_message():
    db = FakeDB()
    repo = MessageAttachmentRepository(db)
    repo.insert(chat_id="c1", attachment_id="a1", user_id="u1", message_id="m1")
    repo.insert(chat_id="c1", attachment_id="a2", user_id="u1", message_id="m1")
    repo.insert(chat_id="c1", attachment_id="a3", user_id="u2", message_id="m1")  # other user
    rows = repo.list_for_message("m1", "u1")
    assert [r["attachment_id"] for r in rows] == ["a1", "a2"]
    # ownership scoping: u2 only sees its own link.
    assert [r["attachment_id"] for r in repo.list_for_message("m1", "u2")] == ["a3"]


def test_message_attachment_list_for_chat():
    db = FakeDB()
    repo = MessageAttachmentRepository(db)
    repo.insert(chat_id="c1", attachment_id="a1", user_id="u1", message_id="m1")
    repo.insert(chat_id="c1", attachment_id="a2", user_id="u1", message_id="m2")
    assert len(repo.list_for_chat("c1", "u1")) == 2
    assert repo.list_for_chat("c1", "u2") == []


def test_list_for_chat_grouped_by_message_matches_per_message_reads():
    """The chat-scoped bulk read is the substitute for the per-message N+1.

    load_chat groups list_for_chat by message_id instead of issuing one
    list_for_message per user message, so the grouping must reproduce the
    per-message result exactly — same attachments, same order, same scoping.
    """
    db = FakeDB()
    repo = MessageAttachmentRepository(db)
    repo.insert(chat_id="c1", attachment_id="a1", user_id="u1", message_id="m1")
    repo.insert(chat_id="c1", attachment_id="a2", user_id="u1", message_id="m1")
    repo.insert(chat_id="c1", attachment_id="a3", user_id="u1", message_id="m2")
    repo.insert(chat_id="c1", attachment_id="a4", user_id="u2", message_id="m1")
    repo.insert(chat_id="c2", attachment_id="a5", user_id="u1", message_id="m3")

    grouped: dict = {}
    for link in repo.list_for_chat("c1", "u1"):
        grouped.setdefault(link["message_id"], []).append(link["attachment_id"])

    assert grouped == {"m1": ["a1", "a2"], "m2": ["a3"]}
    for message_id, attachment_ids in grouped.items():
        assert [
            r["attachment_id"] for r in repo.list_for_message(message_id, "u1")
        ] == attachment_ids
    # Another user's link and another chat's link never enter the grouping.
    assert "a4" not in grouped["m1"]
    assert "m3" not in grouped


def test_list_for_chat_reports_message_id_as_text():
    """Callers key the grouping on ``str(messages.id)``.

    ``messages.id`` is an integer PK but ``message_attachment.message_id`` is
    TEXT, so insert stores the text form and reads must return it — otherwise
    the bulk grouping silently matches nothing.
    """
    db = FakeDB()
    repo = MessageAttachmentRepository(db)
    repo.insert(chat_id="c1", attachment_id="a1", user_id="u1", message_id=4242)

    (link,) = repo.list_for_chat("c1", "u1")
    assert link["message_id"] == "4242"


def test_parser_repo_create_pending_is_dedup_safe():
    db = FakeDB()
    repo = AttachmentParserRepository(db)
    row1 = repo.create_pending(
        gap_fingerprint="gap1", category="data", extension="parquet",
        draft_agent_id="d1", source_attachment_id="a1", source_chat_id="c1",
        requested_by="u1",
    )
    assert row1["status"] == STATUS_PENDING
    # Second create for the same gap returns the SAME row (no duplicate draft).
    row2 = repo.create_pending(
        gap_fingerprint="gap1", category="data", extension="parquet",
        draft_agent_id="d2", source_attachment_id="a9", source_chat_id="c9",
        requested_by="u2",
    )
    assert row2["id"] == row1["id"]
    assert len(db.attachment_parser) == 1


def test_parser_repo_mark_live_and_lookup():
    db = FakeDB()
    repo = AttachmentParserRepository(db)
    repo.create_pending(
        gap_fingerprint="gap2", category="archive", extension="zip",
        draft_agent_id="d1", source_attachment_id="a1", source_chat_id="c1",
        requested_by="u1",
    )
    repo.mark_live("gap2", live_agent_id="parser-zip-1", tool_name="parse_zip",
                   approved_by="admin1")
    row = repo.get_by_gap("gap2")
    assert row["status"] == STATUS_LIVE
    assert row["live_agent_id"] == "parser-zip-1"
    assert row["tool_name"] == "parse_zip"
    assert row["approved_by"] == "admin1"
    assert repo.get_by_draft("d1")["gap_fingerprint"] == "gap2"


def test_parser_repo_mark_status_and_list():
    db = FakeDB()
    repo = AttachmentParserRepository(db)
    repo.create_pending(gap_fingerprint="g", category="data", extension="avro",
                        draft_agent_id=None, source_attachment_id=None,
                        source_chat_id=None, requested_by="u1")
    repo.mark_status("g", STATUS_FAILED)
    assert repo.get_by_gap("g")["status"] == STATUS_FAILED
    assert [r["gap_fingerprint"] for r in repo.list_by_status(STATUS_FAILED)] == ["g"]
    assert repo.list_by_status(STATUS_PENDING) == []
