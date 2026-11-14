"""Checks that a node's committed model answers reach its requesting peer while unpublished conversation messages remain private.
The real orchestrator, Plane publication transaction and Kernel mesh run against isolated local fixtures.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from orchestrator import node_mesh_chat
from orchestrator.history import ConversationNotFound
from orchestrator.node_mesh_calls import OrchestratorCalls
from shared.feature_flags import flags
from tests.test_node_mesh_kernel_integration import ak, paired, until  # noqa: F401
from tests import test_node_mesh_peer_tools as peer_tools

OWNER = peer_tools.OWNER
node_host = peer_tools.node_host


@pytest.mark.parametrize("routing", [False, True])
async def test_a_committed_node_answer_reaches_the_requesting_peer(node_host, tmp_path, monkeypatch, routing):
    orch = node_host.orch
    monkeypatch.setitem(flags._flags, "typesafe_routing", routing)
    orch._llm_store.set_sync(
        OWNER, provider="custom", base_url="http://test.invalid/v1", model="test-model", api_key="test-key",
    )
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    async def model(*args, **kwargs):
        return SimpleNamespace(
            role="assistant", content="the committed node answer", tool_calls=None, reasoning_content=None,
        ), usage

    monkeypatch.setattr(orch, "_call_llm", model)
    monkeypatch.setattr(orch, "_resolve_llm_client_for", AsyncMock(
        return_value=(object(), None, SimpleNamespace(model="test-model", base_url=None)),
    ))
    monkeypatch.setattr(orch, "summarize_chat_title", AsyncMock())
    async with paired(tmp_path, monkeypatch, calls=OrchestratorCalls(orch), history=orch.history) as (
        node, peer, node_device, bridge,
    ):
        bridge._runner = node_mesh_chat.orchestrator_runner(orch)
        chat = peer.create_chat("committed answer")
        peer.send_message(chat, "hello", None)
        await until(lambda: any(item.chat_id == chat for item in node.kernel.list_chats()), "chat sync")
        request_id = peer.send_request(chat, "answer this request", node_device, None, None)
        await until(
            lambda: peer.request(request_id).state in (ak.RequestState.ANSWERED, ak.RequestState.REFUSED),
            "node request completion",
        )
        request = peer.request(request_id)
        assert request.state == ak.RequestState.ANSWERED, request.code
        await until(
            lambda: any(item.text == "the committed node answer" for item in peer.chat(chat).view.messages),
            "answer sync",
        )
        assert [turn.id for turn in peer.chat(chat).view.turns] == [request.turn]
        local = bridge.state.local_for(chat)
        stored = await asyncio.to_thread(orch.history.get_chat, local, OWNER)
        assert "the committed node answer" in json.dumps(stored)


async def test_an_aborted_node_answer_is_never_observed(node_host, monkeypatch):
    orch = node_host.orch
    observed = []
    monkeypatch.setattr(node_mesh_chat, "observe_message", lambda *args: observed.append(args))
    chat = await asyncio.to_thread(orch.history.create_chat, user_id=OWNER)
    stage = await asyncio.to_thread(
        orch.conversation_commits.stage_commit,
        chat_id=chat, owner_user_id=OWNER, request_generation=str(uuid4()),
    )
    await asyncio.to_thread(
        orch.conversation_commits.prepare_canvas_stage, commit_id=stage["commit_id"], owner_user_id=OWNER,
    )
    await asyncio.to_thread(
        orch.conversation_commits.append_staged_message,
        commit_id=stage["commit_id"], owner_user_id=OWNER, role="assistant", content="unpublished answer",
    )
    before = await asyncio.to_thread(orch.history.get_chat, chat, OWNER)
    assert "unpublished answer" not in json.dumps(before)
    assert observed == []
    await asyncio.to_thread(
        orch.conversation_commits.abort_commit, commit_id=stage["commit_id"], owner_user_id=OWNER,
    )
    after = await asyncio.to_thread(orch.history.get_chat, chat, OWNER)
    assert "unpublished answer" not in json.dumps(after)
    assert observed == []


@pytest.mark.parametrize("prepared", [False, True])
async def test_a_publication_notifies_only_its_committed_messages_once(node_host, monkeypatch, prepared):
    orch = node_host.orch
    chat = await asyncio.to_thread(orch.history.create_chat, user_id=OWNER)
    observed = []

    def observe(owner, local_chat, role, content):
        stored = orch.history.get_chat(local_chat, owner)
        assert content in [item["content"] for item in stored["messages"]]
        observed.append((owner, local_chat, role, content))

    monkeypatch.setattr(node_mesh_chat, "observe_message", observe)
    stage = await asyncio.to_thread(
        orch.conversation_commits.stage_commit,
        chat_id=chat, owner_user_id=OWNER, request_generation=str(uuid4()),
    )
    answer = [{"type": "text", "content": "answer"}]
    messages = [{"role": "user", "content": "question"}, {"role": "assistant", "content": answer}]
    if prepared:
        for message in messages:
            await asyncio.to_thread(
                orch.conversation_commits.append_staged_message,
                commit_id=stage["commit_id"], owner_user_id=OWNER, **message,
            )
    assert observed == []
    for _ in range(2):
        await asyncio.to_thread(
            orch.conversation_commits.publish_commit,
            commit_id=stage["commit_id"], owner_user_id=OWNER,
            messages=None if prepared else messages, canvas_components=[],
        )
    assert observed == [(OWNER, chat, "user", "question"), (OWNER, chat, "assistant", answer)]


@pytest.mark.parametrize("completion_state", ["failed", "cancelled"])
async def test_an_incomplete_publication_does_not_answer_a_mesh_request(node_host, monkeypatch, completion_state):
    orch = node_host.orch
    observed = []
    monkeypatch.setattr(node_mesh_chat, "observe_message", lambda *args: observed.append(args))
    chat = await asyncio.to_thread(orch.history.create_chat, user_id=OWNER)
    stage = await asyncio.to_thread(
        orch.conversation_commits.stage_commit,
        chat_id=chat, owner_user_id=OWNER, request_generation=str(uuid4()),
    )
    await asyncio.to_thread(
        orch.conversation_commits.publish_commit,
        commit_id=stage["commit_id"], owner_user_id=OWNER,
        messages=[{"role": "assistant", "content": "partial answer"}], canvas_components=[],
        completion_state=completion_state,
    )
    assert observed == []


@pytest.mark.parametrize("failure", ["owner", "transaction"])
async def test_a_rejected_publication_never_notifies_the_mesh(node_host, monkeypatch, failure):
    orch = node_host.orch
    observed = []
    monkeypatch.setattr(node_mesh_chat, "observe_message", lambda *args: observed.append(args))
    chat = await asyncio.to_thread(orch.history.create_chat, user_id=OWNER)
    stage = await asyncio.to_thread(
        orch.conversation_commits.stage_commit,
        chat_id=chat, owner_user_id=OWNER, request_generation=str(uuid4()),
    )

    def fault(_point):
        raise RuntimeError("publication transaction failed")

    with pytest.raises(ConversationNotFound if failure == "owner" else RuntimeError):
        await asyncio.to_thread(
            orch.conversation_commits.publish_commit,
            commit_id=stage["commit_id"], owner_user_id="another-owner" if failure == "owner" else OWNER,
            messages=[{"role": "assistant", "content": "unpublished answer"}], canvas_components=[],
            fault_hook=fault if failure == "transaction" else None,
        )
    assert observed == []
    stored = await asyncio.to_thread(orch.history.get_chat, chat, OWNER)
    assert stored["messages"] == []


async def test_an_observer_error_does_not_relabel_a_committed_publication(node_host, monkeypatch, caplog):
    orch = node_host.orch
    attempts = []

    def observe(*args):
        attempts.append(args)
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(node_mesh_chat, "observe_message", observe)
    chat = await asyncio.to_thread(orch.history.create_chat, user_id=OWNER)
    stage = await asyncio.to_thread(
        orch.conversation_commits.stage_commit,
        chat_id=chat, owner_user_id=OWNER, request_generation=str(uuid4()),
    )
    for _ in range(2):
        result = await asyncio.to_thread(
            orch.conversation_commits.publish_commit,
            commit_id=stage["commit_id"], owner_user_id=OWNER,
            messages=[{"role": "assistant", "content": "committed answer"}], canvas_components=[],
        )
        assert result["state"] == "committed"
    assert len(attempts) == 1
    stored = await asyncio.to_thread(orch.history.get_chat, chat, OWNER)
    assert stored["messages"][0]["content"] == "committed answer"
    assert "node_mesh_committed_message_observation_failed" in caplog.text
