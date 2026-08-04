"""066 R-9 follow-through: bounded VAD pre-roll pins.

Speech onsets ramp from below the release threshold and the candidate
buffer clears on any sub-release frame, so the head of an utterance was
lost ("transcribed from the middle"). The ring retains the last
VAD_PREROLL_FRAMES admitted frames regardless of posterior dips and seeds
the utterance at activation. A capture-epoch change (fence transition)
clears it so pre-fence audio never resurfaces.
"""

from __future__ import annotations

import asyncio

import pytest

from voice_agent import session as session_module
from voice_agent.session import (
    ASR_TAIL_SILENCE_FRAMES,
    AUDIO_FRAME_SAMPLES,
    AUDIO_STREAM_SAMPLE_RATE,
    VAD_PREROLL_FRAMES,
    DirectRtcSession,
    SessionNotice,
    _OwnedEvent,
)
from voice_agent.tests.test_session_start_065 import (
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


FRAME_BYTES = AUDIO_FRAME_SAMPLES * 2


def _frame_event(epoch: int, fill: int) -> _OwnedEvent:
    return _OwnedEvent(
        "audio_frame",
        (
            "TR_mic",
            bytes([fill, 0]) * AUDIO_FRAME_SAMPLES,
            AUDIO_STREAM_SAMPLE_RATE,
            1,
            AUDIO_FRAME_SAMPLES,
            0,
            epoch,
        ),
    )


def _session(vad: FakeVad, asr: FakeAsr, notices: list[SessionNotice]):
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    return DirectRtcSession(
        _binding(),
        rtc_factory=FakeRtcFactory(room),
        vad=vad,
        asr=asr,
        tts=FakeTts(),
        notice_sink=notices.append,
        worker_control_secret=b"c" * 32,
    )


@pytest.mark.asyncio
async def test_onset_frames_below_release_are_retained_via_preroll() -> None:
    # Three onset frames score BELOW release (discarded by the candidate
    # logic), then confident speech activates. The ASR audio must contain
    # the onset frames — previously they were structurally lost.
    silence_frames = session_module.VAD_END_SILENCE_FRAMES
    probabilities = [0.1, 0.2, 0.3] + [0.9] * 5 + [0.0] * silence_frames
    vad = FakeVad(list(probabilities))
    asr = FakeAsr()
    notices: list[SessionNotice] = []
    session = _session(vad, asr, notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    epoch = session.capture_epoch
    for index in range(len(probabilities)):
        session._enqueue_rtc(_frame_event(epoch, fill=index + 1))
    await _wait_for(lambda: len(asr.calls) == 1)

    audio = asr.calls[0]
    # Onset fills 1..3 (sub-release) must be present — the pre-roll kept them.
    assert bytes([1, 0]) * AUDIO_FRAME_SAMPLES in audio
    assert bytes([2, 0]) * AUDIO_FRAME_SAMPLES in audio
    assert bytes([3, 0]) * AUDIO_FRAME_SAMPLES in audio
    # Speech fills are present too, and the total is onset + speech + tail.
    assert bytes([4, 0]) * AUDIO_FRAME_SAMPLES in audio
    assert len(audio) == (3 + 5 + ASR_TAIL_SILENCE_FRAMES) * FRAME_BYTES

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_preroll_is_bounded_to_its_ring_capacity() -> None:
    # Long ambient run below release, then speech: only the last
    # VAD_PREROLL_FRAMES of context may seed the utterance.
    silence_frames = session_module.VAD_END_SILENCE_FRAMES
    ambient = [0.1] * (VAD_PREROLL_FRAMES * 2)
    probabilities = ambient + [0.9] * 5 + [0.0] * silence_frames
    vad = FakeVad(list(probabilities))
    asr = FakeAsr()
    notices: list[SessionNotice] = []
    session = _session(vad, asr, notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    epoch = session.capture_epoch
    for index in range(len(probabilities)):
        session._enqueue_rtc(_frame_event(epoch, fill=(index % 251) + 1))
        # Let the session loop drain each frame: this feed is longer than
        # the bounded rtc queue and would overrun in a synchronous burst.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    await _wait_for(lambda: len(asr.calls) == 1)

    audio = asr.calls[0]
    # Activation fires on the 4th speech frame: the ring then holds exactly
    # VAD_PREROLL_FRAMES frames (ambient tail + those 4 speech frames). The
    # 5th speech frame extends post-activation, and the trailing silence is
    # trimmed to the retained ASR tail.
    expected_frames = VAD_PREROLL_FRAMES + 1 + ASR_TAIL_SILENCE_FRAMES
    assert len(audio) == expected_frames * FRAME_BYTES

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_epoch_change_clears_preroll() -> None:
    # Frames admitted under an earlier capture epoch must never seed a turn
    # after a fence transition.
    silence_frames = session_module.VAD_END_SILENCE_FRAMES
    probabilities = [0.1, 0.1] + [0.9] * 5 + [0.0] * silence_frames
    vad = FakeVad(list(probabilities))
    asr = FakeAsr()
    notices: list[SessionNotice] = []
    session = _session(vad, asr, notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    first_epoch = session.capture_epoch
    session._enqueue_rtc(_frame_event(first_epoch, fill=201))
    session._enqueue_rtc(_frame_event(first_epoch, fill=202))
    await _wait_for(lambda: len(vad.calls) == 2)

    # Fence transition: close and reopen capture (epoch bumps).
    session.deliver(_set_capture(False))
    await _wait_for(lambda: not session.capture_open)
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    second_epoch = session.capture_epoch
    assert second_epoch != first_epoch

    for index in range(5 + silence_frames):
        session._enqueue_rtc(_frame_event(second_epoch, fill=index + 1))
    await _wait_for(lambda: len(asr.calls) == 1)

    audio = asr.calls[0]
    assert bytes([201, 0]) * AUDIO_FRAME_SAMPLES not in audio
    assert bytes([202, 0]) * AUDIO_FRAME_SAMPLES not in audio

    await session.close("test")
    await task
