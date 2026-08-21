"""Feature 031 US2 — admin-gated approval of auto-created parser drafts (T035).

A non-admin cannot promote an ``auto_attachment`` parser draft (the approval
gate refuses and audits, and approve_agent is never called); an admin passes
the gate through to approve_agent. Covers FR-015.
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
        # Return a non-live status so the heavy global-promotion path is skipped.
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
    # Gated: approve_agent never reached.
    assert orch._lifecycle.approve_called_with is None
    # An error/explanation card was surfaced.
    assert orch._sent, "expected a refusal card to be sent"


@pytest.mark.asyncio
async def test_admin_passes_the_gate():
    orch = _fake_orch(_DRAFT)
    await agentic_creation._h_draft_approve(
        orch, object(), user_id="some-admin", roles=["admin"], payload={"draft_id": "d-parquet"})
    # Gate passed → approve_agent invoked with the draft id.
    assert orch._lifecycle.approve_called_with == "d-parquet"


@pytest.mark.asyncio
async def test_non_auto_attachment_draft_uses_ownership_not_admin():
    # A normal (027) draft owned by the caller still approves without admin.
    normal = {"id": "d-normal", "origin": "auto_chat", "agent_slug": "x_agent",
              "agent_name": "X", "user_id": "owner"}
    orch = _fake_orch(normal)
    await agentic_creation._h_draft_approve(
        orch, object(), user_id="owner", roles=["user"], payload={"draft_id": "d-normal"})
    assert orch._lifecycle.approve_called_with == "d-normal"
