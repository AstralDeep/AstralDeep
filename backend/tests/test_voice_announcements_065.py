"""Integrated, content-isolated announcement behavior for Feature 065."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.tests.test_voice_bootstrap_065 import (
    _eventually,
    _runner_services,
    _voice_turn,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_kind", "announcement_kind", "expected_text"),
    (
        ("failed", "failure", "I couldn't complete that request."),
        ("refused", "refusal", "I can't help with that request."),
        ("cancelled", "cancellation", "That request was cancelled."),
    ),
)
async def test_terminal_status_announcements_are_truthful_and_ephemeral(
    terminal_kind: str,
    announcement_kind: str,
    expected_text: str,
) -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-000000000171",
        turn_id="00000000-0000-4000-8000-000000000172",
    )
    services, clock, _repository, media = _runner_services(turn)
    runner = None
    try:
        start = asyncio.create_task(services.start_turn_announcements(turn))
        await _eventually(lambda: len(media.calls) == 1)
        media.finish()
        await start
        runner = services.announcement_runners[(turn.session_id, 1)]

        terminal = asyncio.create_task(
            services.finish_turn_announcements(
                turn,
                terminal_kind=terminal_kind,
                recap_text="provider detail must not be spoken",
                recap_source="terminal_status",
                sensitivity="unknown",
                result_commit_id=None,
            )
        )
        clock.advance(0.25)
        runner.wake()
        await _eventually(lambda: len(media.calls) == 2)
        assert media.calls[-1]["kind"] == announcement_kind
        assert media.calls[-1]["text"] == expected_text
        media.finish()
        completed = await terminal
        assert completed.state == terminal_kind
    finally:
        if runner is not None:
            await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_waiting_notice_is_once_only_until_normal_processing_resumes() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-000000000176",
        turn_id="00000000-0000-4000-8000-000000000177",
    )
    services, clock, _repository, media = _runner_services(turn)
    runner = None
    try:
        start = asyncio.create_task(services.start_turn_announcements(turn))
        await _eventually(lambda: len(media.calls) == 1)
        media.finish()
        await start
        runner = services.announcement_runners[(turn.session_id, 1)]

        waiting = asyncio.create_task(
            services.wait_turn_announcements(turn, waiting_reason="login")
        )
        clock.advance(0.25)
        runner.wake()
        await _eventually(lambda: len(media.calls) == 2)
        assert media.calls[-1]["kind"] == "waiting"
        assert media.calls[-1]["text"] == "Please sign in so I can continue."
        media.finish()
        await waiting

        clock.advance(65)
        runner.wake()
        await asyncio.sleep(0)
        assert len(media.calls) == 2

        await services.resume_turn_announcements(turn)
        clock.advance(14)
        runner.wake()
        await _eventually(lambda: len(media.calls) == 3)
        assert media.calls[-1]["kind"] == "progress"
        media.finish()
    finally:
        if runner is not None:
            await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_concurrent_results_are_serialized_and_audibly_attributed() -> None:
    session_id = "00000000-0000-4000-8000-000000000181"
    earlier = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-000000000182",
    )
    latest = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-000000000183",
    )
    services, clock, repository, media = _runner_services(earlier, latest)
    runner = None
    try:
        earlier_start = asyncio.create_task(services.start_turn_announcements(earlier))
        await _eventually(lambda: len(media.calls) == 1)
        latest_start = asyncio.create_task(services.start_turn_announcements(latest))
        runner = services.announcement_runners[(session_id, 1)]
        await _eventually(lambda: runner._scheduler.active_turn_count == 2)

        media.finish()
        await earlier_start
        clock.advance(0.25)
        runner.wake()
        await _eventually(lambda: len(media.calls) == 2)
        media.finish()
        await latest_start

        earlier_terminal = asyncio.create_task(
            services.finish_turn_announcements(
                earlier,
                terminal_kind="succeeded",
                recap_text="The earlier report is ready.",
                recap_source="authoritative_summary",
                sensitivity="non_sensitive",
                result_commit_id="00000000-0000-4000-8000-000000000184",
            )
        )
        latest_terminal = asyncio.create_task(
            services.finish_turn_announcements(
                latest,
                terminal_kind="succeeded",
                recap_text="The latest report is ready.",
                recap_source="authoritative_summary",
                sensitivity="non_sensitive",
                result_commit_id="00000000-0000-4000-8000-000000000185",
            )
        )

        await _eventually(lambda: len(media.calls) == 3)
        assert media.calls[2]["turn_id"] == earlier.turn_id
        assert media.calls[2]["text"] == "Earlier request done."
        media.finish()

        await _eventually(lambda: len(media.calls) == 4)
        assert media.calls[3]["turn_id"] == latest.turn_id
        assert media.calls[3]["text"] == "Latest request done."
        media.finish()

        for expected_count in (5, 6):
            await _eventually(lambda: len(media.calls) == expected_count)
            media.finish()

        await asyncio.gather(earlier_terminal, latest_terminal)
        assert [call["turn_id"] for call in media.calls[4:]] == [
            earlier.turn_id,
            latest.turn_id,
        ]
        assert [call["text"] for call in media.calls[4:]] == [
            "The earlier report is ready.",
            "The latest report is ready.",
        ]
        assert all(
            set(call) == {"turn_id", "kind", "text", "announcement_id"}
            for call in media.calls
        )
        # The announcement runner has no history/chat publication seam. Its
        # fake repository records only durable voice state and idle flags.
        assert set(repository.__dict__) == {"turns", "idle_updates"}
    finally:
        if runner is not None:
            await services._close_announcement_runner(session_id, 1)


@pytest.mark.asyncio
async def test_terminal_fence_and_mute_drop_stale_audio_without_catchup() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-000000000191",
        turn_id="00000000-0000-4000-8000-000000000192",
    )
    services, clock, repository, media = _runner_services(turn)
    runner = None
    try:
        start = asyncio.create_task(services.start_turn_announcements(turn))
        await _eventually(lambda: len(media.calls) == 1)
        media.finish()
        await start
        runner = services.announcement_runners[(turn.session_id, 1)]

        clock.advance(14)
        runner.wake()
        await _eventually(lambda: len(media.calls) == 2)
        assert media.calls[-1]["kind"] == "progress"

        await services.set_session_speech_muted(turn.session_id, 1, True)
        assert media.stops == 1
        terminal = await services.finish_turn_announcements(
            turn,
            terminal_kind="cancelled",
            recap_text="",
            recap_source="terminal_status",
            sensitivity="unknown",
            result_commit_id=None,
        )
        assert terminal.state == "cancelled"
        assert repository.turns[turn.turn_id].state == "cancelled"

        await services.set_session_speech_muted(turn.session_id, 1, False)
        clock.advance(65)
        runner.wake()
        await asyncio.sleep(0)
        assert [call["kind"] for call in media.calls] == [
            "acknowledgement",
            "progress",
        ]
    finally:
        if runner is not None:
            await services._close_announcement_runner(turn.session_id, 1)
