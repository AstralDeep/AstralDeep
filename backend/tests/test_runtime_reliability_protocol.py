"""Tests for the canonical runtime-reliability wire protocol (shared/protocol.py):
ui_event, conversation-snapshot, operation-status, and agent-lifecycle round trips,
generation fencing, and fail-closed validation.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from shared.protocol import (
    AgentHostRegistered,
    AgentHostRegistration,
    AgentLifecycle,
    CandidateCapabilities,
    CandidateCapabilityMap,
    ConversationCommitReady,
    ConversationFrameFence,
    ConversationSnapshot,
    FrameDisposition,
    Message,
    OperationStatus,
    PersonalAgentHostCapabilities,
    PersonalAgentHostCapability,
    ProtocolValidationError,
    RegisterUI,
    RuntimeFence,
    TransientFrameScope,
    UIEvent,
)


CHAT_ID = "11111111-1111-4111-8111-111111111111"
CONNECTION_GENERATION = "22222222-2222-4222-8222-222222222222"
REQUEST_GENERATION = "33333333-3333-4333-8333-333333333333"
SNAPSHOT_ID = "44444444-4444-4444-8444-444444444444"
OPERATION_ID = "55555555-5555-4555-8555-555555555555"
HOST_ID = "66666666-6666-4666-8666-666666666666"
HOST_SESSION_ID = "77777777-7777-4777-8777-777777777777"
DELIVERY_ID = "88888888-8888-4888-8888-888888888888"
REVISION_ID = "99999999-9999-4999-8999-999999999999"
RUNTIME_INSTANCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROCESS_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LOCK_SHA256 = "ab" * 32
SUBMISSION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _snapshot(
    *,
    snapshot_id: str = SNAPSHOT_ID,
    snapshot_purpose: str = "hydration",
    render_revision: int = 7,
    text: str = "Committed answer",
    connection_generation: str = CONNECTION_GENERATION,
    request_generation: str = REQUEST_GENERATION,
) -> ConversationSnapshot:
    return ConversationSnapshot(
        schema_version=1,
        snapshot_id=snapshot_id,
        chat_id=CHAT_ID,
        connection_generation=connection_generation,
        request_generation=request_generation,
        snapshot_purpose=snapshot_purpose,
        render_revision=render_revision,
        committed_at="2026-07-15T18:41:00Z",
        transcript=[
            {
                "message_id": "1842",
                "role": "assistant",
                "created_at": "2026-07-15T18:40:59Z",
                "parts": [{"type": "text", "text": text}],
                "attachments": [],
            }
        ],
        canvas={"target": "canvas", "components": []},
    )


def test_ui_event_round_trip_accepts_exact_client_operation_identity() -> None:
    payload = {
        "submission_id": SUBMISSION_ID,
        "request_generation": REQUEST_GENERATION,
        "connection_generation": CONNECTION_GENERATION,
        "surface": "runtime_probe",
    }
    frame = UIEvent(
        action="future_generic_action",
        payload=payload,
        session_id=CHAT_ID,
        submission_id=SUBMISSION_ID,
        request_generation=REQUEST_GENERATION,
        connection_generation=CONNECTION_GENERATION,
        surface="runtime_probe",
    )

    parsed = Message.from_json(frame.to_json())

    assert isinstance(parsed, UIEvent)
    assert parsed == frame


@pytest.mark.parametrize(
    "mutation",
    [
        {"submission_id": "DDDDDDDD-DDDD-4DDD-8DDD-DDDDDDDDDDDD"},
        {"request_generation": "{33333333-3333-4333-8333-333333333333}"},
        {"submission_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"},
    ],
)
def test_ui_event_rejects_noncanonical_or_conflicting_identity(
    mutation: dict[str, str],
) -> None:
    wire = {
        "type": "ui_event",
        "action": "future_generic_action",
        "payload": {
            "submission_id": SUBMISSION_ID,
            "request_generation": REQUEST_GENERATION,
            "connection_generation": CONNECTION_GENERATION,
        },
        "submission_id": SUBMISSION_ID,
        "request_generation": REQUEST_GENERATION,
        "connection_generation": CONNECTION_GENERATION,
    }
    wire.update(mutation)

    with pytest.raises(ProtocolValidationError):
        Message.from_json(json.dumps(wire))


def test_conversation_snapshot_round_trip_has_every_canonical_field() -> None:
    frame = _snapshot()

    frame.validate()
    payload = json.loads(frame.to_json())

    assert payload == {
        "type": "conversation_snapshot",
        "schema_version": 1,
        "snapshot_id": SNAPSHOT_ID,
        "chat_id": CHAT_ID,
        "connection_generation": CONNECTION_GENERATION,
        "request_generation": REQUEST_GENERATION,
        "snapshot_purpose": "hydration",
        "render_revision": 7,
        "committed_at": "2026-07-15T18:41:00Z",
        "transcript": frame.transcript,
        "canvas": {"target": "canvas", "components": []},
    }
    parsed = Message.from_json(frame.to_json())
    assert isinstance(parsed, ConversationSnapshot)
    assert parsed == frame


def test_server_originated_commit_ready_round_trip_is_exact() -> None:
    frame = ConversationCommitReady(
        chat_id=CHAT_ID,
        connection_generation=CONNECTION_GENERATION,
        request_generation=REQUEST_GENERATION,
        render_revision=8,
    )

    assert json.loads(frame.to_json()) == {
        "type": "conversation_commit_ready",
        "schema_version": 1,
        "chat_id": CHAT_ID,
        "connection_generation": CONNECTION_GENERATION,
        "request_generation": REQUEST_GENERATION,
        "render_revision": 8,
    }
    assert Message.from_json(frame.to_json()) == frame

    with pytest.raises(ProtocolValidationError, match="exactly"):
        ConversationCommitReady.from_dict(
            {**json.loads(frame.to_json()), "unexpected": True}
        )
    with pytest.raises(ProtocolValidationError, match="positive"):
        dataclasses.replace(frame, render_revision=0).validate()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"snapshot_purpose": "preview"}, "snapshot_purpose"),
        ({"render_revision": -1}, "render_revision"),
        ({"canvas": []}, "canvas"),
        ({"canvas": {"target": "chat", "components": []}}, "canvas"),
        ({"request_generation": "not-a-uuid"}, "request_generation"),
    ],
)
def test_conversation_snapshot_validation_fails_closed(
    changes: dict[str, object], message: str
) -> None:
    values = {
        field.name: getattr(_snapshot(), field.name)
        for field in dataclasses.fields(ConversationSnapshot)
        if field.name != "type"
    }
    values.update(changes)
    frame = ConversationSnapshot(**values)

    with pytest.raises(ProtocolValidationError, match=message):
        frame.validate()


def test_empty_committed_conversation_is_a_valid_snapshot() -> None:
    frame = dataclasses.replace(_snapshot(), transcript=[])

    frame.validate()
    assert json.loads(frame.to_json())["transcript"] == []


def test_equal_revision_is_accepted_once_only_for_fresh_hydration() -> None:
    fence = ConversationFrameFence(
        chat_id=CHAT_ID,
        connection_generation=CONNECTION_GENERATION,
        request_generation=REQUEST_GENERATION,
        request_purpose="hydration",
        last_committed_render_revision=7,
    )
    snapshot = _snapshot(render_revision=7)

    assert fence.accept_snapshot(snapshot) is FrameDisposition.APPLY
    assert fence.last_committed_render_revision == 7
    assert fence.accept_snapshot(snapshot) is FrameDisposition.REPLAY
    assert fence.accept_snapshot(
        _snapshot(snapshot_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    ) is FrameDisposition.REVISION_CONFLICT
    assert fence.accept_snapshot(_snapshot(text="mutated")) is FrameDisposition.REVISION_CONFLICT


def test_equal_commit_never_advances_and_one_snapshot_owns_each_revision() -> None:
    fence = ConversationFrameFence(
        chat_id=CHAT_ID,
        connection_generation=CONNECTION_GENERATION,
        request_generation=REQUEST_GENERATION,
        request_purpose="commit",
        last_committed_render_revision=7,
    )

    equal = _snapshot(snapshot_purpose="commit", render_revision=7)
    next_commit = _snapshot(snapshot_purpose="commit", render_revision=8)

    assert fence.accept_snapshot(equal) is FrameDisposition.UNEXPECTED_EQUAL_COMMIT
    assert fence.accept_snapshot(next_commit) is FrameDisposition.APPLY
    assert fence.last_committed_render_revision == 8
    assert fence.accept_snapshot(next_commit) is FrameDisposition.UNEXPECTED_EQUAL_COMMIT
    assert fence.accept_snapshot(
        _snapshot(
            snapshot_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            snapshot_purpose="commit",
            render_revision=6,
        )
    ) is FrameDisposition.STALE


def test_snapshot_scope_and_purpose_must_match_registered_generation() -> None:
    fence = ConversationFrameFence(
        chat_id=CHAT_ID,
        connection_generation=CONNECTION_GENERATION,
        request_generation=REQUEST_GENERATION,
        request_purpose="hydration",
        last_committed_render_revision=7,
    )

    assert fence.accept_snapshot(
        _snapshot(connection_generation="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    ) is FrameDisposition.WRONG_SCOPE
    assert fence.accept_snapshot(
        _snapshot(request_generation="ffffffff-ffff-4fff-8fff-ffffffffffff")
    ) is FrameDisposition.WRONG_SCOPE
    assert fence.accept_snapshot(
        _snapshot(snapshot_purpose="commit", render_revision=8)
    ) is FrameDisposition.WRONG_PURPOSE
    assert fence.last_committed_render_revision == 7


def test_transient_overlay_requires_current_base_and_strict_sequence() -> None:
    fence = ConversationFrameFence(
        chat_id=CHAT_ID,
        connection_generation=CONNECTION_GENERATION,
        request_generation=REQUEST_GENERATION,
        request_purpose="commit",
        last_committed_render_revision=7,
    )

    def scope(sequence: int, **changes: object) -> TransientFrameScope:
        values = {
            "chat_id": CHAT_ID,
            "connection_generation": CONNECTION_GENERATION,
            "request_generation": REQUEST_GENERATION,
            "base_render_revision": 7,
            "frame_sequence": sequence,
        }
        values.update(changes)
        return TransientFrameScope(**values)

    assert fence.accept_transient(scope(1)) is FrameDisposition.APPLY_OVERLAY
    assert fence.accept_transient(scope(1)) is FrameDisposition.OUT_OF_ORDER
    assert fence.accept_transient(scope(0)) is FrameDisposition.OUT_OF_ORDER
    assert fence.accept_transient(scope(2)) is FrameDisposition.APPLY_OVERLAY
    assert fence.accept_transient(
        scope(3, base_render_revision=6)
    ) is FrameDisposition.WRONG_BASE_REVISION
    assert fence.accept_transient(
        scope(3, chat_id="12121212-1212-4212-8212-121212121212")
    ) is FrameDisposition.WRONG_SCOPE

    assert fence.accept_snapshot(
        _snapshot(snapshot_purpose="commit", render_revision=8)
    ) is FrameDisposition.APPLY
    assert fence.accept_transient(scope(3)) is FrameDisposition.WRONG_BASE_REVISION


def test_operation_status_round_trip_and_state_flags() -> None:
    active = OperationStatus(
        operation_id=OPERATION_ID,
        action="chrome_llm_save",
        surface="llm_settings",
        chat_id=None,
        connection_generation=CONNECTION_GENERATION,
        request_generation=REQUEST_GENERATION,
        sequence=2,
        state="validating",
        phase="validating_credentials",
        label="Checking your provider credentials…",
        terminal=False,
        retryable=False,
        error=None,
        retry_after_ms=None,
        updated_at="2026-07-15T18:41:00Z",
    )
    terminal = dataclasses.replace(
        active,
        sequence=3,
        state="retryable",
        phase="provider_probe",
        label="Provider unavailable",
        terminal=True,
        retryable=True,
        error={"code": "provider_unavailable", "message": "Try again shortly."},
        retry_after_ms=1000,
    )

    active.validate()
    terminal.validate()
    active_payload = json.loads(active.to_json())
    assert set(active_payload) == {
        "type",
        "operation_id",
        "action",
        "surface",
        "chat_id",
        "connection_generation",
        "request_generation",
        "sequence",
        "state",
        "phase",
        "label",
        "terminal",
        "retryable",
        "error",
        "retry_after_ms",
        "updated_at",
    }
    assert active_payload["chat_id"] is None
    assert active_payload["error"] is None
    assert isinstance(Message.from_json(terminal.to_json()), OperationStatus)


@pytest.mark.parametrize(
    "changes",
    [
        {"terminal": True},
        {"retryable": True},
        {"state": "completed", "terminal": False},
        {"state": "completed", "terminal": True, "error": {"code": "x", "message": "x"}},
        {"state": "failed", "terminal": True, "error": None},
        {"state": "retryable", "terminal": True, "retryable": True, "error": None},
        {"retry_after_ms": 1},
        {"sequence": -1},
        {"action": "Not Snake Case"},
    ],
)
def test_operation_status_rejects_invalid_state_projection(
    changes: dict[str, object]
) -> None:
    frame = OperationStatus(
        operation_id=OPERATION_ID,
        action="chrome_llm_save",
        surface="llm_settings",
        chat_id=None,
        connection_generation=CONNECTION_GENERATION,
        request_generation=REQUEST_GENERATION,
        sequence=0,
        state="accepted",
        phase="accepted",
        label="Accepted",
        terminal=False,
        retryable=False,
        error=None,
        retry_after_ms=None,
        updated_at="2026-07-15T18:41:00Z",
    )
    invalid = dataclasses.replace(frame, **changes)

    with pytest.raises(ProtocolValidationError):
        invalid.validate()


def test_agent_lifecycle_round_trip_carries_both_generation_fences() -> None:
    frame = AgentLifecycle(
        agent_id="ua-dice-4f3c2a",
        revision_id=REVISION_ID,
        runtime_instance_id=RUNTIME_INSTANCE_ID,
        lifecycle_generation=14,
        state_revision=3,
        state="online",
        reason_code=None,
        label="Online",
        updated_at="2026-07-15T18:41:00Z",
    )

    frame.validate()
    payload = json.loads(frame.to_json())
    assert payload["type"] == "agent_lifecycle"
    assert payload["lifecycle_generation"] == 14
    assert payload["state_revision"] == 3
    assert payload["reason_code"] is None
    assert isinstance(Message.from_json(frame.to_json()), AgentLifecycle)


def test_structured_v2_host_registration_and_server_ack_round_trip() -> None:
    host = AgentHostRegistration(
        host_id=HOST_ID,
        supported_runtime_contract_versions=(2,),
        runtime_lock_sha256=LOCK_SHA256,
        platform="windows",
        client_version="0.4.0",
    )
    registration = RegisterUI(
        capabilities=["render", "agent_host"],
        session_id="ui-session",
        connection_generation=CONNECTION_GENERATION,
        agent_host=host,
    )
    acknowledgement = AgentHostRegistered(
        host_id=HOST_ID,
        host_session_id=HOST_SESSION_ID,
        inventory_required=True,
        accepted_at="2026-07-15T18:41:00Z",
    )

    host.validate()
    acknowledgement.validate()
    registration_payload = json.loads(registration.to_json())
    assert registration_payload["agent_host"] == {
        "host_id": HOST_ID,
        "supported_runtime_contract_versions": [2],
        "runtime_lock_sha256": LOCK_SHA256,
        "platform": "windows",
        "client_version": "0.4.0",
    }
    assert registration_payload.get("host_session_id") is None
    parsed_registration = Message.from_json(registration.to_json())
    assert isinstance(parsed_registration, RegisterUI)
    assert isinstance(parsed_registration.agent_host, AgentHostRegistration)
    assert parsed_registration.agent_host == host
    assert isinstance(Message.from_json(acknowledgement.to_json()), AgentHostRegistered)


@pytest.mark.parametrize(
    "changes",
    [
        {"host_id": "not-a-uuid"},
        {"supported_runtime_contract_versions": ()},
        {"supported_runtime_contract_versions": (2, 2)},
        {"supported_runtime_contract_versions": (0, 2)},
        {"runtime_lock_sha256": LOCK_SHA256.upper()},
        {"platform": "linux"},
        {"client_version": " 0.4.0"},
        {"client_version": "0.4"},
    ],
)
def test_host_registration_validation_fails_closed(changes: dict[str, object]) -> None:
    host = AgentHostRegistration(
        host_id=HOST_ID,
        supported_runtime_contract_versions=(2,),
        runtime_lock_sha256=LOCK_SHA256,
        platform="windows",
        client_version="0.4.0",
    )
    invalid = dataclasses.replace(host, **changes)

    with pytest.raises(ProtocolValidationError):
        invalid.validate()


def test_runtime_fence_is_nullable_prelaunch_and_process_binds_once() -> None:
    prelaunch = RuntimeFence(
        agent_id="ua-dice-4f3c2a",
        host_id=HOST_ID,
        host_session_id=HOST_SESSION_ID,
        delivery_id=DELIVERY_ID,
        revision_id=REVISION_ID,
        runtime_instance_id=RUNTIME_INSTANCE_ID,
        process_id=None,
        lifecycle_generation=14,
    )

    prelaunch.validate(allow_prelaunch=True)
    with pytest.raises(ProtocolValidationError, match="process_id"):
        prelaunch.validate(allow_prelaunch=False)

    bound = prelaunch.bind_process(PROCESS_ID)
    assert prelaunch.process_id is None
    assert bound.process_id == PROCESS_ID
    bound.validate(allow_prelaunch=False)
    with pytest.raises(ProtocolValidationError, match="already bound"):
        bound.bind_process("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    with pytest.raises(dataclasses.FrozenInstanceError):
        bound.process_id = None  # type: ignore[misc]


def test_runtime_fence_round_trip_keeps_explicit_prelaunch_null() -> None:
    fence = RuntimeFence(
        agent_id="ua-dice-4f3c2a",
        host_id=HOST_ID,
        host_session_id=HOST_SESSION_ID,
        delivery_id=DELIVERY_ID,
        revision_id=REVISION_ID,
        runtime_instance_id=RUNTIME_INSTANCE_ID,
        process_id=None,
        lifecycle_generation=14,
    )

    payload = fence.to_dict()
    assert payload["process_id"] is None
    assert RuntimeFence.from_dict(payload) == fence


def test_candidate_capability_map_is_immutable_and_round_trips_exact_shape() -> None:
    capability_map = CandidateCapabilityMap()
    payload = capability_map.to_dict()

    assert payload == {
        "capabilities": {
            "personal_agent_host": {
                "macos": {
                    "supported": False,
                    "runtime_contract_versions": [],
                    "source_feature": None,
                }
            }
        }
    }
    assert CandidateCapabilityMap.from_dict(payload) == capability_map
    with pytest.raises(dataclasses.FrozenInstanceError):
        capability_map.capabilities = CandidateCapabilities()  # type: ignore[misc]


def test_supported_macos_capability_is_owned_only_by_feature_059() -> None:
    supported = CandidateCapabilityMap(
        capabilities=CandidateCapabilities(
            personal_agent_host=PersonalAgentHostCapabilities(
                macos=PersonalAgentHostCapability(
                    supported=True,
                    runtime_contract_versions=(2,),
                    source_feature="059",
                )
            )
        )
    )

    assert supported.to_dict()["capabilities"]["personal_agent_host"]["macos"] == {
        "supported": True,
        "runtime_contract_versions": [2],
        "source_feature": "059",
    }
    with pytest.raises(ProtocolValidationError):
        PersonalAgentHostCapability(
            supported=True,
            runtime_contract_versions=(2,),
            source_feature="060",
        ).validate()
    with pytest.raises(ProtocolValidationError):
        CandidateCapabilityMap.from_dict({"capabilities": {}})
