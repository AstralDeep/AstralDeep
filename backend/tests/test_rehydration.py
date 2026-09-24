"""Tests for orchestrator/history.py, orchestrator.py and workspace.py's chat-load
rehydration against Postgres: the workspace round-trips through a fresh manager,
transcripts render only text, and canvas context lists restored components.
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
    with isolated_plane_runtime("rehydration") as runtime:
        yield runtime


@pytest.fixture
def chat_env(plane_runtime):
    history = history_manager(plane_runtime)
    user_id = f"test-user-{uuid.uuid4()}"
    chat_id = history.create_chat(user_id=user_id)
    yield history, user_id, chat_id
    history.delete_chat(chat_id, user_id=user_id)


def _seed_two_components(workspace, chat_id, user_id):
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


def test_workspace_round_trips_through_fresh_manager(chat_env, tmp_path):
    history, user_id, chat_id = chat_env
    workspace = WorkspaceManager(history)
    cid1, cid2 = _seed_two_components(workspace, chat_id, user_id)
    assert cid1 != cid2

    fresh_history = HistoryManager(
        data_dir=str(tmp_path / "fresh"),
        plane_runtime=history.plane_runtime,
        plane_repositories=history.plane_repositories,
    )
    fresh_workspace = WorkspaceManager(fresh_history)

    restored = fresh_workspace.live_components(chat_id, user_id)
    assert [c["component_id"] for c in restored] == [cid1, cid2]
    assert restored[0]["type"] == "table"
    assert restored[0]["rows"] == [["Alice"], ["Bob"]]
    assert restored[1]["type"] == "metric"
    assert restored[1]["value"] == "42"
    assert restored[0]["_source_tool"] == "list_patients"
    assert restored[1]["_source_agent"] == "agent-y"
    assert fresh_workspace.live_components(chat_id, f"other-{uuid.uuid4()}") == []


def test_transcript_renders_only_text_not_rich_components(chat_env):
    from orchestrator.orchestrator import Orchestrator
    history, user_id, chat_id = chat_env
    history.add_message(chat_id, "user", "show me my labs", user_id=user_id)
    history.add_message(chat_id, "assistant", [
        {"type": "alert", "message": "Lab results ready", "variant": "info"},
        {"type": "table", "title": "Labs", "headers": ["Test"], "rows": [["A1C"]]},
    ], user_id=user_id)

    chat = history.get_chat(chat_id, user_id=user_id)
    assert chat is not None

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

    html = comp_msg.get("html")
    assert html and "Lab results ready" in html
    assert "A1C" not in html and "<table" not in html

    assert "html" not in text_msg
    assert text_msg["content"] == "show me my labs"


def test_transcript_pure_rich_message_gets_no_html(chat_env):
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


def test_canvas_context_lists_the_restored_component_ids(chat_env):
    history, user_id, chat_id = chat_env
    workspace = WorkspaceManager(history)
    cid1, cid2 = _seed_two_components(workspace, chat_id, user_id)

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
    visible_ids = [c["component_id"] for c in workspace.live_components(chat_id, user_id)]
    assert visible_ids == [cid1, cid2]
    for cid in visible_ids:
        assert f"component_id: {cid} " in canvas_context
    assert "Tool: list_patients | Agent: agent-x" in canvas_context
    assert "Tool: average_age | Agent: agent-y" in canvas_context
    assert "Title: Patients" in canvas_context
    assert "Type: metric" in canvas_context
