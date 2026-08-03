"""Feature 060 release-contract schema guardrails (T002).

The production validator is intentionally deferred to T106.  These tests use a
small test-only Draft 2020-12 oracle for the exact assertion vocabulary used by
the three tracked schemas.  That keeps the contract executable without adding
``jsonschema`` (or any other product/test dependency) and prevents the schema
documents from silently growing beyond the future standard-library validator.
"""
from __future__ import annotations

import copy
import json
import math
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = (
    REPO_ROOT / "specs" / "060-runtime-reliability-hardening" / "contracts"
)
SCHEMA_PATHS = (
    CONTRACT_ROOT / "windows-deployment-profile.schema.json",
    CONTRACT_ROOT / "release-evidence.schema.json",
    CONTRACT_ROOT / "release-trust.schema.json",
)
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

if not (REPO_ROOT / "specs").is_dir():  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )

GIT_SHA = "a" * 40
OTHER_GIT_SHA = "b" * 40
SHA256 = "c" * 64
OTHER_SHA256 = "d" * 64
THIRD_SHA256 = "e" * 64
NOW = "2026-07-15T16:00:00Z"
SPEECH_PROFILE_SHA256 = (
    "9b857a3d788a5d6c4ff67278eca4a169028fb2e185ccaf0b01961283f629445b"
)
PINNED_LIVEKIT_IMAGE_SHA256 = (
    "3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1"
)

ANNOTATION_KEYWORDS = {
    "$schema",
    "$id",
    "title",
    "description",
    "$comment",
}
ASSERTION_KEYWORDS = {
    "$defs",
    "$ref",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "pattern",
    "format",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "uniqueItems",
    "items",
    "contains",
    "allOf",
    "oneOf",
    "not",
    "if",
    "then",
    "else",
}
SUPPORTED_KEYWORDS = ANNOTATION_KEYWORDS | ASSERTION_KEYWORDS
SUPPORTED_TYPES = {
    "null",
    "boolean",
    "object",
    "array",
    "number",
    "integer",
    "string",
}


class DuplicateKeyError(ValueError):
    """Raised when JSON text repeats an object key."""


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_load(path: Path) -> dict[str, Any]:
    loaded = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_nonfinite,
    )
    assert isinstance(loaded, dict), f"{path} must contain one JSON object"
    return loaded


SCHEMAS = {path.name: _strict_load(path) for path in SCHEMA_PATHS}
PROFILE_SCHEMA = SCHEMAS["windows-deployment-profile.schema.json"]
EVIDENCE_SCHEMA = SCHEMAS["release-evidence.schema.json"]
TRUST_SCHEMA = SCHEMAS["release-trust.schema.json"]


def _schema_children(schema: dict[str, Any]) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for key in ("$defs", "properties"):
        mapping = schema.get(key, {})
        assert isinstance(mapping, dict), f"{key} must be an object"
        children.extend(mapping.values())
    for key in ("items", "contains", "not", "if", "then", "else"):
        child = schema.get(key)
        if child is not None:
            children.append(child)
    for key in ("allOf", "oneOf"):
        children.extend(schema.get(key, []))
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        children.append(additional)
    return children


def _walk_schema(
    schema: dict[str, Any], *, location: str = "$"
) -> list[tuple[str, dict[str, Any]]]:
    assert isinstance(schema, dict), f"{location} must be a schema object"
    unknown = set(schema) - SUPPORTED_KEYWORDS
    assert not unknown, f"unsupported schema keywords at {location}: {sorted(unknown)}"

    schema_type = schema.get("type")
    if schema_type is not None:
        declared = {schema_type} if isinstance(schema_type, str) else set(schema_type)
        assert declared and declared <= SUPPORTED_TYPES, (
            f"invalid type declaration at {location}: {schema_type!r}"
        )
    if "$ref" in schema:
        reference = schema["$ref"]
        assert isinstance(reference, str)
        assert reference.startswith("#/$defs/"), (
            f"only local $defs references are permitted at {location}: {reference}"
        )
    if "required" in schema:
        required = schema["required"]
        assert isinstance(required, list) and all(
            isinstance(item, str) for item in required
        )
        assert len(required) == len(set(required)), (
            f"duplicate required property at {location}"
        )
    if "pattern" in schema:
        re.compile(schema["pattern"])
    if "format" in schema:
        assert schema["format"] in {"uuid", "date-time", "uri"}

    walked = [(location, schema)]
    for index, child in enumerate(_schema_children(schema)):
        walked.extend(_walk_schema(child, location=f"{location}/{index}"))
    return walked


