"""Feature 060 canonical progress and lifecycle reducers for native Windows."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ASTRAL_WIN_AGENT"] = "0"

from astral_client import app as appmod  # noqa: E402
from astral_client.app import MainWindow  # noqa: E402
from astral_client.protocol import (  # noqa: E402
    AdmissionRefusal,
    OrchestratorClient,
    WindowsProtocolError,
)


CHAT = "11111111-1111-4111-8111-111111111111"
CONNECTION = "22222222-2222-4222-8222-222222222222"
REQUEST = "33333333-3333-4333-8333-333333333333"
OPERATION = "44444444-4444-4444-8444-444444444444"
OPERATION_2 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OPERATION_3 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
REVISION = "55555555-5555-4555-8555-555555555555"
RUNTIME = "66666666-6666-4666-8666-666666666666"
HOST = "77777777-7777-4777-8777-777777777777"
SUBMISSION = "88888888-8888-4888-8888-888888888888"
OTHER = "99999999-9999-4999-8999-999999999999"


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_start_integrity_check", lambda self: None)
    monkeypatch.setattr(MainWindow, "_init_workspace", lambda self: None)
    monkeypatch.setattr(appmod, "load_or_create_host_id", lambda: HOST)
    win = MainWindow("ws://127.0.0.1:9/ws", "dev-token", connect=False)
    win._set_active_chat(CHAT)
    win._continuity.bind_connection(CONNECTION)
    win._continuity.open_request("commit", REQUEST)
    yield win
    win.close()


def operation(
    sequence: int,
    state: str,
    *,
    operation_id: str = OPERATION,
    request: str = REQUEST,
    action: str = "curated_example",
    chat_id: str | None = CHAT,
    connection: str = CONNECTION,
) -> dict:
    terminal = state in {"completed", "failed", "cancelled", "retryable"}
    retryable = state == "retryable"
    error = None
    if state in {"failed", "cancelled", "retryable"}:
        error = {"code": "operation_failed", "message": "Safe failure"}
    return {
        "type": "operation_status",
        "operation_id": operation_id,
        "action": action,
        "surface": "chat",
        "chat_id": chat_id,
        "connection_generation": connection,
        "request_generation": request,
        "sequence": sequence,
        "state": state,
        "phase": state,
        "label": state.title(),
        "terminal": terminal,
        "retryable": retryable,
        "error": error,
        "retry_after_ms": 500 if retryable else None,
        "updated_at": "2026-07-16T12:00:00Z",
    }


def lifecycle(generation: int, revision: int, state: str) -> dict:
    return {
        "type": "agent_lifecycle",
        "agent_id": "ua-dice",
        "revision_id": REVISION,
        "runtime_instance_id": None if state in {"failed", "offline"} else RUNTIME,
        "lifecycle_generation": generation,
        "state_revision": revision,
        "state": state,
        "reason_code": "child_exited" if state == "failed" else None,
        "label": f"Agent {state}",
        "updated_at": "2026-07-16T12:00:00Z",
    }


def refusal(submission_id: str = SUBMISSION, **overrides) -> dict:
    frame = {
        "type": "error",
        "submission_id": submission_id,
        "accepted": False,
        "code": "capacity_exceeded",
        "message": "Try again shortly.",
        "retryable": True,
        "retry_after_ms": 1000,
    }
    frame.update(overrides)
    return frame


def test_operation_keeps_highest_sequence_and_first_terminal_visible(window):
    assert window._reduce_operation_status(operation(0, "accepted"))
    assert window._reduce_operation_status(operation(1, "running"))
    assert not window._reduce_operation_status(operation(0, "accepted"))
    assert window._reduce_operation_status(operation(2, "failed"))
    assert not window._reduce_operation_status(operation(3, "completed"))

    retained = window._operation_status_by_id[OPERATION]
    assert retained.state == "failed"
    assert "Safe failure" in window._banner.text()
    assert "Safe failure" in window.topbar._mark.toolTip()


def test_success_is_idle_and_restores_a_different_active_operation(window):
    assert window._banner.text() == ""

    assert window._reduce_operation_status(operation(0, "accepted"))
    assert window._banner.text() == "Accepted"
    assert window._operation_banner_operation_id == OPERATION

    assert window._reduce_operation_status(
        operation(0, "running", operation_id=OPERATION_2)
    )
    assert window._banner.text() == "Running"
    assert window._operation_banner_operation_id == OPERATION_2

    # Completing the visible operation restores the other genuine activity;
    # "Completed" itself is never projected as a banner or busy state.
    assert window._reduce_operation_status(
        operation(1, "completed", operation_id=OPERATION_2)
    )
    assert window._banner.text() == "Accepted"
    assert window._operation_banner_operation_id == OPERATION

    assert window._reduce_operation_status(operation(1, "completed"))
    assert window._banner.text() == ""
    assert window._banner.isHidden()
    assert window.topbar._mark.toolTip() == "Connected"
    assert window._operation_status_by_id[OPERATION].state == "completed"
    assert window._operation_status_by_id[OPERATION_2].state == "completed"


def test_success_restores_a_newer_unacknowledged_local_submission(window):
    window.client.connection_generation = CONNECTION
    window.client._send = lambda _frame: None
    first = window.client.send_event("curated_example", {})

    assert window._reduce_operation_status(
        operation(
            0,
            "accepted",
            request=first.request_generation,
            action="curated_example",
            chat_id=None,
        )
    )
    second = window.client.send_event("table_paginate", {})
    assert window._banner.text() == "Submitting…"

    assert window._reduce_operation_status(
        operation(
            1,
            "running",
            request=first.request_generation,
            action="curated_example",
            chat_id=None,
        )
    )
    assert window._banner.text() == "Running"
    assert window._reduce_operation_status(
        operation(
            2,
            "completed",
            request=first.request_generation,
            action="curated_example",
            chat_id=None,
        )
    )
    assert window._banner.text() == "Submitting…"
    assert window._operation_banner_request_generation == second.request_generation

    assert window._reduce_operation_status(
        operation(
            0,
            "completed",
            operation_id=OPERATION_2,
            request=second.request_generation,
            action="table_paginate",
            chat_id=None,
        )
    )
    assert window._banner.text() == ""
    assert window._banner.isHidden()


def test_replayed_success_on_initial_idle_does_not_create_completed_notice(window):
    assert window._banner.text() == ""

    assert window._reduce_operation_status(operation(0, "completed"))

    assert window._banner.text() == ""
    assert window._banner.isHidden()
    assert "Completed" not in window.topbar._mark.toolTip()
    assert window._operation_status_by_id[OPERATION].state == "completed"


def test_success_before_commit_snapshot_preserves_transient_answer(window):
    window._apply_transient_frame(
        {
            "type": "ui_render",
            "target": "chat",
            "components": [{"type": "text", "content": "Transient answer"}],
        }
    )
    window._apply_transient_frame(
        {
            "type": "ui_render",
            "target": "canvas",
            "components": [{"type": "text", "content": "Transient canvas"}],
        }
    )

    assert window._reduce_operation_status(
        operation(0, "completed", action="chat_message")
    )

    assert window._transient_chat_lines == ["Transient answer"]
    assert window._transient_canvas_components == [
        {"type": "text", "content": "Transient canvas"}
    ]
    assert window.canvas._transient_overlay is not None
    assert window._banner.text() == ""
    assert window._banner.isHidden()


def test_startup_metadata_is_retained_without_activity_banner(window):
    window.client.connection_generation = CONNECTION
    window.client._send = lambda _frame: None

    local = window.client.send_event("get_history", {})

    assert local.request_generation in window._pending_submissions_by_generation
    assert window._banner.text() == ""
    assert window._banner.isHidden()
    assert window._reduce_operation_status(
        operation(
            0,
            "accepted",
            request=local.request_generation,
            action="get_history",
            chat_id=None,
        )
    )
    assert window._banner.text() == ""
    assert window._banner.isHidden()
    assert window._reduce_operation_status(
        operation(
            1,
            "completed",
            request=local.request_generation,
            action="get_history",
            chat_id=None,
        )
    )
    assert local.request_generation not in window._pending_submissions_by_generation
    assert window._banner.text() == ""


def test_terminal_failure_is_settled_and_unrelated_success_cannot_erase_it(window):
    assert window._reduce_operation_status(
        operation(0, "running", operation_id=OPERATION_2)
    )
    assert window._reduce_operation_status(
        operation(0, "failed", operation_id=OPERATION_3)
    )
    assert window._banner.text() == "Safe failure"
    assert window._banner_kind == "error"
    assert window._operation_banner_operation_id is None

    assert window._reduce_operation_status(
        operation(1, "completed", operation_id=OPERATION_2)
    )
    assert window._banner.text() == "Safe failure"
    assert window._banner_kind == "error"
    assert window.topbar._mark.toolTip() == "Safe failure"


def test_operation_drops_a_stale_request_without_mutating_visible_state(window):
    stale = OTHER
    assert not window._reduce_operation_status(operation(0, "accepted", request=stale))
    assert OPERATION not in window._operation_status_by_id


def test_twenty_five_state_sequences_converge_without_agent_list_reload(window):
    window._on_message(
        {
            "type": "agent_list",
            "agents": [
                {
                    "id": "ua-dice",
                    "name": "Dice",
                    "description": "Rolls dice",
                    "scopes": {},
                    "is_public": False,
                }
            ],
        }
    )
    states = ["starting", "online", "updating", "failed", "offline"]
    for generation in range(1, 21):
        for revision, state in enumerate(states):
            assert window._reduce_agent_lifecycle(
                lifecycle(generation, revision, state)
            )
        assert not window._reduce_agent_lifecycle(
            lifecycle(generation, 1, "online")
        )

    current = window._agent_lifecycle_by_id["ua-dice"]
    assert (current.lifecycle_generation, current.state) == (20, "offline")
    assert window._agents[0]["_lifecycle_label"] == "Agent offline"
    assert "Agent offline" in window._banner.text()


def test_transport_projects_identity_before_io_and_preserves_canonical_values(qapp):
    client = OrchestratorClient("ws://127.0.0.1:9/ws", "dev-token")
    order: list[tuple[str, object]] = []
    client.submission.connect(lambda local: order.append(("submission", local)))
    client._send = lambda frame: order.append(("send", frame))

    local = client.send_event(
        "curated_example",
        {"submission_id": SUBMISSION, "request_generation": REQUEST},
    )

    assert [entry[0] for entry in order] == ["submission", "send"]
    assert local.submission_id == SUBMISSION
    assert local.request_generation == REQUEST
    frame = order[1][1]
    assert isinstance(frame, dict)
    assert frame["submission_id"] == frame["payload"]["submission_id"] == SUBMISSION
    assert frame["request_generation"] == frame["payload"]["request_generation"] == REQUEST


def test_transport_generates_canonical_identity_for_chat_and_surface(qapp):
    client = OrchestratorClient("ws://127.0.0.1:9/ws", "dev-token")
    sent: list[dict] = []
    client._send = sent.append

    surface = client.send_event("discover_agents", {})
    chat = client.send_chat("hello", CHAT)

    for local, frame in zip((surface, chat), sent, strict=True):
        assert uuid.UUID(local.submission_id).version == 4
        assert uuid.UUID(local.request_generation).version == 4
        assert frame["submission_id"] == frame["payload"]["submission_id"]
        assert frame["request_generation"] == frame["payload"]["request_generation"]
    assert chat.action == "chat_message"
    assert chat.chat_id == CHAT


def test_surface_before_chat_submits_then_accepts_and_terminalizes(window):
    window._set_active_chat(None, persist=False)
    window.client.connection_generation = CONNECTION
    window.client._send = lambda _frame: None

    local = window.client.send_event("curated_example", {})

    assert window.active_chat is None
    assert window._pending_submissions_by_generation[local.request_generation] == local
    assert window._pending_submissions_by_id[local.submission_id] == local
    assert window._banner.text() == "Submitting…"
    assert window._reduce_operation_status(
        operation(
            0,
            "accepted",
            request=local.request_generation,
            chat_id=None,
        )
    )
    assert window._reduce_operation_status(
        operation(
            1,
            "completed",
            request=local.request_generation,
            chat_id=None,
        )
    )
    assert not window._pending_submissions_by_generation
    assert not window._pending_submissions_by_id


def test_chat_terminal_uses_retained_submission_after_request_state_clears(window):
    window.client.connection_generation = CONNECTION
    window.client._send = lambda _frame: None
    local = window.client.send_chat("hello", CHAT, request_generation=REQUEST)
    assert local.chat_id == CHAT

    # A complete commit snapshot may clear the conversation request before the
    # durable operation's terminal projection arrives. The retained local
    # submission remains the correlation fence.
    window._continuity._request = None

    assert window._reduce_operation_status(
        operation(1, "completed", action="chat_message")
    )
    assert not window._pending_submissions_by_generation
    wrong_chat = operation(2, "completed", request=OTHER, chat_id=OTHER)
    assert not window._reduce_operation_status(wrong_chat)


def test_admission_refusal_and_disconnect_clear_only_local_pending(window):
    window.client.connection_generation = CONNECTION
    window.client._send = lambda _frame: None
    refused = window.client.send_event("curated_example", {})
    retained = window.client.send_event("discover_agents", {})

    for uncorrelated in (
        refusal(None),
        refusal(OTHER),
        refusal("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
    ):
        window._on_message(uncorrelated)
        assert refused.submission_id in window._pending_submissions_by_id
        assert retained.submission_id in window._pending_submissions_by_id

    window._on_message(
        {
            "type": "error",
            "submission_id": refused.submission_id,
            "accepted": False,
            "code": "capacity_exceeded",
            "message": "Try again shortly.",
            "retryable": True,
            "retry_after_ms": 1000,
        }
    )

    assert refused.submission_id not in window._pending_submissions_by_id
    assert retained.submission_id in window._pending_submissions_by_id
    assert "Try again shortly." in window._banner.text()
    window._on_status("closed:test")
    assert not window._pending_submissions_by_generation
    assert not window._pending_submissions_by_id


def test_admission_refusal_parser_accepts_only_the_exact_canonical_shape():
    for code in AdmissionRefusal._CODES:
        parsed = AdmissionRefusal.from_dict(
            refusal(code=code, retry_after_ms=None)
        )
        assert parsed.submission_id == SUBMISSION
        assert parsed.code == code

    non_retryable = AdmissionRefusal.from_dict(
        refusal(
            code="registration_required",
            retryable=False,
            retry_after_ms=None,
        )
    )
    assert not non_retryable.retryable
    assert non_retryable.retry_after_ms is None

    base = refusal()
    without_delay = dict(base)
    without_delay.pop("retry_after_ms")
    invalid = [
        {**base, "unexpected": True},
        without_delay,
        {**base, "submission_id": None},
        {
            **base,
            "submission_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        },
        {**base, "code": "raw_internal_trace"},
        {**base, "message": "  "},
        {**base, "accepted": True},
        {**base, "retryable": "true"},
        {**base, "retryable": False, "retry_after_ms": 1},
        {**base, "retry_after_ms": -1},
        {**base, "retry_after_ms": 1.5},
        {**base, "retry_after_ms": "1000"},
    ]
    for frame in invalid:
        with pytest.raises((TypeError, WindowsProtocolError)):
            AdmissionRefusal.from_dict(frame)


def test_malformed_admission_refusals_render_generically_without_settling(window):
    window.client.connection_generation = CONNECTION
    window.client._send = lambda _frame: None
    pending = window.client.send_event("curated_example", {})
    base = refusal(pending.submission_id)
    malformed = [
        {**base, "unexpected": True},
        {**base, "code": "raw_internal_trace"},
        {**base, "retryable": False, "retry_after_ms": 1},
    ]

    for frame in malformed:
        window._on_message(frame)
        assert pending.submission_id in window._pending_submissions_by_id

    assert window._reduce_admission_refusal(base)
    assert pending.submission_id not in window._pending_submissions_by_id


def test_operation_and_lifecycle_reject_noncanonical_codes_and_active_identity(window):
    bad_error = operation(0, "failed")
    bad_error["error"] = {"code": "internal_trace", "message": "Safe"}
    assert not window._reduce_operation_status(bad_error)

    bad_reason = lifecycle(1, 0, "failed")
    bad_reason["reason_code"] = "raw_child_trace"
    assert not window._reduce_agent_lifecycle(bad_reason)

    missing_runtime = lifecycle(1, 0, "starting")
    missing_runtime["runtime_instance_id"] = None
    assert not window._reduce_agent_lifecycle(missing_runtime)
    missing_revision = lifecycle(1, 0, "online")
    missing_revision["revision_id"] = None
    assert not window._reduce_agent_lifecycle(missing_revision)


def test_queued_surface_rebind_preserves_identity_and_uses_current_connection(qapp):
    client = OrchestratorClient("ws://127.0.0.1:9/ws", "dev-token")
    local = client.send_event("curated_example", {})
    client.connection_generation = CONNECTION

    rebound = json.loads(
        client._rebind_pending_conversation_frame(client._pending.popleft())
    )

    assert rebound["connection_generation"] == CONNECTION
    assert rebound["submission_id"] == rebound["payload"]["submission_id"] == local.submission_id
    assert (
        rebound["request_generation"]
        == rebound["payload"]["request_generation"]
        == local.request_generation
    )


def test_queued_surface_disconnect_reconnect_restores_before_accepted_and_terminal(window, qapp):
    window._set_active_chat(None, persist=False)
    window.client.connection_generation = CONNECTION
    local = window.client.send_event("curated_example", {})
    assert local.submission_id in window._pending_submissions_by_id

    window._on_status("closed:test")
    assert not window._pending_submissions_by_id
    window.client.connection_generation = OTHER
    sent: list[str] = []

    class FakeWs:
        async def send(self, frame):
            # This executes on the transport worker only after the GUI-thread
            # acknowledgement has installed both the generation and local map.
            assert window._continuity.connection_generation == OTHER
            assert local.submission_id in window._pending_submissions_by_id
            sent.append(frame)

    error: list[BaseException] = []

    def flush_on_transport_thread():
        try:
            asyncio.run(window.client._flush_pending(FakeWs()))
        except BaseException as exc:  # make worker assertion failures visible
            error.append(exc)

    worker = threading.Thread(target=flush_on_transport_thread)
    worker.start()
    deadline = time.monotonic() + 3.0
    while worker.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    worker.join(timeout=1.0)
    assert not worker.is_alive(), "queued replay acknowledgement deadlocked"
    assert not error

    assert local.submission_id in window._pending_submissions_by_id
    assert window._banner.text() == "Submitting…"
    replayed = json.loads(sent[0])
    assert replayed["submission_id"] == local.submission_id
    assert replayed["request_generation"] == local.request_generation
    accepted = operation(
        0,
        "accepted",
        request=local.request_generation,
        chat_id=None,
        connection=OTHER,
    )
    accepted["surface"] = "operation"
    assert window._reduce_operation_status(accepted)
    terminal = operation(
        1,
        "completed",
        request=local.request_generation,
        chat_id=None,
        connection=OTHER,
    )
    terminal["surface"] = "operation"
    assert window._reduce_operation_status(terminal)
    assert local.submission_id not in window._pending_submissions_by_id


def test_queued_chat_disconnect_reconnect_accepts_commit_snapshot_and_terminal(window):
    window.client.connection_generation = CONNECTION
    local = window.client.send_chat("queued turn", CHAT)
    window._on_status("closed:test")
    assert local.submission_id not in window._pending_submissions_by_id
    window.client.connection_generation = OTHER
    sent: list[str] = []

    class FakeWs:
        async def send(self, frame):
            assert window._continuity.connection_generation == OTHER
            assert window._continuity.request_generation == local.request_generation
            assert local.submission_id in window._pending_submissions_by_id
            sent.append(frame)

    asyncio.run(window.client._flush_pending(FakeWs()))

    assert local.submission_id in window._pending_submissions_by_id
    assert window._continuity.connection_generation == OTHER
    assert window._continuity.request_generation == local.request_generation
    assert window._reduce_operation_status(
        operation(
            0,
            "accepted",
            request=local.request_generation,
            action="chat_message",
            connection=OTHER,
        )
    )
    window._on_message(
        {
            "type": "conversation_commit_ready",
            "schema_version": 1,
            "chat_id": CHAT,
            "connection_generation": OTHER,
            "request_generation": local.request_generation,
            "render_revision": 1,
        }
    )
    window._on_message(
        {
            "type": "conversation_snapshot",
            "schema_version": 1,
            "snapshot_id": REVISION,
            "chat_id": CHAT,
            "connection_generation": OTHER,
            "request_generation": local.request_generation,
            "snapshot_purpose": "commit",
            "render_revision": 1,
            "committed_at": "2026-07-16T12:00:01Z",
            "transcript": [],
            "canvas": {"target": "canvas", "components": []},
        }
    )
    assert window._continuity.last_committed_render_revision == 1
    assert local.submission_id in window._pending_submissions_by_id
    assert window._reduce_operation_status(
        operation(
            1,
            "completed",
            request=local.request_generation,
            action="chat_message",
            connection=OTHER,
        )
    )
    assert local.submission_id not in window._pending_submissions_by_id
