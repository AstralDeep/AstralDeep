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
        capability=None,
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
    ready = SimpleNamespace(session_id=session.session_id, generation=1, speech_revision=2)
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
        capability=None,
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
        capability=None,
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
        capability=None,
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
        capability=None,
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
        capability=None,
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
        capability=None,
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
        capability=None,
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
        capability=None,
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
        capability=None,
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
        capability=None,
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
    assert media.calls[0]["text"] == "I didn't understand that. Please try again."
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