def _json_equal(left: Any, right: Any) -> bool:
    return json.dumps(
        left, sort_keys=True, separators=(",", ":"), allow_nan=False
    ) == json.dumps(right, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _type_matches(instance: Any, expected: str) -> bool:
    if expected == "null":
        return instance is None
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return (
            isinstance(instance, (int, float))
            and not isinstance(instance, bool)
            and math.isfinite(instance)
        )
    if expected == "string":
        return isinstance(instance, str)
    raise AssertionError(f"unsupported type in test oracle: {expected}")


def _resolve_local_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    value: Any = root
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[token]
    assert isinstance(value, dict)
    return value


def _format_matches(value: str, format_name: str) -> bool:
    try:
        if format_name == "uuid":
            return str(uuid.UUID(value)) == value.lower()
        if format_name == "date-time":
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return "T" in value and parsed.tzinfo is not None
        if format_name == "uri":
            parsed = urlsplit(value)
            return bool(parsed.scheme and (parsed.netloc or parsed.path))
    except (TypeError, ValueError):
        return False
    raise AssertionError(f"unsupported format in test oracle: {format_name}")


def _validate(
    instance: Any,
    schema: dict[str, Any],
    *,
    root: dict[str, Any] | None = None,
    location: str = "$",
) -> list[str]:
    """Return deterministic validation errors for the schemas' exact subset."""
    root = schema if root is None else root
    errors: list[str] = []

    if "$ref" in schema:
        errors.extend(
            _validate(
                instance,
                _resolve_local_ref(root, schema["$ref"]),
                root=root,
                location=location,
            )
        )

    expected_type = schema.get("type")
    if expected_type is not None:
        options = [expected_type] if isinstance(expected_type, str) else expected_type
        if not any(_type_matches(instance, option) for option in options):
            return [f"{location}: expected type {options!r}"]

    if "const" in schema and not _json_equal(instance, schema["const"]):
        errors.append(f"{location}: value does not equal const")
    if "enum" in schema and not any(
        _json_equal(instance, option) for option in schema["enum"]
    ):
        errors.append(f"{location}: value is not in enum")

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in instance:
                errors.append(f"{location}: missing required property {required}")
        for key, value in instance.items():
            if key in properties:
                errors.extend(
                    _validate(
                        value,
                        properties[key],
                        root=root,
                        location=f"{location}.{key}",
                    )
                )
            elif schema.get("additionalProperties") is False:
                errors.append(f"{location}: unexpected property {key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(
                    _validate(
                        value,
                        schema["additionalProperties"],
                        root=root,
                        location=f"{location}.{key}",
                    )
                )

    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            errors.append(f"{location}: string is shorter than minLength")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{location}: string is longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append(f"{location}: string does not match pattern")
        if "format" in schema and not _format_matches(instance, schema["format"]):
            errors.append(f"{location}: string does not match format")

    if (
        isinstance(instance, (int, float))
        and not isinstance(instance, bool)
        and math.isfinite(instance)
    ):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{location}: number is below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{location}: number is above maximum")

    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{location}: array has fewer than minItems")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{location}: array has more than maxItems")
        if schema.get("uniqueItems"):
            encoded = [
                json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
                for item in instance
            ]
            if len(encoded) != len(set(encoded)):
                errors.append(f"{location}: array items are not unique")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(
                    _validate(
                        item,
                        schema["items"],
                        root=root,
                        location=f"{location}[{index}]",
                    )
                )
        if "contains" in schema and not any(
            not _validate(item, schema["contains"], root=root)
            for item in instance
        ):
            errors.append(f"{location}: array has no item matching contains")

    for child in schema.get("allOf", []):
        errors.extend(_validate(instance, child, root=root, location=location))
    if "oneOf" in schema:
        matches = sum(
            not _validate(instance, child, root=root, location=location)
            for child in schema["oneOf"]
        )
        if matches != 1:
            errors.append(f"{location}: oneOf matched {matches} branches")
    if "not" in schema and not _validate(
        instance, schema["not"], root=root, location=location
    ):
        errors.append(f"{location}: value matched forbidden schema")
    if "if" in schema:
        branch = "then" if not _validate(instance, schema["if"], root=root) else "else"
        if branch in schema:
            errors.extend(
                _validate(instance, schema[branch], root=root, location=location)
            )
    return errors


