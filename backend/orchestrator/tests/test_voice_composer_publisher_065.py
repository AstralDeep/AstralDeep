"""Server-owned composer projections after registration and REST mutations."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.voice_runtime import VoiceSessionRuntime
from orchestrator.orchestrator import Orchestrator
from orchestrator.voice_control_binding import VoiceControlClaims


DEVICE = "00000000-0000-4000-8000-000000000001"
OTHER_DEVICE = "00000000-0000-4000-8000-000000000002"
CONNECTION = "00000000-0000-4000-8000-000000000003"
CHAT = "00000000-0000-4000-8000-000000000004"
SESSION = "00000000-0000-4000-8000-000000000005"
NOW = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)


class _Capability:
    def __init__(self, status: str = "ready", reason: str = "ready") -> None:
        self.value = SimpleNamespace(status=status, reason=reason)

    async def readiness(self):
        return self.value


class _Repository:
    def __init__(self) -> None:
        self.session = None

    def get_live_session(self, *, user_id: str):
        assert user_id == "user-a"
        return self.session


def _runtime() -> tuple[VoiceSessionRuntime, _Repository, _Capability]:
    repository = _Repository()
    capability = _Capability()
    return (
        VoiceSessionRuntime(
            repository=repository,
            capability=capability,
            media=SimpleNamespace(),
            replica_id="voice-test-replica",
        ),
        repository,
        capability,
    )


def _session(**changes):
    values = {
        "session_id": SESSION,
        "device_id": DEVICE,
        "device_kind": "web",
        "state": "active",
        "generation": 2,
        "media_grant_revision": 3,
        "visible_chat_id": CHAT,
        "chat_context_revision": 4,
        "applied_chat_context_revision": 4,
        "speech_muted": False,
        "microphone_enabled": True,
        "foreground_active": True,
        "foreground_reason": "foreground",
        "idle_expires_at": NOW + timedelta(minutes=5),
        "end_reason": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_ready_ownerless_projection_enables_only_start() -> None:
    runtime, _repository, _capability = _runtime()
    frame = await runtime.get_composer_state(
        user_id="user-a",
        device_id=DEVICE,
        device_kind="web",
        connection_generation=CONNECTION,
        selected_chat_id=CHAT,
        revision=0,
    )

    assert frame["voice"]["state"] == "off"
    assert frame["voice"]["available"] is True
    visible = [item["action"] for item in frame["voice"]["controls"] if item["visible"]]
    assert visible == ["voice_session_start"]


@pytest.mark.asyncio
async def test_unready_projection_maps_internal_media_reason_without_detail() -> None:
    runtime, _repository, capability = _runtime()
    capability.value = SimpleNamespace(status="unavailable", reason="media_unreachable")
    frame = await runtime.get_composer_state(
        user_id="user-a",
        device_id=DEVICE,
        device_kind="windows",
        connection_generation=CONNECTION,
        selected_chat_id=None,
        revision=7,
    )

    assert frame["voice"]["state"] == "unavailable"
    assert frame["voice"]["reason"] == "media_unavailable"
    assert frame["voice"]["controls"][0]["enabled"] is False


@pytest.mark.asyncio
async def test_current_owner_projection_exposes_generation_fenced_controls() -> None:
    runtime, repository, _capability = _runtime()
    repository.session = _session()
    frame = await runtime.get_composer_state(
        user_id="user-a",
        device_id=DEVICE,
        device_kind="web",
        connection_generation=CONNECTION,
        selected_chat_id=CHAT,
        revision=9,
    )

    voice = frame["voice"]
    assert voice["state"] == "listening"
    assert voice["generation"] == 2
    assert voice["media_grant_revision"] == 3
    assert voice["chat_context_synced"] is True
    assert voice["owner_device"]["device_id"] == DEVICE
    assert [item["action"] for item in voice["controls"] if item["visible"]] == [
        "voice_session_end",
        "voice_microphone_set",
        "voice_speech_mute_set",
        "voice_visible_chat_update",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("speech_muted", "microphone_enabled", "state", "message"),
    (
        (False, False, "listening", "Microphone is off."),
        (True, True, "muted", "Assistant speech is muted."),
        (True, False, "muted", "Microphone and assistant speech are muted."),
    ),
)
async def test_current_owner_projection_distinguishes_independent_mutes(
    speech_muted: bool,
    microphone_enabled: bool,
    state: str,
    message: str,
) -> None:
    runtime, repository, _capability = _runtime()
    repository.session = _session(
        speech_muted=speech_muted,
        microphone_enabled=microphone_enabled,
    )

    frame = await runtime.get_composer_state(
        user_id="user-a",
        device_id=DEVICE,
        device_kind="web",
        connection_generation=CONNECTION,
        selected_chat_id=CHAT,
        revision=10,
    )

    assert frame["voice"]["state"] == state
    assert frame["voice"]["message"] == message


@pytest.mark.asyncio
async def test_other_device_projection_offers_takeover_without_media_controls() -> None:
    runtime, repository, _capability = _runtime()
    repository.session = _session(device_id=OTHER_DEVICE, device_kind="android")
    frame = await runtime.get_composer_state(
        user_id="user-a",
        device_id=DEVICE,
        device_kind="web",
        connection_generation=CONNECTION,
        selected_chat_id=CHAT,
        revision=4,
    )

    assert frame["voice"]["state"] == "suspended"
    assert frame["voice"]["reason"] == "takeover_required"
    assert [
        item["action"] for item in frame["voice"]["controls"] if item["visible"]
    ] == ["voice_session_takeover"]


@pytest.mark.asyncio
async def test_orchestrator_publishes_monotonic_state_only_to_current_binding() -> None:
    socket = object()
    sent: list[dict] = []

    class _Runtime:
        async def get_composer_state(self, **kwargs):
            return {
                "type": "composer_state",
                "connection_generation": kwargs["connection_generation"],
                "revision": kwargs["revision"],
                "voice": {"available": True},
            }

    orchestrator = object.__new__(Orchestrator)
    orchestrator._voice_device_bindings = {("user-a", DEVICE): id(socket)}
    orchestrator._voice_control_bindings = {
        id(socket): VoiceControlClaims(
            subject="user-a",
            device_id=DEVICE,
            connection_generation=CONNECTION,
            binding_id="00000000-0000-4000-8000-000000000006",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
    }
    orchestrator._voice_device_kinds = {("user-a", DEVICE): "web"}
    orchestrator._voice_composer_revisions = {}
    orchestrator._voice_composer_tasks = {}
    orchestrator._ws_active_chat = {id(socket): CHAT}
    orchestrator.ui_sessions = {socket: {"sub": "user-a"}}
    orchestrator.voice_runtime = _Runtime()

    async def _safe_send(target, payload):
        assert target is socket
        sent.append(json.loads(payload))
        return True

    orchestrator._safe_send = _safe_send

    first = await orchestrator.publish_voice_composer_state(
        user_id="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
    )
    second = await orchestrator.publish_voice_composer_state(
        user_id="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
    )

    assert first["revision"] == 0
    assert second["revision"] == 1
    assert [frame["revision"] for frame in sent] == [0, 1]
    assert (
        await orchestrator.publish_voice_composer_state(
            user_id="user-a",
            device_id=DEVICE,
            connection_generation="00000000-0000-4000-8000-000000000007",
        )
        is None
    )
