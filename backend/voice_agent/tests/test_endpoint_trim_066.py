"""Feature 066 latency: trailing-silence trim and the tunable endpoint."""

from __future__ import annotations

import asyncio

import pytest

from voice_agent.session import (
    ASR_TAIL_SILENCE_FRAMES,
    AUDIO_FRAME_SAMPLES,
    VAD_END_SILENCE_FRAMES,
    _endpoint_silence_frames,
)
from voice_agent.speech_adapters import Transcript
from voice_agent.tests.test_session_start_065 import (
    FakeAsr,
    FakeParticipant,
    FakePublication,
    FakeRoom,
    FakeRtcFactory,
    FakeTrack,
    FakeVad,
    _session,
    _set_capture,
    _wait_for,
)

_FRAME_BYTES = AUDIO_FRAME_SAMPLES * 2
_SPEECH_PCM = b"\x11\x22" * AUDIO_FRAME_SAMPLES
_LATER_SPEECH_PCM = b"\x33\x44" * AUDIO_FRAME_SAMPLES
_SILENCE_PCM = b"\x00\x00" * AUDIO_FRAME_SAMPLES


@pytest.mark.asyncio
async def test_trailing_endpoint_silence_trims_to_tail_with_speech_intact() -> None:
    """The ASR POST carries the speech bytes untouched plus a 128-ms tail."""

    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    speech_frames = 8
    vad = FakeVad([0.9] * speech_frames + [0.1] * VAD_END_SILENCE_FRAMES)
    asr = FakeAsr([Transcript("Book the follow-up", "en")])
    session = _session(factory, vad=vad, asr=asr)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    for _ in range(speech_frames):
        factory.streams[0].feed(_SPEECH_PCM)
    for _ in range(VAD_END_SILENCE_FRAMES):
        factory.streams[0].feed(_SILENCE_PCM)
    await _wait_for(lambda: len(asr.calls) == 1)

    assert asr.calls[0] == (
        _SPEECH_PCM * speech_frames + _SILENCE_PCM * ASR_TAIL_SILENCE_FRAMES
    )

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_internal_clause_pause_is_never_trimmed() -> None:
    """Only the trailing run is trimmed; bridged mid-utterance pauses stay."""

    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    pause_frames = VAD_END_SILENCE_FRAMES - 2
    vad = FakeVad(
        [0.9] * 4
        + [0.1] * pause_frames
        + [0.9] * 3
        + [0.1] * VAD_END_SILENCE_FRAMES
    )
    asr = FakeAsr([Transcript("Two clauses, one turn", "en")])
    session = _session(factory, vad=vad, asr=asr)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    # Fed in two batches so the bounded RTC event queue is never overrun.
    for _ in range(4):
        factory.streams[0].feed(_SPEECH_PCM)
    for _ in range(pause_frames):
        factory.streams[0].feed(_SILENCE_PCM)
    for _ in range(3):
        factory.streams[0].feed(_LATER_SPEECH_PCM)
    await _wait_for(lambda: len(vad.calls) == 4 + pause_frames + 3)
    for _ in range(VAD_END_SILENCE_FRAMES):
        factory.streams[0].feed(_SILENCE_PCM)
    await _wait_for(lambda: len(asr.calls) == 1)

    assert asr.calls[0] == (
        _SPEECH_PCM * 4
        + _SILENCE_PCM * pause_frames
        + _LATER_SPEECH_PCM * 3
        + _SILENCE_PCM * ASR_TAIL_SILENCE_FRAMES
    )

    await session.close("test")
    await task


def test_short_silence_run_is_not_over_trimmed() -> None:
    """A run at or under the retained tail (max-length finalize) trims zero."""

    factory = FakeRtcFactory(FakeRoom([FakeParticipant("client-a", [])]))
    session = _session(factory)
    untouched = _SPEECH_PCM * 6 + _SILENCE_PCM * 2
    session._utterance.extend(untouched)
    session._silence_frames = 2
    session._trim_trailing_silence()
    assert bytes(session._utterance) == untouched

    session._silence_frames = ASR_TAIL_SILENCE_FRAMES
    session._trim_trailing_silence()
    assert bytes(session._utterance) == untouched


def test_trim_keeps_exactly_the_context_tail() -> None:
    factory = FakeRtcFactory(FakeRoom([FakeParticipant("client-a", [])]))
    session = _session(factory)
    session._utterance.extend(
        _SPEECH_PCM * 2 + _SILENCE_PCM * (ASR_TAIL_SILENCE_FRAMES + 3)
    )
    session._silence_frames = ASR_TAIL_SILENCE_FRAMES + 3
    session._trim_trailing_silence()
    assert bytes(session._utterance) == (
        _SPEECH_PCM * 2 + _SILENCE_PCM * ASR_TAIL_SILENCE_FRAMES
    )


def test_trim_fails_closed_rather_than_emptying_the_buffer() -> None:
    """An inconsistent counter larger than the buffer must trim nothing."""

    factory = FakeRtcFactory(FakeRoom([FakeParticipant("client-a", [])]))
    session = _session(factory)
    session._utterance.extend(_SILENCE_PCM * 2)
    session._silence_frames = VAD_END_SILENCE_FRAMES + 50
    session._trim_trailing_silence()
    assert len(session._utterance) == 2 * _FRAME_BYTES


@pytest.mark.parametrize(
    ("raw", "expected_frames"),
    (
        (None, 30),  # unset -> 960 ms default (30 x 32 ms frames)
        ("", 30),  # compose passes empty when the operator sets nothing
        ("garbage", 30),  # invalid -> default, never a crash
        ("1.5", 30),  # non-integer -> default
        ("960", 30),
        ("1000", 31),  # rounded to whole 32 ms frames
        ("320", 10),  # floor of the sane range
        ("100", 10),  # clamped up to 320 ms
        ("-5000", 10),
        ("2560", 80),  # ceiling of the sane range
        ("999999", 80),  # clamped down to 2560 ms
    ),
)
def test_endpoint_silence_env_parsing_and_clamping(
    raw: str | None, expected_frames: int
) -> None:
    assert _endpoint_silence_frames(raw) == expected_frames


def test_endpoint_default_is_within_the_clamp_range() -> None:
    """The import-time constant always lands inside [320, 2560] ms."""

    assert 10 <= VAD_END_SILENCE_FRAMES <= 80
