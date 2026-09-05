"""Backend-aware local session construction and lifecycle tests."""

from __future__ import annotations

import asyncio
import threading
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


class _LocalCapability:
    async def readiness(self) -> SimpleNamespace:
        return SimpleNamespace(
            status="requires_client_readiness",
            reason="client_readiness_required",
        )


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
        capability=_LocalCapability(),
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
        capability=_LocalCapability(),
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
        capability=_LocalCapability(),
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
    reservation = await runtime._reserve_local_activation(create)
    assert await runtime._activate_local(session, create, reservation) is active
    await runtime._release_local_activation(reservation)
    repository.claim_control_lease.assert_awaited_once()
    repository.apply_chat_context.assert_called_once()
    repository.mark_session_active.assert_called_once()

    with pytest.raises(VoiceApiError, match="activation_replay_ended"):
        reservation = await runtime._reserve_local_activation(create)
        await runtime._activate_local(
            SimpleNamespace(**{**session.__dict__, "ended_at": NOW}),
            create,
            reservation,
            replayed=True,
        )
    with pytest.raises(VoiceApiError, match="backend_mismatch"):
        reservation = await runtime._reserve_local_activation(create)
        await runtime._activate_local(
            SimpleNamespace(**{**session.__dict__, "speech_backend": "llm_factory"}),
            create,
            reservation,
            replayed=True,
        )
    repository.mark_session_active.return_value = SimpleNamespace(
        chat_context_synced=False
    )
    with pytest.raises(RuntimeError, match="chat_context_not_applied"):
        reservation = await runtime._reserve_local_activation(create)
        await runtime._activate_local(
            session,
            create,
            reservation,
            replayed=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("apply failed"), asyncio.CancelledError()])
async def test_local_activation_failure_or_cancellation_aborts_exact_session(
    failure: BaseException,
) -> None:
    ended = SimpleNamespace(
        **{
            "session_id": "00000000-0000-4000-8000-000000000206",
            "generation": 1,
            "media_grant_revision": 1,
            "ended_at": NOW,
        }
    )
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
        capability=_LocalCapability(),
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
        reservation = await runtime._reserve_local_activation(create)
        await runtime._activate_local(session, create, reservation)
    media.abort.assert_awaited_once_with(session)
    repository.end_session.assert_called_once()
    ended_handler.assert_awaited_once_with(ended, "media_error")


