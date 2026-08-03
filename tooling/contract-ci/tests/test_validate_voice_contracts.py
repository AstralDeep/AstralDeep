"""Failure-first tests for the isolated Feature 065 contract validator."""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = REPO_ROOT / "tooling/contract-ci/validate_voice_contracts.py"
FIXTURE_PATH = (
    REPO_ROOT / "backend/tests/fixtures/voice_065/client_conformance.json"
)


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "validate_voice_contracts", VALIDATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load validator module at {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


@pytest.fixture(scope="module")
def bundle() -> Any:
    return validator.load_contract_bundle(REPO_ROOT)


@pytest.fixture(scope="module")
def fixture_document() -> dict[str, Any]:
    loaded = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _find_vector(document: dict[str, Any], vector_id: str) -> dict[str, Any]:
    vectors = validator.index_fixture_vectors(document)
    return validator.materialize_vector(vectors[vector_id], document, vectors)


def test_complete_repository_bundle_passes_meta_and_fixture_validation(
    bundle: Any,
) -> None:
    summary = validator.validate_repository(REPO_ROOT)

    assert summary.case_ids == ("C0", "C1", "C2", "C3", "C4", "C5", "C6")
    assert summary.voice_positive_vectors >= 20
    assert summary.worker_positive_vectors == 25
    assert summary.openapi_positive_vectors == 2
    assert summary.negative_vectors >= 20
    assert summary.proof_vectors == 6
    assert summary.aggregate_cases == 5
    assert bundle.voice_schema["$schema"] == validator.DRAFT_2020_12
    assert bundle.worker_schema["$schema"] == validator.DRAFT_2020_12


def test_json_schema_meta_validation_and_local_reference_resolution(
    bundle: Any,
) -> None:
    validator.validate_json_schema_document(bundle.voice_schema, "voice-control")
    validator.validate_json_schema_document(bundle.worker_schema, "worker-control")

    malformed = copy.deepcopy(bundle.voice_schema)
    malformed["$defs"]["uuid"]["type"] = "uuid4"
    with pytest.raises(validator.ContractValidationError, match="meta-schema"):
        validator.validate_json_schema_document(malformed, "malformed")

    remote_ref = copy.deepcopy(bundle.worker_schema)
    remote_ref["oneOf"][0]["$ref"] = "https://attacker.invalid/schema.json"
    with pytest.raises(validator.ContractValidationError, match="local.*ref"):
        validator.validate_json_schema_document(remote_ref, "remote-ref")

    missing_ref = copy.deepcopy(bundle.worker_schema)
    missing_ref["oneOf"][0]["$ref"] = "#/$defs/absent"
    with pytest.raises(validator.ContractValidationError, match="unresolved.*ref"):
        validator.validate_json_schema_document(missing_ref, "missing-ref")


def test_openapi_meta_validation_and_refs_are_strictly_local(bundle: Any) -> None:
    validator.validate_openapi_document(bundle.openapi)

    malformed = copy.deepcopy(bundle.openapi)
    malformed["openapi"] = "2.0"
    with pytest.raises(validator.ContractValidationError, match="OpenAPI"):
        validator.validate_openapi_document(malformed)

    remote_ref = copy.deepcopy(bundle.openapi)
    remote_ref["paths"]["/api/voice/capability"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"] = "https://attacker.invalid/schema"
    with pytest.raises(validator.ContractValidationError, match="local.*ref"):
        validator.validate_openapi_document(remote_ref)


def test_discriminator_coverage_and_strict_extra_fields(
    bundle: Any, fixture_document: dict[str, Any]
) -> None:
    validator.validate_discriminator_coverage(bundle, fixture_document)

    incomplete = copy.deepcopy(fixture_document)
    incomplete["expected_discriminators"]["voice_control"].remove(
        "voice_playout_event"
    )
    with pytest.raises(
        validator.ContractValidationError, match="voice_control discriminator"
    ):
        validator.validate_discriminator_coverage(bundle, incomplete)

    vectors = validator.index_fixture_vectors(fixture_document)
    for vector in vectors.values():
        if vector.get("contract") not in {"voice_control", "worker_control"}:
            continue
        materialized = validator.materialize_vector(
            vector, fixture_document, vectors
        )
        if vector["id"] not in validator.positive_vector_ids(fixture_document):
            continue
        payload = copy.deepcopy(materialized["payload"])
        payload["unexpected_contract_field"] = True
        errors = validator.instance_errors(
            vector["contract"], payload, bundle=bundle
        )
        assert errors, f"{vector['id']} accepted an extra top-level field"


def test_rest_action_mapping_matches_unique_openapi_operations(
    bundle: Any, fixture_document: dict[str, Any]
) -> None:
    validator.validate_rest_operation_mapping(
        bundle.openapi, fixture_document["rest_operation_mapping"]
    )

    wrong = dict(fixture_document["rest_operation_mapping"])
    wrong["voice_session_start"] = "stopVoiceSpeech"
    with pytest.raises(validator.ContractValidationError, match="REST mapping"):
        validator.validate_rest_operation_mapping(bundle.openapi, wrong)

    duplicate = copy.deepcopy(bundle.openapi)
    duplicate["paths"]["/api/voice/capability"]["get"][
        "operationId"
    ] = "createVoiceSession"
    with pytest.raises(validator.ContractValidationError, match="duplicate operationId"):
        validator.validate_rest_operation_mapping(
            duplicate, fixture_document["rest_operation_mapping"]
        )


def test_worker_registration_and_fixed_profile_are_executable(
    bundle: Any, fixture_document: dict[str, Any]
) -> None:
    register = _find_vector(fixture_document, "W-P01-register")["payload"]
    assert not validator.instance_errors("worker_control", register, bundle=bundle)

    bad_capacity = copy.deepcopy(register)
    bad_capacity["max_sessions"] = 0
    assert validator.instance_errors(
        "worker_control", bad_capacity, bundle=bundle
    )

    bad_profile = copy.deepcopy(register)
    bad_profile["profile"]["voice"] = "not-af-heart"
    assert validator.instance_errors(
        "worker_control", bad_profile, bundle=bundle
    )


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("room_name", "other-room", "worker room equality"),
        ("worker_identity", "voice-worker-02", "worker identity equality"),
        ("revision", 6, "worker grant revision equality"),
        ("expires_at", "2026-07-31T12:04:59Z", "worker grant expiry equality"),
    ],
)
def test_worker_grant_outer_nested_equality(
    bundle: Any,
    fixture_document: dict[str, Any],
    path: str,
    value: Any,
    message: str,
) -> None:
    bind = _find_vector(fixture_document, "W-P03-bind")["payload"]
    grant = copy.deepcopy(bind)
    grant["worker_rtc_grant"][path] = value
    assert any(
        message in error
        for error in validator.worker_bind_errors(
            grant, server_received_at="2026-07-31T12:00:00Z"
        )
    )
    assert not validator.instance_errors("worker_control", bind, bundle=bundle)


