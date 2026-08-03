"""Feature 065 Windows manifest-led direct-RTC playout behavior."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass

import pytest

pytest.importorskip("PySide6")
rtc = pytest.importorskip("livekit.rtc")

from astral_client.voice import (  # noqa: E402
    LiveKitRoomSession,
    VOICE_ANNOUNCEMENT_TOPIC,
    VoiceController,
)


DEVICE = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"
CHAT = "00000000-0000-4000-8000-000000000003"
SESSION = "00000000-0000-4000-8000-000000000004"
TURN = "00000000-0000-4000-8000-000000000005"
BINDING_ID = "00000000-0000-4000-8000-000000000009"
BINDING = "v1." + "a" * 64 + "." + "b" * 43
WORKER = "voice-worker-a"


def _uuid(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def _manifest(
    sequence: int = 1,
    *,
    kind: str = "progress",
    role: str = "single",
    quantum_index: int = 0,
    duration_samples: int = 100,
    reservation: int | None = None,
    turn_id: str | None = TURN,
    track_sid: str | None = None,
    track_name: str | None = None,
) -> dict:
    announcement_id = _uuid(100 + sequence)
    value = {
        "type": "voice_announcement_media",
        "schema_version": "1",
        "session_id": SESSION,
        "generation": 2,
        "media_grant_revision": 4,
        "announcement_id": announcement_id,
        "announcement_sequence": sequence,
        "turn_id": None if kind == "greeting" else turn_id,
        "kind": kind,
        "quantum_role": role,
        "quantum_index": quantum_index,
        "transport": "livekit",
        "worker_identity": WORKER,
        "sample_rate_hz": 24_000,
        "duration_samples": duration_samples,
        "track_sid": track_sid or f"TR_{sequence}",
        "track_name": track_name or f"astraldeep.voice.{announcement_id}",
    }
    if reservation is not None:
        value["result_reserved_samples_after"] = reservation
    return value


class _Transport:
    def __init__(self) -> None:
        self.connection_generation = CONNECTION
        self.playout: list[dict] = []

    def send_voice_transcript(self, _frame: dict) -> None:
        pass

    def send_voice_playout_event(self, frame: dict) -> None:
        self.playout.append(copy.deepcopy(frame))


class _ControllerAudio:
    def capability(self) -> dict:
        return {
            "has_microphone": True,
            "has_audio_output": True,
            "microphone_permission": "authorized",
            "full_duplex": True,
            "transport": "livekit",
        }

    @staticmethod
    def request_microphone_permission(callback) -> None:
        callback("authorized")

    @staticmethod
    def stop_all() -> None:
        pass


class _ControllerHttp:
    @staticmethod
    def capability() -> dict:
        return {"schema_version": "1", "status": "ready", "reason": "ready"}

    @staticmethod
    def create(_body, _scope) -> dict:
        return {
            "session": {
                "session_id": SESSION,
                "device_id": DEVICE,
                "device_kind": "windows",
                "transport": "livekit",
                "state": "active",
                "generation": 2,
                "media_grant_revision": 4,
                "owner_connection_generation": CONNECTION,
                "visible_chat_id": CHAT,
                "applied_visible_chat_id": CHAT,
                "chat_context_revision": 3,
                "applied_chat_context_revision": 3,
                "chat_context_synced": True,
                "foreground_active": True,
                "foreground_reason": "foreground",
                "foreground_changed_at": "2026-07-31T18:00:00Z",
                "speech_muted": False,
                "microphone_enabled": True,
                "lease_expires_at": "2099-07-31T18:04:00Z",
                "started_at": "2026-07-31T18:00:00Z",
                "idle_expires_at": None,
            },
            "grant": {
                "grant_id": "grant-a",
                "transport": "livekit",
                "session_id": SESSION,
                "generation": 2,
                "media_grant_revision": 4,
                "expires_at": "2099-07-31T18:05:00Z",
                "url": "ws://127.0.0.1:7880",
                "join_token": "secret-client-room-token-" + "x" * 40,
                "room_name": "voice-room",
                "participant_identity": "voice-client-a",
                "worker_identity": WORKER,
            },
        }


class _ControllerMedia:
    def __init__(self) -> None:
        self.authorized: list[dict] = []
        self.on_data = None
        self.on_playout = None

    def connect(self, _grant, _audio, on_data, on_state, on_playout) -> None:
        self.on_data = on_data
        self.on_playout = on_playout
        on_state("connected", "")

    def authorize_announcement(self, manifest: dict) -> None:
        self.authorized.append(copy.deepcopy(manifest))

    @staticmethod
    def set_microphone_enabled(_enabled: bool) -> None:
        pass

    @staticmethod
    def stop_playback() -> None:
        pass

    @staticmethod
    def close() -> None:
        pass


def _active_controller() -> tuple[VoiceController, _Transport, _ControllerMedia]:
    transport = _Transport()
    media = _ControllerMedia()
    controller = VoiceController(
        device_id=DEVICE,
        token_provider=lambda: "token",
        http_base="http://127.0.0.1:8001",
        connection_provider=lambda: transport.connection_generation,
        chat_provider=lambda: CHAT,
        transport=transport,
        audio=_ControllerAudio(),
        http=_ControllerHttp(),
        media=media,
        run_async=lambda work: work(),
    )
    assert controller.accept_frame(
        {
            "type": "voice_control_binding",
            "schema_version": "1",
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
            "binding_id": BINDING_ID,
            "binding": BINDING,
            "expires_at": "2099-07-31T18:04:00Z",
        }
    )
    controller.handle_action("voice_session_start")
    return controller, transport, media


def test_controller_authorizes_only_strict_current_expected_worker_manifests(qapp):
    controller, _transport, media = _active_controller()
    valid = _manifest()

    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, valid)
    assert media.authorized == [valid]

    invalid_values = []
    wrong_sender = _manifest(2)
    invalid_values.append(("voice-worker-b", wrong_sender))
    stale = _manifest(2)
    stale["generation"] = 1
    invalid_values.append((WORKER, stale))
    over_budget = _manifest(2, duration_samples=96_001)
    invalid_values.append((WORKER, over_budget))
    extra = _manifest(2)
    extra["text"] = "must never cross the content-free manifest seam"
    invalid_values.append((WORKER, extra))
    duplicate_track = _manifest(2, track_sid=valid["track_sid"])
    invalid_values.append((WORKER, duplicate_track))

    for sender, value in invalid_values:
        media.on_data(VOICE_ANNOUNCEMENT_TOPIC, sender, value)
    assert media.authorized == [valid]
    assert controller._last_announcement_sequence == 1


def test_controller_enforces_result_quantum_reservation_and_index_chain(qapp):
    _controller, _transport, media = _active_controller()
    opening = _manifest(
        1,
        kind="result",
        role="result_opening",
        duration_samples=30_000,
        reservation=36_000,
    )
    continuation = _manifest(
        2,
        kind="result",
        role="result_continuation",
        quantum_index=1,
        duration_samples=90_000,
        reservation=132_000,
    )
    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, opening)
    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, continuation)
    assert media.authorized == [opening, continuation]

    skipped_index = _manifest(
        3,
        kind="result",
        role="result_continuation",
        quantum_index=3,
        duration_samples=1,
        reservation=132_001,
    )
    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, skipped_index)
    assert media.authorized == [opening, continuation]

    valid_next = _manifest(
        3,
        kind="result",
        role="result_continuation",
        quantum_index=2,
        duration_samples=96_000,
        reservation=228_000,
    )
    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, valid_next)
    assert media.authorized[-1] == valid_next


def test_muted_or_background_manifest_is_consumed_and_never_replayed(qapp):
    controller, _transport, media = _active_controller()
    muted = _manifest(1)
    controller.speech_muted = True
    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, muted)
    controller.speech_muted = False
    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, muted)
    assert not media.authorized

    controller._foreground_active = False
    background = _manifest(2)
    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, background)
    controller._foreground_active = True
    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, background)
    assert not media.authorized

    current = _manifest(3)
    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, current)
    assert media.authorized == [current]


def test_controller_emits_content_free_correlated_local_playout_lifecycle(qapp):
    _controller, transport, media = _active_controller()
    manifest = _manifest(
        1,
        kind="result",
        role="result_opening",
        duration_samples=24_000,
        reservation=30_000,
    )
    media.on_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, manifest)
    media.on_playout(manifest, "started")
    media.on_playout(manifest, "finished")

    assert [event["phase"] for event in transport.playout] == ["started", "finished"]
    assert [event["client_sequence"] for event in transport.playout] == [0, 1]
    for event in transport.playout:
        assert event["device_id"] == DEVICE
        assert event["connection_generation"] == CONNECTION
        assert event["session_id"] == SESSION
        assert event["announcement_id"] == manifest["announcement_id"]
        assert event["result_reserved_samples_after"] == 30_000
        assert "text" not in event
        assert set(event) == {
            "type",
            "schema_version",
            "device_id",
            "connection_generation",
            "session_id",
            "generation",
            "media_grant_revision",
            "announcement_id",
            "announcement_sequence",
            "turn_id",
            "kind",
            "quantum_role",
            "quantum_index",
            "result_reserved_samples_after",
            "phase",
            "client_sequence",
            "observed_at",
        }


@dataclass
class _Frame:
    samples_per_channel: int
    sample_rate: int = 24_000
    num_channels: int = 1
    data: bytes | None = None

    def __post_init__(self) -> None:
        if self.data is None:
            self.data = bytes(self.samples_per_channel * 2)


@dataclass
class _Event:
    frame: _Frame


class _Stream:
    def __init__(self, frames: list[_Frame]) -> None:
        self._events = iter(_Event(frame) for frame in frames)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class _Track:
    kind = rtc.TrackKind.KIND_AUDIO

    def __init__(self, sid: str, name: str) -> None:
        self.sid = sid
        self.name = name


class _Publication:
    kind = rtc.TrackKind.KIND_AUDIO

    def __init__(self, sid: str, name: str, track: _Track | None = None) -> None:
        self.sid = sid
        self.name = name
        self.track = track
        self.subscriptions: list[bool] = []

    def set_subscribed(self, subscribed: bool) -> None:
        self.subscriptions.append(subscribed)


class _PlayoutAudio:
    def __init__(self, *, auto_finish: bool = True) -> None:
        self.auto_finish = auto_finish
        self.begun: list[tuple[str, int, int, int]] = []
        self.chunks: dict[str, list[bytes]] = {}
        self.sealed: list[str] = []
        self.interrupted: list[str] = []
        self.overlap = False
        self.current: str | None = None
        self._started = None
        self._finished = None

    def begin_playout(
        self,
        playout_id,
        sample_rate,
        channels,
        duration_samples,
        on_started,
        on_finished,
    ) -> None:
        if self.current is not None:
            self.overlap = True
        self.current = playout_id
        self._started = on_started
        self._finished = on_finished
        self.begun.append((playout_id, sample_rate, channels, duration_samples))
        self.chunks[playout_id] = []

    def push_playout(self, playout_id: str, data: bytes) -> None:
        assert self.current == playout_id
        self.chunks[playout_id].append(bytes(data))
        callback, self._started = self._started, None
        if callback is not None:
            callback()

    def seal_playout(self, playout_id: str) -> None:
        assert self.current == playout_id
        self.sealed.append(playout_id)
        if self.auto_finish:
            self.finish_current("finished")

    def interrupt_playout(self, playout_id: str | None = None) -> None:
        if self.current is None or (playout_id is not None and playout_id != self.current):
            return
        self.interrupted.append(self.current)
        self.finish_current("interrupted")

    def finish_current(self, phase: str) -> None:
        callback = self._finished
        self.current = None
        self._started = None
        self._finished = None
        if callback is not None:
            callback(phase)


def _media_session(streams: dict[str, list[_Frame]], audio: _PlayoutAudio):
    created: list[_Stream] = []

    def factory(track, **options):
        assert options == {"sample_rate": 24_000, "num_channels": 1, "capacity": 16}
        stream = _Stream(streams[track.sid])
        created.append(stream)
        return stream

    session = LiveKitRoomSession(stream_factory=factory)
    session._loop = asyncio.get_running_loop()
    session._grant = {"worker_identity": WORKER}
    session._audio = audio
    phases: list[tuple[str, str]] = []
    session._on_playout = lambda manifest, phase: phases.append(
        (manifest["announcement_id"], phase)
    )
    return session, phases, created


def _publication(manifest: dict, *, name: str | None = None) -> _Publication:
    actual_name = name or manifest["track_name"]
    track = _Track(manifest["track_sid"], actual_name)
    return _Publication(manifest["track_sid"], actual_name, track)


async def _spin_until(predicate, *, attempts: int = 50) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("async voice state did not settle")


def test_media_matches_manifest_and_hard_stops_at_exact_declared_samples(qapp):
    async def scenario() -> None:
        manifest = _manifest(duration_samples=100)
        audio = _PlayoutAudio()
        session, phases, streams = _media_session(
            {manifest["track_sid"]: [_Frame(60), _Frame(80)]}, audio
        )
        publication = _publication(manifest)

        session._authorize_announcement(manifest)
        session._remember_publication(publication, WORKER)
        await _spin_until(lambda: session._active_playout is None)

        assert audio.begun == [(manifest["announcement_id"], 24_000, 1, 100)]
        assert [len(chunk) for chunk in audio.chunks[manifest["announcement_id"]]] == [120, 80]
        assert sum(map(len, audio.chunks[manifest["announcement_id"]])) == 200
        assert audio.sealed == [manifest["announcement_id"]]
        assert phases == [
            (manifest["announcement_id"], "started"),
            (manifest["announcement_id"], "finished"),
        ]
        assert publication.subscriptions == [True, False]
        assert streams[0].closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("frames", "started"),
    (
        ([_Frame(60)], True),
        ([_Frame(60, sample_rate=48_000), _Frame(40)], False),
        ([_Frame(60, data=bytes(118)), _Frame(40)], False),
    ),
)
def test_media_underrun_or_malformed_pcm_interrupts_without_claiming_finish(
    qapp, frames, started
):
    async def scenario() -> None:
        manifest = _manifest(duration_samples=100)
        audio = _PlayoutAudio()
        session, phases, _streams = _media_session(
            {manifest["track_sid"]: frames}, audio
        )
        session._authorize_announcement(manifest)
        session._remember_publication(_publication(manifest), WORKER)
        await _spin_until(lambda: session._active_playout is None)

        assert audio.sealed == []
        assert audio.interrupted == [manifest["announcement_id"]]
        if started:
            assert phases[-1] == (manifest["announcement_id"], "interrupted")
        else:
            assert phases == []
        assert (manifest["announcement_id"], "finished") not in phases

    asyncio.run(scenario())


def test_media_buffers_audio_before_manifest_but_drops_unmatched_or_wrong_worker(qapp):
    async def scenario() -> None:
        manifest = _manifest(duration_samples=50)
        audio = _PlayoutAudio()
        session, phases, _streams = _media_session(
            {manifest["track_sid"]: [_Frame(50)]}, audio
        )
        early = _publication(manifest)
        session._remember_publication(early, WORKER)
        assert early.subscriptions == []
        session._authorize_announcement(manifest)
        await _spin_until(lambda: session._active_playout is None)
        assert phases[-1][1] == "finished"

        wrong_manifest = _manifest(2, duration_samples=50)
        wrong_name = _publication(wrong_manifest, name="unexpected.track")
        session._authorize_announcement(wrong_manifest)
        session._remember_publication(wrong_name, WORKER)
        assert wrong_name.subscriptions == [False]

        unknown_manifest = _manifest(3, duration_samples=50)
        wrong_worker = _publication(unknown_manifest)
        session._remember_publication(wrong_worker, "voice-worker-b")
        assert wrong_worker.subscriptions == [False]
        session._authorize_announcement(unknown_manifest)
        session._expire_unmatched(unknown_manifest["track_sid"])
        assert len(audio.begun) == 1

    asyncio.run(scenario())


def test_media_serializes_ready_tracks_by_announcement_sequence(qapp):
    async def scenario() -> None:
        first = _manifest(1, duration_samples=50)
        second = _manifest(2, duration_samples=50)
        audio = _PlayoutAudio(auto_finish=False)
        session, phases, _streams = _media_session(
            {
                first["track_sid"]: [_Frame(50)],
                second["track_sid"]: [_Frame(50)],
            },
            audio,
        )
        session._authorize_announcement(first)
        session._authorize_announcement(second)
        session._remember_publication(_publication(second), WORKER)
        await asyncio.sleep(0)
        assert not audio.begun

        session._remember_publication(_publication(first), WORKER)
        await _spin_until(lambda: audio.sealed == [first["announcement_id"]])
        assert [item[0] for item in audio.begun] == [first["announcement_id"]]
        assert not audio.overlap

        audio.finish_current("finished")
        await _spin_until(lambda: len(audio.sealed) == 2)
        assert [item[0] for item in audio.begun] == [
            first["announcement_id"],
            second["announcement_id"],
        ]
        assert not audio.overlap
        audio.finish_current("finished")
        await _spin_until(lambda: session._active_playout is None)
        assert phases == [
            (first["announcement_id"], "started"),
            (first["announcement_id"], "finished"),
            (second["announcement_id"], "started"),
            (second["announcement_id"], "finished"),
        ]

    asyncio.run(scenario())


def test_media_interruption_clears_active_and_queued_audio_without_replay(qapp):
    async def scenario() -> None:
        first = _manifest(1, duration_samples=50)
        second = _manifest(2, duration_samples=50)
        audio = _PlayoutAudio(auto_finish=False)
        session, phases, _streams = _media_session(
            {
                first["track_sid"]: [_Frame(50)],
                second["track_sid"]: [_Frame(50)],
            },
            audio,
        )
        first_publication = _publication(first)
        second_publication = _publication(second)
        session._authorize_announcement(first)
        session._authorize_announcement(second)
        session._remember_publication(first_publication, WORKER)
        session._remember_publication(second_publication, WORKER)
        await _spin_until(lambda: audio.sealed == [first["announcement_id"]])
        await asyncio.sleep(0)

        active_task = session._active_playout.task
        session._interrupt_all_playout()
        await asyncio.gather(active_task, return_exceptions=True)

        assert [item[0] for item in audio.begun] == [first["announcement_id"]]
        assert audio.interrupted == [first["announcement_id"]]
        assert phases == [
            (first["announcement_id"], "started"),
            (first["announcement_id"], "interrupted"),
        ]
        assert session._active_playout is None
        assert not session._announcements
        assert first_publication.subscriptions[-1] is False
        assert second_publication.subscriptions[-1] is False

    asyncio.run(scenario())
