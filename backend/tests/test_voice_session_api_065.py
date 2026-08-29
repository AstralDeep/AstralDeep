"""Feature-065 authenticated voice session control API contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.auth import require_user_id
from orchestrator.voice_api import (
    VoiceApiError,
    VoiceHttpResult,
    _CapabilityRateLimiter,
    router,
)
from orchestrator.voice_control_binding import VoiceControlClaims


DEVICE = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"
CHAT = "00000000-0000-4000-8000-000000000003"
ACTIVATION = "00000000-0000-4000-8000-000000000004"
SESSION = "00000000-0000-4000-8000-000000000005"
BINDING_ID = "00000000-0000-4000-8000-000000000006"
REFRESH = "00000000-0000-4000-8000-000000000007"
TURN = "00000000-0000-4000-8000-000000000008"
RESULT = "result-commit-1"
BEARER = "v1." + "a" * 64 + "." + "b" * 43
NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failure: Exception | None = None

    async def _call(self, name: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((name, kwargs))
        if self.failure is not None:
            raise self.failure
        if name == "get_capability":
            return {
                "schema_version": "1",
                "status": "ready",
                "reason": "ready",
            }
        if name == "create_session":
            return VoiceHttpResult(
                {"session": _session(), "grant": {"transport": "livekit"}},
                status_code=201,
            )
        if name == "get_media_grant_state":
            return {
                "session": _session(),
                "grant_state": {
                    "transport": "livekit",
                    "media_grant_revision": 1,
                    "status": "active",
                    "expires_at": "2026-07-31T18:05:00Z",
                },
            }
        if name == "refresh_media_grant":
            return VoiceHttpResult(
                {
                    "refresh_id": REFRESH,
                    "replayed": False,
                    "replay_expires_at": "2026-07-31T18:00:30Z",
                    "session": _session(),
                    "grant": {
                        "transport": "livekit",
                        "join_token": "secret-runtime-token",
                    },
                },
                status_code=201,
            )
        if name == "consent_sensitive_recap":
            return None
        return _session()

    async def get_capability(self, **kwargs: Any) -> Any:
        return await self._call("get_capability", kwargs)

    async def create_session(self, **kwargs: Any) -> Any:
        return await self._call("create_session", kwargs)

    async def take_over_session(self, **kwargs: Any) -> Any:
        return await self._call("take_over_session", kwargs)

    async def update_session(self, **kwargs: Any) -> Any:
        return await self._call("update_session", kwargs)

    async def end_session(self, **kwargs: Any) -> None:
        await self._call("end_session", kwargs)

    async def stop_speech(self, **kwargs: Any) -> None:
        await self._call("stop_speech", kwargs)

    async def get_media_grant_state(self, **kwargs: Any) -> Any:
        return await self._call("get_media_grant_state", kwargs)

    async def refresh_media_grant(self, **kwargs: Any) -> Any:
        return await self._call("refresh_media_grant", kwargs)

    async def consent_sensitive_recap(self, **kwargs: Any) -> None:
        await self._call("consent_sensitive_recap", kwargs)


class _Orchestrator:
    def __init__(self, runtime: _Runtime) -> None:
        self.voice_runtime = runtime
        self.voice_services = runtime
        self.validations: list[dict[str, str]] = []
        self.composer_publications: list[dict[str, Any]] = []

    async def publish_voice_composer_state(self, **kwargs: Any) -> None:
        self.composer_publications.append(kwargs)

    def validate_voice_control_binding(self, **kwargs: str) -> VoiceControlClaims:
        self.validations.append(kwargs)
        if kwargs != {
            "bearer": BEARER,
            "subject": "user-a",
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
        }:
            raise RuntimeError("test binding mismatch")
        return VoiceControlClaims(
            subject="user-a",
            device_id=DEVICE,
            connection_generation=CONNECTION,
            binding_id=BINDING_ID,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )


@pytest.fixture
def api() -> tuple[TestClient, _Runtime, _Orchestrator]:
    runtime = _Runtime()
    orchestrator = _Orchestrator(runtime)
    app = FastAPI()
    app.state.orchestrator = orchestrator
    app.dependency_overrides[require_user_id] = lambda: "user-a"
    app.include_router(router)
    return TestClient(app), runtime, orchestrator


def _headers(**changes: str) -> dict[str, str]:
    values = {
        "X-Astral-Device-Id": DEVICE,
        "X-Astral-Connection-Generation": CONNECTION,
        "X-Astral-Voice-Control-Binding": BEARER,
    }
    values.update(changes)
    return values


def _capability() -> dict[str, Any]:
    return {
        "has_microphone": True,
        "has_audio_output": True,
        "microphone_permission": "authorized",
        "full_duplex": True,
        "transport": "livekit",
    }


def test_legacy_v1_capability_fails_voice_closed_on_local_deployment(api) -> None:
    client, runtime, _orchestrator = api
    runtime.speech_backend = "client_local"

    response = client.get("/api/voice/capability")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "urn:astraldeep:voice:client_contract_upgrade_required",
        "title": "Voice request could not be completed",
        "status": 503,
        "code": "client_contract_upgrade_required",
    }


def test_legacy_v1_create_never_accepts_client_backend_override(api) -> None:
    client, runtime, _orchestrator = api
    body = _create_body()
    body["speech_backend"] = "llm_factory"

    response = client.post("/api/voice/sessions", headers=_headers(), json=body)

    assert response.status_code == 400
    assert runtime.calls == []


def _create_body(**changes: Any) -> dict[str, Any]:
    values = {
        "device_id": DEVICE,
        "device_kind": "web",
        "visible_chat_id": CHAT,
        "activation_id": ACTIVATION,
        "capability": _capability(),
        "foreground_active": True,
    }
    values.update(changes)
    return values


def _session() -> dict[str, Any]:
    return {
        "session_id": SESSION,
        "device_id": DEVICE,
        "generation": 1,
        "media_grant_revision": 1,
        "state": "active",
    }


def test_only_control_plane_routes_are_exported() -> None:
    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/api/voice/capability" in paths
    assert "/api/voice/sessions" in paths
    assert "/api/voice/transcribe" not in paths
    assert "/api/voice/speak" not in paths
    assert "/api/voice/stream" not in paths
    assert "/api/voice/health" not in paths


def test_capability_is_authenticated_no_store_and_has_no_binding_requirement(api) -> None:
    client, runtime, orchestrator = api
    response = client.get("/api/voice/capability")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "ready"
    assert runtime.calls == [("get_capability", {"user_id": "user-a"})]
    assert orchestrator.validations == []


def test_capability_is_rate_limited_per_authenticated_user(api) -> None:
    client, runtime, _orchestrator = api

    for _ in range(30):
        assert client.get("/api/voice/capability").status_code == 200
    limited = client.get("/api/voice/capability")

    assert limited.status_code == 429
    assert limited.headers["cache-control"] == "no-store"
    assert limited.headers["retry-after"] == "60"
    assert limited.json() == {
        "type": "urn:astraldeep:voice:voice_rate_limited",
        "title": "Voice request could not be completed",
        "status": 429,
        "code": "voice_rate_limited",
    }
    assert len(runtime.calls) == 30


def test_capability_rate_limit_expires_and_subject_storage_is_bounded() -> None:
    now = [100.0]
    limiter = _CapabilityRateLimiter(
        limit=1,
        window_seconds=10.0,
        max_subjects=2,
        clock=lambda: now[0],
    )
    limiter.check("user-a")
    limiter.check("user-b")

    with pytest.raises(VoiceApiError, match="voice_rate_limited"):
        limiter.check("user-c")
    with pytest.raises(VoiceApiError, match="voice_rate_limited"):
        limiter.check("user-a")

    now[0] = 110.1
    limiter.check("user-c")


def test_create_validates_current_binding_and_forwards_only_safe_claims(api) -> None:
    client, runtime, orchestrator = api
    response = client.post(
        "/api/voice/sessions", headers=_headers(), json=_create_body()
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    name, kwargs = runtime.calls[-1]
    assert name == "create_session"
    assert kwargs["user_id"] == "user-a"
    assert kwargs["request"]["capability"]["transport"] == "livekit"
    assert kwargs["control"]["binding_id"] == BINDING_ID
    assert "bearer" not in kwargs["control"]
    assert orchestrator.validations[0]["bearer"] == BEARER
    assert orchestrator.composer_publications == [
        {
            "user_id": "user-a",
            "device_id": DEVICE,
            "connection_generation": CONNECTION,
            "selected_chat_id": CHAT,
        }
    ]


@pytest.mark.parametrize(
    ("body", "headers", "status"),
    (
        (_create_body(extra=True), _headers(), 400),
        (_create_body(device_id="00000000-0000-4000-8000-000000000099"), _headers(), 403),
        (_create_body(), _headers(**{"X-Astral-Voice-Control-Binding": "short"}), 403),
        (_create_body(foreground_active=False), _headers(), 400),
    ),
)
def test_create_fails_closed_on_strict_shape_or_binding_scope(
    api, body: dict[str, Any], headers: dict[str, str], status: int
) -> None:
    client, runtime, _orchestrator = api
    response = client.post("/api/voice/sessions", headers=headers, json=body)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert BEARER not in response.text
    assert runtime.calls == []


def test_takeover_carries_exact_generation_and_grant_fences(api) -> None:
    client, runtime, _orchestrator = api
    body = _create_body(expected_generation=4, expected_media_grant_revision=7)
    response = client.post(
        f"/api/voice/sessions/{SESSION}/takeover",
        headers=_headers(),
        json=body,
    )
    assert response.status_code == 200
    name, kwargs = runtime.calls[-1]
    assert name == "take_over_session"
    assert kwargs["session_id"] == SESSION
    assert kwargs["request"]["expected_generation"] == 4
    assert kwargs["request"]["expected_media_grant_revision"] == 7


def test_update_enforces_foreground_pairing_and_microphone_shutdown(api) -> None:
    client, runtime, _orchestrator = api
    invalid = client.patch(
        f"/api/voice/sessions/{SESSION}",
        headers=_headers(),
        json={
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "foreground_active": False,
            "foreground_reason": "backgrounded",
            "microphone_enabled": True,
        },
    )
    assert invalid.status_code == 400
    assert runtime.calls == []

    valid = client.patch(
        f"/api/voice/sessions/{SESSION}",
        headers=_headers(),
        json={
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "foreground_active": False,
            "foreground_reason": "backgrounded",
            "microphone_enabled": False,
        },
    )
    assert valid.status_code == 200
    assert runtime.calls[-1][0] == "update_session"


def test_fence_only_update_is_an_authenticated_lease_heartbeat(api) -> None:
    client, runtime, orchestrator = api

    response = client.patch(
        f"/api/voice/sessions/{SESSION}",
        headers=_headers(),
        json={
            "expected_generation": 1,
            "expected_media_grant_revision": 2,
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    name, kwargs = runtime.calls[-1]
    assert name == "update_session"
    assert kwargs["request"] == {
        "expected_generation": 1,
        "expected_media_grant_revision": 2,
    }
    assert kwargs["control"] == {
        "subject": "user-a",
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "binding_id": BINDING_ID,
        "binding_expires_at": NOW + timedelta(minutes=5),
    }
    assert orchestrator.validations[-1]["bearer"] == BEARER


def test_end_and_stop_are_generation_fenced_and_bodyless(api) -> None:
    client, runtime, _orchestrator = api
    ended = client.delete(
        f"/api/voice/sessions/{SESSION}",
        headers=_headers(),
        params={"expected_generation": 1, "expected_media_grant_revision": 2},
    )
    assert ended.status_code == 204
    assert ended.content == b""
    assert runtime.calls[-1][0] == "end_session"

    stopped = client.post(
        f"/api/voice/sessions/{SESSION}/speech/stop",
        headers=_headers(),
        json={"expected_generation": 1, "expected_media_grant_revision": 2},
    )
    assert stopped.status_code == 202
    assert stopped.content == b""
    assert runtime.calls[-1][0] == "stop_speech"


def test_media_grant_state_is_bearer_free_and_refresh_is_exactly_bound(api) -> None:
    client, runtime, _orchestrator = api
    state = client.get(
        f"/api/voice/sessions/{SESSION}/media-grants",
        headers=_headers(),
    )
    assert state.status_code == 200
    assert state.headers["cache-control"] == "no-store"
    assert state.json()["grant_state"]["status"] == "active"
    assert "token" not in state.text
    assert "ticket" not in state.text
    assert runtime.calls[-1][0] == "get_media_grant_state"

    refreshed = client.post(
        f"/api/voice/sessions/{SESSION}/media-grants",
        headers=_headers(),
        json={
            "refresh_id": REFRESH,
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "device_id": DEVICE,
        },
    )
    assert refreshed.status_code == 201
    assert refreshed.headers["cache-control"] == "no-store"
    name, kwargs = runtime.calls[-1]
    assert name == "refresh_media_grant"
    assert kwargs["request"] == {
        "expected_generation": 1,
        "expected_media_grant_revision": 1,
        "refresh_id": REFRESH,
        "device_id": DEVICE,
    }
    assert "bearer" not in kwargs["control"]


def test_sensitive_read_consent_is_one_result_turn_and_generation_bound(api) -> None:
    client, runtime, _orchestrator = api
    accepted = client.post(
        f"/api/voice/sessions/{SESSION}/results/{RESULT}/read-consent",
        headers=_headers(),
        json={
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "turn_id": TURN,
            "consent_method": "tap",
        },
    )
    assert accepted.status_code == 202
    assert accepted.content == b""
    name, kwargs = runtime.calls[-1]
    assert name == "consent_sensitive_recap"
    assert kwargs["session_id"] == SESSION
    assert kwargs["result_id"] == RESULT
    assert kwargs["request"]["turn_id"] == TURN

    rejected = client.post(
        f"/api/voice/sessions/{SESSION}/results/{RESULT}/read-consent",
        headers=_headers(),
        json={
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "turn_id": TURN,
            "consent_method": "voice_guess",
        },
    )
    assert rejected.status_code == 400


def test_runtime_refusals_map_to_content_free_problems(api) -> None:
    client, runtime, _orchestrator = api
    runtime.failure = VoiceApiError(
        "voice_takeover_required",
        status_code=409,
        payload={"owner": _session()},
    )
    response = client.post(
        "/api/voice/sessions", headers=_headers(), json=_create_body()
    )
    assert response.status_code == 409
    assert response.json()["code"] == "voice_takeover_required"
    assert response.json()["owner"]["session_id"] == SESSION
    assert BEARER not in response.text


def test_activation_permission_uses_the_exact_cross_client_contract_enum(api) -> None:
    client, runtime, _orchestrator = api
    body = _create_body()
    body["capability"]["microphone_permission"] = "granted"
    response = client.post(
        "/api/voice/sessions", headers=_headers(), json=body
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert runtime.calls == []


def test_missing_runtime_is_fail_closed_without_breaking_router_auth() -> None:
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace()
    app.dependency_overrides[require_user_id] = lambda: "user-a"
    app.include_router(router)
    response = TestClient(app).get("/api/voice/capability")
    assert response.status_code == 503
    assert response.json()["code"] == "voice_unavailable"
    assert response.headers["cache-control"] == "no-store"