def test_worker_grant_expiry_is_positive_and_at_most_five_minutes(
    fixture_document: dict[str, Any],
) -> None:
    bind = _find_vector(fixture_document, "W-P03-bind")["payload"]
    assert not validator.worker_bind_errors(
        bind, server_received_at="2026-07-31T12:00:00Z"
    )

    too_long = copy.deepcopy(bind)
    too_long["grant_expires_at"] = "2026-07-31T12:05:01Z"
    too_long["worker_rtc_grant"]["expires_at"] = "2026-07-31T12:05:01Z"
    assert any(
        "five minutes" in error
        for error in validator.worker_bind_errors(
            too_long, server_received_at="2026-07-31T12:00:00Z"
        )
    )

    assert any(
        "server receipt" in error
        for error in validator.worker_bind_errors(
            bind, server_received_at="2026-07-31T12:05:00Z"
        )
    )


def test_packet_byte_limits_use_utf8_not_character_count(
    fixture_document: dict[str, Any]
) -> None:
    valid = _find_vector(fixture_document, "C2-P2-final")["payload"]
    oversized = _find_vector(fixture_document, "C2-N2-packet-too-large")[
        "payload"
    ]

    assert not validator.packet_size_errors(valid)
    errors = validator.packet_size_errors(oversized)
    assert any("packet size" in error and "12288" in error for error in errors)
    assert len(oversized["text"]) < 8000


