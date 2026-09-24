"""Tests for backend/shared/protocol.py's RegisterUI connection and resume fields:
legacy registrations without generation/resume still round-trip, and resume locators
require canonical UUIDs and an exact schema.
"""

from __future__ import annotations

import json

import pytest

from shared.protocol import Message, ProtocolValidationError, RegisterUI


CONNECTION_GENERATION = "22222222-2222-4222-8222-222222222222"
ACTIVE_CHAT_ID = "11111111-1111-4111-8111-111111111111"
REQUEST_GENERATION = "33333333-3333-4333-8333-333333333333"


def _resume(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "active_chat_id": ACTIVE_CHAT_ID,
        "request_generation": REQUEST_GENERATION,
    }
    value.update(changes)
    return value


def _registration(*, resume: object = None) -> RegisterUI:
    return RegisterUI(
        capabilities=["render", "stream"],
        session_id="ui-session",
        connection_generation=CONNECTION_GENERATION,
        resume=resume,  # type: ignore[arg-type]
    )


def test_registration_without_resume_round_trips_with_connection_fence() -> None:
    registration = _registration()

    payload = json.loads(registration.to_json())
    parsed = Message.from_json(registration.to_json())

    assert payload["connection_generation"] == CONNECTION_GENERATION
    assert payload["resume"] is None
    assert isinstance(parsed, RegisterUI)
    assert parsed == registration


def test_legacy_registration_may_omit_both_generation_and_resume() -> None:
    registration = RegisterUI(capabilities=["render"], session_id="legacy-session")

    payload = json.loads(registration.to_json())
    parsed = RegisterUI.from_json(registration.to_json())

    assert payload["connection_generation"] is None
    assert payload["resume"] is None
    assert parsed == registration


def test_exact_schema_one_resume_round_trips() -> None:
    registration = _registration(resume=_resume())

    payload = json.loads(registration.to_json())
    parsed = RegisterUI.from_json(registration.to_json())

    assert payload["resume"] == _resume()
    assert parsed.resume == _resume()


@pytest.mark.parametrize(
    "connection_generation",
    [
        False,
        "not-a-uuid",
        "22222222-2222-1222-8222-222222222222",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ],
)
def test_present_connection_generation_must_be_canonical_uuid4(
    connection_generation: object,
) -> None:
    registration = RegisterUI(
        connection_generation=connection_generation,  # type: ignore[arg-type]
    )

    with pytest.raises(ProtocolValidationError, match="connection_generation"):
        registration.validate()


def test_resume_requires_connection_generation() -> None:
    registration = RegisterUI(resume=_resume())

    with pytest.raises(
        ProtocolValidationError,
        match="connection_generation is required when resume is present",
    ):
        registration.validate()
    with pytest.raises(
        ProtocolValidationError,
        match="connection_generation is required when resume is present",
    ):
        Message.from_json(
            json.dumps({"type": "register_ui", "resume": _resume()})
        )


@pytest.mark.parametrize("resume", [False, True, [], "resume", 1])
def test_resume_rejects_non_mapping_values(resume: object) -> None:
    registration = _registration(resume=resume)

    with pytest.raises(ProtocolValidationError, match="resume must be a mapping"):
        registration.validate()


@pytest.mark.parametrize(
    "resume",
    [
        {},
        {
            "schema_version": 1,
            "active_chat_id": ACTIVE_CHAT_ID,
        },
        {
            "schema_version": 1,
            "request_generation": REQUEST_GENERATION,
        },
        {
            "active_chat_id": ACTIVE_CHAT_ID,
            "request_generation": REQUEST_GENERATION,
        },
        {**_resume(), "unknown": "refused"},
    ],
)
def test_resume_rejects_missing_or_unknown_fields(resume: dict[str, object]) -> None:
    registration = _registration(resume=resume)

    with pytest.raises(ProtocolValidationError, match="resume must contain exactly"):
        registration.validate()


@pytest.mark.parametrize("schema_version", [False, True, None, 0, 2, 1.0, "1"])
def test_resume_schema_version_is_exact_integer_one(schema_version: object) -> None:
    registration = _registration(resume=_resume(schema_version=schema_version))

    with pytest.raises(ProtocolValidationError, match="resume.schema_version"):
        registration.validate()


@pytest.mark.parametrize("field_name", ["active_chat_id", "request_generation"])
@pytest.mark.parametrize(
    "invalid_value",
    [
        False,
        None,
        "not-a-uuid",
        "44444444-4444-1444-8444-444444444444",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ],
)
def test_resume_identifiers_require_canonical_uuid4(
    field_name: str,
    invalid_value: object,
) -> None:
    registration = _registration(resume=_resume(**{field_name: invalid_value}))

    with pytest.raises(ProtocolValidationError, match=f"resume.{field_name}"):
        registration.validate()


@pytest.mark.parametrize(
    ("resume", "message"),
    [
        (False, "resume must be a mapping"),
        ({**_resume(), "unknown": True}, "resume must contain exactly"),
        (_resume(schema_version=True), "resume.schema_version"),
        (_resume(active_chat_id="bad"), "resume.active_chat_id"),
        (_resume(request_generation="bad"), "resume.request_generation"),
    ],
)
def test_json_parser_surfaces_stable_protocol_validation_errors(
    resume: object,
    message: str,
) -> None:
    payload = {
        "type": "register_ui",
        "connection_generation": CONNECTION_GENERATION,
        "resume": resume,
    }

    with pytest.raises(ProtocolValidationError, match=message):
        Message.from_json(json.dumps(payload))
