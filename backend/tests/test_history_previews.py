"""Tests for chat-history listing (orchestrator/history.py) over Plane: component-list
message previews flatten to text without leaking reprs, and zero-message chats are
excluded until their first message lands.
"""

import uuid

import pytest

from orchestrator.history import HistoryManager, PREVIEW_MAX_CHARS
from tests.helpers.voice_plane_runtime import (
    history_manager,
    isolated_voice_plane_runtime,
)


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_voice_plane_runtime("history_previews") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def hm(plane_runtime) -> HistoryManager:
    return history_manager(plane_runtime)


@pytest.fixture
def user_id():
    return f"hist-prev-{uuid.uuid4().hex[:12]}"


def _listing_entry(hm, user_id, chat_id):
    chats = hm.get_recent_chats(user_id=user_id)
    return next((c for c in chats if c["id"] == chat_id), None)


def test_component_list_preview_extracts_text_content(hm, user_id):
    chat_id = hm.create_chat(user_id=user_id)
    hm.add_message(
        chat_id,
        "assistant",
        [{"type": "text", "content": "This system can help you orchestrate agents.", "variant": "markdown"}],
        user_id=user_id,
    )

    entry = _listing_entry(hm, user_id, chat_id)
    assert entry is not None
    assert entry["preview"] == "This system can help you orchestrate agents."
    assert "{" not in entry["preview"]
    assert "[" not in entry["preview"]


def test_component_list_preview_title_fallback(hm, user_id):
    chat_id = hm.create_chat(user_id=user_id)
    hm.add_message(
        chat_id,
        "assistant",
        [{"type": "chart", "title": "Revenue by Quarter", "data": {"x": [1, 2], "y": [3, 4]}}],
        user_id=user_id,
    )

    entry = _listing_entry(hm, user_id, chat_id)
    assert entry["preview"] == "Revenue by Quarter"
    assert "{" not in entry["preview"]


def test_component_list_preview_mixed_components(hm, user_id):
    chat_id = hm.create_chat(user_id=user_id)
    hm.add_message(
        chat_id,
        "assistant",
        [
            {"type": "text", "content": "Here are your results.", "variant": "markdown"},
            {"type": "table", "title": "ETF Holdings", "rows": [["VTI", "60%"]]},
            {"type": "chart", "data": {"x": [1], "y": [2]}},
            "as requested.",
            42,
        ],
        user_id=user_id,
    )

    entry = _listing_entry(hm, user_id, chat_id)
    assert entry["preview"] == "Here are your results. ETF Holdings as requested."
    assert "{" not in entry["preview"]


def test_component_preview_collapses_whitespace_and_truncates(hm, user_id):
    chat_id = hm.create_chat(user_id=user_id)
    long_text = "Lorem  ipsum\n\ndolor sit amet. " * 20
    hm.add_message(
        chat_id,
        "assistant",
        [{"type": "text", "content": long_text, "variant": "markdown"}],
        user_id=user_id,
    )

    entry = _listing_entry(hm, user_id, chat_id)
    preview = entry["preview"]
    assert "\n" not in preview
    assert "  " not in preview
    assert preview.endswith("...")
    assert len(preview) == PREVIEW_MAX_CHARS + 3
    assert preview.startswith("Lorem ipsum dolor sit amet.")


def test_plain_string_preview_passthrough(hm, user_id):
    chat_id = hm.create_chat(user_id=user_id)
    hm.add_message(chat_id, "user", "What is the weather in Lexington?", user_id=user_id)

    entry = _listing_entry(hm, user_id, chat_id)
    assert entry["preview"] == "What is the weather in Lexington?"


def test_plain_string_preview_truncates(hm, user_id):
    chat_id = hm.create_chat(user_id=user_id)
    long_text = "z" * (PREVIEW_MAX_CHARS * 2)
    hm.add_message(chat_id, "user", long_text, user_id=user_id)

    entry = _listing_entry(hm, user_id, chat_id)
    assert entry["preview"] == "z" * PREVIEW_MAX_CHARS + "..."


def test_scalar_json_content_stringified(hm, user_id):
    chat_id = hm.create_chat(user_id=user_id)
    hm.add_message(chat_id, "assistant", 123, user_id=user_id)

    entry = _listing_entry(hm, user_id, chat_id)
    assert entry["preview"] == "123"


def test_dict_content_never_leaks_repr(hm, user_id):
    chat_id = hm.create_chat(user_id=user_id)
    hm.add_message(chat_id, "assistant", {"response": "Hi there"}, user_id=user_id)

    entry = _listing_entry(hm, user_id, chat_id)
    assert entry is not None
    assert entry["preview"] == ""
    assert "{" not in entry["preview"]


def test_empty_chat_excluded_from_listing(hm, user_id):
    chat_id = hm.create_chat(user_id=user_id)
    assert _listing_entry(hm, user_id, chat_id) is None
    assert hm.get_chat(chat_id, user_id=user_id) is not None


def test_chat_listed_as_soon_as_first_message_lands(hm, user_id):
    chat_id = hm.create_chat(user_id=user_id)
    assert _listing_entry(hm, user_id, chat_id) is None

    hm.add_message(chat_id, "user", "hello", user_id=user_id)

    entry = _listing_entry(hm, user_id, chat_id)
    assert entry is not None
    assert entry["preview"] == "hello"
    assert set(entry.keys()) == {
        "id", "title", "agent_id", "updated_at", "preview", "has_saved_components",
    }
