"""Adversarial interruption guards for Feature 065 direct RTC speech."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

import pytest
import voice_agent.session as session_module
from voice_agent.session import (
    AUDIO_FRAME_SAMPLES,
    AUDIO_STREAM_SAMPLE_RATE,
    DirectRtcSession,
    OUTPUT_OPERATION_TIMEOUT_SECONDS,
    RtcSessionError,
    SPEECH_QUIESCE_TIMEOUT_SECONDS,
    SessionNotice,
    _OwnedEvent,
)
from voice_agent.speech_adapters import SynthesizedAudio, Transcript
from voice_agent.tests.test_session_start_065 import (
    NOW,
    FakeAsr,
    FakeAudioSource,
    FakeLocalParticipant,
    FakeLocalPublication,
    FakeParticipant,
    FakePublication,
    FakeRoom,
    FakeRtcFactory,
    FakeTrack,
    FakeTts,
    FakeVad,
    _binding,
    _set_capture,
    _speak,
    _uuid,
    _wait_for,
)


class SequencedLocalParticipant(FakeLocalParticipant):
    """Publish a distinct SID for every replacement output track."""

    async def publish_track(
        self,
        track: FakeTrack,
        options: Any,
    ) -> FakeLocalPublication:
        self.published.append((track, options))
        if self.publish_error is not None:
            raise self.publish_error
        return FakeLocalPublication(f"TR_output_{len(self.published)}")


class PlannedRtcFactory(FakeRtcFactory):
    def __init__(
        self,
        room: FakeRoom,
        sources: list[FakeAudioSource],
    ) -> None:
        super().__init__(room)
        self._planned_sources = deque(sources)

    def create_audio_source(
        self,
        *,
        sample_rate: int,
        num_channels: int,
        queue_size_ms: int,
    ) -> FakeAudioSource:
        self.source_calls.append(
            {
                "sample_rate": sample_rate,
                "num_channels": num_channels,
                "queue_size_ms": queue_size_ms,
            }
        )
        source = self._planned_sources.popleft()
        self.sources.append(source)
        return source


class CancellationResistantCaptureSource(FakeAudioSource):
    """Model one native capture call that acknowledges cancellation late."""

    def __init__(self, *, hold_close: bool = False) -> None:
        super().__init__()
        self.hold_close = hold_close
        self.cancel_seen = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()

    async def capture_frame(self, frame: bytes) -> None:
        self.capture_entered.set()
        try:
            await self.capture_release.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.capture_release.wait()
        # Deliberately append even after close. The worker's post-close clear
        # must remove this stale frame before replacing the source.
        self.frames.append(frame)

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        if self.hold_close:
            await self.close_release.wait()
        self.capture_release.set()


class HoldingPlayoutSource(FakeAudioSource):
    def __init__(self) -> None:
        super().__init__()
        self.playout_entered = asyncio.Event()
        self.playout_release = asyncio.Event()

    async def wait_for_playout(self) -> None:
        self.playout_calls += 1
        self.playout_entered.set()
        await self.playout_release.wait()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.playout_release.set()


class DelayedCancellationTts:
    """Return stale PCM after cancellation so epoch handling is exercised."""

    def __init__(self) -> None:
        self.entered = [asyncio.Event(), asyncio.Event()]
        self.release = [asyncio.Event(), asyncio.Event()]
        self.calls: list[tuple[str, int]] = []

    async def synthesize(
        self,
        text: str,
        *,
        max_duration_samples: int,
    ) -> SynthesizedAudio:
        index = len(self.calls)
        self.calls.append((text, max_duration_samples))
        self.entered[index].set()
        try:
            await self.release[index].wait()
        except asyncio.CancelledError:
            await self.release[index].wait()
        return SynthesizedAudio(
            pcm_s16le=bytes((index + 1, 0)) * 960,
            sample_rate=24_000,
            channels=1,
            sample_width_bytes=2,
            samples=960,
        )


def _direct_session(
    factory: FakeRtcFactory,
    *,
    vad: FakeVad | None = None,
    tts: Any | None = None,
    notices: list[SessionNotice] | None = None,
    input_audio_gate: Any | None = None,
) -> DirectRtcSession:
    destination = notices if notices is not None else []
    return DirectRtcSession(
        _binding(),
        rtc_factory=factory,
        vad=vad or FakeVad(),
        asr=FakeAsr(),
        tts=tts or FakeTts(),
        notice_sink=destination.append,
        worker_control_secret=b"c" * 32,
        utcnow=lambda: NOW,
        input_audio_gate=input_audio_gate,
    )


def _audio_event(epoch: int) -> _OwnedEvent:
    return _OwnedEvent(
        "audio_frame",
        (
            "TR_mic",
            b"\0\0" * AUDIO_FRAME_SAMPLES,
            AUDIO_STREAM_SAMPLE_RATE,
            1,
            AUDIO_FRAME_SAMPLES,
            0,
            epoch,
        ),
    )


def test_worker_interruption_operations_have_250ms_launch_ceilings() -> None:
    assert SPEECH_QUIESCE_TIMEOUT_SECONDS == 0.25
    assert OUTPUT_OPERATION_TIMEOUT_SECONDS == 0.25


@pytest.mark.asyncio
async def test_barge_in_fences_and_closes_output_before_capture_reopens() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    room.local_participant = SequencedLocalParticipant()
    old_source = CancellationResistantCaptureSource(hold_close=True)
    replacement_source = FakeAudioSource()
    factory = PlannedRtcFactory(room, [old_source, replacement_source])
    notices: list[SessionNotice] = []
    session = _direct_session(factory, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    listening_epoch = session.capture_epoch

    session.deliver(_speak())
    await old_source.capture_entered.wait()
    speaking_epoch = session.speech_epoch
    assert session.capture_open is False
    assert session.capture_epoch > listening_epoch

    session.deliver(_set_capture(True))
    await old_source.close_entered.wait()
    assert session.speech_epoch == speaking_epoch + 1
    assert session.capture_open is False
    assert old_source.clear_calls >= 2
    old_source.close_release.set()
    await _wait_for(lambda: session.capture_open)

    assert old_source.cancel_seen.is_set()
    assert old_source.frames == []
    assert old_source.close_calls == 1
    assert room.local_participant.unpublished == ["TR_output_1"]
    assert any(
        notice.kind == "speech_interrupted" and notice.reason == "barge_in"
        for notice in notices
    )

    session.deliver(_speak(announcement_id=_uuid(21)))
    await _wait_for(
        lambda: any(
            notice.kind == "speech_finished" and notice.announcement_id == _uuid(21)
            for notice in notices
        )
    )
    assert factory.sources == [old_source, replacement_source]
    assert room.local_participant.unpublished == ["TR_output_1", "TR_output_2"]
    assert session.capture_open is False
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_capture_timeout_discards_stale_frame_and_replaces_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "OUTPUT_OPERATION_TIMEOUT_SECONDS", 0.01)
    room = FakeRoom()
    room.local_participant = SequencedLocalParticipant()
    timed_out_source = CancellationResistantCaptureSource()
    replacement_source = FakeAudioSource()
    factory = PlannedRtcFactory(room, [timed_out_source, replacement_source])
    notices: list[SessionNotice] = []
    session = _direct_session(factory, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()

    session.deliver(_speak())
    await _wait_for(
        lambda: any(
            notice.kind == "speech_failed" and notice.reason == "output_capture_timeout"
            for notice in notices
        )
    )
    failure = next(notice for notice in notices if notice.kind == "speech_failed")
    assert failure.text is None
    assert timed_out_source.frames == []
    assert timed_out_source.clear_calls >= 2
    assert timed_out_source.close_calls == 1
    assert room.local_participant.unpublished == ["TR_output_1"]

    session.deliver(_speak(announcement_id=_uuid(21)))
    await _wait_for(
        lambda: any(
            notice.kind == "speech_finished" and notice.announcement_id == _uuid(21)
            for notice in notices
        )
    )
    assert factory.sources == [timed_out_source, replacement_source]
    assert room.local_participant.unpublished == ["TR_output_1", "TR_output_2"]

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_assistant_output_and_platform_aec_gate_suppress_asr_then_resume() -> (
    None
):
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    source = HoldingPlayoutSource()
    factory = PlannedRtcFactory(room, [source])
    vad = FakeVad([0.9, 0.9])
    aec_ready: bool | None = False
    gate_calls: list[str] = []

    def input_audio_gate(source_id: str) -> bool:
        gate_calls.append(source_id)
        if aec_ready is None:
            raise RuntimeError("acoustic payload must stay redacted")
        return aec_ready

    notices: list[SessionNotice] = []
    session = _direct_session(
        factory,
        vad=vad,
        notices=notices,
        input_audio_gate=input_audio_gate,
    )
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    session.deliver(_speak())
    await source.playout_entered.wait()
    assistant_epoch = session.capture_epoch
    factory.streams[0].feed()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.capture_open is False
    assert vad.calls == []

    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    resumed_epoch = session.capture_epoch
    assert resumed_epoch > assistant_epoch

    session._enqueue_rtc(_audio_event(resumed_epoch))
    await _wait_for(lambda: len(gate_calls) == 1)
    assert vad.calls == []
    assert gate_calls == ["TR_mic"]

    aec_ready = None
    session._enqueue_rtc(_audio_event(resumed_epoch))
    await _wait_for(lambda: len(gate_calls) == 2)
    assert vad.calls == []

    aec_ready = True
    gate_count = len(gate_calls)
    session._enqueue_rtc(_audio_event(assistant_epoch))
    session._enqueue_rtc(_audio_event(resumed_epoch))
    await _wait_for(lambda: len(vad.calls) == 1)
    assert len(gate_calls) == gate_count + 1
    assert gate_calls[-1] == "TR_mic"
    assert session.capture_open is True
    assert any(notice.kind == "speech_interrupted" for notice in notices)

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_source_drain_and_client_terminal_keep_capture_closed_for_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_module,
        "POST_PLAYOUT_CAPTURE_GUARD_SECONDS",
        0.01,
    )
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    vad = FakeVad([0.9])
    notices: list[SessionNotice] = []
    session = _direct_session(factory, vad=vad, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    session.deliver(_speak())
    await _wait_for(lambda: any(item.kind == "speech_finished" for item in notices))

    assert factory.sources[0].playout_calls == 1
    finished = next(item for item in notices if item.kind == "speech_finished")
    assert finished.text is None
    assert "client_playout" not in finished.metadata
    assert not any("playout" in item.kind for item in notices)
    assert session.capture_open is False

    session._enqueue_rtc(_audio_event(session.capture_epoch))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert vad.calls == []

    # This generation-fenced command models the coordinator's release only
    # after an exact authenticated client terminal playout event.
    tail_epoch = session.capture_epoch
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session._playout_tail_guard)
    assert session.capture_open is False

    # A confirmation timeout already queued at the terminal boundary is stale
    # once the authenticated release has entered its tail phase.
    session._enqueue_rtc(
        _OwnedEvent("client_playout_timeout", (session._playout_hold_epoch,))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert task.done() is False

    # Render-tail frames that arrive after the client terminal remain fenced,
    # and their old epoch cannot cross the later listening transition.
    session._enqueue_rtc(_audio_event(tail_epoch))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert vad.calls == []
    await _wait_for(lambda: session.capture_open)
    assert session.capture_epoch > tail_epoch
    session._enqueue_rtc(_audio_event(tail_epoch))
    session._enqueue_rtc(_audio_event(session.capture_epoch))
    await _wait_for(lambda: len(vad.calls) == 1)

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_authenticated_barge_in_releases_post_source_playout_hold_immediately() -> (
    None
):
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    notices: list[SessionNotice] = []
    session = _direct_session(FakeRtcFactory(room), notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    session.deliver(_speak())
    await _wait_for(lambda: any(item.kind == "speech_finished" for item in notices))
    assert session.capture_open is False
    assert session._playout_capture_hold is True
    assert session._playout_confirmation_task is not None

    session.deliver(
        {
            "type": "stop_speech",
            "session_id": _uuid(1),
            "generation": 1,
            "media_grant_revision": 1,
            "reason": "barge_in",
        }
    )
    await _wait_for(lambda: session.capture_open)

    assert session._playout_capture_hold is False
    assert session._playout_tail_guard is False
    assert session._playout_confirmation_task is None

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_recent_speech_fingerprint_suppresses_only_bounded_exact_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_module,
        "POST_PLAYOUT_CAPTURE_GUARD_SECONDS",
        0.001,
    )
    suppression_window = session_module.SELF_SPEECH_SUPPRESSION_WINDOW_SECONDS
    monotonic_now = [0.0]
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    probabilities = ([0.9] * 4 + [0.0] * 40) * 3
    vad = FakeVad(probabilities)
    asr = FakeAsr(
        [
            Transcript(" hello, WHAT can I help with ", "en"),
            Transcript("Please show my tasks", "en"),
            Transcript("Hello. What can I help with?", "en"),
        ]
    )
    notices: list[SessionNotice] = []
    session = DirectRtcSession(
        _binding(),
        rtc_factory=FakeRtcFactory(room),
        vad=vad,
        asr=asr,
        tts=FakeTts(),
        notice_sink=notices.append,
        worker_control_secret=b"c" * 32,
        utcnow=lambda: NOW,
        monotonic=lambda: monotonic_now[0],
    )
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    session.deliver(_speak())
    await _wait_for(lambda: any(item.kind == "speech_finished" for item in notices))
    assert session._recent_speech_fingerprints == deque()

    # A playout longer than the suppression window must not age out the
    # fingerprint before the exact authenticated terminal release.
    monotonic_now[0] += suppression_window + 1.0
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    assert len(session._recent_speech_fingerprints) == 1
    fingerprint = session._recent_speech_fingerprints[0]
    assert isinstance(fingerprint.digest, bytes)
    assert not hasattr(fingerprint, "text")

    def feed_utterance() -> None:
        epoch = session.capture_epoch
        for _ in range(44):
            session._enqueue_rtc(_audio_event(epoch))

    feed_utterance()
    await _wait_for(
        lambda: any(
            item.kind == "recognition_failed" and item.reason == "self_speech"
            for item in notices
        )
    )
    assert session._retained_finals == {}
    assert session._recognition_bindings == {}
    await _wait_for(lambda: session.capture_open)

    feed_utterance()
    await _wait_for(lambda: len(session._retained_finals) == 1)
    assert {item.text for item in session._retained_finals.values()} == {
        "Please show my tasks"
    }
    await _wait_for(lambda: session.capture_open)

    monotonic_now[0] += suppression_window + 1.0
    feed_utterance()
    await _wait_for(lambda: len(session._retained_finals) == 2)
    assert {item.text for item in session._retained_finals.values()} == {
        "Hello. What can I help with?",
        "Please show my tasks",
    }

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_missing_client_playout_confirmation_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_module,
        "CLIENT_PLAYOUT_CONFIRMATION_TIMEOUT_SECONDS",
        0.01,
    )
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    notices: list[SessionNotice] = []
    session = _direct_session(FakeRtcFactory(room), notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    session.deliver(_speak())

    with pytest.raises(RtcSessionError, match="client_playout_timeout"):
        await asyncio.wait_for(task, timeout=1.0)
    assert session.capture_open is False
    assert any(
        notice.kind == "media_state"
        and notice.reason == "client_playout_timeout"
        and notice.metadata.get("state") == "failed"
        for notice in notices
    )


@pytest.mark.asyncio
async def test_mute_waits_for_client_terminal_before_capture_reopens() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    source = HoldingPlayoutSource()
    notices: list[SessionNotice] = []
    session = _direct_session(
        PlannedRtcFactory(room, [source]),
        notices=notices,
    )
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    session.deliver(_speak())
    await source.playout_entered.wait()

    session.deliver(
        {
            "type": "stop_speech",
            "session_id": _uuid(1),
            "generation": 1,
            "media_grant_revision": 1,
            "reason": "mute",
        }
    )
    await _wait_for(
        lambda: any(
            notice.kind == "speech_interrupted" and notice.reason == "mute"
            for notice in notices
        )
    )
    assert session.capture_open is False
    assert session._playout_capture_hold is True
    await _wait_for(lambda: session._playout_confirmation_task is not None)

    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    assert session._playout_capture_hold is False
    assert session._playout_confirmation_task is None

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_teardown_cancels_pending_playout_confirmation_without_reopening() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    session = _direct_session(FakeRtcFactory(room))
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    session.deliver(_speak())
    await _wait_for(lambda: session._playout_confirmation_task is not None)
    assert session.capture_open is False

    await session.close("test")
    await task
    assert session.capture_open is False
    assert session._playout_capture_hold is False
    assert session._playout_confirmation_task is None


@pytest.mark.asyncio
async def test_stale_recap_completion_cannot_replace_current_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "SPEECH_QUIESCE_TIMEOUT_SECONDS", 0.01)
    room = FakeRoom()
    room.local_participant = SequencedLocalParticipant()
    factory = FakeRtcFactory(room)
    tts = DelayedCancellationTts()
    notices: list[SessionNotice] = []
    session = _direct_session(factory, tts=tts, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()

    session.deliver(_speak())
    await tts.entered[0].wait()
    session.deliver(_speak(announcement_id=_uuid(21)))
    await tts.entered[1].wait()
    current_synthesis = session._synthesis_task
    assert current_synthesis is not None

    tts.release[0].set()
    await _wait_for(lambda: len(session._retired_output_tasks) == 0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session._synthesis_task is current_synthesis
    assert room.local_participant.published == []

    tts.release[1].set()
    await _wait_for(
        lambda: any(
            notice.kind == "speech_finished" and notice.announcement_id == _uuid(21)
            for notice in notices
        )
    )
    manifests = [
        json.loads(record["payload"])
        for record in room.local_participant.published_data
    ]
    assert [manifest["announcement_id"] for manifest in manifests] == [_uuid(21)]
    assert not any(
        notice.kind in {"speech_started", "speech_finished"}
        and notice.announcement_id == _uuid(20)
        for notice in notices
    )

    await session.close("test")
    await task
