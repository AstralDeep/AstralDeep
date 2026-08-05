"""Feature 060 Windows conversation continuity contract tests (T047/T053)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ["ASTRAL_WIN_AGENT"] = "0"

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QLabel  # noqa: E402

from astral_client import app as appmod  # noqa: E402
from astral_client.app import MainWindow  # noqa: E402
from astral_client.protocol import (  # noqa: E402
    CANONICAL_TEXT_PART_VARIANTS,
    ConversationCommitReady,
    ConversationContinuityReducer,
    ConversationResumeStore,
    OrchestratorClient,
    WindowsProtocolError,
    decode_semantic_transcript,
    parse_runtime_frame,
)
from astral_client.protocol_manifest import CLASSIFICATION, HANDLED  # noqa: E402


CHAT = "11111111-1111-4111-8111-111111111111"
OTHER_CHAT = "22222222-2222-4222-8222-222222222222"
CONNECTION = "33333333-3333-4333-8333-333333333333"
OTHER_CONNECTION = "44444444-4444-4444-8444-444444444444"
HYDRATION = "55555555-5555-4555-8555-555555555555"
NEXT_HYDRATION = "66666666-6666-4666-8666-666666666666"
COMMIT = "77777777-7777-4777-8777-777777777777"
DETACHED_COMMIT = "88888888-8888-4888-8888-888888888888"
SNAPSHOT_1 = "99999999-9999-4999-8999-999999999999"
SNAPSHOT_2 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SNAPSHOT_3 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _settings(path: Path) -> QSettings:
    settings = QSettings(str(path), QSettings.Format.IniFormat)
    settings.clear()
    settings.sync()
    return settings


def _snapshot(
    *,
    snapshot_id: str = SNAPSHOT_1,
    request: str = HYDRATION,
    purpose: str = "hydration",
    revision: int = 7,
    connection: str = CONNECTION,
    chat: str = CHAT,
    text: str = "The result is 21.",
) -> dict:
    return {
        "type": "conversation_snapshot",
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "chat_id": chat,
        "connection_generation": connection,
        "request_generation": request,
        "snapshot_purpose": purpose,
        "render_revision": revision,
        "committed_at": "2026-07-15T18:41:00Z",
        "transcript": [
            {
                "message_id": "1842",
                "role": "assistant",
                "created_at": "2026-07-15T18:40:59Z",
                "parts": [{"type": "text", "text": text}],
                "attachments": [],
            }
        ],
        "canvas": {
            "target": "canvas",
            "components": [{"type": "text", "content": text}],
        },
    }


def _commit_ready(**changes) -> dict:
    frame = {
        "type": "conversation_commit_ready",
        "schema_version": 1,
        "chat_id": CHAT,
        "connection_generation": CONNECTION,
        "request_generation": DETACHED_COMMIT,
        "render_revision": 8,
    }
    frame.update(changes)
    return frame


def test_qsettings_locator_is_account_scoped_minimal_and_restart_durable(tmp_path) -> None:
    settings = _settings(tmp_path / "resume.ini")
    store = ConversationResumeStore(settings)
    key = store.bind_account("https://iam.example/realms/prod", "user-7")

    assert key.startswith("astraldeep.active_chat.v1.")
    assert store.set_active_chat(CHAT) is True
    raw = settings.value(key, "", type=str)
    assert set(json.loads(raw)) == {"schema_version", "chat_id", "updated_at"}
    assert "user-7" not in raw and "https://" not in raw

    restarted = ConversationResumeStore(QSettings(str(tmp_path / "resume.ini"), QSettings.Format.IniFormat))
    restarted.bind_account("https://iam.example/realms/prod", "user-7")
    assert restarted.active_chat() == CHAT
    for transient in (
        "socket_close",
        "service_restart",
        "hydration_failure",
        "provider_failure",
    ):
        assert restarted.clear(transient, CHAT) is False
        assert restarted.active_chat() == CHAT


def test_locator_unknown_schema_is_retained_but_not_interpreted(tmp_path) -> None:
    settings = _settings(tmp_path / "unknown.ini")
    store = ConversationResumeStore(settings)
    key = store.bind_account("issuer", "subject")
    raw = json.dumps({"schema_version": 2, "chat_id": CHAT, "updated_at": "future"})
    settings.setValue(key, raw)
    settings.sync()

    assert store.active_chat() is None
    assert settings.value(key, "", type=str) == raw


@pytest.mark.parametrize(
    "reason",
    ["explicit_new_chat", "definitive_sign_out", "account_switch", "confirmed_deletion"],
)
def test_locator_clears_only_for_four_definitive_actions(tmp_path, reason) -> None:
    settings = _settings(tmp_path / f"{reason}.ini")
    store = ConversationResumeStore(settings)
    store.bind_account("issuer", "subject")
    store.set_active_chat(CHAT)

    assert store.clear(reason, CHAT) is True
    assert store.active_chat() is None


def test_confirmed_deletion_cannot_clear_a_different_chat(tmp_path) -> None:
    store = ConversationResumeStore(_settings(tmp_path / "deletion.ini"))
    store.bind_account("issuer", "subject")
    store.set_active_chat(CHAT)

    assert store.clear("confirmed_deletion", OTHER_CHAT) is False
    assert store.active_chat() == CHAT


def test_registration_load_and_turn_emit_fresh_purpose_bound_generations(qapp) -> None:
    client = OrchestratorClient("ws://127.0.0.1:9/ws", "token")
    client.configure_resume(CHAT)
    registration = client._register_frame()

    assert registration["resume"] == {
        "schema_version": 1,
        "active_chat_id": CHAT,
        "request_generation": client.request_generation,
    }
    assert client.request_purpose == "hydration"
    assert registration["connection_generation"] == client.connection_generation

    first = client.begin_conversation_request("hydration", CHAT)
    second = client.begin_conversation_request("commit", CHAT)
    assert first != second
    assert client.request_purpose == "commit"
    assert client.request_generation == second

    client.send_chat("first turn", request_generation=COMMIT)
    queued = json.loads(client._pending[-1])
    assert queued["payload"]["snapshot_purpose"] == "commit"
    assert queued["payload"]["request_generation"] == COMMIT
    assert "chat_id" not in queued["payload"]


def test_offline_first_turn_rebinds_to_the_next_connection_generation(qapp) -> None:
    client = OrchestratorClient("ws://127.0.0.1:9/ws", "token")
    client.connection_generation = OTHER_CONNECTION
    client.send_chat("first turn", request_generation=COMMIT)
    queued = client._pending.popleft()

    client.connection_generation = CONNECTION
    rebound = json.loads(client._rebind_pending_conversation_frame(queued))

    assert rebound["connection_generation"] == CONNECTION
    assert rebound["payload"]["connection_generation"] == CONNECTION
    assert rebound["payload"]["request_generation"] == COMMIT
    assert "chat_id" not in rebound["payload"]
    assert client.request_chat_id is None
    assert client.request_generation == COMMIT
    assert client.request_purpose == "commit"


def test_hydration_equal_revision_accepts_once_then_replay_conflict_and_commit_equal_reject() -> None:
    reducer = ConversationContinuityReducer()
    reducer.activate_chat(CHAT)
    reducer.bind_connection(CONNECTION)
    reducer.open_request("hydration", HYDRATION)
    assert reducer.reduce_snapshot(_snapshot()) == "snapshot_applied"
    assert reducer.last_committed_render_revision == 7

    reducer.bind_connection(OTHER_CONNECTION)
    reducer.open_request("hydration", NEXT_HYDRATION)
    equal = _snapshot(
        snapshot_id=SNAPSHOT_2,
        request=NEXT_HYDRATION,
        connection=OTHER_CONNECTION,
    )
    assert reducer.reduce_snapshot(equal) == "snapshot_applied"
    assert reducer.reduce_snapshot(dict(equal)) == "snapshot_replay"
    assert reducer.reduce_snapshot(dict(equal, snapshot_id=SNAPSHOT_3)) == "revision_conflict"

    reducer.open_request("commit", COMMIT)
    assert reducer.reduce_snapshot(
        _snapshot(request=COMMIT, purpose="commit", connection=OTHER_CONNECTION)
    ) == "unexpected_equal_commit"
    assert reducer.last_committed_render_revision == 7


def test_only_one_commit_snapshot_advances_and_wrong_generations_are_noops() -> None:
    reducer = ConversationContinuityReducer()
    reducer.activate_chat(CHAT)
    reducer.bind_connection(CONNECTION)
    reducer.open_request("commit", COMMIT)

    assert reducer.reduce_snapshot(
        _snapshot(request=COMMIT, purpose="commit", revision=1)
    ) == "snapshot_applied"
    assert reducer.reduce_snapshot(
        _snapshot(snapshot_id=SNAPSHOT_2, request=COMMIT, purpose="commit", revision=1)
    ) == "unexpected_equal_commit"
    assert reducer.reduce_snapshot(
        _snapshot(snapshot_id=SNAPSHOT_3, request=COMMIT, purpose="commit", revision=0)
    ) == "stale_frame_ignored"
    assert reducer.reduce_snapshot(
        _snapshot(snapshot_id=SNAPSHOT_3, request=HYDRATION, purpose="commit", revision=2)
    ) == "wrong_scope"
    assert reducer.last_committed_render_revision == 1


def test_commit_ready_opens_server_generation_only_for_exact_newer_active_scope() -> None:
    reducer = ConversationContinuityReducer()
    reducer.activate_chat(CHAT)
    reducer.bind_connection(CONNECTION)
    reducer.open_request("hydration", HYDRATION)
    reducer.reduce_snapshot(_snapshot(revision=7))

    assert reducer.reduce_commit_ready(_commit_ready()) == "commit_ready"
    assert reducer.request_generation == DETACHED_COMMIT
    assert reducer.request_purpose == "commit"
    assert reducer.reduce_snapshot(
        _snapshot(
            snapshot_id=SNAPSHOT_2,
            request=DETACHED_COMMIT,
            purpose="commit",
            revision=8,
        )
    ) == "snapshot_applied"

    for invalid in (
        _commit_ready(chat_id=OTHER_CHAT, request_generation=COMMIT, render_revision=9),
        _commit_ready(connection_generation=OTHER_CONNECTION, request_generation=COMMIT, render_revision=9),
        _commit_ready(request_generation=COMMIT, render_revision=8),
        dict(_commit_ready(request_generation=COMMIT, render_revision=9), unexpected=True),
    ):
        assert reducer.reduce_commit_ready(invalid) != "commit_ready"
    assert reducer.request_generation == DETACHED_COMMIT


def test_commit_ready_decoder_and_manifest_disposition_are_strict() -> None:
    parsed = parse_runtime_frame(_commit_ready())
    assert isinstance(parsed, ConversationCommitReady)
    assert parsed.render_revision == 8
    assert CLASSIFICATION["conversation_commit_ready"] == HANDLED
    with pytest.raises(ValueError, match="fields"):
        parse_runtime_frame(dict(_commit_ready(), unexpected=True))
    with pytest.raises(ValueError, match="version/type"):
        parse_runtime_frame(_commit_ready(schema_version=True))
    with pytest.raises(ValueError, match="version/type"):
        parse_runtime_frame(dict(_snapshot(), schema_version=True))


def test_semantic_decoder_preserves_parts_components_structured_recovery_and_attachments() -> None:
    transcript = [
        {
            "message_id": "m1",
            "role": "assistant",
            "created_at": "2026-07-15T18:40:59Z",
            "parts": [
                {"type": "text", "text": "Hello Ω"},
                {"type": "components", "components": [{"type": "text", "content": "21"}]},
                {"type": "structured", "value": {"total": 21}, "plain_text": "total: 21"},
                {
                    "type": "recovery",
                    "code": "saved_content_unrenderable",
                    "message": "A saved response could not be displayed.",
                },
            ],
            "attachments": [{"attachment_id": "a1", "filename": "rolls.json"}],
        }
    ]

    decoded = decode_semantic_transcript(transcript)
    assert [part.type for part in decoded[0].parts] == [
        "text", "components", "structured", "recovery"
    ]
    assert decoded[0].parts[0].text == "Hello Ω"
    assert decoded[0].parts[0].variant is None
    assert decoded[0].parts[1].components[0]["content"] == "21"
    assert decoded[0].parts[2].plain_text == "total: 21"
    assert decoded[0].parts[3].code == "saved_content_unrenderable"
    assert decoded[0].attachments[0]["filename"] == "rolls.json"


def test_text_part_variant_is_bounded_and_carried() -> None:
    """066 T023 contract extension: optional bounded variant on text parts."""

    def _message(part: dict) -> list[dict]:
        return [
            {
                "message_id": "m1",
                "role": "assistant",
                "created_at": "2026-08-05T10:00:00Z",
                "parts": [part],
                "attachments": [],
            }
        ]

    # Drift pin against the backend's closed set.
    assert CANONICAL_TEXT_PART_VARIANTS == frozenset({"caption"})

    decoded = decode_semantic_transcript(
        _message({"type": "text", "text": "As of July", "variant": "caption"})
    )
    assert decoded[0].parts[0].variant == "caption"
    assert decoded[0].parts[0].text == "As of July"

    with pytest.raises(WindowsProtocolError):
        decode_semantic_transcript(
            _message({"type": "text", "text": "words", "variant": "h1"})
        )
    with pytest.raises(WindowsProtocolError):
        decode_semantic_transcript(
            _message({"type": "text", "text": "words", "weight": "caption"})
        )


def test_request_scoped_transients_are_overlay_only_and_strictly_sequenced() -> None:
    reducer = ConversationContinuityReducer()
    reducer.activate_chat(CHAT)
    reducer.bind_connection(CONNECTION)
    reducer.open_request("commit", COMMIT)
    before = reducer.committed_snapshot
    frame = {
        "type": "ui_render",
        "target": "canvas",
        "components": [{"type": "text", "content": "preview"}],
        "chat_id": CHAT,
        "connection_generation": CONNECTION,
        "request_generation": COMMIT,
        "base_render_revision": 0,
        "frame_sequence": 1,
    }

    assert reducer.reduce_transient(frame) == "transient_overlay_applied"
    assert reducer.committed_snapshot is before
    assert reducer.overlay_frames == [frame]
    assert reducer.reduce_transient(dict(frame)) == "transient_frame_ignored"
    assert reducer.reduce_transient(dict(frame, frame_sequence=0)) == "transient_frame_ignored"
    assert reducer.reduce_transient(dict(frame, frame_sequence=2, chat_id=OTHER_CHAT)) == "transient_frame_ignored"

    committed = _snapshot(request=COMMIT, purpose="commit", revision=1)
    assert reducer.reduce_snapshot(committed) == "snapshot_applied"
    assert reducer.overlay_frames == []


class _Signal:
    def connect(self, *_args):
        pass

    def disconnect(self, *_args):
        pass


class _FakeClient:
    message = _Signal()
    status = _Signal()

    def __init__(self, *_args, **_kwargs):
        self.sent = []
        self.session_id = "win-client"
        self.connection_generation = CONNECTION
        self.request_generation = None
        self.request_purpose = None

    def configure_agent_host(self, _host_id):
        pass

    def configure_resume(self, chat_id):
        self.session_id = chat_id or "win-client"

    def begin_conversation_request(self, purpose, _chat_id, generation=None):
        self.request_generation = generation or (HYDRATION if purpose == "hydration" else COMMIT)
        self.request_purpose = purpose
        return self.request_generation

    def start(self):
        pass

    def stop(self):
        pass

    def send_event(self, action, payload, session_id=None):
        self.sent.append((action, payload, session_id))

    def send_chat(self, message, chat_id=None, attachments=None, request_generation=None):
        self.sent.append(("chat_message", {
            "message": message,
            "chat_id": chat_id,
            "attachments": attachments,
            "request_generation": request_generation,
        }, chat_id))

    def send_host_frame(self, _frame):
        pass


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "OrchestratorClient", _FakeClient)
    monkeypatch.setattr(appmod, "QSettings", lambda *_a, **_k: _settings(tmp_path / "app.ini"))
    monkeypatch.setattr(MainWindow, "_start_integrity_check", lambda self: None)
    monkeypatch.setattr(MainWindow, "_init_workspace", lambda self: None)
    monkeypatch.setattr(appmod, "load_or_create_host_id", lambda: SNAPSHOT_1)
    win = MainWindow("ws://127.0.0.1:9/ws", "dev-token", connect=False)
    yield win
    win.close()


def test_main_window_applies_snapshot_transcript_and_canvas_together(window) -> None:
    window._set_active_chat(CHAT)
    window._continuity.bind_connection(CONNECTION)
    window._continuity.open_request("hydration", HYDRATION)
    window._on_message(_snapshot())

    labels = [label.text() for label in window.rail.findChildren(QLabel)]
    assert any("The result is 21." in text for text in labels)
    assert window.canvas._last_components == [{"type": "text", "content": "The result is 21."}]
    assert window._continuity.last_committed_render_revision == 7


def test_commit_ready_is_bound_before_following_snapshot_in_main_window(window) -> None:
    window._set_active_chat(CHAT)
    window._continuity.bind_connection(CONNECTION)
    window._continuity.open_request("hydration", HYDRATION)
    window._on_message(_snapshot())

    window._on_message(_commit_ready())
    assert window._continuity.request_generation == DETACHED_COMMIT
    window._on_message(
        _snapshot(
            snapshot_id=SNAPSHOT_2,
            request=DETACHED_COMMIT,
            purpose="commit",
            revision=8,
            text="Detached result",
        )
    )
    assert window._continuity.last_committed_render_revision == 8


def test_transient_overlay_does_not_replace_committed_canvas(window) -> None:
    window._set_active_chat(CHAT)
    window._continuity.bind_connection(CONNECTION)
    window._continuity.open_request("hydration", HYDRATION)
    window._on_message(_snapshot())
    window._continuity.open_request("commit", COMMIT)
    window._on_message({
        "type": "ui_render",
        "target": "canvas",
        "components": [{"type": "text", "content": "preview"}],
        "chat_id": CHAT,
        "connection_generation": CONNECTION,
        "request_generation": COMMIT,
        "base_render_revision": 7,
        "frame_sequence": 1,
    })

    assert window.canvas._last_components == [{"type": "text", "content": "The result is 21."}]
    assert window.canvas._transient_overlay is not None


def test_confirmed_deleted_frame_clears_only_matching_locator(window) -> None:
    window._resume_store.bind_account("issuer", "subject")
    window._resume_store.set_active_chat(CHAT)
    window._set_active_chat(CHAT, persist=False)

    window._on_message({"type": "chat_deleted", "chat_id": OTHER_CHAT})
    assert window._resume_store.active_chat() == CHAT
    window._on_message({"type": "chat_deleted", "chat_id": CHAT})
    assert window._resume_store.active_chat() is None
    assert window.active_chat is None
