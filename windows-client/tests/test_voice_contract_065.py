"""Feature 065 Windows composer, registration, and transcript contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtGui import QAccessible  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QLineEdit, QPushButton  # noqa: E402

from astral_client.protocol import (  # noqa: E402
    OrchestratorClient,
    WindowsProtocolError,
    load_or_create_voice_device_id,
)
from astral_client import app as appmod  # noqa: E402
from astral_client.app import MainWindow  # noqa: E402
from astral_client.voice import (  # noqa: E402
    QtAudioBackend,
    VoiceComposerWidget,
    VoiceHttpClient,
)


DEVICE = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"
OTHER_CONNECTION = "00000000-0000-4000-8000-000000000012"
CHAT = "00000000-0000-4000-8000-000000000003"
SESSION = "00000000-0000-4000-8000-000000000004"
TURN = "00000000-0000-4000-8000-000000000005"
OTHER_TURN = "00000000-0000-4000-8000-000000000015"
CLIENT_TURN = "00000000-0000-4000-8000-000000000006"
SUBMISSION = "00000000-0000-4000-8000-000000000007"
REQUEST = "00000000-0000-4000-8000-000000000008"
PROOF = "b" * 64
ANNOUNCEMENT = "00000000-0000-4000-8000-000000000010"


def test_livekit_vendor_diagnostics_are_disabled_at_module_import() -> None:
    assert all(
        logging.getLogger(name).disabled
        for name in ("livekit", "livekit.rtc", "livekit.rtc.synchronizer")
    )


def _transcript(*, final: bool = True, text: str = "Open my schedule") -> dict:
    value = {
        "type": "voice_transcript",
        "schema_version": "1",
        "session_id": SESSION,
        "generation": 2,
        "turn_id": TURN,
        "client_turn_id": CLIENT_TURN,
        "submission_id": SUBMISSION,
        "request_generation": REQUEST,
        "chat_id": CHAT,
        "chat_context_revision": 3,
        "media_grant_revision": 4,
        "sequence": 5,
        "final": final,
        "text": text,
        "detected_language": "en" if final else None,
        "source_participant_identity": "voice-worker-a",
    }
    if final:
        value.update(
            {
                "text_digest_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "transcript_proof": PROOF,
                "proof_expires_at": "2099-07-31T18:02:00Z",
            }
        )
    return value


def _composer_frame(*, revision: int = 1) -> dict:
    return {
        "type": "composer_state",
        "schema_version": "1",
        "revision": revision,
        "connection_generation": CONNECTION,
        "voice": {
            "available": True,
            "state": "off",
            "speech_muted": False,
            "microphone_enabled": False,
            "foreground_active": False,
            "reason": "ready",
            "output_locale": "en-US",
            "chat_context_revision": None,
            "applied_chat_context_revision": None,
            "chat_context_synced": False,
            "controls": [
                {
                    "key": "voice-start",
                    "action": "voice_session_start",
                    "label": "Start voice conversation",
                    "icon": "microphone",
                    "visible": True,
                    "enabled": True,
                    "pressed": False,
                    "busy": False,
                },
                {
                    "key": "voice-takeover",
                    "action": "voice_session_takeover",
                    "label": "Take over voice conversation",
                    "icon": "microphone",
                    "visible": False,
                    "enabled": False,
                    "pressed": False,
                    "busy": False,
                },
            ],
        },
    }


def _playout_event() -> dict:
    return {
        "type": "voice_playout_event",
        "schema_version": "1",
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "session_id": SESSION,
        "generation": 2,
        "media_grant_revision": 4,
        "announcement_id": ANNOUNCEMENT,
        "announcement_sequence": 1,
        "turn_id": TURN,
        "kind": "progress",
        "quantum_role": "single",
        "quantum_index": 0,
        "phase": "started",
        "client_sequence": 0,
        "observed_at": "2026-07-31T18:00:00.000Z",
    }


def test_device_identity_is_stable_non_secret_uuid4(tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    first = load_or_create_voice_device_id(settings)
    second = load_or_create_voice_device_id(settings)

    assert first == second
    assert len(first) == 36 and first[14] == "4"
    assert settings.value("astraldeep.voice.device_id.v1") == first


def test_qtmultimedia_reports_only_canonical_voice_capability(qapp):
    audio = QtAudioBackend()

    capability = audio.capability()

    assert set(capability) == {
        "has_microphone",
        "has_audio_output",
        "microphone_permission",
        "full_duplex",
        "transport",
    }
    assert isinstance(capability["has_microphone"], bool)
    assert isinstance(capability["has_audio_output"], bool)
    assert isinstance(capability["full_duplex"], bool)
    assert capability["microphone_permission"] in {
        "not_determined",
        "authorized",
        "denied",
        "restricted",
    }
    assert capability["transport"] == "livekit"
    audio.stop_all()
    audio.deleteLater()


def test_registration_reports_stable_device_fresh_connection_and_voice_capability(qapp):
    client = OrchestratorClient(
        "ws://127.0.0.1:9/ws",
        "token",
        {"device_type": "windows", "voice": {"has_microphone": True}},
        device_id=DEVICE,
    )

    first = client._register_frame()
    second = client._register_frame()

    assert first["device_id"] == second["device_id"] == DEVICE
    assert first["connection_generation"] != second["connection_generation"]
    assert "voice" in first["capabilities"]
    assert first["device"]["voice"]["has_microphone"] is True


def test_server_owned_composer_order_state_and_accessibility(qapp):
    widget = VoiceComposerWidget()
    actions: list[str] = []
    widget.action_requested.connect(actions.append)

    assert widget.apply_composer_state(_composer_frame(), CONNECTION)
    buttons = widget.findChildren(QPushButton, "voiceComposerControl")
    assert [button.accessibleName() for button in buttons] == [
        "Start voice conversation"
    ]
    start = buttons[0]
    interface = QAccessible.queryAccessibleInterface(start)
    assert interface is not None
    assert interface.role() == QAccessible.Role.Button
    assert start.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert start.isEnabled()

    start.setFocus()
    QTest.keyClick(start, Qt.Key.Key_Space)
    assert actions == ["voice_session_start"]

    stale = _composer_frame(revision=0)
    assert not widget.apply_composer_state(stale, CONNECTION)
    assert not widget.apply_composer_state(
        _composer_frame(revision=2), "bad-connection"
    )
    widget.close()


def test_composer_revisions_are_scoped_to_connection_generation(qapp):
    widget = VoiceComposerWidget()

    assert widget.apply_composer_state(_composer_frame(revision=100), CONNECTION)

    reconnected = _composer_frame(revision=0)
    reconnected["connection_generation"] = OTHER_CONNECTION
    assert widget.apply_composer_state(reconnected, OTHER_CONNECTION)

    current = _composer_frame(revision=2)
    current["connection_generation"] = OTHER_CONNECTION
    assert widget.apply_composer_state(current, OTHER_CONNECTION)
    assert not widget.apply_composer_state(current, OTHER_CONNECTION)

    decreasing = _composer_frame(revision=1)
    decreasing["connection_generation"] = OTHER_CONNECTION
    assert not widget.apply_composer_state(decreasing, OTHER_CONNECTION)
    widget.close()


def test_voice_widget_never_disables_typed_fallback(qapp):
    typed = QLineEdit()
    widget = VoiceComposerWidget()
    widget.apply_composer_state(_composer_frame(), CONNECTION)
    widget.set_voice_status("unavailable", "Microphone permission was denied.")

    assert typed.isEnabled()
    assert widget.status_label.accessibleName() == "Voice conversation status"
    assert "denied" in widget.status_label.accessibleDescription().lower()
    widget.close()
    typed.close()


@pytest.mark.parametrize(
    ("state", "outcome", "accessible_name"),
    (
        ("failed", "Request did not complete.", "Voice request did not complete"),
        ("cancelled", "Request did not complete.", "Voice request did not complete"),
        ("abandoned", "Request did not complete.", "Voice request did not complete"),
        ("refused", "Request did not start.", "Voice request did not start"),
    ),
)
def test_terminal_voice_request_notice_is_prominent_accessible_and_verbatim(
    qapp, state, outcome, accessible_name
):
    typed = QLineEdit()
    widget = VoiceComposerWidget()
    safe_server_message = "The provider is temporarily unavailable (llm_config_invalid)."

    widget.set_voice_turn_status(state, safe_server_message)

    notice = widget.request_notice_label
    assert not notice.isHidden()
    assert notice.text().splitlines() == [f"⚠ {outcome}", safe_server_message]
    assert notice.textFormat() == Qt.TextFormat.PlainText
    assert notice.wordWrap()
    assert notice.property("noticeKind") == "request_failure"
    assert notice.accessibleName() == accessible_name
    assert notice.accessibleDescription() == notice.text()
    interface = QAccessible.queryAccessibleInterface(notice)
    assert interface is not None
    assert interface.role() == QAccessible.Role.StaticText
    assert typed.isEnabled()

    widget.close()
    typed.close()


def test_terminal_voice_request_notice_persists_through_ordinary_voice_status(qapp):
    widget = VoiceComposerWidget()
    safe_server_message = "The request could not be completed."

    widget.set_voice_turn_status("failed", safe_server_message)
    terminal_text = widget.request_notice_label.text()
    widget.set_voice_status("listening", "Listening for another request.")

    assert not widget.request_notice_label.isHidden()
    assert widget.request_notice_label.text() == terminal_text
    assert safe_server_message in widget.request_notice_label.text()
    widget.close()


@pytest.mark.parametrize(
    "newer_state",
    ("recognizing", "submitting", "accepted", "processing", "succeeded"),
)
def test_newer_voice_turn_clears_stale_terminal_request_notice(
    qapp, newer_state
):
    widget = VoiceComposerWidget()
    widget.set_voice_turn_status(
        "failed",
        "The request could not be completed.",
        turn_id=TURN,
        occurred_at="2026-07-31T12:00:01Z",
    )
    assert not widget.request_notice_label.isHidden()

    widget.set_voice_turn_status(
        newer_state,
        "The retry is current.",
        turn_id=OTHER_TURN,
        occurred_at="2026-07-31T12:00:02Z",
    )

    assert widget.request_notice_label.isHidden()
    assert widget.request_notice_label.text() == ""
    assert widget.request_notice_label.accessibleDescription() == ""
    widget.close()


def test_older_turn_update_cannot_clear_terminal_request_notice(qapp):
    widget = VoiceComposerWidget()
    widget.set_voice_turn_status(
        "failed",
        "The request could not be completed.",
        turn_id=TURN,
        occurred_at="2026-07-31T12:00:02Z",
    )
    terminal_text = widget.request_notice_label.text()

    widget.set_voice_turn_status(
        "processing",
        "An older request is still processing.",
        turn_id=OTHER_TURN,
        occurred_at="2026-07-31T12:00:01Z",
    )

    assert not widget.request_notice_label.isHidden()
    assert widget.request_notice_label.text() == terminal_text
    widget.close()


def test_explicit_composer_reset_clears_terminal_request_notice(qapp):
    widget = VoiceComposerWidget()
    widget.set_voice_turn_status(
        "failed",
        "The request could not be completed.",
        turn_id=TURN,
        occurred_at="2026-07-31T12:00:01Z",
    )

    assert widget.apply_composer_state(_composer_frame(), CONNECTION)
    assert widget.request_notice_label.isHidden()
    assert widget.request_notice_label.text() == ""
    widget.close()


def test_speech_error_notice_does_not_claim_the_text_request_failed(qapp):
    typed = QLineEdit()
    widget = VoiceComposerWidget()
    safe_server_message = "Speech synthesis stopped before playback."

    widget.set_speech_error(safe_server_message)

    notice = widget.request_notice_label
    assert not notice.isHidden()
    assert notice.text().splitlines() == [
        "⚠ Speech playback failed. The text result may still be available in the conversation.",
        safe_server_message,
    ]
    assert notice.property("noticeKind") == "speech_error"
    assert notice.accessibleName() == "Voice speech error"
    assert notice.accessibleDescription() == notice.text()
    assert "request did not complete" not in notice.text().lower()
    assert typed.isEnabled()

    widget.close()
    typed.close()


def test_turn_scoped_speech_error_cannot_overwrite_newer_success(qapp):
    widget = VoiceComposerWidget()
    widget.set_speech_error(
        "The result audio could not be delivered.",
        turn_id=TURN,
        occurred_at="2026-07-31T12:00:02Z",
    )
    assert not widget.request_notice_label.isHidden()

    widget.set_voice_turn_status(
        "succeeded",
        "The newer text result is available.",
        turn_id=OTHER_TURN,
        occurred_at="2026-07-31T12:00:03Z",
    )
    assert widget.request_notice_label.isHidden()
    assert widget.property("voiceState") == "speaking_result"

    widget.set_speech_error(
        "Delayed older recap failure.",
        turn_id=TURN,
        occurred_at="2026-07-31T12:00:02Z",
    )
    assert widget.request_notice_label.isHidden()
    widget.close()


@pytest.mark.parametrize(
    ("turn_state", "expected_phase"),
    (
        ("succeeded", "speaking_result"),
        ("failed", "listening"),
        ("refused", "listening"),
        ("cancelled", "listening"),
        ("abandoned", "listening"),
    ),
)
def test_terminal_turn_notice_does_not_change_session_to_error(
    qapp, turn_state, expected_phase
):
    widget = VoiceComposerWidget()

    widget.set_voice_turn_status(
        turn_state,
        "Bounded terminal detail.",
        turn_id=TURN,
        occurred_at="2026-07-31T12:00:02Z",
    )

    assert widget.property("voiceState") == expected_phase
    widget.close()


@pytest.mark.parametrize(
    ("retry_policy", "guidance"),
    (
        (
            "explicit_user_retry",
            "Please try speaking again, or use typed chat.",
        ),
        (
            "none",
            "This request will not retry automatically. Use typed chat to continue.",
        ),
    ),
)
def test_submission_rejection_is_prominent_verbatim_and_has_retry_guidance(
    qapp, retry_policy, guidance
):
    typed = QLineEdit()
    widget = VoiceComposerWidget()
    server_message = "The spoken request could not be verified <safely>."

    widget.set_voice_submission_rejected(
        server_message,
        retry_policy=retry_policy,
        turn_id=TURN,
        occurred_at="2026-07-31T12:00:01Z",
    )

    notice = widget.request_notice_label
    assert not notice.isHidden()
    assert notice.text().splitlines() == [
        "⚠ Request did not start.",
        server_message,
        guidance,
    ]
    assert notice.textFormat() == Qt.TextFormat.PlainText
    assert notice.property("noticeKind") == "request_failure"
    assert notice.accessibleName() == "Voice request did not start"
    assert notice.accessibleDescription() == notice.text()
    assert typed.isEnabled()

    widget.close()
    typed.close()


def test_server_owned_composer_speech_error_keeps_text_result_wording(qapp):
    widget = VoiceComposerWidget()
    safe_server_message = "Speech synthesis is temporarily unavailable."
    frame = _composer_frame()
    frame["voice"].update(
        {
            "state": "error",
            "reason": "speech_error",
            "message": safe_server_message,
        }
    )

    assert widget.apply_composer_state(frame, CONNECTION)
    assert widget.request_notice_label.text().splitlines() == [
        "⚠ Speech playback failed. The text result may still be available in the conversation.",
        safe_server_message,
    ]
    assert widget.request_notice_label.property("noticeKind") == "speech_error"
    widget.close()


def test_main_window_places_server_control_beside_usable_typed_input(
    qapp, monkeypatch
):
    monkeypatch.setenv("ASTRAL_WIN_AGENT", "0")
    monkeypatch.setattr(MainWindow, "_start_integrity_check", lambda self: None)
    monkeypatch.setattr(MainWindow, "_init_workspace", lambda self: None)
    monkeypatch.setattr(
        appmod,
        "load_or_create_host_id",
        lambda: "77777777-7777-4777-8777-777777777777",
    )
    window = MainWindow("ws://127.0.0.1:9/ws", "dev-token", connect=False)
    connection = window.client._register_frame()["connection_generation"]
    frame = _composer_frame()
    frame["connection_generation"] = connection
    window._on_message(frame)

    control = window._voice_widget.findChild(
        QPushButton, "voiceComposerControl"
    )
    assert control is not None
    assert control.accessibleName() == "Start voice conversation"
    assert window._input.isEnabled()
    assert window._send_btn.isEnabled()

    window.close()


def test_main_window_routes_turn_failures_and_speech_errors_to_persistent_notice(
    qapp, monkeypatch
):
    monkeypatch.setenv("ASTRAL_WIN_AGENT", "0")
    monkeypatch.setattr(MainWindow, "_start_integrity_check", lambda self: None)
    monkeypatch.setattr(MainWindow, "_init_workspace", lambda self: None)
    monkeypatch.setattr(
        appmod,
        "load_or_create_host_id",
        lambda: "77777777-7777-4777-8777-777777777777",
    )
    window = MainWindow("ws://127.0.0.1:9/ws", "dev-token", connect=False)
    monkeypatch.setattr(window._voice_controller, "accept_frame", lambda _frame: True)

    terminal_message = "The request could not be completed by the configured provider."
    window._on_message(
        {
            "type": "voice_turn_state",
            "state": "failed",
            "message": terminal_message,
        }
    )
    assert window._voice_widget.request_notice_label.text().splitlines() == [
        "⚠ Request did not complete.",
        terminal_message,
    ]

    rejection_message = "The spoken request could not be verified."
    window._on_message(
        {
            "type": "voice_submission_rejected",
            "turn_id": TURN,
            "submission_id": SUBMISSION,
            "retry_policy": "explicit_user_retry",
            "occurred_at": "2026-07-31T12:00:01Z",
            "message": rejection_message,
        }
    )
    assert window._voice_widget.request_notice_label.text().splitlines() == [
        "⚠ Request did not start.",
        rejection_message,
        "Please try speaking again, or use typed chat.",
    ]

    speech_message = "Speech synthesis is temporarily unavailable."
    window._on_message(
        {
            "type": "voice_session_state",
            "state": "error",
            "reason": "speech_error",
            "message": speech_message,
        }
    )
    assert window._voice_widget.request_notice_label.text().splitlines() == [
        "⚠ Speech playback failed. The text result may still be available in the conversation.",
        speech_message,
    ]
    assert window._input.isEnabled()
    assert window._send_btn.isEnabled()

    window.close()


@pytest.mark.parametrize("speech_outcome", (None, "source_finished", "suppressed"))
def test_main_window_keeps_normal_success_for_nonfailed_speech_outcome(
    qapp, monkeypatch, speech_outcome
):
    monkeypatch.setenv("ASTRAL_WIN_AGENT", "0")
    monkeypatch.setattr(MainWindow, "_start_integrity_check", lambda self: None)
    monkeypatch.setattr(MainWindow, "_init_workspace", lambda self: None)
    monkeypatch.setattr(
        appmod,
        "load_or_create_host_id",
        lambda: "77777777-7777-4777-8777-777777777777",
    )
    window = MainWindow("ws://127.0.0.1:9/ws", "dev-token", connect=False)
    monkeypatch.setattr(window._voice_controller, "accept_frame", lambda _frame: True)
    frame = {
        "type": "voice_turn_state",
        "state": "succeeded",
        "turn_id": TURN,
        "occurred_at": "2026-07-31T12:00:01Z",
        "message": "Request completed. The text result is available.",
    }
    if speech_outcome is not None:
        frame["speech_outcome"] = speech_outcome

    window._on_message(frame)

    assert window._voice_widget.request_notice_label.isHidden()
    window.close()


def test_main_window_routes_failed_speech_outcome_without_session_error(
    qapp, monkeypatch
):
    monkeypatch.setenv("ASTRAL_WIN_AGENT", "0")
    monkeypatch.setattr(MainWindow, "_start_integrity_check", lambda self: None)
    monkeypatch.setattr(MainWindow, "_init_workspace", lambda self: None)
    monkeypatch.setattr(
        appmod,
        "load_or_create_host_id",
        lambda: "77777777-7777-4777-8777-777777777777",
    )
    window = MainWindow("ws://127.0.0.1:9/ws", "dev-token", connect=False)
    monkeypatch.setattr(window._voice_controller, "accept_frame", lambda _frame: True)

    window._on_message(
        {
            "type": "voice_turn_state",
            "state": "succeeded",
            "speech_outcome": "failed",
            "turn_id": TURN,
            "occurred_at": "2026-07-31T12:00:02Z",
            "message": "Request completed. The text result is available.",
        }
    )

    notice = window._voice_widget.request_notice_label
    assert notice.text().splitlines() == [
        "⚠ Speech playback failed.",
        "The result audio could not be delivered.",
        (
            "The text result is still available in the conversation. "
            "Typed chat remains available."
        ),
    ]
    assert notice.property("noticeKind") == "speech_error"
    assert window._voice_controller.state != "error"
    window.close()


def test_voice_http_calls_are_authenticated_and_connection_bound(qapp):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read(_limit):
            return b"{}"

    def opener(request, *, timeout):
        requests.append((request, timeout))
        return Response()

    scope = {
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "control_binding": "b" * 48,
    }
    client = VoiceHttpClient(
        "https://voice.example/",
        lambda: "access-token",
        opener=opener,
        timeout=3.5,
    )

    client.capability()
    client.create({"visible_chat_id": CHAT}, scope)
    client.update(SESSION, {"microphone_enabled": False}, scope)
    client.stop_speech(SESSION, {"expected_generation": 2}, scope)
    client.takeover(SESSION, {"expected_generation": 2}, scope)
    client.end(SESSION, 2, 4, scope)

    assert [request.method for request, _timeout in requests] == [
        "GET",
        "POST",
        "PATCH",
        "POST",
        "POST",
        "DELETE",
    ]
    assert all(timeout == 3.5 for _request, timeout in requests)
    assert requests[0][0].full_url == "https://voice.example/api/voice/capability"
    assert requests[-1][0].full_url.endswith(
        f"/api/voice/sessions/{SESSION}?expected_generation=2&"
        "expected_media_grant_revision=4"
    )
    for index, (request, _timeout) in enumerate(requests):
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["authorization"] == "Bearer access-token"
        if index == 0:
            assert "x-astral-device-id" not in headers
            continue
        assert headers["x-astral-device-id"] == DEVICE
        assert headers["x-astral-connection-generation"] == CONNECTION
        assert headers["x-astral-voice-control-binding"] == "b" * 48


def test_final_transcript_builds_only_strict_normal_chat_message(qapp):
    client = OrchestratorClient(
        "ws://127.0.0.1:9/ws", "token", device_id=DEVICE
    )
    client.connection_generation = CONNECTION
    sent: list[dict] = []
    client._send_voice_frame = lambda frame: sent.append(frame)

    submission = client.send_voice_transcript(_transcript())

    assert submission.submission_id == SUBMISSION
    assert len(sent) == 1
    frame = sent[0]
    assert frame["type"] == "ui_event"
    assert frame["action"] == "chat_message"
    assert frame["session_id"] == frame["payload"]["chat_id"] == CHAT
    assert frame["payload"]["message"] == "Open my schedule"
    assert frame["payload"]["snapshot_purpose"] == "commit"
    assert frame["payload"]["voice_origin"] == {
        "schema_version": "1",
        "session_id": SESSION,
        "generation": 2,
        "media_grant_revision": 4,
        "turn_id": TURN,
        "client_turn_id": CLIENT_TURN,
        "chat_context_revision": 3,
        "source_participant_identity": "voice-worker-a",
        "detected_language": "en",
        "text_digest_sha256": hashlib.sha256(b"Open my schedule").hexdigest(),
        "transcript_proof": PROOF,
        "proof_expires_at": "2099-07-31T18:02:00Z",
    }
    assert "voice_action" not in json.dumps(frame)


@pytest.mark.parametrize(
    "changes",
    (
        {"final": False},
        {"text": ""},
        {"text_digest_sha256": "a" * 64},
        {"source_participant_identity": ""},
        {"extra": "not allowed"},
    ),
)
def test_partial_empty_tampered_or_malformed_transcripts_never_dispatch(qapp, changes):
    client = OrchestratorClient(
        "ws://127.0.0.1:9/ws", "token", device_id=DEVICE
    )
    client.connection_generation = CONNECTION
    value = _transcript()
    value.update(changes)

    with pytest.raises(WindowsProtocolError):
        client.send_voice_transcript(value)
    assert not client._voice_pending


def test_voice_submission_retries_exact_ids_on_new_connection_until_matching_ack(qapp):
    client = OrchestratorClient(
        "ws://127.0.0.1:9/ws", "token", device_id=DEVICE
    )
    client.connection_generation = CONNECTION
    client._send_voice_frame = lambda _frame: None
    client.send_voice_transcript(_transcript())

    next_connection = "00000000-0000-4000-8000-000000000009"
    client.connection_generation = next_connection
    frames = client._pending_voice_frames()
    assert len(frames) == 1
    retried = frames[0]
    assert retried["connection_generation"] == next_connection
    assert retried["payload"]["connection_generation"] == next_connection
    assert retried["submission_id"] == SUBMISSION
    assert retried["request_generation"] == REQUEST
    assert retried["payload"]["voice_origin"]["turn_id"] == TURN

    wrong = {
        "type": "user_message_acked",
        "schema_version": "1",
        "chat_id": CHAT,
        "message_id": 7,
        "submission_id": SUBMISSION,
        "request_generation": REQUEST,
        "connection_generation": CONNECTION,
        "voice_turn_id": TURN,
    }
    assert not client.settle_voice_submission(wrong)
    assert TURN in client._voice_pending

    exact = dict(wrong, connection_generation=next_connection)
    assert client.settle_voice_submission(exact)
    assert TURN not in client._voice_pending


def test_matching_rejection_is_terminal_and_never_automatically_reused(qapp):
    client = OrchestratorClient(
        "ws://127.0.0.1:9/ws", "token", device_id=DEVICE
    )
    client.connection_generation = CONNECTION
    client._send_voice_frame = lambda _frame: None
    client.send_voice_transcript(_transcript())

    rejection = {
        "type": "voice_submission_rejected",
        "schema_version": "1",
        "session_id": SESSION,
        "connection_generation": CONNECTION,
        "generation": 2,
        "media_grant_revision": 4,
        "turn_id": TURN,
        "client_turn_id": CLIENT_TURN,
        "submission_id": SUBMISSION,
        "request_generation": REQUEST,
        "chat_id": CHAT,
        "reason": "invalid_proof",
        "retry_policy": "explicit_user_retry",
        "occurred_at": "2026-07-31T18:00:00Z",
    }
    assert not client._handle_runtime_frame(dict(rejection, reason="invented"))
    assert TURN in client._voice_pending
    assert client._handle_runtime_frame(rejection)
    assert not client._voice_pending
    assert client._pending_voice_frames() == []


def test_reconnect_flushes_retained_voice_after_ordinary_queue(qapp):
    client = OrchestratorClient(
        "ws://127.0.0.1:9/ws", "token", device_id=DEVICE
    )
    client.connection_generation = CONNECTION
    client._send_voice_frame = lambda _frame: None
    client.send_voice_transcript(_transcript())
    client.send_event("get_history", {})
    sent: list[dict] = []

    class FakeWs:
        async def send(self, raw):
            sent.append(json.loads(raw))

    asyncio.run(client._flush_pending(FakeWs()))
    asyncio.run(client._resend_voice_pending(FakeWs()))

    assert [frame["action"] for frame in sent] == ["get_history", "chat_message"]


def test_correlated_new_chat_uses_current_socket_and_exact_strict_shape(
    qapp, monkeypatch
):
    client = OrchestratorClient(
        "ws://127.0.0.1:9/ws", "token", device_id=DEVICE
    )
    client.connection_generation = CONNECTION
    client._connected = True
    sent: list[dict] = []

    class FakeFuture:
        def add_done_callback(self, callback):
            self.callback = callback

    class FakeLoop:
        pass

    class FakeWs:
        async def send(self, raw):
            sent.append(json.loads(raw))

    client._loop = FakeLoop()
    client._ws = FakeWs()
    def immediate(coro, _loop):
        asyncio.run(coro)
        return FakeFuture()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", immediate)
    assert client.send_correlated_new_chat(SUBMISSION, REQUEST)

    assert sent == [
        {
            "type": "ui_event",
            "action": "new_chat",
            "schema_version": "1",
            "connection_generation": CONNECTION,
            "submission_id": SUBMISSION,
            "request_generation": REQUEST,
            "payload": {
                "schema_version": "1",
                "connection_generation": CONNECTION,
                "submission_id": SUBMISSION,
                "request_generation": REQUEST,
            },
        }
    ]


def test_playout_event_is_strict_content_free_and_never_offline_queued(qapp):
    client = OrchestratorClient(
        "ws://127.0.0.1:9/ws", "token", device_id=DEVICE
    )
    client.connection_generation = CONNECTION
    sent: list[dict] = []
    client._send_voice_frame = lambda frame: sent.append(frame)

    event = _playout_event()
    client.send_voice_playout_event(event)

    assert sent == [event]
    assert not client._pending
    assert not client._voice_pending
    assert sent[0]["type"] == "voice_playout_event"
    assert "action" not in sent[0]
    assert "text" not in json.dumps(sent[0])


@pytest.mark.parametrize(
    "changes",
    (
        {"device_id": "00000000-0000-4000-8000-000000000099"},
        {"connection_generation": "00000000-0000-4000-8000-000000000099"},
        {"phase": "queued"},
        {"kind": "result"},
        {"turn_id": None},
        {"text": "forbidden content"},
        {"client_sequence": -1},
    ),
)
def test_playout_event_rejects_stale_malformed_or_content_bearing_frames(
    qapp, changes
):
    client = OrchestratorClient(
        "ws://127.0.0.1:9/ws", "token", device_id=DEVICE
    )
    client.connection_generation = CONNECTION
    sent: list[dict] = []
    client._send_voice_frame = lambda frame: sent.append(frame)
    event = _playout_event()
    event.update(changes)

    with pytest.raises(WindowsProtocolError):
        client.send_voice_playout_event(event)
    assert not sent
