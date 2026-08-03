#!/usr/bin/env python3
"""Validate Feature 065 voice contracts and their shared conformance fixture.

This tool is intentionally isolated from every product package.  Its only
third-party imports come from the hash-locked ``tooling/contract-ci`` test
environment: jsonschema, openapi-spec-validator, and their locked PyYAML
transitive dependency.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import importlib.metadata
import json
import re
import sys
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
TRANSCRIPT_PACKET_BYTES = 12 * 1024
ANNOUNCEMENT_PACKET_BYTES = 4 * 1024
PLAYOUT_PACKET_BYTES = 2 * 1024
MAX_QUANTUM_SAMPLES = 96_000
MAX_RESULT_OPENING_SAMPLES = 36_000
MAX_RESULT_SAMPLES = 720_000
MAX_PROOF_LIFETIME = timedelta(minutes=2)
MAX_WORKER_GRANT_LIFETIME = timedelta(minutes=5)
WATCH_PLAYBACK_SAMPLES_PER_FRAME = 480

UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
PACKAGE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")
HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")

EXPECTED_DIRECT_DEPENDENCIES = {
    "jsonschema": "4.25.1",
    "openapi-spec-validator": "0.7.2",
}
EXPECTED_REST_MAPPING = {
    "voice_session_start": "createVoiceSession",
    "voice_session_takeover": "takeOverVoiceSession",
    "voice_session_end": "endVoiceSession",
    "voice_microphone_set": "updateVoiceSession",
    "voice_speech_mute_set": "updateVoiceSession",
    "voice_visible_chat_update": "updateVoiceSession",
    "voice_speech_stop": "stopVoiceSpeech",
    "voice_sensitive_recap_request": "consentToSensitiveRecap",
}
EXPECTED_COMPOSER_ACTION_ORDER = (
    "voice_session_start",
    "voice_session_takeover",
    "voice_session_end",
    "voice_microphone_set",
    "voice_speech_stop",
    "voice_speech_mute_set",
    "voice_visible_chat_update",
    "voice_sensitive_recap_request",
)
EXPECTED_OPERATION_LOCATIONS = {
    "getVoiceCapability": ("get", "/api/voice/capability"),
    "createVoiceSession": ("post", "/api/voice/sessions"),
    "takeOverVoiceSession": (
        "post",
        "/api/voice/sessions/{session_id}/takeover",
    ),
    "updateVoiceSession": ("patch", "/api/voice/sessions/{session_id}"),
    "endVoiceSession": ("delete", "/api/voice/sessions/{session_id}"),
    "stopVoiceSpeech": (
        "post",
        "/api/voice/sessions/{session_id}/speech/stop",
    ),
    "consentToSensitiveRecap": (
        "post",
        "/api/voice/sessions/{session_id}/results/{result_id}/read-consent",
    ),
}


class ContractValidationError(ValueError):
    """Raised for a deterministic contract or fixture validation failure."""


class DuplicateKeyError(ContractValidationError):
    """Raised when JSON or YAML repeats a mapping key."""


class ContractBundle:
    """Loaded contract documents used by the fixture runner."""

    __slots__ = ("voice_schema", "worker_schema", "openapi", "fixture")

    def __init__(
        self,
        *,
        voice_schema: dict[str, Any],
        worker_schema: dict[str, Any],
        openapi: dict[str, Any],
        fixture: dict[str, Any],
    ) -> None:
        self.voice_schema = voice_schema
        self.worker_schema = worker_schema
        self.openapi = openapi
        self.fixture = fixture


class ValidationSummary:
    """Content-free counts emitted after successful validation."""

    __slots__ = (
        "case_ids",
        "voice_positive_vectors",
        "worker_positive_vectors",
        "openapi_positive_vectors",
        "negative_vectors",
        "proof_vectors",
        "aggregate_cases",
    )

    def __init__(
        self,
        *,
        case_ids: tuple[str, ...],
        voice_positive_vectors: int,
        worker_positive_vectors: int,
        openapi_positive_vectors: int,
        negative_vectors: int,
        proof_vectors: int,
        aggregate_cases: int,
    ) -> None:
        self.case_ids = case_ids
        self.voice_positive_vectors = voice_positive_vectors
        self.worker_positive_vectors = worker_positive_vectors
        self.openapi_positive_vectors = openapi_positive_vectors
        self.negative_vectors = negative_vectors
        self.proof_vectors = proof_vectors
        self.aggregate_cases = aggregate_cases

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregate_cases": self.aggregate_cases,
            "case_ids": list(self.case_ids),
            "negative_vectors": self.negative_vectors,
            "openapi_positive_vectors": self.openapi_positive_vectors,
            "proof_vectors": self.proof_vectors,
            "voice_positive_vectors": self.voice_positive_vectors,
            "worker_positive_vectors": self.worker_positive_vectors,
        }


def _reject_duplicate_pairs(pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ContractValidationError(f"non-finite JSON number is forbidden: {value}")


def _read_bounded(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ContractValidationError(f"cannot stat contract file {path}: {exc}") from exc
    if size > MAX_DOCUMENT_BYTES:
        raise ContractValidationError(
            f"contract file exceeds {MAX_DOCUMENT_BYTES} bytes: {path}"
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractValidationError(f"cannot read UTF-8 contract file {path}: {exc}") from exc


def strict_load_json(path: Path) -> dict[str, Any]:
    """Load one bounded JSON object with duplicate/non-finite rejection."""

    try:
        loaded = json.loads(
            _read_bounded(path),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise ContractValidationError(f"invalid JSON document {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ContractValidationError(f"JSON document must be one object: {path}")
    return loaded


def strict_load_yaml(path: Path) -> dict[str, Any]:
    """Load one bounded safe YAML object with duplicate-key rejection."""

    # These dependencies are deliberately isolated to the contract-validator
    # environment. Keeping the import at the YAML boundary lets the stdlib-only
    # fixture materializer and semantic gates be reused by backend conformance
    # tests without leaking PyYAML into the runtime/test closure.
    import yaml

    class UniqueKeyLoader(yaml.SafeLoader):
        """Safe YAML loader that rejects duplicate mapping keys."""

    def construct_unique_mapping(
        loader: Any,
        node: Any,
        deep: bool = False,
    ) -> dict[Any, Any]:
        loader.flatten_mapping(node)
        pairs = loader.construct_pairs(node, deep=deep)
        result: dict[Any, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateKeyError(f"duplicate YAML key: {key!r}")
            result[key] = value
        return result

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_unique_mapping,
    )

    try:
        loaded = yaml.load(_read_bounded(path), Loader=UniqueKeyLoader)
    except (yaml.YAMLError, TypeError) as exc:
        raise ContractValidationError(f"invalid YAML document {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ContractValidationError(f"YAML document must be one object: {path}")
    _reject_nonfinite_values(loaded, location="$")
    return loaded


def _reject_nonfinite_values(value: Any, *, location: str) -> None:
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ContractValidationError(f"non-finite YAML number at {location}")
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_nonfinite_values(child, location=f"{location}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nonfinite_values(child, location=f"{location}/{index}")


def load_contract_bundle(repo_root: Path) -> ContractBundle:
    """Load the two schemas, OpenAPI document, and canonical shared fixture."""

    contract_root = repo_root / "specs/065-conversational-voice/contracts"
    fixture_path = repo_root / "backend/tests/fixtures/voice_065/client_conformance.json"
    return ContractBundle(
        voice_schema=strict_load_json(contract_root / "voice-control.schema.json"),
        worker_schema=strict_load_json(contract_root / "worker-control.schema.json"),
        openapi=strict_load_yaml(contract_root / "voice-rest.openapi.yaml"),
        fixture=strict_load_json(fixture_path),
    )


def _walk_values(value: Any, *, location: str = "$") -> list[tuple[str, Any]]:
    walked = [(location, value)]
    if isinstance(value, dict):
        for key, child in value.items():
            walked.extend(_walk_values(child, location=f"{location}/{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            walked.extend(_walk_values(child, location=f"{location}/{index}"))
    return walked


def _resolve_json_pointer(document: Any, reference: str) -> Any:
    if not reference.startswith("#/"):
        raise ContractValidationError(f"only local JSON-pointer refs are allowed: {reference}")
    current = document
    for raw_token in reference[2:].split("/"):
        token = unquote(raw_token).replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(current, list):
                current = current[int(token)]
            else:
                current = current[token]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ContractValidationError(f"unresolved local ref: {reference}") from exc
    return current


def _validate_local_refs(document: dict[str, Any], *, name: str) -> None:
    for location, value in _walk_values(document):
        if not isinstance(value, dict):
            continue
        reference = value.get("$ref")
        if reference is None:
            continue
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise ContractValidationError(
                f"{name} permits only local $ref values at {location}"
            )
        _resolve_json_pointer(document, reference)


def validate_json_schema_document(schema: dict[str, Any], name: str) -> None:
    """Validate one Draft 2020-12 schema and resolve every local reference."""

    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    if schema.get("$schema") != DRAFT_2020_12:
        raise ContractValidationError(
            f"{name} must declare the Draft 2020-12 meta-schema"
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        path = "/".join(str(item) for item in exc.path) or "$"
        raise ContractValidationError(
            f"{name} failed Draft 2020-12 meta-schema validation at {path}"
        ) from exc
    _validate_local_refs(schema, name=name)


def validate_openapi_document(document: dict[str, Any]) -> None:
    """Validate OpenAPI 3.1 shape, local references, and discriminator mappings."""

    from openapi_spec_validator import validate

    _validate_local_refs(document, name="OpenAPI")
    for location, value in _walk_values(document):
        if not isinstance(value, dict):
            continue
        discriminator = value.get("discriminator")
        if not isinstance(discriminator, dict):
            continue
        mapping = discriminator.get("mapping", {})
        if not isinstance(mapping, dict):
            raise ContractValidationError(
                f"OpenAPI discriminator mapping must be an object at {location}"
            )
        for mapped in mapping.values():
            if not isinstance(mapped, str) or not mapped.startswith("#/"):
                raise ContractValidationError(
                    f"OpenAPI permits only local discriminator refs at {location}"
                )
            _resolve_json_pointer(document, mapped)
    try:
        validate(document)
    except Exception as exc:
        raise ContractValidationError(f"OpenAPI meta-validation failed: {exc}") from exc


def _openapi_schema_wrapper(document: dict[str, Any], schema_name: str) -> dict[str, Any]:
    schemas = document.get("components", {}).get("schemas", {})
    if schema_name not in schemas:
        raise ContractValidationError(f"unknown OpenAPI component schema: {schema_name}")
    return {
        "$schema": DRAFT_2020_12,
        "$ref": f"#/components/schemas/{schema_name}",
        "components": {"schemas": schemas},
    }


def _format_schema_error(error: Any) -> str:
    path = "/" + "/".join(str(part) for part in error.absolute_path)
    keyword = str(error.validator or "schema")
    return f"schema: {keyword} failed at {path}"


def instance_errors(
    contract: str,
    payload: Any,
    *,
    bundle: ContractBundle,
    schema_name: str | None = None,
) -> list[str]:
    """Return content-free schema errors for one contract instance."""

    from jsonschema import Draft202012Validator, FormatChecker

    if contract == "voice_control":
        schema = bundle.voice_schema
    elif contract == "worker_control":
        schema = bundle.worker_schema
    elif contract == "openapi":
        if not schema_name:
            return ["schema: OpenAPI component name is required"]
        schema = _openapi_schema_wrapper(bundle.openapi, schema_name)
    else:
        return [f"schema: unknown contract selector {contract!r}"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        _format_schema_error(error)
        for error in sorted(
            validator.iter_errors(payload),
            key=lambda item: (
                tuple(str(part) for part in item.absolute_path),
                str(item.validator),
            ),
        )
    ]


def _merge_vector(base: dict[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in child.items():
        if key in {"base_vector", "mutations"}:
            continue
        if key == "context" and isinstance(result.get(key), dict) and isinstance(value, dict):
            merged = copy.deepcopy(result[key])
            merged.update(copy.deepcopy(value))
            result[key] = merged
        else:
            result[key] = copy.deepcopy(value)
    return result


def _pointer_parent(document: Any, path: str) -> tuple[Any, str]:
    if not path.startswith("/") or path == "/":
        raise ContractValidationError(f"invalid fixture mutation path: {path!r}")
    tokens = [token.replace("~1", "/").replace("~0", "~") for token in path[1:].split("/")]
    current = document
    for token in tokens[:-1]:
        try:
            current = current[int(token)] if isinstance(current, list) else current[token]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ContractValidationError(f"fixture mutation path does not exist: {path}") from exc
    return current, tokens[-1]


def _apply_mutation(target: Any, mutation: Mapping[str, Any]) -> None:
    operation = mutation.get("op")
    path = mutation.get("path")
    if not isinstance(path, str):
        raise ContractValidationError("fixture mutation path must be a string")
    parent, token = _pointer_parent(target, path)

    def present() -> bool:
        if isinstance(parent, list):
            try:
                index = int(token)
            except ValueError:
                return False
            return 0 <= index < len(parent)
        return isinstance(parent, dict) and token in parent

    if operation == "remove":
        if not present():
            raise ContractValidationError(f"cannot remove absent fixture path: {path}")
        if isinstance(parent, list):
            del parent[int(token)]
        else:
            del parent[token]
        return
    if operation not in {"add", "replace", "repeat"}:
        raise ContractValidationError(f"unsupported fixture mutation operation: {operation!r}")
    if operation in {"replace", "repeat"} and not present():
        raise ContractValidationError(f"cannot replace absent fixture path: {path}")
    if operation == "repeat":
        value = mutation.get("value")
        count = mutation.get("count")
        if not isinstance(value, str) or not isinstance(count, int) or count < 0:
            raise ContractValidationError("repeat mutation requires a string and non-negative count")
        replacement: Any = value * count
    else:
        replacement = copy.deepcopy(mutation.get("value"))
    if isinstance(parent, list):
        index = int(token)
        if operation == "add" and index == len(parent):
            parent.append(replacement)
        else:
            parent[index] = replacement
    else:
        parent[token] = replacement


def index_fixture_vectors(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index every non-aggregate fixture vector and reject duplicate IDs."""

    groups: list[list[Any]] = []
    for case in document.get("cases", []):
        if isinstance(case, dict):
            groups.extend([case.get("positive", []), case.get("negative", [])])
    worker = document.get("worker_control_vectors", {})
    openapi = document.get("openapi_instances", {})
    proofs = document.get("proof_vectors", {})
    for section in (worker, openapi):
        if isinstance(section, dict):
            groups.extend([section.get("positive", []), section.get("negative", [])])
    if isinstance(proofs, dict):
        groups.extend([proofs.get("golden", []), proofs.get("negative", [])])

    indexed: dict[str, dict[str, Any]] = {}
    for group in groups:
        if not isinstance(group, list):
            raise ContractValidationError("fixture vector groups must be arrays")
        for vector in group:
            if not isinstance(vector, dict) or not isinstance(vector.get("id"), str):
                raise ContractValidationError("every fixture vector requires a string id")
            vector_id = vector["id"]
            if vector_id in indexed:
                raise ContractValidationError(f"duplicate fixture vector id: {vector_id}")
            indexed[vector_id] = vector
    return indexed


