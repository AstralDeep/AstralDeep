"""HistoryManager integration over the application-scoped Plane runtime."""

from __future__ import annotations

import json

import pytest

from tests.helpers.voice_plane_runtime import (
    history_manager,
    isolated_voice_plane_runtime,
)


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_voice_plane_runtime("history_database") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def history(plane_runtime):
    return history_manager(plane_runtime)


def test_runtime_initializes_plane_history_schema(plane_runtime, history) -> None:
    assert plane_runtime.repositories.history is history.plane_repositories.history
    assert plane_runtime.repositories.workspaces is history.plane_repositories.workspaces


def test_create_chat(history) -> None:
    chat_id = history.create_chat()
    record = history.get_conversation_record(chat_id, user_id="legacy")
    assert record is not None
    assert record.title == "New Chat"


def test_add_message_and_get_chat(history) -> None:
    chat_id = history.create_chat()

    history.add_message(chat_id, "user", "Hello World")
    history.add_message(chat_id, "assistant", {"response": "Hi there"})

    chat = history.get_chat(chat_id)
    assert len(chat["messages"]) == 2
    assert chat["messages"][0]["content"] == "Hello World"
    assert chat["messages"][1]["content"] == {"response": "Hi there"}
    assert isinstance(chat["messages"][1]["content"], dict)
    assert json.loads(json.dumps(chat))["messages"][1]["content"] == {
        "response": "Hi there"
    }
    assert chat["title"] == "Hello World"
