"""Fail-closed voice service bootstrap tests for Feature 065."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from astralplane import create_repository_catalog
from fastapi import APIRouter, FastAPI

from orchestrator.voice_bootstrap import (
    VoiceBootstrapError,
    VoiceServices,
    _MAX_LOCAL_IDENTITY_END_ATTEMPTS,
    _sensitive_result_quanta,
    build_voice_services,
    install_voice_worker_control,
)
from orchestrator.voice_backend import VoiceSpeechBackend
from orchestrator.voice_control_binding import (
    ClientLocalBindingRegistry,
    VoiceControlBindingError,
    VoiceControlClaims,
)
from shared.protocol import VoiceLocalReady, VoiceLocalRecognitionStarted
from orchestrator.voice_api import VoiceApiError
from orchestrator.voice_coordinator import (
    APPROVED_PHRASE_KEYS,
    CADENCE_TARGET_SECONDS,
    AnnouncementFence,
    AnnouncementState,
    AnnouncementStateAdapter,
    CoordinatorClock,
    PhraseBook,
    StaleFence,
    WorkerRegistrationReceipt,
)
from orchestrator.voice_media import ClientPlayoutObservation
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.voice_sessions import (
    ChatUnavailableMutation,
    VoiceSessionRepositoryError,
    VoiceTurnRecord,
)
from orchestrator.voice_worker_endpoint import (
    WORKER_CONTROL_PATH,
    WorkerControlConfigError,
    WorkerControlSettings,
)


class _PlaneRuntime:
    def __init__(self) -> None:
        self.repositories = create_repository_catalog()

    def transaction(self):  # pragma: no cover - construction never opens DB.
        raise AssertionError("unexpected database access")


class _EnabledLocalCapability:
    def feature_enabled(self) -> bool:
        return True


def _voice_turn(
    *,
    turn_id: str = "00000000-0000-4000-8000-000000000032",
    client_turn_id: str = "00000000-0000-4000-8000-000000000042",
    submission_id: str = "00000000-0000-4000-8000-000000000052",
    request_generation: str = "00000000-0000-4000-8000-000000000062",
    session_id: str = "00000000-0000-4000-8000-000000000031",
    state: str = "processing",
    media_grant_revision: int = 2,
    rejection_reason: str | None = None,
    announcement_sequence: int = 0,
    result_reserved_samples: int = 0,
    result_quantum_count: int = 0,
    sensitivity: str = "unknown",
    result_commit_id: str | None = None,
) -> VoiceTurnRecord:
    now = datetime.now(UTC)
    terminal = state in {"succeeded", "failed", "refused", "cancelled"}
    abandoned = state == "abandoned"
    return VoiceTurnRecord(
        turn_id=turn_id,
        client_turn_id=client_turn_id,
        session_id=session_id,
        session_generation=1,
        media_grant_revision=media_grant_revision,
        user_id="user-a",
        chat_id="00000000-0000-4000-8000-000000000072",
        chat_context_revision=1,
        execution_base_render_revision=0,
        submission_id=submission_id,
        request_generation=request_generation,
        message_id=None if abandoned else 1,
        acceptance_commit_id=None,
        result_commit_id=result_commit_id,
        operation_id=None,
        state=state,
        is_foreground=not (terminal or abandoned),
        detected_language="en",
        spoken_output_policy="full_recap",
        output_reason="supported",
        terminal_kind=state if terminal or abandoned else None,
        rejection_reason=rejection_reason,
        rejection_retry_policy=("none" if abandoned else None),
        recap_source="none",
        sensitivity=sensitivity,
        announcement_sequence=announcement_sequence,
        result_reserved_samples=result_reserved_samples,
        result_quantum_count=result_quantum_count,
        last_phrase_key=None,
        next_announcement_due_at=None,
        accepted_at=None if abandoned else now,
        processing_started_at=None if abandoned else now,
        terminal_at=now if terminal or abandoned else None,
        created_at=now,
        updated_at=now,
    )


def _local_recognition_context(
    now: datetime,
    *,
    client_turn_id: str = "00000000-0000-4000-8000-000000000042",
    recognition_sequence: int = 1,
) -> tuple[Any, VoiceControlClaims, VoiceLocalRecognitionStarted]:
    session = SimpleNamespace(
        session_id="00000000-0000-4000-8000-000000000031",
        user_id="user-a",
        device_id="00000000-0000-4000-8000-000000000021",
        owner_connection_generation="00000000-0000-4000-8000-000000000022",
        control_binding_id="00000000-0000-4000-8000-000000000023",
        control_binding_expires_at=now + timedelta(minutes=4),
        lease_expires_at=now + timedelta(minutes=2),
        control_owner_id="voice-coordinator-local-1",
        control_lease_expires_at=now + timedelta(seconds=30),
        generation=1,
        media_grant_revision=1,
        speech_backend="client_local",
        state="active",
        ended_at=None,
        foreground_active=True,
        microphone_enabled=True,
        speech_muted=False,
        visible_chat_id="00000000-0000-4000-8000-000000000072",
        chat_context_revision=1,
        applied_visible_chat_id="00000000-0000-4000-8000-000000000072",
        applied_chat_context_revision=1,
    )
    claims = VoiceControlClaims(
        subject="user-a",
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        binding_id=session.control_binding_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=4),
    )
    frame = VoiceLocalRecognitionStarted(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=1,
        speech_revision=1,
        client_turn_id=client_turn_id,
        chat_id=session.visible_chat_id,
        chat_context_revision=1,
        recognition_sequence=recognition_sequence,
    )
    return session, claims, frame


def test_remote_bootstrap_keeps_legacy_backend_when_selector_is_missing() -> None:
    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime().repositories,
        environ={
            "ASTRAL_ENV": "development",
            "VOICE_WORKER_CLOSURE_SHA256": "0" * 64,
            "LIVEKIT_INTERNAL_URL": "http://livekit:7880",
            "LIVEKIT_PUBLIC_URL": "ws://localhost:7880",
            "LIVEKIT_API_KEY": "development-key",
            "LIVEKIT_API_SECRET": "development-secret-with-32-bytes-minimum",
            "VOICE_CONTROL_SECRET": "development-worker-secret-with-32-bytes",
        },
    )

    assert services.speech_backend.value == "llm_factory"
    assert services.runtime is not None
    assert services.worker_pool is not None


@pytest.mark.asyncio
async def test_client_local_services_cover_the_content_free_lifecycle() -> None:
    session = SimpleNamespace(
        session_id="00000000-0000-4000-8000-000000000031",
        user_id="user-a",
        device_id="00000000-0000-4000-8000-000000000021",
        owner_connection_generation="00000000-0000-4000-8000-000000000022",
        generation=1,
        media_grant_revision=2,
        visible_chat_id="00000000-0000-4000-8000-000000000072",
        chat_context_revision=1,
        applied_chat_context_revision=1,
        foreground_active=True,
        microphone_enabled=True,
        speech_muted=False,
        state="active",
        speech_backend="client_local",
        ended_at=None,
        control_owner_id="voice-coordinator-local-1",
        control_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        control_binding_id="00000000-0000-4000-8000-000000000023",
        control_binding_expires_at=datetime.now(UTC) + timedelta(minutes=4),
    )
    turn = _voice_turn()
    authority = SimpleNamespace(
        user_id="user-a",
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        binding_id="00000000-0000-4000-8000-000000000023",
        session_id=session.session_id,
        generation=1,
        speech_revision=2,
        client_turn_id=turn.client_turn_id,
        turn_id=turn.turn_id,
        submission_id=turn.submission_id,
        request_generation=turn.request_generation,
        chat_id=turn.chat_id,
        chat_context_revision=1,
        recognition_sequence=1,
    )
    admission = SimpleNamespace(canonical_text="hello", turn=turn, replayed=False)
    repository = SimpleNamespace(
        get_controlled_session=Mock(return_value=session),
        bind_recognition_turn=Mock(return_value=SimpleNamespace(turn=turn)),
        admit_local_transcript=Mock(return_value=admission),
        get_session=Mock(return_value=session),
        get_turn=Mock(return_value=turn),
        abandon_preacceptance_turns=Mock(),
    )
    bindings = SimpleNamespace(
        authorize_ready=Mock(),
        reserve_turn=Mock(return_value=authority),
        finalize_turn=Mock(return_value=authority),
        release_reservation=Mock(),
        verify_final=Mock(return_value=("hello", False)),
        get_turn=Mock(return_value=authority),
        clear_connection=Mock(),
        clear_session=Mock(),
    )
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=repository,
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=bindings,
    )
    claims = VoiceControlClaims(
        subject="user-a",
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        binding_id="00000000-0000-4000-8000-000000000023",
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=4),
    )
    ready = SimpleNamespace(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=1,
        speech_revision=2,
        client_sequence=1,
    )
    assert await services.local_ready(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=ready,
        now=datetime.now(UTC),
    ) is session

    started = SimpleNamespace(
        session_id=session.session_id,
        generation=1,
        speech_revision=2,
        client_turn_id=turn.client_turn_id,
        chat_id=turn.chat_id,
        chat_context_revision=1,
    )
    assert (await services.bind_local_recognition(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=started,
        execution_base_render_revision=4,
        now=datetime.now(UTC),
    )) == (turn, authority)

    final = SimpleNamespace(client_turn_id=turn.client_turn_id)
    assert await services.admit_local_final(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        frame=final,
        now=datetime.now(UTC),
    ) is admission

    services.clear_local_connection(claims)
    services.clear_local_session(session)
    await services.cleanup_local_buffers(session)
    assert bindings.clear_connection.called
    assert bindings.clear_session.call_count == 2
    assert await services.local_ready(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=ready,
        now=datetime.now(UTC),
    ) is session
    await services.complete_local_ready_delivery(
        session,
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=ready,
        now=datetime.now(UTC),
        authority_is_current=lambda: True,
    )

    publisher = AsyncMock()
    services.bind_local_announcement_publisher(publisher)
    await services._publish_local_announcement(
        turn,
        kind="failure",
    )
    publisher.assert_awaited_once()
    assert services.voice_status()["speech_backend"] == "client_local"
    session.control_owner_id = "replica-b"
    with pytest.raises(VoiceBootstrapError, match="local_control_unavailable"):
        await services._publish_local_announcement(turn, kind="failure")
    publisher.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_announcement_delivery_failure_discards_ephemeral_authority() -> None:
    now = datetime.now(UTC)
    session = SimpleNamespace(
        session_id="00000000-0000-4000-8000-000000000031",
        device_id="00000000-0000-4000-8000-000000000021",
        owner_connection_generation="00000000-0000-4000-8000-000000000022",
        generation=1,
        media_grant_revision=2,
        foreground_active=True,
        speech_muted=False,
        state="active",
        speech_backend="client_local",
        ended_at=None,
        control_owner_id="voice-coordinator-local-1",
        control_lease_expires_at=now + timedelta(minutes=1),
        lease_expires_at=now + timedelta(minutes=2),
    )
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=SimpleNamespace(get_session=Mock(return_value=session)),
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
    )
    turn = _voice_turn(session_id=session.session_id)

    with pytest.raises(TypeError, match="publisher must be callable"):
        services.bind_local_announcement_publisher(None)  # type: ignore[arg-type]
    with pytest.raises(
        VoiceBootstrapError, match="local_announcement_publisher_unavailable"
    ):
        await services._publish_local_announcement(turn, kind="failure")
    assert services.local_announcements._announcements == {}

    publisher = AsyncMock(side_effect=RuntimeError("socket unavailable"))
    services.bind_local_announcement_publisher(publisher)
    with pytest.raises(RuntimeError, match="socket unavailable"):
        await services._publish_local_announcement(turn, kind="failure")
    assert services.local_announcements._announcements == {}
    with pytest.raises(RuntimeError, match="already_bound"):
        services.bind_local_announcement_publisher(publisher)


@pytest.mark.asyncio
async def test_local_rejection_cleans_preacceptance_but_preserves_accepted_replay() -> None:
    authority = SimpleNamespace(
        turn_id="00000000-0000-4000-8000-000000000032",
        session_id="00000000-0000-4000-8000-000000000031",
        generation=1,
    )
    bindings = SimpleNamespace(
        get_turn=Mock(return_value=authority),
        release_turn=Mock(),
    )
    repository = SimpleNamespace(reject_transcript=Mock())
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=repository,
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=bindings,
    )
    client_turn_id = "00000000-0000-4000-8000-000000000042"
    now = datetime.now(UTC)

    await services.reject_local_turn(
        user_id="user-a",
        client_turn_id=client_turn_id,
        reason="untrusted_detail",
        now=now,
    )
    repository.reject_transcript.assert_called_once_with(
        user_id="user-a",
        turn_id=authority.turn_id,
        reason="invalid_binding",
        retry_policy="explicit_user_retry",
        now=now,
    )
    bindings.release_turn.assert_called_once_with(
        user_id="user-a",
        client_turn_id=client_turn_id,
    )

    repository.reject_transcript.side_effect = VoiceSessionRepositoryError(
        "voice_turn_already_accepted"
    )
    bindings.release_turn.reset_mock()
    await services.reject_local_turn(
        user_id="user-a",
        client_turn_id=client_turn_id,
        reason="malformed_final",
        now=now,
    )
    bindings.release_turn.assert_called_once_with(
        user_id="user-a",
        client_turn_id=client_turn_id,
    )

    bindings.get_turn.side_effect = VoiceControlBindingError("invalid_binding")
    bindings.release_turn.reset_mock()
    await services.reject_local_turn(
        user_id="user-a",
        client_turn_id=client_turn_id,
        reason="stale_session",
        now=now,
    )
    bindings.release_turn.assert_not_called()


@pytest.mark.asyncio
async def test_transient_local_rejection_retains_and_drains_content_free_authority() -> None:
    client_turn_id = "00000000-0000-4000-8000-000000000042"
    authority = SimpleNamespace(
        user_id="user-a",
        client_turn_id=client_turn_id,
        turn_id="00000000-0000-4000-8000-000000000032",
        session_id="00000000-0000-4000-8000-000000000031",
        generation=1,
    )
    bindings = SimpleNamespace(
        get_turn=Mock(return_value=authority),
        release_turn=Mock(),
    )
    repository = SimpleNamespace(
        reject_transcript=Mock(
            side_effect=[RuntimeError("database unavailable"), None]
        )
    )
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=repository,
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=bindings,
    )

    await services.reject_local_turn(
        user_id="user-a",
        client_turn_id=client_turn_id,
        reason="invalid_binding",
        now=datetime.now(UTC),
    )

    assert len(services.pending_local_rejections) == 1
    retained = next(iter(services.pending_local_rejections.values()))
    assert not hasattr(retained, "text")
    assert not hasattr(retained, "text_digest_sha256")
    bindings.release_turn.assert_not_called()

    await services._drain_pending_local_rejections(now=datetime.now(UTC))

    assert services.pending_local_rejections == {}
    bindings.release_turn.assert_called_once_with(
        user_id="user-a",
        client_turn_id=client_turn_id,
    )


@pytest.mark.asyncio
async def test_pending_local_rejection_capacity_fails_closed_without_release() -> None:
    authority = SimpleNamespace(
        user_id="user-a",
        client_turn_id="new-turn",
        turn_id="00000000-0000-4000-8000-000000000032",
        session_id="00000000-0000-4000-8000-000000000031",
        generation=1,
    )
    bindings = SimpleNamespace(
        get_turn=Mock(return_value=authority),
        release_turn=Mock(),
    )
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=SimpleNamespace(
            reject_transcript=Mock(side_effect=RuntimeError("database unavailable"))
        ),
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=bindings,
    )
    services.pending_local_rejections.update(
        {
            ("user-a", f"turn-{index}"): SimpleNamespace(
                user_id="user-a",
                client_turn_id=f"turn-{index}",
                turn_id=f"00000000-0000-4000-8000-{index:012d}",
                session_id="00000000-0000-4000-8000-000000000031",
                generation=1,
                reason="invalid_binding",
                retry_policy="explicit_user_retry",
                reservation=None,
            )
            for index in range(256)
        }
    )

    with pytest.raises(VoiceBootstrapError, match="local_cleanup_capacity_exhausted"):
        await services.reject_local_turn(
            user_id="user-a",
            client_turn_id="new-turn",
            reason="invalid_binding",
            now=datetime.now(UTC),
        )
    bindings.release_turn.assert_not_called()


@pytest.mark.asyncio
async def test_repeatedly_cancelled_rejection_reconciles_before_propagating() -> None:
    client_turn_id = "00000000-0000-4000-8000-000000000042"
    authority = SimpleNamespace(
        user_id="user-a",
        client_turn_id=client_turn_id,
        turn_id="00000000-0000-4000-8000-000000000032",
        session_id="00000000-0000-4000-8000-000000000031",
        generation=1,
    )
    entered = threading.Event()
    release = threading.Event()

    def reject_transcript(**_kwargs: Any) -> None:
        entered.set()
        assert release.wait(timeout=2)

    bindings = SimpleNamespace(
        get_turn=Mock(return_value=authority),
        release_turn=Mock(),
    )
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=SimpleNamespace(reject_transcript=reject_transcript),
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=bindings,
    )
    task = asyncio.create_task(
        services.reject_local_turn(
            user_id="user-a",
            client_turn_id=client_turn_id,
            reason="invalid_binding",
            now=datetime.now(UTC),
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert len(services.pending_local_rejections) == 1

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert services.pending_local_rejections == {}
    bindings.release_turn.assert_called_once()


@pytest.mark.asyncio
async def test_session_cleanup_retains_then_clears_pending_rejection_handle() -> None:
    session = SimpleNamespace(
        user_id="user-a",
        session_id="00000000-0000-4000-8000-000000000031",
        generation=1,
    )
    authority = SimpleNamespace(
        user_id="user-a",
        client_turn_id="00000000-0000-4000-8000-000000000042",
        turn_id="00000000-0000-4000-8000-000000000032",
        session_id=session.session_id,
        generation=session.generation,
    )
    repository = SimpleNamespace(
        reject_transcript=Mock(side_effect=RuntimeError("database unavailable")),
        abandon_preacceptance_turns=Mock(
            side_effect=[RuntimeError("database unavailable"), None]
        ),
    )
    bindings = SimpleNamespace(
        get_turn=Mock(return_value=authority),
        release_turn=Mock(),
        clear_session=Mock(),
    )
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=repository,
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=bindings,
    )
    await services.reject_local_turn(
        user_id="user-a",
        client_turn_id=authority.client_turn_id,
        reason="invalid_binding",
        now=datetime.now(UTC),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await services.cleanup_local_buffers(session)
    assert len(services.pending_local_rejections) == 1

    await services.cleanup_local_buffers(session)
    assert services.pending_local_rejections == {}


@pytest.mark.asyncio
async def test_local_recognition_authority_precedes_every_durable_insert() -> None:
    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session = SimpleNamespace(
        session_id="00000000-0000-4000-8000-000000000031",
        user_id="user-a",
        device_id="00000000-0000-4000-8000-000000000021",
        owner_connection_generation="00000000-0000-4000-8000-000000000022",
        control_binding_id="00000000-0000-4000-8000-000000000023",
        control_binding_expires_at=now + timedelta(minutes=4),
        lease_expires_at=now + timedelta(minutes=2),
        control_owner_id="voice-coordinator-local-1",
        control_lease_expires_at=now + timedelta(seconds=30),
        generation=1,
        media_grant_revision=1,
        speech_backend="client_local",
        state="active",
        ended_at=None,
        foreground_active=True,
        microphone_enabled=True,
        speech_muted=False,
        visible_chat_id="00000000-0000-4000-8000-000000000072",
        chat_context_revision=1,
        applied_visible_chat_id="00000000-0000-4000-8000-000000000072",
        applied_chat_context_revision=1,
    )
    claims = VoiceControlClaims(
        subject="user-a",
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        binding_id=session.control_binding_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=4),
    )
    first = VoiceLocalRecognitionStarted(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=1,
        speech_revision=1,
        client_turn_id="00000000-0000-4000-8000-000000000041",
        chat_id=session.visible_chat_id,
        chat_context_revision=1,
        recognition_sequence=1,
    )
    second = VoiceLocalRecognitionStarted(
        **{
            **first.__dict__,
            "client_turn_id": "00000000-0000-4000-8000-000000000042",
            "recognition_sequence": 2,
        }
    )
    turn = _voice_turn(client_turn_id=first.client_turn_id)
    registry = ClientLocalBindingRegistry(capacity=1)
    registry.bind_turn(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        session=session,
        frame=first,
        turn=turn,
        now=now,
    )
    repository = SimpleNamespace(
        get_controlled_session=Mock(return_value=session),
        bind_recognition_turn=Mock(
            return_value=SimpleNamespace(turn=_voice_turn(client_turn_id=second.client_turn_id))
        ),
    )
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=repository,
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )

    with pytest.raises(VoiceControlBindingError, match="capacity_exhausted"):
        await services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=second,
            execution_base_render_revision=0,
            now=now,
        )
    assert repository.bind_recognition_turn.call_count == 0

    registry.clear_session(session_id=session.session_id, generation=1)
    registry.authorize_ready(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        session=session,
        frame=VoiceLocalReady(
            device_id=session.device_id,
            connection_generation=session.owner_connection_generation,
            session_id=session.session_id,
            generation=1,
            speech_revision=1,
            client_sequence=3,
        ),
        now=now,
    )
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=second,
            execution_base_render_revision=0,
            now=now,
        )
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await services.bind_local_recognition(
            socket_id=8,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=VoiceLocalRecognitionStarted(
                **{**second.__dict__, "recognition_sequence": 4}
            ),
            execution_base_render_revision=0,
            now=now,
        )
    assert repository.bind_recognition_turn.call_count == 0


@pytest.mark.asyncio
async def test_local_reservation_releases_on_bind_and_finalize_failures() -> None:
    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session = SimpleNamespace(
        session_id="00000000-0000-4000-8000-000000000031",
        user_id="user-a",
        device_id="00000000-0000-4000-8000-000000000021",
        owner_connection_generation="00000000-0000-4000-8000-000000000022",
        control_binding_id="00000000-0000-4000-8000-000000000023",
        control_binding_expires_at=now + timedelta(minutes=4),
        lease_expires_at=now + timedelta(minutes=2),
        control_owner_id="voice-coordinator-local-1",
        control_lease_expires_at=now + timedelta(seconds=30),
        generation=1,
        media_grant_revision=1,
        speech_backend="client_local",
        state="active",
        ended_at=None,
        foreground_active=True,
        microphone_enabled=True,
        speech_muted=False,
        visible_chat_id="00000000-0000-4000-8000-000000000072",
        chat_context_revision=1,
        applied_visible_chat_id="00000000-0000-4000-8000-000000000072",
        applied_chat_context_revision=1,
    )
    claims = VoiceControlClaims(
        subject="user-a",
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        binding_id=session.control_binding_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=4),
    )
    frame = VoiceLocalRecognitionStarted(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=1,
        speech_revision=1,
        client_turn_id="00000000-0000-4000-8000-000000000042",
        chat_id=session.visible_chat_id,
        chat_context_revision=1,
        recognition_sequence=1,
    )
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    registry = ClientLocalBindingRegistry()
    repository = SimpleNamespace(
        get_controlled_session=Mock(return_value=session),
        bind_recognition_turn=Mock(side_effect=RuntimeError("database unavailable")),
        reject_transcript=Mock(),
    )
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=repository,
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    assert registry._reservations == registry._sequences == {}

    repository.bind_recognition_turn.side_effect = None
    repository.bind_recognition_turn.return_value = SimpleNamespace(turn=turn)
    registry.finalize_turn = Mock(side_effect=VoiceControlBindingError("invalid_binding"))
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    repository.reject_transcript.assert_called_once_with(
        user_id="user-a",
        turn_id=turn.turn_id,
        reason="invalid_binding",
        retry_policy="none",
        now=now,
    )
    assert registry._reservations == registry._sequences == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("repository_outcome", ["rejected", "reject_fails", "bind_fails"])
async def test_cancelled_local_recognition_reconciles_late_thread_commit(
    repository_outcome: str,
) -> None:
    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session = SimpleNamespace(
        session_id="00000000-0000-4000-8000-000000000031",
        user_id="user-a",
        device_id="00000000-0000-4000-8000-000000000021",
        owner_connection_generation="00000000-0000-4000-8000-000000000022",
        control_binding_id="00000000-0000-4000-8000-000000000023",
        control_binding_expires_at=now + timedelta(minutes=4),
        lease_expires_at=now + timedelta(minutes=2),
        control_owner_id="voice-coordinator-local-1",
        control_lease_expires_at=now + timedelta(seconds=30),
        generation=1,
        media_grant_revision=1,
        speech_backend="client_local",
        state="active",
        ended_at=None,
        foreground_active=True,
        microphone_enabled=True,
        speech_muted=False,
        visible_chat_id="00000000-0000-4000-8000-000000000072",
        chat_context_revision=1,
        applied_visible_chat_id="00000000-0000-4000-8000-000000000072",
        applied_chat_context_revision=1,
    )
    claims = VoiceControlClaims(
        subject="user-a",
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        binding_id=session.control_binding_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=4),
    )
    frame = VoiceLocalRecognitionStarted(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=1,
        speech_revision=1,
        client_turn_id="00000000-0000-4000-8000-000000000042",
        chat_id=session.visible_chat_id,
        chat_context_revision=1,
        recognition_sequence=1,
    )
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    entered = threading.Event()
    release = threading.Event()
    rejection_entered = threading.Event()
    rejection_release = threading.Event()
    durable = {"state": "absent"}

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            entered.set()
            assert release.wait(timeout=2)
            if repository_outcome == "bind_fails":
                raise RuntimeError("late repository failure")
            durable["state"] = "recognizing"
            return SimpleNamespace(turn=turn)

        def reject_transcript(self, **_kwargs: Any) -> None:
            rejection_entered.set()
            assert rejection_release.wait(timeout=2)
            if repository_outcome == "reject_fails":
                raise RuntimeError("database unavailable")
            durable["state"] = "refused"

    registry = ClientLocalBindingRegistry()
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    task = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
        assert len(registry._reservations) == 1
    finally:
        release.set()
        if repository_outcome != "bind_fails":
            assert await asyncio.to_thread(rejection_entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            rejection_release.set()
        result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    if repository_outcome == "reject_fails":
        assert durable["state"] == "recognizing"
        assert len(registry._reservations) == len(registry._sequences) == 1
        assert registry._turns == {}
    elif repository_outcome == "rejected":
        assert durable["state"] == "refused"
        assert registry._reservations == registry._turns == registry._sequences == {}
    else:
        assert durable["state"] == "absent"
        assert registry._reservations == registry._turns == registry._sequences == {}


@pytest.mark.asyncio
async def test_repeated_cancel_during_late_bind_promotion_cannot_orphan_turn() -> None:
    """Promotion must stay joined while its pre-reserved slot lock is blocked."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    insert_entered = threading.Event()
    insert_release = threading.Event()
    insert_committed = threading.Event()
    rejection_entered = threading.Event()
    rejection_release = threading.Event()
    durable = {"state": "absent"}

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            insert_entered.set()
            assert insert_release.wait(timeout=2)
            durable["state"] = "recognizing"
            insert_committed.set()
            return SimpleNamespace(turn=turn)

        def reject_transcript(self, **_kwargs: Any) -> None:
            rejection_entered.set()
            assert rejection_release.wait(timeout=2)
            durable["state"] = "refused"

    registry = ClientLocalBindingRegistry()
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    task = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    assert await asyncio.to_thread(insert_entered.wait, 1)
    await services.pending_local_rejection_lock.acquire()
    task.cancel()
    insert_release.set()
    assert await asyncio.to_thread(insert_committed.wait, 1)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
        assert len(services.pending_local_rejection_reservations) == 1
        reserved = next(iter(services.pending_local_rejection_reservations.values()))
        assert not hasattr(reserved, "text")
        assert not hasattr(reserved, "text_digest_sha256")
    finally:
        services.pending_local_rejection_lock.release()
    assert await asyncio.to_thread(rejection_entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    rejection_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert durable["state"] == "refused"
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}
    assert registry._reservations == registry._turns == registry._sequences == {}