def _assert_valid(
    instance: Any,
    schema: dict[str, Any],
    *,
    root: dict[str, Any] | None = None,
) -> None:
    assert not (errors := _validate(instance, schema, root=root)), "\n".join(errors)


def _assert_invalid(
    instance: Any,
    schema: dict[str, Any],
    *,
    root: dict[str, Any] | None = None,
) -> None:
    assert _validate(
        instance, schema, root=root
    ), "instance unexpectedly satisfied schema"


def _profile() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile_id": "astraldeep-production",
        "release_id": "windows-0.4.0",
        "client_version": "0.4.0",
        "distribution": "production",
        "local_only": False,
        "authority": "https://identity.astraldeep.invalid/realms/Astral",
        "websocket_endpoint": "wss://service.astraldeep.invalid/ws",
        "client_id": "astral-desktop",
        "auth_mode": "keycloak_oidc_pkce",
        "override_policy": {
            "managed_profile_allowed": True,
            "command_line_profile_allowed": True,
            "persisted_profile_allowed": True,
            "configure_dialog_allowed": False,
            "development_defaults_allowed": False,
        },
        "agent_connection": {
            "byo_host": {"disposition": "authenticated_ui_tunnel"},
            "legacy_tools": {
                "disposition": "disabled",
                "credential_source": "none",
            },
        },
    }


def _staging_environment() -> dict[str, Any]:
    return {
        "environment_id": "stage-060-request-1",
        "topology": "shared_reachable_ephemeral",
        "deployment_run_id": "12345",
        "deployed_at": NOW,
        "candidate_image_reference": (
            f"ghcr.io/astraldeep/astraldeep@sha256:{SHA256}"
        ),
        "candidate_image_sha256": SHA256,
        "representative_dataset_sha256": OTHER_SHA256,
        "fixture_manifest_sha256": THIRD_SHA256,
        "keycloak_realm_sha256": "f" * 64,
        "source_schema_revision": "057.001",
        "migrated_schema_revision": "060.004",
        "authentication_posture": "real_keycloak_oidc",
        "database_posture": "representative_postgresql",
        "worker_paths": ["background", "scheduler", "voice"],
        "voice_runtime": {
            "voice_worker_image_reference": (
                f"ghcr.io/astraldeep/voice-worker@sha256:{OTHER_SHA256}"
            ),
            "voice_worker_image_sha256": OTHER_SHA256,
            "livekit_image_reference": (
                "livekit/livekit-server:v1.13.5@sha256:"
                f"{PINNED_LIVEKIT_IMAGE_SHA256}"
            ),
            "livekit_image_sha256": PINNED_LIVEKIT_IMAGE_SHA256,
            "livekit_config_sha256": "f" * 64,
            "livekit_public_url": "wss://voice.stage-060.astraldeep.invalid",
            "livekit_turn_domain": "turn.stage-060.astraldeep.invalid",
            "speech_profile": {
                "asr_model": "Systran/faster-whisper-large-v3",
                "tts_model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
                "voice": "af_heart",
                "sample_rate_hz": 24000,
                "inventory_sha256": "2" * 64,
                "profile_sha256": SPEECH_PROFILE_SHA256,
            },
        },
        "macos_personal_agent_host": {
            "supported": False,
            "runtime_contract_versions": [],
            "source_feature": None,
            "source": "candidate_capability_map",
            "manifest_sha256": "1" * 64,
        },
        "endpoint": "https://stage-060.astraldeep.invalid",
    }


def _artifact(kind: str) -> dict[str, Any]:
    reference = (
        f"oci://ghcr.io/astraldeep/astraldeep@sha256:{SHA256}"
        if kind == "container"
        else "bundle://artifacts/client.bin"
    )
    return {
        "name": "candidate.bin",
        "kind": kind,
        "immutable_reference": reference,
        "sha256": SHA256,
        "build_identity": "build-060-1",
    }


