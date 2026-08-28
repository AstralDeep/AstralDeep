"""Authenticated strict REST-v2 client-local voice API tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.auth import require_user_id
from orchestrator.voice_api import (
    VoiceApiError,
    VoiceHttpResult,
    _capability_v2,
    _local_session_response,
    _status_v2,
    _v2_error_response,
    router,
)
from orchestrator.voice_backend import VoiceSpeechBackend
from orchestrator.voice_control_binding import (
    VoiceControlBindingError,
    VoiceControlClaims,
)


DEVICE = "00000000-0000-4000-8000-000000000101"
CONNECTION = "00000000-0000-4000-8000-000000000102"
CHAT = "00000000-0000-4000-8000-000000000103"
ACTIVATION = "00000000-0000-4000-8000-000000000104"
SESSION = "00000000-0000-4000-8000-000000000105"
BINDING = "00000000-0000-4000-8000-000000000106"
BEARER = "v1." + "a" * 64 + "." + "b" * 43
NOW = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)


class _Runtime:
    speech_backend = VoiceSpeechBackend.CLIENT_LOCAL

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_capability(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_capability", kwargs))
        return _capability_response()

    async def create_session(self, **kwargs: Any) -> VoiceHttpResult:
        self.calls.append(("create_session", kwargs))
        return VoiceHttpResult(_session_response(), status_code=201)

    async def take_over_session(self, **kwargs: Any) -> VoiceHttpResult:
        self.calls.append(("take_over_session", kwargs))
        return VoiceHttpResult(_session_response(), status_code=200)


class _Services:
    speech_backend = VoiceSpeechBackend.CLIENT_LOCAL

    def voice_status(self) -> dict[str, Any]:
        return {
            "schema_version": "2",
            "speech_backend": "client_local",
            "state": "ready",
            "reason": "ready",
            "checked_at": "2026-08-28T16:00:00Z",
        }


class _CodedRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Orchestrator:
    def __init__(self) -> None:
        self.voice_runtime = _Runtime()
        self.voice_services = _Services()
        self.publications: list[dict[str, Any]] = []

    def validate_voice_control_binding(self, **kwargs: str) -> VoiceControlClaims:
        assert kwargs == {
            "bearer": BEARER,
            "subject": "user-a",
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
        }
        return VoiceControlClaims(
            subject="user-a",
            device_id=DEVICE,
            connection_generation=CONNECTION,
            binding_id=BINDING,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )

    async def publish_voice_composer_state(self, **kwargs: Any) -> None:
        self.publications.append(kwargs)


@pytest.fixture
def api() -> tuple[TestClient, _Orchestrator]:
    orchestrator = _Orchestrator()
    app = FastAPI()
    app.state.orchestrator = orchestrator
    app.dependency_overrides[require_user_id] = lambda: "user-a"
    app.include_router(router)
    return TestClient(app), orchestrator


def _headers(**changes: str) -> dict[str, str]:
    headers = {
        "X-Astral-Device-Id": DEVICE,
        "X-Astral-Connection-Generation": CONNECTION,
        "X-Astral-Voice-Control-Binding": BEARER,
    }
    headers.update(changes)
    return headers


def _local_capability(**changes: Any) -> dict[str, Any]:
    value = {
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
    value.update(changes)
    return value


def _create_body(**changes: Any) -> dict[str, Any]:
    value = {
        "schema_version": "2",
        "activation_id": ACTIVATION,
        "device_id": DEVICE,
        "device_kind": "web",
        "visible_chat_id": CHAT,
        "foreground_active": True,
        "client_capability": _local_capability(),
    }
    value.update(changes)
    return value


def _capability_response() -> dict[str, Any]:
    return {
        "schema_version": "2",
        "speech_backend": "client_local",
        "status": "requires_client_readiness",
        "reason": "client_readiness_required",
        "checked_at": "2026-08-28T16:00:00Z",
        "expires_at": "2026-08-28T16:00:30Z",
        "supported_transports": ["client_local"],
        "requirements": {
            "session_contract": "voice-rest/v2-client-local",
            "local_frame_contract": "client_local/v1",
            "configured_locale": "en-US",
            "recognition_must_be_local": True,
            "synthesis_must_be_local": True,
            "installation_policy": "explicit_user_action_only",
            "requirement_revision": 1,
            "max_final_unicode_scalars": 8000,
            "max_announcement_utf8_bytes": 600,
            "announcement_ttl_seconds": 10,
            "echo_suppression_milliseconds": 500,
        },
    }


def _session_response() -> dict[str, Any]:
    return {
        "schema_version": "2",
        "session_id": SESSION,
        "speech_backend": "client_local",
        "transport": "client_local",
        "generation": 1,
        "speech_revision": 1,
        "state": "starting",
        "visible_chat_id": CHAT,
        "chat_context_revision": 1,
        "applied_chat_context_revision": None,
        "chat_context_synced": False,
        "foreground_active": True,
        "microphone_enabled": True,
        "speech_muted": False,
        "configured_locale": "en-US",
        "idle_expires_at": "2026-08-28T16:05:00Z",
    }


def test_v2_capability_and_status_are_authenticated_bounded_and_no_store(api) -> None:
    client, _ = api

    capability = client.get("/api/voice/v2/capability")
    status = client.get("/api/voice/v2/status")

    assert capability.status_code == status.status_code == 200
    assert capability.headers["cache-control"] == "no-store"
    assert status.headers["cache-control"] == "no-store"
    assert capability.json() == _capability_response()
    assert status.json() == _Services().voice_status()
    serialized = (capability.text + status.text).lower()
    assert all(
        forbidden not in serialized
        for forbidden in ("endpoint", "credential", "api_key", "engine", "model")
    )


def test_remote_v2_projection_and_invalid_selection_use_closed_v2_shapes(api) -> None:
    capability = _capability_v2(
        {
            "schema_version": "1",
            "status": "ready",
            "reason": "ready",
            "checked_at": "2026-08-28T16:00:00Z",
            "expires_at": "2026-08-28T16:00:30Z",
            "supported_transports": ["livekit", "watch_pcm_websocket"],
        },
        VoiceSpeechBackend.LLM_FACTORY,
    )
    status = _status_v2(
        {"ready": True, "reason": "ready"},
        VoiceSpeechBackend.LLM_FACTORY,
    )
    assert capability == {
        **capability,
        "schema_version": "2",
        "speech_backend": "llm_factory",
        "status": "ready",
        "reason": "ready",
    }
    assert status["schema_version"] == "2"
    assert status["speech_backend"] == "llm_factory"
    assert status["state"] == status["reason"] == "ready"
    assert capability["requirements"]["session_contract"] == "voice-rest/v1"
    assert capability["requirements"]["local_frame_contract"] is None

    client, orchestrator = api
    orchestrator.voice_services.speech_backend = None
    for path in ("/api/voice/v2/capability", "/api/voice/v2/status"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json() == {
            "error": "voice_unavailable",
            "reason": "backend_selection_invalid",
            "retryable": False,
        }


@pytest.mark.parametrize("selector", [None, "unknown_backend"])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/voice/v2/capability", None),
        ("get", "/api/voice/v2/status", None),
        ("post", "/api/voice/v2/sessions", _create_body()),
        (
            "post",
            f"/api/voice/v2/sessions/{SESSION}/takeover",
            _create_body(
                expected_generation=1,
                expected_speech_revision=1,
            ),
        ),
    ],
)
def test_every_v2_route_maps_invalid_selector_to_503(
    api,
    selector: object,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    client, orchestrator = api
    orchestrator.voice_runtime.speech_backend = selector
    orchestrator.voice_services.speech_backend = selector

    response = client.request(method, path, headers=_headers(), json=body)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "error": "voice_unavailable",
        "reason": "backend_selection_invalid",
        "retryable": False,
    }


def test_v2_routes_keep_keycloak_authentication_dependency() -> None:
    app = FastAPI()
    app.state.orchestrator = _Orchestrator()
    app.include_router(router)
    client = TestClient(app)

    for response in (
        client.get("/api/voice/v2/capability"),
        client.post("/api/voice/v2/sessions", json=_create_body()),
    ):
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "error": "voice_unavailable",
            "reason": "authentication_required",
            "retryable": False,
        }


def test_v1_authentication_error_shape_is_unchanged() -> None:
    app = FastAPI()
    app.state.orchestrator = _Orchestrator()
    app.include_router(router)
    response = TestClient(app).get("/api/voice/capability")

    assert response.status_code == 401
    assert response.headers.get("cache-control") is None
    assert response.json() == {"detail": "Not authenticated"}


def test_v2_create_is_strict_header_bound_and_has_no_media_grant(api) -> None:
    client, orchestrator = api

    response = client.post(
        "/api/voice/v2/sessions",
        headers=_headers(),
        json=_create_body(),
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == _session_response()
    assert "grant" not in response.json()
    name, call = orchestrator.voice_runtime.calls[-1]
    assert name == "create_session"
    assert call["control"]["device_id"] == DEVICE
    assert call["request"]["capability"]["transport"] == "client_local"


def test_v2_exact_create_replay_and_takeover_reason_use_frozen_shapes(api) -> None:
    client, orchestrator = api

    async def replay(**kwargs: Any) -> VoiceHttpResult:
        orchestrator.voice_runtime.calls.append(("create_session", kwargs))
        return VoiceHttpResult(_session_response(), status_code=200)

    orchestrator.voice_runtime.create_session = replay  # type: ignore[method-assign]
    response = client.post(
        "/api/voice/v2/sessions",
        headers=_headers(),
        json=_create_body(),
    )
    assert response.status_code == 200
    assert response.json() == _session_response()
    assert response.headers["cache-control"] == "no-store"

    takeover = _v2_error_response(
        VoiceApiError("voice_takeover_required", status_code=409)
    )
    assert takeover.status_code == 409
    assert json.loads(takeover.body)["reason"] == "takeover_required"


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("stale_generation", "stale_session"),
        ("stale_media_grant_revision", "stale_speech_revision"),
        ("stale_chat_context_revision", "stale_chat_context"),
        ("activation_id_payload_mismatch", "invalid_binding"),
    ],
)
def test_v2_error_response_has_exact_closed_conflict_mapping(
    code: str,
    reason: str,
) -> None:
    response = _v2_error_response(_CodedRuntimeError(code))

    assert response.status_code == 409
    assert json.loads(response.body) == {
        "error": "voice_unavailable",
        "reason": reason,
        "retryable": False,
    }


@pytest.mark.parametrize(
    ("route", "runtime_method", "code", "reason"),
    [
        (
            "/api/voice/v2/sessions",
            "create_session",
            "activation_id_payload_mismatch",
            "invalid_binding",
        ),
        (
            f"/api/voice/v2/sessions/{SESSION}/takeover",
            "take_over_session",
            "stale_media_grant_revision",
            "stale_speech_revision",
        ),
    ],
)
def test_real_v2_mutation_routes_apply_exact_repository_conflict_mapping(
    api,
    route: str,
    runtime_method: str,
    code: str,
    reason: str,
) -> None:
    client, orchestrator = api

    async def fail(**_kwargs: Any) -> VoiceHttpResult:
        raise _CodedRuntimeError(code)

    setattr(orchestrator.voice_runtime, runtime_method, fail)
    body = _create_body()
    if runtime_method == "take_over_session":
        body.update(expected_generation=1, expected_speech_revision=1)

    response = client.post(route, headers=_headers(), json=body)

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["reason"] == reason


@pytest.mark.parametrize(
    "mutation",
    [
        {"speech_backend": "llm_factory"},
        {"unknown": True},
        {"schema_version": 2},
        {"foreground_active": False},
    ],
)
def test_v2_create_rejects_overrides_unknown_fields_and_non_strict_values(
    api,
    mutation: dict[str, Any],
) -> None:
    client, orchestrator = api

    response = client.post(
        "/api/voice/v2/sessions",
        headers=_headers(),
        json=_create_body(**mutation),
    )

    assert response.status_code == 400
    assert orchestrator.voice_runtime.calls == []


def test_v2_create_rejects_body_header_device_mismatch_without_enumeration(api) -> None:
    client, orchestrator = api
    other = "00000000-0000-4000-8000-000000000199"

    response = client.post(
        "/api/voice/v2/sessions",
        headers=_headers(**{"X-Astral-Device-Id": other}),
        json=_create_body(),
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "invalid_binding"
    assert SESSION not in response.text
    assert orchestrator.voice_runtime.calls == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("recognition_processing", "unsupported", "local_processing_not_guaranteed"),
        ("synthesis_processing", "unavailable", "local_synthesis_unavailable"),
        ("recognition_installation", "downloadable", "local_language_download_required"),
        ("microphone_permission", "denied", "microphone_permission_denied"),
    ],
)
def test_v2_create_returns_stable_eligibility_reason(
    api,
    field: str,
    value: str,
    reason: str,
) -> None:
    client, orchestrator = api
    body = _create_body(
        client_capability=_local_capability(**{field: value})
    )

    response = client.post(
        "/api/voice/v2/sessions", headers=_headers(), json=body
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "voice_unavailable",
        "reason": reason,
        "retryable": False,
    }
    assert orchestrator.voice_runtime.calls == []


def test_v2_takeover_translates_speech_revision_without_media(api) -> None:
    client, orchestrator = api
    body = _create_body(
        expected_generation=1,
        expected_speech_revision=1,
    )

    response = client.post(
        f"/api/voice/v2/sessions/{SESSION}/takeover",
        headers=_headers(),
        json=body,
    )

    assert response.status_code == 200
    assert response.json() == _session_response()
    name, call = orchestrator.voice_runtime.calls[-1]
    assert name == "take_over_session"
    assert call["request"]["expected_media_grant_revision"] == 1
    assert "expected_speech_revision" not in call["request"]


def test_v2_create_refuses_remote_selected_backend(api) -> None:
    client, orchestrator = api
    orchestrator.voice_runtime.speech_backend = VoiceSpeechBackend.LLM_FACTORY
    orchestrator.voice_services.speech_backend = VoiceSpeechBackend.LLM_FACTORY

    response = client.post(
        "/api/voice/v2/sessions", headers=_headers(), json=_create_body()
    )

    assert response.status_code == 409
    assert response.json()["reason"] == "backend_mismatch"
    assert orchestrator.voice_runtime.calls == []


def test_v2_runtime_projection_and_non_enumerating_error_fallbacks() -> None:
    legacy_runtime_session = {
        "session_id": SESSION,
        "speech_backend": "client_local",
        "media_grant_revision": 3,
        "generation": 2,
        "state": "active",
        "visible_chat_id": CHAT,
        "chat_context_revision": 4,
        "applied_chat_context_revision": 4,
        "chat_context_synced": True,
        "foreground_active": True,
        "microphone_enabled": True,
        "speech_muted": False,
        "idle_expires_at": "2026-08-28T16:05:00Z",
    }
    response = _local_session_response(legacy_runtime_session, default_status=201)
    projected = json.loads(response.body)
    assert response.status_code == 201
    assert projected["schema_version"] == "2"
    assert projected["speech_revision"] == 3
    assert projected["transport"] == "client_local"
    assert response.headers["cache-control"] == "no-store"

    bound = _v2_error_response(VoiceControlBindingError("binding_scope_mismatch"))
    assert bound.status_code == 403
    assert json.loads(bound.body)["reason"] == "invalid_binding"

    unknown = _v2_error_response(RuntimeError("internal detail must not enumerate"))
    assert unknown.status_code == 503
    assert json.loads(unknown.body) == {
        "error": "voice_unavailable",
        "reason": "internal_error",
        "retryable": False,
    }


def test_v2_session_admission_enforces_the_real_twelfth_request_limit(api) -> None:
    client, orchestrator = api
    responses = [
        client.post(
            "/api/voice/v2/sessions",
            headers=_headers(),
            json=_create_body(),
        )
        for _ in range(13)
    ]
    assert all(response.status_code == 201 for response in responses[:12])
    limited = responses[12]
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert limited.headers["cache-control"] == "no-store"
    assert limited.json() == {
        "error": "voice_unavailable",
        "reason": "capacity_exhausted",
        "retryable": True,
    }
    assert len(orchestrator.voice_runtime.calls) == 12