@pytest.mark.asyncio
async def test_local_rejection_capacity_is_reserved_before_repository_insert() -> None:
    """The last cleanup slot fences a later insert before it can become durable."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, first = _local_recognition_context(now)
    _, _, second = _local_recognition_context(
        now,
        client_turn_id="00000000-0000-4000-8000-000000000043",
        recognition_sequence=2,
    )
    insert_entered = threading.Event()
    insert_release = threading.Event()
    inserted: list[str] = []

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def reject_transcript(self, **_kwargs: Any) -> None:
            raise RuntimeError("pending cleanup unavailable")

        def bind_recognition_turn(self, binding: Any, **_kwargs: Any) -> Any:
            inserted.append(binding.client_turn_id)
            if binding.client_turn_id == first.client_turn_id:
                insert_entered.set()
                assert insert_release.wait(timeout=2)
                raise RuntimeError("late repository failure")
            return SimpleNamespace(
                turn=_voice_turn(client_turn_id=binding.client_turn_id)
            )

    registry = ClientLocalBindingRegistry()
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    services.pending_local_rejections.update(
        {
            ("other-user", f"turn-{index}"): SimpleNamespace(
                user_id="other-user",
                client_turn_id=f"turn-{index}",
                turn_id=f"00000000-0000-4000-8000-{index:012d}",
                session_id="other-session",
                generation=1,
                reason="invalid_binding",
                retry_policy="none",
                reservation=None,
            )
            for index in range(255)
        }
    )
    first_task = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=first,
            execution_base_render_revision=0,
            now=now,
        )
    )
    assert await asyncio.to_thread(insert_entered.wait, 1)
    try:
        with pytest.raises(VoiceControlBindingError, match="capacity_exhausted"):
            await services.bind_local_recognition(
                socket_id=7,
                current_socket_id=7,
                user_id="user-a",
                claims=claims,
                frame=second,
                execution_base_render_revision=0,
                now=now,
            )
    finally:
        insert_release.set()
    with pytest.raises(RuntimeError, match="late repository failure"):
        await first_task
    assert inserted == [first.client_turn_id]
    assert services.pending_local_rejection_reservations == {}
    assert len(services.pending_local_rejections) == 255
    assert registry._reservations == registry._turns == registry._sequences == {}


_LOCAL_RECOGNITION_CANCELLATION_PHASES = (
    "reserve",
    "insert",
    "promotion",
    "finalize",
    "slot_release",
    "pre_return",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", _LOCAL_RECOGNITION_CANCELLATION_PHASES)
async def test_local_recognition_cancellation_phase_table_terminalizes_commit(
    phase: str,
) -> None:
    """A cancellation after durable insert always owns exact terminal cleanup."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    phase_entered = threading.Event()
    phase_release = threading.Event()
    insert_entered = threading.Event()
    insert_release = threading.Event()
    durable = {"state": "absent"}
    public_task: asyncio.Task[Any] | None = None

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            insert_entered.set()
            if phase in {"insert", "promotion"}:
                if phase == "insert":
                    phase_entered.set()
                assert insert_release.wait(timeout=3)
            durable["state"] = "recognizing"
            return SimpleNamespace(turn=turn)

        def reject_transcript(self, **_kwargs: Any) -> None:
            durable["state"] = "refused"

    class Registry(ClientLocalBindingRegistry):
        def finalize_turn(self, **kwargs: Any) -> Any:
            authority = super().finalize_turn(**kwargs)
            if phase == "finalize":
                assert public_task is not None
                public_task.cancel()
            return authority

    class Services(VoiceServices):
        async def _reserve_pending_local_rejection(
            self,
            reservation: Any,
            *,
            coordination: Any,
        ) -> Any:
            if phase == "reserve":
                phase_entered.set()
                await asyncio.to_thread(phase_release.wait, 3)
            return await super()._reserve_pending_local_rejection(
                reservation,
                coordination=coordination,
            )

        async def _promote_pending_local_rejection(
            self,
            reserved: Any,
            *,
            turn_id: str,
            defer_for_end_fence: bool = False,
        ) -> Any:
            if phase == "promotion":
                phase_entered.set()
                await asyncio.to_thread(phase_release.wait, 3)
            return await super()._promote_pending_local_rejection(
                reserved,
                turn_id=turn_id,
                defer_for_end_fence=defer_for_end_fence,
            )

        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            if phase in {"finalize", "slot_release"}:
                phase_entered.set()
                await asyncio.to_thread(phase_release.wait, 3)
            acquired = await super()._acquire_pending_local_rejection_release(
                reserved
            )
            if phase == "pre_return":
                phase_entered.set()
                await asyncio.to_thread(phase_release.wait, 3)
            return acquired

    registry = Registry()
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    public_task = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    if phase == "promotion":
        assert await asyncio.to_thread(insert_entered.wait, 1)
        public_task.cancel()
        insert_release.set()
    assert await asyncio.to_thread(phase_entered.wait, 1)
    if phase != "finalize":
        public_task.cancel()
    public_task.cancel()
    await asyncio.sleep(0)
    try:
        if phase != "reserve":
            assert not public_task.done()
    finally:
        phase_release.set()
        insert_release.set()
    with pytest.raises(asyncio.CancelledError):
        await public_task
    if phase == "reserve":
        assert durable["state"] == "absent"
    else:
        assert durable["state"] == "refused"
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}
    assert registry._reservations == registry._turns == {}
    if phase in {"finalize", "slot_release", "pre_return"}:
        assert len(registry._sequences) == 1
    else:
        assert registry._sequences == {}


_LOCAL_RECOGNITION_REPLAY_CANCELLATION_PHASES = (
    "insert",
    "finalize",
    "slot_release",
    "pre_return",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", _LOCAL_RECOGNITION_REPLAY_CANCELLATION_PHASES)
async def test_local_recognition_replay_cancellation_preserves_durable_turn(
    phase: str,
) -> None:
    """A cancelled exact replay owns no durable recognition row."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    phase_entered = threading.Event()
    settlement_entered = threading.Event()
    phase_release = threading.Event()
    durable = {"state": "recognizing"}
    rejection_calls = 0
    public_task: asyncio.Task[Any] | None = None

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            if phase == "insert" and not phase_entered.is_set():
                phase_entered.set()
                assert phase_release.wait(timeout=3)
            return SimpleNamespace(turn=turn, replayed=True)

        def reject_transcript(self, **_kwargs: Any) -> None:
            nonlocal rejection_calls
            rejection_calls += 1
            durable["state"] = "refused"

    class Registry(ClientLocalBindingRegistry):
        def finalize_turn(self, **kwargs: Any) -> Any:
            authority = super().finalize_turn(**kwargs)
            if phase == "finalize" and not phase_entered.is_set():
                assert public_task is not None
                phase_entered.set()
                public_task.cancel()
            return authority

    class Services(VoiceServices):
        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            if phase in {"finalize", "slot_release"}:
                settlement_entered.set()
            if phase == "slot_release" and not phase_entered.is_set():
                phase_entered.set()
            if phase in {"finalize", "slot_release"}:
                await asyncio.to_thread(phase_release.wait, 3)
            acquired = await super()._acquire_pending_local_rejection_release(
                reserved
            )
            if phase == "pre_return" and not phase_entered.is_set():
                phase_entered.set()
                await asyncio.to_thread(phase_release.wait, 3)
            return acquired

    registry = Registry()
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    public_task = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    assert await asyncio.to_thread(phase_entered.wait, 1)
    if phase == "finalize":
        assert await asyncio.to_thread(settlement_entered.wait, 1)
    if phase != "finalize":
        public_task.cancel()
    public_task.cancel()
    await asyncio.sleep(0)
    try:
        assert not public_task.done()
    finally:
        phase_release.set()
    with pytest.raises(asyncio.CancelledError):
        await public_task

    assert durable["state"] == "recognizing"
    assert rejection_calls == 0
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}
    assert registry._reservations == registry._turns == registry._sequences == {}

    retry_turn, retry_authority = await services.bind_local_recognition(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=frame,
        execution_base_render_revision=0,
        now=now,
    )
    assert retry_turn is turn
    assert retry_authority.turn_id == turn.turn_id
    assert durable["state"] == "recognizing"
    assert rejection_calls == 0


@pytest.mark.asyncio
async def test_exact_recognition_replay_waits_for_originating_cleanup_settlement() -> None:
    """A replay cannot receive authority that its in-flight origin may reject."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()
    rejection_entered = threading.Event()
    rejection_release = threading.Event()
    durable = {"state": "absent"}
    get_turn_calls = 0

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            durable["state"] = "recognizing"
            return SimpleNamespace(turn=turn, replayed=False)

        def get_turn(self, **_kwargs: Any) -> Any:
            nonlocal get_turn_calls
            get_turn_calls += 1
            return turn

        def reject_transcript(self, **_kwargs: Any) -> None:
            rejection_entered.set()
            assert rejection_release.wait(timeout=3)
            durable["state"] = "refused"

    class Services(VoiceServices):
        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            settlement_entered.set()
            await settlement_release.wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    first = asyncio.create_task(services.bind_local_recognition(**kwargs))
    await asyncio.wait_for(settlement_entered.wait(), timeout=1)
    second = asyncio.create_task(services.bind_local_recognition(**kwargs))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        assert not second.done()
        assert get_turn_calls == 0
        first.cancel()
        first.cancel()
        settlement_release.set()
        assert await asyncio.to_thread(rejection_entered.wait, 1)
        assert not second.done()
    finally:
        rejection_release.set()
        settlement_release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert durable["state"] == "refused"
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await second
    assert get_turn_calls == 0
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    ["different_user", "different_turn"],
)
async def test_distinct_recognition_identities_insert_concurrently(
    identity: str,
) -> None:
    """Only an exact user/client-turn pair is serialized."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    first_session, first_claims, first_frame = _local_recognition_context(now)
    second_user = "user-b" if identity == "different_user" else "user-a"
    second_turn_id = (
        first_frame.client_turn_id
        if identity == "different_user"
        else "00000000-0000-4000-8000-000000000043"
    )
    second_session_id = (
        "00000000-0000-4000-8000-000000000034"
        if identity == "different_user"
        else first_session.session_id
    )
    second_session = SimpleNamespace(
        **{
            **first_session.__dict__,
            "user_id": second_user,
            "session_id": second_session_id,
        }
    )
    second_claims = VoiceControlClaims(
        subject=second_user,
        device_id=second_session.device_id,
        connection_generation=second_session.owner_connection_generation,
        binding_id=second_session.control_binding_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=4),
    )
    second_frame = VoiceLocalRecognitionStarted(
        **{
            **first_frame.__dict__,
            "session_id": second_session_id,
            "client_turn_id": second_turn_id,
            "recognition_sequence": (
                1 if identity == "different_user" else 2
            ),
        }
    )
    turns = {
        ("user-a", first_frame.client_turn_id): _voice_turn(
            client_turn_id=first_frame.client_turn_id
        ),
        (second_user, second_turn_id): replace(
            _voice_turn(
                turn_id="00000000-0000-4000-8000-000000000033",
                client_turn_id=second_turn_id,
                session_id=second_session_id,
            ),
            user_id=second_user,
        ),
    }
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    class Repository:
        def get_controlled_session(self, *, user_id: str, **_kwargs: Any) -> Any:
            return first_session if user_id == "user-a" else second_session

        def bind_recognition_turn(self, binding: Any, **_kwargs: Any) -> Any:
            nonlocal calls
            with call_lock:
                index = calls
                calls += 1
            entered[index].set()
            assert release.wait(timeout=3)
            return SimpleNamespace(
                turn=turns[(binding.user_id, binding.client_turn_id)],
                replayed=False,
            )

    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )

    def invoke(user_id: str, claims: Any, frame: Any, socket_id: int) -> Any:
        return services.bind_local_recognition(
            socket_id=socket_id,
            current_socket_id=socket_id,
            user_id=user_id,
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )

    first = asyncio.create_task(invoke("user-a", first_claims, first_frame, 7))
    assert await asyncio.to_thread(entered[0].wait, 1)
    second = asyncio.create_task(
        invoke(
            second_user,
            second_claims,
            second_frame,
            8 if identity == "different_user" else 7,
        )
    )
    try:
        assert await asyncio.to_thread(entered[1].wait, 1)
    finally:
        release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result[0].client_turn_id == first_frame.client_turn_id
    assert second_result[0].client_turn_id == second_turn_id
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
async def test_failed_recognition_rejection_retains_key_until_drain() -> None:
    """Transient terminalization keeps same-turn replay behind its exact owner."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()
    reject_fails = True

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=False)

        def reject_transcript(self, **_kwargs: Any) -> None:
            if reject_fails:
                raise RuntimeError("database unavailable")

    class Services(VoiceServices):
        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            settlement_entered.set()
            await settlement_release.wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    first = asyncio.create_task(services.bind_local_recognition(**kwargs))
    await asyncio.wait_for(settlement_entered.wait(), timeout=1)
    second = asyncio.create_task(services.bind_local_recognition(**kwargs))
    first.cancel()
    first.cancel()
    settlement_release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(services.pending_local_rejections) == 1
    assert not second.done()
    second.cancel()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert len(services.local_recognition_requests) == 1
    assert len(services.local_recognition_keys) == 1
    reject_fails = False
    await services._drain_pending_local_rejections(now=now)
    assert services.pending_local_rejections == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
