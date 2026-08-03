"""Compatibility guards for the voice proxy retired by Feature 065.

The former test module exercised three backend-owned Speaches proxy endpoints,
ambient ``SPEACHES_*`` configuration, and process-local session bookkeeping.
Feature 065 deliberately removed that data path: authenticated clients now use
the fixed-profile voice control plane while audio stays on direct RTC tracks.
These tests keep the legacy filename in the root suite and fail if the retired
HTTP/audio proxy surface is accidentally restored.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import api, voice_api


_RETIRED_PATHS = (
    "/api/voice/health",
    "/api/voice/transcribe",
    "/api/voice/speak",
    "/api/voice/stream",
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI(title="Retired voice proxy guard")
    app.include_router(api.voice_router)
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "request_kwargs"),
    (
        ("/api/voice/health", {}),
        ("/api/voice/transcribe", {"files": {"file": ("a.webm", b"audio")}}),
        ("/api/voice/speak", {"json": {"text": "do not proxy"}}),
    ),
)
def test_retired_http_audio_proxy_is_not_routable(
    client: TestClient,
    path: str,
    request_kwargs: dict[str, object],
) -> None:
    request = client.get if path.endswith("health") else client.post
    response = request(path, **request_kwargs)
    assert response.status_code == 404


def test_retired_realtime_proxy_route_is_absent() -> None:
    paths = {getattr(route, "path", "") for route in api.voice_router.routes}
    assert not set(_RETIRED_PATHS).intersection(paths)


def test_voice_router_is_the_fixed_profile_control_router() -> None:
    assert api.voice_router is voice_api.router
    paths = {getattr(route, "path", "") for route in api.voice_router.routes}
    assert {
        "/api/voice/capability",
        "/api/voice/sessions",
        "/api/voice/sessions/{session_id}/takeover",
        "/api/voice/sessions/{session_id}",
        "/api/voice/sessions/{session_id}/speech/stop",
    } <= paths


def test_legacy_proxy_dependencies_and_process_local_helpers_stay_absent() -> None:
    source = inspect.getsource(api)
    for retired_symbol in (
        "SPEACHES_URL",
        "_truncate_for_speech",
        "_active_voice_sessions",
        "_register_voice_session",
        "_cleanup_stale_voice_sessions",
        "aiohttp.ClientSession",
        "websockets.connect",
    ):
        assert retired_symbol not in source
