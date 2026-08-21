"""031 T031 — auto-continue the original turn once a parser goes live."""
import asyncio
import sys
import types
from contextlib import contextmanager
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import attachment_autoparse  # noqa: E402
from astralplane.repositories.artifacts import (  # noqa: E402
    AttachmentRecord,
    MessageAttachmentRecord,
)
from astralplane.repositories.history import MessageRecord  # noqa: E402


class _Runtime:
    @contextmanager
    def transaction(self):
        yield object()


class _MessageAttachments:
    def __init__(self, link=True, content="summarize this file"):
        self._link = link
        self._content = content

    def list_for_conversation(self, _query, *, owner_id, conversation_id):
        if not self._link:
            return ()
        return (
            MessageAttachmentRecord(
                link_id="link-1",
                conversation_id=conversation_id,
                message_id="1",
                attachment_id="a1",
                owner_id=owner_id,
                created_at=1,
            ),
        )


class _Messages:
    def __init__(self, content):
        self.content = content

    def get(self, _query, *, owner_id, conversation_id, message_id):
        if message_id != 1:
            return None
        return MessageRecord(
            message_id=1,
            conversation_id=conversation_id,
            owner_id=owner_id,
            role="user",
            content=self.content,
            timestamp=1,
            publication_id=None,
            commit_position=None,
            committed_render_revision=None,
        )


class _Attachments:
    def get(self, _query, *, owner_id, attachment_id):
        return AttachmentRecord(
            attachment_id=attachment_id,
            owner_id=owner_id,
            filename="data.xyz",
            content_type="application/octet-stream",
            category="data",
            extension="xyz",
            size_bytes=1,
            sha256="0" * 64,
            storage_locator="opaque",
            created_at=1,
        )


def _machine_seam(ns):
    """056 US2: model the orchestrator's machine-turn authority seam on a
    SimpleNamespace stand-in (no durable consent in tests → AuthoritySkip, so
    the turn runs unbound exactly as it does in dev posture)."""
    async def derive_machine_authority(**kwargs):
        from orchestrator.chain_authority import AuthoritySkip
        return AuthoritySkip("missing_consent", "test double")
    ns.derive_machine_authority = derive_machine_authority
    ns._bind_machine_turn = lambda vws, authority: None
    ns._unbind_machine_turn = lambda vws: None
    return ns


def _orch(link, calls, *, content="summarize this file"):
    async def handle_chat_message(ws, message, chat_id, *, user_id=None, attachments=None, **kw):
        calls.append({"message": message, "chat_id": chat_id,
                      "user_id": user_id, "attachments": attachments})

    return _machine_seam(types.SimpleNamespace(
        runtime_composition=types.SimpleNamespace(
            plane=types.SimpleNamespace(
                runtime=_Runtime(),
                repositories=types.SimpleNamespace(
                    artifacts=types.SimpleNamespace(
                        message_attachments=_MessageAttachments(link, content),
                        attachments=_Attachments(),
                    ),
                    history=types.SimpleNamespace(messages=_Messages(content)),
                ),
            )
        ),
        handle_chat_message=handle_chat_message,
    ))


def test_auto_continue_replays_original_turn():
    calls = []
    orch = _orch(True, calls)
    ok = asyncio.run(attachment_autoparse.auto_continue_after_go_live(
        orch, requested_by="u1", source_chat_id="c1", source_attachment_id="a1",
        extension="xyz", category="data"))
    assert ok is True
    assert len(calls) == 1
    assert calls[0]["message"] == "summarize this file"  # original (un-augmented) text
    assert calls[0]["chat_id"] == "c1"
    assert calls[0]["user_id"] == "u1"
    assert calls[0]["attachments"][0]["attachment_id"] == "a1"


def test_auto_continue_returns_false_when_no_link():
    calls = []
    orch = _orch(False, calls)
    ok = asyncio.run(attachment_autoparse.auto_continue_after_go_live(
        orch, requested_by="u1", source_chat_id="c1", source_attachment_id="a1",
        extension="xyz", category="data"))
    assert ok is False
    assert calls == []


def test_auto_continue_returns_false_on_missing_args():
    ok = asyncio.run(attachment_autoparse.auto_continue_after_go_live(
        None, requested_by=None, source_chat_id=None, source_attachment_id=None,
        extension="x", category="data"))
    assert ok is False
