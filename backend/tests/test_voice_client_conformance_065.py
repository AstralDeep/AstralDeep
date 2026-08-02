"""Backend execution of the canonical Feature-065 C0-C6 client fixture.

The canonical schema validator is imported from the isolated contract tool;
this suite does not reimplement its rules.  Selected public protocol classes
and the server-owned composer builder are also exercised so schema-only drift
cannot masquerade as product-parser conformance.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from shared.protocol import (
    ChatCreated,
    Message,
    UserMessageAcknowledged,
    VoiceControlBinding,
    VoicePlayoutEvent,
    VoiceSubmissionRejected,
)
from webrender.chrome.composer_model import (
    VoiceComposerContext,
    build_composer_state,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = (
    REPO_ROOT / "backend/tests/fixtures/voice_065/client_conformance.json"
)
VALIDATOR_PATH = REPO_ROOT / "tooling/contract-ci/validate_voice_contracts.py"
SCHEMA_ENGINE_PATH = Path(
    os.environ.get(
        "ASTRAL_VOICE_SCHEMA_ENGINE_PATH",
        REPO_ROOT / "scripts/validate_release_evidence.py",
    )
)
VOICE_SCHEMA_PATH = Path(
    os.environ.get(
        "ASTRAL_VOICE_SCHEMA_PATH",
        REPO_ROOT
        / "specs/065-conversational-voice/contracts/voice-control.schema.json",
    )
)


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "backend_voice_conformance_validator",
        VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot import validator at {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _load_schema_engine() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "backend_voice_schema_engine",
        SCHEMA_ENGINE_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot import schema engine at {SCHEMA_ENGINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


schema_engine = _load_schema_engine()
fixture = validator.strict_load_json(FIXTURE_PATH)


def _expand_boolean_schemas(value: Any) -> Any:
    """Express Draft boolean schemas in the engine's equivalent object profile."""

    def expand_schema(schema: Any) -> Any:
        if schema is True:
            return {}
        if schema is False:
            return {"not": {}}
        if not isinstance(schema, dict):
            return schema
        result = dict(schema)
        for key in ("$defs", "properties"):
            children = result.get(key)
            if isinstance(children, dict):
                result[key] = {
                    name: expand_schema(child) for name, child in children.items()
                }
        for key in ("items", "contains", "not", "if", "then", "else"):
            if key in result:
                result[key] = expand_schema(result[key])
        for key in ("allOf", "oneOf"):
            if isinstance(result.get(key), list):
                result[key] = [expand_schema(child) for child in result[key]]
        if isinstance(result.get("additionalProperties"), dict):
            result["additionalProperties"] = expand_schema(
                result["additionalProperties"]
            )
        return result

    return expand_schema(value)


voice_schema = _expand_boolean_schemas(validator.strict_load_json(VOICE_SCHEMA_PATH))
indexed = validator.index_fixture_vectors(fixture)


def test_native_clients_disable_vendor_rtc_diagnostics_before_connect() -> None:
    android = (
        REPO_ROOT
        / "android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController.kt"
    ).read_text(encoding="utf-8")
    apple = (
        REPO_ROOT
        / "apple-clients/AstralApp/AstralApp/Voice/VoiceSessionController.swift"
    ).read_text(encoding="utf-8")
    windows = (REPO_ROOT / "windows-client/astral_client/voice.py").read_text(
        encoding="utf-8"
    )

    assert "LiveKit.loggingLevel = LoggingLevel.OFF" in android
    assert "LiveKit.enableWebRTCLogging = false" in android
    assert android.index("LiveKit.loggingLevel") < android.index("LiveKit.create")
    assert "LiveKitSDK.disableLogging()" in apple
    assert apple.index("_ = Self.vendorLoggingDisabled") < apple.index("Room(")
    assert '_LIVEKIT_VENDOR_LOGGERS = ("livekit", "livekit.rtc"' in windows
    assert windows.index("\n_disable_livekit_vendor_logging()\n") < windows.index(
        "from livekit import rtc"
    )


