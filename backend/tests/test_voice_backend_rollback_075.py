"""PostgreSQL restart/rollback evidence for Feature 075 speech backends."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

import pytest

from orchestrator.voice_backend import (
    SpeechBackendSelection,
    VoiceSpeechBackend,
)
from orchestrator.voice_runtime import ActivatedVoiceMedia, VoiceSessionRuntime
from tests.helpers.voice_plane_runtime import (
    VoicePlaneTestRuntime,
    history_manager,
    isolated_voice_plane_runtime,
    voice_session_repository,
)


USER_ID = "rollback-user-075"
CHAT_ID = "75000000-0000-4000-8000-000000000001"
DEVICE_ID = "75000000-0000-4000-8000-000000000002"
ACTIVATION_IDS = (
    "75000000-0000-4000-8000-000000000011",
    "75000000-0000-4000-8000-000000000012",
    "75000000-0000-4000-8000-000000000013",
)
CONNECTION_GENERATIONS = (
    "75000000-0000-4000-8000-000000000021",
    "75000000-0000-4000-8000-000000000022",
    "75000000-0000-4000-8000-000000000023",
)
CONTROL_BINDING_IDS = (
    "75000000-0000-4000-8000-000000000031",
    "75000000-0000-4000-8000-000000000032",
    "75000000-0000-4000-8000-000000000033",
)
SESSION_IDS = (
    "75000000-0000-4000-8000-000000000041",
    "75000000-0000-4000-8000-000000000042",
    "75000000-0000-4000-8000-000000000043",
)
REMOTE_ASSIGNMENT_ID = "75000000-0000-4000-8000-000000000051"
REMOTE_GRANT_ID = "75000000-0000-4000-8000-000000000052"
STARTED_AT = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
BACKENDS = (
    VoiceSpeechBackend.CLIENT_LOCAL,
    VoiceSpeechBackend.LLM_FACTORY,
    VoiceSpeechBackend.CLIENT_LOCAL,
)


class _ReadySnapshot:
    def __init__(self, backend: VoiceSpeechBackend) -> None:
        if backend is VoiceSpeechBackend.CLIENT_LOCAL:
            self.status = "requires_client_readiness"
            self.reason = "client_readiness_required"
        else:
            self.status = "ready"
            self.reason = "ready"

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": "1",
            "status": self.status,
            "reason": self.reason,
        }


class _ReadyCapability:
    def __init__(self, backend: VoiceSpeechBackend) -> None:
        self._backend = backend

    async def readiness(self) -> _ReadySnapshot:
        return _ReadySnapshot(self._backend)


class _LocalOnlyMedia:
    """No-network lifecycle adapter that rejects accidental remote activation."""

    async def activate(self, _session: Any) -> ActivatedVoiceMedia:
        raise AssertionError("client-local reconstruction allocated remote media")

    async def assignment_is_current(
        self,
        _session: Any,
        *,
        assignment_id: str,
        worker_identity: str,
    ) -> bool:
        del assignment_id, worker_identity
        raise AssertionError("client-local reconstruction inspected remote media")

    async def apply_context(self, _session: Any) -> None:
        return None

    async def set_capture(self, _session: Any, _enabled: bool) -> None:
        return None

    async def barge_in(self, _session: Any) -> None:
        return None

    async def stop_speech(self, _session: Any) -> None:
        return None

    async def end(self, _session: Any, _reason: str) -> None:
        return None

    async def abort(self, _session: Any) -> None:
        return None

    async def rotate_media_grant(
        self,
        _previous: Any,
        _session: Any,
        *,
        refresh_id: str,
    ) -> Mapping[str, Any]:
        del refresh_id
        raise AssertionError("client-local reconstruction rotated remote media")


class _DeterministicRemoteMedia(_LocalOnlyMedia):
    """In-memory Factory media boundary with no network or credentials."""

    def __init__(self, now: datetime) -> None:
        self._now = now
        self._current: tuple[str, int, str, str] | None = None
        self.ended: list[tuple[str, str]] = []

    async def activate(self, session: Any) -> ActivatedVoiceMedia:
        worker_identity = "rollback-worker-075"
        self._current = (
            session.session_id,
            session.generation,
            REMOTE_ASSIGNMENT_ID,
            worker_identity,
        )
        return ActivatedVoiceMedia(
            assignment_id=REMOTE_ASSIGNMENT_ID,
            worker_identity=worker_identity,
            worker_grant_issued_at=self._now,
            worker_grant_expires_at=self._now + timedelta(minutes=5),
            client_grant={
                "grant_id": REMOTE_GRANT_ID,
                "transport": "livekit",
            },
        )

    async def assignment_is_current(
        self,
        session: Any,
        *,
        assignment_id: str,
        worker_identity: str,
    ) -> bool:
        return self._current == (
            session.session_id,
            session.generation,
            assignment_id,
            worker_identity,
        )

    async def end(self, session: Any, reason: str) -> None:
        self.ended.append((session.session_id, reason))
        self._current = None

    async def abort(self, session: Any) -> None:
        await self.end(session, "media_error")


def _control(index: int, now: datetime) -> dict[str, Any]:
    return {
        "device_id": DEVICE_ID,
        "connection_generation": CONNECTION_GENERATIONS[index],
        "binding_id": CONTROL_BINDING_IDS[index],
        "binding_expires_at": now + timedelta(minutes=10),
    }


def _activation(index: int, backend: VoiceSpeechBackend) -> dict[str, Any]:
    if backend is VoiceSpeechBackend.CLIENT_LOCAL:
        capability = {
            "contract": "client_local/v1",
            "transport": "client_local",
            "configured_locale": "en-US",
            "full_duplex": False,
            "has_microphone": True,
            "has_audio_output": True,
            "microphone_permission": "authorized",
            "recognition_permission": "authorized",
            "recognition_processing": "guaranteed_local",
            "recognition_locale": "ready",
            "recognition_installation": "ready",
            "synthesis_processing": "guaranteed_local",
            "synthesis_locale": "ready",
        }
    else:
        capability = {
            "transport": "livekit",
            "full_duplex": True,
            "has_microphone": True,
            "has_audio_output": True,
            "microphone_permission": "authorized",
        }
    return {
        "activation_id": ACTIVATION_IDS[index],
        "device_id": DEVICE_ID,
        "device_kind": "web",
        "visible_chat_id": CHAT_ID,
        "foreground_active": True,
        "capability": capability,
    }


def _reconstruct_runtime(
    database: VoicePlaneTestRuntime,
    index: int,
) -> tuple[VoiceSessionRuntime, Any, _LocalOnlyMedia]:
    backend = BACKENDS[index]
    selection = SpeechBackendSelection.from_environ(
        {"VOICE_SPEECH_BACKEND": backend.value}
    )
    assert selection.value is backend
    now = STARTED_AT + timedelta(minutes=index)
    repository = voice_session_repository(
        database,
        uuid_factory=lambda: uuid.UUID(SESSION_IDS[index]),
    )
    media: _LocalOnlyMedia
    if backend is VoiceSpeechBackend.LLM_FACTORY:
        media = _DeterministicRemoteMedia(now)
    else:
        media = _LocalOnlyMedia()
    runtime = VoiceSessionRuntime(
        repository=repository,
        capability=_ReadyCapability(backend),
        media=media,
        replica_id=f"rollback-replica-{index}",
        clock=lambda: now,
        speech_backend=backend,
        backend_selection=selection,
    )
    return runtime, repository, media


def _schema_identity(database: VoicePlaneTestRuntime) -> dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in database.fetch_all(
            "SELECT key, value FROM schema_meta "
            "WHERE key IN ('revision', 'astralplane_migration_digest') "
            "ORDER BY key"
        )
    }


def _conversation_snapshot(
    database: VoicePlaneTestRuntime,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    conversation = database.fetch_one(
        "SELECT * FROM chats WHERE id = ? AND user_id = ?",
        (CHAT_ID, USER_ID),
    )
    assert conversation is not None
    messages = database.fetch_all(
        "SELECT * FROM messages WHERE chat_id = ? AND user_id = ? ORDER BY id",
        (CHAT_ID, USER_ID),
    )
    return dict(conversation), tuple(dict(row) for row in messages)


def _session_rows(
    database: VoicePlaneTestRuntime,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(row)
        for row in database.fetch_all(
            "SELECT * FROM voice_session WHERE user_id = ? "
            "AND visible_chat_id = ? ORDER BY started_at, session_id",
            (USER_ID, CHAT_ID),
        )
    )


async def _run_phase(
    database: VoicePlaneTestRuntime,
    index: int,
) -> tuple[Any, _LocalOnlyMedia]:
    backend = BACKENDS[index]
    now = STARTED_AT + timedelta(minutes=index)
    runtime, repository, media = _reconstruct_runtime(database, index)
    control = _control(index, now)
    result = await runtime.create_session(
        user_id=USER_ID,
        control=control,
        request=_activation(index, backend),
    )
    assert result.status_code == 201
    assert result.payload["session"]["session_id"] == SESSION_IDS[index]
    if backend is VoiceSpeechBackend.LLM_FACTORY:
        assert "speech_backend" not in result.payload["session"]
        assert result.payload["grant"] == {
            "grant_id": REMOTE_GRANT_ID,
            "transport": "livekit",
        }
    else:
        assert result.payload["session"]["speech_backend"] == backend.value
        assert "grant" not in result.payload

    active = await asyncio.to_thread(
        repository.get_session,
        user_id=USER_ID,
        session_id=SESSION_IDS[index],
    )
    assert active.state == "active"
    assert active.speech_backend == backend.value
    await runtime.end_session(
        user_id=USER_ID,
        session_id=active.session_id,
        control=control,
        request={
            "expected_generation": active.generation,
            "expected_media_grant_revision": active.media_grant_revision,
        },
    )
    ended = await asyncio.to_thread(
        repository.get_session,
        user_id=USER_ID,
        session_id=active.session_id,
    )
    assert ended.state == "ended"
    assert ended.end_reason == "user"
    assert ended.speech_backend == backend.value
    return ended, media


@pytest.mark.asyncio
async def test_backend_restart_rollback_preserves_conversation_and_history() -> None:
    with isolated_voice_plane_runtime("voice_backend_rollback_075") as database:
        history = history_manager(database)
        assert history.create_chat(chat_id=CHAT_ID, user_id=USER_ID) == CHAT_ID
        history.add_message(CHAT_ID, "user", "Keep this request", USER_ID)
        history.add_message(CHAT_ID, "assistant", "Durable answer", USER_ID)

        conversation_before = _conversation_snapshot(database)
        assert [row["role"] for row in conversation_before[1]] == [
            "user",
            "assistant",
        ]
        schema_before = _schema_identity(database)
        assert schema_before["revision"] == "079.001"
        assert re.fullmatch(
            r"[0-9a-f]{64}",
            schema_before["astralplane_migration_digest"],
        )

        historical_rows: tuple[dict[str, Any], ...] = ()
        remote_media: _DeterministicRemoteMedia | None = None
        for index, backend in enumerate(BACKENDS):
            ended, media = await _run_phase(database, index)
            if backend is VoiceSpeechBackend.LLM_FACTORY:
                assert isinstance(media, _DeterministicRemoteMedia)
                remote_media = media

            rows = _session_rows(database)
            assert rows[: len(historical_rows)] == historical_rows
            assert [row["session_id"] for row in rows] == list(SESSION_IDS[: index + 1])
            assert [row["activation_id"] for row in rows] == list(
                ACTIVATION_IDS[: index + 1]
            )
            assert [row["speech_backend"] for row in rows] == [
                value.value for value in BACKENDS[: index + 1]
            ]
            assert all(
                row["state"] == "ended"
                and row["end_reason"] == "user"
                and row["ended_at"] is not None
                for row in rows
            )
            assert rows[-1]["session_id"] == ended.session_id
            historical_rows = rows

            assert _conversation_snapshot(database) == conversation_before
            assert _schema_identity(database) == schema_before

        assert len(historical_rows) == 3
        assert len({row["session_id"] for row in historical_rows}) == 3
        assert historical_rows[0]["room_name"] is None
        assert historical_rows[0]["participant_identity"] is None
        assert historical_rows[1]["room_name"] is not None
        assert historical_rows[1]["participant_identity"] is not None
        assert historical_rows[2]["room_name"] is None
        assert historical_rows[2]["participant_identity"] is None
        assert remote_media is not None
        assert remote_media.ended == [(SESSION_IDS[1], "user")]
