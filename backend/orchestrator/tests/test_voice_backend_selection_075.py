"""Immutable deployment speech-backend selection for Feature 075."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.api import chat_router
from orchestrator.auth import require_user_id
from orchestrator.voice_api import VoiceApiError
from orchestrator.voice_api import router as voice_router
from orchestrator.voice_backend import (
    SpeechBackendSelection,
    VoiceSpeechBackend,
    backend_value,
)
from orchestrator.voice_bootstrap import VoiceBootstrapError, build_voice_services
from orchestrator.voice_runtime import VoiceSessionRuntime
from shared.feature_flags import flags


class _PlaneRuntime:
    repositories = SimpleNamespace(voice=object())

    def transaction(self):  # pragma: no cover - construction must not transact.
        raise AssertionError("unexpected database access")


class _TypedHistory:
    def create_chat(self, *, user_id: str, agent_id: str | None = None) -> str:
        assert user_id == "user-a"
        assert agent_id is None
        return "75000000-0000-4000-8000-000000000099"


@pytest.mark.parametrize(
    ("environment", "voice_status", "voice_reason"),
    (
        (
            {"ASTRAL_ENV": "test", "VOICE_SPEECH_BACKEND": ""},
            503,
            "backend_selection_invalid",
        ),
        (
            {"ASTRAL_ENV": "test", "VOICE_SPEECH_BACKEND": "client_local"},
            200,
            "feature_disabled",
        ),
    ),
)
def test_typed_chat_api_remains_available_when_voice_is_closed(
    monkeypatch,
    environment: dict[str, str],
    voice_status: int,
    voice_reason: str,
) -> None:
    monkeypatch.setitem(flags._flags, "conversational_voice", False)
    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime.repositories,
        environ=environment,
    )
    orchestrator = SimpleNamespace(
        history=_TypedHistory(),
        voice_services=services,
        voice_runtime=services.runtime,
    )
    app = FastAPI()
    app.state.orchestrator = orchestrator
    app.dependency_overrides[require_user_id] = lambda: "user-a"
    app.include_router(chat_router)
    app.include_router(voice_router)

    with TestClient(app) as client:
        typed = client.post("/api/chats", json={})
        voice = client.get("/api/voice/v2/capability")

    assert typed.status_code == 201
    assert typed.json()["chat_id"] == "75000000-0000-4000-8000-000000000099"
    assert typed.json()["agent_id"] is None
    assert voice.status_code == voice_status
    assert voice.json()["reason"] == voice_reason


def test_missing_selector_preserves_exact_legacy_default() -> None:
    selection = SpeechBackendSelection.from_environ({})

    assert selection.value is VoiceSpeechBackend.LLM_FACTORY
    assert selection.valid is True
    assert selection.source == "legacy_default"


@pytest.mark.parametrize("value", ["llm_factory", "client_local"])
def test_only_exact_selector_values_are_accepted(value: str) -> None:
    selection = SpeechBackendSelection.from_environ(
        {"VOICE_SPEECH_BACKEND": value}
    )

    assert selection.value.value == value
    assert selection.valid is True
    assert selection.source == "explicit"


@pytest.mark.parametrize(
    "value",
    ["", " ", "LLM_FACTORY", "client-local", "remote", "client_local\n"],
)
def test_blank_unknown_or_malformed_selector_fails_voice_closed(value: str) -> None:
    selection = SpeechBackendSelection.from_environ(
        {"VOICE_SPEECH_BACKEND": value}
    )

    assert selection.value is None
    assert selection.valid is False
    assert selection.source == "explicit"
    assert repr(selection) == (
        "SpeechBackendSelection(value=None, valid=False, source='explicit')"
    )


def test_selection_is_frozen_and_independent_of_later_environment_mutation() -> None:
    environ = {"VOICE_SPEECH_BACKEND": "client_local"}
    selection = SpeechBackendSelection.from_environ(environ)
    environ["VOICE_SPEECH_BACKEND"] = "llm_factory"

    assert selection.value is VoiceSpeechBackend.CLIENT_LOCAL
    with pytest.raises(AttributeError):
        selection.value = VoiceSpeechBackend.LLM_FACTORY  # type: ignore[misc]


def test_selector_value_object_rejects_impossible_states_and_aliases() -> None:
    with pytest.raises(ValueError, match="invalid_speech_backend_source"):
        SpeechBackendSelection(
            value=VoiceSpeechBackend.LLM_FACTORY,
            valid=True,
            source="request",
        )
    with pytest.raises(ValueError, match="invalid_speech_backend_selection"):
        SpeechBackendSelection(value=None, valid=True, source="explicit")
    with pytest.raises(ValueError, match="invalid_speech_backend_default"):
        SpeechBackendSelection(
            value=VoiceSpeechBackend.CLIENT_LOCAL,
            valid=True,
            source="legacy_default",
        )
    assert backend_value(VoiceSpeechBackend.CLIENT_LOCAL) is (
        VoiceSpeechBackend.CLIENT_LOCAL
    )
    assert backend_value("client-local") is None


def test_client_local_bootstrap_constructs_no_remote_media_or_worker(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("client_local constructed a remote dependency")

    monkeypatch.setattr("orchestrator.voice_bootstrap.LiveKitService", forbidden)
    monkeypatch.setattr("orchestrator.voice_bootstrap.WorkerPool", forbidden)
    monkeypatch.setattr("orchestrator.voice_bootstrap.DirectRtcVoiceMedia", forbidden)

    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime.repositories,
        environ={
            "ASTRAL_ENV": "test",
            "VOICE_SPEECH_BACKEND": "client_local",
        },
    )

    assert services.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
    assert services.livekit is None
    assert services.worker_pool is None
    assert services.worker_endpoint is None


def test_invalid_selection_returns_unavailable_services_without_remote_construction(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "orchestrator.voice_bootstrap.LiveKitService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid selection constructed remote media")
        ),
    )

    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime.repositories,
        environ={"ASTRAL_ENV": "test", "VOICE_SPEECH_BACKEND": ""},
    )

    assert services.speech_backend is None
    assert services.runtime is None
    assert services.voice_status()["reason"] == "backend_selection_invalid"


def test_bootstrap_shares_one_immutable_process_selection() -> None:
    environment = {
        "ASTRAL_ENV": "test",
        "VOICE_SPEECH_BACKEND": "client_local",
    }
    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime.repositories,
        environ=environment,
    )
    environment["VOICE_SPEECH_BACKEND"] = "llm_factory"

    assert services.backend_selection is services.runtime.backend_selection
    assert services.backend_selection.value is VoiceSpeechBackend.CLIENT_LOCAL
    assert services.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
    assert services.runtime.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
    with pytest.raises(AttributeError, match="speech_backend_immutable"):
        services.speech_backend = VoiceSpeechBackend.LLM_FACTORY
    with pytest.raises(AttributeError, match="speech_backend_immutable"):
        services.runtime.speech_backend = VoiceSpeechBackend.LLM_FACTORY


def test_service_assembly_rejects_distinct_runtime_selection_authority() -> None:
    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime.repositories,
        environ={
            "ASTRAL_ENV": "test",
            "VOICE_SPEECH_BACKEND": "client_local",
        },
    )
    distinct = SpeechBackendSelection.from_environ(
        {"VOICE_SPEECH_BACKEND": "client_local"}
    )
    assert distinct is not services.runtime.backend_selection

    with pytest.raises(ValueError, match="mismatched_runtime_backend_selection"):
        replace(services, backend_selection=distinct)


def test_http_api_rejects_half_shared_backend_authority() -> None:
    selection = SpeechBackendSelection.from_environ(
        {"VOICE_SPEECH_BACKEND": "client_local"}
    )
    runtime = SimpleNamespace(
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        get_capability=AsyncMock(),
    )
    services = SimpleNamespace(
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        backend_selection=selection,
    )
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace(
        voice_runtime=runtime,
        voice_services=services,
    )
    app.dependency_overrides[require_user_id] = lambda: "user-a"
    app.include_router(voice_router)

    with TestClient(app) as client:
        response = client.get("/api/voice/v2/capability")

    assert response.status_code == 503
    assert response.json()["reason"] == "backend_selection_invalid"
    runtime.get_capability.assert_not_awaited()


@pytest.mark.parametrize(
    "broken_capability",
    (SimpleNamespace(), SimpleNamespace(feature_enabled=True)),
)
def test_missing_local_kill_switch_gate_fails_closed(broken_capability) -> None:
    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime.repositories,
        environ={
            "ASTRAL_ENV": "test",
            "VOICE_SPEECH_BACKEND": "client_local",
        },
    )
    services.capability = broken_capability

    assert services.voice_status()["reason"] == "feature_disabled"
    with pytest.raises(VoiceBootstrapError, match="feature_disabled"):
        services._require_local_backend()


@pytest.mark.asyncio
async def test_client_local_honors_voice_kill_switch(monkeypatch) -> None:
    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime.repositories,
        environ={
            "ASTRAL_ENV": "test",
            "VOICE_SPEECH_BACKEND": "client_local",
        },
    )
    monkeypatch.setitem(flags._flags, "conversational_voice", False)
    drain = AsyncMock()
    services.runtime._drain_pending_local_activation_cleanup = drain

    capability = await services.runtime.get_capability(user_id="user-a")

    assert capability["status"] == "unavailable"
    assert capability["reason"] == "feature_disabled"
    assert services.voice_status()["reason"] == "feature_disabled"
    with pytest.raises(VoiceApiError, match="feature_disabled"):
        await services.runtime._require_ready()
    with pytest.raises(VoiceBootstrapError, match="feature_disabled"):
        services._require_local_backend()
    drain.assert_not_awaited()


@pytest.mark.asyncio
async def test_kill_switch_blocks_every_local_speech_entrypoint(monkeypatch) -> None:
    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime.repositories,
        environ={
            "ASTRAL_ENV": "test",
            "VOICE_SPEECH_BACKEND": "client_local",
        },
    )
    monkeypatch.setitem(flags._flags, "conversational_voice", False)
    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    calls = (
        services.local_ready(
            socket_id=1,
            current_socket_id=1,
            user_id="user-a",
            claims=None,
            frame=None,
            now=now,
        ),
        services.complete_local_ready_delivery(
            None,
            socket_id=1,
            current_socket_id=1,
            user_id="user-a",
            claims=None,
            frame=None,
            now=now,
            authority_is_current=lambda: True,
        ),
        services.bind_local_recognition(
            socket_id=1,
            current_socket_id=1,
            user_id="user-a",
            claims=None,
            frame=None,
            execution_base_render_revision=0,
            now=now,
        ),
        services.verify_local_final_authority(
            socket_id=1,
            current_socket_id=1,
            user_id="user-a",
            frame=None,
            now=now,
        ),
        services.handle_local_playout(
            user_id="user-a",
            claims=None,
            event=None,
            now=now,
        ),
        services._publish_local_announcement(None, kind="greeting"),
    )

    for call in calls:
        with pytest.raises(VoiceBootstrapError, match="feature_disabled"):
            await call


@pytest.mark.asyncio
async def test_nonterminal_old_backend_row_is_rejected_before_media_work() -> None:
    now = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    session = SimpleNamespace(
        speech_backend="llm_factory",
        session_id="00000000-0000-4000-8000-000000000121",
        generation=1,
    )
    repository = SimpleNamespace(get_controlled_session=Mock(return_value=session))
    media = SimpleNamespace(barge_in=AsyncMock())
    runtime = VoiceSessionRuntime(
        repository=repository,
        capability=SimpleNamespace(),
        media=media,
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
    )
    cleanup = AsyncMock()
    stop = AsyncMock()
    runtime.bind_local_buffer_cleanup_handler(cleanup)
    runtime.bind_speech_stop_handler(stop)

    with pytest.raises(VoiceApiError, match="backend_mismatch"):
        await runtime.stop_speech(
            user_id="user-a",
            session_id=session.session_id,
            control={
                "device_id": "00000000-0000-4000-8000-000000000123",
                "connection_generation": (
                    "00000000-0000-4000-8000-000000000124"
                ),
                "binding_id": "00000000-0000-4000-8000-000000000125",
                "binding_expires_at": now + timedelta(minutes=5),
            },
            request={
                "expected_generation": 1,
                "expected_media_grant_revision": 1,
            },
        )

    cleanup.assert_not_awaited()
    stop.assert_not_awaited()
    media.barge_in.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_old_backend_cleanup_skips_incompatible_media() -> None:
    session = SimpleNamespace(
        speech_backend="llm_factory",
        session_id="00000000-0000-4000-8000-000000000122",
        generation=1,
    )
    media = SimpleNamespace(end=AsyncMock())
    runtime = VoiceSessionRuntime(
        repository=SimpleNamespace(),
        capability=SimpleNamespace(),
        media=media,
        replica_id="replica-a",
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
    )
    ended = AsyncMock()
    runtime.bind_session_end_handler(ended)

    await runtime._cleanup_ended_session(session, "shutdown")

    media.end.assert_not_awaited()
    ended.assert_awaited_once_with(session, "shutdown")