def test_uuid_format_check_rejects_non_v4_and_noncanonical_values(
    bundle: Any, fixture_document: dict[str, Any]
) -> None:
    frame = _find_vector(fixture_document, "C3-P3-playout")["payload"]
    for invalid in (
        "00000000-0000-3000-8000-000000000001",
        "00000000-0000-4000-C000-000000000001",
        "abcdefab-cdef-4abc-8def-abcdefabcdef".upper(),
    ):
        candidate = copy.deepcopy(frame)
        candidate["device_id"] = invalid
        assert validator.instance_errors(
            "voice_control", candidate, bundle=bundle
        )


def test_proof_golden_and_negative_vectors(fixture_document: dict[str, Any]) -> None:
    vectors = validator.index_fixture_vectors(fixture_document)
    golden = validator.materialize_vector(
        vectors["P-G1-normalized-final"], fixture_document, vectors
    )
    assert not validator.proof_vector_errors(golden)

    for vector in fixture_document["proof_vectors"]["negative"]:
        materialized = validator.materialize_vector(
            vector, fixture_document, vectors
        )
        errors = validator.proof_vector_errors(materialized)
        assert errors, vector["id"]
        assert any(vector["expected_error"] in error for error in errors)


def test_quantum_schema_bounds_and_watch_sample_range(
    bundle: Any, fixture_document: dict[str, Any]
) -> None:
    opening = _find_vector(fixture_document, "C3-P1-livekit-opening")["payload"]
    assert not validator.instance_errors("voice_control", opening, bundle=bundle)

    opening["duration_samples"] = 36001
    assert validator.instance_errors("voice_control", opening, bundle=bundle)

    speak = _find_vector(fixture_document, "W-P12-speak")["payload"]
    speak["quantum_role"] = "result_continuation"
    speak["quantum_index"] = 1
    speak["max_duration_samples"] = 96000
    speak["result_reserved_samples_after"] = 132000
    assert not validator.instance_errors("worker_control", speak, bundle=bundle)

    speak["max_duration_samples"] = 96001
    assert validator.instance_errors("worker_control", speak, bundle=bundle)

    watch = _find_vector(fixture_document, "C3-N3-watch-range-mismatch")[
        "payload"
    ]
    assert any("sample range" in error for error in validator.voice_semantic_errors(watch))


def test_aggregate_reservation_boundary_retry_and_overrun(
    fixture_document: dict[str, Any]
) -> None:
    cases = validator.materialize_aggregate_cases(fixture_document)
    for case in cases:
        errors = validator.aggregate_reservation_errors(case)
        if case["expect"] == "accept":
            assert not errors, (case["id"], errors)
        else:
            assert errors, case["id"]
            assert any(case["expected_error"] in error for error in errors)


def test_all_tracked_negative_vectors_fail_for_the_declared_reason(
    bundle: Any, fixture_document: dict[str, Any]
) -> None:
    validator.validate_fixture_document(fixture_document, bundle)


def test_validator_dependency_lane_is_exact_and_hash_locked() -> None:
    validator.validate_dependency_lock(REPO_ROOT)