async def test_recognition_coordinator_capacity_fails_before_repository_access() -> None:
    """The bounded key registry refuses a 257th live request before mutation."""

    repository = SimpleNamespace(get_controlled_session=Mock())
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=repository,
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
    )
    requests = [
        await services._acquire_local_recognition_request(
            user_id="user-a",
            client_turn_id=f"00000000-0000-4000-8000-{index:012d}",
        )
        for index in range(256)
    ]
    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    _session, claims, frame = _local_recognition_context(
        now,
        client_turn_id="10000000-0000-4000-8000-000000000257",
    )
    with pytest.raises(VoiceControlBindingError, match="capacity_exhausted"):
        await services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    repository.get_controlled_session.assert_not_called()
    for request in requests:
        await services._release_local_recognition_request(request)
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    ["insert", "finalize", "slot_release", "pre_return"],
)
async def test_exact_recognition_replay_waits_at_every_return_phase(
    phase: str,
) -> None:
    """A successful origin owns its turn key until its return is committed."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    phase_entered = threading.Event()
    phase_release = threading.Event()
    get_turn_calls = 0

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            if phase == "insert":
                phase_entered.set()
                assert phase_release.wait(timeout=3)
            return SimpleNamespace(turn=turn, replayed=False)

        def get_turn(self, **_kwargs: Any) -> Any:
            nonlocal get_turn_calls
            get_turn_calls += 1
            return turn

    class Registry(ClientLocalBindingRegistry):
        def finalize_turn(self, **kwargs: Any) -> Any:
            authority = super().finalize_turn(**kwargs)
            if phase == "finalize":
                phase_entered.set()
            return authority

    class Services(VoiceServices):
        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            if phase in {"finalize", "slot_release"}:
                if phase == "slot_release":
                    phase_entered.set()
                await asyncio.to_thread(phase_release.wait, 3)
            acquired = await super()._acquire_pending_local_rejection_release(
                reserved
            )
            if phase == "pre_return":
                phase_entered.set()
                await asyncio.to_thread(phase_release.wait, 3)
            return acquired

    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=Registry(),
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    first = asyncio.create_task(services.bind_local_recognition(**kwargs))
    assert await asyncio.to_thread(phase_entered.wait, 1)
    second = asyncio.create_task(services.bind_local_recognition(**kwargs))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not second.done()
        assert get_turn_calls == 0
    finally:
        phase_release.set()
    first_result = await first
    second_result = await second
    assert first_result[0].turn_id == turn.turn_id
    assert second_result[0].turn_id == turn.turn_id
    assert get_turn_calls == 1
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("replayed", [False, True], ids=["new", "replay"])
@pytest.mark.parametrize(
    "cleanup_fails",
    [False, True],
    ids=["abandon-succeeds", "abandon-fails"],
)
@pytest.mark.parametrize("cancelled", [False, True], ids=["return", "cancel"])
async def test_session_cleanup_before_recognition_settlement_never_returns_stale_authority(
    replayed: bool,
    cleanup_fails: bool,
    cancelled: bool,
) -> None:
    """A completed session cleanup wins before the bind return boundary."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()
    rejections: list[str] = []

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=replayed)

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            if cleanup_fails:
                raise RuntimeError("database unavailable")
            return None

        def reject_transcript(self, **kwargs: Any) -> None:
            rejections.append(kwargs["turn_id"])

    class Services(VoiceServices):
        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            settlement_entered.set()
            await settlement_release.wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    registry = ClientLocalBindingRegistry()
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    task = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    await asyncio.wait_for(settlement_entered.wait(), timeout=1)
    if cleanup_fails:
        with pytest.raises(RuntimeError, match="database unavailable"):
            await services.cleanup_local_buffers(session)
    else:
        await services.cleanup_local_buffers(session)
    if cancelled:
        task.cancel()
        task.cancel()
    settlement_release.set()

    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
            await task
    assert rejections == (
        [turn.turn_id] if cleanup_fails and not replayed else []
    )
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}
    assert registry._reservations == registry._turns == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_turn_id",
    [
        "00000000-0000-4000-8000-000000000042",
        "00000000-0000-4000-8000-000000000043",
    ],
    ids=["exact-turn", "different-turn"],
)
async def test_different_session_cleanup_cannot_invalidate_recognition_return(
    client_turn_id: str,
) -> None:
    """Cleanup serializes only authority owned by its exact session generation."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(
        now,
        client_turn_id=client_turn_id,
    )
    turn = _voice_turn(client_turn_id=client_turn_id)
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()
    other_session = SimpleNamespace(
        user_id="user-a",
        session_id="00000000-0000-4000-8000-000000000099",
        generation=1,
    )

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=False)

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            return None

    class Services(VoiceServices):
        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            settlement_entered.set()
            await settlement_release.wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    task = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    await asyncio.wait_for(settlement_entered.wait(), timeout=1)
    await services.cleanup_local_buffers(other_session)
    settlement_release.set()

    returned_turn, authority = await task
    assert returned_turn.turn_id == turn.turn_id
    assert authority.client_turn_id == client_turn_id
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("replayed", [False, True], ids=["new", "replay"])
async def test_replaced_finalized_authority_fails_exact_return_identity(
    replayed: bool,
) -> None:
    """Object-distinct authority for the same key cannot satisfy this request."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()
    rejections: list[str] = []

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=replayed)

        def reject_transcript(self, **kwargs: Any) -> None:
            rejections.append(kwargs["turn_id"])

    class Services(VoiceServices):
        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            settlement_entered.set()
            await settlement_release.wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    registry = ClientLocalBindingRegistry()
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    task = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    await asyncio.wait_for(settlement_entered.wait(), timeout=1)
    key = ("user-a", frame.client_turn_id)
    finalized = registry._turns[key]
    replacement = replace(
        finalized,
        turn_id="00000000-0000-4000-8000-000000000099",
    )
    registry._turns[key] = replacement
    settlement_release.set()

    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await task
    assert rejections == ([] if replayed else [turn.turn_id])
    if replayed:
        assert registry._turns[key] is replacement
    else:
        assert key not in registry._turns
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_fails",
    [False, True],
    ids=["abandon-succeeds", "abandon-fails"],
)
@pytest.mark.parametrize("cancelled", [False, True], ids=["return", "cancel"])
async def test_existing_authority_replay_cleanup_during_lookup_never_returns_stale_authority(
    cleanup_fails: bool,
    cancelled: bool,
) -> None:
    """Session cleanup that wins the replay lookup revokes its exact authority."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    lookup_entered = threading.Event()
    lookup_release = threading.Event()
    lookup_finished = threading.Event()

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=False)

        def get_turn(self, **_kwargs: Any) -> Any:
            lookup_entered.set()
            assert lookup_release.wait(timeout=3)
            lookup_finished.set()
            return turn

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            if cleanup_fails:
                raise RuntimeError("database unavailable")

    registry = ClientLocalBindingRegistry()
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    initial_turn, initial_authority = await services.bind_local_recognition(
        **kwargs
    )
    assert initial_turn is turn
    assert registry.get_turn(
        user_id="user-a",
        client_turn_id=frame.client_turn_id,
        now=now,
    ) is initial_authority

    replay = asyncio.create_task(services.bind_local_recognition(**kwargs))
    assert await asyncio.to_thread(lookup_entered.wait, 1)
    if cancelled:
        replay.cancel()
        replay.cancel()
    try:
        if cleanup_fails:
            with pytest.raises(RuntimeError, match="database unavailable"):
                await services.cleanup_local_buffers(session)
        else:
            await services.cleanup_local_buffers(session)
    finally:
        lookup_release.set()

    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await replay
    else:
        with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
            await replay
    assert await asyncio.to_thread(lookup_finished.wait, 1)
    assert registry._reservations == registry._turns == registry._sequences == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
async def test_repeated_cancelled_existing_authority_lookup_preserves_replay_authority() -> None:
    """Lookup cancellation releases only its coordinator, not replay-owned authority."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    lookup_entered = threading.Event()
    lookup_release = threading.Event()
    lookup_finished = threading.Event()

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=False)

        def get_turn(self, **_kwargs: Any) -> Any:
            lookup_entered.set()
            assert lookup_release.wait(timeout=3)
            lookup_finished.set()
            return turn

    registry = ClientLocalBindingRegistry()
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    _, authority = await services.bind_local_recognition(**kwargs)
    replay = asyncio.create_task(services.bind_local_recognition(**kwargs))
    assert await asyncio.to_thread(lookup_entered.wait, 1)
    await services.pending_local_rejection_lock.acquire()
    try:
        replay.cancel()
        await asyncio.sleep(0)
        replay.cancel()
        await asyncio.sleep(0)
        replay.cancel()
        assert not replay.done()
    finally:
        lookup_release.set()
        services.pending_local_rejection_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await replay
    assert await asyncio.to_thread(lookup_finished.wait, 1)
    assert registry.get_turn(
        user_id="user-a",
        client_turn_id=frame.client_turn_id,
        now=now,
    ) is authority
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
async def test_existing_authority_replay_proves_exact_identity_before_no_await_return() -> None:
    """An object-distinct replacement cannot satisfy the replay return fence."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    replace_authority = False
    outer_release_calls = 0

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=False)

        def get_turn(self, **_kwargs: Any) -> Any:
            if replace_authority:
                key = ("user-a", frame.client_turn_id)
                registry._turns[key] = replace(
                    registry._turns[key],
                    turn_id="00000000-0000-4000-8000-000000000099",
                )
            return turn

    class Services(VoiceServices):
        async def _join_local_recognition_request_release(
            self,
            request: Any,
        ) -> None:
            nonlocal outer_release_calls
            outer_release_calls += 1
            await super()._join_local_recognition_request_release(request)

    registry = ClientLocalBindingRegistry()
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    _, authority = await services.bind_local_recognition(**kwargs)
    assert outer_release_calls == 0

    returned_turn, returned_authority = await services.bind_local_recognition(
        **kwargs
    )
    assert returned_turn is turn
    assert returned_authority is authority
    assert outer_release_calls == 0

    replace_authority = True
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await services.bind_local_recognition(**kwargs)
    replacement = registry.get_turn(
        user_id="user-a",
        client_turn_id=frame.client_turn_id,
        now=now,
    )
    assert replacement is not authority
    assert replacement.turn_id == "00000000-0000-4000-8000-000000000099"
    assert outer_release_calls == 0
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    ["different_session", "different_turn", "different_user"],
)
async def test_existing_authority_replay_settlement_is_exact(
    identity: str,
) -> None:
    """Unrelated session, turn, or user authority cannot disturb replay proof."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    lookup_entered = threading.Event()
    lookup_release = threading.Event()
    outer_release_calls = 0

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=False)

        def get_turn(self, **_kwargs: Any) -> Any:
            lookup_entered.set()
            assert lookup_release.wait(timeout=3)
            return turn

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            return None

    class Services(VoiceServices):
        async def _join_local_recognition_request_release(
            self,
            request: Any,
        ) -> None:
            nonlocal outer_release_calls
            outer_release_calls += 1
            await super()._join_local_recognition_request_release(request)

    registry = ClientLocalBindingRegistry()
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    _, authority = await services.bind_local_recognition(**kwargs)
    assert outer_release_calls == 0

    other_user = "user-b" if identity == "different_user" else "user-a"
    other_session_id = (
        session.session_id
        if identity == "different_turn"
        else "00000000-0000-4000-8000-000000000099"
    )
    other_client_turn_id = (
        frame.client_turn_id
        if identity == "different_user"
        else "00000000-0000-4000-8000-000000000043"
    )
    other_session = SimpleNamespace(
        **{
            **session.__dict__,
            "user_id": other_user,
            "session_id": other_session_id,
        }
    )
    other_claims = VoiceControlClaims(
        subject=other_user,
        device_id=other_session.device_id,
        connection_generation=other_session.owner_connection_generation,
        binding_id=other_session.control_binding_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=4),
    )
    other_frame = VoiceLocalRecognitionStarted(
        device_id=other_session.device_id,
        connection_generation=other_session.owner_connection_generation,
        session_id=other_session.session_id,
        generation=other_session.generation,
        speech_revision=other_session.media_grant_revision,
        client_turn_id=other_client_turn_id,
        chat_id=other_session.visible_chat_id,
        chat_context_revision=other_session.chat_context_revision,
        recognition_sequence=2 if identity == "different_turn" else 1,
    )
    other_turn = replace(
        _voice_turn(
            turn_id="00000000-0000-4000-8000-000000000033",
            client_turn_id=other_client_turn_id,
            session_id=other_session_id,
        ),
        user_id=other_user,
    )
    registry.bind_turn(
        socket_id=7,
        current_socket_id=7,
        user_id=other_user,
        claims=other_claims,
        session=other_session,
        frame=other_frame,
        turn=other_turn,
        now=now,
    )

    replay = asyncio.create_task(services.bind_local_recognition(**kwargs))
    assert await asyncio.to_thread(lookup_entered.wait, 1)
    if identity == "different_turn":
        registry.release_turn(
            user_id=other_user,
            client_turn_id=other_client_turn_id,
        )
    else:
        await services.cleanup_local_buffers(other_session)
    lookup_release.set()

    returned_turn, returned_authority = await replay
    assert returned_turn is turn
    assert returned_authority is authority
    assert registry.get_turn(
        user_id="user-a",
        client_turn_id=frame.client_turn_id,
        now=now,
    ) is authority
    assert outer_release_calls == 0
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    ["existing_authority", "new_mutation", "repository_replay"],
)
@pytest.mark.parametrize(
    "cleanup_first",
    [True, False],
    ids=["cleanup-proof-first", "return-proof-first"],
)
async def test_cleanup_fence_orders_every_recognition_return_before_abandonment(
    shape: str,
    cleanup_first: bool,
) -> None:
    """The settlement-lock winner is the sole permitted durable ordering."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()
    abandonment_started = threading.Event()
    abandonment_release = threading.Event()

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                turn=turn,
                replayed=shape == "repository_replay",
            )

        def get_turn(self, **_kwargs: Any) -> Any:
            return turn

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            abandonment_started.set()
            assert abandonment_release.wait(timeout=3)

        def reject_transcript(self, **_kwargs: Any) -> None:
            return None

    class Services(VoiceServices):
        gate_returns = False

        async def _complete_existing_local_recognition_before_return(
            self,
            **kwargs: Any,
        ) -> None:
            if self.gate_returns:
                settlement_entered.set()
                await settlement_release.wait()
            await super()._complete_existing_local_recognition_before_return(
                **kwargs
            )

        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            if self.gate_returns:
                settlement_entered.set()
                await settlement_release.wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    if shape == "existing_authority":
        await services.bind_local_recognition(**kwargs)
    services.gate_returns = True
    bind_task = asyncio.create_task(services.bind_local_recognition(**kwargs))
    await asyncio.wait_for(settlement_entered.wait(), timeout=1)
    await services.pending_local_rejection_lock.acquire()
    cleanup_task: asyncio.Task[None] | None = None
    try:
        if cleanup_first:
            cleanup_task = asyncio.create_task(
                services.cleanup_local_buffers(session)
            )
            await asyncio.sleep(0)
            settlement_release.set()
        else:
            settlement_release.set()
            await asyncio.sleep(0)
            cleanup_task = asyncio.create_task(
                services.cleanup_local_buffers(session)
            )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not abandonment_started.is_set()
    finally:
        services.pending_local_rejection_lock.release()

    assert cleanup_task is not None
    assert await asyncio.to_thread(abandonment_started.wait, 1)
    abandonment_release.set()
    await cleanup_task
    if cleanup_first:
        with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
            await bind_task
    else:
        returned_turn, authority = await bind_task
        assert returned_turn is turn
        assert authority.turn_id == turn.turn_id
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
async def test_cancelled_duplicate_cleanup_callers_join_one_retained_operation() -> None:
    """Repeated cancellation cannot orphan or duplicate exact abandonment."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    abandonment_started = threading.Event()
    abandonment_release = threading.Event()
    abandonment_calls = 0
    bind_calls = 0

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            nonlocal bind_calls
            bind_calls += 1
            return SimpleNamespace(turn=turn, replayed=False)

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            nonlocal abandonment_calls
            abandonment_calls += 1
            abandonment_started.set()
            assert abandonment_release.wait(timeout=3)

    registry = ClientLocalBindingRegistry()
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    await services.bind_local_recognition(**kwargs)
    first = asyncio.create_task(services.cleanup_local_buffers(session))
    assert await asyncio.to_thread(abandonment_started.wait, 1)
    duplicate = asyncio.create_task(services.cleanup_local_buffers(session))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done()
    assert not duplicate.done()
    assert abandonment_calls == 1
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await services.bind_local_recognition(**kwargs)
    assert bind_calls == 1

    abandonment_release.set()
    await duplicate
    with pytest.raises(asyncio.CancelledError):
        await first
    await services.cleanup_local_buffers(session)
    assert abandonment_calls == 1
    assert registry._reservations == registry._turns == registry._sequences == {}
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}
    assert len(services.local_cleanup_epochs) == 1
    assert len(services.local_cleanup_operations) == 1


