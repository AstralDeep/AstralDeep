"""Regression guards for physical retirement of the legacy voice proxy."""

from __future__ import annotations

import inspect

from astralprojection.resources import static_path, template_path
from orchestrator import api, voice_api


def test_only_authenticated_fixed_profile_voice_control_routes_remain() -> None:
    paths = {getattr(route, "path", "") for route in voice_api.router.routes}
    assert {
        "/api/voice/capability",
        "/api/voice/sessions",
        "/api/voice/sessions/{session_id}/takeover",
        "/api/voice/sessions/{session_id}",
        "/api/voice/sessions/{session_id}/speech/stop",
    } <= paths
    assert not {
        "/api/voice/health",
        "/api/voice/transcribe",
        "/api/voice/speak",
        "/api/voice/stream",
    }.intersection(paths)
    assert api.voice_router is voice_api.router


def test_caller_selected_models_uploads_and_realtime_proxy_are_physically_absent() -> None:
    source = inspect.getsource(api)
    assert "_legacy_voice_router" not in source
    assert "UploadFile" not in source
    assert "WebSocketDisconnect" not in source
    assert "websockets.connect" not in source
    assert "aiohttp.ClientSession" not in source
    for path in (
        "/api/voice/health",
        "/api/voice/transcribe",
        "/api/voice/speak",
        "/api/voice/stream",
    ):
        assert path not in source


def test_typed_chat_composer_remains_available_when_voice_is_unavailable() -> None:
    shell = template_path("shell.html").read_text(encoding="utf-8")
    client = static_path("client.js").read_text(encoding="utf-8")
    assert 'id="astral-input"' in shell
    assert 'id="astral-form"' in shell
    assert "Voice is unavailable. You can keep typing messages." in client
    assert "astralInput.disabled = true" not in client