def _runner(os_name: str) -> dict[str, Any]:
    return {
        "os": os_name,
        "architecture": "x86_64",
        "runner_image": f"{os_name}-image-pinned",
        "runner_name": f"{os_name}-runner-1",
        "runner_environment": "github_hosted",
    }


def _workflow(job_id: str = "backend") -> dict[str, Any]:
    return {
        "name": "release-readiness",
        "run_id": "12345",
        "run_attempt": 1,
        "job_id": job_id,
    }


def _check(
    check_id: str, *, outcome: str = "passed", quantitative: bool = False
) -> dict[str, Any]:
    measurement = {
        "metric": "trial_success_rate",
        "aggregation": "rate",
        "value": 100,
        "unit": "percent",
        "sample_count": 20,
        "comparator": "gte",
        "threshold": 95,
    }
    evidence_artifact = {
        "name": "metrics.json",
        "kind": "json_metrics",
        "immutable_reference": "bundle://raw/metrics.json",
        "sha256": OTHER_SHA256,
    }
    return {
        "id": check_id,
        "outcome": outcome,
        "duration_ms": 25,
        "detail_code": None,
        "applicability_reason": (
            "unsupported by canonical capability map"
            if outcome == "not_applicable"
            else None
        ),
        "measurements": [measurement] if quantitative else [],
        "evidence_artifacts": [evidence_artifact] if quantitative else [],
    }


ARTIFACT_KIND = {
    "backend": "container",
    "web": "web_deployment",
    "windows": "windows_exe",
    "android": "android_apk",
    "macos": "macos_app",
    "ios": "ios_app",
    "watchos": "watchos_app",
    "docs": "source_tree",
}
RUNNER_OS = {
    "backend": "linux",
    "web": "linux",
    "windows": "windows",
    "android": "linux",
    "macos": "macos",
    "ios": "macos",
    "watchos": "macos",
    "docs": "linux",
}


def _platform_evidence(platform: str) -> dict[str, Any]:
    if platform == "backend":
        check_ids = [
            "candidate_staging",
            "runtime_admission_stress",
            "scheduler_exactly_once",
            "migration_multi_instance",
            "process_supervision_stress",
        ]
    elif platform == "docs":
        check_ids = ["documentation_links", "apply_config_reload"]
    else:
        check_ids = [
            "sign_in",
            "rendered_chat",
            "reconnect_resume",
            "agent_lifecycle",
            "accessibility_semantics",
            "personal_agent",
        ]
        if platform == "windows":
            check_ids.extend(
                [
                    "windows_deployment_validation",
                    "windows_clean_profile_no_dialog",
                    "windows_frozen_worker",
                    "windows_upgrade_from_0_3_0",
                    "dependency_lock_reproducibility",
                ]
            )
        elif platform == "android":
            check_ids.append("android_next_toolchain_readiness")
        elif platform in {"macos", "ios"}:
            check_ids.append("apple_first_login_llm")
        if platform == "macos":
            check_ids.append("macos_personal_agent_host")

    checks = []
    for index, check_id in enumerate(check_ids):
        outcome = (
            "not_applicable"
            if (platform == "watchos" and check_id == "personal_agent")
            or (platform == "macos" and check_id == "macos_personal_agent_host")
            else "passed"
        )
        checks.append(_check(check_id, outcome=outcome, quantitative=index == 0))

    return {
        "document_type": "platform_evidence",
        "schema_version": 1,
        "evidence_id": "11111111-1111-4111-8111-111111111111",
        "candidate_sha": GIT_SHA,
        "release_id": "release-060-1",
        "release_version": "0.4.0",
        "platform": platform,
        "target_description": f"060 {platform} release target",
        "artifact": _artifact(ARTIFACT_KIND[platform]),
        "staging_environment": None if platform == "docs" else _staging_environment(),
        "runner": _runner(RUNNER_OS[platform]),
        "workflow": _workflow(platform),
        "started_at": NOW,
        "completed_at": "2026-07-15T16:01:00Z",
        "outcome": "passed",
        "unavailable_reason": None,
        "unavailability_observation": None,
        "checks": checks,
    }


