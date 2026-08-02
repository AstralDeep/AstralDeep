"""Feature-065 shipped web composer, accessibility, and asset guards."""

from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path

from webrender.chrome.composer_model import (
    VoiceComposerContext,
    build_composer_state,
)


BACKEND_DIR = Path(__file__).resolve().parents[2]
SHELL_PATH = BACKEND_DIR / "webrender/templates/shell.html"
CLIENT_PATH = BACKEND_DIR / "webrender/static/client.js"
CSS_PATH = BACKEND_DIR / "webrender/static/astral.css"
FIXTURE_PATH = BACKEND_DIR / "tests/fixtures/voice_065/client_conformance.json"
VENDOR_DIR = BACKEND_DIR / "webrender/static/vendor"
LIVEKIT_PATH = VENDOR_DIR / "livekit-client.umd.min.js"
LIVEKIT_DIGEST_PATH = VENDOR_DIR / "livekit-client.sha256"


class _ShellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}
        self.scripts: list[dict[str, str | None]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if identifier := values.get("id"):
            self.by_id[identifier] = values
        if tag == "script":
            self.scripts.append(values)


def _shell_parser() -> _ShellParser:
    parser = _ShellParser()
    parser.feed(SHELL_PATH.read_text(encoding="utf-8"))
    return parser


def _fixture_composer() -> dict[str, object]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    case = next(item for item in fixture["cases"] if item["id"] == "C0")
    return case["positive"][0]["payload"]


def test_server_composer_builder_matches_canonical_web_projection() -> None:
    actual = build_composer_state(
        VoiceComposerContext(
            revision=7,
            connection_generation="00000000-0000-4000-8000-000000000002",
            local_device_id="00000000-0000-4000-8000-000000000001",
            available=True,
            state="off",
            reason="ready",
            visible_chat_id="00000000-0000-4000-8000-000000000004",
        )
    )

    assert actual == _fixture_composer()


def test_shell_hosts_accessible_voice_controls_without_replacing_typed_chat() -> None:
    parser = _shell_parser()
    controls = parser.by_id["astral-voice-controls"]
    status = parser.by_id["astral-voice-status"]
    transcript = parser.by_id["astral-voice-transcript"]
    resume = parser.by_id["astral-voice-audio-resume"]
    terminal_notice = parser.by_id["astral-voice-turn-notice"]

    assert controls["role"] == "group"
    assert controls["aria-label"] == "Voice conversation controls"
    assert "astral-voice-controls" in str(controls["class"]).split()
    assert status["role"] == "status"
    assert status["aria-live"] == "polite"
    assert status["aria-atomic"] == "true"
    assert transcript["aria-live"] == "polite"
    assert resume["type"] == "button"
    assert resume["hidden"] is None
    assert terminal_notice["role"] == "alert"
    assert terminal_notice["aria-live"] == "assertive"
    assert terminal_notice["aria-atomic"] == "true"
    assert terminal_notice["hidden"] is None
    assert parser.by_id["astral-input"]["type"] == "text"
    assert parser.by_id["astral-input"].get("disabled") is None
    assert "astral-voice-audio" in parser.by_id

    shell = SHELL_PATH.read_text(encoding="utf-8")
    assert shell.index('id="astral-voice-controls"') < shell.index(
        'id="astral-input"'
    )


def test_shell_loads_hash_pinned_local_livekit_before_client() -> None:
    parser = _shell_parser()
    sources = [script.get("src") for script in parser.scripts if script.get("src")]
    livekit = (
        "/static/vendor/livekit-client.umd.min.js"
        "?v=%%ASTRAL_V:vendor/livekit-client.umd.min.js%%"
    )
    client = "/static/client.js?v=%%ASTRAL_V:client.js%%"

    expected_digest = LIVEKIT_DIGEST_PATH.read_text(encoding="ascii").strip().split()[0]
    actual_digest = hashlib.sha256(LIVEKIT_PATH.read_bytes()).hexdigest()
    assert len(LIVEKIT_PATH.read_bytes()) > 100_000
    assert actual_digest == expected_digest
    assert sources.index(livekit) < sources.index(client)
    assert not any(
        "livekit" in source and not source.startswith("/static/")
        for source in sources
    )


def test_client_uses_explicit_media_and_all_required_voice_frame_handlers() -> None:
    source = CLIENT_PATH.read_text(encoding="utf-8")
    required = {
        "navigator.mediaDevices.getUserMedia",
        'addEventListener("devicechange"',
        'addEventListener("pagehide"',
        'addEventListener("visibilitychange"',
        'case "composer_state"',
        'case "voice_control_binding"',
        'case "voice_session_state"',
        'case "voice_turn_state"',
        'case "voice_transcript"',
        'case "user_message_acked"',
        'case "voice_submission_rejected"',
        '"/api/voice/sessions"',
        '"/takeover"',
        '"/speech/stop"',
        "X-Astral-Voice-Control-Binding",
        "LivekitClient.Room",
        "startAudio()",
    }
    missing = sorted(item for item in required if item not in source)
    assert not missing, f"web voice controller is missing contract seams: {missing}"

    assert "SpeechRecognition" not in source
    assert "webkitSpeechRecognition" not in source
    assert "speechSynthesis" not in source


def test_client_requires_exact_worker_identity_at_data_and_track_boundaries() -> None:
    source = CLIENT_PATH.read_text(encoding="utf-8")

    assert "participant.identity !== voiceExpectedWorker" in source
    assert "frame.worker_identity !== voiceExpectedWorker" in source
    assert "published.participant.identity !== manifest.worker_identity" in source
    assert source.count("participant.identity !== voiceExpectedWorker") >= 3


def test_client_consumes_server_order_labels_and_canonical_rest_actions() -> None:
    source = CLIENT_PATH.read_text(encoding="utf-8")
    composer = _fixture_composer()
    mapping = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[
        "rest_operation_mapping"
    ]

    assert "control.label" in source
    assert "voice.controls" in source
    assert {
        control["action"] for control in composer["voice"]["controls"]
    } == set(mapping)
    for action in mapping:
        assert f'case "{action}"' in source


def test_client_distinguishes_request_terminal_failure_from_speech_failure() -> None:
    source = CLIENT_PATH.read_text(encoding="utf-8")

    assert 'failed: "Voice request did not complete."' in source
    assert 'refused: "Voice request did not start."' in source
    assert 'cancelled: "Voice request did not complete because it was cancelled."' in source
    assert 'abandoned: "Voice request did not complete."' in source
    assert "voiceTurnNoticeMessageEl.textContent = frame.message" in source
    assert "The text result may still be available in chat." in source


def test_accessible_css_exposes_non_color_voice_states() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    required_selectors = {
        ".astral-voice-control:focus-visible",
        '.astral-voice-control[aria-pressed="true"]',
        '.astral-voice-control[aria-busy="true"]',
        '.astral-voice-feedback[data-state="listening"]',
        '.astral-voice-feedback[data-state="muted"]',
        '.astral-voice-feedback[data-state="error"]',
        '.astral-voice-feedback[data-reason="permission_denied"]',
        ".astral-voice-turn-notice",
        ".astral-voice-turn-notice-icon",
        ".astral-voice-turn-notice-title",
    }
    missing = sorted(selector for selector in required_selectors if selector not in css)
    assert not missing, f"astral.css is missing accessible voice selectors: {missing}"
    assert ".astral-voice-state-label::before" in css
    assert "content:" in css
