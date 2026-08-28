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
        capability=SimpleNamespace(),
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
        capability=SimpleNamespace(),
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
        capability=SimpleNamespace(),
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
        capability=SimpleNamespace(),
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
        capability=SimpleNamespace(),
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
    await runtime._reconcile_local_activation_abort(abandoned, abandoned_create)
    assert len(runtime._pending_local_activation_cleanup) == 1

    await runtime._require_ready()
    assert runtime._pending_local_activation_cleanup == {}
    assert attempts == 2


@pytest.mark.asyncio
async def test_local_activation_cleanup_capacity_fails_closed_before_insert() -> None:
    repository = SimpleNamespace(create_session=Mock())
    runtime = VoiceSessionRuntime(
        repository=repository,  # type: ignore[arg-type]
        capability=SimpleNamespace(),
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
    for generation in range(1, 257):
        session = SimpleNamespace(
            **{
                **_round2_session(generation=generation).__dict__,
                "session_id": f"pending-{generation}",
            }
        )
        runtime._pending_local_activation_cleanup[
            (session.session_id, generation)
        ] = (session, create)
    runtime._abort_activation = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(VoiceApiError, match="local_cleanup_capacity_exhausted"):
        await runtime.create_session(
            user_id="user-a",
            control=_round2_control(),
            request=_round2_request(),
        )
    repository.create_session.assert_not_called()
    assert len(runtime._pending_local_activation_cleanup) == 256


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