def _trusted_member() -> dict[str, Any]:
    return {
        "kind": "github_actions_artifact_member",
        "repository": "AstralDeep/AstralDeep",
        "run_id": "12345",
        "run_attempt": 1,
        "artifact_id": "67890",
        "artifact_name": "release-evidence",
        "member": "reports/backend.json",
        "immutable_reference": (
            "gh://AstralDeep/AstralDeep/runs/12345/attempts/1/"
            "artifacts/67890/members/reports/backend.json"
        ),
        "sha256": SHA256,
    }


def _trusted_workflow_provenance() -> dict[str, Any]:
    return {
        "document_type": "trusted_workflow_provenance",
        "schema_version": 1,
        "manifest_id": "22222222-2222-4222-8222-222222222222",
        "repository": "AstralDeep/AstralDeep",
        "candidate_sha": GIT_SHA,
        "workflow": _workflow("backend"),
        "workflow_ref": (
            "AstralDeep/AstralDeep/.github/workflows/release-readiness.yml@"
            f"{OTHER_GIT_SHA}"
        ),
        "runner": _runner("linux"),
        "trusted_builder": {
            "repository": "AstralDeep/AstralDeep",
            "workflow_path": ".github/workflows/release-trusted-builder.yml",
            "signer_digest": OTHER_GIT_SHA,
            "certificate_identity": "https://github.com/AstralDeep/AstralDeep",
        },
        "generated_at": NOW,
        "artifacts": [_trusted_member()],
    }


def test_tracked_schemas_are_strict_draft_2020_12_documents() -> None:
    assert {path.name for path in SCHEMA_PATHS} == set(SCHEMAS)
    for path in SCHEMA_PATHS:
        schema = SCHEMAS[path.name]
        assert schema["$schema"] == DRAFT_2020_12
        assert schema["$id"].endswith(f"/{path.name}")
        assert schema.get("title")
        assert _walk_schema(schema)


def test_schema_vocabulary_is_exactly_the_documented_stdlib_subset() -> None:
    used = {
        keyword
        for schema in SCHEMAS.values()
        for _, node in _walk_schema(schema)
        for keyword in node
    }
    assert used == SUPPORTED_KEYWORDS


@pytest.mark.parametrize(
    ("schema", "valid", "invalid"),
    [
        ({"type": "string"}, "ok", 1),
        ({"const": "ok"}, "ok", "no"),
        ({"enum": ["ok", "yes"]}, "ok", "no"),
        ({"required": ["x"]}, {"x": 1}, {}),
        ({"properties": {"x": {"type": "integer"}}}, {"x": 1}, {"x": "1"}),
        ({"additionalProperties": False}, {}, {"x": 1}),
        ({"pattern": "^ok$"}, "ok", "no"),
        ({"minLength": 2, "maxLength": 2}, "ok", "x"),
        ({"minimum": 1, "maximum": 2}, 2, 3),
        ({"minItems": 1, "maxItems": 1}, [1], []),
        ({"uniqueItems": True}, [1, 2], [1, 1]),
        ({"items": {"type": "integer"}}, [1], ["1"]),
        ({"contains": {"const": "ok"}}, ["ok"], ["no"]),
        ({"allOf": [{"type": "string"}, {"minLength": 2}]}, "ok", "x"),
        ({"oneOf": [{"const": "ok"}, {"const": "yes"}]}, "ok", "no"),
        ({"not": {"const": "no"}}, "ok", "no"),
        (
            {
                "if": {"properties": {"kind": {"const": "a"}}},
                "then": {"required": ["a"]},
                "else": {"required": ["b"]},
            },
            {"kind": "a", "a": 1},
            {"kind": "a", "b": 1},
        ),
        (
            {"$defs": {"value": {"const": "ok"}}, "$ref": "#/$defs/value"},
            "ok",
            "no",
        ),
    ],
)
def test_test_oracle_exercises_every_assertion_form(
    schema: dict[str, Any], valid: Any, invalid: Any
) -> None:
    _assert_valid(valid, schema)
    _assert_invalid(invalid, schema)


