"""Feature 028 — workspace re-hydration on chat load (FR-027..FR-029).

Proves the three load-time guarantees over a REAL Postgres-backed
``HistoryManager``/``WorkspaceManager``:

- FR-027: the persisted workspace round-trips through a completely fresh
  manager stack — restore needs no capability re-execution.
- FR-028: component-bearing transcript messages get a meaningful
  server-rendered ``html`` form (the ``chat_loaded`` additive field), while
  plain-string messages are untouched by that path.
- FR-029: the LLM's canvas context block is built from the SAME workspace
  rows (and stable component_ids) the user actually sees.

The transcript/canvas loops replicate the orchestrator's ``load_chat`` and
system-prompt blocks verbatim (orchestrator.py — search "FR-028" and
"COMPONENTS CURRENTLY ON CANVAS").
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.history import HistoryManager
from orchestrator.workspace import WorkspaceManager
from tests.helpers.voice_plane_runtime import (
    history_manager,
    isolated_plane_runtime,
)


@pytest.fixture(scope="module")
def plane_runtime():
    """One managed application Plane runtime for this integration module."""
    with isolated_plane_runtime("rehydration") as runtime:
        yield runtime


@pytest.fixture
def chat_env(plane_runtime):
    """Real HistoryManager + unique user/chat; chat deleted on teardown
    (FK CASCADE clears messages, saved_components, workspace_snapshot)."""
    history = history_manager(plane_runtime)
    user_id = f"test-user-{uuid.uuid4()}"
    chat_id = history.create_chat(user_id=user_id)
    yield history, user_id, chat_id
    history.delete_chat(chat_id, user_id=user_id)


def _seed_two_components(workspace, chat_id, user_id):
    """Upsert two distinct-provenance components; returns their ids in order."""
    comp1 = {
        "type": "table", "title": "Patients",
        "headers": ["Name"], "rows": [["Alice"], ["Bob"]],
        "_source_agent": "agent-x", "_source_tool": "list_patients",
        "_source_params": {"page": 1},
    }
    comp2 = {
        "type": "metric", "title": "Average Age", "value": "42",
        "_source_agent": "agent-y", "_source_tool": "average_age",
        "_source_params": {"cohort": "all"},
    }
    ops = workspace.upsert(chat_id, user_id, [comp1, comp2])
    assert len(ops) == 2
    return ops[0]["component_id"], ops[1]["component_id"]


# ---------------------------------------------------------------------------
# FR-027 — restore from persisted state, no re-execution
# ---------------------------------------------------------------------------

def test_workspace_round_trips_through_fresh_manager(chat_env, tmp_path):
    """028 FR-027: re-opening a chat restores the workspace exactly as left,
    purely from persisted state — a FRESH WorkspaceManager over a FRESH
    HistoryManager returns both components with their stable component_ids
    and original order, with no capability execution involved."""
    history, user_id, chat_id = chat_env
    workspace = WorkspaceManager(history)
    cid1, cid2 = _seed_two_components(workspace, chat_id, user_id)
    assert cid1 != cid2

    # Brand-new manager stack — nothing cached in memory.
    fresh_history = HistoryManager(
        data_dir=str(tmp_path / "fresh"),
        plane_runtime=history.plane_runtime,
        plane_repositories=history.plane_repositories,
    )
    fresh_workspace = WorkspaceManager(fresh_history)

    restored = fresh_workspace.live_components(chat_id, user_id)
    assert [c["component_id"] for c in restored] == [cid1, cid2]  # order kept
    assert restored[0]["type"] == "table"
    assert restored[0]["rows"] == [["Alice"], ["Bob"]]
    assert restored[1]["type"] == "metric"
    assert restored[1]["value"] == "42"
    # Provenance survives the round trip — actions stay refreshable.
    assert restored[0]["_source_tool"] == "list_patients"
    assert restored[1]["_source_agent"] == "agent-y"
    # And nothing for another user leaks in.
    assert fresh_workspace.live_components(chat_id, f"other-{uuid.uuid4()}") == []


# ---------------------------------------------------------------------------
# FR-028 — component-bearing transcript messages render meaningfully
# ---------------------------------------------------------------------------

def test_transcript_renders_only_text_not_rich_components(chat_env):
    """045 (was 028 FR-028): a component-bearing transcript message renders
    ONLY its text primitives — rich components (tables/charts/metrics) are
    dropped from the chat rail and shown on the canvas instead. Plain-string
    messages are untouched by that path."""
    from orchestrator.orchestrator import Orchestrator
    history, user_id, chat_id = chat_env
    history.add_message(chat_id, "user", "show me my labs", user_id=user_id)
    history.add_message(chat_id, "assistant", [
        {"type": "alert", "message": "Lab results ready", "variant": "info"},
        {"type": "table", "title": "Labs", "headers": ["Test"], "rows": [["A1C"]]},
    ], user_id=user_id)

    chat = history.get_chat(chat_id, user_id=user_id)
    assert chat is not None

    # Replicate the orchestrator's load_chat 045 block: text-only transcript html.
    for m in chat.get("messages", []):
        if not isinstance(m.get("content"), str) and isinstance(
            m.get("content"), (list, tuple)
        ):
            _h = Orchestrator._transcript_html(m["content"])
            if _h:
                m["html"] = _h

    messages = chat["messages"]
    text_msg = next(m for m in messages if isinstance(m["content"], str))
    comp_msg = next(m for m in messages if isinstance(m["content"], (list, tuple)))

    # Component message: the text primitive renders; the rich table does NOT.
    html = comp_msg.get("html")
    assert html and "Lab results ready" in html
    assert "A1C" not in html and "<table" not in html

    # Plain-string message: untouched — no html field, content as written.
    assert "html" not in text_msg
    assert text_msg["content"] == "show me my labs"


def test_transcript_pure_rich_message_gets_no_html(chat_env):
    """045: a message whose content is only rich components yields no
    transcript html at all — the client renders no chat bubble for it (the
    components live on the canvas, re-hydrated from the workspace)."""
    from orchestrator.orchestrator import Orchestrator
    history, user_id, chat_id = chat_env
    history.add_message(chat_id, "assistant", [
        {"type": "table", "title": "Labs", "headers": ["Test"], "rows": [["A1C"]]},
        {"type": "metric", "title": "A1C", "value": "5.4"},
    ], user_id=user_id)
    chat = history.get_chat(chat_id, user_id=user_id)
    comp_msg = next(
        m for m in chat["messages"] if isinstance(m["content"], (list, tuple))
    )
    assert Orchestrator._transcript_html(comp_msg["content"]) == ""


# ---------------------------------------------------------------------------
# FR-029 — LLM canvas context matches what the user sees
# ---------------------------------------------------------------------------

def test_canvas_context_lists_the_restored_component_ids(chat_env):
    """028 FR-029: the canvas prompt block is built from workspace.live_rows,
    so every component_id (and provenance) the user sees on the restored
    canvas appears in the assistant's context — follow-ups update in place
    instead of duplicating."""
    history, user_id, chat_id = chat_env
    workspace = WorkspaceManager(history)
    cid1, cid2 = _seed_two_components(workspace, chat_id, user_id)

    # Replicate the orchestrator's canvas_context construction verbatim
    # (orchestrator.py — "COMPONENTS CURRENTLY ON CANVAS").
    canvas_saved = workspace.live_rows(chat_id, user_id=user_id) if chat_id else []
    canvas_context = ""
    if canvas_saved:
        canvas_context = "\nCOMPONENTS CURRENTLY ON CANVAS:\n"
        for sc in canvas_saved:
            cd = sc.get("component_data", {})
            if not isinstance(cd, dict):
                cd = {}
            source_tool = cd.get("_source_tool", "unknown")
            source_agent = cd.get("_source_agent", "unknown")
            canvas_context += (
                f"- component_id: {sc.get('component_id') or sc['id']} | Title: {sc['title']} "
                f"| Type: {sc['component_type']} | Tool: {source_tool} | Agent: {source_agent}\n"
            )

    assert canvas_context.startswith("\nCOMPONENTS CURRENTLY ON CANVAS:\n")
    # Every id the user's canvas shows is named in the assistant's context.
    visible_ids = [c["component_id"] for c in workspace.live_components(chat_id, user_id)]
    assert visible_ids == [cid1, cid2]
    for cid in visible_ids:
        assert f"component_id: {cid} " in canvas_context
    # Provenance the upsert path matches on rides along.
    assert "Tool: list_patients | Agent: agent-x" in canvas_context
    assert "Tool: average_age | Agent: agent-y" in canvas_context
    assert "Title: Patients" in canvas_context
    assert "Type: metric" in canvas_context