@pytest.mark.asyncio
async def test_cleanup_cancelled_before_fence_publishes_has_no_side_effect() -> None:
    """Cancellation while waiting for publication cannot launch abandonment."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    abandonment_calls = 0

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=False)

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            nonlocal abandonment_calls
            abandonment_calls += 1

    registry = ClientLocalBindingRegistry()
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    _, authority = await services.bind_local_recognition(**kwargs)
    await services.pending_local_rejection_lock.acquire()
    cleanup = asyncio.create_task(services.cleanup_local_buffers(session))
    try:
        await asyncio.sleep(0)
        cleanup.cancel()
        cleanup.cancel()
    finally:
        services.pending_local_rejection_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert abandonment_calls == 0
    assert registry.get_turn(
        user_id="user-a",
        client_turn_id=frame.client_turn_id,
        now=now,
    ) is authority
    assert services.local_cleanup_epochs == {}
    assert services.local_cleanup_operations == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_fails",
    [False, True],
    ids=["success", "failure"],
)
async def test_cleanup_cancellation_after_database_waits_for_reconciliation(
    cleanup_fails: bool,
) -> None:
    """A completed DB call is reconciled under lock before cancellation escapes."""

    session = SimpleNamespace(
        user_id="user-a",
        session_id="00000000-0000-4000-8000-000000000031",
        generation=1,
    )
    abandonment_started = threading.Event()
    abandonment_release = threading.Event()
    abandonment_finished = threading.Event()

    def abandon_preacceptance_turns(**_kwargs: Any) -> None:
        abandonment_started.set()
        assert abandonment_release.wait(timeout=3)
        abandonment_finished.set()
        if cleanup_fails:
            raise RuntimeError("database unavailable")

    bindings = SimpleNamespace(clear_session=Mock())
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=SimpleNamespace(
            abandon_preacceptance_turns=abandon_preacceptance_turns
        ),
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=bindings,
    )
    cleanup = asyncio.create_task(services.cleanup_local_buffers(session))
    assert await asyncio.to_thread(abandonment_started.wait, 1)
    await services.pending_local_rejection_lock.acquire()
    try:
        abandonment_release.set()
        assert await asyncio.to_thread(abandonment_finished.wait, 1)
        await asyncio.sleep(0)
        cleanup.cancel()
        await asyncio.sleep(0)
        cleanup.cancel()
        await asyncio.sleep(0)
        assert not cleanup.done()
        bindings.clear_session.assert_not_called()
    finally:
        services.pending_local_rejection_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    bindings.clear_session.assert_called_once_with(
        user_id="user-a",
        session_id=session.session_id,
        generation=session.generation,
    )
    operation = next(iter(services.local_cleanup_operations.values()))
    assert operation.status == ("failed" if cleanup_fails else "succeeded")
    assert operation.task is not None and operation.task.done()
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_state", ["succeeded", "failed", "active"])
async def test_durable_end_joins_and_prunes_exact_cleanup_state(
    cleanup_state: str,
) -> None:
    """Ended generations retain neither completed nor in-flight cleanup slots."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, _claims, _frame = _local_recognition_context(now)
    ended = SimpleNamespace(
        **{
            **session.__dict__,
            "ended_at": now,
            "end_reason": "user",
        }
    )
    abandonment_started = threading.Event()
    abandonment_release = threading.Event()

    def abandon_preacceptance_turns(**_kwargs: Any) -> None:
        abandonment_started.set()
        if cleanup_state == "active":
            assert abandonment_release.wait(timeout=3)
        if cleanup_state == "failed":
            raise RuntimeError("database unavailable")

    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=SimpleNamespace(
            abandon_preacceptance_turns=abandon_preacceptance_turns
        ),
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    cleanup = asyncio.create_task(services.cleanup_local_buffers(session))
    assert await asyncio.to_thread(abandonment_started.wait, 1)
    if cleanup_state == "failed":
        with pytest.raises(RuntimeError, match="database unavailable"):
            await cleanup
    elif cleanup_state == "succeeded":
        await cleanup

    terminal = asyncio.create_task(
        services.handle_runtime_session_end(ended, "user")
    )
    if cleanup_state == "active":
        await asyncio.sleep(0)
        assert not terminal.done()
        assert len(services.local_cleanup_operations) == 1
        abandonment_release.set()
        await cleanup
    await terminal
    assert services.local_cleanup_operations == {}
    assert services.local_cleanup_epochs == {}
    assert services.local_end_fences == {}


@pytest.mark.asyncio
async def test_failed_cleanup_retries_before_fresh_ready_and_old_work_stays_stale() -> None:
    """Only a successful retry plus fresh ready rotates a closed epoch."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, old_frame = _local_recognition_context(now)
    old_turn = _voice_turn(client_turn_id=old_frame.client_turn_id)
    new_client_turn_id = "00000000-0000-4000-8000-000000000043"
    new_turn = _voice_turn(
        turn_id="00000000-0000-4000-8000-000000000033",
        client_turn_id=new_client_turn_id,
    )
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()
    abandon_calls = 0

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, binding: Any, **_kwargs: Any) -> Any:
            selected = (
                old_turn
                if binding.client_turn_id == old_frame.client_turn_id
                else new_turn
            )
            return SimpleNamespace(turn=selected, replayed=False)

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            nonlocal abandon_calls
            abandon_calls += 1
            if abandon_calls == 1:
                raise RuntimeError("database unavailable")

    class Services(VoiceServices):
        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            if reserved.client_turn_id == old_frame.client_turn_id:
                settlement_entered.set()
                await settlement_release.wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    registry = ClientLocalBindingRegistry()
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    old_bind = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=old_frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    await asyncio.wait_for(settlement_entered.wait(), timeout=1)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await services.cleanup_local_buffers(session)
    ready = VoiceLocalReady(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=session.generation,
        speech_revision=session.media_grant_revision,
        client_sequence=2,
    )
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await services.local_ready(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=ready,
            now=now,
        )
    await services.cleanup_local_buffers(session)
    assert abandon_calls == 2
    assert await services.local_ready(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=ready,
        now=now,
    ) is session
    assert len(services.local_cleanup_epochs) == 1
    assert len(services.local_cleanup_operations) == 1
    await services.complete_local_ready_delivery(
        session,
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=ready,
        now=now,
        authority_is_current=lambda: True,
    )
    assert services.local_cleanup_epochs == {}
    assert services.local_cleanup_operations == {}

    settlement_release.set()
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await old_bind
    new_frame = VoiceLocalRecognitionStarted(
        **{
            **old_frame.__dict__,
            "client_turn_id": new_client_turn_id,
            "recognition_sequence": 3,
        }
    )
    returned_turn, authority = await services.bind_local_recognition(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=new_frame,
        execution_base_render_revision=0,
        now=now,
    )
    assert returned_turn is new_turn
    assert authority.client_turn_id == new_client_turn_id
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}
    assert services.local_cleanup_epochs == {}
    assert services.local_cleanup_operations == {}


@pytest.mark.asyncio
async def test_local_cleanup_blocks_announcements_until_ready_delivery() -> None:
    """A stopped epoch cannot publish new speech before its ready barrier."""

    now = datetime.now(UTC)
    session, _claims, frame = _local_recognition_context(now)
    turn = _voice_turn(session_id=session.session_id)

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return session

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            return None

    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    services.local_announcements.issue(
        session=session,
        kind="acknowledgement",
        turn_id=frame.client_turn_id,
        requested_text="ignored",
        output_policy="lifecycle",
        mute_revision=1,
        consent_revision=1,
        now=now,
    )
    publisher = AsyncMock()
    services.bind_local_announcement_publisher(publisher)
    await services.cleanup_local_buffers(session)

    with pytest.raises(VoiceBootstrapError, match="local_session_not_ready"):
        await services._publish_local_announcement(turn, kind="failure")

    publisher.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_cleanup_ready_delivery_starts_announcement_sequence_at_one() -> None:
    """The delivered ready frame is the barrier for a fresh output epoch."""

    now = datetime.now(UTC)
    session, claims, recognition = _local_recognition_context(now)
    turn = _voice_turn(session_id=session.session_id)

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def get_session(self, **_kwargs: Any) -> Any:
            return session

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            return None

    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    for kind in ("acknowledgement", "failure"):
        services.local_announcements.issue(
            session=session,
            kind=kind,
            turn_id=recognition.client_turn_id,
            requested_text="ignored",
            output_policy="lifecycle",
            mute_revision=1,
            consent_revision=1,
            now=now,
        )
    await services.cleanup_local_buffers(session)
    ready = VoiceLocalReady(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=session.generation,
        speech_revision=session.media_grant_revision,
        client_sequence=1,
    )

    assert await services.local_ready(
        socket_id=7,
        current_socket_id=7,
        user_id=session.user_id,
        claims=claims,
        frame=ready,
        now=now,
    ) is session
    assert services.local_announcements.retained_counts() == {
        "sessions": 0,
        "announcements": 0,
    }
    assert len(services.local_cleanup_epochs) == 1
    assert len(services.local_cleanup_operations) == 1

    await services.complete_local_ready_delivery(
        session,
        socket_id=7,
        current_socket_id=7,
        user_id=session.user_id,
        claims=claims,
        frame=ready,
        now=now,
        authority_is_current=lambda: True,
    )

    assert services.local_cleanup_epochs == {}
    assert services.local_cleanup_operations == {}
    publisher = AsyncMock()
    services.bind_local_announcement_publisher(publisher)
    await services._publish_local_announcement(turn, kind="failure")
    delivered = publisher.await_args.args[0]
    assert delivered.announcement_sequence == 1


@pytest.mark.asyncio
async def test_later_cleanup_supersedes_inflight_ready_delivery() -> None:
    """A ready send that predates a later Stop cannot reopen local speech."""

    now = datetime.now(UTC)
    session, claims, recognition = _local_recognition_context(now)

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            return None

    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    ready = VoiceLocalReady(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=session.generation,
        speech_revision=session.media_grant_revision,
        client_sequence=recognition.recognition_sequence + 1,
    )
    await services.cleanup_local_buffers(session)
    assert await services.local_ready(
        socket_id=7,
        current_socket_id=7,
        user_id=session.user_id,
        claims=claims,
        frame=ready,
        now=now,
    ) is session

    await services.cleanup_local_buffers(session)

    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await services.complete_local_ready_delivery(
            session,
            socket_id=7,
            current_socket_id=7,
            user_id=session.user_id,
            claims=claims,
            frame=ready,
            now=now,
            authority_is_current=lambda: True,
        )
    assert len(services.local_cleanup_epochs) == 1
    assert len(services.local_cleanup_operations) == 1


@pytest.mark.asyncio
async def test_cleanup_supersedes_ready_blocked_in_repository_lookup() -> None:
    """A Stop during readiness lookup invalidates that stale observation."""

    now = datetime.now(UTC)
    session, claims, recognition = _local_recognition_context(now)
    lookup_started = threading.Event()
    lookup_release = threading.Event()

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            lookup_started.set()
            assert lookup_release.wait(timeout=3)
            return session

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            return None

    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    ready = VoiceLocalReady(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=session.generation,
        speech_revision=session.media_grant_revision,
        client_sequence=recognition.recognition_sequence + 1,
    )
    await services.cleanup_local_buffers(session)
    pending_ready = asyncio.create_task(
        services.local_ready(
            socket_id=7,
            current_socket_id=7,
            user_id=session.user_id,
            claims=claims,
            frame=ready,
            now=now,
        )
    )
    assert await asyncio.to_thread(lookup_started.wait, 1)

    await services.cleanup_local_buffers(session)
    lookup_release.set()

    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await pending_ready
    assert len(services.local_cleanup_epochs) == 1
    assert next(iter(services.local_cleanup_epochs.values())).ready_transition is None


@pytest.mark.asyncio
async def test_ready_completion_rechecks_live_authority_after_repository_lookup() -> None:
    """Socket revocation during final lookup leaves the output epoch closed."""

    now = datetime.now(UTC)
    session, claims, recognition = _local_recognition_context(now)
    completion_lookup_started = threading.Event()
    completion_lookup_release = threading.Event()
    lookup_count = 0
    authority_current = True

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            nonlocal lookup_count
            lookup_count += 1
            if lookup_count == 2:
                completion_lookup_started.set()
                assert completion_lookup_release.wait(timeout=3)
            return session

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            return None

    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    ready = VoiceLocalReady(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=session.generation,
        speech_revision=session.media_grant_revision,
        client_sequence=recognition.recognition_sequence + 1,
    )
    await services.cleanup_local_buffers(session)
    assert await services.local_ready(
        socket_id=7,
        current_socket_id=7,
        user_id=session.user_id,
        claims=claims,
        frame=ready,
        now=now,
    ) is session
    completing = asyncio.create_task(
        services.complete_local_ready_delivery(
            session,
            socket_id=7,
            current_socket_id=7,
            user_id=session.user_id,
            claims=claims,
            frame=ready,
            now=now,
            authority_is_current=lambda: authority_current,
        )
    )
    assert await asyncio.to_thread(completion_lookup_started.wait, 1)

    authority_current = False
    completion_lookup_release.set()

    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await completing
    assert len(services.local_cleanup_epochs) == 1
    assert len(services.local_cleanup_operations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replayed", [False, True], ids=["new", "replay"])
async def test_cleanup_joins_prefence_mutation_before_durable_abandonment(
    replayed: bool,
) -> None:
    """A late commit/replay cannot escape the retained cleanup owner."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    mutation_started = threading.Event()
    mutation_release = threading.Event()
    abandonment_started = threading.Event()

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            mutation_started.set()
            assert mutation_release.wait(timeout=3)
            return SimpleNamespace(turn=turn, replayed=replayed)

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            abandonment_started.set()

        def reject_transcript(self, **_kwargs: Any) -> None:
            return None

    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    bind = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    assert await asyncio.to_thread(mutation_started.wait, 1)
    cleanup = asyncio.create_task(services.cleanup_local_buffers(session))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not abandonment_started.is_set()
    mutation_release.set()
    assert await asyncio.to_thread(abandonment_started.wait, 1)
    await cleanup
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await bind
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    ["different_user", "different_session", "different_generation"],
)
async def test_blocked_cleanup_is_exact_and_does_not_serialize_other_identity(
    identity: str,
) -> None:
    """Cleanup DB work holds no global lock and clears only its exact owner."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    cleanup_session, _claims, _frame = _local_recognition_context(now)
    other_user = "user-b" if identity == "different_user" else "user-a"
    other_session_id = (
        "00000000-0000-4000-8000-000000000099"
        if identity == "different_session"
        else cleanup_session.session_id
    )
    other_generation = 2 if identity == "different_generation" else 1
    other_session = SimpleNamespace(
        **{
            **cleanup_session.__dict__,
            "user_id": other_user,
            "session_id": other_session_id,
            "generation": other_generation,
        }
    )
    claims = VoiceControlClaims(
        subject=other_user,
        device_id=other_session.device_id,
        connection_generation=other_session.owner_connection_generation,
        binding_id=other_session.control_binding_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=4),
    )
    frame = VoiceLocalRecognitionStarted(
        device_id=other_session.device_id,
        connection_generation=other_session.owner_connection_generation,
        session_id=other_session.session_id,
        generation=other_session.generation,
        speech_revision=other_session.media_grant_revision,
        client_turn_id="00000000-0000-4000-8000-000000000043",
        chat_id=other_session.visible_chat_id,
        chat_context_revision=other_session.chat_context_revision,
        recognition_sequence=1,
    )
    turn = replace(
        _voice_turn(
            turn_id="00000000-0000-4000-8000-000000000033",
            client_turn_id=frame.client_turn_id,
            session_id=other_session.session_id,
        ),
        user_id=other_user,
        session_generation=other_generation,
    )
    abandonment_started = threading.Event()
    abandonment_release = threading.Event()

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return other_session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=False)

        def abandon_preacceptance_turns(self, **_kwargs: Any) -> None:
            abandonment_started.set()
            assert abandonment_release.wait(timeout=3)

    registry = ClientLocalBindingRegistry()
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=registry,
    )
    cleanup = asyncio.create_task(
        services.cleanup_local_buffers(cleanup_session)
    )
    assert await asyncio.to_thread(abandonment_started.wait, 1)
    returned_turn, authority = await services.bind_local_recognition(
        socket_id=7,
        current_socket_id=7,
        user_id=other_user,
        claims=claims,
        frame=frame,
        execution_base_render_revision=0,
        now=now,
    )
    assert returned_turn is turn
    abandonment_release.set()
    await cleanup
    assert registry.get_turn(
        user_id=other_user,
        client_turn_id=frame.client_turn_id,
        now=now,
    ) is authority
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    ["existing_authority", "new_mutation", "repository_replay"],
)
async def test_pre_durable_session_end_fence_settles_before_every_bind_return(
    shape: str,
) -> None:
    """The reversible pre-CAS fence wins before any later bind return proof."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                turn=turn,
                replayed=shape == "repository_replay",
            )

        def get_turn(self, **_kwargs: Any) -> Any:
            return turn

    class Services(VoiceServices):
        gate_returns = False

        async def _complete_existing_local_recognition_before_return(
            self,
            **kwargs: Any,
        ) -> None:
            if self.gate_returns:
                settlement_entered.set()
                await settlement_release.wait()
            await super()._complete_existing_local_recognition_before_return(
                **kwargs
            )

        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            if self.gate_returns:
                settlement_entered.set()
                await settlement_release.wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    kwargs = {
        "socket_id": 7,
        "current_socket_id": 7,
        "user_id": "user-a",
        "claims": claims,
        "frame": frame,
        "execution_base_render_revision": 0,
        "now": now,
    }
    if shape == "existing_authority":
        await services.bind_local_recognition(**kwargs)
    services.gate_returns = True
    bind = asyncio.create_task(services.bind_local_recognition(**kwargs))
    await asyncio.wait_for(settlement_entered.wait(), timeout=1)
    await services.pending_local_rejection_lock.acquire()
    prepared = asyncio.create_task(
        services.prepare_local_session_end(session)
    )
    try:
        await asyncio.sleep(0)
        settlement_release.set()
    finally:
        services.pending_local_rejection_lock.release()
    end_fence = await prepared
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await bind
    await services.resolve_local_session_end(end_fence, True)
    await services.handle_runtime_session_end(session, "user")
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}
    assert services.local_cleanup_epochs == {}
    assert services.local_cleanup_operations == {}
    assert services.local_end_fences == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_phase",
    ["cancellation", "finalize", "settlement"],
)
async def test_failed_end_reconciles_end_fenced_nonreplay_insert(
    failure_phase: str,
) -> None:
    """A failed CAS terminalizes every non-replay row deferred to its fence."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, frame = _local_recognition_context(now)
    turn = _voice_turn(client_turn_id=frame.client_turn_id)
    phase_entered = asyncio.Event()
    phase_release = asyncio.Event()
    rejected_turn_ids: list[str] = []

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=turn, replayed=False)

        def reject_transcript(self, **kwargs: Any) -> None:
            rejected_turn_ids.append(kwargs["turn_id"])

    class Registry(ClientLocalBindingRegistry):
        def finalize_turn(self, **kwargs: Any) -> Any:
            if failure_phase == "finalize":
                raise VoiceControlBindingError("invalid_binding")
            return super().finalize_turn(**kwargs)

    class Services(VoiceServices):
        async def _finish_local_recognition_mutation(
            self,
            request: Any,
            task: Any,
        ) -> None:
            if failure_phase == "finalize":
                phase_entered.set()
                await phase_release.wait()
            await super()._finish_local_recognition_mutation(request, task)

        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            if failure_phase != "finalize":
                phase_entered.set()
                await phase_release.wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=Registry(),
    )
    bind = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    await asyncio.wait_for(phase_entered.wait(), timeout=1)
    end_fence = await services.prepare_local_session_end(session)
    if failure_phase == "cancellation":
        bind.cancel()
        await asyncio.sleep(0)
        bind.cancel()
    phase_release.set()
    if failure_phase == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            await bind
    else:
        with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
            await bind

    assert rejected_turn_ids == []
    assert len(services.pending_local_rejections) == 1
    pending = next(iter(services.pending_local_rejections.values()))
    assert pending.end_fence is end_fence
    await services.resolve_local_session_end(end_fence, False)
    assert rejected_turn_ids == [turn.turn_id]
    assert services.pending_local_rejections == {}
    assert services.pending_local_rejection_reservations == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}

    ready = VoiceLocalReady(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=session.generation,
        speech_revision=session.media_grant_revision,
        client_sequence=2,
    )
    assert await services.local_ready(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=ready,
        now=now,
    ) is session
    await services.complete_local_ready_delivery(
        session,
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=ready,
        now=now,
        authority_is_current=lambda: True,
    )
    assert services.local_cleanup_epochs == {}
    assert services.local_end_fences == {}


@pytest.mark.asyncio
async def test_failed_end_rejects_promotion_during_reconciliation() -> None:
    """A bind promoted after the failed-end snapshot is not deferred to it."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, claims, first_frame = _local_recognition_context(now)
    second_frame = replace(
        first_frame,
        client_turn_id="00000000-0000-4000-8000-000000000043",
        recognition_sequence=2,
    )
    first_turn = _voice_turn(client_turn_id=first_frame.client_turn_id)
    second_turn = _voice_turn(
        turn_id="00000000-0000-4000-8000-000000000033",
        client_turn_id=second_frame.client_turn_id,
    )
    settlement_entered = {
        first_frame.client_turn_id: asyncio.Event(),
        second_frame.client_turn_id: asyncio.Event(),
    }
    settlement_release = {
        first_frame.client_turn_id: asyncio.Event(),
        second_frame.client_turn_id: asyncio.Event(),
    }
    first_rejection_started = threading.Event()
    first_rejection_release = threading.Event()
    rejected_turn_ids: list[str] = []

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def bind_recognition_turn(self, binding: Any, **_kwargs: Any) -> Any:
            turn = (
                first_turn
                if binding.client_turn_id == first_frame.client_turn_id
                else second_turn
            )
            return SimpleNamespace(turn=turn, replayed=False)

        def reject_transcript(self, **kwargs: Any) -> None:
            turn_id = kwargs["turn_id"]
            if turn_id == first_turn.turn_id:
                first_rejection_started.set()
                assert first_rejection_release.wait(timeout=3)
            rejected_turn_ids.append(turn_id)

    class Services(VoiceServices):
        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            client_turn_id = reserved.client_turn_id
            settlement_entered[client_turn_id].set()
            await settlement_release[client_turn_id].wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )

    def start_bind(frame: VoiceLocalRecognitionStarted) -> asyncio.Task[Any]:
        return asyncio.create_task(
            services.bind_local_recognition(
                socket_id=7,
                current_socket_id=7,
                user_id="user-a",
                claims=claims,
                frame=frame,
                execution_base_render_revision=0,
                now=now,
            )
        )

    first_bind = start_bind(first_frame)
    second_bind = start_bind(second_frame)
    await asyncio.wait_for(
        settlement_entered[first_frame.client_turn_id].wait(),
        timeout=1,
    )
    await asyncio.wait_for(
        settlement_entered[second_frame.client_turn_id].wait(),
        timeout=1,
    )
    end_fence = await services.prepare_local_session_end(session)

    settlement_release[first_frame.client_turn_id].set()
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await first_bind
    resolving = asyncio.create_task(
        services.resolve_local_session_end(end_fence, False)
    )
    assert await asyncio.to_thread(first_rejection_started.wait, 1)

    settlement_release[second_frame.client_turn_id].set()
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await second_bind
    rejected_while_first_blocked = tuple(rejected_turn_ids)

    first_rejection_release.set()
    await resolving
    assert rejected_while_first_blocked == (second_turn.turn_id,)
    assert set(rejected_turn_ids) == {first_turn.turn_id, second_turn.turn_id}
    assert services.pending_local_rejection_reservations == {}
    assert services.pending_local_rejections == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}

    ready = VoiceLocalReady(
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        session_id=session.session_id,
        generation=session.generation,
        speech_revision=session.media_grant_revision,
        client_sequence=2,
    )
    assert await services.local_ready(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=ready,
        now=now,
    ) is session
    await services.complete_local_ready_delivery(
        session,
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        frame=ready,
        now=now,
        authority_is_current=lambda: True,
    )
    assert services.local_cleanup_epochs == {}
    assert services.local_end_fences == {}


