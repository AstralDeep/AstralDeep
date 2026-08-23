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


def test_legacy_speech_server_env_is_gone_from_the_orchestrator() -> None:
    """The retired dedicated speech-server URL setting has no reader left.

    The ``rote_config`` handshake used to report ``speech_server_available``
    from an ambient ``SPEACHES_URL`` value the orchestrator never consumed
    otherwise. The worker's speech endpoint is ``VOICE_SPEECH_BASE_URL`` and
    this process must not grow a second, unused speech-server knob back.
    """
    from orchestrator import orchestrator as orchestrator_module

    source = inspect.getsource(orchestrator_module)
    assert "SPEACHES_URL" not in source
    assert 'os.getenv("SPEACHES' not in source


def _bare_orchestrator():
    from orchestrator.orchestrator import Orchestrator

    return Orchestrator.__new__(Orchestrator)


class _WorkerPool:
    def __init__(self, worker_count: int) -> None:
        self._worker_count = worker_count
        self.calls = 0

    def readiness(self):
        from orchestrator.voice_coordinator import WorkerPoolReadiness

        self.calls += 1
        return WorkerPoolReadiness(
            ready=self._worker_count > 0,
            reason="ready" if self._worker_count else "worker_unavailable",
            worker_count=self._worker_count,
            capacity_total=self._worker_count,
            capacity_available=self._worker_count,
        )


@pytest.mark.parametrize(
    ("flag_on", "runtime_built", "worker_count", "expected"),
    (
        (True, True, 1, True),
        (True, True, 0, False),
        (True, False, 1, False),
        (False, True, 1, False),
    ),
)
def test_speech_server_available_reflects_the_voice_runtime(
    monkeypatch: pytest.MonkeyPatch,
    flag_on: bool,
    runtime_built: bool,
    worker_count: int,
    expected: bool,
) -> None:
    """``speech_server_available`` keeps its wire name but answers from voice.

    True only when ``FF_CONVERSATIONAL_VOICE`` is on, the voice services were
    constructed, and a preflight-gated speech worker is live — never from an
    ambient URL.
    """
    from orchestrator import orchestrator as orchestrator_module

    monkeypatch.setattr(
        orchestrator_module.flags,
        "is_enabled",
        lambda name: flag_on if name == "conversational_voice" else False,
    )
    monkeypatch.setenv("SPEACHES_URL", "http://speech.invalid")
    orch = _bare_orchestrator()
    orch.voice_runtime = object() if runtime_built else None
    orch.voice_worker_pool = _WorkerPool(worker_count) if runtime_built else None

    assert orch.speech_server_available() is expected


def test_speech_server_available_fails_closed_on_an_unconstructed_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process whose voice bootstrap failed reports no speech server."""
    from orchestrator import orchestrator as orchestrator_module

    # Pin the flag ON so the assertion exercises the missing-runtime branch,
    # not the flag branch (FF_CONVERSATIONAL_VOICE is not ambient-stripped).
    monkeypatch.setattr(orchestrator_module.flags, "is_enabled", lambda name: True)
    orch = _bare_orchestrator()
    assert orch.speech_server_available() is False


def test_speech_server_available_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import orchestrator as orchestrator_module

    monkeypatch.setattr(
        orchestrator_module.flags, "is_enabled", lambda name: True
    )
    orch = _bare_orchestrator()
    orch.voice_runtime = object()

    class _Broken:
        def readiness(self):
            raise RuntimeError("pool exploded")

    orch.voice_worker_pool = _Broken()
    assert orch.speech_server_available() is False
