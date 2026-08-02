"""Authenticated direct-runtime wiring for Feature-065 playout evidence."""

from __future__ import annotations

import json
import types
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import Orchestrator
from orchestrator.voice_control_binding import VoiceControlClaims
from shared.protocol import VoicePlayoutEvent


DEVICE = "10000000-0000-4000-8000-000000000001"
CONNECTION = "20000000-0000-4000-8000-000000000001"
SESSION = "30000000-0000-4000-8000-000000000001"
TURN = "40000000-0000-4000-8000-000000000001"
ANNOUNCEMENT = "50000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _event(**changes: object) -> VoicePlayoutEvent:
    values: dict[str, object] = {
        "type": "voice_playout_event",
        "schema_version": "1",
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "session_id": SESSION,
        "generation": 1,
        "media_grant_revision": 1,
        "announcement_id": ANNOUNCEMENT,
        "announcement_sequence": 1,
        "turn_id": TURN,
        "kind": "acknowledgement",
        "quantum_role": "single",
        "quantum_index": 0,
        "phase": "started",
        "client_sequence": 1,
        "observed_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    values.update(changes)
    return VoicePlayoutEvent.from_dict(values)


def _claims(**changes: object) -> VoiceControlClaims:
    values: dict[str, object] = {
        "subject": "voice-owner",
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "binding_id": "60000000-0000-4000-8000-000000000001",
        "issued_at": datetime.now(UTC) - timedelta(minutes=1),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    values.update(changes)
    return VoiceControlClaims(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_playout_frame_bypasses_operation_admission() -> None:
    raw = _event().to_json()
    websocket = object()
    handled: list[str] = []
    fake = SimpleNamespace()
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame
    fake._ui_control_kind = Orchestrator._ui_control_kind

    async def run_ui_control(_context, value):
        handled.append(value)

    async def forbidden_enqueue(*_args, **_kwargs):
        raise AssertionError("playout evidence entered operation admission")

    fake._run_ui_control = run_ui_control
    fake._enqueue_connection_frame = forbidden_enqueue
    context = SimpleNamespace(
        registered=True,
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        closing=False,
    )

    assert Orchestrator._ui_control_kind(json.loads(raw)) == (
        "voice_playout_event"
    )
    assert await Orchestrator._route_ui_frame(fake, context, raw)
    assert handled == [raw]


@pytest.mark.asyncio
async def test_authenticated_playout_routes_without_audit_or_error_frame() -> None:
    websocket = object()
    event = _event()
    binding = _claims()
    accepted: list[dict[str, object]] = []
    sent: list[str] = []

    class Services:
        async def handle_client_playout(self, **kwargs):
            accepted.append(kwargs)

    fake = SimpleNamespace(
        ui_sessions={websocket: {"sub": "voice-owner"}},
        _voice_control_bindings={id(websocket): binding},
        _voice_device_bindings={("voice-owner", DEVICE): id(websocket)},
        voice_services=Services(),
    )
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame
    fake._handle_voice_playout_event = types.MethodType(
        Orchestrator._handle_voice_playout_event,
        fake,
    )

    async def safe_send(_websocket, data):
        sent.append(data)
        return True

    fake._safe_send = safe_send
    await Orchestrator.handle_ui_message(fake, websocket, event.to_json())

    assert accepted == [
        {
            "user_id": "voice-owner",
            "claims": binding,
            "event": event,
        }
    ]
    assert sent == []


@pytest.mark.asyncio
async def test_wrong_socket_and_malformed_playout_fail_content_free() -> None:
    websocket = object()
    event = _event()
    binding = _claims()
    calls = 0
    sent: list[str] = []

    class Services:
        async def handle_client_playout(self, **_kwargs):
            nonlocal calls
            calls += 1

    fake = SimpleNamespace(
        ui_sessions={websocket: {"sub": "voice-owner"}},
        _voice_control_bindings={id(websocket): binding},
        _voice_device_bindings={("voice-owner", DEVICE): id(websocket) + 1},
        voice_services=Services(),
    )
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame
    fake._handle_voice_playout_event = types.MethodType(
        Orchestrator._handle_voice_playout_event,
        fake,
    )

    async def safe_send(_websocket, data):
        sent.append(data)
        return True

    fake._safe_send = safe_send
    await Orchestrator.handle_ui_message(fake, websocket, event.to_json())

    malformed = json.loads(event.to_json())
    malformed["transcript"] = "must never be accepted or logged"
    await Orchestrator.handle_ui_message(
        fake,
        websocket,
        json.dumps(malformed),
    )
    assert calls == 0
    assert sent == []