@pytest.mark.asyncio
async def test_logout_end_fence_precedes_mutation_and_joins_repeated_cancellation(
) -> None:
    """Identity teardown cannot outlive its exact reversible local fence."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session, _claims, _frame = _local_recognition_context(now)
    ended = SimpleNamespace(
        **{
            **session.__dict__,
            "ended_at": now,
            "end_reason": "logout",
        }
    )
    fence_published = asyncio.Event()
    fence_release = asyncio.Event()
    mutation_started = threading.Event()
    mutation_release = threading.Event()

    class Repository:
        def get_live_session(self, **_kwargs: Any) -> Any:
            return session

        def end_live_user_session(self, **_kwargs: Any) -> Any:
            mutation_started.set()
            assert mutation_release.wait(timeout=3)
            return ended

    class Services(VoiceServices):
        async def prepare_local_session_end(self, current: Any) -> Any:
            fence = await super().prepare_local_session_end(current)
            fence_published.set()
            await fence_release.wait()
            return fence

    media = SimpleNamespace(end=AsyncMock())
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=media,  # type: ignore[arg-type]
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    task = asyncio.create_task(
        services.end_user_voice_session(
            user_id="user-a",
            reason="logout",
        )
    )
    await asyncio.wait_for(fence_published.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not mutation_started.is_set()
    fence_release.set()
    assert await asyncio.to_thread(mutation_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    mutation_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    media.end.assert_awaited_once_with(ended, "logout")
    assert services.local_cleanup_epochs == {}
    assert services.local_end_fences == {}
    assert services.local_recognition_requests == set()
    assert services.local_recognition_keys == {}


@pytest.mark.asyncio
async def test_logout_retries_exact_generation_after_takeover_race() -> None:
    """Logout fences B and revokes its blocked bind before ending B."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    first, _claims, _frame = _local_recognition_context(now)
    replacement = SimpleNamespace(
        **{
            **first.__dict__,
            "session_id": "00000000-0000-4000-8000-000000000039",
            "generation": 2,
            "media_grant_revision": 2,
        }
    )
    ended_first = SimpleNamespace(
        **{
            **first.__dict__,
            "ended_at": now,
            "end_reason": "takeover",
        }
    )
    ended_replacement = SimpleNamespace(
        **{
            **replacement.__dict__,
            "ended_at": now,
            "end_reason": "logout",
        }
    )
    replacement_claims = VoiceControlClaims(
        subject="user-a",
        device_id=replacement.device_id,
        connection_generation=replacement.owner_connection_generation,
        binding_id=replacement.control_binding_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=4),
    )
    replacement_frame = VoiceLocalRecognitionStarted(
        device_id=replacement.device_id,
        connection_generation=replacement.owner_connection_generation,
        session_id=replacement.session_id,
        generation=replacement.generation,
        speech_revision=replacement.media_grant_revision,
        client_turn_id="00000000-0000-4000-8000-000000000049",
        chat_id=replacement.visible_chat_id,
        chat_context_revision=replacement.chat_context_revision,
        recognition_sequence=1,
    )
    replacement_turn = replace(
        _voice_turn(
            turn_id="00000000-0000-4000-8000-000000000039",
            client_turn_id=replacement_frame.client_turn_id,
            session_id=replacement.session_id,
            media_grant_revision=replacement.media_grant_revision,
        ),
        session_generation=replacement.generation,
    )
    live_reads = 0
    prepared: list[tuple[str, int]] = []
    end_attempts: list[tuple[str, int]] = []
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()
    replacement_fence_published = asyncio.Event()
    replacement_end_started = threading.Event()
    replacement_end_release = threading.Event()

    class Repository:
        def get_live_session(self, **_kwargs: Any) -> Any:
            nonlocal live_reads
            live_reads += 1
            return first if live_reads == 1 else replacement

        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return replacement

        def bind_recognition_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=replacement_turn, replayed=False)

        def reject_transcript(self, **_kwargs: Any) -> None:
            raise AssertionError("durable end owns replacement abandonment")

        def end_live_user_session(self, **kwargs: Any) -> Any:
            key = (
                kwargs["expected_session_id"],
                kwargs["expected_generation"],
            )
            assert key in prepared
            end_attempts.append(key)
            if len(end_attempts) == 1:
                raise VoiceSessionRepositoryError("stale_generation")
            replacement_end_started.set()
            assert replacement_end_release.wait(timeout=3)
            return ended_replacement

    class Services(VoiceServices):
        async def prepare_local_session_end(self, current: Any) -> Any:
            fence = await super().prepare_local_session_end(current)
            prepared.append((current.session_id, current.generation))
            if current.session_id == replacement.session_id:
                replacement_fence_published.set()
            return fence

        async def _acquire_pending_local_rejection_release(
            self,
            reserved: Any,
        ) -> bool:
            settlement_entered.set()
            await settlement_release.wait()
            return await super()._acquire_pending_local_rejection_release(
                reserved
            )

    media = SimpleNamespace(end=AsyncMock())
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=media,  # type: ignore[arg-type]
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    bind = asyncio.create_task(
        services.bind_local_recognition(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=replacement_claims,
            frame=replacement_frame,
            execution_base_render_revision=0,
            now=now,
        )
    )
    await asyncio.wait_for(settlement_entered.wait(), timeout=1)
    logout = asyncio.create_task(
        services.end_user_voice_session(
            user_id="user-a",
            reason="logout",
        )
    )
    await asyncio.wait_for(replacement_fence_published.wait(), timeout=1)
    assert await asyncio.to_thread(replacement_end_started.wait, 1)
    settlement_release.set()
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await bind
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        services.local_bindings.get_turn(
            user_id="user-a",
            client_turn_id=replacement_frame.client_turn_id,
            now=now,
        )
    replacement_end_release.set()
    assert await logout is ended_replacement
    assert prepared == [
        (first.session_id, 1),
        (replacement.session_id, 2),
    ]
    assert end_attempts == prepared
    media.end.assert_awaited_once_with(ended_replacement, "logout")

    # Both the eager stale proof and a repeated durable callback are idempotent.
    await services.handle_runtime_session_end(ended_first, "takeover")
    assert services.local_cleanup_epochs == {}
    assert services.local_end_fences == {}