@pytest.mark.parametrize(
    ("format_name", "valid", "invalid"),
    [
        ("uuid", "11111111-1111-4111-8111-111111111111", "not-a-uuid"),
        ("date-time", NOW, "2026-07-15"),
        ("uri", "https://stage.astraldeep.invalid", "not a uri"),
    ],
)
def test_active_formats_have_valid_and_invalid_vectors(
    format_name: str, valid: str, invalid: str
) -> None:
    schema = {"type": "string", "format": format_name}
    _assert_valid(valid, schema)
    _assert_invalid(invalid, schema)


def test_production_deployment_profile_is_complete_and_credential_free() -> None:
    profile = _profile()
    _assert_valid(profile, PROFILE_SCHEMA)
    serialized = json.dumps(profile, sort_keys=True)
    assert "client_secret" not in serialized.lower()
    assert "api_key" not in serialized.lower()
    assert "password" not in serialized.lower()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authority", "http://identity.astraldeep.invalid/realms/Astral"),
        ("authority", "https://localhost:8443/realms/Astral"),
        ("authority", "https://user@identity.astraldeep.invalid/realms/Astral"),
        ("authority", "https://identity.astraldeep.invalid/realms/Astral?q=1"),
        ("authority", "https://identity.astraldeep.invalid/realms/Astral#frag"),
        ("websocket_endpoint", "ws://service.astraldeep.invalid/ws"),
        ("websocket_endpoint", "wss://localhost:8001/ws"),
        ("websocket_endpoint", "wss://user@service.astraldeep.invalid/ws"),
        ("websocket_endpoint", "wss://service.astraldeep.invalid/ws?q=1"),
        ("websocket_endpoint", "wss://service.astraldeep.invalid/ws#frag"),
    ],
)
def test_production_profile_rejects_local_or_credential_bearing_uris(
    field: str, value: str
) -> None:
    profile = _profile()
    profile[field] = value
    _assert_invalid(profile, PROFILE_SCHEMA)


@pytest.mark.parametrize(
    "valid",
    [
        "0.0.0",
        "0.4.0",
        "1.2.3-alpha",
        "1.2.3-alpha.1",
        "1.2.3-0",
        "1.2.3+build.7",
        "1.2.3-rc.1+build.7",
    ],
)
def test_strict_semver_accepts_legal_forms(valid: str) -> None:
    _assert_valid(valid, EVIDENCE_SCHEMA["$defs"]["semver"])
    profile = _profile()
    profile["client_version"] = valid
    _assert_valid(profile, PROFILE_SCHEMA)


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "v0.4.0",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2",
        "1.2.3.4",
        "1.2.3-01",
        "1.2.3-alpha..1",
        "1.2.3+build..1",
        "1.2.3_alpha",
    ],
)
def test_strict_semver_rejects_illegal_forms(invalid: str) -> None:
    _assert_invalid(invalid, EVIDENCE_SCHEMA["$defs"]["semver"])


WHITESPACE_AND_LINE_TERMINATORS = [
    " ",
    "\t",
    "\n",
    "\r",
    "\r\n",
    "\v",
    "\f",
    "\u0085",
    "\u00a0",
    "\u1680",
    *[chr(value) for value in range(0x2000, 0x200B)],
    "\u2028",
    "\u2029",
    "\u202f",
    "\u205f",
    "\u3000",
]


@pytest.mark.parametrize("whitespace", WHITESPACE_AND_LINE_TERMINATORS)
@pytest.mark.parametrize("position", ["prefix", "middle", "suffix"])
def test_strict_semver_rejects_every_whitespace_and_line_terminator(
    whitespace: str, position: str
) -> None:
    values = {
        "prefix": f"{whitespace}1.2.3",
        "middle": f"1.2{whitespace}.3",
        "suffix": f"1.2.3{whitespace}",
    }
    _assert_invalid(values[position], EVIDENCE_SCHEMA["$defs"]["semver"])


@pytest.mark.parametrize(
    "reference",
    [
        "bundle://reports/backend.json",
        (
            "gh://AstralDeep/AstralDeep/runs/123/attempts/1/artifacts/456/"
            "members/reports/backend.json"
        ),
        "gh://AstralDeep/AstralDeep/releases/123/assets/456",
        f"oci://ghcr.io/AstralDeep/AstralDeep@sha256:{SHA256}",
    ],
)
def test_immutable_reference_grammar_accepts_canonical_forms(reference: str) -> None:
    _assert_valid(reference, EVIDENCE_SCHEMA["$defs"]["immutable_reference"])


