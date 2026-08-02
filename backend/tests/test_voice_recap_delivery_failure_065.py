"""Committed-result recap delivery failure coverage for Feature 065."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.orchestrator import (
    Orchestrator,
    _VOICE_REQUEST_SUCCEEDED_MESSAGE,
    _VoiceDispatchContext,
)
from orchestrator.tests.test_voice_bootstrap_065 import (
    _eventually,
    _runner_services,
    _voice_turn,
)
from orchestrator.voice_bootstrap import VoiceTerminalAnnouncementResult
from orchestrator.work_admission import ExecutionFence, OperationState


@pytest.mark.asyncio
async def test_failed_result_speech_reports_failure_without_rolling_back_text() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-000000000211",
        turn_id="00000000-0000-4000-8000-000000000212",
    )
    services, clock, repository, media = _runner_services(turn)
    runner = None
    try:
        start = asyncio.create_task(services.start_turn_announcements(turn))
        await _eventually(lambda: len(media.calls) == 1)
        media.finish()
        await start
        runner = services.announcement_runners[(turn.session_id, 1)]

        async def fail_speech(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("provider-body-must-stay-private")

        media.speak_turn = fail_speech  # type: ignore[method-assign]
        completion = asyncio.create_task(
            services.finish_turn_announcements(
                turn,
                terminal_kind="succeeded",
                recap_text="The committed report is ready.",
                recap_source="authoritative_summary",
                sensitivity="non_sensitive",
                result_commit_id="00000000-0000-4000-8000-000000000213",
                with_delivery_status=True,
            )
        )
        await _eventually(
            lambda: repository.turns[turn.turn_id].state == "succeeded"
        )
        clock.advance(0.25)
        runner.wake()

        delivery = await completion
        assert isinstance(delivery, VoiceTerminalAnnouncementResult)
        assert delivery.speech_outcome == "failed"
        assert delivery.turn.state == "succeeded"
        assert repository.turns[turn.turn_id].state == "succeeded"
        assert repository.turns[turn.turn_id].result_commit_id == (
            "00000000-0000-4000-8000-000000000213"
        )
        assert "provider-body-must-stay-private" not in repr(delivery)
    finally:
        if runner is not None:
            await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_partial_result_reservation_reports_incomplete_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-000000000214",
        turn_id="00000000-0000-4000-8000-000000000215",
    )
    services, clock, repository, media = _runner_services(turn)
    runner = None
    try:
        start = asyncio.create_task(services.start_turn_announcements(turn))
        await _eventually(lambda: len(media.calls) == 1)
        media.finish()
        await start
        runner = services.announcement_runners[(turn.session_id, 1)]
        original_reserve = services._reserve_announcement
        result_reservations = 0

        async def fail_second_result_reservation(
            _services: Any,
            turn_arg: Any,
            kind: str,
            **kwargs: Any,
        ) -> Any:
            nonlocal result_reservations
            if kind == "result":
                result_reservations += 1
                if result_reservations == 2:
                    raise RuntimeError("provider-body-must-stay-private")
            return await original_reserve(turn_arg, kind, **kwargs)

        monkeypatch.setattr(
            type(services),
            "_reserve_announcement",
            fail_second_result_reservation,
        )
        completion = asyncio.create_task(
            services.finish_turn_announcements(
                turn,
                terminal_kind="succeeded",
                recap_text="The committed report is ready.",
                recap_source="authoritative_summary",
                sensitivity="non_sensitive",
                result_commit_id="00000000-0000-4000-8000-000000000216",
                with_delivery_status=True,
            )
        )
        await _eventually(
            lambda: repository.turns[turn.turn_id].state == "succeeded"
        )
        clock.advance(0.25)
        runner.wake()
        await _eventually(lambda: len(media.calls) == 2)
        assert media.calls[-1]["text"] == "Done."
        media.finish()

        delivery = await completion
        assert isinstance(delivery, VoiceTerminalAnnouncementResult)
        assert delivery.speech_outcome == "failed"
        assert delivery.turn.state == "succeeded"
        assert repository.turns[turn.turn_id].result_quantum_count == 1
    finally:
        if runner is not None:
            await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_intentional_stop_marks_exact_success_recap_suppressed() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-000000000217",
        turn_id="00000000-0000-4000-8000-000000000218",
    )
    services, _clock, repository, media = _runner_services(turn)
    runner = None
    try:
        start = asyncio.create_task(services.start_turn_announcements(turn))
        await _eventually(lambda: len(media.calls) == 1)
        media.finish()
        await start
        runner = services.announcement_runners[(turn.session_id, 1)]

        completion = asyncio.create_task(
            services.finish_turn_announcements(
                turn,
                terminal_kind="succeeded",
                recap_text="The committed report is ready.",
                recap_source="authoritative_summary",
                sensitivity="non_sensitive",
                result_commit_id="00000000-0000-4000-8000-000000000219",
                with_delivery_status=True,
            )
        )
        await _eventually(lambda: len(media.calls) == 2)
        await services.stop_session_speech(turn.session_id, 1)

        delivery = await completion
        assert isinstance(delivery, VoiceTerminalAnnouncementResult)
        assert delivery.speech_outcome == "suppressed"
        assert delivery.turn.state == "succeeded"
        assert repository.turns[turn.turn_id].state == "succeeded"
        assert media.stops == 1
        await asyncio.sleep(0)
        assert len(media.calls) == 2
    finally:
        if runner is not None:
            await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_foreground_suspension_marks_recap_suppressed_without_replay() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-000000000223",
        turn_id="00000000-0000-4000-8000-000000000224",
    )
    services, clock, repository, media = _runner_services(turn)
    runner = None
    try:
        start = asyncio.create_task(services.start_turn_announcements(turn))
        await _eventually(lambda: len(media.calls) == 1)
        media.finish()
        await start
        runner = services.announcement_runners[(turn.session_id, 1)]

        completion = asyncio.create_task(
            services.finish_turn_announcements(
                turn,
                terminal_kind="succeeded",
                recap_text="The committed report is ready.",
                recap_source="authoritative_summary",
                sensitivity="non_sensitive",
                result_commit_id="00000000-0000-4000-8000-000000000225",
                with_delivery_status=True,
            )
        )
        await _eventually(lambda: len(media.calls) == 2)
        await services.set_session_speech_suspended(turn.session_id, 1, True)

        delivery = await completion
        assert isinstance(delivery, VoiceTerminalAnnouncementResult)
        assert delivery.speech_outcome == "suppressed"
        assert delivery.turn.state == "succeeded"
        assert repository.turns[turn.turn_id].state == "succeeded"
        assert (turn.session_id, 1) in services.announcement_suspended_sessions

        await services.set_session_speech_suspended(turn.session_id, 1, False)
        clock.advance(30)
        runner.wake()
        await asyncio.sleep(0)
        assert len(media.calls) == 2
        assert (turn.session_id, 1) not in services.announcement_suspended_sessions
    finally:
        if runner is not None:
            await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_suspend_resume_race_drops_active_recap_remainder() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-000000000226",
        turn_id="00000000-0000-4000-8000-000000000227",
    )
    services, clock, repository, media = _runner_services(turn)
    runner = None
    try:
        start = asyncio.create_task(services.start_turn_announcements(turn))
        await _eventually(lambda: len(media.calls) == 1)
        media.finish()
        await start
        runner = services.announcement_runners[(turn.session_id, 1)]

        completion = asyncio.create_task(
            services.finish_turn_announcements(
                turn,
                terminal_kind="succeeded",
                recap_text="The committed report is ready for review now.",
                recap_source="authoritative_summary",
                sensitivity="non_sensitive",
                result_commit_id="00000000-0000-4000-8000-000000000228",
                with_delivery_status=True,
            )
        )
        await _eventually(lambda: len(media.calls) == 2)

        async def delayed_source_stop(_session: Any) -> None:
            # Model the exact race where mute wins locally while the worker's
            # already-in-flight source reaches its normal terminal event.
            media.stops += 1

        media.stop_speech = delayed_source_stop  # type: ignore[method-assign]
        await services.set_session_speech_suspended(turn.session_id, 1, True)
        await services.set_session_speech_suspended(turn.session_id, 1, False)
        media.finish("speech_finished")

        delivery = await completion
        assert isinstance(delivery, VoiceTerminalAnnouncementResult)
        assert delivery.speech_outcome == "suppressed"
        assert delivery.turn.state == "succeeded"
        assert repository.turns[turn.turn_id].state == "succeeded"
        assert media.stops == 1

        clock.advance(30)
        runner.wake()
        await asyncio.sleep(0)
        assert len(media.calls) == 2
        assert not runner._continuations
    finally:
        if runner is not None:
            await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_worker_barge_in_marks_exact_recap_suppressed_not_failed() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-000000000229",
        turn_id="00000000-0000-4000-8000-00000000022a",
    )
    services, _clock, repository, media = _runner_services(turn)
    runner = None
    try:
        start = asyncio.create_task(services.start_turn_announcements(turn))
        await _eventually(lambda: len(media.calls) == 1)
        media.finish()
        await start
        runner = services.announcement_runners[(turn.session_id, 1)]

        completion = asyncio.create_task(
            services.finish_turn_announcements(
                turn,
                terminal_kind="succeeded",
                recap_text="The committed report is ready.",
                recap_source="authoritative_summary",
                sensitivity="non_sensitive",
                result_commit_id="00000000-0000-4000-8000-00000000022b",
                with_delivery_status=True,
            )
        )
        await _eventually(lambda: len(media.calls) == 2)
        # Worker VAD can barge in without a preceding REST stop command.
        media.finish("speech_interrupted")

        delivery = await completion
        assert isinstance(delivery, VoiceTerminalAnnouncementResult)
        assert delivery.speech_outcome == "suppressed"
        assert delivery.turn.state == "succeeded"
        assert repository.turns[turn.turn_id].state == "succeeded"
        assert media.stops == 0
        assert len(media.calls) == 2
    finally:
        if runner is not None:
            await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_stop_between_recap_quanta_clears_queued_continuation() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-000000000220",
        turn_id="00000000-0000-4000-8000-000000000221",
    )
    services, _clock, repository, media = _runner_services(turn)
    runner = None
    try:
        start = asyncio.create_task(services.start_turn_announcements(turn))
        await _eventually(lambda: len(media.calls) == 1)
        media.finish()
        await start
        runner = services.announcement_runners[(turn.session_id, 1)]

        at_boundary = asyncio.Event()
        original_next = runner._next_continuation

        def hold_queued_continuation() -> Any:
            if runner._continuations:
                at_boundary.set()
                return None
            return original_next()

        runner._next_continuation = hold_queued_continuation  # type: ignore[method-assign]
        completion = asyncio.create_task(
            services.finish_turn_announcements(
                turn,
                terminal_kind="succeeded",
                recap_text="The committed report is ready.",
                recap_source="authoritative_summary",
                sensitivity="non_sensitive",
                result_commit_id="00000000-0000-4000-8000-000000000222",
                with_delivery_status=True,
            )
        )
        await _eventually(lambda: len(media.calls) == 2)
        media.finish()
        await asyncio.wait_for(at_boundary.wait(), timeout=1)
        assert runner._active_bundle is None
        assert runner._continuations

        await services.stop_session_speech(turn.session_id, 1)
        delivery = await completion

        assert isinstance(delivery, VoiceTerminalAnnouncementResult)
        assert delivery.speech_outcome == "suppressed"
        assert delivery.turn.state == "succeeded"
        assert repository.turns[turn.turn_id].state == "succeeded"
        assert not runner._commands
        assert not runner._continuations
        assert not runner._terminal
        assert not runner._stop_waiters
        assert runner._scheduler.active_turn_count == 0
        await asyncio.sleep(0)
        assert len(media.calls) == 2
    finally:
        if runner is not None:
            await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_committed_success_broadcasts_exact_turn_failed_speech_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_commit_id = str(uuid.uuid4())
    operation_id = uuid.uuid4()
    fence = ExecutionFence(operation_id, 1, uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(uuid.uuid4()),
            result_commit_id=result_commit_id,
        ),
        user_id="recap-user",
        chat_id=str(uuid.uuid4()),
        operation_id=str(operation_id),
    )
    terminal_turn = replace(
        turn,
        state="succeeded",
        terminal_kind="succeeded",
        is_foreground=False,
        result_commit_id=result_commit_id,
        recap_source="authoritative_summary",
    )
    finish_kwargs: dict[str, Any] = {}

    class Repository:
        def get_turn(self, *, user_id: str, turn_id: str) -> Any:
            assert (user_id, turn_id) == (turn.user_id, turn.turn_id)
            return turn

    class Services:
        repository = Repository()

        async def finish_turn_announcements(
            self, _turn: Any, **kwargs: Any
        ) -> VoiceTerminalAnnouncementResult:
            finish_kwargs.update(kwargs)
            return VoiceTerminalAnnouncementResult(terminal_turn, "failed")

        async def remember_sensitive_recap(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("non-sensitive results need no consent registry")

    class Commits:
        def committed_assistant_content(self, **kwargs: Any) -> Any:
            assert kwargs == {
                "commit_id": result_commit_id,
                "owner_user_id": turn.user_id,
            }
            return [{"type": "text", "content": "The report is ready."}]

    runtime = object.__new__(Orchestrator)
    runtime.voice_services = Services()
    runtime.conversation_commits = Commits()
    events: list[tuple[str, Any]] = []

    async def operation_state(**_kwargs: Any) -> tuple[OperationState, ExecutionFence]:
        return OperationState.COMPLETED, fence

    async def turn_state(
        value: Any,
        *,
        message: str,
        speech_outcome: str | None = None,
    ) -> None:
        events.append(("turn", (value, message, speech_outcome)))

    runtime._voice_dispatch_operation_state = operation_state
    runtime._broadcast_voice_turn_state = turn_state
    monkeypatch.setattr(
        "personalization.phi_gate.get_phi_gate",
        lambda: SimpleNamespace(detect_for_notice=lambda _text: False),
    )

    completed = await Orchestrator._finish_voice_chat_dispatch(
        runtime,
        voice_dispatch=_VoiceDispatchContext(
            admission=SimpleNamespace(turn=turn),
            connection_generation=str(uuid.uuid4()),
            origin=object(),
        ),
        user_id=turn.user_id,
        chat_id=turn.chat_id,
        stage=SimpleNamespace(
            sealed=True,
            committed=True,
            commit_id=result_commit_id,
            operation_fence=fence,
            summary_text="The report is ready.",
        ),
    )

    assert completed is True
    assert finish_kwargs["with_delivery_status"] is True
    assert events == [
        (
            "turn",
            (terminal_turn, _VOICE_REQUEST_SUCCEEDED_MESSAGE, "failed"),
        ),
    ]


def test_success_frame_reports_source_outcome_without_claiming_audibility() -> None:
    turn = replace(
        _voice_turn(
            state="succeeded",
            result_commit_id=str(uuid.uuid4()),
        ),
        is_foreground=False,
    )

    frame = Orchestrator._voice_turn_state_frame(
        turn,
        connection_generation=str(uuid.uuid4()),
        message=_VOICE_REQUEST_SUCCEEDED_MESSAGE,
        speech_outcome="source_finished",
    )

    assert frame["state"] == "succeeded"
    assert frame["speech_outcome"] == "source_finished"
    assert "audible" not in str(frame).lower()

    with pytest.raises(ValueError, match="invalid_voice_turn_speech_outcome"):
        Orchestrator._voice_turn_state_frame(
            replace(turn, state="failed"),
            connection_generation=str(uuid.uuid4()),
            message="failed",
            speech_outcome="source_finished",
        )
