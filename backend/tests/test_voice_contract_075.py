"""Executable Feature-075 client-local speech planning contracts."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonschema
import openapi_spec_validator
import pytest
import yaml

Draft202012Validator = jsonschema.Draft202012Validator
FormatChecker = jsonschema.FormatChecker
ValidationError = jsonschema.ValidationError


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = REPO_ROOT / "specs/075-client-local-speech/contracts"
LOCAL_SCHEMA_PATH = CONTRACT_ROOT / "voice-local.schema.json"
OPENAPI_PATH = CONTRACT_ROOT / "voice-rest-v2.openapi.yaml"
REMOTE_FIXTURE_PATH = (
    REPO_ROOT
    / "components/AstralProjection/contracts/fixtures/voice_065/client_conformance.json"
)
VALIDATOR_PATH = REPO_ROOT / "tooling/contract-ci/validate_voice_contracts.py"
REMOTE_V1_FIXTURE_SHA256 = (
    "bc98077594fa8d51dd664fadefaa48cf596a94e7fb2a961a972dbabca4f02143"
)


def _load_contract_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "voice_contract_validator_075", VALIDATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot import contract validator at {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_contract_validator()


def _enforce_locked_validator_environment(repo_root: Path = REPO_ROOT) -> None:
    """Fail unless the required standards tools match the reviewed hash lock."""

    for required_module in (jsonschema, openapi_spec_validator, yaml):
        if required_module is None:
            raise AssertionError("required standards validator import is unavailable")
    validator.validate_dependency_lock(repo_root)
    validator._validate_installed_versions()


_enforce_locked_validator_environment()
local_schema = validator.strict_load_json(LOCAL_SCHEMA_PATH)
openapi = validator.strict_load_yaml(OPENAPI_PATH)
local_frame_validator = Draft202012Validator(
    local_schema,
    format_checker=FormatChecker(),
)

DEVICE_ID = "00000000-0000-4000-8000-000000000001"
CONNECTION_GENERATION = "00000000-0000-4000-8000-000000000002"
SESSION_ID = "00000000-0000-4000-8000-000000000003"
CHAT_ID = "00000000-0000-4000-8000-000000000004"
CLIENT_TURN_ID = "00000000-0000-4000-8000-000000000005"
TURN_ID = "00000000-0000-4000-8000-000000000006"
SUBMISSION_ID = "00000000-0000-4000-8000-000000000007"
REQUEST_GENERATION = "00000000-0000-4000-8000-000000000008"
ANNOUNCEMENT_ID = "00000000-0000-4000-8000-000000000009"
COMMON_BINDING = {
    "schema_version": "2",
    "speech_backend": "client_local",
    "device_id": DEVICE_ID,
    "connection_generation": CONNECTION_GENERATION,
    "session_id": SESSION_ID,
    "generation": 1,
    "speech_revision": 2,
}
TURN_BINDING = {
    "client_turn_id": CLIENT_TURN_ID,
    "turn_id": TURN_ID,
    "submission_id": SUBMISSION_ID,
    "request_generation": REQUEST_GENERATION,
    "chat_id": CHAT_ID,
    "chat_context_revision": 3,
    "recognition_sequence": 1,
}


def _golden_local_frames() -> dict[str, dict[str, Any]]:
    return {
        "voice_local_ready": {
            **COMMON_BINDING,
            "type": "voice_local_ready",
            "contract": "client_local/v1",
            "transport": "client_local",
            "configured_locale": "en-US",
            "full_duplex": False,
            "has_microphone": True,
            "has_audio_output": True,
            "microphone_permission": "authorized",
            "recognition_permission": "authorized",
            "recognition_processing": "guaranteed_local",
            "recognition_locale": "ready",
            "recognition_installation": "ready",
            "synthesis_processing": "guaranteed_local",
            "synthesis_locale": "ready",
            "client_sequence": 1,
        },
        "voice_local_session_ready": {
            **COMMON_BINDING,
            "type": "voice_local_session_ready",
            "contract": "client_local/v1",
            "transport": "client_local",
            "configured_locale": "en-US",
            "chat_id": CHAT_ID,
            "chat_context_revision": 3,
            "applied_chat_context_revision": 3,
            "foreground_active": True,
            "microphone_enabled": True,
            "speech_muted": False,
            "lease_expires_at": "2026-08-28T12:10:00Z",
        },
        "voice_local_recognition_started": {
            **COMMON_BINDING,
            "type": "voice_local_recognition_started",
            "client_turn_id": CLIENT_TURN_ID,
            "chat_id": CHAT_ID,
            "chat_context_revision": 3,
            "recognition_sequence": 1,
        },
        "voice_local_turn_bound": {
            **COMMON_BINDING,
            **TURN_BINDING,
            "type": "voice_local_turn_bound",
            "binding_expires_at": "2026-08-28T12:02:00Z",
        },
        "voice_local_final": {
            **COMMON_BINDING,
            **TURN_BINDING,
            "type": "voice_local_final",
            "final": True,
            "recognized_locale": "en-US",
            "text": "Please continue with my request.",
            "text_digest_sha256": (
                "bf2a146c64ad6c2e07d2ffa471bb5339e88fc61eca42badd273b8a5a2362be4c"
            ),
        },
        "voice_local_recognition_failed": {
            **COMMON_BINDING,
            **TURN_BINDING,
            "type": "voice_local_recognition_failed",
            "reason": "local_recognition_failed",
        },
        "voice_local_final_rejected": {
            **COMMON_BINDING,
            **TURN_BINDING,
            "type": "voice_local_final_rejected",
            "reason": "stale_speech_revision",
            "retry_policy": "none",
            "occurred_at": "2026-08-28T12:00:01Z",
        },
        "voice_local_announcement": {
            **COMMON_BINDING,
            "type": "voice_local_announcement",
            "announcement_id": ANNOUNCEMENT_ID,
            "announcement_sequence": 1,
            "turn_id": None,
            "kind": "greeting",
            "output_policy": "lifecycle",
            "locale": "en-US",
            "text": "I’m listening.",
            "text_digest_sha256": (
                "9059f16b0325e80e16907604e6b067ee663909754e7c785e298d42ae693749b9"
            ),
            "expires_at": "2026-08-28T12:00:10Z",
            "foreground_required": True,
            "mute_revision": 1,
            "consent_revision": 1,
        },
        "voice_local_playout_event": {
            **COMMON_BINDING,
            "type": "voice_local_playout_event",
            "announcement_id": ANNOUNCEMENT_ID,
            "announcement_sequence": 1,
            "turn_id": None,
            "kind": "greeting",
            "phase": "finished",
            "client_sequence": 2,
            "observed_at": "2026-08-28T12:00:02Z",
        },
    }


def _openapi_validator(schema_name: str) -> Draft202012Validator:
    schemas = openapi["components"]["schemas"]
    wrapper = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/components/schemas/{schema_name}",
        "components": {"schemas": schemas},
    }
    return Draft202012Validator(wrapper, format_checker=FormatChecker())


def _requirements(*, local: bool) -> dict[str, Any]:
    return {
        "session_contract": (
            "voice-rest/v2-client-local" if local else "voice-rest/v1"
        ),
        "local_frame_contract": "client_local/v1" if local else None,
        "configured_locale": "en-US",
        "recognition_must_be_local": local,
        "synthesis_must_be_local": local,
        "installation_policy": "explicit_user_action_only",
        "requirement_revision": 1,
        "max_final_unicode_scalars": 8000,
        "max_announcement_utf8_bytes": 600,
        "announcement_ttl_seconds": 10,
        "echo_suppression_milliseconds": 500,
    }


def _client_local_capability() -> dict[str, Any]:
    return {
        "contract": "client_local/v1",
        "transport": "client_local",
        "configured_locale": "en-US",
        "full_duplex": False,
        "has_microphone": True,
        "has_audio_output": True,
        "microphone_permission": "authorized",
        "recognition_permission": "authorized",
        "recognition_processing": "guaranteed_local",
        "recognition_locale": "ready",
        "recognition_installation": "ready",
        "synthesis_processing": "guaranteed_local",
        "synthesis_locale": "ready",
    }


def _rest_v2_golden_vectors() -> dict[str, dict[str, Any]]:
    return {
        "VoiceCapabilityV2": {
            "schema_version": "2",
            "speech_backend": "client_local",
            "status": "requires_client_readiness",
            "reason": "client_readiness_required",
            "checked_at": "2026-08-28T12:00:00Z",
            "expires_at": "2026-08-28T12:00:10Z",
            "supported_transports": ["client_local"],
            "requirements": _requirements(local=True),
        },
        "ClientLocalCapability": _client_local_capability(),
        "VoiceStatusV2": {
            "schema_version": "2",
            "speech_backend": "client_local",
            "state": "ready",
            "reason": "ready",
            "checked_at": "2026-08-28T12:00:00Z",
            "phase_timings_ms": {"configuration": 12},
        },
        "CreateClientLocalSessionRequest": {
            "schema_version": "2",
            "activation_id": REQUEST_GENERATION,
            "device_id": DEVICE_ID,
            "device_kind": "web",
            "visible_chat_id": CHAT_ID,
            "foreground_active": True,
            "client_capability": _client_local_capability(),
        },
        "TakeoverClientLocalSessionRequest": {
            "schema_version": "2",
            "activation_id": REQUEST_GENERATION,
            "device_id": DEVICE_ID,
            "device_kind": "windows",
            "expected_generation": 1,
            "expected_speech_revision": 2,
            "visible_chat_id": CHAT_ID,
            "foreground_active": True,
            "client_capability": _client_local_capability(),
        },
        "ClientLocalSession": {
            "schema_version": "2",
            "session_id": SESSION_ID,
            "speech_backend": "client_local",
            "transport": "client_local",
            "generation": 1,
            "speech_revision": 2,
            "state": "active",
            "visible_chat_id": CHAT_ID,
            "chat_context_revision": 3,
            "applied_chat_context_revision": 3,
            "chat_context_synced": True,
            "foreground_active": True,
            "microphone_enabled": True,
            "speech_muted": False,
            "configured_locale": "en-US",
            "idle_expires_at": "2026-08-28T12:10:00Z",
        },
        "VoiceError": {
            "error": "voice_unavailable",
            "reason": "local_recognition_unavailable",
            "retryable": True,
            "retry_after_seconds": 5,
            "current_session_id": None,
        },
    }


def test_voice_local_schema_is_valid_draft_2020_12() -> None:
    assert local_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    validator.validate_json_schema_document(local_schema, LOCAL_SCHEMA_PATH.name)


def test_voice_rest_v2_is_valid_openapi_31_with_exact_local_operations() -> None:
    validator.validate_openapi_document(openapi)
    assert openapi["openapi"] == "3.1.0"
    assert {
        (method, path, operation["operationId"])
        for path, path_item in openapi["paths"].items()
        for method, operation in path_item.items()
    } == {
        ("get", "/api/voice/v2/capability", "getVoiceCapabilityV2"),
        ("get", "/api/voice/v2/status", "getVoiceStatusV2"),
        ("post", "/api/voice/v2/sessions", "createClientLocalVoiceSession"),
        (
            "post",
            "/api/voice/v2/sessions/{session_id}/takeover",
            "takeOverClientLocalVoiceSession",
        ),
    }


def test_every_client_local_frame_golden_vector_is_strict() -> None:
    frames = _golden_local_frames()
    assert set(frames) == {
        "voice_local_ready",
        "voice_local_session_ready",
        "voice_local_recognition_started",
        "voice_local_turn_bound",
        "voice_local_final",
        "voice_local_recognition_failed",
        "voice_local_final_rejected",
        "voice_local_announcement",
        "voice_local_playout_event",
    }
    for name, frame in frames.items():
        local_frame_validator.validate(frame)
        with pytest.raises(ValidationError):
            local_frame_validator.validate({**frame, "unexpected": name})


def test_voice_rest_v2_golden_vectors_reject_extra_keys() -> None:
    vectors = _rest_v2_golden_vectors()
    assert set(vectors) == {
        "VoiceCapabilityV2",
        "ClientLocalCapability",
        "VoiceStatusV2",
        "CreateClientLocalSessionRequest",
        "TakeoverClientLocalSessionRequest",
        "ClientLocalSession",
        "VoiceError",
    }
    for schema_name, payload in vectors.items():
        schema_validator = _openapi_validator(schema_name)
        schema_validator.validate(payload)
        with pytest.raises(ValidationError, match="Additional properties"):
            schema_validator.validate({**payload, "unexpected": schema_name})


def test_local_v2_and_remote_v1_contracts_remain_disjoint() -> None:
    assert hashlib.sha256(REMOTE_FIXTURE_PATH.read_bytes()).hexdigest() == (
        REMOTE_V1_FIXTURE_SHA256
    )
    remote_capability_v2 = {
        "schema_version": "2",
        "speech_backend": "llm_factory",
        "status": "ready",
        "reason": "ready",
        "checked_at": "2026-08-28T12:00:00Z",
        "expires_at": "2026-08-28T12:00:10Z",
        "supported_transports": ["livekit", "watch_pcm_websocket"],
        "requirements": _requirements(local=False),
    }
    _openapi_validator("VoiceCapabilityV2").validate(remote_capability_v2)

    remote_v1_playout = copy.deepcopy(_golden_local_frames()["voice_local_playout_event"])
    remote_v1_playout.update(
        {
            "type": "voice_playout_event",
            "schema_version": "1",
            "media_grant_revision": remote_v1_playout.pop("speech_revision"),
        }
    )
    remote_v1_playout.pop("speech_backend")
    with pytest.raises(ValidationError):
        local_frame_validator.validate(remote_v1_playout)


def test_every_external_rest_v2_schema_has_strict_goldens() -> None:
    required_field = {
        "VoiceCapabilityV2": "requirements",
        "ClientLocalCapability": "recognition_processing",
        "VoiceStatusV2": "state",
        "CreateClientLocalSessionRequest": "activation_id",
        "TakeoverClientLocalSessionRequest": "expected_generation",
        "ClientLocalSession": "session_id",
        "VoiceError": "error",
    }
    discriminator_mutation = {
        "VoiceCapabilityV2": ("speech_backend", "device_selected"),
        "ClientLocalCapability": ("transport", "livekit"),
        "VoiceStatusV2": ("schema_version", "1"),
        "CreateClientLocalSessionRequest": ("schema_version", "1"),
        "TakeoverClientLocalSessionRequest": ("foreground_active", False),
        "ClientLocalSession": ("speech_backend", "llm_factory"),
        "VoiceError": ("reason", "unknown_reason"),
    }

    for schema_name, payload in _rest_v2_golden_vectors().items():
        schema_validator = _openapi_validator(schema_name)
        missing = copy.deepcopy(payload)
        missing.pop(required_field[schema_name])
        with pytest.raises(ValidationError):
            schema_validator.validate(missing)

        field, invalid_value = discriminator_mutation[schema_name]
        mutated = copy.deepcopy(payload)
        mutated[field] = invalid_value
        with pytest.raises(ValidationError):
            schema_validator.validate(mutated)

    for schema_name in (
        "CreateClientLocalSessionRequest",
        "TakeoverClientLocalSessionRequest",
    ):
        nested_extra = copy.deepcopy(_rest_v2_golden_vectors()[schema_name])
        nested_extra["client_capability"]["unexpected"] = True
        with pytest.raises(ValidationError, match="Additional properties"):
            _openapi_validator(schema_name).validate(nested_extra)


def test_feature_075_standards_require_locked_validator_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enforce_locked_validator_environment()

    original_version = validator.importlib.metadata.version
    for package in ("jsonschema", "openapi-spec-validator", "pyyaml"):
        with monkeypatch.context() as context:
            context.setattr(
                validator.importlib.metadata,
                "version",
                lambda candidate, package=package: (
                    "0.0.0" if candidate == package else original_version(candidate)
                ),
            )
            with pytest.raises(validator.ContractValidationError, match="expected"):
                _enforce_locked_validator_environment()

    tool_root = tmp_path / "tooling/contract-ci"
    tool_root.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "tooling/contract-ci/requirements.in", tool_root)
    lock = (REPO_ROOT / "tooling/contract-ci/requirements.lock.txt").read_text(
        encoding="utf-8"
    )
    (tool_root / "requirements.lock.txt").write_text(
        lock.replace("pyyaml==6.0.3", "pyyaml==6.0.2"),
        encoding="utf-8",
    )
    with pytest.raises(validator.ContractValidationError, match="missing exact pyyaml pin"):
        _enforce_locked_validator_environment(tmp_path)
