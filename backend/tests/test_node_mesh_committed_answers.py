"""Exercises local history publication through the Plane transaction.
Local writes remain available to history readers without creating signed Kernel turns."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from orchestrator import node_mesh_chat
from orchestrator.history import ConversationNotFound
from tests import test_node_mesh_peer_tools as peer_tools

node_host = peer_tools.node_host


@pytest.fixture
def mesh_bridge(monkeypatch):
    bridge = Mock()
    monkeypatch.setattr(node_mesh_chat, "_bridge", bridge)
    yield bridge
    assert bridge.mock_calls == []


async def test_direct_history_writes_stay_local(node_host, mesh_bridge):
    history = node_host.orch.history
    chat = await asyncio.to_thread(history.create_chat, user_id=node_host.owner)
    await asyncio.to_thread(history.add_message, chat, "user", "question", node_host.owner)
    await asyncio.to_thread(history.add_message, chat, "assistant", "answer", node_host.owner)
    stored = await asyncio.to_thread(history.get_chat, chat, node_host.owner)
    assert [m["content"] for m in stored["messages"]] == ["question", "answer"]


async def test_aborted_staged_messages_stay_unpublished(node_host, mesh_bridge):
    orch = node_host.orch
    chat = await asyncio.to_thread(orch.history.create_chat, user_id=node_host.owner)
    stage = await asyncio.to_thread(
        orch.conversation_commits.stage_commit,
        chat_id=chat, owner_user_id=node_host.owner, request_generation=str(uuid4()),
    )
    await asyncio.to_thread(
        orch.conversation_commits.prepare_canvas_stage, commit_id=stage["commit_id"], owner_user_id=node_host.owner,
    )
    await asyncio.to_thread(
        orch.conversation_commits.append_staged_message,
        commit_id=stage["commit_id"], owner_user_id=node_host.owner, role="assistant", content="unpublished answer",
    )
    before = await asyncio.to_thread(orch.history.get_chat, chat, node_host.owner)
    assert "unpublished answer" not in json.dumps(before)
    await asyncio.to_thread(
        orch.conversation_commits.abort_commit, commit_id=stage["commit_id"], owner_user_id=node_host.owner,
    )
    after = await asyncio.to_thread(orch.history.get_chat, chat, node_host.owner)
    assert "unpublished answer" not in json.dumps(after)


@pytest.mark.parametrize("prepared", [False, True])
async def test_committed_messages_stay_local_and_replay_once(node_host, mesh_bridge, prepared):
    orch = node_host.orch
    chat = await asyncio.to_thread(orch.history.create_chat, user_id=node_host.owner)
    stage = await asyncio.to_thread(
        orch.conversation_commits.stage_commit,
        chat_id=chat, owner_user_id=node_host.owner, request_generation=str(uuid4()),
    )
    answer = [{"type": "text", "content": "answer"}]
    messages = [{"role": "user", "content": "question"}, {"role": "assistant", "content": answer}]
    if prepared:
        for message in messages:
            await asyncio.to_thread(
                orch.conversation_commits.append_staged_message,
                commit_id=stage["commit_id"], owner_user_id=node_host.owner, **message,
            )
    for _ in range(2):
        await asyncio.to_thread(
            orch.conversation_commits.publish_commit,
            commit_id=stage["commit_id"], owner_user_id=node_host.owner,
            messages=None if prepared else messages, canvas_components=[],
        )
    stored = await asyncio.to_thread(orch.history.get_chat, chat, node_host.owner)
    assert [(m["role"], m["content"]) for m in stored["messages"]] == [("user", "question"), ("assistant", answer)]


@pytest.mark.parametrize("completion_state", ["failed", "cancelled"])
async def test_an_incomplete_publication_does_not_answer_a_mesh_request(node_host, mesh_bridge, completion_state):
    orch = node_host.orch
    chat = await asyncio.to_thread(orch.history.create_chat, user_id=node_host.owner)
    stage = await asyncio.to_thread(
        orch.conversation_commits.stage_commit,
        chat_id=chat, owner_user_id=node_host.owner, request_generation=str(uuid4()),
    )
    await asyncio.to_thread(
        orch.conversation_commits.publish_commit,
        commit_id=stage["commit_id"], owner_user_id=node_host.owner,
        messages=[{"role": "assistant", "content": "partial answer"}], canvas_components=[],
        completion_state=completion_state,
    )


@pytest.mark.parametrize("failure", ["owner", "transaction"])
async def test_a_rejected_publication_never_notifies_the_mesh(node_host, mesh_bridge, failure):
    orch = node_host.orch
    chat = await asyncio.to_thread(orch.history.create_chat, user_id=node_host.owner)
    stage = await asyncio.to_thread(
        orch.conversation_commits.stage_commit,
        chat_id=chat, owner_user_id=node_host.owner, request_generation=str(uuid4()),
    )

    def fault(_point):
        raise RuntimeError("publication transaction failed")

    with pytest.raises(ConversationNotFound if failure == "owner" else RuntimeError):
        await asyncio.to_thread(
            orch.conversation_commits.publish_commit,
            commit_id=stage["commit_id"], owner_user_id="another-owner" if failure == "owner" else node_host.owner,
            messages=[{"role": "assistant", "content": "unpublished answer"}], canvas_components=[],
            fault_hook=fault if failure == "transaction" else None,
        )
    stored = await asyncio.to_thread(orch.history.get_chat, chat, node_host.owner)
    assert stored["messages"] == []
