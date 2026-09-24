"""Tests that orchestrator.py's _deliver_accepted_voice_turn detaches the
spoken-acknowledgement runner instead of awaiting it, so a slow or failing
announcement cannot block the model turn.
"""

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import Orchestrator


def _bare_orchestrator(start_turn_announcements):
    orch = Orchestrator.__new__(Orchestrator)
    orch._voice_ack_tasks = set()
    orch._reconnectable_operations = {}

    async def _noop(*args, **kwargs):
        return None

    orch._deliver_committed_conversation_snapshot = _noop
    orch._broadcast_voice_ack = _noop
    orch._broadcast_voice_turn_state = _noop
    orch.voice_services = SimpleNamespace(
        coordinator=SimpleNamespace(emit_transcript_accepted=_noop),
        start_turn_announcements=start_turn_announcements,
    )
    return orch


def _dispatch():
    return SimpleNamespace(
        admission=SimpleNamespace(turn=SimpleNamespace(request_generation="1")),
        connection_generation="1",
    )


async def _deliver(orch):
    return await orch._deliver_accepted_voice_turn(
        object(),
        voice_dispatch=_dispatch(),
        operation_context={},
        acceptance_stage=None,
        acceptance_record=None,
        accepted_turn=SimpleNamespace(turn_id="turn-1"),
        accepted_message_id=7,
    )


@pytest.mark.asyncio
async def test_dispatch_returns_while_acknowledgement_is_still_speaking():
    speaking = asyncio.Event()

    async def _never_finishes(_turn):
        speaking.set()
        await asyncio.Event().wait()

    orch = _bare_orchestrator(_never_finishes)

    result = await asyncio.wait_for(_deliver(orch), timeout=2)

    assert result["message_id"] == 7
    await asyncio.wait_for(speaking.wait(), timeout=2)
    assert len(orch._voice_ack_tasks) == 1

    for task in list(orch._voice_ack_tasks):
        task.cancel()


@pytest.mark.asyncio
async def test_acknowledgement_task_is_tracked_until_it_completes():
    async def _finishes(_turn):
        return None

    orch = _bare_orchestrator(_finishes)
    await _deliver(orch)

    assert len(orch._voice_ack_tasks) == 1
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert orch._voice_ack_tasks == set()


@pytest.mark.asyncio
async def test_acknowledgement_failure_does_not_fail_the_turn():
    async def _raises(_turn):
        raise RuntimeError("runner unavailable")

    orch = _bare_orchestrator(_raises)

    result = await _deliver(orch)

    assert result["message_id"] == 7
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert orch._voice_ack_tasks == set()
