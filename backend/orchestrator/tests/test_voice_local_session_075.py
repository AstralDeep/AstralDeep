"""Backend-aware local session construction and lifecycle tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator.voice_backend import VoiceSpeechBackend
from orchestrator.voice_api import VoiceApiError
from orchestrator.voice_runtime import VoiceSessionRuntime, _session_projection
from orchestrator.voice_sessions import CreateSession


NOW = datetime(2026, 8, 28, 17, 0, tzinfo=UTC)
DEVICE = "00000000-0000-4000-8000-000000000201"
CONNECTION = "00000000-0000-4000-8000-000000000202"
CHAT = "00000000-0000-4000-8000-000000000203"
ACTIVATION = "00000000-0000-4000-8000-000000000204"
BINDING = "00000000-0000-4000-8000-000000000205"


def test_local_create_model_accepts_only_null_remote_media_fields() -> None:
    request = CreateSession(
        user_id="user-a",
        activation_id=ACTIVATION,
        device_id=DEVICE,
        device_kind="web",
        speech_backend="client_local",
        transport="client_local",
        room_name=None,
        participant_identity=None,
        visible_chat_id=CHAT,
        owner_connection_generation=CONNECTION,
        control_binding_id=BINDING,
        control_binding_expires_at=NOW + timedelta(minutes=5),
        lease_expires_at=NOW + timedelta(minutes=1),
        media_grant_nonce_hash=None,
        media_grant_issued_at=None,
        media_grant_expires_at=None,
    )

    assert request.speech_backend == request.transport == "client_local"
    assert request.room_name is None
    assert request.participant_identity is None
    assert request.media_grant_nonce_hash is None


def test_local_runtime_builds_no_remote_identity_or_grant() -> None:
    runtime = VoiceSessionRuntime(
        repository=SimpleNamespace(),
        capability=SimpleNamespace(),
        media=SimpleNamespace(),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )

    request = runtime._create_request(
        "user-a",
        {
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
            "binding_id": BINDING,
            "binding_expires_at": NOW + timedelta(minutes=5),
        },
        {
            "activation_id": ACTIVATION,
            "device_id": DEVICE,
            "device_kind": "web",
            "visible_chat_id": CHAT,
            "foreground_active": True,
            "capability": {
                "transport": "client_local",
                "has_microphone": True,
                "has_audio_output": True,
                "microphone_permission": "authorized",
                "recognition_permission": "authorized",
                "recognition_processing": "guaranteed_local",
                "recognition_locale": "ready",
                "recognition_installation": "ready",
                "synthesis_processing": "guaranteed_local",
                "synthesis_locale": "ready",
                "configured_locale": "en-US",
                "contract": "client_local/v1",
                "full_duplex": False,
            },
        },
        now=NOW,
    )

    assert request.speech_backend == "client_local"
    assert request.transport == "client_local"
    assert request.room_name is None
    assert request.participant_identity is None
    assert request.media_grant_nonce_hash is None


@pytest.mark.parametrize(
    "override",
    [
        {"transport": "livekit"},
        {"contract": "voice-rest/v1"},
        {"full_duplex": True},
        {"configured_locale": "en-GB"},
    ],
)
def test_local_runtime_rejects_non_exact_capability(override: dict[str, Any]) -> None:
    runtime = VoiceSessionRuntime(
        repository=SimpleNamespace(),
        capability=SimpleNamespace(),
        media=SimpleNamespace(),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    capability = {
        "transport": "client_local",
        "has_microphone": True,
        "has_audio_output": True,
        "microphone_permission": "authorized",
        "recognition_permission": "authorized",
        "recognition_processing": "guaranteed_local",
        "recognition_locale": "ready",
        "recognition_installation": "ready",
        "synthesis_processing": "guaranteed_local",
        "synthesis_locale": "ready",
        "configured_locale": "en-US",
        "contract": "client_local/v1",
        "full_duplex": False,
    }
    capability.update(override)

    with pytest.raises(Exception):
        runtime._create_request(
            "user-a",
            {
                "device_id": DEVICE,
                "connection_generation": CONNECTION,
                "binding_id": BINDING,
                "binding_expires_at": NOW + timedelta(minutes=5),
            },
            {
                "activation_id": ACTIVATION,
                "device_id": DEVICE,
                "device_kind": "web",
                "visible_chat_id": CHAT,
                "foreground_active": True,
                "capability": capability,
            },
            now=NOW,
        )


@pytest.mark.asyncio
async def test_local_activation_claims_and_applies_without_remote_media() -> None:
    active = SimpleNamespace(chat_context_synced=True)
    repository = SimpleNamespace(
        claim_control_lease=AsyncMock(
            return_value=SimpleNamespace(owner_id="replica-a")
        ),
        apply_chat_context=Mock(
            return_value=SimpleNamespace(
                session=SimpleNamespace(chat_context_synced=True)
            )
        ),
        mark_session_active=Mock(return_value=active),
    )
    runtime = VoiceSessionRuntime(
        repository=repository,
        capability=SimpleNamespace(),
        media=SimpleNamespace(),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    create = runtime._create_request(
        "user-a",
        {
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
            "binding_id": BINDING,
            "binding_expires_at": NOW + timedelta(minutes=5),
        },
        {
            "activation_id": ACTIVATION,
            "device_id": DEVICE,
            "device_kind": "web",
            "visible_chat_id": CHAT,
            "foreground_active": True,
            "capability": {
                "transport": "client_local",
                "has_microphone": True,
                "has_audio_output": True,
                "microphone_permission": "authorized",
                "recognition_permission": "authorized",
                "recognition_processing": "guaranteed_local",
                "recognition_locale": "ready",
                "recognition_installation": "ready",
                "synthesis_processing": "guaranteed_local",
                "synthesis_locale": "ready",
                "configured_locale": "en-US",
                "contract": "client_local/v1",
                "full_duplex": False,
            },
        },
        now=NOW,
    )
    session = SimpleNamespace(
        ended_at=None,
        speech_backend="client_local",
        user_id="user-a",
        session_id="00000000-0000-4000-8000-000000000206",
        generation=1,
        media_grant_revision=1,
        visible_chat_id=CHAT,
        chat_context_revision=1,
    )
    await runtime._require_ready()
    assert await runtime._activate_local(session, create) is active
    repository.claim_control_lease.assert_awaited_once()
    repository.apply_chat_context.assert_called_once()
    repository.mark_session_active.assert_called_once()

    with pytest.raises(VoiceApiError, match="activation_replay_ended"):
        await runtime._activate_local(
            SimpleNamespace(**{**session.__dict__, "ended_at": NOW}), create
        )
    with pytest.raises(VoiceApiError, match="backend_mismatch"):
        await runtime._activate_local(
            SimpleNamespace(**{**session.__dict__, "speech_backend": "llm_factory"}),
            create,
        )
    repository.mark_session_active.return_value = SimpleNamespace(
        chat_context_synced=False
    )
    with pytest.raises(RuntimeError, match="chat_context_not_applied"):
        await runtime._activate_local(session, create)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("apply failed"), asyncio.CancelledError()])
async def test_local_activation_failure_or_cancellation_aborts_exact_session(
    failure: BaseException,
) -> None:
    ended = SimpleNamespace(**{
        "session_id": "00000000-0000-4000-8000-000000000206",
        "generation": 1,
    })
    repository = SimpleNamespace(
        claim_control_lease=AsyncMock(
            return_value=SimpleNamespace(owner_id="replica-a")
        ),
        apply_chat_context=Mock(side_effect=failure),
        end_session=Mock(return_value=ended),
    )
    media = SimpleNamespace(abort=AsyncMock())
    runtime = VoiceSessionRuntime(
        repository=repository,
        capability=SimpleNamespace(),
        media=media,
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    ended_handler = AsyncMock()
    runtime.bind_session_end_handler(ended_handler)
    create = runtime._create_request(
        "user-a",
        {
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
            "binding_id": BINDING,
            "binding_expires_at": NOW + timedelta(minutes=5),
        },
        {
            "activation_id": ACTIVATION,
            "device_id": DEVICE,
            "device_kind": "web",
            "visible_chat_id": CHAT,
            "foreground_active": True,
            "capability": {
                "transport": "client_local",
                "has_microphone": True,
                "has_audio_output": True,
                "microphone_permission": "authorized",
                "recognition_permission": "authorized",
                "recognition_processing": "guaranteed_local",
                "recognition_locale": "ready",
                "recognition_installation": "ready",
                "synthesis_processing": "guaranteed_local",
                "synthesis_locale": "ready",
                "configured_locale": "en-US",
                "contract": "client_local/v1",
                "full_duplex": False,
            },
        },
        now=NOW,
    )
    session = SimpleNamespace(
        ended_at=None,
        speech_backend="client_local",
        user_id="user-a",
        session_id=ended.session_id,
        generation=1,
        media_grant_revision=1,
        visible_chat_id=CHAT,
        chat_context_revision=1,
    )
    with pytest.raises(type(failure)):
        await runtime._activate_local(session, create)
    media.abort.assert_awaited_once_with(session)
    repository.end_session.assert_called_once()
    ended_handler.assert_awaited_once_with(ended, "media_error")


def test_local_cleanup_binding_and_capability_shape_fail_closed() -> None:
    runtime = VoiceSessionRuntime(
        repository=SimpleNamespace(),
        capability=SimpleNamespace(),
        media=SimpleNamespace(),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    with pytest.raises(TypeError, match="local buffer cleanup handler"):
        runtime.bind_local_buffer_cleanup_handler(None)  # type: ignore[arg-type]
    runtime.bind_local_buffer_cleanup_handler(AsyncMock())
    with pytest.raises(RuntimeError, match="already_bound"):
        runtime.bind_local_buffer_cleanup_handler(AsyncMock())

    with pytest.raises(VoiceApiError, match="invalid_request"):
        runtime._create_request(
            "user-a",
            {},
            {"foreground_active": True, "capability": "not-a-mapping"},
            now=NOW,
        )


def test_session_projection_adds_backend_only_to_versioned_local_lane() -> None:
    values = {
        "session_id": "00000000-0000-4000-8000-000000000206",
        "device_id": DEVICE,
        "device_kind": "web",
        "transport": "livekit",
        "state": "active",
        "generation": 1,
        "media_grant_revision": 1,
        "owner_connection_generation": CONNECTION,
        "visible_chat_id": CHAT,
        "applied_visible_chat_id": CHAT,
        "chat_context_revision": 1,
        "applied_chat_context_revision": 1,
        "chat_context_synced": True,
        "foreground_active": True,
        "foreground_reason": "foreground",
        "updated_at": NOW,
        "speech_muted": False,
        "microphone_enabled": True,
        "lease_expires_at": NOW + timedelta(minutes=1),
        "started_at": NOW,
        "idle_expires_at": None,
    }
    remote = _session_projection(
        SimpleNamespace(**values, speech_backend="llm_factory")
    )
    local = _session_projection(
        SimpleNamespace(
            **{**values, "transport": "client_local"},
            speech_backend="client_local",
        )
    )
    assert "speech_backend" not in remote
    assert local["speech_backend"] == "client_local"
