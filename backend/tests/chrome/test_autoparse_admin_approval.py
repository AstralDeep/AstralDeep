"""Tests for orchestrator/agentic_creation.py's admin-gated approval of auto-created
parser drafts: a non-admin is refused and audited, an admin passes through to
approve_agent, and ordinary drafts skip the admin gate.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import agentic_creation  # noqa: E402
from tests.helpers.draft_store_double import InMemoryDraftStore  # noqa: E402


class _FakeLifecycle:
    def __init__(self, draft_store: InMemoryDraftStore):
        self.draft_store = draft_store
        self.approve_called_with = None

    async def approve_agent(self, draft_id, websocket=None):
        self.approve_called_with = draft_id
        return {"status": "pending_review"}


def _fake_orch(draft):
    sent = []

    async def send_ui_render(ws, components, target=None):
        sent.append((target, components))

    draft_values = dict(draft)
    draft_values["draft_id"] = draft_values.pop("id")
    draft_values.setdefault("description", "Parser draft awaiting approval")
    draft_store = InMemoryDraftStore()
    draft_store.create_draft_agent(**draft_values)
    lifecycle = _FakeLifecycle(draft_store)
    orch = types.SimpleNamespace(
        lifecycle_manager=lifecycle,
        send_ui_render=send_ui_render,
        _ws_active_chat={},
        _sent=sent,
        _lifecycle=lifecycle,
    )
    return orch


_DRAFT = {"id": "d-parquet", "origin": "auto_attachment", "agent_slug": "parquet_parser",
          "agent_name": "PARQUET Parser", "user_id": "uploader"}


@pytest.mark.asyncio
async def test_non_admin_cannot_approve_parser_draft():
    orch = _fake_orch(_DRAFT)
    await agentic_creation._h_draft_approve(
        orch, object(), user_id="uploader", roles=["user"], payload={"draft_id": "d-parquet"})
    assert orch._lifecycle.approve_called_with is None
    assert orch._sent, "expected a refusal card to be sent"


@pytest.mark.asyncio
async def test_admin_passes_the_gate():
    orch = _fake_orch(_DRAFT)
    await agentic_creation._h_draft_approve(
        orch, object(), user_id="some-admin", roles=["admin"], payload={"draft_id": "d-parquet"})
    assert orch._lifecycle.approve_called_with == "d-parquet"


@pytest.mark.asyncio
async def test_non_auto_attachment_draft_uses_ownership_not_admin():
    normal = {"id": "d-normal", "origin": "auto_chat", "agent_slug": "x_agent",
              "agent_name": "X", "user_id": "owner"}
    orch = _fake_orch(normal)
    await agentic_creation._h_draft_approve(
        orch, object(), user_id="owner", roles=["user"], payload={"draft_id": "d-normal"})
    assert orch._lifecycle.approve_called_with == "d-normal"


@pytest.mark.asyncio
async def test_promoting_a_parser_tells_the_requester_to_ask_again(monkeypatch):
    from orchestrator import attachment_autoparse

    class _Repo:
        def get_by_draft(self, draft_id, for_administration=False):
            return {"gap_fingerprint": "gap-1", "extension": "parquet", "requested_by": "uploader"}

        def mark_live(self, gap, **kwargs):
            self.live = (gap, kwargs)

    repo = _Repo()
    notices = []

    async def notify(orch, user_id, message, chat_id=None):
        notices.append((user_id, message))

    monkeypatch.setattr(agentic_creation, "_parser_repository", lambda orch: repo)
    monkeypatch.setattr(attachment_autoparse, "_notify_user", notify)
    orch = _fake_orch(_DRAFT)
    orch.tool_permissions = types.SimpleNamespace(
        scopes_required_by_tools=lambda agent_id: [], set_agent_scopes=lambda *a: None)

    await agentic_creation._promote_parser_global(
        orch, _DRAFT, "parquet-parser-1", approved_by="some-admin")

    assert repo.live[0] == "gap-1"
    assert notices == [("uploader", "The .parquet reader is live — ask again to read your file.")]
