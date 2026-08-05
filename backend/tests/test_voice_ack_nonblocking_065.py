"""The spoken acknowledgement must not gate the model turn.

``_deliver_accepted_voice_turn`` used to ``await`` the announcement runner,
which settles its future only after the acknowledgement has finished *playing*
(and, when a speech terminal is lost, only after a 12s source timeout). That put
the whole utterance on the critical path to the first model token of every voice
turn. The runner's own command deque still serializes the acknowledgement ahead
of the terminal recap, so detaching the call is safe.
"""

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import Orchestrator


def _bare_orchestrator(start_turn_announcements):
    """An Orchestrator with only what _deliver_accepted_voice_turn touches."""

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
    """A never-settling announcement must not stall the turn."""

    speaking = asyncio.Event()

    async def _never_finishes(_turn):
        speaking.set()
        await asyncio.Event().wait()  # models an utterance still playing

    orch = _bare_orchestrator(_never_finishes)

    result = await asyncio.wait_for(_deliver(orch), timeout=2)

    assert result["message_id"] == 7
    await asyncio.wait_for(speaking.wait(), timeout=2)
    assert len(orch._voice_ack_tasks) == 1

    for task in list(orch._voice_ack_tasks):
        task.cancel()


@pytest.mark.asyncio
async def test_acknowledgement_task_is_tracked_until_it_completes():
    """The task is strongly referenced, then discarded — asyncio holds only weak refs."""

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
    """A runner that raises is logged by the done-callback, never propagated."""

    async def _raises(_turn):
        raise RuntimeError("runner unavailable")

    orch = _bare_orchestrator(_raises)

    result = await _deliver(orch)

    assert result["message_id"] == 7
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert orch._voice_ack_tasks == set()
