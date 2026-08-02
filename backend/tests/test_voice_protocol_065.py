"""Feature 065 strict shared UI/voice frame contracts (T017/T028).

These tests stay at the shared protocol boundary. Authenticated binding minting,
socket-lifetime rotation, and server admission are exercised with the
orchestrator in T029; this module proves that malformed wire values cannot
reach that authority boundary as loosely shaped dictionaries.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from shared.protocol import (
    ChatCreated,
    CorrelatedNewChat,
    Message,
    ProtocolValidationError,
    RegisterUI,
    UIEvent,
    UserMessageAcknowledged,
    VoiceControlBinding,
    VoiceControlBindingMemory,
    VoiceOrigin,
    VoicePlayoutEvent,
    VoiceSubmissionRejected,
)


DEVICE_ID = "00000000-0000-4000-8000-000000000001"
CONNECTION_GENERATION = "00000000-0000-4000-8000-000000000002"
SESSION_ID = "00000000-0000-4000-8000-000000000003"
CHAT_ID = "00000000-0000-4000-8000-000000000004"
TURN_ID = "00000000-0000-4000-8000-000000000005"
CLIENT_TURN_ID = "00000000-0000-4000-8000-000000000006"
SUBMISSION_ID = "00000000-0000-4000-8000-000000000007"
REQUEST_GENERATION = "00000000-0000-4000-8000-000000000008"
ANNOUNCEMENT_ID = "00000000-0000-4000-8000-000000000009"
BINDING_ID = "00000000-0000-4000-8000-00000000000a"
OTHER_ID = "00000000-0000-4000-8000-00000000000b"
TEXT_DIGEST = "5b6c9147d242ef629cc5731bd8844b86f5ef8bcf4e6d4d741bb80d2bc446ab04"
PROOF = "af81ff8058e12f5622e53f8c1dc3ed460b753c13a3df46f9e09e40ca8f96a7f9"
BINDING = "synthetic-binding-value-000000000000"


def _new_chat() -> dict[str, object]:
    return {
        "type": "ui_event",
        "action": "new_chat",
        "schema_version": "1",
        "connection_generation": CONNECTION_GENERATION,
        "submission_id": SUBMISSION_ID,
        "request_generation": REQUEST_GENERATION,
        "payload": {
            "schema_version": "1",
            "connection_generation": CONNECTION_GENERATION,
            "submission_id": SUBMISSION_ID,
            "request_generation": REQUEST_GENERATION,
        },
    }


def _chat_created() -> dict[str, object]:
    return {
        "type": "chat_created",
        "schema_version": "1",
        "connection_generation": CONNECTION_GENERATION,
        "submission_id": SUBMISSION_ID,
        "request_generation": REQUEST_GENERATION,
        "payload": {
            "schema_version": "1",
            "chat_id": CHAT_ID,
            "from_message": False,
            "connection_generation": CONNECTION_GENERATION,
            "submission_id": SUBMISSION_ID,
            "request_generation": REQUEST_GENERATION,
        },
    }


def _voice_origin() -> dict[str, object]:
    return {
        "schema_version": "1",
        "session_id": SESSION_ID,
        "generation": 1,
        "media_grant_revision": 2,
        "turn_id": TURN_ID,
        "client_turn_id": CLIENT_TURN_ID,
        "chat_context_revision": 3,
        "source_participant_identity": "voice-worker-01",
        "detected_language": "en-US",
        "text_digest_sha256": TEXT_DIGEST,
        "transcript_proof": PROOF,
        "proof_expires_at": "2026-07-31T12:02:00Z",
    }


def _playout() -> dict[str, object]:
    return {
        "type": "voice_playout_event",
        "schema_version": "1",
        "device_id": DEVICE_ID,
        "connection_generation": CONNECTION_GENERATION,
        "session_id": SESSION_ID,
        "generation": 1,
        "media_grant_revision": 2,
        "announcement_id": ANNOUNCEMENT_ID,
        "announcement_sequence": 3,
        "turn_id": TURN_ID,
        "kind": "result",
        "quantum_role": "result_opening",
        "quantum_index": 0,
        "result_reserved_samples_after": 36_000,
        "phase": "started",
        "client_sequence": 12,
        "observed_at": "2026-07-31T12:00:12Z",
    }


def test_register_ui_round_trips_canonical_device_and_fresh_connection() -> None:
    frame = RegisterUI(
        capabilities=["render", "voice_media"],
        session_id="ui-session",
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_GENERATION,
    )

    parsed = Message.from_json(frame.to_json())

    assert isinstance(parsed, RegisterUI)
    assert parsed.device_id == DEVICE_ID
    assert parsed.connection_generation == CONNECTION_GENERATION


def test_register_ui_device_is_additive_for_legacy_clients() -> None:
    frame = RegisterUI(capabilities=["render"], session_id="legacy-session")

    payload = json.loads(frame.to_json())

    assert "device_id" not in payload
    assert Message.from_json(frame.to_json()) == frame


@pytest.mark.parametrize(
    ("device_id", "connection_generation"),
    [
        ("not-a-uuid", CONNECTION_GENERATION),
        ("00000000-0000-1000-8000-000000000001", CONNECTION_GENERATION),
        ("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", CONNECTION_GENERATION),
        (DEVICE_ID, None),
    ],
)
def test_register_ui_rejects_malformed_or_unfenced_device(
    device_id: object, connection_generation: object
) -> None:
    frame = RegisterUI(
        device_id=device_id,  # type: ignore[arg-type]
        connection_generation=connection_generation,  # type: ignore[arg-type]
    )

    with pytest.raises(ProtocolValidationError):
        frame.validate()


def test_correlated_new_chat_and_chat_created_round_trip_strictly() -> None:
    request = Message.from_json(json.dumps(_new_chat()))
    response = Message.from_json(json.dumps(_chat_created()))

    assert isinstance(request, CorrelatedNewChat)
    assert isinstance(request, UIEvent)
    assert request.payload["submission_id"] == request.submission_id
    assert isinstance(response, ChatCreated)
    assert response.payload["chat_id"] == CHAT_ID
    assert json.loads(request.to_json()) == _new_chat()
    assert json.loads(response.to_json()) == _chat_created()


def test_existing_uncorrelated_new_chat_action_remains_an_ordinary_ui_event() -> None:
    wire = '{"type":"ui_event","action":"new_chat","payload":{}}'

    parsed = Message.from_json(wire)

    assert type(parsed) is UIEvent
    assert parsed.action == "new_chat"
    assert parsed.payload == {}


@pytest.mark.parametrize(
    ("factory", "mutation"),
    [
        (_new_chat, lambda value: value["payload"].update({"submission_id": OTHER_ID})),
        (_chat_created, lambda value: value["payload"].update({"request_generation": OTHER_ID})),
        (_new_chat, lambda value: value.update({"extra": "refused"})),
        (_chat_created, lambda value: value["payload"].update({"extra": "refused"})),
        (_new_chat, lambda value: value.update({"schema_version": 1})),
        (_chat_created, lambda value: value["payload"].update({"from_message": True})),
    ],
)
def test_correlated_chat_frames_reject_mismatch_extras_and_wrong_modes(
    factory, mutation
) -> None:
    payload = factory()
    mutation(payload)

    with pytest.raises(ProtocolValidationError):
        Message.from_json(json.dumps(payload))


def test_voice_origin_is_strict_without_changing_ordinary_typed_turn_bytes() -> None:
    ordinary = UIEvent(
        action="chat_message",
        payload={"message": "hello"},
        session_id="chat-legacy",
    )
    expected_legacy_wire = (
        '{"type": "ui_event", "action": "chat_message", '
        '"payload": {"message": "hello"}, "session_id": "chat-legacy", '
        '"submission_id": null, "request_generation": null, '
        '"connection_generation": null, "snapshot_purpose": null, "surface": null}'
    )
    assert ordinary.to_json() == expected_legacy_wire
    assert Message.from_json(expected_legacy_wire) == ordinary
    assert ordinary.voice_origin is None

    voiced = UIEvent(
        action="chat_message",
        session_id=CHAT_ID,
        submission_id=SUBMISSION_ID,
        request_generation=REQUEST_GENERATION,
        connection_generation=CONNECTION_GENERATION,
        payload={
            "message": "Please review Café.",
            "chat_id": CHAT_ID,
            "submission_id": SUBMISSION_ID,
            "request_generation": REQUEST_GENERATION,
            "connection_generation": CONNECTION_GENERATION,
            "snapshot_purpose": "commit",
            "voice_origin": _voice_origin(),
        },
    )

    parsed = Message.from_json(voiced.to_json())

    assert isinstance(parsed, UIEvent)
    assert parsed.voice_origin == VoiceOrigin.from_dict(_voice_origin())
    assert parsed.payload["voice_origin"] == _voice_origin()
    assert parsed.voice_origin.to_dict() == _voice_origin()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"extra": "secret"}),
        lambda value: value.update({"turn_id": "not-a-uuid"}),
        lambda value: value.update({"generation": True}),
        lambda value: value.update({"text_digest_sha256": TEXT_DIGEST.upper()}),
        lambda value: value.update({"transcript_proof": PROOF.upper()}),
        lambda value: value.update({"source_participant_identity": "bad identity"}),
        lambda value: value.update({"detected_language": "EN"}),
        lambda value: value.update({"proof_expires_at": "2026-07-31T08:02:00-04:00"}),
        lambda value: value.update({"proof_expires_at": "2026-02-30T08:02:00Z"}),
        lambda value: value.pop("transcript_proof"),
    ],
)
def test_voice_origin_rejects_missing_extra_or_malformed_values(mutation) -> None:
    origin = _voice_origin()
    mutation(origin)

    with pytest.raises(ProtocolValidationError):
        VoiceOrigin.from_dict(origin)


def test_voice_origin_is_legal_only_on_chat_message() -> None:
    with pytest.raises(ProtocolValidationError, match="chat_message"):
        UIEvent(action="load_chat", payload={"voice_origin": _voice_origin()})

    with pytest.raises(ProtocolValidationError, match="object"):
        VoiceOrigin.from_dict("proof-bearing text is not an object")


def test_correlated_frame_classes_reject_wrong_type_unrelated_fields_and_arrays() -> None:
    request = CorrelatedNewChat.from_dict(_new_chat())
    response = ChatCreated.from_dict(_chat_created())

    with pytest.raises(ProtocolValidationError):
        dataclasses.replace(request, type="voice_action")
    with pytest.raises(ProtocolValidationError, match="unrelated"):
        dataclasses.replace(request, session_id=CHAT_ID)
    with pytest.raises(ProtocolValidationError, match="object"):
        dataclasses.replace(request, payload=[])  # type: ignore[arg-type]
    with pytest.raises(ProtocolValidationError, match="type"):
        dataclasses.replace(response, type="created").validate()
    with pytest.raises(ProtocolValidationError, match="object"):
        dataclasses.replace(response, payload=[]).validate()  # type: ignore[arg-type]


def test_control_binding_is_redacted_and_rotates_only_in_memory() -> None:
    first = VoiceControlBinding(
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_GENERATION,
        binding_id=BINDING_ID,
        binding=BINDING,
        expires_at="2026-07-31T12:10:00Z",
    )
    replacement = dataclasses.replace(
        first,
        connection_generation=OTHER_ID,
        binding_id=OTHER_ID,
        binding="replacement-binding-value-000000000",
        expires_at="2026-07-31T12:11:00Z",
    )
    memory = VoiceControlBindingMemory()
    received = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    credential_expiry = received + timedelta(minutes=15)

    memory.rotate(first, received_at=received, credential_expires_at=credential_expiry)
    assert memory.current(
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_GENERATION,
        at=received,
    ) == first
    memory.rotate(
        replacement,
        received_at=received + timedelta(minutes=1),
        credential_expires_at=credential_expiry,
    )

    assert memory.current(
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_GENERATION,
        at=received + timedelta(minutes=1),
    ) is None
    assert memory.current(
        device_id=DEVICE_ID,
        connection_generation=OTHER_ID,
        at=received + timedelta(minutes=11, seconds=1),
    ) is None
    assert BINDING not in repr(first)
    assert BINDING not in repr(memory)
    assert first.redacted_dict()["binding"] == "[REDACTED]"
    assert Message.from_json(first.to_json()) == first
    memory.clear()
    assert memory.current(
        device_id=DEVICE_ID,
        connection_generation=OTHER_ID,
        at=received + timedelta(minutes=1),
    ) is None


def test_control_binding_memory_rejects_nonbinding_cross_device_and_replay() -> None:
    received = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    first = VoiceControlBinding(
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_GENERATION,
        binding_id=BINDING_ID,
        binding=BINDING,
        expires_at="2026-07-31T12:10:00Z",
    )
    memory = VoiceControlBindingMemory()

    with pytest.raises(ProtocolValidationError, match="voice control binding"):
        memory.rotate(  # type: ignore[arg-type]
            "not-a-binding",
            received_at=received,
            credential_expires_at=received + timedelta(minutes=10),
        )
    memory.rotate(
        first,
        received_at=received,
        credential_expires_at=received + timedelta(minutes=10),
    )
    with pytest.raises(ProtocolValidationError, match="device"):
        memory.rotate(
            dataclasses.replace(
                first,
                device_id=OTHER_ID,
                binding_id=OTHER_ID,
                binding="different-binding-value-000000000000",
            ),
            received_at=received,
            credential_expires_at=received + timedelta(minutes=10),
        )
    with pytest.raises(ProtocolValidationError, match="replace"):
        memory.rotate(
            first,
            received_at=received,
            credential_expires_at=received + timedelta(minutes=10),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"binding": "short"},
        {"binding": "x" * 513},
        {"expires_at": "not-a-time"},
        {"schema_version": 1},
    ],
)
def test_control_binding_rejects_malformed_fields(changes: dict[str, object]) -> None:
    frame = VoiceControlBinding(
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_GENERATION,
        binding_id=BINDING_ID,
        binding=BINDING,
        expires_at="2026-07-31T12:10:00Z",
    )

    with pytest.raises(ProtocolValidationError):
        dataclasses.replace(frame, **changes).validate()

    with pytest.raises(ProtocolValidationError, match="type"):
        dataclasses.replace(frame, type="binding").validate()


def test_control_binding_rejects_expired_overlong_or_post_credential_lifetime() -> None:
    received = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

    for expiry, credential_expiry in (
        ("2026-07-31T12:00:00Z", received + timedelta(minutes=20)),
        ("2026-07-31T12:10:01Z", received + timedelta(minutes=20)),
        ("2026-07-31T12:09:00Z", received + timedelta(minutes=8)),
    ):
        frame = VoiceControlBinding(
            device_id=DEVICE_ID,
            connection_generation=CONNECTION_GENERATION,
            binding_id=BINDING_ID,
            binding=BINDING,
            expires_at=expiry,
        )
        with pytest.raises(ProtocolValidationError):
            frame.validate_lifetime(
                received_at=received,
                credential_expires_at=credential_expiry,
            )

    valid = VoiceControlBinding(
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_GENERATION,
        binding_id=BINDING_ID,
        binding=BINDING,
        expires_at="2026-07-31T12:10:00Z",
    )
    with pytest.raises(ProtocolValidationError, match="timezone-aware"):
        valid.validate_lifetime(
            received_at=received.replace(tzinfo=None),
            credential_expires_at=received + timedelta(minutes=10),
        )


def test_acknowledgement_accepts_voice_or_explicit_typed_null_correlation() -> None:
    voice = UserMessageAcknowledged(
        chat_id=CHAT_ID,
        message_id=41,
        submission_id=SUBMISSION_ID,
        request_generation=REQUEST_GENERATION,
        connection_generation=CONNECTION_GENERATION,
        voice_turn_id=TURN_ID,
    )
    typed = dataclasses.replace(voice, voice_turn_id=None)

    assert Message.from_json(voice.to_json()) == voice
    assert Message.from_json(typed.to_json()) == typed
    with pytest.raises(ProtocolValidationError, match="type"):
        dataclasses.replace(voice, type="message_acked").validate()


def test_fully_correlated_rejection_round_trips_without_secret_or_content() -> None:
    frame = VoiceSubmissionRejected(
        session_id=SESSION_ID,
        connection_generation=CONNECTION_GENERATION,
        generation=1,
        media_grant_revision=2,
        turn_id=TURN_ID,
        client_turn_id=CLIENT_TURN_ID,
        submission_id=SUBMISSION_ID,
        request_generation=REQUEST_GENERATION,
        chat_id=CHAT_ID,
        reason="chat_unavailable",
        retry_policy="explicit_user_retry",
        occurred_at="2026-07-31T12:00:10Z",
    )

    parsed = Message.from_json(frame.to_json())

    assert parsed == frame
    assert set(json.loads(frame.to_json())) == {
        "type",
        "schema_version",
        "session_id",
        "connection_generation",
        "generation",
        "media_grant_revision",
        "turn_id",
        "client_turn_id",
        "submission_id",
        "request_generation",
        "chat_id",
        "reason",
        "retry_policy",
        "occurred_at",
    }
    with_message = dataclasses.replace(frame, message="Choose an available chat.")
    assert json.loads(with_message.to_json())["message"] == "Choose an available chat."
    with pytest.raises(ProtocolValidationError, match="type"):
        dataclasses.replace(frame, type="rejected").validate()
    with pytest.raises(ProtocolValidationError, match="retry_policy"):
        dataclasses.replace(frame, retry_policy="automatic").validate()


@pytest.mark.parametrize(
    ("factory", "changes"),
    [
        (
            lambda: UserMessageAcknowledged(
                chat_id=CHAT_ID,
                message_id=41,
                submission_id=SUBMISSION_ID,
                request_generation=REQUEST_GENERATION,
                connection_generation=CONNECTION_GENERATION,
                voice_turn_id=TURN_ID,
            ),
            {"message_id": True},
        ),
        (
            lambda: UserMessageAcknowledged(
                chat_id=CHAT_ID,
                message_id=41,
                submission_id=SUBMISSION_ID,
                request_generation=REQUEST_GENERATION,
                connection_generation=CONNECTION_GENERATION,
                voice_turn_id=TURN_ID,
            ),
            {"voice_turn_id": "not-a-uuid"},
        ),
        (
            lambda: VoiceSubmissionRejected(
                session_id=SESSION_ID,
                connection_generation=CONNECTION_GENERATION,
                generation=1,
                media_grant_revision=2,
                turn_id=TURN_ID,
                client_turn_id=CLIENT_TURN_ID,
                submission_id=SUBMISSION_ID,
                request_generation=REQUEST_GENERATION,
                chat_id=CHAT_ID,
                reason="chat_unavailable",
                retry_policy="explicit_user_retry",
                occurred_at="2026-07-31T12:00:10Z",
            ),
            {"reason": "upstream_body"},
        ),
        (
            lambda: VoiceSubmissionRejected(
                session_id=SESSION_ID,
                connection_generation=CONNECTION_GENERATION,
                generation=1,
                media_grant_revision=2,
                turn_id=TURN_ID,
                client_turn_id=CLIENT_TURN_ID,
                submission_id=SUBMISSION_ID,
                request_generation=REQUEST_GENERATION,
                chat_id=CHAT_ID,
                reason="chat_unavailable",
                retry_policy="explicit_user_retry",
                occurred_at="2026-07-31T12:00:10Z",
            ),
            {"message": "x" * 241},
        ),
    ],
)
def test_acknowledgement_and_rejection_fail_closed(factory, changes) -> None:
    with pytest.raises(ProtocolValidationError):
        dataclasses.replace(factory(), **changes).validate()


def test_content_free_playout_event_round_trips_under_two_kibibytes() -> None:
    parsed = Message.from_json(json.dumps(_playout()))

    assert isinstance(parsed, VoicePlayoutEvent)
    assert parsed.turn_id == TURN_ID
    encoded = parsed.to_json().encode("utf-8")
    assert len(encoded) <= 2 * 1024
    assert not ({"text", "content", "audio", "binding", "transcript_proof"} & set(json.loads(parsed.to_json())))


def test_greeting_single_and_result_continuation_playout_shapes() -> None:
    greeting = _playout()
    greeting.update(
        {
            "turn_id": None,
            "kind": "greeting",
            "quantum_role": "single",
            "quantum_index": 0,
        }
    )
    greeting.pop("result_reserved_samples_after")
    continuation = _playout()
    continuation.update(
        {
            "quantum_role": "result_continuation",
            "quantum_index": 1,
            "result_reserved_samples_after": 132_000,
        }
    )

    parsed_greeting = Message.from_json(json.dumps(greeting))
    parsed_continuation = Message.from_json(json.dumps(continuation))

    assert isinstance(parsed_greeting, VoicePlayoutEvent)
    assert "result_reserved_samples_after" not in json.loads(parsed_greeting.to_json())
    assert isinstance(parsed_continuation, VoicePlayoutEvent)
    assert parsed_continuation.result_reserved_samples_after == 132_000


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"text": "must not cross this frame"}),
        lambda value: value.update({"device_id": "not-a-uuid"}),
        lambda value: value.update({"generation": True}),
        lambda value: value.update({"turn_id": None}),
        lambda value: value.update({"kind": "progress"}),
        lambda value: value.update({"quantum_index": 1}),
        lambda value: value.pop("result_reserved_samples_after"),
        lambda value: value.update({"result_reserved_samples_after": 36_001}),
        lambda value: value.update({"phase": "rendered"}),
        lambda value: value.update({"kind": "made_up"}),
        lambda value: value.update({"quantum_role": "other"}),
    ],
)
def test_playout_rejects_content_extras_malformed_ids_and_quantum_mismatch(
    mutation,
) -> None:
    payload = _playout()
    mutation(payload)

    with pytest.raises(ProtocolValidationError):
        Message.from_json(json.dumps(payload))


def test_playout_rejects_oversized_raw_utf8_frame() -> None:
    wire = json.dumps(_playout()) + (" " * 2_048)

    with pytest.raises(ProtocolValidationError, match="2048"):
        Message.from_json(wire)

    with pytest.raises(ProtocolValidationError, match="2048"):
        Message.from_json((json.dumps(_playout()) + (" " * 2_048)).encode())


def test_playout_rejects_wrong_type_and_malformed_single_or_continuation() -> None:
    frame = VoicePlayoutEvent.from_dict(_playout())
    with pytest.raises(ProtocolValidationError, match="type"):
        dataclasses.replace(frame, type="playout").validate()
    with pytest.raises(ProtocolValidationError, match="single"):
        dataclasses.replace(
            frame,
            kind="result",
            quantum_role="single",
            quantum_index=0,
            result_reserved_samples_after=None,
        ).validate()
    with pytest.raises(ProtocolValidationError, match="reservation"):
        dataclasses.replace(
            frame,
            kind="progress",
            quantum_role="single",
            quantum_index=0,
            result_reserved_samples_after=1,
        ).validate()
    with pytest.raises(ProtocolValidationError, match="continuation"):
        dataclasses.replace(
            frame,
            quantum_role="result_continuation",
            quantum_index=0,
        ).validate()
    with pytest.raises(ProtocolValidationError, match="greeting"):
        dataclasses.replace(frame, kind="greeting", turn_id=TURN_ID).validate()


def test_strict_voice_frames_reject_unknown_top_level_fields() -> None:
    for payload in (
        _chat_created(),
        _playout(),
        json.loads(
            VoiceControlBinding(
                device_id=DEVICE_ID,
                connection_generation=CONNECTION_GENERATION,
                binding_id=BINDING_ID,
                binding=BINDING,
                expires_at="2026-07-31T12:10:00Z",
            ).to_json()
        ),
    ):
        payload["credential"] = "must-not-be-accepted"
        with pytest.raises(ProtocolValidationError, match="canonical fields"):
            Message.from_json(json.dumps(payload))


def test_voice_frames_reject_duplicate_keys_and_nonfinite_numbers() -> None:
    wire = json.dumps(_playout(), separators=(",", ":"))
    duplicate = wire[:-1] + ',"device_id":"' + DEVICE_ID + '"}'
    nonfinite = wire.replace('"client_sequence":12', '"client_sequence":NaN')

    with pytest.raises(ProtocolValidationError, match="duplicate"):
        Message.from_json(duplicate)
    with pytest.raises(ProtocolValidationError, match="non-finite"):
        Message.from_json(nonfinite)
