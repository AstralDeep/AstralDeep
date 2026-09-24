"""Tests for voice_agent/session.py's ASR-hallucination refusal: the voiced-frame floor
combined with the stock-phrase check, and that neither alone eats legitimate speech.
"""

from __future__ import annotations

import asyncio

import pytest

from voice_agent.session import (
    ASR_HALLUCINATION_MIN_VOICED_FRAMES,
    DirectRtcSession,
    SessionNotice,
)
from voice_agent.speech_adapters import Transcript
from voice_agent.tests.test_barge_in_065 import _audio_event
from voice_agent.tests.test_session_start_065 import (
    NOW,
    FakeAsr,
    FakeParticipant,
    FakePublication,
    FakeRoom,
    FakeRtcFactory,
    FakeTrack,
    FakeTts,
    FakeVad,
    _binding,
    _set_capture,
    _wait_for,
)


def _session(
    probabilities: list[float],
    transcripts: list[Transcript],
    notices: list[SessionNotice],
) -> DirectRtcSession:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    return DirectRtcSession(
        _binding(),
        rtc_factory=FakeRtcFactory(room),
        vad=FakeVad(probabilities),
        asr=FakeAsr(transcripts),
        tts=FakeTts(),
        notice_sink=notices.append,
        worker_control_secret=b"c" * 32,
        utcnow=lambda: NOW,
        monotonic=lambda: 0.0,
    )


async def _run_utterance(
    session: DirectRtcSession,
    *,
    frames: int,
    settled: object,
) -> set[str]:
    task = asyncio.create_task(session.run())
    try:
        await session.wait_started()
        session.deliver(_set_capture(True))
        await _wait_for(lambda: session.capture_open)
        epoch = session.capture_epoch
        for _ in range(frames):
            session._enqueue_rtc(_audio_event(epoch))
        await _wait_for(settled)
        return {item.text for item in session._retained_finals.values()}
    finally:
        await session.close("test")
        await task


def test_floor_is_256ms_of_voiced_evidence() -> None:
    assert ASR_HALLUCINATION_MIN_VOICED_FRAMES == 8


@pytest.mark.asyncio
async def test_stock_phrase_without_voiced_evidence_is_refused_silently() -> None:
    notices: list[SessionNotice] = []
    session = _session(
        [0.9] * 4 + [0.0] * 40,
        [Transcript("Thank you.", "en")],
        notices,
    )
    retained = await _run_utterance(
        session,
        frames=44,
        settled=lambda: any(
            item.kind == "recognition_failed"
            and item.reason == "hallucinated_transcript"
            for item in notices
        ),
    )
    assert retained == set()
    assert session._recognition_bindings == {}
    assert not any(item.kind == "transcript_emitted" for item in notices)


@pytest.mark.asyncio
async def test_stock_phrase_with_real_voiced_evidence_is_retained() -> None:
    notices: list[SessionNotice] = []
    session = _session(
        [0.9] * 12 + [0.0] * 40,
        [Transcript("Thank you.", "en")],
        notices,
    )
    retained = await _run_utterance(
        session,
        frames=52,
        settled=lambda: len(session._retained_finals) == 1,
    )
    assert retained == {"Thank you."}
    assert not any(
        item.kind == "recognition_failed"
        and item.reason == "hallucinated_transcript"
        for item in notices
    )


@pytest.mark.asyncio
async def test_short_command_below_floor_is_not_eaten() -> None:
    notices: list[SessionNotice] = []
    session = _session(
        [0.9] * 4 + [0.0] * 40,
        [Transcript("Stop.", "en")],
        notices,
    )
    retained = await _run_utterance(
        session,
        frames=44,
        settled=lambda: len(session._retained_finals) == 1,
    )
    assert retained == {"Stop."}
    assert not any(
        item.kind == "recognition_failed" for item in notices
    )
