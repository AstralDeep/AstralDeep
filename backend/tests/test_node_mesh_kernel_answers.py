"""Checks Kernel-owned request answering and bounded scheduling using explicit in-memory Kernel fixtures.
The production bridge must not preclaim, run a Python model or record a second answer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.node_mesh import NodeMeshError
from orchestrator.node_mesh_chat import MeshChatBridge
from tests import fake_astral_kernel as fake
from tests.test_node_mesh_personal_authority import OWNER, personal  # noqa: F401


@pytest.fixture
def kernel_host(personal, tmp_path):  # noqa: F811
    host = personal
    host.request.state = SimpleNamespace(name="PENDING")
    host.request.labels = SimpleNamespace(confidentiality="PRIVATE", integrity="OWNER")
    host.mesh.chat = AsyncMock(return_value=SimpleNamespace(view=SimpleNamespace(messages=[])))
    host.mesh.claim_request = AsyncMock(side_effect=AssertionError("Python must not claim Kernel-owned turns"))
    host.mesh.record_turn = AsyncMock(side_effect=AssertionError("Python must not record Kernel-owned turns"))
    host.mesh._kernel.supports_node_request_tools = lambda: True
    host.mesh._kernel.interrupt_request = MagicMock()
    refusals = []

    async def refuse(request_id, code):
        if host.request.state.name in ("ANSWERED", "REFUSED"):
            raise NodeMeshError("request.closed")
        refusals.append((request_id, code))
        host.request.state = SimpleNamespace(name="REFUSED")

    async def answer(request_id):
        assert request_id == host.request.id
        host.request.state = SimpleNamespace(name="ANSWERED")
        return SimpleNamespace(status="completed", text="Kernel answer")

    host.mesh.refuse_request = AsyncMock(side_effect=refuse)
    native = AsyncMock(side_effect=answer)
    host.mesh._kernel.answer_request = native
    history = SimpleNamespace(create_chat=MagicMock(side_effect=AssertionError("No Python history for this turn")))
    bridge = MeshChatBridge(host.mesh, owner_id=lambda: host.state.owner, history=history, runner=None,
                            state_path=tmp_path / "bridge.json", phi_active=lambda _owner: False)
    return SimpleNamespace(host=host, bridge=bridge, native=native, refusals=refusals, history=history)


async def test_kernel_owns_claim_and_answer_recording(kernel_host):
    state = kernel_host
    await state.bridge._answer(state.host.request)
    state.native.assert_awaited_once_with("request-1")
    state.host.mesh.claim_request.assert_not_awaited()
    state.host.mesh.record_turn.assert_not_awaited()
    state.history.create_chat.assert_not_called()
    assert not state.refusals and state.host.request.state.name == "ANSWERED"


async def test_old_request_runtime_is_refused_without_a_python_fallback(kernel_host):
    state = kernel_host
    state.host.mesh._kernel.supports_node_request_tools = None
    await state.bridge._answer(state.host.request)
    assert state.refusals == [("request-1", "node.request_runtime_unsupported")]
    state.native.assert_not_awaited()
    state.host.mesh.claim_request.assert_not_awaited()


@pytest.mark.parametrize("change,expected", [("owner", "node.owner_mismatch"), ("stop", "gate.mesh_stopped")])
async def test_current_identity_and_stop_are_checked_before_kernel_answering(kernel_host, change, expected):
    state = kernel_host
    if change == "owner":
        state.host.state.owner = "unproven-owner"
    else:
        state.host.state.stopped = True
    await state.bridge._answer(state.host.request)
    assert state.refusals == [("request-1", expected)]
    state.native.assert_not_awaited()


async def test_a_failed_claimed_kernel_turn_is_marked_unknown_and_never_replayed(kernel_host):
    state = kernel_host

    async def uncertain(_request):
        state.host.request.state = SimpleNamespace(name="CLAIMED")
        raise fake.KernelError.State("engine.failure", "unknown test effect")

    state.native.side_effect = uncertain
    await state.bridge._answer(state.host.request)
    assert state.refusals == [("request-1", "node.outcome_unknown")]
    await state.bridge._answer(state.host.request)
    state.native.assert_awaited_once()
    assert state.refusals == [("request-1", "node.outcome_unknown")]


async def test_an_error_after_kernel_turn_start_preserves_its_journal_without_a_refusal(kernel_host):
    state = kernel_host

    async def committed(_request):
        state.host.request.state = SimpleNamespace(name="ANSWERED")
        raise RuntimeError("journal failure after turn_start")

    state.native.side_effect = committed
    await state.bridge._answer(state.host.request)
    assert state.refusals == []
    state.native.assert_awaited_once()


async def test_two_outer_turns_do_not_hold_the_slots_needed_for_their_peer_calls(kernel_host):
    bridge = kernel_host.bridge
    ready = asyncio.Event()
    started = []
    children = []

    async def answer(request):
        if request.call is None:
            started.append(request.id)
            if len(started) == 2:
                ready.set()
            await ready.wait()
            await bridge._serve(SimpleNamespace(id="child-" + request.id, call=object()))
        else:
            children.append(request.id)

    bridge._answer = answer
    await asyncio.wait_for(asyncio.gather(*(
        bridge._serve(SimpleNamespace(id=str(index), call=None)) for index in range(2)
    )), timeout=5)
    assert sorted(children) == ["child-0", "child-1"]


@pytest.mark.parametrize("status,author,claim", [
    ("RUNNING", "node", "claim-1"), ("ENDED", "node", "claim-1"),
    ("OUTCOME_UNKNOWN", "node", "claim-1"), ("RUNNING", "phone", "claim-1"),
    ("RUNNING", "node", None),
])
async def test_only_a_claimed_own_open_requested_turn_can_resume(kernel_host, status, author, claim):
    state = kernel_host
    request = state.host.request
    request.state = SimpleNamespace(name="ANSWERED")
    request.turn, request.claim = "turn-1", claim
    turn = SimpleNamespace(id="turn-1", author=author, status=SimpleNamespace(name=status))
    state.host.mesh._kernel.chat = lambda _chat: SimpleNamespace(view=SimpleNamespace(turns=[turn]))
    await state.bridge._answer(request)
    assert state.native.await_count == (1 if status == "RUNNING" and author == "node" and claim else 0)
    assert state.refusals == []


async def test_restart_recovers_only_own_open_answered_text_requests(kernel_host):
    state = kernel_host
    request = state.host.request
    request.state = SimpleNamespace(name="ANSWERED")
    request.turn, request.claim = "turn-1", "claim-1"
    completed = SimpleNamespace(**{**vars(request), "id": "closed", "turn": "turn-2"})
    unknown = SimpleNamespace(**{**vars(request), "id": "unknown", "turn": "turn-3"})
    foreign = SimpleNamespace(**{**vars(request), "id": "foreign", "to_device": "phone"})
    peer = SimpleNamespace(**{**vars(request), "id": "peer", "call": object()})
    view = SimpleNamespace(messages=[], turns=[
        SimpleNamespace(id=turn, author="node", status=SimpleNamespace(name=status))
        for turn, status in [("turn-1", "RUNNING"), ("turn-2", "ENDED"), ("turn-3", "OUTCOME_UNKNOWN")]
    ])
    state.host.mesh.chat.return_value = SimpleNamespace(view=view)
    state.host.mesh._kernel.chat = lambda _chat: SimpleNamespace(view=view)
    state.host.mesh.list_chats = AsyncMock(return_value=[SimpleNamespace(chat_id="mesh-chat")])
    state.host.mesh.chat_requests = AsyncMock(return_value=[request, completed, unknown, foreign, peer])
    await state.bridge._recover("node")
    await asyncio.gather(*state.bridge._tasks)
    state.native.assert_awaited_once_with("request-1")
    state.host.mesh.claim_request.assert_not_awaited()
    assert not state.bridge._running and state.refusals == []


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_shutdown_explicitly_interrupt_and_wait_for_native_outcome(kernel_host, cancel):
    state = kernel_host
    entered, interrupted, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def running(_request):
        state.host.request.state = SimpleNamespace(name="ANSWERED")
        entered.set()
        await interrupted.wait()
        finished.set()

    state.native.side_effect = running
    loop = asyncio.get_running_loop()
    state.host.mesh._kernel.interrupt_request = MagicMock(side_effect=lambda request: loop.call_soon_threadsafe(interrupted.set))
    state.bridge._turn_seconds = 5 if cancel else 0.01
    task = asyncio.create_task(state.bridge._answer(state.host.request))
    await entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert finished.is_set() and state.refusals == []
    state.host.mesh._kernel.interrupt_request.assert_called_once_with("request-1")


async def test_pending_native_failure_refuses_but_never_uses_python_runner(kernel_host):
    state = kernel_host
    state.native.side_effect = fake.KernelError.State("gate.denied", "before claim")
    await state.bridge._answer(state.host.request)
    assert state.refusals == [("request-1", "gate.denied")]
    state.host.mesh.claim_request.assert_not_awaited()


async def test_owner_change_during_preflight_refuses_before_native_answer(kernel_host):
    state = kernel_host
    state.host.state.on_proof = lambda: setattr(state.host.state, "owner", "changed")
    await state.bridge._answer(state.host.request)
    assert state.refusals == [("request-1", "node.owner_mismatch")]
    state.native.assert_not_awaited()


async def test_timeout_retries_interrupt_until_native_registration_without_restarting(kernel_host):
    state = kernel_host
    registered, interrupted = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    attempts = []

    async def delayed_registration(_request):
        await registered.wait()
        state.host.request.state = SimpleNamespace(name="ANSWERED")
        await interrupted.wait()
        return SimpleNamespace(status="interrupted")

    def interrupt(_chat):
        attempts.append(True)
        if len(attempts) == 1:
            loop.call_soon_threadsafe(registered.set)
            raise fake.KernelError.State("engine.no_open_turn", "not registered yet")
        loop.call_soon_threadsafe(interrupted.set)

    state.native.side_effect = delayed_registration
    state.host.mesh._kernel.interrupt_request = interrupt
    state.host.mesh._kernel.interrupt = MagicMock(side_effect=AssertionError("Another turn uses this chat"))
    state.bridge._turn_seconds = 0.01
    await asyncio.wait_for(state.bridge._answer(state.host.request), 5)
    assert len(attempts) == 2 and interrupted.is_set() and state.refusals == []
    state.native.assert_awaited_once()


async def test_missing_request_bound_interrupt_refuses_before_any_native_work(kernel_host):
    state = kernel_host
    state.host.mesh._kernel.interrupt_request = None
    await state.bridge._answer(state.host.request)
    assert state.refusals == [("request-1", "node.request_runtime_unsupported")]
    state.native.assert_not_awaited()