def test_validator_imports_no_product_modules() -> None:
    tree = ast.parse(VALIDATOR_PATH.read_text(encoding="utf-8"))
    allowed = {
        "argparse",
        "__future__",
        "copy",
        "dataclasses",
        "datetime",
        "hashlib",
        "hmac",
        "importlib",
        "json",
        "pathlib",
        "re",
        "sys",
        "typing",
        "unicodedata",
        "urllib",
        "jsonschema",
        "openapi_spec_validator",
        "yaml",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert imported <= allowed


def test_strict_document_loaders_reject_ambiguous_or_unbounded_input(
    tmp_path: Path,
) -> None:
    duplicate_json = tmp_path / "duplicate.json"
    duplicate_json.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    with pytest.raises(validator.DuplicateKeyError):
        validator.strict_load_json(duplicate_json)

    nonfinite_json = tmp_path / "nonfinite.json"
    nonfinite_json.write_text('{"a": NaN}', encoding="utf-8")
    with pytest.raises(validator.ContractValidationError, match="non-finite"):
        validator.strict_load_json(nonfinite_json)

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(validator.ContractValidationError, match="invalid JSON"):
        validator.strict_load_json(invalid_json)

    array_json = tmp_path / "array.json"
    array_json.write_text("[]", encoding="utf-8")
    with pytest.raises(validator.ContractValidationError, match="one object"):
        validator.strict_load_json(array_json)

    duplicate_yaml = tmp_path / "duplicate.yaml"
    duplicate_yaml.write_text("a: 1\na: 2\n", encoding="utf-8")
    with pytest.raises(validator.DuplicateKeyError):
        validator.strict_load_yaml(duplicate_yaml)

    nonfinite_yaml = tmp_path / "nonfinite.yaml"
    nonfinite_yaml.write_text("a: .nan\n", encoding="utf-8")
    with pytest.raises(validator.ContractValidationError, match="non-finite"):
        validator.strict_load_yaml(nonfinite_yaml)

    array_yaml = tmp_path / "array.yaml"
    array_yaml.write_text("- item\n", encoding="utf-8")
    with pytest.raises(validator.ContractValidationError, match="one object"):
        validator.strict_load_yaml(array_yaml)

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(validator.ContractValidationError, match="cannot read UTF-8"):
        validator.strict_load_json(invalid_utf8)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (validator.MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(validator.ContractValidationError, match="exceeds"):
        validator.strict_load_json(oversized)

    with pytest.raises(validator.ContractValidationError, match="cannot stat"):
        validator.strict_load_json(tmp_path / "absent.json")


def test_fixture_mutation_and_inheritance_fail_closed(
    fixture_document: dict[str, Any]
) -> None:
    assert validator._resolve_json_pointer({"a": ["ok"]}, "#/a/0") == "ok"
    with pytest.raises(validator.ContractValidationError, match="only local"):
        validator._resolve_json_pointer({}, "https://invalid.example/ref")

    target = {"items": ["first"], "value": "old"}
    validator._apply_mutation(
        target, {"op": "add", "path": "/items/1", "value": "second"}
    )
    validator._apply_mutation(
        target, {"op": "replace", "path": "/items/0", "value": "changed"}
    )
    validator._apply_mutation(target, {"op": "remove", "path": "/items/1"})
    assert target == {"items": ["changed"], "value": "old"}

    for mutation, message in (
        ({"op": "replace", "path": "bad", "value": 1}, "invalid"),
        ({"op": "replace", "path": "/absent", "value": 1}, "absent"),
        ({"op": "remove", "path": "/absent"}, "absent"),
        ({"op": "unknown", "path": "/value"}, "unsupported"),
        (
            {"op": "repeat", "path": "/value", "value": 1, "count": -1},
            "repeat",
        ),
    ):
        with pytest.raises(validator.ContractValidationError, match=message):
            validator._apply_mutation(copy.deepcopy(target), mutation)

    malformed = copy.deepcopy(fixture_document)
    malformed["cases"][0]["positive"].append(
        {"id": "cycle-a", "base_vector": "cycle-b"}
    )
    malformed["cases"][0]["positive"].append(
        {"id": "cycle-b", "base_vector": "cycle-a"}
    )
    indexed = validator.index_fixture_vectors(malformed)
    with pytest.raises(validator.ContractValidationError, match="cycle"):
        validator.materialize_vector(indexed["cycle-a"], malformed, indexed)

    with pytest.raises(validator.ContractValidationError, match="unknown base"):
        validator.materialize_vector(
            {"id": "unknown", "base_vector": "absent"},
            fixture_document,
            validator.index_fixture_vectors(fixture_document),
        )


def test_proof_validator_rejects_malformed_binding_and_crypto_fields(
    fixture_document: dict[str, Any]
) -> None:
    vectors = validator.index_fixture_vectors(fixture_document)
    golden = validator.materialize_vector(
        vectors["P-G1-normalized-final"], fixture_document, vectors
    )

    mutations = [
        ("text_input", None, "text must be"),
        ("text_input", "  \t\n", "non-empty"),
        ("canonical_text", "wrong", "canonical transcript"),
        ("binding", None, "binding must be"),
        ("key_hex", "00", "proof key"),
        ("transcript_proof", "0" * 64, "HMAC"),
        ("issued_at", "2026-07-31", "canonical UTC"),
    ]
    for field, value, message in mutations:
        candidate = copy.deepcopy(golden)
        candidate[field] = value
        assert any(
            message in error for error in validator.proof_vector_errors(candidate)
        )

    missing_field = copy.deepcopy(golden)
    del missing_field["binding"]["turn_id"]
    assert any(
        "binding fields" in error
        for error in validator.proof_vector_errors(missing_field)
    )

    bad_uuid = copy.deepcopy(golden)
    bad_uuid["binding"]["turn_id"] = "not-a-uuid"
    assert any("UUID4" in error for error in validator.proof_vector_errors(bad_uuid))

    bad_integer = copy.deepcopy(golden)
    bad_integer["binding"]["generation"] = 0
    assert any(
        "positive integer" in error
        for error in validator.proof_vector_errors(bad_integer)
    )

    non_ascii = copy.deepcopy(golden)
    non_ascii["binding"]["source_participant_identity"] = "wörker"
    assert any("ASCII" in error for error in validator.proof_vector_errors(non_ascii))

    reversed_expiry = copy.deepcopy(golden)
    reversed_expiry["proof_expires_at"] = reversed_expiry["issued_at"]
    assert any(
        "after issuance" in error
        for error in validator.proof_vector_errors(reversed_expiry)
    )


def test_aggregate_reservation_validator_rejects_malformed_commands() -> None:
    assert validator.aggregate_reservation_errors({})
    invalid_cases = [
        {"commands": ["not-an-object"]},
        {
            "commands": [
                {
                    "announcement_id": "not-a-uuid",
                    "quantum_role": "result_opening",
                    "quantum_index": 0,
                    "max_duration_samples": 1,
                    "result_reserved_samples_after": 1,
                }
            ]
        },
        {
            "commands": [
                {
                    "announcement_id": "00000000-0000-4000-8000-000000000130",
                    "quantum_role": "result_continuation",
                    "quantum_index": 0,
                    "max_duration_samples": 1,
                    "result_reserved_samples_after": 1,
                }
            ]
        },
        {
            "commands": [
                {
                    "announcement_id": "00000000-0000-4000-8000-000000000131",
                    "quantum_role": "single",
                    "quantum_index": 0,
                    "max_duration_samples": 1,
                    "result_reserved_samples_after": 1,
                }
            ]
        },
        {
            "commands": [
                {
                    "announcement_id": "00000000-0000-4000-8000-000000000132",
                    "quantum_role": "result_opening",
                    "quantum_index": 0,
                    "max_duration_samples": 0,
                    "result_reserved_samples_after": 0,
                }
            ]
        },
        {
            "commands": [
                {
                    "announcement_id": "00000000-0000-4000-8000-000000000133",
                    "quantum_role": "result_opening",
                    "quantum_index": 0,
                    "max_duration_samples": 1,
                    "result_reserved_samples_after": 1,
                    "retry": True,
                }
            ]
        },
    ]
    for case in invalid_cases:
        assert validator.aggregate_reservation_errors(case)

    changed_retry = {
        "commands": [
            {
                "announcement_id": "00000000-0000-4000-8000-000000000134",
                "quantum_role": "result_opening",
                "quantum_index": 0,
                "max_duration_samples": 1,
                "result_reserved_samples_after": 1,
            },
            {
                "announcement_id": "00000000-0000-4000-8000-000000000134",
                "quantum_role": "result_opening",
                "quantum_index": 0,
                "max_duration_samples": 2,
                "result_reserved_samples_after": 2,
                "retry": True,
            },
        ]
    }
    errors = validator.aggregate_reservation_errors(changed_retry)
    assert any("exact announcement retry" in error for error in errors)
    assert any("echo changed" in error for error in errors)


def test_fixture_root_and_vector_indexes_reject_malformed_metadata(
    fixture_document: dict[str, Any]
) -> None:
    malformed = copy.deepcopy(fixture_document)
    malformed["extra"] = True
    with pytest.raises(validator.ContractValidationError, match="root fields"):
        validator._validate_case_shape(malformed)

    malformed = copy.deepcopy(fixture_document)
    malformed["schema_version"] = "2"
    with pytest.raises(validator.ContractValidationError, match="schema_version"):
        validator._validate_case_shape(malformed)

    malformed = copy.deepcopy(fixture_document)
    malformed["cases"] = {}
    with pytest.raises(validator.ContractValidationError, match="cases"):
        validator._validate_case_shape(malformed)

    malformed = copy.deepcopy(fixture_document)
    malformed["cases"][0]["positive"] = []
    with pytest.raises(validator.ContractValidationError, match="positive and negative"):
        validator._validate_case_shape(malformed)

    malformed = copy.deepcopy(fixture_document)
    duplicate = copy.deepcopy(malformed["cases"][0]["positive"][0])
    malformed["cases"][0]["negative"].append(duplicate)
    with pytest.raises(validator.ContractValidationError, match="duplicate fixture"):
        validator.index_fixture_vectors(malformed)


def test_dependency_lock_and_installed_version_failures_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_root = tmp_path / "tooling/contract-ci"
    tool_root.mkdir(parents=True)
    requirements = tool_root / "requirements.in"
    lock = tool_root / "requirements.lock.txt"

    requirements.write_text("jsonschema>=4\n", encoding="utf-8")
    lock.write_text("--only-binary=:all:\n", encoding="utf-8")
    with pytest.raises(validator.ContractValidationError, match="exact pins"):
        validator.validate_dependency_lock(tmp_path)

    requirements.write_text(
        "jsonschema==4.25.1\nopenapi-spec-validator==0.7.2\n",
        encoding="utf-8",
    )
    lock.write_text(
        "--only-binary=:all:\njsonschema==4.25.1 \\\n+    --hash=sha256:" + "1" * 64 + "\n",
        encoding="utf-8",
    )
    with pytest.raises(validator.ContractValidationError, match="missing exact"):
        validator.validate_dependency_lock(tmp_path)

    def missing_version(_package: str) -> str:
        raise validator.importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(validator.importlib.metadata, "version", missing_version)
    with pytest.raises(validator.ContractValidationError, match="not installed"):
        validator._validate_installed_versions()

    monkeypatch.setattr(validator.importlib.metadata, "version", lambda _package: "0")
    with pytest.raises(validator.ContractValidationError, match="expected"):
        validator._validate_installed_versions()


def test_cli_success_and_content_free_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert validator.main(["--repo-root", str(REPO_ROOT)]) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["status"] == "passed"
    assert success["case_ids"] == [f"C{index}" for index in range(7)]

    def reject(_root: Path) -> Any:
        raise validator.ContractValidationError("synthetic-safe-error")

    monkeypatch.setattr(validator, "validate_repository", reject)
    assert validator.main(["--repo-root", str(REPO_ROOT)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "synthetic-safe-error" in captured.err
