"""Runs requested node turns through the real Kernel provider book and signed self-peer-tool dispatch.
Only the loopback model and sandbox transport are fake; enrollment, sealed credentials, grants, tickets, protected dispatch and journal completion are real."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from orchestrator.node_mesh_calls import OrchestratorCalls, skill_definition
from tests.test_node_mesh_kernel_integration import OWNER, ak, paired, until  # noqa: F401
from tests.test_node_mesh_peer_tools import API_KEY, FakeModel, REPLY, TOOL, model_server, node_host  # noqa: F401
from tests.test_node_mesh_personal_authority_integration import forbid_iam, verify_actual_principal
from tests.test_node_mesh_turn_tools import AGENT, SANDBOXED_TOOL, dice_grant, grant, sandboxed_grant, turn_host  # noqa: F401


async def setup_native(host, node, laptop, device, bridge, port, monkeypatch):
    verify_actual_principal(node)
    assert node.kernel.supports_node_request_tools() is True
    forbid_iam(host, monkeypatch)
    monkeypatch.setattr(host.orch, "_call_llm", AsyncMock(side_effect=AssertionError("Kernel owns provider calls")))
    monkeypatch.setattr(host.orch, "_resolve_llm_client_for", AsyncMock(side_effect=AssertionError("No Python provider lookup")))
    own = (await laptop.mesh_status()).device_id
    laptop.set_model_provider(ak.ModelProviderConfig(
        name="fake", base_url=f"http://127.0.0.1:{port}/v1", models=["fake-1"], api_keys=[API_KEY],
        fallback_order=0, self_hosted=True, strict_tools=False, recipients=[own, device],
    ))
    await until(lambda: any(provider.name == "fake" for provider in node.kernel.model_providers()),
                "the sealed provider book to sync")
    await bridge._publish_catalog()
    completed = []
    answer = node.answer_request

    async def watched(request, kernel):
        result = await answer(request, kernel)
        completed.append((request, result))
        return result

    monkeypatch.setattr(node, "answer_request", watched)
    return completed


async def ask(node, laptop, device, text="roll two dice on the node"):
    chat = laptop.create_chat("Kernel requested turn")
    laptop.send_message(chat, "hello node", None)
    await until(lambda: any(item.chat_id == chat for item in node.kernel.list_chats()), "the chat to sync")
    request = laptop.send_request(chat, text, device, None, None)
    return chat, request


@pytest.mark.parametrize("kind", ["in_process", "sandboxed"])
async def test_sealed_node_provider_answers_once_through_real_peer_dispatch(turn_host, model_server, tmp_path, monkeypatch, kind):  # noqa: F811
    host = turn_host
    calls = OrchestratorCalls(host.orch)
    async with paired(tmp_path, monkeypatch, calls=calls, history=host.orch.history, kernel_answers=True) as (
        node, laptop, device, bridge,
    ):
        completed = await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        if kind == "sandboxed":
            FakeModel.tool, FakeModel.arguments = SANDBOXED_TOOL, {"q": "weather"}
        await grant(node, laptop, sandboxed_grant(host) if kind == "sandboxed" else dice_grant(host))
        chat, request = await ask(node, laptop, device)
        await until(lambda: bool(completed), "the actual Kernel answer outcome")
        result = completed[0][1]
        assert result.status == "completed" and result.text == REPLY
        assert len(host.sent) == (1 if kind == "sandboxed" else 0)
        assert len(host.dispatched[AGENT]) == (1 if kind == "in_process" else 0)
        assert host.dispatched.get("general-1") == []
        inner = [item for item in node.kernel.chat_requests(chat) if item.call is not None]
        assert len(inner) == 1 and inner[0].state == ak.RequestState.ANSWERED
        assert inner[0].from_device == inner[0].to_device == device
        action = json.loads(inner[0].call.action_json)
        assert action["tool_def"] == (host.sandboxed_definition if kind == "sandboxed" else skill_definition(host.skill))
        assert action["tool"] == FakeModel.tool
        assert json.loads(inner[0].call.ticket_json)["capability"]
        parent = node.kernel.request(request)
        assert parent.state == ak.RequestState.ANSWERED and parent.turn == result.turn_id
        view = node.kernel.chat(chat).view
        assert next(turn for turn in view.turns if turn.id == parent.turn).status == ak.TurnStatus.ENDED
        assert API_KEY not in json.dumps([message.text for message in view.messages])
        await bridge._answer(parent)
        await bridge.scan()
        assert len(completed) == 1
        assert len(host.sent) + len(host.dispatched[AGENT]) == 1
    host.orch._call_llm.assert_not_awaited()
    host.orch._resolve_llm_client_for.assert_not_awaited()
    host.orch.derive_machine_authority.assert_not_awaited()
    host.orch.delegation.exchange_token_for_agent.assert_not_awaited()


async def test_two_native_outer_requests_both_complete_their_inner_calls(turn_host, model_server, tmp_path, monkeypatch):  # noqa: F811
    host = turn_host
    async with paired(tmp_path, monkeypatch, calls=OrchestratorCalls(host.orch), history=host.orch.history,
                      kernel_answers=True) as (node, laptop, device, bridge):
        completed = await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        await grant(node, laptop, dice_grant(host))
        requests = await asyncio.gather(*(ask(node, laptop, device) for _ in range(2)))
        await until(lambda: len(completed) == 2, "both Kernel-owned turns and their inner calls", seconds=60)
        assert all(result.status == "completed" for _, result in completed)
        assert len(host.dispatched[AGENT]) == 2
        assert all(node.kernel.request(request).state == ak.RequestState.ANSWERED for _, request in requests)


@pytest.mark.parametrize("grant_kind", ["none", "bare_name", "wrong_hash"])
async def test_native_route_never_dispatches_without_exact_granted_hash(turn_host, model_server, tmp_path, monkeypatch, grant_kind):  # noqa: F811
    host = turn_host
    async with paired(tmp_path, monkeypatch, calls=OrchestratorCalls(host.orch), history=host.orch.history,
                      kernel_answers=True) as (node, laptop, device, bridge):
        completed = await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        if grant_kind != "none":
            await grant(node, laptop, TOOL if grant_kind == "bare_name" else TOOL + "@00000000", "*")
        chat, _ = await ask(node, laptop, device)
        await until(lambda: bool(completed), "the Kernel denial outcome")
        assert not any(host.dispatched.values())
        assert not any(item.call is not None for item in node.kernel.chat_requests(chat))


async def test_native_route_refuses_an_unproven_owner_before_model_or_tool_use(turn_host, model_server, tmp_path, monkeypatch):  # noqa: F811
    host = turn_host
    async with paired(tmp_path, monkeypatch, calls=OrchestratorCalls(host.orch), history=host.orch.history,
                      kernel_answers=True) as (node, laptop, device, bridge):
        completed = await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        bridge._owner_of = lambda: "oidc.abc.owner"
        _, request = await ask(node, laptop, device)
        await until(lambda: node.kernel.request(request).state == ak.RequestState.REFUSED, "owner proof denial")
        assert node.kernel.request(request).code == "node.owner_mismatch"
        assert not completed and not FakeModel.seen and not any(host.dispatched.values())


@pytest.mark.parametrize("change", ["stop", "remove", "permission"])
async def test_revocation_after_native_mint_blocks_the_actual_agent_effect(turn_host, model_server, tmp_path, monkeypatch, change):  # noqa: F811
    host = turn_host
    changed = asyncio.Event()
    async with paired(tmp_path, monkeypatch, calls=OrchestratorCalls(host.orch), history=host.orch.history,
                      kernel_answers=True) as (node, laptop, device, bridge):
        await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        await grant(node, laptop, dice_grant(host))
        record = host.orch.audit_recorder.record

        async def mutate(event):
            result = await record(event)
            if event.action_type != f"tool.{TOOL}.start" or changed.is_set():
                return result
            if change == "stop":
                node.kernel.raise_stop()
            elif change == "remove":
                laptop.remove_device(device)
                await until(lambda: not any(member.is_local for member in node.kernel.roster().members),
                            "the local verifier to observe its removal")
            else:
                host.orch.tool_permissions.set_agent_scopes(OWNER, AGENT, {"tools:read": False})
            changed.set()
            return result

        monkeypatch.setattr(host.orch.audit_recorder, "record", mutate)
        chat, request = await ask(node, laptop, device)
        await asyncio.wait_for(changed.wait(), 30)
        await until(lambda: request not in bridge._running, "the refused native request to settle")
        inner = [item for item in node.kernel.chat_requests(chat) if item.call is not None]
        assert len(inner) == 1 and inner[0].state == ak.RequestState.REFUSED
        assert not any(host.dispatched.values()) and not host.sent


async def test_an_effect_before_reply_commit_failure_remains_unknown_without_replay(turn_host, model_server, tmp_path, monkeypatch):  # noqa: F811
    from orchestrator.node_mesh import NodeMeshError

    host = turn_host
    counter = tmp_path / "completed-effects.txt"
    counter.write_text("0")
    reply_failed = asyncio.Event()
    invoke = host.agent.handle_mcp_request

    async def persisted_effect(*args, **kwargs):
        result = await invoke(*args, **kwargs)
        counter.write_text(str(int(counter.read_text()) + 1))
        return result

    monkeypatch.setattr(host.agent, "handle_mcp_request", persisted_effect)
    async with paired(tmp_path, monkeypatch, calls=OrchestratorCalls(host.orch), history=host.orch.history,
                      kernel_answers=True) as (node, laptop, device, bridge):
        completed = await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        await grant(node, laptop, dice_grant(host))

        async def lost_reply(*_args, **_kwargs):
            reply_failed.set()
            raise NodeMeshError("store.reply_unwritten")

        monkeypatch.setattr(node, "record_turn", lost_reply)
        chat, request = await ask(node, laptop, device)
        await asyncio.wait_for(reply_failed.wait(), 30)
        await bridge._recover(device)
        inner = [item for item in node.kernel.chat_requests(chat) if item.call is not None]
        assert len(inner) == 1 and inner[0].state == ak.RequestState.CLAIMED
        assert counter.read_text() == "1"
        node.kernel.interrupt_request(request)
        await until(lambda: bool(completed), "the native unknown-effect outcome")
        assert completed[0][1].status == "needs_owner"
        resumed = await node.kernel.answer_request(request)
        assert resumed.status == "needs_owner" and resumed.calls_needing_owner
        await bridge._recover(device)
        assert counter.read_text() == "1" and len(host.dispatched[AGENT]) == 1
        assert node.kernel.request(inner[0].id).state == ak.RequestState.CLAIMED
        assert len([item for item in node.kernel.chat_requests(chat) if item.call is not None]) == 1


async def test_a_real_binding_without_native_tool_capability_refuses_without_python_fallback(turn_host, tmp_path, monkeypatch):  # noqa: F811
    host = turn_host
    async with paired(tmp_path, monkeypatch, calls=OrchestratorCalls(host.orch), history=host.orch.history,
                      kernel_answers=True) as (node, laptop, device, bridge):
        verify_actual_principal(node)
        forbid_iam(host, monkeypatch)
        monkeypatch.setattr(node.kernel, "supports_node_request_tools", None, raising=False)
        answer = AsyncMock(side_effect=AssertionError("Unsupported runtime must not run"))
        monkeypatch.setattr(node.kernel, "answer_request", answer)
        _, request = await ask(node, laptop, device)
        await until(lambda: node.kernel.request(request).state == ak.RequestState.REFUSED, "capability denial")
        assert node.kernel.request(request).code == "node.request_runtime_unsupported"
        assert not any(host.dispatched.values()) and not host.sent
        answer.assert_not_awaited()
        host.orch.derive_machine_authority.assert_not_awaited()