def test_local_cleanup_binding_and_capability_shape_fail_closed() -> None:
    runtime = VoiceSessionRuntime(
        repository=SimpleNamespace(),
        capability=_LocalCapability(),
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


def _round2_request(**changes: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
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
    }
    request.update(changes)
    return request


def _round2_control() -> dict[str, Any]:
    return {
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "binding_id": BINDING,
        "binding_expires_at": NOW + timedelta(minutes=5),
    }


def _round2_session(*, generation: int = 1, ended: bool = False) -> Any:
    return SimpleNamespace(
        user_id="user-a",
        session_id="00000000-0000-4000-8000-000000000206",
        activation_id=ACTIVATION,
        device_id=DEVICE,
        device_kind="web",
        speech_backend="client_local",
        transport="client_local",
        generation=generation,
        media_grant_revision=generation,
        owner_connection_generation=CONNECTION,
        control_binding_id=BINDING,
        control_binding_expires_at=NOW + timedelta(minutes=5),
        visible_chat_id=CHAT,
        chat_context_revision=1,
        ended_at=NOW if ended else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("abort_fails", [False, True])
async def test_cancelled_local_create_reconciles_late_repository_commit(
    abort_fails: bool,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    abort_entered = threading.Event()
    abort_release = threading.Event()
    session = _round2_session()
    durable = {"state": "absent"}

    class Repository:
        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            entered.set()
            assert release.wait(timeout=2)
            durable["state"] = "starting"
            return SimpleNamespace(session=session, replayed=False)

        def end_session(self, **_kwargs: Any) -> Any:
            abort_entered.set()
            assert abort_release.wait(timeout=2)
            if abort_fails:
                raise RuntimeError("database unavailable")
            durable["state"] = "ended"
            return _round2_session(ended=True)

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    task = asyncio.create_task(
        runtime.create_session(
            user_id="user-a",
            control=_round2_control(),
            request=_round2_request(),
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
    finally:
        release.set()
        assert await asyncio.to_thread(abort_entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        abort_release.set()
        result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    if abort_fails:
        assert durable["state"] == "starting"
        assert len(runtime._pending_local_activation_cleanup) == 1
    else:
        assert durable["state"] == "ended"
        assert runtime._pending_local_activation_cleanup == {}


@pytest.mark.asyncio
async def test_cancelled_local_takeover_cleanup_aborts_replacement_only() -> None:
    previous = _round2_session(ended=True)
    replacement = _round2_session(generation=2)
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    ended: list[tuple[str, int]] = []

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return previous

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(session=replacement, replayed=False)

        def end_session(self, **kwargs: Any) -> Any:
            ended.append((kwargs["session_id"], kwargs["expected_generation"]))
            return SimpleNamespace(**{**replacement.__dict__, "ended_at": NOW})

    class Media:
        async def end(self, session: Any, _reason: str) -> None:
            assert session is previous
            cleanup_entered.set()
            await cleanup_release.wait()

        async def abort(self, session: Any) -> None:
            assert session is replacement

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=Media(),  # type: ignore[arg-type]
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    task = asyncio.create_task(
        runtime.take_over_session(
            user_id="user-a",
            session_id=previous.session_id,
            control=_round2_control(),
            request=_round2_request(
                activation_id="00000000-0000-4000-8000-000000000207",
                expected_generation=1,
                expected_media_grant_revision=1,
            ),
        )
    )
    await asyncio.wait_for(cleanup_entered.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended == [(replacement.session_id, replacement.generation)]
    assert runtime._pending_local_activation_cleanup == {}


@pytest.mark.asyncio
async def test_cancelled_local_takeover_reconciles_late_repository_commit() -> None:
    previous = _round2_session(ended=True)
    replacement = _round2_session(generation=2)
    entered = threading.Event()
    release = threading.Event()
    ended: list[tuple[str, int]] = []

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return previous

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            entered.set()
            assert release.wait(timeout=2)
            return SimpleNamespace(session=replacement, replayed=False)

        def end_session(self, **kwargs: Any) -> Any:
            ended.append((kwargs["session_id"], kwargs["expected_generation"]))
            return SimpleNamespace(**{**replacement.__dict__, "ended_at": NOW})

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    task = asyncio.create_task(
        runtime.take_over_session(
            user_id="user-a",
            session_id=previous.session_id,
            control=_round2_control(),
            request=_round2_request(
                activation_id="00000000-0000-4000-8000-000000000207",
                expected_generation=1,
                expected_media_grant_revision=1,
            ),
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended == [(replacement.session_id, replacement.generation)]
    assert runtime._pending_local_activation_cleanup == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["create", "takeover"])
@pytest.mark.parametrize("stage", ["claim", "apply", "activate"])
async def test_repeated_cancel_during_local_activation_joins_exact_abort(
    mutation: str,
    stage: str,
) -> None:
    """A second cancel must not outrun exact durable activation cleanup."""

    previous = _round2_session(ended=True)
    replacement = _round2_session(generation=2 if mutation == "takeover" else 1)
    stage_entered = threading.Event()
    stage_release = threading.Event()
    abort_entered = threading.Event()
    abort_release = threading.Event()
    durable = {"state": "starting"}

    def block_if(target: str) -> None:
        if stage == target:
            stage_entered.set()
            assert stage_release.wait(timeout=2)

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return previous

        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(session=replacement, replayed=False)

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(session=replacement, replayed=False)

        async def claim_control_lease(self, **_kwargs: Any) -> Any:
            if stage == "claim":
                await asyncio.to_thread(block_if, "claim")
            return SimpleNamespace(owner_id="replica-a")

        def apply_chat_context(self, **_kwargs: Any) -> Any:
            block_if("apply")
            return SimpleNamespace(
                session=SimpleNamespace(
                    **{**replacement.__dict__, "chat_context_synced": True}
                )
            )

        def mark_session_active(self, **_kwargs: Any) -> Any:
            block_if("activate")
            if durable["state"] == "ended":
                raise RuntimeError("activation already ended")
            durable["state"] = "active"
            return SimpleNamespace(
                **{**replacement.__dict__, "chat_context_synced": True}
            )

        def end_session(self, **kwargs: Any) -> Any:
            assert kwargs["session_id"] == replacement.session_id
            assert kwargs["expected_generation"] == replacement.generation
            abort_entered.set()
            assert abort_release.wait(timeout=2)
            durable["state"] = "ended"
            return SimpleNamespace(**{**replacement.__dict__, "ended_at": NOW})

    media = SimpleNamespace(end=AsyncMock(), abort=AsyncMock())
    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=media,  # type: ignore[arg-type]
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    if mutation == "create":
        coroutine = runtime.create_session(
            user_id="user-a",
            control=_round2_control(),
            request=_round2_request(),
        )
    else:
        coroutine = runtime.take_over_session(
            user_id="user-a",
            session_id=previous.session_id,
            control=_round2_control(),
            request=_round2_request(
                activation_id="00000000-0000-4000-8000-000000000207",
                expected_generation=1,
                expected_media_grant_revision=1,
            ),
        )
    task = asyncio.create_task(coroutine)
    assert await asyncio.to_thread(stage_entered.wait, 1)
    task.cancel()
    assert await asyncio.to_thread(abort_entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
    finally:
        stage_release.set()
        abort_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert durable["state"] == "ended"
    assert runtime._pending_local_activation_cleanup == {}
    assert runtime._local_activation_reservations == set()


@pytest.mark.asyncio
async def test_repeated_cancel_records_failed_local_activation_abort_before_return() -> None:
    """A failed exact end must retain its bounded production drain handle."""

    session = _round2_session()
    stage_entered = threading.Event()
    stage_release = threading.Event()
    abort_entered = threading.Event()
    abort_release = threading.Event()

    class Repository:
        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(session=session, replayed=False)

        async def claim_control_lease(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(owner_id="replica-a")

        def apply_chat_context(self, **_kwargs: Any) -> Any:
            stage_entered.set()
            assert stage_release.wait(timeout=2)
            return SimpleNamespace(
                session=SimpleNamespace(
                    **{**session.__dict__, "chat_context_synced": True}
                )
            )

        def end_session(self, **_kwargs: Any) -> Any:
            abort_entered.set()
            assert abort_release.wait(timeout=2)
            raise RuntimeError("database unavailable")

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    task = asyncio.create_task(
        runtime.create_session(
            user_id="user-a",
            control=_round2_control(),
            request=_round2_request(),
        )
    )
    assert await asyncio.to_thread(stage_entered.wait, 1)
    task.cancel()
    assert await asyncio.to_thread(abort_entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
    finally:
        stage_release.set()
        abort_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending = tuple(runtime._pending_local_activation_cleanup.values())
    assert len(pending) == 1
    assert pending[0].session.session_id == session.session_id
    assert pending[0].session.generation == session.generation
    assert runtime._local_activation_reservations == set()


_LOCAL_OWNERSHIP_CANCELLATION_PHASES = (
    ("create", "mutation"),
    ("create", "claim"),
    ("create", "apply"),
    ("create", "activate"),
    ("create", "release"),
    ("create", "pre_return"),
    ("takeover", "mutation"),
    ("takeover", "predecessor"),
    ("takeover", "claim"),
    ("takeover", "apply"),
    ("takeover", "activate"),
    ("takeover", "release"),
    ("takeover", "pre_return"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("replayed", [False, True], ids=["new", "replay"])
@pytest.mark.parametrize(
    "cleanup_fails",
    [False, True],
    ids=["cleanup-ended", "cleanup-retained"],
)
@pytest.mark.parametrize(
    ("mutation_kind", "phase"),
    _LOCAL_OWNERSHIP_CANCELLATION_PHASES,
    ids=[f"{kind}-{phase}" for kind, phase in _LOCAL_OWNERSHIP_CANCELLATION_PHASES],
)
async def test_local_ownership_cancellation_phase_table_preserves_exact_owner(
    mutation_kind: str,
    phase: str,
    replayed: bool,
    cleanup_fails: bool,
) -> None:
    """Every await boundary either rolls back new ownership or preserves replay."""

    previous = _round2_session(ended=True)
    session = SimpleNamespace(
        **{
            **_round2_session(
                generation=2 if mutation_kind == "takeover" else 1
            ).__dict__,
            "state": "active",
            "applied_visible_chat_id": CHAT,
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
    )
    if replayed:
        session.state = "active"
    phase_entered = threading.Event()
    phase_release = threading.Event()
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    release_cleanup_entered = threading.Event()
    release_cleanup_release = threading.Event()
    durable = {"state": "active" if replayed else "starting"}
    public_task: asyncio.Task[Any] | None = None

    def block_phase(target: str) -> None:
        if phase == target:
            phase_entered.set()
            assert phase_release.wait(timeout=3)

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return previous

        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            block_phase("mutation")
            return SimpleNamespace(session=session, replayed=replayed)

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            block_phase("mutation")
            return SimpleNamespace(session=session, replayed=replayed)

        async def claim_control_lease(self, **_kwargs: Any) -> Any:
            if phase == "claim":
                await asyncio.to_thread(block_phase, "claim")
            return SimpleNamespace(owner_id="replica-a")

        def apply_chat_context(self, **_kwargs: Any) -> Any:
            block_phase("apply")
            return SimpleNamespace(
                session=SimpleNamespace(
                    **{**session.__dict__, "chat_context_synced": True}
                )
            )

        def mark_session_active(self, **_kwargs: Any) -> Any:
            block_phase("activate")
            if durable["state"] == "ended":
                raise RuntimeError("activation already ended")
            durable["state"] = "active"
            return SimpleNamespace(**{**session.__dict__, "chat_context_synced": True})

        def end_session(self, **kwargs: Any) -> Any:
            assert kwargs["session_id"] == session.session_id
            cleanup_entered.set()
            assert cleanup_release.wait(timeout=3)
            if cleanup_fails:
                raise RuntimeError("database unavailable")
            durable["state"] = "ended"
            return SimpleNamespace(**{**session.__dict__, "ended_at": NOW})

    class Media:
        async def end(self, ended: Any, _reason: str) -> None:
            if phase == "predecessor":
                assert ended is previous
                await asyncio.to_thread(block_phase, "predecessor")

        async def abort(self, _session: Any) -> None:
            return None

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=Media(),  # type: ignore[arg-type]
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    original_release = runtime._release_local_activation
    original_handoff = runtime._acquire_local_activation_return_handoff

    async def gated_release(reservation: Any) -> None:
        if phase == "release":
            await asyncio.to_thread(block_phase, "release")
        await original_release(reservation)
        if phase == "pre_return":
            await asyncio.to_thread(block_phase, "pre_return")
        elif public_task is not None and public_task.cancelling():
            release_cleanup_entered.set()
            await asyncio.to_thread(release_cleanup_release.wait, 3)

    async def gated_handoff(
        active_session: Any,
        create: CreateSession,
        reservation: Any,
    ) -> Any:
        if phase == "release":
            await asyncio.to_thread(block_phase, "release")
        pending = await original_handoff(active_session, create, reservation)
        if phase == "pre_return":
            await asyncio.to_thread(block_phase, "pre_return")
        return pending

    runtime._release_local_activation = gated_release  # type: ignore[method-assign]
    runtime._acquire_local_activation_return_handoff = gated_handoff  # type: ignore[method-assign]
    if mutation_kind == "create":
        coroutine = runtime.create_session(
            user_id="user-a",
            control=_round2_control(),
            request=_round2_request(),
        )
    else:
        coroutine = runtime.take_over_session(
            user_id="user-a",
            session_id=previous.session_id,
            control=_round2_control(),
            request=_round2_request(
                activation_id="00000000-0000-4000-8000-000000000207",
                expected_generation=1,
                expected_media_grant_revision=1,
            ),
        )
    public_task = asyncio.create_task(coroutine)
    assert await asyncio.to_thread(phase_entered.wait, 1)
    public_task.cancel()
    if phase not in {"release", "pre_return"}:
        phase_release.set()
    for _ in range(100):
        if (
            public_task.done()
            or cleanup_entered.is_set()
            or release_cleanup_entered.is_set()
        ):
            break
        await asyncio.sleep(0)
    public_task.cancel()
    await asyncio.sleep(0)
    try:
        assert not public_task.done()
    finally:
        cleanup_release.set()
        release_cleanup_release.set()
        phase_release.set()
    with pytest.raises(asyncio.CancelledError):
        await public_task
    if replayed:
        assert durable["state"] == "active"
        assert not cleanup_entered.is_set()
        assert runtime._pending_local_activation_cleanup == {}
    elif cleanup_fails:
        assert durable["state"] in {"starting", "active"}
        pending = tuple(runtime._pending_local_activation_cleanup.values())
        assert len(pending) == 1
        assert pending[0].session.session_id == session.session_id
        assert pending[0].session.generation == session.generation
    else:
        assert durable["state"] == "ended"
        assert runtime._pending_local_activation_cleanup == {}
    assert runtime._local_activation_reservations == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["create", "takeover"])
async def test_local_repository_mutation_exception_releases_reserved_capacity(
    mutation: str,
) -> None:
    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return _round2_session(ended=True)

        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("repository unavailable")

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("repository unavailable")

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="repository unavailable"):
        if mutation == "create":
            await runtime.create_session(
                user_id="user-a",
                control=_round2_control(),
                request=_round2_request(),
            )
        else:
            await runtime.take_over_session(
                user_id="user-a",
                session_id=_round2_session().session_id,
                control=_round2_control(),
                request=_round2_request(
                    expected_generation=1,
                    expected_media_grant_revision=1,
                ),
            )
    assert runtime._local_activation_reservations == set()
    assert runtime._pending_local_activation_cleanup == {}


@pytest.mark.asyncio
async def test_pending_local_activation_cleanup_drains_before_next_create() -> None:
    abandoned = _round2_session()
    next_session = SimpleNamespace(
        **{
            **_round2_session(generation=2).__dict__,
            "session_id": "00000000-0000-4000-8000-000000000208",
        }
    )
    attempts = 0

    class Repository:
        def end_session(self, **_kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("database unavailable")
            return SimpleNamespace(**{**abandoned.__dict__, "ended_at": NOW})

        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(session=next_session, replayed=False)

        def apply_chat_context(self, **_kwargs: Any) -> Any:
            active = SimpleNamespace(
                **{
                    **next_session.__dict__,
                    "chat_context_synced": True,
                    "applied_visible_chat_id": CHAT,
                    "applied_chat_context_revision": 1,
                }
            )
            return SimpleNamespace(session=active)

        def mark_session_active(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                **{
                    **next_session.__dict__,
                    "chat_context_synced": True,
                    "applied_visible_chat_id": CHAT,
                    "applied_chat_context_revision": 1,
                    "state": "active",
                }
            )

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    abandoned_create = runtime._create_request(
        "user-a",
        _round2_control(),
        _round2_request(),
        now=NOW,
    )
    reservation = await runtime._reserve_local_activation(abandoned_create)
    await runtime._reconcile_local_activation_abort(
        abandoned,
        abandoned_create,
        reservation,
    )
    assert len(runtime._pending_local_activation_cleanup) == 1

    await runtime._require_ready()
    assert runtime._pending_local_activation_cleanup == {}
    assert attempts == 2


@pytest.mark.asyncio
async def test_local_activation_cleanup_capacity_fails_closed_before_insert() -> None:
    repository = SimpleNamespace(create_session=Mock())
    runtime = VoiceSessionRuntime(
        repository=repository,  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    runtime._abort_activation = AsyncMock(return_value=False)  # type: ignore[method-assign]
    for generation in range(1, 257):
        create = runtime._create_request(
            "user-a",
            _round2_control(),
            _round2_request(
                activation_id=(
                    f"20000000-0000-4000-8000-{generation:012d}"
                )
            ),
            now=NOW,
        )
        session = SimpleNamespace(
            **{
                **_round2_session(generation=generation).__dict__,
                "session_id": f"pending-{generation}",
            }
        )
        reservation = await runtime._reserve_local_activation(create)
        await runtime._reconcile_local_activation_abort(
            session,
            create,
            reservation,
        )

    with pytest.raises(VoiceApiError, match="local_cleanup_capacity_exhausted"):
        await runtime.create_session(
            user_id="user-a",
            control=_round2_control(),
            request=_round2_request(),
        )
    repository.create_session.assert_not_called()
    assert len(runtime._pending_local_activation_cleanup) == 256


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_kind", ["create", "takeover"])
@pytest.mark.parametrize("replayed", [False, True], ids=["new", "replay"])
@pytest.mark.parametrize(
    ("invalid_kind", "expected_code"),
    [
        ("ended", "activation_replay_ended"),
        ("backend", "backend_mismatch"),
    ],
)
async def test_local_early_activation_validation_never_exhausts_capacity(
    mutation_kind: str,
    replayed: bool,
    invalid_kind: str,
    expected_code: str,
) -> None:
    """Every post-reservation validation refusal settles its exact slot."""

    sessions: dict[str, Any] = {}
    ended_ids: list[str] = []

    def invalid_session(create: CreateSession) -> Any:
        ordinal = int(create.activation_id[-12:])
        session = SimpleNamespace(
            **{
                **_round2_session().__dict__,
                "activation_id": create.activation_id,
                "session_id": f"00000000-0000-4000-8001-{ordinal:012d}",
                "ended_at": NOW if invalid_kind == "ended" else None,
                "speech_backend": (
                    "llm_factory" if invalid_kind == "backend" else "client_local"
                ),
            }
        )
        sessions[session.session_id] = session
        return session

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return _round2_session(ended=True)

        def create_session(self, create: CreateSession, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                session=invalid_session(create),
                replayed=replayed,
            )

        def take_over_session(self, takeover: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                session=invalid_session(takeover.create),
                replayed=replayed,
            )

        def end_session(self, **kwargs: Any) -> Any:
            session = sessions[kwargs["session_id"]]
            ended_ids.append(session.session_id)
            return SimpleNamespace(**{**session.__dict__, "ended_at": NOW})

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    for ordinal in range(1, 258):
        activation_id = f"00000000-0000-4000-8000-{ordinal:012d}"
        request = _round2_request(activation_id=activation_id)
        if mutation_kind == "takeover":
            request.update(
                expected_generation=1,
                expected_media_grant_revision=1,
            )
        with pytest.raises(VoiceApiError, match=expected_code):
            if mutation_kind == "create":
                await runtime.create_session(
                    user_id="user-a",
                    control=_round2_control(),
                    request=request,
                )
            else:
                await runtime.take_over_session(
                    user_id="user-a",
                    session_id=_round2_session().session_id,
                    control=_round2_control(),
                    request=request,
                )
        assert runtime._local_activation_reservations == set()
        assert runtime._pending_local_activation_cleanup == {}

    assert len(ended_ids) == (0 if replayed else 257)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_kind", ["create", "takeover"])
@pytest.mark.parametrize("replayed", [False, True], ids=["new", "replay"])
@pytest.mark.parametrize("invalid_kind", ["ended", "backend"])
async def test_local_early_validation_settlement_joins_repeated_cancellation(
    mutation_kind: str,
    replayed: bool,
    invalid_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validation error remains primary until its exact slot is settled."""

    mutation_entered = threading.Event()
    mutation_release = threading.Event()
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    session = SimpleNamespace(
        **{
            **_round2_session().__dict__,
            "ended_at": NOW if invalid_kind == "ended" else None,
            "speech_backend": (
                "llm_factory" if invalid_kind == "backend" else "client_local"
            ),
        }
    )

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return _round2_session(ended=True)

        def _mutation(self) -> Any:
            mutation_entered.set()
            assert mutation_release.wait(timeout=3)
            return SimpleNamespace(session=session, replayed=replayed)

        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._mutation()

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._mutation()

        def end_session(self, **_kwargs: Any) -> Any:
            cleanup_entered.set()
            assert cleanup_release.wait(timeout=3)
            return SimpleNamespace(**{**session.__dict__, "ended_at": NOW})

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    settlement_entered = asyncio.Event()
    real_settlement = runtime._settle_local_activation_failure

    async def observe_settlement(*args: Any, **kwargs: Any) -> None:
        settlement_entered.set()
        await real_settlement(*args, **kwargs)

    monkeypatch.setattr(runtime, "_settle_local_activation_failure", observe_settlement)
    request = _round2_request()
    if mutation_kind == "takeover":
        request.update(
            activation_id="00000000-0000-4000-8000-000000000207",
            expected_generation=1,
            expected_media_grant_revision=1,
        )
        coroutine = runtime.take_over_session(
            user_id="user-a",
            session_id=_round2_session().session_id,
            control=_round2_control(),
            request=request,
        )
    else:
        coroutine = runtime.create_session(
            user_id="user-a",
            control=_round2_control(),
            request=request,
        )
    task = asyncio.create_task(coroutine)
    assert await asyncio.to_thread(mutation_entered.wait, 1)
    lock_held = False
    if replayed:
        await runtime._local_activation_capacity_lock.acquire()
        lock_held = True
    mutation_release.set()
    if not replayed:
        assert await asyncio.to_thread(cleanup_entered.wait, 1)
    else:
        # Yielding the event loop cannot prove the repository thread returned.
        # Cancel only after the validation error owns its real settlement path.
        await asyncio.wait_for(settlement_entered.wait(), timeout=3)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
    finally:
        cleanup_release.set()
        if lock_held:
            runtime._local_activation_capacity_lock.release()
    expected_code = (
        "activation_replay_ended" if invalid_kind == "ended" else "backend_mismatch"
    )
    with pytest.raises(VoiceApiError, match=expected_code):
        await task
    assert runtime._local_activation_reservations == set()
    assert runtime._pending_local_activation_cleanup == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_kind", ["create", "takeover"])
async def test_same_activation_id_reservations_have_exact_request_ownership(
    mutation_kind: str,
) -> None:
    """A cancelled same-ID waiter cannot release the current exact owner."""

    active = SimpleNamespace(**{**_round2_session().__dict__, "state": "active"})
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    call_lock = threading.Lock()
    call_count = 0

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return _round2_session(ended=True)

        def _mutation(self) -> Any:
            nonlocal call_count
            with call_lock:
                index = call_count
                call_count += 1
            entered[index].set()
            assert release[index].wait(timeout=3)
            return SimpleNamespace(session=active, replayed=True)

        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._mutation()

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._mutation()

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )

    def request_coroutine() -> Any:
        if mutation_kind == "create":
            return runtime.create_session(
                user_id="user-a",
                control=_round2_control(),
                request=_round2_request(),
            )
        return runtime.take_over_session(
            user_id="user-a",
            session_id=_round2_session().session_id,
            control=_round2_control(),
            request=_round2_request(
                expected_generation=1,
                expected_media_grant_revision=1,
            ),
        )

    first = asyncio.create_task(request_coroutine())
    assert await asyncio.to_thread(entered[0].wait, 1)
    second = asyncio.create_task(request_coroutine())
    for _ in range(100):
        if len(runtime._local_activation_reservations) == 2:
            break
        await asyncio.sleep(0)
    assert not entered[1].is_set()
    assert len(runtime._local_activation_reservations) == 2
    assert len({id(item) for item in runtime._local_activation_reservations}) == 2

    second.cancel()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert len(runtime._local_activation_reservations) == 1
    assert not entered[1].is_set()

    first.cancel()
    release[0].set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert runtime._local_activation_reservations == set()
    assert runtime._local_activation_keys == {}
    assert runtime._pending_local_activation_cleanup == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_kind", ["create", "takeover"])
@pytest.mark.parametrize("origin_outcome", ["cancel", "failure"])
async def test_exact_activation_replay_waits_for_originating_abort_settlement(
    mutation_kind: str,
    origin_outcome: str,
) -> None:
    """A replay cannot receive authority that its in-flight origin may revoke."""

    previous = _round2_session(ended=True)
    starting = _round2_session(generation=2 if mutation_kind == "takeover" else 1)
    first_apply_entered = threading.Event()
    first_apply_release = threading.Event()
    second_mutation_entered = threading.Event()
    abort_entered = threading.Event()
    abort_release = threading.Event()
    call_lock = threading.Lock()
    mutation_calls = 0
    apply_calls = 0
    durable = {"state": "starting"}

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return previous

        def _mutation(self) -> Any:
            nonlocal mutation_calls
            with call_lock:
                index = mutation_calls
                mutation_calls += 1
            if index:
                second_mutation_entered.set()
            snapshot = SimpleNamespace(
                **{
                    **starting.__dict__,
                    "ended_at": NOW if durable["state"] == "ended" else None,
                }
            )
            return SimpleNamespace(session=snapshot, replayed=bool(index))

        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._mutation()

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._mutation()

        async def claim_control_lease(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(owner_id="replica-a")

        def apply_chat_context(self, **_kwargs: Any) -> Any:
            nonlocal apply_calls
            with call_lock:
                index = apply_calls
                apply_calls += 1
            if index == 0:
                first_apply_entered.set()
                assert first_apply_release.wait(timeout=3)
                if origin_outcome == "failure":
                    raise RuntimeError("activation failed")
            return SimpleNamespace(
                session=SimpleNamespace(
                    **{**starting.__dict__, "chat_context_synced": True}
                )
            )

        def mark_session_active(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                **{**starting.__dict__, "chat_context_synced": True}
            )

        def end_session(self, **_kwargs: Any) -> Any:
            abort_entered.set()
            assert abort_release.wait(timeout=3)
            durable["state"] = "ended"
            return SimpleNamespace(**{**starting.__dict__, "ended_at": NOW})

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(end=AsyncMock(), abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    request = _round2_request()
    if mutation_kind == "takeover":
        request.update(
            activation_id="00000000-0000-4000-8000-000000000207",
            expected_generation=1,
            expected_media_grant_revision=1,
        )

    def invoke() -> Any:
        if mutation_kind == "create":
            return runtime.create_session(
                user_id="user-a",
                control=_round2_control(),
                request=request,
            )
        return runtime.take_over_session(
            user_id="user-a",
            session_id=previous.session_id,
            control=_round2_control(),
            request=request,
        )

    first = asyncio.create_task(invoke())
    assert await asyncio.to_thread(first_apply_entered.wait, 1)
    second = asyncio.create_task(invoke())
    try:
        assert not await asyncio.to_thread(second_mutation_entered.wait, 0.1)
        assert not second.done()
        if origin_outcome == "cancel":
            first.cancel()
            first.cancel()
        else:
            first_apply_release.set()
        assert await asyncio.to_thread(abort_entered.wait, 1)
        assert not second_mutation_entered.is_set()
    finally:
        first_apply_release.set()
        abort_release.set()
    if origin_outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await first
    else:
        with pytest.raises(RuntimeError, match="activation failed"):
            await first
    assert await asyncio.to_thread(second_mutation_entered.wait, 1)
    with pytest.raises(VoiceApiError, match="activation_replay_ended"):
        await second
    assert runtime._local_activation_reservations == set()
    assert runtime._pending_local_activation_cleanup == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_kind", ["create", "takeover"])
@pytest.mark.parametrize(
    "identity",
    ["different_user", "different_activation"],
)
async def test_distinct_activation_identities_mutate_concurrently(
    mutation_kind: str,
    identity: str,
) -> None:
    """Only an exact user/activation pair is serialized."""

    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    call_lock = threading.Lock()
    calls = 0
    session = SimpleNamespace(**{**_round2_session().__dict__, "state": "active"})

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return _round2_session(ended=True)

        def _mutation(self) -> Any:
            nonlocal calls
            with call_lock:
                index = calls
                calls += 1
            entered[index].set()
            assert release.wait(timeout=3)
            return SimpleNamespace(session=session, replayed=True)

        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._mutation()

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._mutation()

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )

    def invoke(index: int) -> Any:
        user_id = "user-b" if index and identity == "different_user" else "user-a"
        activation_id = (
            "00000000-0000-4000-8000-000000000209"
            if index and identity == "different_activation"
            else ACTIVATION
        )
        request = _round2_request(activation_id=activation_id)
        if mutation_kind == "create":
            return runtime.create_session(
                user_id=user_id,
                control=_round2_control(),
                request=request,
            )
        request.update(
            expected_generation=1,
            expected_media_grant_revision=1,
        )
        return runtime.take_over_session(
            user_id=user_id,
            session_id=_round2_session().session_id,
            control=_round2_control(),
            request=request,
        )

    first = asyncio.create_task(invoke(0))
    assert await asyncio.to_thread(entered[0].wait, 1)
    second = asyncio.create_task(invoke(1))
    try:
        assert await asyncio.to_thread(entered[1].wait, 1)
    finally:
        first.cancel()
        second.cancel()
        release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert runtime._local_activation_reservations == set()
    assert runtime._local_activation_keys == {}


@pytest.mark.asyncio
async def test_failed_activation_abort_retains_key_until_production_drain() -> None:
    """A retained abort handle keeps an exact replay from racing its later end."""

    starting = _round2_session()
    apply_entered = threading.Event()
    apply_release = threading.Event()
    second_mutation_entered = threading.Event()
    abort_attempts = 0
    mutation_calls = 0

    class Repository:
        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            nonlocal mutation_calls
            mutation_calls += 1
            if mutation_calls > 1:
                second_mutation_entered.set()
            return SimpleNamespace(session=starting, replayed=mutation_calls > 1)

        async def claim_control_lease(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(owner_id="replica-a")

        def apply_chat_context(self, **_kwargs: Any) -> Any:
            apply_entered.set()
            assert apply_release.wait(timeout=3)
            return SimpleNamespace(
                session=SimpleNamespace(
                    **{**starting.__dict__, "chat_context_synced": True}
                )
            )

        def end_session(self, **_kwargs: Any) -> Any:
            nonlocal abort_attempts
            abort_attempts += 1
            if abort_attempts == 1:
                raise RuntimeError("database unavailable")
            return SimpleNamespace(**{**starting.__dict__, "ended_at": NOW})

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    kwargs = {
        "user_id": "user-a",
        "control": _round2_control(),
        "request": _round2_request(),
    }
    first = asyncio.create_task(runtime.create_session(**kwargs))
    assert await asyncio.to_thread(apply_entered.wait, 1)
    second = asyncio.create_task(runtime.create_session(**kwargs))
    first.cancel()
    first.cancel()
    apply_release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(runtime._pending_local_activation_cleanup) == 1
    assert not second_mutation_entered.is_set()
    second.cancel()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert len(runtime._local_activation_reservations) == 0
    assert len(runtime._local_activation_keys) == 1
    await runtime._drain_pending_local_activation_cleanup()
    assert runtime._pending_local_activation_cleanup == {}
    assert runtime._local_activation_keys == {}


_LOCAL_ACTIVATION_OVERLAP_SUCCESS_PHASES = (
    ("create", "claim"),
    ("create", "apply"),
    ("create", "activate"),
    ("create", "release"),
    ("create", "pre_return"),
    ("takeover", "predecessor"),
    ("takeover", "claim"),
    ("takeover", "apply"),
    ("takeover", "activate"),
    ("takeover", "release"),
    ("takeover", "pre_return"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation_kind", "phase"),
    _LOCAL_ACTIVATION_OVERLAP_SUCCESS_PHASES,
    ids=[
        f"{mutation_kind}-{phase}"
        for mutation_kind, phase in _LOCAL_ACTIVATION_OVERLAP_SUCCESS_PHASES
    ],
)
async def test_exact_activation_replay_waits_at_every_postmutation_phase(
    mutation_kind: str,
    phase: str,
) -> None:
    """A successful origin exclusively owns its key through return commit."""

    previous = _round2_session(ended=True)
    session = SimpleNamespace(
        **{
            **_round2_session(
                generation=2 if mutation_kind == "takeover" else 1
            ).__dict__,
            "state": "active",
            "applied_visible_chat_id": CHAT,
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
    )
    phase_entered = threading.Event()
    phase_release = threading.Event()
    second_mutation_entered = threading.Event()
    mutation_calls = 0
    phase_calls = 0

    def gate(target: str) -> None:
        nonlocal phase_calls
        if phase != target:
            return
        index = phase_calls
        phase_calls += 1
        if index == 0:
            phase_entered.set()
            assert phase_release.wait(timeout=3)

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return previous

        def _mutation(self) -> Any:
            nonlocal mutation_calls
            index = mutation_calls
            mutation_calls += 1
            if index:
                second_mutation_entered.set()
            return SimpleNamespace(session=session, replayed=bool(index))

        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._mutation()

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._mutation()

        async def claim_control_lease(self, **_kwargs: Any) -> Any:
            if phase == "claim":
                await asyncio.to_thread(gate, "claim")
            return SimpleNamespace(owner_id="replica-a")

        def apply_chat_context(self, **_kwargs: Any) -> Any:
            gate("apply")
            return SimpleNamespace(
                session=SimpleNamespace(
                    **{**session.__dict__, "chat_context_synced": True}
                )
            )

        def mark_session_active(self, **_kwargs: Any) -> Any:
            gate("activate")
            return SimpleNamespace(
                **{**session.__dict__, "chat_context_synced": True}
            )

    class Media:
        async def end(self, _session: Any, _reason: str) -> None:
            if phase == "predecessor":
                await asyncio.to_thread(gate, "predecessor")

        async def abort(self, _session: Any) -> None:
            return None

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=Media(),  # type: ignore[arg-type]
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    original_handoff = runtime._acquire_local_activation_return_handoff

    async def gated_handoff(
        active_session: Any,
        create: CreateSession,
        reservation: Any,
    ) -> Any:
        if phase == "release":
            await asyncio.to_thread(gate, "release")
        pending = await original_handoff(active_session, create, reservation)
        if phase == "pre_return":
            await asyncio.to_thread(gate, "pre_return")
        return pending

    runtime._acquire_local_activation_return_handoff = gated_handoff  # type: ignore[method-assign]
    request = _round2_request()
    if mutation_kind == "takeover":
        request.update(
            activation_id="00000000-0000-4000-8000-000000000207",
            expected_generation=1,
            expected_media_grant_revision=1,
        )

    def invoke() -> Any:
        if mutation_kind == "create":
            return runtime.create_session(
                user_id="user-a",
                control=_round2_control(),
                request=request,
            )
        return runtime.take_over_session(
            user_id="user-a",
            session_id=previous.session_id,
            control=_round2_control(),
            request=request,
        )

    first = asyncio.create_task(invoke())
    assert await asyncio.to_thread(phase_entered.wait, 1)
    second = asyncio.create_task(invoke())
    try:
        assert not await asyncio.to_thread(second_mutation_entered.wait, 0.05)
        assert not second.done()
    finally:
        phase_release.set()
    first_result = await first
    assert first_result.status_code == (201 if mutation_kind == "create" else 200)
    assert await asyncio.to_thread(second_mutation_entered.wait, 1)
    second_result = await second
    assert second_result.status_code == 200
    assert runtime._local_activation_reservations == set()
    assert runtime._local_activation_keys == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_kind", ["create", "takeover"])
@pytest.mark.parametrize("abort_fails", [False, True], ids=["ended", "retained"])
async def test_cancelled_active_local_session_handoffs_capacity_before_abort(
    mutation_kind: str,
    abort_fails: bool,
) -> None:
    """An unreturned active session owns a cleanup slot before abort awaits."""

    previous = _round2_session(ended=True)
    session = _round2_session(generation=2 if mutation_kind == "takeover" else 1)
    abort_entered = threading.Event()
    abort_release = threading.Event()
    public_task: asyncio.Task[Any] | None = None

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return previous

        def create_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(session=session, replayed=False)

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(session=session, replayed=False)

        async def claim_control_lease(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(owner_id="replica-a")

        def apply_chat_context(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                session=SimpleNamespace(
                    **{**session.__dict__, "chat_context_synced": True}
                )
            )

        def mark_session_active(self, **_kwargs: Any) -> Any:
            assert public_task is not None
            public_task.cancel()
            return SimpleNamespace(**{**session.__dict__, "chat_context_synced": True})

        def end_session(self, **_kwargs: Any) -> Any:
            abort_entered.set()
            assert abort_release.wait(timeout=3)
            if abort_fails:
                raise RuntimeError("database unavailable")
            return SimpleNamespace(**{**session.__dict__, "ended_at": NOW})

    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    request = _round2_request()
    if mutation_kind == "takeover":
        request.update(
            activation_id="00000000-0000-4000-8000-000000000207",
            expected_generation=1,
            expected_media_grant_revision=1,
        )
    if mutation_kind == "create":
        coroutine = runtime.create_session(
            user_id="user-a",
            control=_round2_control(),
            request=request,
        )
    else:
        coroutine = runtime.take_over_session(
            user_id="user-a",
            session_id=previous.session_id,
            control=_round2_control(),
            request=request,
        )
    public_task = asyncio.create_task(coroutine)
    assert await asyncio.to_thread(abort_entered.wait, 1)
    public_task.cancel()
    await asyncio.sleep(0)
    try:
        assert not public_task.done()
        assert len(runtime._pending_local_activation_cleanup) == 1
        assert runtime._local_activation_reservations == set()
        fillers = []
        for ordinal in range(1, 256):
            filler = runtime._create_request(
                "user-a",
                _round2_control(),
                _round2_request(
                    activation_id=f"10000000-0000-4000-8000-{ordinal:012d}"
                ),
                now=NOW,
            )
            fillers.append(await runtime._reserve_local_activation(filler))
        with pytest.raises(
            VoiceApiError,
            match="local_cleanup_capacity_exhausted",
        ):
            overflow = runtime._create_request(
                "user-a",
                _round2_control(),
                _round2_request(
                    activation_id="10000000-0000-4000-8000-000000000256"
                ),
                now=NOW,
            )
            await runtime._reserve_local_activation(overflow)
    finally:
        abort_release.set()
    with pytest.raises(asyncio.CancelledError):
        await public_task
    assert len(runtime._pending_local_activation_cleanup) == (1 if abort_fails else 0)
    for filler in fillers:
        await runtime._release_local_activation(filler)
    assert runtime._local_activation_reservations == set()


@pytest.mark.asyncio
async def test_local_activation_handoff_identity_loss_fails_closed() -> None:
    """A missing exact token cannot free or replace another request's slot."""

    session = _round2_session()
    repository = SimpleNamespace(end_session=Mock())
    runtime = VoiceSessionRuntime(
        repository=repository,  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=SimpleNamespace(abort=AsyncMock()),
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )
    create = runtime._create_request(
        "user-a",
        _round2_control(),
        _round2_request(),
        now=NOW,
    )
    reservation = await runtime._reserve_local_activation(create)
    runtime._local_activation_reservations.remove(reservation)

    with pytest.raises(RuntimeError, match="local_activation_reservation_lost"):
        await runtime._complete_local_activation_before_return(
            session,
            create,
            reservation,
            replayed=False,
        )

    repository.end_session.assert_not_called()
    assert runtime._local_activation_reservations == set()
    assert runtime._pending_local_activation_cleanup == {}


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["success", "failure", "cancel_after_commit"],
)
async def test_local_explicit_end_fences_before_mutation_and_resolves_exact_outcome(
    outcome: str,
) -> None:
    """The exact reversible fence precedes CAS and survives cancellation."""

    session = _round2_session()
    ended = SimpleNamespace(**{**session.__dict__, "ended_at": NOW})
    prepare_entered = asyncio.Event()
    prepare_release = asyncio.Event()
    mutation_started = threading.Event()
    mutation_release = threading.Event()
    token = object()
    resolutions: list[tuple[object, bool]] = []

    class Repository:
        def get_controlled_session(self, **_kwargs: Any) -> Any:
            return session

        def end_session(self, **_kwargs: Any) -> Any:
            mutation_started.set()
            if outcome == "cancel_after_commit":
                assert mutation_release.wait(timeout=3)
            if outcome == "failure":
                raise RuntimeError("cas conflict")
            return ended

    media = SimpleNamespace(end=AsyncMock())
    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=media,  # type: ignore[arg-type]
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )

    async def prepare(current: Any) -> object:
        assert current is session
        prepare_entered.set()
        await prepare_release.wait()
        return token

    async def resolve(current: object, committed: bool) -> None:
        resolutions.append((current, committed))

    runtime.bind_local_session_end_fence_handlers(prepare, resolve)
    task = asyncio.create_task(
        runtime.end_session(
            user_id="user-a",
            session_id=session.session_id,
            control=_round2_control(),
            request={
                "expected_generation": session.generation,
                "expected_media_grant_revision": (
                    session.media_grant_revision
                ),
            },
        )
    )
    await asyncio.wait_for(prepare_entered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not mutation_started.is_set()
    prepare_release.set()

    if outcome == "failure":
        with pytest.raises(RuntimeError, match="cas conflict"):
            await task
        assert resolutions == [(token, False)]
        media.end.assert_not_awaited()
        return

    if outcome == "cancel_after_commit":
        assert await asyncio.to_thread(mutation_started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        mutation_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task

    assert resolutions == [(token, True)]
    media.end.assert_awaited_once_with(ended, "user")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "cancel_after_commit"])
async def test_local_takeover_fences_before_mutation_and_resolves_exact_outcome(
    outcome: str,
) -> None:
    """Takeover resolves its old-generation fence on conflict and late commit."""

    previous = _round2_session()
    replacement = _round2_session(generation=2)
    prepare_entered = asyncio.Event()
    prepare_release = asyncio.Event()
    mutation_started = threading.Event()
    mutation_release = threading.Event()
    token = object()
    resolutions: list[tuple[object, bool]] = []

    class Repository:
        def get_session(self, **_kwargs: Any) -> Any:
            return previous

        def take_over_session(self, *_args: Any, **_kwargs: Any) -> Any:
            mutation_started.set()
            if outcome == "cancel_after_commit":
                assert mutation_release.wait(timeout=3)
            if outcome == "failure":
                raise RuntimeError("cas conflict")
            return SimpleNamespace(session=replacement, replayed=True)

    media = SimpleNamespace(end=AsyncMock())
    runtime = VoiceSessionRuntime(
        repository=Repository(),  # type: ignore[arg-type]
        capability=_LocalCapability(),
        media=media,  # type: ignore[arg-type]
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        clock=lambda: NOW,
    )

    async def prepare(current: Any) -> object:
        assert current is previous
        prepare_entered.set()
        await prepare_release.wait()
        return token

    async def resolve(current: object, committed: bool) -> None:
        resolutions.append((current, committed))

    runtime.bind_local_session_end_fence_handlers(prepare, resolve)
    task = asyncio.create_task(
        runtime.take_over_session(
            user_id="user-a",
            session_id=previous.session_id,
            control=_round2_control(),
            request=_round2_request(
                activation_id="00000000-0000-4000-8000-000000000207",
                expected_generation=1,
                expected_media_grant_revision=1,
            ),
        )
    )
    await asyncio.wait_for(prepare_entered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not mutation_started.is_set()
    prepare_release.set()

    if outcome == "failure":
        with pytest.raises(RuntimeError, match="cas conflict"):
            await task
        assert resolutions == [(token, False)]
        media.end.assert_not_awaited()
        return

    assert await asyncio.to_thread(mutation_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    mutation_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert resolutions == [(token, True)]
    media.end.assert_awaited_once_with(previous, "takeover")
    assert runtime._local_activation_reservations == set()
    assert runtime._local_activation_keys == {}
