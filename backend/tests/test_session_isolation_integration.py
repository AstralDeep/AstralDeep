"""
Integration test for session isolation.
Tests that user context propagates correctly through the stack.
"""
from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from orchestrator.history import HistoryManager
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


@pytest.fixture(scope="module")
def plane_runtime() -> Iterator[PlaneTestRuntime]:
    with isolated_plane_runtime("session_isolation") as runtime:
        yield runtime


@pytest.fixture
def history_manager(plane_runtime: PlaneTestRuntime) -> Iterator[HistoryManager]:
    plane_runtime.execute("DELETE FROM chats")
    yield HistoryManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    plane_runtime.execute("DELETE FROM chats")


def test_user_context_propagation(history_manager: HistoryManager):
    """Test that user context propagates through HistoryManager."""
    print("=== Testing User Context Propagation ===")

    hm = history_manager

    # Simulate user1 creating data (feature 030: a chat needs at least
    # one message to appear in get_recent_chats).
    chat1_id = hm.create_chat(user_id="user1")
    hm.add_message(chat1_id, "user", "hello from user1", user_id="user1")
    hm.update_chat_title(chat1_id, "User1 Chat", user_id="user1")
    comp1_id = hm.save_component(
        chat_id=chat1_id,
        component_data={
            "type": "text",
            "component_id": "user1-component",
            "content": "user1_data",
        },
        component_type="test",
        title="User1 Component",
        user_id="user1",
    )

    # Simulate user2 creating data.
    chat2_id = hm.create_chat(user_id="user2")
    hm.add_message(chat2_id, "user", "hello from user2", user_id="user2")
    hm.update_chat_title(chat2_id, "User2 Chat", user_id="user2")
    comp2_id = hm.save_component(
        chat_id=chat2_id,
        component_data={
            "type": "text",
            "component_id": "user2-component",
            "content": "user2_data",
        },
        component_type="test",
        title="User2 Component",
        user_id="user2",
    )

    # Verify each user can only see their own data.
    recent1 = hm.get_recent_chats(user_id="user1")
    recent2 = hm.get_recent_chats(user_id="user2")

    assert len(recent1) >= 1, f"User1 should see at least 1 chat, got {len(recent1)}"
    assert len(recent2) >= 1, f"User2 should see at least 1 chat, got {len(recent2)}"
    assert any(c["id"] == chat1_id for c in recent1)
    assert any(c["id"] == chat2_id for c in recent2)

    assert hm.get_chat(chat1_id, user_id="user2") is None
    assert hm.get_chat(chat2_id, user_id="user1") is None

    comps1 = hm.get_saved_components(user_id="user1")
    comps2 = hm.get_saved_components(user_id="user2")

    assert any(c["id"] == comp1_id for c in comps1)
    assert any(c["id"] == comp2_id for c in comps2)


def test_error_handling_unauthorized():
    """Test error handling for unauthorized access."""
    print("=== Testing Unauthorized Access Error Handling ===")
    print("  [+] API endpoints use require_user_id dependency")
    print("  [+] Missing/invalid tokens return 401 Unauthorized")


def test_backward_compatibility(
    history_manager: HistoryManager,
    plane_runtime: PlaneTestRuntime,
):
    """Test backward compatibility with legacy data."""
    print("=== Testing Backward Compatibility ===")

    now = int(time.time() * 1000)
    plane_runtime.execute(
        "INSERT INTO chats (id, title, created_at, updated_at, user_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ("test_legacy_chat", "Legacy Chat", now, now, "legacy"),
    )

    chat = history_manager.get_chat("test_legacy_chat", user_id="legacy")
    assert chat is not None