def test_apple_authenticated_transports_are_ephemeral_and_no_store() -> None:
    no_store = (
        REPO_ROOT
        / "apple-clients/AstralCore/Sources/AstralCore/Transport/NoStoreHTTP.swift"
    ).read_text(encoding="utf-8")
    assert "URLSessionConfiguration.ephemeral" in no_store
    for marker in (
        "reloadIgnoringLocalAndRemoteCacheData",
        "configuration.urlCache = nil",
        "configuration.urlCredentialStorage = nil",
        "configuration.httpCookieStorage = nil",
        'request.setValue("no-store", forHTTPHeaderField: "Cache-Control")',
        'request.setValue("no-cache", forHTTPHeaderField: "Pragma")',
    ):
        assert marker in no_store

    paths = (
        "apple-clients/AstralCore/Sources/AstralCore/API/Rest.swift",
        "apple-clients/AstralCore/Sources/AstralCore/Auth/DeviceLogin.swift",
        "apple-clients/AstralCore/Sources/AstralCore/Auth/TokenStore.swift",
        "apple-clients/AstralCore/Sources/AstralCore/Transport/WSClient.swift",
        "apple-clients/AstralApp/AstralApp/AppModel.swift",
        "apple-clients/AstralApp/AstralApp/Voice/VoiceSessionController.swift",
    )
    for relative in paths:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "URLSession.shared" not in source, relative
        assert "NoStoreHTTP" in source, relative


def _materialized(vector_id: str) -> dict[str, Any]:
    return validator.materialize_vector(indexed[vector_id], fixture, indexed)


def _case_vector_parameters() -> list[pytest.ParameterSet]:
    positive = validator.positive_vector_ids(fixture)
    parameters: list[pytest.ParameterSet] = []
    for case in fixture["cases"]:
        for group in ("positive", "negative"):
            for vector in case[group]:
                vector_id = vector["id"]
                parameters.append(
                    pytest.param(
                        vector_id,
                        vector_id in positive,
                        id=vector_id,
                    )
                )
    return parameters


@pytest.mark.parametrize(("vector_id", "accepted"), _case_vector_parameters())
def test_canonical_c0_c6_vectors_run_through_authoritative_validator(
    vector_id: str,
    accepted: bool,
) -> None:
    """Every canonical client vector reaches the real schema/semantic gates."""

    vector = _materialized(vector_id)
    errors: list[str] = []
    try:
        schema_engine.validate_document(vector["payload"], voice_schema)
    except schema_engine.SchemaValidationError as exc:
        errors.append(f"schema: {exc}")
    errors.extend(
        validator.voice_semantic_errors(
            vector["payload"],
            vector.get("context"),
        )
    )

    if accepted:
        assert not errors, f"{vector_id} was rejected: {errors}"
        return
    assert errors, f"{vector_id} was unexpectedly accepted"
    assert any(vector["expected_error"] in error for error in errors), errors


def test_fixture_c0_is_the_actual_server_owned_composer_projection() -> None:
    expected = _materialized("C0-P1-composer")["payload"]

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

    assert actual == expected
    assert [control["action"] for control in actual["voice"]["controls"]] == list(
        validator.EXPECTED_COMPOSER_ACTION_ORDER
    )
    assert fixture["rest_operation_mapping"] == validator.EXPECTED_REST_MAPPING


@pytest.mark.parametrize(
    ("vector_id", "frame_type"),
    [
        ("C1-P1-control-binding", VoiceControlBinding),
        ("C1-P3-chat-created", ChatCreated),
        ("C2-P3-acknowledged", UserMessageAcknowledged),
        ("C2-P4-rejected", VoiceSubmissionRejected),
        ("C3-P3-playout", VoicePlayoutEvent),
    ],
)
def test_public_backend_protocol_parsers_accept_canonical_frames(
    vector_id: str,
    frame_type: type[Any],
) -> None:
    payload = _materialized(vector_id)["payload"]

    parsed = Message.from_json(json.dumps(payload, separators=(",", ":")))

    assert isinstance(parsed, frame_type)
    assert json.loads(parsed.to_json()) == payload


@pytest.mark.parametrize(
    "vector_id",
    [
        "C0-N1-extra-field",
        "C1-N1-correlation-mismatch",
        "C2-N1-final-missing-proof",
        "C2-N2-packet-too-large",
        "C3-N2-playout-text-bearing",
        "C4-N1-en-wrong-policy",
        "C5-N1-background-mic-enabled",
        "C6-N1-result-null-turn",
        "C6-N2-unexpected-worker",
        "C6-N3-wrong-device",
        "C6-N4-stale-grant-revision",
    ],
)
def test_each_failure_class_is_rejected_for_its_canonical_reason(
    vector_id: str,
) -> None:
    """Pin strict shape, packet, policy, identity, and stale-fence failures."""

    vector = _materialized(vector_id)
    errors: list[str] = []
    try:
        schema_engine.validate_document(vector["payload"], voice_schema)
    except schema_engine.SchemaValidationError as exc:
        errors.append(f"schema: {exc}")
    errors.extend(
        validator.voice_semantic_errors(
            vector["payload"],
            vector.get("context"),
        )
    )

    assert errors
    assert any(vector["expected_error"] in error for error in errors), errors