@pytest.mark.parametrize(
    "reference",
    [
        "https://github.com/AstralDeep/AstralDeep/actions/runs/123",
        "bundle://",
        "gh://AstralDeep/AstralDeep/runs/0/attempts/1/artifacts/2/members/a",
        "gh://AstralDeep/AstralDeep/runs/1/artifacts/2/members/a",
        "gh://AstralDeep/AstralDeep/releases/v0.4.0/assets/2",
        "oci://ghcr.io/AstralDeep/AstralDeep:latest",
        f"oci://ghcr.io/AstralDeep/AstralDeep@sha256:{SHA256}?tag=latest",
    ],
)
def test_immutable_reference_grammar_rejects_mutable_or_noncanonical_forms(
    reference: str,
) -> None:
    _assert_invalid(reference, EVIDENCE_SCHEMA["$defs"]["immutable_reference"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_paths", ["background"]),
        ("worker_paths", ["scheduler"]),
        ("worker_paths", ["background", "scheduler"]),
        (
            "candidate_image_reference",
            "ghcr.io/astraldeep/astraldeep:latest",
        ),
        ("endpoint", "https://localhost:8443"),
        ("endpoint", "https://user@stage-060.astraldeep.invalid"),
        ("endpoint", "https://stage-060.astraldeep.invalid?request=1"),
        ("endpoint", "https://stage-060.astraldeep.invalid#request-1"),
    ],
)
def test_staging_shape_requires_real_workers_digest_and_nonlocal_clean_endpoint(
    field: str, value: Any
) -> None:
    staging = _staging_environment()
    schema = EVIDENCE_SCHEMA["$defs"]["staging_environment"]
    _assert_valid(staging, schema, root=EVIDENCE_SCHEMA)
    staging[field] = value
    _assert_invalid(staging, schema, root=EVIDENCE_SCHEMA)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("voice_worker_image_reference",), "ghcr.io/astraldeep/voice-worker:latest"),
        (("livekit_image_reference",), "docker.io/livekit/livekit-server:latest"),
        (("speech_profile", "asr_model"), "other/asr"),
        (("speech_profile", "tts_model"), "other/tts"),
        (("speech_profile", "voice"), "af_alloy"),
        (("speech_profile", "sample_rate_hz"), 16000),
        (("speech_profile", "profile_sha256"), "not-a-sha256"),
    ],
)
def test_voice_runtime_shape_is_digest_pinned_and_exact_profile(
    path: tuple[str, ...], value: Any
) -> None:
    staging = _staging_environment()
    schema = EVIDENCE_SCHEMA["$defs"]["staging_environment"]
    target = staging["voice_runtime"]
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value
    _assert_invalid(staging, schema, root=EVIDENCE_SCHEMA)


def test_non_docs_staging_requires_complete_voice_runtime_identity() -> None:
    schema = EVIDENCE_SCHEMA["$defs"]["staging_environment"]
    staging = _staging_environment()
    _assert_valid(staging, schema, root=EVIDENCE_SCHEMA)
    del staging["voice_runtime"]
    _assert_invalid(staging, schema, root=EVIDENCE_SCHEMA)


def test_measurements_and_raw_provenance_are_structured_and_immutable() -> None:
    evidence = _platform_evidence("backend")
    _assert_valid(evidence, EVIDENCE_SCHEMA)

    missing_sample_count = copy.deepcopy(evidence)
    del missing_sample_count["checks"][0]["measurements"][0]["sample_count"]
    _assert_invalid(missing_sample_count, EVIDENCE_SCHEMA)

    mutable_raw_reference = copy.deepcopy(evidence)
    mutable_raw_reference["checks"][0]["evidence_artifacts"][0][
        "immutable_reference"
    ] = "https://example.invalid/latest.json"
    _assert_invalid(mutable_raw_reference, EVIDENCE_SCHEMA)