def materialize_vector(
    vector: Mapping[str, Any],
    document: dict[str, Any],
    indexed: Mapping[str, dict[str, Any]],
    *,
    _stack: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Expand one base/mutation fixture vector without mutating tracked data."""

    vector_id = vector.get("id")
    if not isinstance(vector_id, str):
        raise ContractValidationError("fixture vector has no string id")
    if vector_id in _stack:
        raise ContractValidationError(f"fixture vector inheritance cycle at {vector_id}")
    base_id = vector.get("base_vector")
    if base_id is not None:
        if not isinstance(base_id, str) or base_id not in indexed:
            raise ContractValidationError(f"unknown base fixture vector for {vector_id}")
        base = materialize_vector(
            indexed[base_id], document, indexed, _stack=(*_stack, vector_id)
        )
        result = _merge_vector(base, vector)
    else:
        result = copy.deepcopy(dict(vector))

    payload_base = result.get("payload_base")
    if payload_base is not None:
        bases = document.get("payload_bases", {})
        if not isinstance(payload_base, str) or payload_base not in bases:
            raise ContractValidationError(f"unknown payload base for {vector_id}")
        base_payload = copy.deepcopy(bases[payload_base])
        payload = result.get("payload", {})
        if not isinstance(base_payload, dict) or not isinstance(payload, dict):
            raise ContractValidationError(f"invalid payload base for {vector_id}")
        base_payload.update(copy.deepcopy(payload))
        result["payload"] = base_payload
        result.pop("payload_base", None)

    target = result.get("payload") if ("contract" in result or "schema" in result) else result
    for mutation in vector.get("mutations", []):
        if not isinstance(mutation, dict):
            raise ContractValidationError(f"invalid mutation in {vector_id}")
        _apply_mutation(target, mutation)
    result.pop("mutations", None)
    result.pop("base_vector", None)
    return result


def positive_vector_ids(document: dict[str, Any]) -> set[str]:
    positive: set[str] = set()
    for case in document.get("cases", []):
        positive.update(vector["id"] for vector in case.get("positive", []))
    positive.update(
        vector["id"]
        for vector in document.get("worker_control_vectors", {}).get("positive", [])
    )
    positive.update(
        vector["id"]
        for vector in document.get("openapi_instances", {}).get("positive", [])
    )
    positive.update(
        vector["id"]
        for vector in document.get("proof_vectors", {}).get("golden", [])
    )
    return positive


def _parse_utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        raise ContractValidationError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractValidationError(f"{field} is not a real timestamp") from exc
    return parsed.astimezone(UTC)


def packet_size_errors(payload: Mapping[str, Any]) -> list[str]:
    """Enforce the media-plane UTF-8 envelope ceilings."""

    frame_type = payload.get("type")
    limits = {
        "voice_transcript": TRANSCRIPT_PACKET_BYTES,
        "voice_announcement_media": ANNOUNCEMENT_PACKET_BYTES,
        "voice_playout_event": PLAYOUT_PACKET_BYTES,
    }
    limit = limits.get(frame_type)
    if limit is None:
        return []
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return ["packet size cannot be measured for a non-JSON value"]
    if len(encoded) > limit:
        return [f"packet size {len(encoded)} exceeds {limit} UTF-8 bytes"]
    return []


def _context_error(
    payload: Mapping[str, Any], context: Mapping[str, Any], context_key: str, payload_key: str, label: str
) -> str | None:
    if context_key in context and payload.get(payload_key) != context[context_key]:
        return f"{label} does not match the expected binding"
    return None


def voice_semantic_errors(
    payload: Mapping[str, Any], context: Mapping[str, Any] | None = None
) -> list[str]:
    """Apply media/client semantics that JSON Schema cannot express."""

    errors = packet_size_errors(payload)
    context = context or {}
    frame_type = payload.get("type")
    if frame_type in {"ui_event", "chat_created"}:
        nested = payload.get("payload")
        if isinstance(nested, dict):
            for field in (
                "schema_version",
                "connection_generation",
                "submission_id",
                "request_generation",
            ):
                if payload.get(field) != nested.get(field):
                    errors.append(f"correlation equality failed for {field}")
    context_fields = (
        ("expected_device_id", "device_id", "device identity"),
        (
            "expected_connection_generation",
            "connection_generation",
            "connection generation",
        ),
        ("expected_session_id", "session_id", "session identity"),
        ("expected_generation", "generation", "session generation"),
        (
            "expected_media_grant_revision",
            "media_grant_revision",
            "media grant revision",
        ),
    )
    for context_key, payload_key, label in context_fields:
        error = _context_error(payload, context, context_key, payload_key, label)
        if error:
            errors.append(error)
    if "expected_worker_identity" in context:
        actual = payload.get("worker_identity", payload.get("source_participant_identity"))
        if actual != context["expected_worker_identity"]:
            errors.append("worker identity does not match the expected binding")

    if (
        frame_type == "voice_announcement_media"
        and payload.get("transport") == "watch_pcm_websocket"
    ):
        first = payload.get("first_media_sequence")
        last = payload.get("last_media_sequence")
        duration = payload.get("duration_samples")
        if all(isinstance(value, int) and not isinstance(value, bool) for value in (first, last, duration)):
            expected = (last - first + 1) * WATCH_PLAYBACK_SAMPLES_PER_FRAME
            if last < first or duration != expected:
                errors.append("watch sample range does not equal declared duration")
    return errors


def worker_bind_errors(
    payload: Mapping[str, Any], *, server_received_at: str | None = None
) -> list[str]:
    """Validate nested direct-worker RTC grant binding and lifetime semantics."""

    if payload.get("type") != "session_bind":
        return []
    errors: list[str] = []
    grant = payload.get("worker_rtc_grant")
    if not isinstance(grant, dict):
        return ["worker grant is missing"]
    if payload.get("room_name") != grant.get("room_name"):
        errors.append("worker room equality failed")
    if payload.get("worker_identity") != grant.get("worker_identity"):
        errors.append("worker identity equality failed")
    if payload.get("worker_rtc_grant_revision") != grant.get("revision"):
        errors.append("worker grant revision equality failed")
    if payload.get("grant_expires_at") != grant.get("expires_at"):
        errors.append("worker grant expiry equality failed")
    try:
        issued = _parse_utc_timestamp(grant.get("issued_at"), field="worker issued_at")
        expires = _parse_utc_timestamp(grant.get("expires_at"), field="worker expires_at")
        sent = _parse_utc_timestamp(payload.get("sent_at"), field="session_bind sent_at")
        received = _parse_utc_timestamp(
            server_received_at or payload.get("sent_at"), field="server_received_at"
        )
    except ContractValidationError as exc:
        errors.append(str(exc))
        return errors
    if issued < sent:
        errors.append("worker grant issued_at predates assignment")
    if expires <= received:
        errors.append("worker grant is not live after server receipt")
    if expires <= issued:
        errors.append("worker grant expiry must be after issuance")
    elif expires - issued > MAX_WORKER_GRANT_LIFETIME:
        errors.append("worker grant lifetime exceeds five minutes")
    return errors


def worker_semantic_errors(
    payload: Mapping[str, Any], context: Mapping[str, Any] | None = None
) -> list[str]:
    context = context or {}
    received = context.get("server_received_at")
    if received is not None and not isinstance(received, str):
        return ["server_received_at context must be a string"]
    return worker_bind_errors(payload, server_received_at=received)


PROOF_BINDING_FIELDS = (
    "session_id",
    "generation",
    "media_grant_revision",
    "turn_id",
    "client_turn_id",
    "submission_id",
    "request_generation",
    "chat_id",
    "chat_context_revision",
    "source_participant_identity",
    "detected_language",
)
PROOF_UUID_FIELDS = {
    "session_id",
    "turn_id",
    "client_turn_id",
    "submission_id",
    "request_generation",
    "chat_id",
}


def _canonical_transcript(value: Any) -> tuple[str | None, list[str]]:
    if not isinstance(value, str):
        return None, ["transcript text must be a string"]
    normalized_newlines = value.replace("\r\n", "\n")
    for character in normalized_newlines:
        if ord(character) < 32 and character not in {"\t", "\n"}:
            return None, ["transcript contains a forbidden control character"]
    canonical = unicodedata.normalize("NFC", normalized_newlines).strip()
    if not canonical:
        return None, ["final transcript must be non-empty"]
    return canonical, []


def _proof_input(binding: Mapping[str, Any], digest: str, expiry: str) -> str:
    values: list[str] = ["ADVT1"]
    for field in PROOF_BINDING_FIELDS:
        value = binding[field]
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ContractValidationError(f"proof binding field {field} has invalid type")
        values.append(str(value))
    values.extend([digest, expiry])
    try:
        return "\n".join(values).encode("ascii").decode("ascii")
    except UnicodeEncodeError as exc:
        raise ContractValidationError("proof binding must be canonical ASCII") from exc


def proof_vector_errors(vector: Mapping[str, Any]) -> list[str]:
    """Recompute one domain-separated transcript proof golden vector."""

    canonical, errors = _canonical_transcript(vector.get("text_input"))
    if canonical is None:
        return errors
    if vector.get("canonical_text") != canonical:
        errors.append("canonical transcript text mismatch")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if vector.get("text_digest_sha256") != digest:
        errors.append("text digest does not match canonical transcript")
    binding = vector.get("binding")
    if not isinstance(binding, dict):
        return [*errors, "proof binding must be an object"]
    if set(binding) != set(PROOF_BINDING_FIELDS):
        errors.append("proof binding fields are incomplete or contain extras")
        return errors
    for field in PROOF_UUID_FIELDS:
        if not isinstance(binding.get(field), str) or not UUID4_RE.fullmatch(binding[field]):
            errors.append(f"proof binding {field} is not canonical UUID4")
    for field in ("generation", "media_grant_revision", "chat_context_revision"):
        if not isinstance(binding.get(field), int) or isinstance(binding.get(field), bool) or binding[field] < 1:
            errors.append(f"proof binding {field} must be a positive integer")
    expiry_value = vector.get("proof_expires_at")
    try:
        issued = _parse_utc_timestamp(vector.get("issued_at"), field="proof issued_at")
        expiry = _parse_utc_timestamp(expiry_value, field="proof_expires_at")
        validation = _parse_utc_timestamp(vector.get("validation_at"), field="validation_at")
    except ContractValidationError as exc:
        errors.append(str(exc))
        return errors
    if expiry <= issued:
        errors.append("proof expiry must be after issuance")
    elif expiry - issued > MAX_PROOF_LIFETIME:
        errors.append("proof lifetime exceeds two minutes")
    if validation > expiry:
        errors.append("transcript proof is expired")
    try:
        expected_input = _proof_input(binding, digest, expiry_value)
    except ContractValidationError as exc:
        errors.append(str(exc))
        return errors
    if vector.get("proof_input") != expected_input:
        errors.append("proof input does not match immutable binding")
    key_hex = vector.get("key_hex")
    if not isinstance(key_hex, str) or not SHA256_RE.fullmatch(key_hex):
        errors.append("proof key must be 32 synthetic bytes in lowercase hex")
        return errors
    expected_proof = hmac.new(
        bytes.fromhex(key_hex), expected_input.encode("ascii"), hashlib.sha256
    ).hexdigest()
    actual_proof = vector.get("transcript_proof")
    if not isinstance(actual_proof, str) or not hmac.compare_digest(
        actual_proof, expected_proof
    ):
        errors.append("transcript proof HMAC mismatch")
    return errors


def materialize_aggregate_cases(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw_cases = document.get("aggregate_reservation_cases")
    if not isinstance(raw_cases, list):
        raise ContractValidationError("aggregate_reservation_cases must be an array")
    indexed: dict[str, dict[str, Any]] = {}
    for case in raw_cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ContractValidationError("every aggregate case requires a string id")
        if case["id"] in indexed:
            raise ContractValidationError(f"duplicate aggregate case id: {case['id']}")
        indexed[case["id"]] = case

    def materialize(case: Mapping[str, Any], stack: tuple[str, ...]) -> dict[str, Any]:
        case_id = case["id"]
        if case_id in stack:
            raise ContractValidationError(f"aggregate case inheritance cycle at {case_id}")
        base_id = case.get("base_case")
        if base_id is not None:
            if not isinstance(base_id, str) or base_id not in indexed:
                raise ContractValidationError(f"unknown aggregate base case for {case_id}")
            result = materialize(indexed[base_id], (*stack, case_id))
            for key, value in case.items():
                if key not in {"base_case", "append_commands"}:
                    result[key] = copy.deepcopy(value)
        else:
            result = copy.deepcopy(dict(case))
        appended = case.get("append_commands", [])
        if not isinstance(appended, list):
            raise ContractValidationError(f"append_commands must be an array for {case_id}")
        if appended:
            result.setdefault("commands", []).extend(copy.deepcopy(appended))
        result.pop("base_case", None)
        result.pop("append_commands", None)
        return result

    return [materialize(case, ()) for case in raw_cases]


def aggregate_reservation_errors(case: Mapping[str, Any]) -> list[str]:
    """Validate no-refund/idempotent result quantum sample reservations."""

    commands = case.get("commands")
    if not isinstance(commands, list) or not commands:
        return ["aggregate reservation case must contain commands"]
    errors: list[str] = []
    running = 0
    next_index = 0
    seen: dict[str, dict[str, Any]] = {}
    for position, command in enumerate(commands):
        if not isinstance(command, dict):
            errors.append(f"aggregate reservation command {position} is not an object")
            continue
        announcement_id = command.get("announcement_id")
        if not isinstance(announcement_id, str) or not UUID4_RE.fullmatch(announcement_id):
            errors.append(f"aggregate reservation command {position} has invalid UUID4")
            continue
        retry = command.get("retry", False)
        signature = {key: value for key, value in command.items() if key != "retry"}
        previous = seen.get(announcement_id)
        if previous is not None:
            if retry is not True or signature != previous:
                errors.append("exact announcement retry changed its reservation")
            if command.get("result_reserved_samples_after") != running:
                errors.append("reservation echo changed during exact retry")
            continue
        if retry is True:
            errors.append("retry flag used before the announcement was reserved")
        seen[announcement_id] = signature

        role = command.get("quantum_role")
        index = command.get("quantum_index")
        maximum = command.get("max_duration_samples")
        echo = command.get("result_reserved_samples_after")
        if role == "result_opening":
            if index != 0 or next_index != 0:
                errors.append("result opening quantum index must be zero and first")
            limit = MAX_RESULT_OPENING_SAMPLES
        elif role == "result_continuation":
            if index != next_index or next_index == 0:
                errors.append("result continuation quantum index is not contiguous")
            limit = MAX_QUANTUM_SAMPLES
        else:
            errors.append("aggregate reservation accepts only result quantum roles")
            limit = MAX_QUANTUM_SAMPLES
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= limit:
            errors.append(f"quantum sample ceiling is outside 1..{limit}")
            continue
        running += maximum
        if running > MAX_RESULT_SAMPLES:
            errors.append("aggregate reservation exceeds 720000 samples")
        if echo != running:
            errors.append("reservation echo does not equal cumulative command ceilings")
        next_index += 1
    return errors


def _discriminator_key(payload: Mapping[str, Any]) -> str:
    frame_type = payload.get("type")
    if frame_type == "ui_event":
        return f"ui_event:{payload.get('action')}"
    return str(frame_type)


def _branch_discriminators(document: dict[str, Any]) -> set[str]:
    discriminators: set[str] = set()
    branches = document.get("oneOf")
    if not isinstance(branches, list):
        raise ContractValidationError("root contract schema requires oneOf branches")
    for branch in branches:
        resolved = _resolve_json_pointer(document, branch["$ref"]) if isinstance(branch, dict) and "$ref" in branch else branch
        if not isinstance(resolved, dict):
            raise ContractValidationError("root discriminator branch must be an object schema")
        candidates = [resolved]
        all_of = resolved.get("allOf", [])
        if isinstance(all_of, list):
            candidates.extend(
                _resolve_json_pointer(document, item["$ref"])
                if isinstance(item, dict) and "$ref" in item
                else item
                for item in all_of
            )
        type_values: set[str] = set()
        action_values: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            properties = candidate.get("properties", {})
            if not isinstance(properties, dict):
                continue
            for name, target in (("type", type_values), ("action", action_values)):
                schema = properties.get(name)
                if not isinstance(schema, dict):
                    continue
                if isinstance(schema.get("const"), str):
                    target.add(schema["const"])
                enum = schema.get("enum")
                if isinstance(enum, list):
                    target.update(value for value in enum if isinstance(value, str))
        if not type_values:
            raise ContractValidationError("root branch has no type discriminator")
        for frame_type in type_values:
            if frame_type == "ui_event":
                if action_values != {"new_chat"}:
                    raise ContractValidationError("ui_event branch lacks exact action discriminator")
                discriminators.add("ui_event:new_chat")
            else:
                discriminators.add(frame_type)
    return discriminators


def validate_discriminator_coverage(
    bundle: ContractBundle, document: dict[str, Any]
) -> None:
    """Require declared/actual positives for every strict root branch."""

    expected = document.get("expected_discriminators")
    if not isinstance(expected, dict):
        raise ContractValidationError("fixture expected_discriminators must be an object")
    indexed = index_fixture_vectors(document)
    positive = positive_vector_ids(document)
    materialized = {
        vector_id: materialize_vector(indexed[vector_id], document, indexed)
        for vector_id in positive
    }
    definitions = {
        "voice_control": _branch_discriminators(bundle.voice_schema),
        "worker_control": _branch_discriminators(bundle.worker_schema),
        "openapi_media_grant": set(
            bundle.openapi["components"]["schemas"]["MediaGrant"]["discriminator"]["mapping"]
        ),
    }
    for contract, schema_keys in definitions.items():
        declared = expected.get(contract)
        if not isinstance(declared, list) or len(declared) != len(set(declared)):
            raise ContractValidationError(f"{contract} discriminator declaration is invalid")
        if set(declared) != schema_keys:
            raise ContractValidationError(
                f"{contract} discriminator declaration differs from the contract schema"
            )
        if contract == "openapi_media_grant":
            actual = {
                vector["payload"].get("transport")
                for vector in materialized.values()
                if vector.get("schema") == "MediaGrant"
            }
        else:
            actual = {
                _discriminator_key(vector["payload"])
                for vector in materialized.values()
                if vector.get("contract") == contract
            }
        if actual != schema_keys:
            raise ContractValidationError(
                f"{contract} discriminator positives do not cover the exact schema branches"
            )

        for key in sorted(schema_keys):
            candidates = []
            for vector in materialized.values():
                payload = vector.get("payload")
                if not isinstance(payload, dict):
                    continue
                if contract == "openapi_media_grant":
                    match = vector.get("schema") == "MediaGrant" and payload.get("transport") == key
                else:
                    match = vector.get("contract") == contract and _discriminator_key(payload) == key
                if match:
                    candidates.append(vector)
            if not candidates:
                raise ContractValidationError(f"missing positive discriminator vector: {contract}/{key}")
            candidate = copy.deepcopy(candidates[0]["payload"])
            candidate["unexpected_contract_field"] = True
            selector = "openapi" if contract == "openapi_media_grant" else contract
            errors = instance_errors(
                selector,
                candidate,
                bundle=bundle,
                schema_name="MediaGrant" if selector == "openapi" else None,
            )
            if not errors:
                raise ContractValidationError(
                    f"{contract} discriminator branch {key} is not strict about extras"
                )


def _collect_openapi_operations(
    document: dict[str, Any],
) -> dict[str, tuple[str, str]]:
    operations: dict[str, tuple[str, str]] = {}
    for path, path_item in document.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "put", "post", "delete", "patch", "options", "head", "trace"):
            operation = path_item.get(method)
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            operation_id = operation["operationId"]
            if not isinstance(operation_id, str):
                raise ContractValidationError("OpenAPI operationId must be a string")
            if operation_id in operations:
                raise ContractValidationError(f"duplicate operationId: {operation_id}")
            operations[operation_id] = (method, path)
    return operations


def validate_rest_operation_mapping(
    openapi: dict[str, Any], mapping: Mapping[str, Any]
) -> None:
    """Bind every composer voice action to its exact OpenAPI operation."""

    if dict(mapping) != EXPECTED_REST_MAPPING:
        raise ContractValidationError("REST mapping differs from the authoritative action map")
    operations = _collect_openapi_operations(openapi)
    for operation_id, expected_location in EXPECTED_OPERATION_LOCATIONS.items():
        if operations.get(operation_id) != expected_location:
            raise ContractValidationError(
                f"REST mapping operation {operation_id} is absent or on the wrong route"
            )
    for operation_id in mapping.values():
        if operation_id not in operations:
            raise ContractValidationError(
                f"REST mapping references unknown operationId {operation_id}"
            )


def _vector_errors(vector: Mapping[str, Any], bundle: ContractBundle) -> list[str]:
    payload = vector.get("payload")
    if not isinstance(payload, dict):
        return ["schema: vector payload must be an object"]
    context = vector.get("context", {})
    if not isinstance(context, dict):
        return ["schema: vector context must be an object"]
    contract = vector.get("contract")
    if contract == "voice_control":
        return [
            *instance_errors("voice_control", payload, bundle=bundle),
            *voice_semantic_errors(payload, context),
        ]
    if contract == "worker_control":
        return [
            *instance_errors("worker_control", payload, bundle=bundle),
            *worker_semantic_errors(payload, context),
        ]
    if "schema" in vector:
        return instance_errors(
            "openapi", payload, bundle=bundle, schema_name=vector.get("schema")
        )
    return ["schema: vector has no recognized contract"]


def _validate_case_shape(document: dict[str, Any]) -> tuple[str, ...]:
    required_root = {
        "schema_version",
        "description",
        "expected_discriminators",
        "rest_operation_mapping",
        "cases",
        "payload_bases",
        "worker_control_vectors",
        "openapi_instances",
        "proof_vectors",
        "aggregate_reservation_cases",
    }
    if set(document) != required_root:
        raise ContractValidationError("fixture root fields are incomplete or contain extras")
    if document.get("schema_version") != "1":
        raise ContractValidationError("fixture schema_version must be 1")
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ContractValidationError("fixture cases must be an array")
    case_ids: list[str] = []
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "title", "positive", "negative"}:
            raise ContractValidationError("each C0-C6 case has invalid fields")
        case_id = case.get("id")
        if not isinstance(case_id, str):
            raise ContractValidationError("case id must be a string")
        if not case.get("positive") or not case.get("negative"):
            raise ContractValidationError(f"{case_id} requires positive and negative vectors")
        case_ids.append(case_id)
    expected = [f"C{index}" for index in range(7)]
    if case_ids != expected:
        raise ContractValidationError("fixture must contain C0-C6 exactly and in order")
    return tuple(case_ids)


def validate_fixture_document(
    document: dict[str, Any], bundle: ContractBundle
) -> ValidationSummary:
    """Run every accepted/rejected fixture and cross-contract semantic gate."""

    case_ids = _validate_case_shape(document)
    indexed = index_fixture_vectors(document)
    positive_ids = positive_vector_ids(document)
    validate_rest_operation_mapping(bundle.openapi, document["rest_operation_mapping"])
    validate_discriminator_coverage(bundle, document)

    c0 = materialize_vector(indexed["C0-P1-composer"], document, indexed)
    controls = c0["payload"]["voice"]["controls"]
    control_actions = [control.get("action") for control in controls]
    if control_actions != list(EXPECTED_COMPOSER_ACTION_ORDER):
        raise ContractValidationError(
            "C0 composer actions do not preserve the authoritative REST mapping order"
        )

    voice_positive = 0
    worker_positive = 0
    openapi_positive = 0
    negative = 0
    for vector_id, raw_vector in indexed.items():
        if vector_id.startswith(("P-G", "P-N")):
            continue
        vector = materialize_vector(raw_vector, document, indexed)
        errors = _vector_errors(vector, bundle)
        if vector_id in positive_ids:
            if errors:
                raise ContractValidationError(
                    f"positive fixture vector {vector_id} was rejected: {errors[0]}"
                )
            if vector.get("contract") == "voice_control":
                voice_positive += 1
            elif vector.get("contract") == "worker_control":
                worker_positive += 1
            elif "schema" in vector:
                openapi_positive += 1
        else:
            negative += 1
            if not errors:
                raise ContractValidationError(
                    f"negative fixture vector {vector_id} was unexpectedly accepted"
                )
            expected_error = vector.get("expected_error")
            if not isinstance(expected_error, str) or not any(
                expected_error in error for error in errors
            ):
                raise ContractValidationError(
                    f"negative fixture vector {vector_id} failed for the wrong reason"
                )

    proof_section = document["proof_vectors"]
    proof_vectors = [*proof_section["golden"], *proof_section["negative"]]
    for raw_vector in proof_vectors:
        vector = materialize_vector(raw_vector, document, indexed)
        errors = proof_vector_errors(vector)
        if vector.get("expect") == "accept":
            if errors:
                raise ContractValidationError(
                    f"proof golden vector {vector['id']} was rejected: {errors[0]}"
                )
        else:
            negative += 1
            expected_error = vector.get("expected_error")
            if not errors or not isinstance(expected_error, str) or not any(
                expected_error in error for error in errors
            ):
                raise ContractValidationError(
                    f"negative proof vector {vector['id']} failed for the wrong reason"
                )

    aggregate_cases = materialize_aggregate_cases(document)
    for case in aggregate_cases:
        errors = aggregate_reservation_errors(case)
        if case.get("expect") == "accept":
            if errors:
                raise ContractValidationError(
                    f"aggregate case {case['id']} was rejected: {errors[0]}"
                )
        else:
            negative += 1
            expected_error = case.get("expected_error")
            if not errors or not isinstance(expected_error, str) or not any(
                expected_error in error for error in errors
            ):
                raise ContractValidationError(
                    f"negative aggregate case {case['id']} failed for the wrong reason"
                )

    return ValidationSummary(
        case_ids=case_ids,
        voice_positive_vectors=voice_positive,
        worker_positive_vectors=worker_positive,
        openapi_positive_vectors=openapi_positive,
        negative_vectors=negative,
        proof_vectors=len(proof_vectors),
        aggregate_cases=len(aggregate_cases),
    )


def validate_dependency_lock(repo_root: Path) -> None:
    """Require the approved validator dependencies and hashes for every lock entry."""

    tool_root = repo_root / "tooling/contract-ci"
    requirements_in = _read_bounded(tool_root / "requirements.in")
    direct: dict[str, str] = {}
    for raw_line in requirements_in.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PACKAGE_RE.fullmatch(line)
        if not match:
            raise ContractValidationError("requirements.in must contain only exact pins")
        direct[match.group(1).lower()] = match.group(2)
    if direct != EXPECTED_DIRECT_DEPENDENCIES:
        raise ContractValidationError("validator direct dependency pins differ from approval")

    lock_text = _read_bounded(tool_root / "requirements.lock.txt")
    if "--only-binary=:all:" not in lock_text:
        raise ContractValidationError("validator lock must require binary wheels")
    blocks: dict[str, tuple[str, str]] = {}
    current_name: str | None = None
    current_version: str | None = None
    current_lines: list[str] = []

    def finish_block() -> None:
        nonlocal current_name, current_version, current_lines
        if current_name is None or current_version is None:
            return
        block = "\n".join(current_lines)
        hashes = HASH_RE.findall(block)
        if not hashes:
            raise ContractValidationError(
                f"validator lock entry {current_name} has no SHA-256 hash"
            )
        blocks[current_name] = (current_version, block)
        current_name = None
        current_version = None
        current_lines = []

    for raw_line in lock_text.splitlines():
        match = PACKAGE_RE.match(raw_line)
        if match:
            finish_block()
            current_name = match.group(1).lower()
            current_version = match.group(2)
            current_lines = [raw_line]
        elif current_name is not None:
            current_lines.append(raw_line)
    finish_block()
    if not blocks:
        raise ContractValidationError("validator lock contains no package pins")
    for name, version in EXPECTED_DIRECT_DEPENDENCIES.items():
        if name not in blocks or blocks[name][0] != version:
            raise ContractValidationError(f"validator lock is missing exact {name} pin")


def _validate_installed_versions() -> None:
    for package, expected in EXPECTED_DIRECT_DEPENDENCIES.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ContractValidationError(
                f"isolated validator dependency is not installed: {package}"
            ) from exc
        if actual != expected:
            raise ContractValidationError(
                f"isolated validator dependency {package} is {actual}, expected {expected}"
            )


def validate_repository(repo_root: Path) -> ValidationSummary:
    """Validate locks, schemas, OpenAPI, and every tracked fixture vector."""

    validate_dependency_lock(repo_root)
    _validate_installed_versions()
    bundle = load_contract_bundle(repo_root)
    validate_json_schema_document(bundle.voice_schema, "voice-control.schema.json")
    validate_json_schema_document(bundle.worker_schema, "worker-control.schema.json")
    validate_openapi_document(bundle.openapi)
    return validate_fixture_document(bundle.fixture, bundle)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="AstralDeep repository root (defaults to this script's checkout)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = validate_repository(args.repo_root.resolve())
    except ContractValidationError as exc:
        print(f"voice contract validation rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "passed", **summary.to_dict()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
