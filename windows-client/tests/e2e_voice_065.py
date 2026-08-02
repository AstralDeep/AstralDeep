"""Feature 065 canonical C0-C6 journey through the real Windows reducers.

This is a deterministic offscreen/native contract journey.  It exercises the
shipping Qt widget, controller, transport serializers, and frozen-package
inputs while deliberately making no claim about Windows audio hardware from a
macOS or hosted test runner.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QPushButton  # noqa: E402

from astral_client import protocol as protocol_module  # noqa: E402
from astral_client import voice as voice_module  # noqa: E402
from astral_client.protocol import (  # noqa: E402
    OrchestratorClient,
    WindowsProtocolError,
)
from astral_client.protocol_manifest import (  # noqa: E402
    CLASSIFICATION,
    HANDLED,
)
from astral_client.voice import (  # noqa: E402
    VOICE_ANNOUNCEMENT_TOPIC,
    VoiceComposerWidget,
    VoiceController,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "backend/tests/fixtures/voice_065/client_conformance.json"
VALIDATOR_PATH = REPO_ROOT / "tooling/contract-ci/validate_voice_contracts.py"
DEVICE = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"
SESSION = "00000000-0000-4000-8000-000000000003"
CHAT = "00000000-0000-4000-8000-000000000004"
WORKER = "voice-worker-01"


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "windows_voice_conformance_validator",
        VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot import validator at {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()
fixture = validator.strict_load_json(FIXTURE_PATH)
indexed = validator.index_fixture_vectors(fixture)


def _vector(vector_id: str) -> dict[str, Any]:
    return validator.materialize_vector(indexed[vector_id], fixture, indexed)


class _FixtureDateTime(datetime):
    """Hold expiry-sensitive canonical vectors at their documented instant."""

    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)


class _Audio:
    def __init__(self) -> None:
        self.stopped = 0

    @staticmethod
    def capability() -> dict[str, Any]:
        return {
            "has_microphone": True,
            "has_audio_output": True,
            "microphone_permission": "authorized",
            "full_duplex": True,
            "transport": "livekit",
        }

    @staticmethod
    def request_microphone_permission(callback) -> None:
        callback("authorized")

    def stop_all(self) -> None:
        self.stopped += 1


class _Media:
    def __init__(self) -> None:
        self.authorized: list[dict[str, Any]] = []
        self.microphone: list[bool] = []
        self.closed = 0

    def authorize_announcement(self, manifest: dict[str, Any]) -> None:
        self.authorized.append(copy.deepcopy(manifest))

    def set_microphone_enabled(self, enabled: bool) -> None:
        self.microphone.append(bool(enabled))

    @staticmethod
    def stop_playback() -> None:
        pass

    def close(self) -> None:
        self.closed += 1


class _Http:
    @staticmethod
    def capability() -> dict[str, Any]:
        return {"schema_version": "1", "status": "ready", "reason": "ready"}


def _client(*, device_id: str = DEVICE) -> tuple[OrchestratorClient, list[dict]]:
    client = OrchestratorClient("ws://127.0.0.1:9/ws", "token", device_id=device_id)
    client.connection_generation = CONNECTION
    sent: list[dict] = []
    client._send_voice_frame = lambda frame: sent.append(copy.deepcopy(frame))
    return client, sent


def _controller(
    *,
    device_id: str = DEVICE,
    connection: str = CONNECTION,
    generation: int = 1,
    grant_revision: int = 2,
    worker: str = WORKER,
    transport: OrchestratorClient | None = None,
) -> tuple[VoiceController, OrchestratorClient, list[dict], _Media]:
    client, sent = (transport, []) if transport is not None else _client(device_id=device_id)
    client.connection_generation = connection
    media = _Media()
    controller = VoiceController(
        device_id=device_id,
        token_provider=lambda: "token",
        http_base="http://127.0.0.1:8001",
        connection_provider=lambda: client.connection_generation,
        chat_provider=lambda: CHAT,
        transport=client,
        audio=_Audio(),
        http=_Http(),
        media=media,
        run_async=lambda work: work(),
    )
    controller.session_id = SESSION
    controller.generation = generation
    controller.media_grant_revision = grant_revision
    controller.worker_identity = worker
    controller.visible_chat_id = CHAT
    controller.chat_context_revision = 3
    return controller, client, sent, media


def _dispose(controller: VoiceController) -> None:
    controller.close()
    controller.deleteLater()


def test_c0_server_composer_drives_accessible_widget_and_manifest(qapp) -> None:
    canonical = _vector("C0-P1-composer")["payload"]
    widget = VoiceComposerWidget()
    assert widget.apply_composer_state(canonical, CONNECTION)

    buttons = widget.findChildren(QPushButton, "voiceComposerControl")
    visible = [control for control in canonical["voice"]["controls"] if control["visible"]]
    assert [button.accessibleName() for button in buttons] == [
        control["label"] for control in visible
    ]
    assert [button.property("voiceAction") for button in buttons] == [
        control["action"] for control in visible
    ]
    assert not widget.apply_composer_state(
        _vector("C0-N1-extra-field")["payload"],
        CONNECTION,
    )
    assert all(
        CLASSIFICATION[frame_type] == HANDLED
        for frame_type in fixture["expected_discriminators"]
        if frame_type in CLASSIFICATION
    )
    widget.close()


def test_c1_binding_and_correlated_new_chat_use_the_current_native_connection(
    qapp,
    monkeypatch,
) -> None:
    monkeypatch.setattr(voice_module, "datetime", _FixtureDateTime)
    controller, client, _sent, _media = _controller()
    binding = _vector("C1-P1-control-binding")["payload"]
    assert controller.accept_frame(binding)
    assert controller.control_binding == binding["binding"]
    assert not controller.accept_frame(dict(binding, device_id=CHAT))

    canonical = _vector("C1-P2-new-chat")["payload"]
    socket_frames: list[dict[str, Any]] = []

    class _WebSocket:
        async def send(self, raw: str) -> None:
            socket_frames.append(json.loads(raw))

    class _Future:
        @staticmethod
        def add_done_callback(_callback) -> None:
            pass

    def _immediate(coroutine, _loop):
        asyncio.run(coroutine)
        return _Future()

    client._connected = True
    client._loop = object()
    client._ws = _WebSocket()
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _immediate)
    assert client.send_correlated_new_chat(
        canonical["submission_id"],
        canonical["request_generation"],
    )
    assert socket_frames == [canonical]
    assert socket_frames[0]["payload"] != _vector(
        "C1-N1-correlation-mismatch"
    )["payload"]["payload"]
    _dispose(controller)


def test_c2_partial_final_ack_and_rejection_use_the_normal_chat_transport(
    qapp,
    monkeypatch,
) -> None:
    monkeypatch.setattr(protocol_module, "datetime", _FixtureDateTime)
    client, sent = _client()
    controller, _client_value, _ignored, _media = _controller(transport=client)
    previews: list[tuple[str, bool]] = []
    controller.transcript_changed.connect(
        lambda text, final: previews.append((text, final))
    )

    partial = _vector("C2-P1-partial")["payload"]
    final = _vector("C2-P2-final")["payload"]
    controller._on_media_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, {})
    controller._on_media_data(
        protocol_module.VOICE_TRANSCRIPT_TOPIC,
        WORKER,
        partial,
    )
    assert previews == [(partial["text"], False)]
    assert not sent

    controller._on_media_data(
        protocol_module.VOICE_TRANSCRIPT_TOPIC,
        WORKER,
        final,
    )
    assert previews[-1] == (final["text"], True)
    assert len(sent) == 1
    assert sent[0]["action"] == "chat_message"
    assert sent[0]["payload"]["message"] == final["text"]
    assert sent[0]["payload"]["voice_origin"]["turn_id"] == final["turn_id"]
    assert "audio" not in json.dumps(sent[0])

    acknowledgement = _vector("C2-P3-acknowledged")["payload"]
    assert client._handle_runtime_frame(acknowledgement)
    assert not client._voice_pending
    _dispose(controller)

    rejected_client, rejected_sent = _client()
    rejected_client.send_voice_transcript(final)
    assert rejected_sent
    assert rejected_client._handle_runtime_frame(
        _vector("C2-P4-rejected")["payload"]
    )
    assert not rejected_client._voice_pending

    for vector_id in ("C2-N1-final-missing-proof", "C2-N2-packet-too-large"):
        invalid_controller, _invalid_client, invalid_sent, _invalid_media = _controller()
        invalid_controller._on_media_data(
            protocol_module.VOICE_TRANSCRIPT_TOPIC,
            WORKER,
            _vector(vector_id)["payload"],
        )
        assert not invalid_sent, vector_id
        _dispose(invalid_controller)


def test_c3_and_c6_manifests_are_identity_fenced_and_content_free(qapp) -> None:
    opening_controller, client, sent, media = _controller()
    opening = _vector("C3-P1-livekit-opening")["payload"]
    opening_controller._on_media_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, opening)
    assert media.authorized == [opening]

    playout = _vector("C3-P3-playout")["payload"]
    client.send_voice_playout_event(playout)
    assert sent == [playout]
    assert "text" not in json.dumps(sent[0])
    _dispose(opening_controller)

    for vector_id in (
        "C3-N1-opening-too-long",
        "C3-N4-track-over-four-seconds",
        "C3-N5-non-result-continuation",
        "C3-N6-announcement-packet-too-large",
        "C6-N1-result-null-turn",
    ):
        controller, _client_value, _invalid_sent, invalid_media = _controller()
        controller._on_media_data(
            VOICE_ANNOUNCEMENT_TOPIC,
            WORKER,
            _vector(vector_id)["payload"],
        )
        assert not invalid_media.authorized, vector_id
        _dispose(controller)

    greeting_controller, _greeting_client, _greeting_sent, greeting_media = _controller()
    greeting = _vector("C6-P1-greeting-null-turn")["payload"]
    greeting_controller._on_media_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, greeting)
    assert greeting_media.authorized == [greeting]
    _dispose(greeting_controller)

    worker_controller, _worker_client, _worker_sent, worker_media = _controller()
    unexpected_worker = _vector("C6-N2-unexpected-worker")["payload"]
    worker_controller._on_media_data(
        VOICE_ANNOUNCEMENT_TOPIC,
        unexpected_worker["worker_identity"],
        unexpected_worker,
    )
    assert not worker_media.authorized
    _dispose(worker_controller)

    stale_controller, _stale_client, _stale_sent, stale_media = _controller(
        grant_revision=3
    )
    stale = _vector("C6-N4-stale-grant-revision")["payload"]
    stale_controller._on_media_data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, stale)
    assert not stale_media.authorized
    _dispose(stale_controller)

    wrong_device = _vector("C6-N3-wrong-device")
    expected_device = wrong_device["context"]["expected_device_id"]
    wrong_client, wrong_sent = _client(device_id=expected_device)
    with pytest.raises(WindowsProtocolError):
        wrong_client.send_voice_playout_event(wrong_device["payload"])
    assert not wrong_sent


@pytest.mark.parametrize(
    "vector_id",
    (
        "C4-P1-en",
        "C4-P2-en-US",
        "C4-P3-en-GB",
        "C4-P4-fr",
        "C4-P5-und",
        "C4-P6-en-US-recovery",
    ),
)
def test_c4_language_policy_runs_through_the_real_turn_reducer(qapp, vector_id) -> None:
    controller, _client_value, _sent, _media = _controller()
    statuses: list[tuple[str, str]] = []
    controller.status_changed.connect(
        lambda state, message: statuses.append((state, message))
    )
    assert controller.accept_frame(_vector(vector_id)["payload"])
    assert statuses[-1][0] == "processing"
    _dispose(controller)


def test_c4_invalid_english_policy_fails_before_native_ui_state_changes(qapp) -> None:
    controller, _client_value, _sent, _media = _controller()
    statuses: list[tuple[str, str]] = []
    controller.status_changed.connect(
        lambda state, message: statuses.append((state, message))
    )
    assert not controller.accept_frame(_vector("C4-N1-en-wrong-policy")["payload"])
    assert not statuses
    assert controller.state == "off"
    _dispose(controller)


def test_c5_lifecycle_disables_background_microphone_and_tears_down(qapp) -> None:
    controller, _client_value, _sent, media = _controller()
    for vector_id in (
        "C5-P1-active",
        "C5-P2-suspended",
        "C5-P3-reconnecting",
    ):
        assert controller.accept_frame(_vector(vector_id)["payload"]), vector_id
    assert media.microphone[-2:] == [False, False]
    assert not controller.accept_frame(
        _vector("C5-N1-background-mic-enabled")["payload"]
    )
    assert controller.accept_frame(_vector("C5-P4-ended")["payload"])
    assert controller.session_id is None
    assert controller.state == "ended"
    assert media.closed >= 1
    controller.deleteLater()


def test_frozen_package_declares_native_audio_and_exact_direct_rtc_closure() -> None:
    spec = (REPO_ROOT / "windows-client/AstralDeep.spec").read_text(encoding="utf-8")
    manifest = json.loads(
        (REPO_ROOT / "windows-client/deployment/runtime-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    requirements = (
        REPO_ROOT / "windows-client/requirements-release.lock.txt"
    ).read_text(encoding="utf-8")

    assert '"PySide6.QtMultimedia"' in spec
    assert 'collect_submodules("livekit.rtc")' in spec
    assert 'collect_dynamic_libs("livekit")' in spec
    assert "livekit==1.1.14" in requirements
    assert manifest["target_platform"] == "win_amd64"
    assert manifest["python_version"] == "3.11"