@pytest.mark.asyncio
async def test_logout_repeated_cancellation_cannot_escape_between_retries(
) -> None:
    """Caller cancellation cannot strand the replacement after stale A."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    first, _claims, _frame = _local_recognition_context(now)
    replacement = SimpleNamespace(
        **{
            **first.__dict__,
            "session_id": "00000000-0000-4000-8000-000000000039",
            "generation": 2,
            "media_grant_revision": 2,
        }
    )
    ended = SimpleNamespace(
        **{
            **replacement.__dict__,
            "ended_at": now,
            "end_reason": "logout",
        }
    )
    live_reads = 0
    end_attempts: list[tuple[str, int]] = []
    between_attempts = asyncio.Event()
    retry_release = asyncio.Event()

    class Repository:
        def get_live_session(self, **_kwargs: Any) -> Any:
            nonlocal live_reads
            live_reads += 1
            return first if live_reads == 1 else replacement

        def end_live_user_session(self, **kwargs: Any) -> Any:
            end_attempts.append(
                (
                    kwargs["expected_session_id"],
                    kwargs["expected_generation"],
                )
            )
            if len(end_attempts) == 1:
                raise VoiceSessionRepositoryError("stale_generation")
            return ended

    class Services(VoiceServices):
        async def _fence_ended_local_session(self, current: Any) -> None:
            await super()._fence_ended_local_session(current)
            if current.session_id == first.session_id:
                between_attempts.set()
                await retry_release.wait()

    media = SimpleNamespace(end=AsyncMock())
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=media,  # type: ignore[arg-type]
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    logout = asyncio.create_task(
        services.end_user_voice_session(
            user_id="user-a",
            reason="logout",
        )
    )
    await asyncio.wait_for(between_attempts.wait(), timeout=1)
    logout.cancel()
    await asyncio.sleep(0)
    logout.cancel()
    await asyncio.sleep(0)
    assert not logout.done()
    retry_release.set()
    with pytest.raises(asyncio.CancelledError):
        await logout
    assert end_attempts == [
        (first.session_id, first.generation),
        (replacement.session_id, replacement.generation),
    ]
    media.end.assert_awaited_once_with(ended, "logout")
    assert services.local_cleanup_epochs == {}
    assert services.local_end_fences == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["last_admitted", "exhausted"])
async def test_logout_identity_retry_bound_is_activation_capacity_plus_one(
    outcome: str,
) -> None:
    """The admitted takeover bound is drained or fails closed without leaks."""

    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    base, _claims, _frame = _local_recognition_context(now)
    session_count = _MAX_LOCAL_IDENTITY_END_ATTEMPTS + (
        1 if outcome == "exhausted" else 0
    )
    sessions = [
        SimpleNamespace(
            **{
                **base.__dict__,
                "session_id": (
                    f"00000000-0000-4000-8001-{index:012x}"
                ),
                "generation": index,
                "media_grant_revision": index,
            }
        )
        for index in range(1, session_count + 1)
    ]
    ended = SimpleNamespace(
        **{
            **sessions[_MAX_LOCAL_IDENTITY_END_ATTEMPTS - 1].__dict__,
            "ended_at": now,
            "end_reason": "logout",
        }
    )
    attempts = 0
    maximum_retained_epochs = 0

    class Repository:
        def get_live_session(self, **_kwargs: Any) -> Any:
            return sessions[attempts]

        def end_live_user_session(self, **kwargs: Any) -> Any:
            nonlocal attempts
            current = sessions[attempts]
            assert kwargs["expected_session_id"] == current.session_id
            assert kwargs["expected_generation"] == current.generation
            attempts += 1
            if (
                outcome == "last_admitted"
                and attempts == _MAX_LOCAL_IDENTITY_END_ATTEMPTS
            ):
                return ended
            raise VoiceSessionRepositoryError("stale_generation")

    class Services(VoiceServices):
        async def prepare_local_session_end(self, current: Any) -> Any:
            nonlocal maximum_retained_epochs
            fence = await super().prepare_local_session_end(current)
            maximum_retained_epochs = max(
                maximum_retained_epochs,
                len(self.local_cleanup_epochs),
            )
            return fence

    media = SimpleNamespace(end=AsyncMock())
    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=media,  # type: ignore[arg-type]
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=ClientLocalBindingRegistry(),
    )
    if outcome == "last_admitted":
        assert await services.end_user_voice_session(
            user_id="user-a",
            reason="logout",
        ) is ended
        media.end.assert_awaited_once_with(ended, "logout")
    else:
        with pytest.raises(
            VoiceSessionRepositoryError,
            match="stale_generation",
        ):
            await services.end_user_voice_session(
                user_id="user-a",
                reason="logout",
            )
        media.end.assert_not_awaited()
    assert attempts == _MAX_LOCAL_IDENTITY_END_ATTEMPTS
    assert maximum_retained_epochs == 1
    assert services.local_cleanup_epochs == {}
    assert services.local_end_fences == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,lease_delta", [("replica-b", 30), ("voice-coordinator-local-1", -1)])
async def test_local_ready_requires_this_replica_and_live_control_lease(
    owner: str,
    lease_delta: int,
) -> None:
    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session = SimpleNamespace(
        speech_backend="client_local",
        state="active",
        ended_at=None,
        control_owner_id=owner,
        control_lease_expires_at=now + timedelta(seconds=lease_delta),
        lease_expires_at=now + timedelta(minutes=1),
    )
    bindings = SimpleNamespace(authorize_ready=Mock())
    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=SimpleNamespace(get_controlled_session=Mock(return_value=session)),
        coordinator=None,
        capability=_EnabledLocalCapability(),
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_bindings=bindings,
    )
    claims = SimpleNamespace(
        device_id="00000000-0000-4000-8000-000000000021",
        connection_generation="00000000-0000-4000-8000-000000000022",
        binding_id="00000000-0000-4000-8000-000000000023",
        expires_at=now + timedelta(minutes=1),
    )
    frame = SimpleNamespace(session_id="session", generation=1, speech_revision=1)
    with pytest.raises(VoiceBootstrapError, match="local_control_unavailable"):
        await services.local_ready(
            socket_id=7,
            current_socket_id=7,
            user_id="user-a",
            claims=claims,
            frame=frame,
            now=now,
        )
    bindings.authorize_ready.assert_not_called()

@pytest.mark.asyncio
async def test_client_local_bootstrap_constructs_no_remote_dependency() -> None:
    plane = _PlaneRuntime()
    services = build_voice_services(
        plane_runtime=plane,
        plane_repositories=plane.repositories,
        environ={
            "ASTRAL_ENV": "development",
            "VOICE_SPEECH_BACKEND": "client_local",
        },
    )
    assert services.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
    assert services.worker_pool is None
    assert services.livekit is None
    assert services.worker_control_settings is None
    snapshot = await services.capability.readiness()
    assert snapshot.to_dict()["requirements"]["max_announcement_utf8_bytes"] == 600
    for operation in (
        services.media.apply_context,
        services.media.set_capture,
        services.media.barge_in,
        services.media.stop_speech,
        services.media.end,
        services.media.abort,
        services.media.current_session,
    ):
        if operation.__name__ == "set_capture":
            assert await operation(SimpleNamespace(), True) is None
        elif operation.__name__ == "end":
            assert await operation(SimpleNamespace(), "ended") is None
        elif operation.__name__ == "current_session":
            assert await operation("session", 1) is None
        else:
            assert await operation(SimpleNamespace()) is None


class _RunnerClock:
    def __init__(self) -> None:
        self.utc = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        self.mono = 100.0

    def coordinator_clock(self) -> CoordinatorClock:
        return CoordinatorClock(utcnow=lambda: self.utc, monotonic=lambda: self.mono)

    def advance(self, seconds: float) -> None:
        self.utc += timedelta(seconds=seconds)
        self.mono += seconds


class _RunnerRepository:
    def __init__(self, *turns: VoiceTurnRecord) -> None:
        self.turns = {turn.turn_id: turn for turn in turns}
        self.idle_updates: list[dict[str, object]] = []

    def get_turn(self, *, user_id: str, turn_id: str) -> VoiceTurnRecord:
        turn = self.turns[turn_id]
        assert turn.user_id == user_id
        return turn

    def set_true_idle(self, **kwargs):
        self.idle_updates.append(kwargs)
        return SimpleNamespace()

    def apply_claim(self, turn_id: str, state: AnnouncementState, now: datetime) -> None:
        turn = self.turns[turn_id]
        next_due_at = (
            now + timedelta(seconds=14)
            if state.last_announcement_kind in {"acknowledgement", "progress"}
            else None
        )
        self.turns[turn_id] = replace(
            turn,
            announcement_sequence=state.announcement_sequence,
            result_reserved_samples=state.result_reserved_samples,
            result_quantum_count=state.result_quantum_count,
            last_phrase_key=state.last_phrase_key,
            next_announcement_due_at=next_due_at,
            updated_at=now,
        )

    def terminalize_turn(
        self,
        *,
        user_id: str,
        turn_id: str,
        terminal_kind: str,
        result_commit_id: str | None,
        recap_source: str,
        sensitivity: str,
        now: datetime,
    ):
        turn = self.get_turn(user_id=user_id, turn_id=turn_id)
        terminal = replace(
            turn,
            state=terminal_kind,
            is_foreground=False,
            terminal_kind=terminal_kind,
            result_commit_id=result_commit_id,
            recap_source=recap_source,
            sensitivity=sensitivity,
            next_announcement_due_at=None,
            terminal_at=now,
            updated_at=now,
        )
        self.turns[turn_id] = terminal
        return SimpleNamespace(turn=terminal, replayed=False)


class _RunnerCoordinator:
    def __init__(self, repository: _RunnerRepository, clock: _RunnerClock) -> None:
        self.repository = repository
        self.clock = clock
        self.adapter = AnnouncementStateAdapter(PhraseBook(APPROVED_PHRASE_KEYS))
        self.states: dict[str, AnnouncementState] = {}

    async def claim_turn_announcement(self, *, user_id, request):
        turn = self.repository.get_turn(user_id=user_id, turn_id=request.turn_id)
        state = self.states.setdefault(
            request.turn_id,
            AnnouncementState(
                generation=turn.session_generation,
                announcement_sequence=turn.announcement_sequence,
                result_reserved_samples=turn.result_reserved_samples,
                result_quantum_count=turn.result_quantum_count,
                last_phrase_key=turn.last_phrase_key,
            ),
        )
        state = replace(
            state,
            terminal=(
                turn.state
                in {"succeeded", "failed", "refused", "cancelled", "abandoned"}
                and not request.authorized_terminal_sensitive_recap
                and request.authorized_preacceptance_rejection_reason is None
            ),
        )
        mutation = self.adapter.claim(state, request, now=self.clock.utc)
        self.states[request.turn_id] = mutation.state
        self.repository.apply_claim(request.turn_id, mutation.state, self.clock.utc)
        return mutation

    async def complete_turn_announcement(
        self,
        *,
        user_id,
        session_id,
        turn_id,
        generation,
        claim_id,
    ):
        turn = self.repository.get_turn(user_id=user_id, turn_id=turn_id)
        assert (turn.session_id, turn.session_generation) == (session_id, generation)
        state = self.states[turn_id]
        self.states[turn_id] = self.adapter.complete(
            state,
            generation=generation,
            claim_id=claim_id,
        )
        return True


class _RunnerMedia:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.calls: list[dict[str, object]] = []
        self.futures: dict[str, asyncio.Future[str]] = {}
        self.active_announcement_id: str | None = None
        self.stops = 0
        self.stop_kinds: list[str] = []

    async def speak_turn(self, turn, claim, **kwargs) -> None:
        if self.active_announcement_id is not None:
            raise AssertionError("physical speech streams overlapped")
        future = asyncio.get_running_loop().create_future()
        self.active_announcement_id = claim.announcement_id
        self.futures[claim.announcement_id] = future
        self.calls.append(
            {
                "turn_id": turn.turn_id,
                "kind": claim.kind,
                "text": kwargs["text"],
                "announcement_id": claim.announcement_id,
            }
        )

    async def await_speech_terminal(self, announcement_id, **_kwargs):
        return await self.futures[announcement_id]

    async def current_session(self, session_id, generation):
        if session_id == self.session_id and generation == 1:
            return SimpleNamespace(
                session_id=session_id,
                generation=generation,
                media_grant_revision=2,
                state="active",
                speech_muted=False,
                device_kind="web",
                transport="livekit",
            )
        return None

    async def stop_speech(self, _session) -> None:
        self.stops += 1
        self.stop_kinds.append("normal")
        self.finish("speech_interrupted")

    async def barge_in(self, _session) -> None:
        self.stops += 1
        self.stop_kinds.append("barge_in")
        self.finish("speech_interrupted")

    def finish(self, status: str = "speech_finished") -> None:
        announcement_id = self.active_announcement_id
        if announcement_id is None:
            raise AssertionError("no active speech to finish")
        self.active_announcement_id = None
        future = self.futures[announcement_id]
        if not future.done():
            future.set_result(status)


def _runner_services(
    *turns: VoiceTurnRecord,
    observability: RuntimeObservability | None = None,
) -> tuple[VoiceServices, _RunnerClock, _RunnerRepository, _RunnerMedia]:
    clock = _RunnerClock()
    repository = _RunnerRepository(*turns)
    media = _RunnerMedia(turns[0].session_id)
    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        coordinator=_RunnerCoordinator(repository, clock),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=media,  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
        observability=observability,
        announcement_clock_factory=clock.coordinator_clock,
    )
    return services, clock, repository, media


async def _eventually(predicate) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


def _environment(**changes: str) -> dict[str, str]:
    values = {
        "ASTRAL_ENV": "development",
        "LIVEKIT_INTERNAL_URL": "http://livekit:7880",
        "LIVEKIT_PUBLIC_URL": "ws://localhost:7880",
        "LIVEKIT_API_KEY": "local-api-key",
        "LIVEKIT_API_SECRET": "local-api-secret-that-is-long-enough-for-tests",
        "VOICE_WORKER_CLOSURE_SHA256": "0" * 64,
        "VOICE_CONTROL_SECRET": "voice-control-secret-that-is-long-enough-for-tests",
    }
    values.update(changes)
    return values


def _route_paths(owner: object) -> list[str]:
    paths: list[str] = []
    pending = [owner]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        for route in getattr(current, "routes", ()):
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.append(path)
            original_router = getattr(route, "original_router", None)
            if original_router is not None:
                pending.append(original_router)
    return paths


def test_sensitive_recap_is_split_into_at_most_seven_four_second_quanta() -> None:
    quanta = _sensitive_result_quanta(
        " ".join(f"word{index}" for index in range(80))
    )
    assert len(quanta) == 7
    assert sum(len(item.split()) for item in quanta) == 63
    assert all(1 <= len(item.split()) <= 9 for item in quanta)


def test_development_bootstrap_builds_isolated_runtime_without_opening_network() -> None:
    services = build_voice_services(
        plane_runtime=_PlaneRuntime(), environ=_environment()
    )
    assert services.runtime is not None
    assert services.worker_pool.readiness().reason == "worker_unavailable"
    assert "api-secret" not in repr(services)


def test_worker_control_route_is_present_once_only_for_built_services() -> None:
    app = FastAPI()
    services = build_voice_services(
        plane_runtime=_PlaneRuntime(), environ=_environment()
    )

    first = install_voice_worker_control(app, services, environ=_environment())
    second = install_voice_worker_control(app, services, environ=_environment())

    assert first is second
    assert services.worker_endpoint is first
    assert first._disconnect_hook == services.handle_worker_disconnect
    assert _route_paths(app).count(WORKER_CONTROL_PATH) == 1


@pytest.mark.asyncio
async def test_worker_disconnect_filters_reassigned_sessions_before_runtime_cleanup() -> None:
    released = "00000000-0000-4000-8000-000000000001"
    reassigned = "00000000-0000-4000-8000-000000000002"
    assignment = "00000000-0000-4000-8000-000000000003"
    calls: list[dict[str, object]] = []

    class Pool:
        def assignment_snapshot(self, session_id: str):
            if session_id == reassigned:
                return SimpleNamespace(assignment_id="newer-assignment")
            raise StaleFence("stale_assignment")

    class Runtime:
        async def reconcile_worker_disconnect(self, **kwargs):
            assignment_is_current = kwargs.pop("assignment_is_current")
            calls.append(
                {
                    **kwargs,
                    "reassigned_is_current": assignment_is_current(reassigned),
                    "released_is_current": assignment_is_current(released),
                }
            )
            return ()

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=Pool(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
        coordinator=object(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=object(),  # type: ignore[arg-type]
        runtime=Runtime(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
    )
    receipt = WorkerRegistrationReceipt(
        connection_id="00000000-0000-4000-8000-000000000004",
        worker_identity="voice-worker-a",
        accepted_max_sessions=2,
        fenced_assignments=(assignment,),
    )

    await services.handle_worker_disconnect(receipt, (reassigned, released))

    assert calls == [
        {
            "worker_identity": "voice-worker-a",
            "released_session_ids": (released, reassigned),
            "released_assignment_ids": (assignment,),
            "reassigned_is_current": True,
            "released_is_current": False,
        }
    ]


@pytest.mark.asyncio
async def test_service_lifecycle_end_releases_runtime_assignment_fence() -> None:
    released: list[tuple[str, int]] = []
    session = SimpleNamespace(
        session_id="00000000-0000-4000-8000-000000000001",
        generation=3,
    )

    class Runtime:
        def release_worker_assignment_fence(self, ended) -> None:
            released.append((ended.session_id, ended.generation))

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
        coordinator=object(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=object(),  # type: ignore[arg-type]
        runtime=Runtime(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
    )

    await services.handle_runtime_session_end(session, "media_error")

    assert released == [(session.session_id, 3)]


def test_worker_control_route_stays_absent_without_built_services() -> None:
    app = FastAPI()

    assert install_voice_worker_control(app, None, environ=_environment()) is None
    assert WORKER_CONTROL_PATH not in _route_paths(app)


def test_production_startup_contains_one_root_app_worker_mount() -> None:
    source = (Path(__file__).resolve().parents[1] / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    assert source.count("self.voice_worker_endpoint = install_voice_worker_control(") == 1
    assert "app,\n                self.voice_services," in source


def test_worker_control_route_collision_fails_closed() -> None:
    app = FastAPI()

    @app.websocket(WORKER_CONTROL_PATH)
    async def conflicting_route(_websocket):
        return None

    services = build_voice_services(
        plane_runtime=_PlaneRuntime(), environ=_environment()
    )
    with pytest.raises(WorkerControlConfigError, match="worker_control_route_conflict"):
        install_voice_worker_control(app, services, environ=_environment())


def test_included_router_worker_control_collision_fails_closed() -> None:
    app = FastAPI()
    router = APIRouter()

    @router.websocket(WORKER_CONTROL_PATH)
    async def conflicting_route(_websocket):
        return None

    app.include_router(router)
    services = build_voice_services(
        plane_runtime=_PlaneRuntime(), environ=_environment()
    )
    with pytest.raises(WorkerControlConfigError, match="worker_control_route_conflict"):
        install_voice_worker_control(app, services, environ=_environment())


@pytest.mark.asyncio
async def test_voice_shutdown_fences_workers_before_livekit_close() -> None:
    events: list[str] = []
    session = SimpleNamespace(
        session_id="00000000-0000-4000-8000-000000000001",
        generation=1,
        end_reason="shutdown",
    )

    class Repository:
        def end_owned_sessions(self, *, owner_id, reason, now):
            assert owner_id == "voice-replica-test"
            assert reason == "shutdown"
            assert now.tzinfo is not None
            events.append("durable")
            return (session,)

    class Coordinator:
        replica_id = "voice-replica-test"

    class Media:
        async def end(self, ended, reason) -> None:
            assert ended is session
            assert reason == "shutdown"
            events.append("media")

    class Pool:
        async def shutdown(self) -> tuple[str, ...]:
            events.append("workers")
            return ()

    class LiveKit:
        async def close(self) -> None:
            events.append("livekit")

    services = VoiceServices(
        livekit=LiveKit(),  # type: ignore[arg-type]
        worker_pool=Pool(),  # type: ignore[arg-type]
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=Coordinator(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
        speech_backend=VoiceSpeechBackend.LLM_FACTORY,
    )

    await services.close()

    assert events == ["durable", "media", "workers", "livekit"]


@pytest.mark.asyncio
async def test_current_chat_unavailable_closes_exact_media_and_replay_is_empty() -> None:
    session = SimpleNamespace(
        session_id="00000000-0000-4000-8000-000000000081",
        generation=3,
        end_reason="chat_deleted",
    )
    media_calls: list[tuple[str, int, str]] = []

    class Media:
        async def end(self, ended, reason) -> None:
            media_calls.append((ended.session_id, ended.generation, reason))

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
        coordinator=object(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
    )
    mutation = ChatUnavailableMutation(
        user_id="user-a",
        chat_id="00000000-0000-4000-8000-000000000082",
        reason="deleted",
        chat_deleted=True,
        replayed=False,
        ended_sessions=(session,),  # type: ignore[arg-type]
        announcement_session_keys=((session.session_id, session.generation),),
        unaccepted_turn_ids=(),
        accepted_turn_ids=(),
        aborted_result_commit_ids=(),
    )

    await services.handle_chat_unavailable(mutation)
    await services.handle_chat_unavailable(
        replace(
            mutation,
            chat_deleted=False,
            replayed=True,
            ended_sessions=(),
            announcement_session_keys=(),
        )
    )

    assert media_calls == [(session.session_id, 3, "chat_deleted")]


@pytest.mark.asyncio
async def test_voice_maintenance_ends_only_expired_media_generations() -> None:
    events: list[tuple[str, str]] = []
    renewals: list[tuple[str, bool]] = []
    terminal_repairs: list[bool] = []
    notified_turns: list[str] = []

    class Session:
        def __init__(self, session_id: str, generation: int, reason: str) -> None:
            self.session_id = session_id
            self.generation = generation
            self.end_reason = reason

    lease = Session("lease-session", 1, "lease_expired")
    idle = Session("idle-session", 2, "idle")
    repairs = (
        SimpleNamespace(turn_id="repair-turn-1"),
        SimpleNamespace(turn_id="repair-turn-2"),
    )

    class Repository:
        def renew_owned_control_leases(self, *, owner_id, now):
            renewals.append((owner_id, now.tzinfo is not None))
            return ()

        def expire_session_leases(self, *, now):
            assert now.tzinfo is not None
            return (lease,)

        def expire_true_idle(self, *, now):
            assert now.tzinfo is not None
            return (idle,)

        def reconcile_ended_unaccepted_turns(self, *, now):
            assert now.tzinfo is not None
            return ()

        def reconcile_ended_terminal_operation_turns(self, *, now):
            terminal_repairs.append(now.tzinfo is not None)
            return repairs

    class Media:
        async def end(self, session, reason: str) -> None:
            events.append((session.session_id, reason))

    class Coordinator:
        replica_id = "voice-replica-test"

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=Coordinator(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
    )

    async def notify(turn) -> None:
        notified_turns.append(turn.turn_id)

    with pytest.raises(TypeError, match="terminal turn notifier must be callable"):
        services.bind_terminal_turn_notifier(None)  # type: ignore[arg-type]
    services.bind_terminal_turn_notifier(notify)
    with pytest.raises(RuntimeError, match="terminal_turn_notifier_already_bound"):
        services.bind_terminal_turn_notifier(notify)

    await services._sweep_sessions()

    assert renewals == [("voice-replica-test", True)]
    assert terminal_repairs == [True]
    assert notified_turns == ["repair-turn-1", "repair-turn-2"]
    assert events == [
        ("lease-session", "lease_expired"),
        ("idle-session", "idle"),
    ]


@pytest.mark.asyncio
async def test_voice_maintenance_skips_cross_backend_media_cleanup() -> None:
    media_ends: list[str] = []
    local = SimpleNamespace(
        user_id="user-a",
        session_id="local-ended",
        generation=1,
        end_reason="lease_expired",
        speech_backend="client_local",
    )
    remote = SimpleNamespace(
        user_id="user-a",
        session_id="remote-ended",
        generation=1,
        end_reason="lease_expired",
        speech_backend="llm_factory",
    )

    class Repository:
        def renew_owned_control_leases(self, **_kwargs):
            return ()

        def expire_session_leases(self, **_kwargs):
            return (local, remote)

        def expire_true_idle(self, **_kwargs):
            return ()

        def reconcile_ended_unaccepted_turns(self, **_kwargs):
            return ()

        def reconcile_ended_terminal_operation_turns(self, **_kwargs):
            return ()

    class Media:
        async def end(self, session, _reason: str) -> None:
            media_ends.append(session.session_id)

    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=SimpleNamespace(replica_id="voice-replica-test"),
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
    )

    await services._sweep_sessions()

    assert media_ends == ["local-ended"]


@pytest.mark.asyncio
async def test_local_process_logout_drains_remote_row_without_local_fence() -> None:
    remote = SimpleNamespace(
        user_id="user-a",
        session_id="remote-live",
        generation=3,
        end_reason=None,
        speech_backend="llm_factory",
    )
    ended = SimpleNamespace(**{**remote.__dict__, "end_reason": "logout"})
    end_calls: list[dict[str, object]] = []
    media_ends: list[str] = []

    class Repository:
        def get_live_session(self, *, user_id):
            assert user_id == "user-a"
            return remote

        def end_live_user_session(self, **kwargs):
            end_calls.append(kwargs)
            return ended

    class Media:
        async def end(self, session, _reason: str) -> None:
            media_ends.append(session.session_id)

    class Services(VoiceServices):
        async def prepare_local_session_end(self, _session):
            raise AssertionError("remote row received local end fence")

    services = Services(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=SimpleNamespace(replica_id="voice-replica-test"),
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(release_worker_assignment_fence=lambda _session: None),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
    )

    assert await services.end_user_voice_session(
        user_id="user-a",
        reason="logout",
    ) is ended

    assert len(end_calls) == 1
    assert end_calls[0]["expected_session_id"] == remote.session_id
    assert end_calls[0]["expected_generation"] == remote.generation
    assert media_ends == []


@pytest.mark.asyncio
async def test_voice_maintenance_isolates_each_terminal_notifier_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    notified_turns: list[str] = []
    media_ends: list[str] = []
    ended = SimpleNamespace(
        session_id="ended-session",
        generation=1,
        end_reason="lease_expired",
    )
    repairs = (
        SimpleNamespace(turn_id="repair-turn-1"),
        SimpleNamespace(turn_id="repair-turn-2"),
    )

    class Repository:
        def renew_owned_control_leases(self, *, owner_id, now):
            return ()

        def expire_session_leases(self, *, now):
            return (ended,)

        def expire_true_idle(self, *, now):
            return ()

        def reconcile_ended_unaccepted_turns(self, *, now):
            return ()

        def reconcile_ended_terminal_operation_turns(self, *, now):
            return repairs

    class Media:
        async def end(self, session, reason: str) -> None:
            media_ends.append(session.session_id)

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=SimpleNamespace(replica_id="voice-replica-test"),
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
    )

    async def failing_notifier(turn) -> None:
        notified_turns.append(turn.turn_id)
        raise RuntimeError("untrusted notifier detail must not be logged")

    services.bind_terminal_turn_notifier(failing_notifier)
    with caplog.at_level("WARNING"):
        await services._sweep_sessions()

    assert notified_turns == ["repair-turn-1", "repair-turn-2"]
    assert media_ends == ["ended-session"]
    matching = [
        record.getMessage()
        for record in caplog.records
        if "voice_terminal_turn_notification_unavailable" in record.getMessage()
    ]
    assert matching == [
        "voice_terminal_turn_notification_unavailable reason=internal_error",
        "voice_terminal_turn_notification_unavailable reason=internal_error",
    ]
    assert "untrusted notifier detail" not in caplog.text


@pytest.mark.asyncio
async def test_authenticated_worker_state_drives_server_owned_true_idle() -> None:
    updates: list[dict[str, object]] = []
    metrics = RuntimeObservability(deployment_instance="test")
    session = SimpleNamespace(
        user_id="user-a",
        session_id="00000000-0000-4000-8000-000000000001",
        generation=1,
        foreground_active=True,
        microphone_enabled=True,
        device_kind="web",
        transport="livekit",
    )

    class Repository:
        def set_true_idle(self, **kwargs):
            updates.append(kwargs)
            return session

    class Media:
        async def handle_worker_frame(self, frame) -> None:
            assert frame["session_id"] == session.session_id

        async def current_session(self, session_id: str, generation: int):
            if (session_id, generation) == (session.session_id, 1):
                return session
            return None

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=object(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
        observability=metrics,
    )
    base = {
        "session_id": session.session_id,
        "generation": 1,
    }

    await services.handle_worker_frame(
        None,  # type: ignore[arg-type]
        {**base, "type": "media_state", "state": "listening"},
    )
    await services.handle_worker_frame(
        None,  # type: ignore[arg-type]
        {**base, "type": "speech_started"},
    )
    await services.handle_worker_frame(
        None,  # type: ignore[arg-type]
        {**base, "type": "speech_finished"},
    )
    await services.handle_worker_frame(
        None,  # type: ignore[arg-type]
        {**base, "type": "media_state", "state": "listening"},
    )

    assert [item["listening"] for item in updates] == [True, False, False, True]
    assert all(item["user_input_gate"] is False for item in updates)
    assert all(item["now"].tzinfo is not None for item in updates)
    assert (session.session_id, 1) in services.listening_sessions
    assert {sample.name for sample in metrics.snapshot()} == {
        "voice_state_transition_total",
        "voice_tts_total",
    }


@pytest.mark.parametrize(
    ("frame_type", "terminal_state", "media_effect_fails"),
    (
        ("media_state", "failed", True),
        ("heartbeat", "ended", False),
        ("worker_ready", "failed", False),
    ),
)
@pytest.mark.asyncio
async def test_authenticated_terminal_worker_frame_releases_and_reconciles_once(
    frame_type: str,
    terminal_state: str,
    media_effect_fails: bool,
) -> None:
    session_id = "00000000-0000-4000-8000-000000000001"
    connection_id = "00000000-0000-4000-8000-000000000002"
    assignment_id = "00000000-0000-4000-8000-000000000003"
    releases: list[dict[str, object]] = []
    reconciliations: list[dict[str, object]] = []
    media_frames: list[str] = []
    released_once = False

    release = SimpleNamespace(
        connection_id=connection_id,
        worker_identity="voice-worker-a",
        accepted_max_sessions=2,
        session_id=session_id,
        assignment_id=assignment_id,
        worker_rtc_grant_revision=4,
        terminal_state=terminal_state,
    )

    class Pool:
        async def release_terminal_assignment(self, **kwargs):
            nonlocal released_once
            releases.append(kwargs)
            if released_once:
                return None
            released_once = True
            return release

        def assignment_snapshot(self, _session_id: str):
            raise StaleFence("stale_assignment")

    class Runtime:
        async def reconcile_worker_disconnect(self, **kwargs):
            assignment_is_current = kwargs.pop("assignment_is_current")
            assert assignment_is_current(session_id) is False
            reconciliations.append(kwargs)
            return ()

    session = SimpleNamespace(
        user_id="user-a",
        session_id=session_id,
        generation=1,
        foreground_active=True,
        microphone_enabled=True,
        device_kind="web",
        transport="livekit",
    )

    class Media:
        async def handle_worker_frame(self, frame) -> None:
            media_frames.append(frame["type"])
            if media_effect_fails:
                raise RuntimeError("local media state unavailable")

        async def current_session(self, current_id: str, generation: int):
            if (current_id, generation) == (session_id, 1):
                return session
            return None

    class Repository:
        def set_true_idle(self, **_kwargs):
            return session

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=Pool(),  # type: ignore[arg-type]
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=object(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=Runtime(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
    )
    receipt = WorkerRegistrationReceipt(
        connection_id=connection_id,
        worker_identity="voice-worker-a",
        accepted_max_sessions=2,
    )
    frame: dict[str, object] = {
        "type": frame_type,
        "session_id": session_id,
        "generation": 1,
    }
    if frame_type == "media_state":
        frame["state"] = terminal_state
    elif frame_type == "heartbeat":
        frame["media_state"] = terminal_state
    else:
        frame["profile_ready"] = False

    await services.handle_worker_frame(receipt, frame)
    await services.handle_worker_frame(receipt, frame)

    assert releases == [
        {
            "connection_id": connection_id,
            "session_id": session_id,
            "generation": 1,
            "terminal_state": terminal_state,
        },
        {
            "connection_id": connection_id,
            "session_id": session_id,
            "generation": 1,
            "terminal_state": terminal_state,
        },
    ]
    assert reconciliations == [
        {
            "worker_identity": "voice-worker-a",
            "released_session_ids": (session_id,),
            "released_assignment_ids": (assignment_id,),
        }
    ]
    assert media_frames == (["media_state", "media_state"] if frame_type == "media_state" else [])


@pytest.mark.asyncio
async def test_terminal_client_playout_releases_the_exact_capture_fence() -> None:
    now = datetime.now(UTC)
    session = SimpleNamespace(
        user_id="user-a",
        session_id="00000000-0000-4000-8000-000000000001",
        generation=1,
    )
    fence = AnnouncementFence(
        session_id=session.session_id,
        generation=1,
        media_grant_revision=2,
        announcement_id="00000000-0000-4000-8000-000000000011",
        announcement_sequence=2,
        turn_id="00000000-0000-4000-8000-000000000021",
        kind="acknowledgement",
        quantum_role="single",
        quantum_index=0,
        result_reserved_samples_after=None,
        max_duration_samples=96_000,
        worker_identity="voice-worker-a",
        device_id="00000000-0000-4000-8000-000000000031",
        connection_generation="00000000-0000-4000-8000-000000000041",
        transport="livekit",
    )
    recorded: list[dict[str, object]] = []
    released: list[str] = []

    class Repository:
        def get_session(self, *, user_id: str, session_id: str):
            assert (user_id, session_id) == (session.user_id, session.session_id)
            return session

        def record_client_playout(self, **kwargs):
            recorded.append(kwargs)

    class Media:
        async def accept_client_playout(self, **kwargs):
            event = kwargs["event"]
            return ClientPlayoutObservation(
                user_id=session.user_id,
                fence=fence,
                turn_announcement_sequence=1,
                phase=event.phase,
                client_sequence=event.client_sequence,
                received_at=now,
            )

        async def release_capture_after_playout(self, _session, announcement_id):
            assert _session is session
            released.append(announcement_id)
            return True

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=object(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
    )
    claims = SimpleNamespace(
        device_id=fence.device_id,
        connection_generation=fence.connection_generation,
    )

    await services.handle_client_playout(
        user_id=session.user_id,
        claims=claims,  # type: ignore[arg-type]
        event=SimpleNamespace(
            session_id=session.session_id,
            phase="started",
            client_sequence=1,
        ),  # type: ignore[arg-type]
    )
    assert released == []
    await services.handle_client_playout(
        user_id=session.user_id,
        claims=claims,  # type: ignore[arg-type]
        event=SimpleNamespace(
            session_id=session.session_id,
            phase="finished",
            client_sequence=2,
        ),  # type: ignore[arg-type]
    )
    assert released == [fence.announcement_id]
    assert len(recorded) == 2
    assert {item["announcement_sequence"] for item in recorded} == {1}


@pytest.mark.asyncio
async def test_authenticated_recognition_failure_is_durably_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = {
        "type": "recognition_failed",
        "session_id": "00000000-0000-4000-8000-000000000041",
        "generation": 1,
        "client_turn_id": "00000000-0000-4000-8000-000000000042",
        "reason": "asr_failed",
    }
    calls: list[dict[str, object]] = []
    guidance: list[tuple[object, str]] = []
    guidance_started = asyncio.Event()
    guidance_release = asyncio.Event()
    rejected_turn = _voice_turn(
        session_id=frame["session_id"],
        turn_id="00000000-0000-4000-8000-000000000043",
        state="abandoned",
        rejection_reason="malformed_final",
    )
    session = SimpleNamespace(
        user_id="user-a",
        session_id=frame["session_id"],
        generation=1,
        foreground_active=True,
        microphone_enabled=True,
    )

    class Coordinator:
        async def reject_recognition_failed(self, received):
            assert received is frame
            return SimpleNamespace(turn=rejected_turn)

    class Repository:
        def set_true_idle(self, **kwargs):
            calls.append(kwargs)
            return session

    class Media:
        async def current_session(self, session_id: str, generation: int):
            if (session_id, generation) == (session.session_id, 1):
                return session
            return None

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=Coordinator(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
    )

    async def speak_rejection(_services, turn, *, reason):
        guidance.append((turn, reason))
        guidance_started.set()
        await guidance_release.wait()

    monkeypatch.setattr(
        VoiceServices,
        "speak_preacceptance_rejection",
        speak_rejection,
    )

    await services.handle_worker_frame(None, frame)  # type: ignore[arg-type]
    await asyncio.wait_for(guidance_started.wait(), timeout=1)

    assert len(calls) == 1
    assert calls[0]["listening"] is False
    assert (session.session_id, 1) not in services.listening_sessions
    assert guidance == [(rejected_turn, "malformed_final")]
    assert len(services.preacceptance_guidance_tasks) == 1
    guidance_release.set()
    await _eventually(lambda: not services.preacceptance_guidance_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_reason", "metric_reason"),
    [
        ("self_speech", "self_speech_suppressed"),
        ("hallucinated_transcript", "hallucination_suppressed"),
    ],
)
async def test_silent_recognition_failures_are_abandoned_without_retry_guidance(
    monkeypatch: pytest.MonkeyPatch,
    failure_reason: str,
    metric_reason: str,
) -> None:
    frame = {
        "type": "recognition_failed",
        "session_id": "00000000-0000-4000-8000-000000000051",
        "generation": 1,
        "client_turn_id": "00000000-0000-4000-8000-000000000052",
        "reason": failure_reason,
    }
    rejected_turn = _voice_turn(
        session_id=frame["session_id"],
        turn_id="00000000-0000-4000-8000-000000000053",
        state="abandoned",
        rejection_reason="malformed_final",
    )
    session = SimpleNamespace(
        user_id="user-a",
        session_id=frame["session_id"],
        generation=1,
        foreground_active=True,
        microphone_enabled=True,
        device_kind="web",
        transport="livekit",
    )
    idle_updates: list[dict[str, object]] = []
    guidance: list[tuple[object, str]] = []
    suppressions: list[object] = []
    metrics = RuntimeObservability(deployment_instance="test")

    class Coordinator:
        async def suppress_self_speech(self, received):
            assert received is frame
            suppressions.append(received)
            return SimpleNamespace(turn=rejected_turn)

        async def reject_recognition_failed(self, _received):
            raise AssertionError(
                "silent recognition failures must not use the retrying "
                "rejection path"
            )

    class Repository:
        def set_true_idle(self, **kwargs):
            idle_updates.append(kwargs)
            return session

    class Media:
        async def current_session(self, session_id: str, generation: int):
            if (session_id, generation) == (session.session_id, 1):
                return session
            return None

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=Coordinator(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
        observability=metrics,
    )

    def schedule_rejection(_services, turn, *, reason):
        guidance.append((turn, reason))

    monkeypatch.setattr(
        VoiceServices,
        "schedule_preacceptance_rejection",
        schedule_rejection,
    )

    await services.handle_worker_frame(None, frame)  # type: ignore[arg-type]

    assert len(idle_updates) == 1
    assert idle_updates[0]["listening"] is False
    assert suppressions == [frame]
    assert guidance == []
    assert services.preacceptance_guidance_tasks == set()
    assert any(
        sample.name == "voice_turn_total"
        and sample.labels["result_code"] == "rejected"
        and sample.labels["voice_reason"] == metric_reason
        for sample in metrics.snapshot()
    )


@pytest.mark.asyncio
async def test_preacceptance_background_guidance_task_count_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = _voice_turn(
        state="abandoned",
        rejection_reason="permission_denied",
    )
    services, _clock, _repository, _media = _runner_services(turn)
    release = asyncio.Event()

    async def blocked_guidance(_services, _turn, *, reason):
        assert reason == "permission_denied"
        await release.wait()

    monkeypatch.setattr(
        VoiceServices,
        "speak_preacceptance_rejection",
        blocked_guidance,
    )
    for _index in range(33):
        services.schedule_preacceptance_rejection(
            turn,
            reason="permission_denied",
        )

    assert len(services.preacceptance_guidance_tasks) == 32
    release.set()
    await _eventually(lambda: not services.preacceptance_guidance_tasks)


@pytest.mark.asyncio
async def test_preacceptance_permission_denial_speaks_one_setup_instruction_only(
) -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-0000000000a3",
        turn_id="00000000-0000-4000-8000-0000000000a4",
        state="abandoned",
        rejection_reason="permission_denied",
    )
    services, clock, repository, media = _runner_services(turn)

    first = asyncio.create_task(
        services.speak_preacceptance_rejection(
            turn,
            reason="permission_denied",
        )
    )
    await _eventually(lambda: len(media.calls) == 1)
    assert media.calls == [
        {
            "turn_id": turn.turn_id,
            "kind": "waiting",
            "text": "Please set up your AI provider in Settings so I can continue.",
            "announcement_id": media.calls[0]["announcement_id"],
        }
    ]
    assert "On it" not in str(media.calls[0]["text"])
    assert repository.turns[turn.turn_id].message_id is None
    assert repository.turns[turn.turn_id].accepted_at is None
    media.finish()
    await first

    await services.speak_preacceptance_rejection(
        repository.turns[turn.turn_id],
        reason="permission_denied",
    )
    clock.advance(30)
    runner = services.announcement_runners[(turn.session_id, 1)]
    runner.wake()
    await asyncio.sleep(0)
    assert len(media.calls) == 1
    assert runner._scheduler.active_turn_count == 0
    await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_preacceptance_malformed_final_is_an_honest_retry_refusal() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-0000000000a5",
        turn_id="00000000-0000-4000-8000-0000000000a6",
        state="abandoned",
        rejection_reason="malformed_final",
    )
    services, _clock, _repository, media = _runner_services(turn)

    prompt = asyncio.create_task(
        services.speak_preacceptance_rejection(
            turn,
            reason="malformed_final",
        )
    )
    await _eventually(lambda: len(media.calls) == 1)
    assert media.calls[0]["kind"] == "refusal"
    assert media.calls[0]["text"] == "Please say that again."
    assert "complete" not in str(media.calls[0]["text"]).lower()
    media.finish()
    await prompt
    await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ("muted", "ended", "stale_revision"))
async def test_preacceptance_guidance_obeys_session_media_and_mute_fences(
    fence: str,
) -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-0000000000a7",
        turn_id="00000000-0000-4000-8000-0000000000a8",
        state="abandoned",
        rejection_reason="permission_denied",
        media_grant_revision=1 if fence == "stale_revision" else 2,
    )
    services, clock, _repository, media = _runner_services(turn)
    key = (turn.session_id, turn.session_generation)
    if fence == "muted":
        services.announcement_muted_sessions.add(key)
    elif fence == "ended":
        await services.handle_runtime_session_end(
            SimpleNamespace(session_id=turn.session_id, generation=1),
            "logout",
        )

    await services.speak_preacceptance_rejection(
        turn,
        reason="permission_denied",
    )
    clock.advance(30)
    await asyncio.sleep(0)

    assert media.calls == []
    assert services.announcement_runners == {}


@pytest.mark.asyncio
async def test_session_runner_serializes_overlapping_turn_acknowledgements() -> None:
    session_id = "00000000-0000-4000-8000-000000000081"
    first_turn = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-000000000082",
    )
    second_turn = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-000000000083",
    )
    services, clock, _repository, media = _runner_services(
        first_turn,
        second_turn,
    )

    first = asyncio.create_task(services.start_turn_announcements(first_turn))
    await _eventually(lambda: len(media.calls) == 1)
    second = asyncio.create_task(services.start_turn_announcements(second_turn))
    runner = services.announcement_runners[(session_id, 1)]
    await _eventually(lambda: runner._scheduler.active_turn_count == 2)

    assert len(media.calls) == 1
    assert media.stops == 0
    media.finish()
    await first
    clock.advance(0.25)
    runner.wake()
    await _eventually(lambda: len(media.calls) == 2)

    assert [item["turn_id"] for item in media.calls] == [
        first_turn.turn_id,
        second_turn.turn_id,
    ]
    assert [item["kind"] for item in media.calls] == [
        "acknowledgement",
        "acknowledgement",
    ]
    assert media.stops == 0
    media.finish()
    await second
    await _eventually(lambda: not runner._speaking)
    await services._close_announcement_runner(session_id, 1)


@pytest.mark.asyncio
async def test_session_runner_emits_acknowledgement_and_cadence_timings() -> None:
    metrics = RuntimeObservability(deployment_instance="test")
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-0000000000a1",
        turn_id="00000000-0000-4000-8000-0000000000a2",
    )
    services, clock, _repository, media = _runner_services(
        turn,
        observability=metrics,
    )
    start = asyncio.create_task(services.start_turn_announcements(turn))
    await _eventually(lambda: len(media.calls) == 1)
    media.finish()
    await start

    clock.advance(14.0)
    runner = services.announcement_runners[(turn.session_id, 1)]
    runner.wake()
    await _eventually(lambda: len(media.calls) == 2)
    media.finish()
    await _eventually(lambda: not runner._speaking)

    names = {sample.name for sample in metrics.snapshot()}
    assert "voice_acknowledgement_seconds" in names
    assert "voice_cadence_gap_seconds" in names
    await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_session_runner_shutdown_interrupts_active_speech() -> None:
    session_id = "00000000-0000-4000-8000-0000000000c1"
    turn = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-0000000000c2",
    )
    services, _clock, _repository, media = _runner_services(turn)
    start = asyncio.create_task(services.start_turn_announcements(turn))
    await _eventually(lambda: len(media.calls) == 1)

    await services._close_announcement_runner(session_id, 1)
    await start

    assert media.stops == 1
    assert media.active_announcement_id is None


@pytest.mark.asyncio
async def test_old_chat_deletion_fences_speech_but_preserves_accepted_work() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-0000000000c3",
        turn_id="00000000-0000-4000-8000-0000000000c4",
    )
    services, _clock, repository, media = _runner_services(turn)
    start = asyncio.create_task(services.start_turn_announcements(turn))
    await _eventually(lambda: len(media.calls) == 1)

    await services.handle_chat_unavailable(
        ChatUnavailableMutation(
            user_id=turn.user_id,
            chat_id=turn.chat_id,
            reason="deleted",
            chat_deleted=True,
            replayed=False,
            ended_sessions=(),
            announcement_session_keys=((turn.session_id, turn.session_generation),),
            unaccepted_turn_ids=(),
            accepted_turn_ids=(turn.turn_id,),
            aborted_result_commit_ids=(),
        )
    )
    await start

    runner = services.announcement_runners[(turn.session_id, 1)]
    assert media.stops == 1
    assert media.active_announcement_id is None
    assert not runner._scheduler.has_turn(turn.turn_id)
    assert repository.turns[turn.turn_id].state == "processing"
    await services._close_announcement_runner(turn.session_id, 1)


@pytest.mark.asyncio
async def test_old_chat_deletion_before_runner_blocks_stale_acknowledgement() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-0000000000c5",
        turn_id="00000000-0000-4000-8000-0000000000c6",
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_chat_unavailable(
        ChatUnavailableMutation(
            user_id=turn.user_id,
            chat_id=turn.chat_id,
            reason="deleted",
            chat_deleted=True,
            replayed=False,
            ended_sessions=(),
            announcement_session_keys=((turn.session_id, turn.session_generation),),
            unaccepted_turn_ids=(),
            accepted_turn_ids=(turn.turn_id,),
            aborted_result_commit_ids=(),
        )
    )

    await services.start_turn_announcements(turn)

    assert media.calls == []
    assert services.announcement_runners == {}
    assert repository.turns[turn.turn_id].state == "processing"


@pytest.mark.asyncio
async def test_session_end_before_runner_blocks_stale_accepted_work_callback() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-0000000000c7",
        turn_id="00000000-0000-4000-8000-0000000000c8",
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "logout",
    )

    await services.start_turn_announcements(turn)

    assert media.calls == []
    assert services.announcement_runners == {}
    assert repository.turns[turn.turn_id].state == "processing"


@pytest.mark.asyncio
async def test_media_end_after_provider_failure_still_terminalizes_turn() -> None:
    turn = _voice_turn(
        session_id="00000000-0000-4000-8000-0000000000c9",
        turn_id="00000000-0000-4000-8000-0000000000ca",
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )

    terminal = await services.finish_turn_announcements(
        turn,
        terminal_kind="failed",
        recap_text="I couldn't complete that request.",
        recap_source="terminal_status",
        sensitivity="unknown",
        result_commit_id=None,
    )

    assert terminal.state == "failed"
    assert terminal.terminal_kind == "failed"
    assert terminal.terminal_at is not None
    assert terminal.result_commit_id is None
    assert terminal.recap_source == "terminal_status"
    assert terminal.sensitivity == "unknown"
    assert repository.turns[turn.turn_id] == terminal
    assert media.calls == []
    assert services.announcement_runners == {}


@pytest.mark.asyncio
async def test_media_end_terminal_fallback_is_exact_turn_isolated() -> None:
    session_id = "00000000-0000-4000-8000-0000000000cb"
    completed = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-0000000000cc",
    )
    peer = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-0000000000cd",
        client_turn_id="00000000-0000-4000-8000-0000000000ce",
        submission_id="00000000-0000-4000-8000-0000000000cf",
        request_generation="00000000-0000-4000-8000-0000000000d0",
    )
    services, _clock, repository, media = _runner_services(completed, peer)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )

    terminal = await services.finish_turn_announcements(
        completed,
        terminal_kind="succeeded",
        recap_text="The report is ready.",
        recap_source="authoritative_summary",
        sensitivity="non_sensitive",
        result_commit_id="00000000-0000-4000-8000-0000000000d1",
    )

    assert terminal.state == "succeeded"
    assert terminal.terminal_kind == "succeeded"
    assert terminal.result_commit_id == "00000000-0000-4000-8000-0000000000d1"
    assert terminal.recap_source == "authoritative_summary"
    assert terminal.sensitivity == "non_sensitive"
    assert terminal.terminal_at is not None
    assert repository.turns[peer.turn_id] == peer
    assert peer.state == "processing"
    assert peer.terminal_at is None
    assert media.calls == []
    assert services.announcement_runners == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_kind", "announcement_kind"),
    (("failed", "failure"), ("refused", "refusal")),
)
async def test_session_runner_speaks_honest_failure_and_refusal(
    terminal_kind: str,
    announcement_kind: str,
) -> None:
    session_id = "00000000-0000-4000-8000-000000000091"
    turn = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-000000000092",
    )
    services, clock, repository, media = _runner_services(turn)
    start = asyncio.create_task(services.start_turn_announcements(turn))
    await _eventually(lambda: len(media.calls) == 1)
    media.finish()
    await start

    terminal = asyncio.create_task(
        services.finish_turn_announcements(
            turn,
            terminal_kind=terminal_kind,
            recap_text="",
            recap_source="terminal_status",
            sensitivity="unknown",
            result_commit_id=None,
        )
    )
    await _eventually(lambda: repository.turns[turn.turn_id].state == terminal_kind)
    clock.advance(0.25)
    runner = services.announcement_runners[(session_id, 1)]
    runner.wake()
    await _eventually(lambda: len(media.calls) == 2)

    assert media.calls[-1]["kind"] == announcement_kind
    assert "Done" not in str(media.calls[-1]["text"])
    media.finish()
    terminal_turn = await terminal
    assert terminal_turn.state == terminal_kind
    await _eventually(lambda: not runner._speaking)
    await services._close_announcement_runner(session_id, 1)


@pytest.mark.asyncio
async def test_session_runner_serializes_committed_result_recap_quanta() -> None:
    session_id = "00000000-0000-4000-8000-0000000000d1"
    turn = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-0000000000d2",
    )
    services, clock, repository, media = _runner_services(turn)
    start = asyncio.create_task(services.start_turn_announcements(turn))
    await _eventually(lambda: len(media.calls) == 1)
    media.finish()
    await start

    recap_text = "The request completed and your report is ready."
    terminal = asyncio.create_task(
        services.finish_turn_announcements(
            turn,
            terminal_kind="succeeded",
            recap_text=recap_text,
            recap_source="authoritative_summary",
            sensitivity="non_sensitive",
            result_commit_id="00000000-0000-4000-8000-0000000000d3",
        )
    )
    await _eventually(lambda: repository.turns[turn.turn_id].state == "succeeded")
    clock.advance(0.25)
    runner = services.announcement_runners[(session_id, 1)]
    runner.wake()
    await _eventually(lambda: len(media.calls) == 2)
    assert media.calls[-1]["kind"] == "result"
    assert media.calls[-1]["text"] == "Done."
    media.finish()

    await _eventually(lambda: len(media.calls) == 3)
    assert media.calls[-1]["kind"] == "result"
    assert media.calls[-1]["text"] == recap_text
    media.finish()

    terminal_turn = await terminal
    assert terminal_turn.state == "succeeded"
    assert terminal_turn.recap_source == "authoritative_summary"
    assert terminal_turn.result_quantum_count == 2
    await _eventually(lambda: not runner._speaking)
    await services._close_announcement_runner(session_id, 1)


@pytest.mark.asyncio
async def test_session_runner_preserves_recap_with_immediate_overlapping_handoffs() -> None:
    session_id = "00000000-0000-4000-8000-0000000000d4"
    earlier = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-0000000000d5",
    )
    latest = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-0000000000d6",
        client_turn_id="00000000-0000-4000-8000-0000000000d7",
        submission_id="00000000-0000-4000-8000-0000000000d8",
        request_generation="00000000-0000-4000-8000-0000000000d9",
    )
    services, clock, repository, media = _runner_services(earlier, latest)

    earlier_start = asyncio.create_task(services.start_turn_announcements(earlier))
    await _eventually(lambda: len(media.calls) == 1)
    media.finish()
    await earlier_start

    runner = services.announcement_runners[(session_id, 1)]
    latest_start = asyncio.create_task(services.start_turn_announcements(latest))
    await _eventually(lambda: len(media.calls) == 2)
    media.finish()
    await latest_start

    clock.advance(CADENCE_TARGET_SECONDS)
    terminal = asyncio.create_task(
        services.finish_turn_announcements(
            latest,
            terminal_kind="succeeded",
            recap_text="The requested report is complete and ready to review.",
            recap_source="authoritative_summary",
            sensitivity="non_sensitive",
            result_commit_id="00000000-0000-4000-8000-0000000000da",
        )
    )
    await _eventually(lambda: len(media.calls) == 3)
    assert media.calls[-1]["turn_id"] == latest.turn_id
    assert media.calls[-1]["kind"] == "result"
    assert media.calls[-1]["text"] == "Latest request done."
    media.finish()

    # The runner must attempt the due handoff immediately. Waiting until the
    # 250 ms maximum before scheduling would make normal timer jitter fatal.
    await _eventually(lambda: len(media.calls) == 4)
    assert media.calls[-1]["turn_id"] == earlier.turn_id
    assert media.calls[-1]["kind"] == "progress"
    assert not runner.task.done()
    media.finish()
    await _eventually(lambda: len(media.calls) == 5)
    assert media.calls[-1]["turn_id"] == latest.turn_id
    assert media.calls[-1]["kind"] == "result"
    assert media.calls[-1]["text"] == (
        "The requested report is complete and ready to review."
    )
    media.finish()

    terminal_turn = await terminal
    assert terminal_turn.state == "succeeded"
    assert terminal_turn.result_quantum_count == 2
    assert not runner.task.done()
    await _eventually(lambda: not runner._speaking)
    await services._close_announcement_runner(session_id, 1)


@pytest.mark.asyncio
async def test_session_runner_cancels_stale_progress_before_cancellation() -> None:
    session_id = "00000000-0000-4000-8000-0000000000a1"
    turn = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-0000000000a2",
    )
    services, clock, repository, media = _runner_services(turn)
    start = asyncio.create_task(services.start_turn_announcements(turn))
    await _eventually(lambda: len(media.calls) == 1)
    media.finish()
    await start
    runner = services.announcement_runners[(session_id, 1)]

    clock.advance(14)
    runner.wake()
    await _eventually(lambda: len(media.calls) == 2)
    assert media.calls[-1]["kind"] == "progress"

    terminal = asyncio.create_task(
        services.finish_turn_announcements(
            turn,
            terminal_kind="cancelled",
            recap_text="",
            recap_source="terminal_status",
            sensitivity="unknown",
            result_commit_id=None,
        )
    )
    await _eventually(
        lambda: media.stops == 1
        and repository.turns[turn.turn_id].state == "cancelled"
    )
    clock.advance(0.25)
    runner.wake()
    await _eventually(lambda: len(media.calls) == 3)

    assert [item["kind"] for item in media.calls] == [
        "acknowledgement",
        "progress",
        "cancellation",
    ]
    media.finish()
    terminal_turn = await terminal
    assert terminal_turn.state == "cancelled"
    clock.advance(30)
    runner.wake()
    await asyncio.sleep(0)
    assert len(media.calls) == 3
    await services._close_announcement_runner(session_id, 1)


@pytest.mark.asyncio
async def test_session_runner_waits_once_and_resumes_progress_cadence() -> None:
    session_id = "00000000-0000-4000-8000-0000000000b1"
    turn = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-0000000000b2",
    )
    services, clock, repository, media = _runner_services(turn)
    start = asyncio.create_task(services.start_turn_announcements(turn))
    await _eventually(lambda: len(media.calls) == 1)
    media.finish()
    await start
    runner = services.announcement_runners[(session_id, 1)]

    waiting = asyncio.create_task(
        services.wait_turn_announcements(turn, waiting_reason="login")
    )
    await _eventually(
        lambda: any(
            update["user_input_gate"] is True
            for update in repository.idle_updates
        )
    )
    clock.advance(0.25)
    runner.wake()
    await _eventually(lambda: len(media.calls) == 2)
    assert media.calls[-1]["kind"] == "waiting"
    assert "sign in" in str(media.calls[-1]["text"]).lower()
    media.finish()
    await waiting

    clock.advance(30)
    runner.wake()
    await asyncio.sleep(0)
    assert len(media.calls) == 2

    await services.resume_turn_announcements(turn)
    clock.advance(14)
    runner.wake()
    await _eventually(lambda: len(media.calls) == 3)
    assert media.calls[-1]["kind"] == "progress"
    media.finish()
    await _eventually(lambda: not runner._speaking)
    await services._close_announcement_runner(session_id, 1)


@pytest.mark.asyncio
async def test_session_runner_mute_interrupts_and_unmute_starts_fresh_cadence() -> None:
    session_id = "00000000-0000-4000-8000-0000000000e1"
    turn = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-0000000000e2",
    )
    services, clock, _repository, media = _runner_services(turn)
    start = asyncio.create_task(services.start_turn_announcements(turn))
    await _eventually(lambda: len(media.calls) == 1)
    media.finish()
    await start
    runner = services.announcement_runners[(session_id, 1)]

    clock.advance(14)
    runner.wake()
    await _eventually(lambda: len(media.calls) == 2)
    assert media.calls[-1]["kind"] == "progress"

    await services.set_session_speech_muted(session_id, 1, True)
    assert media.stops == 1
    assert media.active_announcement_id is None
    clock.advance(30)
    runner.wake()
    await asyncio.sleep(0)
    assert len(media.calls) == 2

    await services.set_session_speech_muted(session_id, 1, False)
    clock.advance(13.999)
    runner.wake()
    await asyncio.sleep(0)
    assert len(media.calls) == 2
    clock.advance(0.001)
    runner.wake()
    await _eventually(lambda: len(media.calls) == 3)
    assert media.calls[-1]["kind"] == "progress"
    media.finish()
    await _eventually(lambda: not runner._speaking)
    await services._close_announcement_runner(session_id, 1)


@pytest.mark.asyncio
async def test_session_runner_does_not_replay_terminal_recap_after_unmute() -> None:
    session_id = "00000000-0000-4000-8000-0000000000f1"
    turn = _voice_turn(
        session_id=session_id,
        turn_id="00000000-0000-4000-8000-0000000000f2",
    )
    services, clock, _repository, media = _runner_services(turn)
    start = asyncio.create_task(services.start_turn_announcements(turn))
    await _eventually(lambda: len(media.calls) == 1)
    media.finish()
    await start
    runner = services.announcement_runners[(session_id, 1)]

    await services.set_session_speech_muted(session_id, 1, True)
    terminal_turn = await services.finish_turn_announcements(
        turn,
        terminal_kind="succeeded",
        recap_text="The committed result remains visible.",
        recap_source="authoritative_summary",
        sensitivity="non_sensitive",
        result_commit_id="00000000-0000-4000-8000-0000000000f3",
    )
    assert terminal_turn.state == "succeeded"
    assert len(media.calls) == 1

    await services.set_session_speech_muted(session_id, 1, False)
    clock.advance(30)
    runner.wake()
    await asyncio.sleep(0)
    assert len(media.calls) == 1
    await services._close_announcement_runner(session_id, 1)


@pytest.mark.asyncio
async def test_sensitive_consent_is_terminal_result_bound_and_consumed_once() -> None:
    session_id = "00000000-0000-4000-8000-000000000031"
    turn_id = "00000000-0000-4000-8000-000000000032"
    device_id = "00000000-0000-4000-8000-000000000033"
    connection_id = "00000000-0000-4000-8000-000000000034"
    binding_id = "00000000-0000-4000-8000-000000000035"
    result_id = "result-commit-1"
    session = SimpleNamespace(
        session_id=session_id,
        generation=1,
        media_grant_revision=2,
    )
    turn = _voice_turn(
        session_id=session_id,
        turn_id=turn_id,
        state="succeeded",
        sensitivity="sensitive",
        result_commit_id=result_id,
        result_quantum_count=1,
        announcement_sequence=2,
        result_reserved_samples=36_000,
    )
    claims = []
    spoken = []

    class Repository:
        def get_controlled_session(self, **kwargs):
            assert kwargs["session_id"] == session_id
            assert kwargs["expected_media_grant_revision"] == 2
            return session

        def get_turn(self, **kwargs):
            assert kwargs == {"user_id": "user-a", "turn_id": turn_id}
            return turn

    class Coordinator:
        async def claim_turn_announcement(self, *, user_id, request):
            assert user_id == "user-a"
            claims.append(request)
            adapter = AnnouncementStateAdapter(PhraseBook(APPROVED_PHRASE_KEYS))
            mutation = adapter.claim(
                AnnouncementState(
                    generation=1,
                    announcement_sequence=2,
                    result_reserved_samples=36_000,
                    result_quantum_count=1,
                    last_announcement_kind="result",
                    terminal=False,
                ),
                request,
                now=datetime.now(UTC),
            )
            return SimpleNamespace(
                claim=mutation.claim,
            )

        async def complete_turn_announcement(self, **_kwargs):
            return None

    class Media:
        async def speak_turn(self, current_turn, claim, **kwargs):
            spoken.append((current_turn, claim, kwargs))

        async def await_speech_terminal(self, _announcement_id, **_kwargs):
            return "speech_finished"

        async def current_session(self, _session_id, _generation):
            return session

        async def stop_speech(self, _session):
            return None

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=Coordinator(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=object(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
        speech_backend=VoiceSpeechBackend.LLM_FACTORY,
    )
    await services.remember_sensitive_recap(
        turn,  # type: ignore[arg-type]
        result_id=result_id,
        text="The private result detail.",
    )
    control = {
        "device_id": device_id,
        "connection_generation": connection_id,
        "binding_id": binding_id,
        "binding_expires_at": datetime.now(UTC) + timedelta(minutes=1),
    }
    request = {
        "expected_generation": 1,
        "expected_media_grant_revision": 2,
        "turn_id": turn_id,
        "consent_method": "tap",
    }
    await services.consent_sensitive_recap(
        user_id="user-a",
        session_id=session_id,
        result_id=result_id,
        control=control,
        request=request,
    )
    assert claims[0].authorized_terminal_sensitive_recap is True
    assert claims[0].quantum_role == "result_continuation"
    assert spoken[0][2] == {
        "text": "The private result detail.",
        "sensitive_authorized": True,
    }
    with pytest.raises(VoiceApiError, match="sensitive_consent_unavailable"):
        await services.consent_sensitive_recap(
            user_id="user-a",
            session_id=session_id,
            result_id=result_id,
            control=control,
            request=request,
        )
    await services._close_announcement_runner(session_id, 1)


def test_production_rejects_unapproved_closure_and_missing_replica() -> None:
    with pytest.raises(VoiceBootstrapError, match="unapproved_voice_worker_closure"):
        build_voice_services(
            plane_runtime=_PlaneRuntime(),
            environ=_environment(ASTRAL_ENV="production"),
        )
    with pytest.raises(VoiceBootstrapError, match="missing_voice_replica_id"):
        build_voice_services(
            plane_runtime=_PlaneRuntime(),
            environ=_environment(
                ASTRAL_ENV="production",
                LIVEKIT_PUBLIC_URL="wss://voice.example.test",
                VOICE_WORKER_CLOSURE_SHA256="a" * 64,
            ),
        )


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("VOICE_MAX_WORKERS", "0"),
        ("VOICE_MAX_SESSIONS_PER_WORKER", "101"),
        ("VOICE_MAX_TOTAL_SESSIONS", "not-an-integer"),
    ),
)
def test_capacity_configuration_is_bounded(name: str, value: str) -> None:
    with pytest.raises(VoiceBootstrapError, match="invalid_voice_capacity"):
        build_voice_services(
            plane_runtime=_PlaneRuntime(),
            environ=_environment(**{name: value}),
        )