@pytest.mark.parametrize("platform", sorted(ARTIFACT_KIND))
def test_platform_documents_bind_expected_artifact_kind(platform: str) -> None:
    evidence = _platform_evidence(platform)
    _assert_valid(evidence, EVIDENCE_SCHEMA)
    wrong_kind = "windows_exe" if platform == "backend" else "container"
    evidence["artifact"]["kind"] = wrong_kind
    _assert_invalid(evidence, EVIDENCE_SCHEMA)


@pytest.mark.parametrize("platform", ["windows", "macos", "ios", "watchos"])
def test_platform_documents_bind_native_runner_os(platform: str) -> None:
    evidence = _platform_evidence(platform)
    _assert_valid(evidence, EVIDENCE_SCHEMA)
    evidence["runner"]["os"] = "linux" if platform != "windows" else "macos"
    _assert_invalid(evidence, EVIDENCE_SCHEMA)


def test_release_trust_accepts_one_exact_producer_manifest() -> None:
    provenance = _trusted_workflow_provenance()
    _assert_valid(provenance, TRUST_SCHEMA)

    missing_job = copy.deepcopy(provenance)
    del missing_job["workflow"]["job_id"]
    _assert_invalid(missing_job, TRUST_SCHEMA)

    mutable_artifact = copy.deepcopy(provenance)
    mutable_artifact["artifacts"][0]["immutable_reference"] = (
        "gh://AstralDeep/AstralDeep/runs/12345/artifacts/67890"
    )
    _assert_invalid(mutable_artifact, TRUST_SCHEMA)

    normalized = copy.deepcopy(provenance)
    normalized["source_provenance"] = {
        "workflow": _workflow("ios-raw-producer"),
        "runner": _runner("macos"),
        "artifacts": [_trusted_member()],
    }
    _assert_valid(normalized, TRUST_SCHEMA)

    unbounded_source = copy.deepcopy(normalized)
    unbounded_source["source_provenance"]["artifacts"] = [
        _trusted_member()
    ] * 4097
    _assert_invalid(unbounded_source, TRUST_SCHEMA)


@pytest.mark.parametrize(
    ("definition", "valid", "invalid"),
    [
        (
            "github_actions_artifact_member",
            _trusted_member(),
            {**_trusted_member(), "artifact_id": "0"},
        ),
        (
            "github_release_asset",
            {
                "kind": "github_release_asset",
                "repository": "AstralDeep/AstralDeep",
                "release_database_id": 123,
                "asset_database_id": 456,
                "asset_name": "AstralDeep.exe",
                "tag": "v0.4.0",
                "target_commit_sha": GIT_SHA,
                "immutable_reference": (
                    "gh://AstralDeep/AstralDeep/releases/123/assets/456"
                ),
                "sha256": SHA256,
            },
            {
                "kind": "github_release_asset",
                "repository": "AstralDeep/AstralDeep",
                "release_database_id": 123,
                "asset_database_id": 456,
                "asset_name": "AstralDeep.exe",
                "tag": "0.4.0",
                "target_commit_sha": GIT_SHA,
                "immutable_reference": (
                    "gh://AstralDeep/AstralDeep/releases/123/assets/456"
                ),
                "sha256": SHA256,
            },
        ),
        (
            "oci_manifest",
            {
                "kind": "oci_manifest",
                "registry": "ghcr.io",
                "repository_path": "AstralDeep/AstralDeep",
                "digest": f"sha256:{SHA256}",
                "immutable_reference": (
                    f"oci://ghcr.io/AstralDeep/AstralDeep@sha256:{SHA256}"
                ),
                "sha256": OTHER_SHA256,
            },
            {
                "kind": "oci_manifest",
                "registry": "ghcr.io",
                "repository_path": "AstralDeep/AstralDeep",
                "digest": f"sha256:{SHA256}",
                "immutable_reference": "oci://ghcr.io/AstralDeep/AstralDeep:latest",
                "sha256": OTHER_SHA256,
            },
        ),
    ],
)
def test_release_trust_artifact_grammars_have_valid_and_invalid_vectors(
    definition: str, valid: dict[str, Any], invalid: dict[str, Any]
) -> None:
    schema = TRUST_SCHEMA["$defs"][definition]
    _assert_valid(valid, schema, root=TRUST_SCHEMA)
    _assert_invalid(invalid, schema, root=TRUST_SCHEMA)
