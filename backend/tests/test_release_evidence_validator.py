"""Fail-closed release-evidence policy tests for feature 060 (T103/T106)."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "validate_release_evidence.py"
CONTRACT_TEST_PATH = REPO_ROOT / "backend" / "tests" / "test_release_contract_schemas.py"
FIXTURE_ROOT = (
    REPO_ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "runtime_reliability_060"
    / "release_evidence"
)
CONTRACT_ROOT = REPO_ROOT / "specs" / "060-runtime-reliability-hardening" / "contracts"

if not (
    (REPO_ROOT / "scripts").is_dir() and (REPO_ROOT / "specs").is_dir()
):  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def validator() -> Any:
    return _load_module("release_evidence_validator_060", SCRIPT_PATH)


@pytest.fixture(scope="module")
def contract_examples() -> Any:
    return _load_module("release_contract_examples_060", CONTRACT_TEST_PATH)


def _measurement(
    metric: str,
    value: int | float,
    *,
    aggregation: str,
    comparator: str,
    threshold: int | float,
    sample_count: int,
    unit: str = "count",
) -> dict[str, Any]:
    return {
        "metric": metric,
        "aggregation": aggregation,
        "value": value,
        "unit": unit,
        "sample_count": sample_count,
        "comparator": comparator,
        "threshold": threshold,
    }


def _add_required_measurements(report: dict[str, Any]) -> None:
    by_id = {check["id"]: check for check in report["checks"]}
    if check := by_id.get("runtime_admission_stress"):
        check["measurements"] = [
            _measurement("message_count", 1000, aggregation="total", comparator="gte", threshold=1000, sample_count=1000),
            _measurement("active_limit_violations", 0, aggregation="total", comparator="eq", threshold=0, sample_count=1000),
            _measurement("unresolved_operations", 0, aggregation="total", comparator="eq", threshold=0, sample_count=1000),
        ]
    if check := by_id.get("scheduler_exactly_once"):
        check["measurements"] = [
            _measurement("interleaving_count", 10000, aggregation="total", comparator="gte", threshold=10000, sample_count=10000),
            _measurement("duplicate_effects", 0, aggregation="total", comparator="eq", threshold=0, sample_count=10000),
        ]
    if check := by_id.get("migration_multi_instance"):
        check["measurements"] = [
            _measurement("trial_count", 50, aggregation="total", comparator="gte", threshold=50, sample_count=50),
            _measurement("migration_owner_violations", 0, aggregation="total", comparator="eq", threshold=0, sample_count=50),
        ]
    if check := by_id.get("process_supervision_stress"):
        check["measurements"] = [
            _measurement("trial_count", 100, aggregation="total", comparator="gte", threshold=100, sample_count=100),
            _measurement("residual_processes", 0, aggregation="total", comparator="eq", threshold=0, sample_count=100),
        ]
    if check := by_id.get("reconnect_resume"):
        check["measurements"] = [
            _measurement("trial_count", 20, aggregation="total", comparator="gte", threshold=20, sample_count=20),
            _measurement("resume_success_rate", 100, aggregation="rate", comparator="gte", threshold=100, sample_count=20, unit="percent"),
        ]
    if check := by_id.get("apple_first_login_llm"):
        check["measurements"] = [
            _measurement("trial_count", 30, aggregation="total", comparator="gte", threshold=30, sample_count=30),
            _measurement("acknowledgement_p95_ms", 200, aggregation="p95", comparator="lte", threshold=250, sample_count=30, unit="milliseconds"),
            _measurement("success_within_five_seconds_percent", 100, aggregation="rate", comparator="gte", threshold=95, sample_count=30, unit="percent"),
            _measurement("terminal_max_ms", 9500, aggregation="maximum", comparator="lte", threshold=10000, sample_count=30, unit="milliseconds"),
            _measurement("responsive_interaction_p95_ms", 200, aggregation="p95", comparator="lte", threshold=250, sample_count=30, unit="milliseconds"),
        ]


def _passing_set(contract_examples: Any) -> dict[str, Any]:
    targets = ["backend", "web", "windows", "android", "macos", "ios", "watchos", "docs"]
    evidence = []
    for index, target in enumerate(targets, 1):
        report = contract_examples._platform_evidence(target)
        report["evidence_id"] = f"00000000-0000-4000-8000-{index:012d}"
        _add_required_measurements(report)
        evidence.append(report)
    return {
        "document_type": "release_evidence_set",
        "schema_version": 1,
        "policy_revision": "060-v1",
        "evidence_set_id": "99999999-9999-4999-8999-999999999999",
        "candidate_sha": contract_examples.GIT_SHA,
        "release_id": "release-060-1",
        "release_version": "0.4.0",
        "generated_at": "2026-07-16T12:00:00Z",
        "required_targets": targets,
        "evidence": evidence,
        "exception_requests": [],
        "decision": "passed",
    }


def test_schema_engine_validates_all_three_contracts_and_rejects_unknown_keyword(
    validator: Any, contract_examples: Any
) -> None:
    schemas = [validator.load_json_document(path) for path in sorted(CONTRACT_ROOT.glob("*.schema.json"))]
    assert len(schemas) == 3
    for schema in schemas:
        validator.validate_schema_document(schema)

    validator.validate_document(contract_examples._profile(), schemas[2])
    broken = copy.deepcopy(schemas[0])
    broken["candidateVerdict"] = True
    with pytest.raises(validator.SchemaDefinitionError, match="unsupported schema keyword"):
        validator.validate_schema_document(broken)


def test_schema_engine_exercises_every_supported_assertion_branch(
    validator: Any,
) -> None:
    schema = {
        "$defs": {
            "uuid": {
                "type": "string",
                "minLength": 36,
                "maxLength": 36,
                "pattern": "^[0-9a-f-]+$",
                "format": "uuid",
            }
        },
        "type": "object",
        "required": ["id", "mode", "count", "values", "choice", "forbidden"],
        "additionalProperties": False,
        "properties": {
            "id": {"$ref": "#/$defs/uuid"},
            "mode": {"enum": ["strict", "relaxed"]},
            "count": {"type": "integer", "minimum": 1, "maximum": 2},
            "values": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "uniqueItems": True,
                "items": {"type": "number"},
                "contains": {"const": 2},
            },
            "choice": {"oneOf": [{"const": "left"}, {"const": "right"}]},
            "forbidden": {"not": {"const": "bad"}},
            "flag": {"type": "boolean"},
            "nothing": {"type": "null"},
        },
        "allOf": [{"properties": {"flag": {"const": True}}}],
        "if": {"properties": {"mode": {"const": "strict"}}, "required": ["mode"]},
        "then": {"properties": {"count": {"const": 2}}},
        "else": {"properties": {"count": {"const": 1}}},
    }
    valid = {
        "id": "11111111-1111-4111-8111-111111111111",
        "mode": "strict",
        "count": 2,
        "values": [1, 2],
        "choice": "left",
        "forbidden": "safe",
        "flag": True,
        "nothing": None,
    }
    validator.validate_document(valid, schema)

    mutations = []
    for mutate in (
        lambda doc: doc.pop("id"),
        lambda doc: doc.__setitem__("extra", 1),
        lambda doc: doc.__setitem__("id", "not-a-uuid"),
        lambda doc: doc.__setitem__("mode", "unknown"),
        lambda doc: doc.__setitem__("count", 0),
        lambda doc: doc.__setitem__("count", 3),
        lambda doc: doc.__setitem__("values", []),
        lambda doc: doc.__setitem__("values", [1, 2, 3]),
        lambda doc: doc.__setitem__("values", [2, 2]),
        lambda doc: doc.__setitem__("values", ["2"]),
        lambda doc: doc.__setitem__("values", [1]),
        lambda doc: doc.__setitem__("choice", "middle"),
        lambda doc: doc.__setitem__("forbidden", "bad"),
        lambda doc: doc.__setitem__("flag", False),
    ):
        broken = copy.deepcopy(valid)
        mutate(broken)
        mutations.append(broken)
    relaxed_wrong = copy.deepcopy(valid)
    relaxed_wrong.update(mode="relaxed", count=2)
    mutations.append(relaxed_wrong)
    for broken in mutations:
        with pytest.raises(validator.SchemaValidationError):
            validator.validate_document(broken, schema)

    validator.validate_document("x", {"type": ["string", "null"]})
    validator.validate_document(None, {"type": ["string", "null"]})
    validator.validate_document([], {"type": "array"})
    with pytest.raises(validator.SchemaValidationError):
        validator.validate_document("x", {"minLength": 2})
    with pytest.raises(validator.SchemaValidationError):
        validator.validate_document("xxx", {"maxLength": 2})
    with pytest.raises(validator.SchemaValidationError):
        validator.validate_document("bad", {"pattern": "^good$"})
    with pytest.raises(validator.SchemaValidationError):
        validator.validate_document("2026-07-16", {"format": "date-time"})
    with pytest.raises(validator.SchemaValidationError):
        validator.validate_document("https://bad host", {"format": "uri"})
    with pytest.raises(validator.SchemaValidationError):
        validator.validate_document("x", {"oneOf": [{"type": "string"}, {"const": "x"}]})
    with pytest.raises(validator.SchemaValidationError):
        validator.validate_document({"x": "bad"}, {"type": "object", "additionalProperties": {"type": "integer"}})


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"properties": []}, "must be an object"),
        ({"allOf": {}}, "must be an array"),
        ({"items": []}, "schema node"),
        ({"type": ["string", "string"]}, "type declaration"),
        ({"$ref": "https://example.invalid/schema"}, "only local"),
        ({"required": ["x", "x"]}, "required array"),
        ({"pattern": 1}, "pattern must be"),
        ({"pattern": "["}, "regular expression"),
        ({"format": "hostname"}, "unsupported format"),
        ({"$defs": {}, "$ref": "#/$defs/missing"}, "unresolved"),
    ],
)
def test_schema_definition_rejects_unsupported_or_malformed_constructs(
    validator: Any, schema: dict[str, Any], message: str
) -> None:
    with pytest.raises(validator.SchemaDefinitionError, match=message):
        validator.validate_schema_document(schema)


def test_bounded_json_and_canonicalization_fail_closed(
    validator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(validator.DocumentError, match="size"):
        validator.load_json_bytes(b"")
    with pytest.raises(validator.DocumentError, match="UTF-8"):
        validator.load_json_bytes(b"\xff")
    with pytest.raises(validator.DocumentError, match="invalid JSON"):
        validator.load_json_bytes(b"{")
    scalar = tmp_path / "scalar.json"
    scalar.write_text("1", encoding="utf-8")
    with pytest.raises(validator.DocumentError, match="top-level"):
        validator.load_json_document(scalar)
    with pytest.raises(validator.DocumentError, match="cannot stat"):
        validator.load_json_document(tmp_path / "missing.json")
    with pytest.raises(validator.DocumentError, match="canonicalized"):
        validator.canonical_json_bytes({"not_json": {1, 2}})
    with pytest.raises(validator.DocumentError, match="key is not a string"):
        validator._bound_document({1: "not-json"})
    monkeypatch.setattr(validator, "MAX_NESTING", 2)
    with pytest.raises(validator.DocumentError, match="nesting"):
        validator.load_json_bytes(b'{"a":{"b":{"c":1}}}')
    monkeypatch.setattr(validator, "MAX_COLLECTION_ITEMS", 2)
    with pytest.raises(validator.DocumentError, match="value count"):
        validator.load_json_bytes(b"[1,2,3]")


@pytest.mark.parametrize("payload", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_json_decoder_rejects_duplicate_keys_and_nonfinite_values(
    validator: Any, tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(validator.DocumentError):
        validator.load_json_document(path)


def test_complete_same_candidate_matrix_passes_local_policy(
    validator: Any, contract_examples: Any
) -> None:
    evidence_set = _passing_set(contract_examples)
    validator.validate_document(evidence_set, validator.load_json_document(CONTRACT_ROOT / "release-evidence.schema.json"))
    result = validator.evaluate_evidence_set(
        evidence_set,
        now=datetime(2026, 7, 16, 12, 30, tzinfo=UTC),
    )
    assert result.required_targets == tuple(evidence_set["required_targets"])
    assert result.staging_environment_id == "stage-060-request-1"
    assert result.used_exception_ids == ()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda doc: doc["evidence"].append(copy.deepcopy(doc["evidence"][0])), "duplicate platform"),
        (lambda doc: doc["evidence"][1]["checks"].append(copy.deepcopy(doc["evidence"][1]["checks"][0])), "duplicate check"),
        (lambda doc: doc["evidence"][2].__setitem__("candidate_sha", "b" * 40), "candidate_sha"),
        (lambda doc: doc["evidence"][3]["staging_environment"].__setitem__("environment_id", "other-stage"), "staging"),
        (
            lambda doc: doc["evidence"][3]["staging_environment"]["voice_runtime"][
                "speech_profile"
            ].__setitem__("inventory_sha256", "0" * 64),
            "staging",
        ),
        (lambda doc: doc["evidence"][1]["checks"][0].__setitem__("outcome", "not_applicable"), "not_applicable"),
        (lambda doc: doc["evidence"][0]["checks"][1].__setitem__("measurements", []), "measurement"),
    ],
)
def test_policy_rejects_duplicates_identity_drift_illegal_na_and_missing_metrics(
    validator: Any,
    contract_examples: Any,
    mutation: Any,
    message: str,
) -> None:
    evidence_set = _passing_set(contract_examples)
    mutation(evidence_set)
    with pytest.raises(validator.PolicyError, match=message):
        validator.evaluate_evidence_set(evidence_set, now=datetime(2026, 7, 16, tzinfo=UTC))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda doc: doc.__setitem__("required_targets", []), "required targets"),
        (lambda doc: doc.__setitem__("evidence", {}), "evidence must"),
        (lambda doc: doc["evidence"].pop(), "platform reports"),
        (lambda doc: doc["evidence"][1].__setitem__("evidence_id", doc["evidence"][0]["evidence_id"]), "duplicate evidence"),
        (lambda doc: doc.__setitem__("exception_requests", {}), "exception_requests"),
        (lambda doc: doc["evidence"][0].__setitem__("checks", {}), "checks must"),
        (lambda doc: doc["evidence"][0]["checks"].pop(), "check set"),
        (lambda doc: doc["evidence"][0].__setitem__("staging_environment", None), "qualifying staging"),
        (lambda doc: doc["evidence"][0].__setitem__("outcome", "failed"), "failed product"),
        (lambda doc: doc["evidence"][1]["checks"][0].__setitem__("outcome", "not_run"), "passed web report"),
        (lambda doc: doc["evidence"][0]["staging_environment"].__setitem__("endpoint", "https://localhost"), "credential-free"),
        (lambda doc: doc["evidence"][0]["staging_environment"].__setitem__("worker_paths", ["maintenance"]), "worker paths"),
        (lambda doc: doc["evidence"][0].__setitem__("outcome", "unknown"), "unsupported outcome"),
        (lambda doc: doc.__setitem__("decision", "failed"), "decision='passed'"),
    ],
)
def test_policy_rejects_every_matrix_shape_and_product_failure_class(
    validator: Any,
    contract_examples: Any,
    mutation: Any,
    message: str,
) -> None:
    evidence_set = _passing_set(contract_examples)
    mutation(evidence_set)
    with pytest.raises(validator.PolicyError, match=message):
        validator.evaluate_evidence_set(
            evidence_set, now=datetime(2026, 7, 16, 12, tzinfo=UTC)
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda staging: staging["worker_paths"].remove("voice"),
            "voice worker path",
        ),
        (
            lambda staging: staging["voice_runtime"].__setitem__(
                "voice_worker_image_sha256", "0" * 64
            ),
            "voice worker image",
        ),
        (
            lambda staging: staging["voice_runtime"].__setitem__(
                "livekit_image_sha256", "0" * 64
            ),
            "LiveKit image",
        ),
        (
            lambda staging: staging["voice_runtime"].__setitem__(
                "livekit_image_reference",
                "docker.io/livekit/livekit-server@sha256:"
                "3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1",
            ),
            "approved exact server image",
        ),
        (
            lambda staging: staging["voice_runtime"]["speech_profile"].__setitem__(
                "voice", "af_alloy"
            ),
            "speech profile",
        ),
        (
            lambda staging: staging["voice_runtime"]["speech_profile"].__setitem__(
                "profile_sha256", "0" * 64
            ),
            "speech profile",
        ),
        (
            lambda staging: staging["voice_runtime"].__setitem__(
                "livekit_public_url", "ws://localhost:7880"
            ),
            "public URL",
        ),
        (
            lambda staging: staging["voice_runtime"].__setitem__(
                "livekit_turn_domain", "localhost"
            ),
            "TURN domain",
        ),
    ],
)
def test_canonical_staging_rejects_unbound_voice_runtime_identity(
    validator: Any,
    contract_examples: Any,
    mutation: Any,
    message: str,
) -> None:
    staging = contract_examples._staging_environment()
    mutation(staging)
    with pytest.raises(validator.PolicyError, match=message):
        validator._canonical_staging(staging)


def test_trusted_stage_binds_support_images_and_livekit_public_443_route(
    validator: Any,
    contract_examples: Any,
) -> None:
    staging = contract_examples._staging_environment()
    support_image = "registry.invalid/support@sha256:" + "9" * 64
    staging["service_image_references"] = {
        "postgres": support_image,
        "keycloak-postgres": support_image,
        "keycloak": "registry.invalid/keycloak@sha256:" + "6" * 64,
        "livekit": staging["voice_runtime"]["livekit_image_reference"],
        "schema-baseline": "registry.invalid/baseline@sha256:" + "5" * 64,
        "astraldeep": staging["candidate_image_reference"],
        "voice-worker": staging["voice_runtime"]["voice_worker_image_reference"],
    }
    staging["livekit_turn_tls"] = {
        "advertised_uri": (
            "turns:turn.stage-060.astraldeep.invalid:443?transport=tcp"
        ),
        "public_port": 443,
        "external_tls": True,
        "terminator_upstream_host": "127.0.0.1",
        "terminator_upstream_port": 15349,
        "livekit_listener_port": 5349,
    }
    validator._canonical_staging(staging)

    mutable = copy.deepcopy(staging)
    mutable["service_image_references"]["keycloak"] = "keycloak:latest"
    with pytest.raises(validator.PolicyError, match="incomplete or mutable"):
        validator._canonical_staging(mutable)

    wrong_route = copy.deepcopy(staging)
    wrong_route["livekit_turn_tls"]["public_port"] = 5349
    with pytest.raises(validator.PolicyError, match="public-443 route"):
        validator._canonical_staging(wrong_route)


def _unavailable_windows_set(contract_examples: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence_set = _passing_set(contract_examples)
    report = next(item for item in evidence_set["evidence"] if item["platform"] == "windows")
    report["outcome"] = "unavailable"
    report["unavailable_reason"] = "The independently observed Windows runner was unavailable."
    report["unavailability_observation"] = {
        "observation_id": "77777777-7777-4777-8777-777777777777",
        "failure_class": "runner_unavailable",
        "attempted_workflow": report["workflow"],
        "target_runner_requirement": {
            **{key: report["runner"][key] for key in ("os", "architecture", "runner_image", "runner_environment")},
            "labels": ["windows-latest"],
        },
        "observed_at": "2026-07-16T12:00:00Z",
        "immutable_reference": "bundle://raw/windows-observation.json",
        "sha256": "d" * 64,
    }
    for check in report["checks"]:
        check.update(
            outcome="not_run",
            duration_ms=None,
            detail_code="runner_unavailable",
            applicability_reason=None,
            measurements=[],
            evidence_artifacts=[],
        )
    request = json.loads(
        (FIXTURE_ROOT / "requests/legal/windows-runner-unavailable-a.json").read_text()
    )
    request["candidate_sha"] = evidence_set["candidate_sha"]
    request["release_id"] = evidence_set["release_id"]
    request["missing_checks"] = [check["id"] for check in report["checks"]]
    request["requested_at"] = "2026-07-16T12:00:00Z"
    evidence_set["exception_requests"] = [request]
    return evidence_set, request


def test_available_exception_path_requires_unique_current_unexpired_approval(
    validator: Any, contract_examples: Any
) -> None:
    evidence_set, request = _unavailable_windows_set(contract_examples)
    approval = {
        "exception_id": request["exception_id"],
        "expires_at": "2026-07-17T12:00:00Z",
    }
    result = validator.evaluate_evidence_set(
        evidence_set,
        now=datetime(2026, 7, 16, 13, tzinfo=UTC),
        trusted_approvals=[approval],
    )
    assert result.used_exception_ids == (request["exception_id"],)
    with pytest.raises(validator.PolicyError, match="duplicate trusted approval"):
        validator.evaluate_evidence_set(
            evidence_set,
            now=datetime(2026, 7, 16, 13, tzinfo=UTC),
            trusted_approvals=[approval, approval],
        )
    expired = {**approval, "expires_at": "2026-07-16T12:30:00Z"}
    with pytest.raises(validator.PolicyError, match="expired"):
        validator.evaluate_evidence_set(
            evidence_set,
            now=datetime(2026, 7, 16, 13, tzinfo=UTC),
            trusted_approvals=[expired],
        )

    no_observation, _ = _unavailable_windows_set(contract_examples)
    windows = next(item for item in no_observation["evidence"] if item["platform"] == "windows")
    windows["unavailability_observation"] = None
    with pytest.raises(validator.PolicyError, match="protected observation"):
        validator.evaluate_evidence_set(no_observation, trusted_approvals=[approval])

    mixed, _ = _unavailable_windows_set(contract_examples)
    mixed_windows = next(item for item in mixed["evidence"] if item["platform"] == "windows")
    mixed_windows["checks"][0]["outcome"] = "passed"
    with pytest.raises(validator.PolicyError, match="all be not_run"):
        validator.evaluate_evidence_set(mixed, trusted_approvals=[approval])

    future, future_request = _unavailable_windows_set(contract_examples)
    future_request["requested_at"] = "2026-07-17T13:00:01Z"
    with pytest.raises(validator.PolicyError, match="future-dated"):
        validator.evaluate_evidence_set(
            future,
            now=datetime(2026, 7, 17, 12, 55, tzinfo=UTC),
            trusted_approvals=[{**approval, "expires_at": "2026-07-18T12:00:00Z"}],
        )


def test_duplicate_and_unused_exception_requests_are_rejected(
    validator: Any, contract_examples: Any
) -> None:
    evidence_set = _passing_set(contract_examples)
    request = validator.load_json_document(
        FIXTURE_ROOT / "requests/legal/windows-runner-unavailable-a.json"
    )
    evidence_set["exception_requests"] = [request, copy.deepcopy(request)]
    with pytest.raises(validator.PolicyError, match="duplicate exception request"):
        validator.evaluate_evidence_set(evidence_set)

    evidence_set["exception_requests"] = [request]
    with pytest.raises(validator.PolicyError, match="unused exception"):
        validator.evaluate_evidence_set(evidence_set)

    second = copy.deepcopy(request)
    second["exception_id"] = "22222222-2222-4222-8222-222222222222"
    evidence_set["exception_requests"] = [request, second]
    with pytest.raises(validator.PolicyError, match="multiple exception"):
        validator.evaluate_evidence_set(evidence_set)


def test_supported_macos_host_requires_v2_feature_059_passing_evidence(
    validator: Any, contract_examples: Any
) -> None:
    evidence_set = _passing_set(contract_examples)
    for report in evidence_set["evidence"]:
        if report["platform"] != "docs":
            report["staging_environment"]["macos_personal_agent_host"] = {
                "supported": True,
                "runtime_contract_versions": [1],
                "source_feature": "059",
                "source": "candidate_capability_map",
                "manifest_sha256": "e" * 64,
            }
        if report["platform"] == "macos":
            host_check = next(
                check for check in report["checks"] if check["id"] == "macos_personal_agent_host"
            )
            host_check.update(outcome="passed", applicability_reason=None)
    with pytest.raises(validator.PolicyError, match="v2 acknowledged"):
        validator.evaluate_evidence_set(evidence_set)


def test_measurement_semantics_reject_duplicates_noncanonical_and_missed_thresholds(
    validator: Any, contract_examples: Any
) -> None:
    evidence_set = _passing_set(contract_examples)
    backend = evidence_set["evidence"][0]
    check = next(item for item in backend["checks"] if item["id"] == "runtime_admission_stress")
    check["measurements"].append(copy.deepcopy(check["measurements"][0]))
    with pytest.raises(validator.PolicyError, match="duplicate measurement"):
        validator.evaluate_evidence_set(evidence_set)
    check["measurements"].pop()
    check["measurements"][0]["aggregation"] = "exact"
    with pytest.raises(validator.PolicyError, match="noncanonical semantics"):
        validator.evaluate_evidence_set(evidence_set)
    check["measurements"][0]["aggregation"] = "total"
    check["measurements"][0]["value"] = 999
    with pytest.raises(validator.PolicyError, match="misses its threshold"):
        validator.evaluate_evidence_set(evidence_set)


def test_unavailable_platform_needs_exact_current_request_and_protected_receipt(
    validator: Any, contract_examples: Any
) -> None:
    evidence_set = _passing_set(contract_examples)
    windows = next(item for item in evidence_set["evidence"] if item["platform"] == "windows")
    windows["outcome"] = "unavailable"
    windows["unavailable_reason"] = "The hosted Windows runner was independently unavailable."
    windows["unavailability_observation"] = {
        "observation_id": "77777777-7777-4777-8777-777777777777",
        "failure_class": "runner_unavailable",
        "attempted_workflow": windows["workflow"],
        "target_runner_requirement": {
            **{key: windows["runner"][key] for key in ("os", "architecture", "runner_image", "runner_environment")},
            "labels": ["windows-latest"],
        },
        "observed_at": "2026-07-16T12:00:00Z",
        "immutable_reference": "bundle://raw/windows-runner-observation.json",
        "sha256": "d" * 64,
    }
    for check in windows["checks"]:
        check.update(
            outcome="not_run",
            duration_ms=None,
            detail_code="runner_unavailable",
            applicability_reason=None,
            measurements=[],
            evidence_artifacts=[],
        )

    with pytest.raises(validator.PolicyError, match="exception request"):
        validator.evaluate_evidence_set(evidence_set, now=datetime(2026, 7, 16, tzinfo=UTC))

    request = json.loads((FIXTURE_ROOT / "requests/legal/windows-runner-unavailable-a.json").read_text())
    request["candidate_sha"] = evidence_set["candidate_sha"]
    request["release_id"] = evidence_set["release_id"]
    request["missing_checks"] = [check["id"] for check in windows["checks"]]
    request["requested_at"] = "2026-07-16T12:00:00Z"
    evidence_set["exception_requests"] = [request]
    with pytest.raises(validator.PolicyError, match="trusted approval"):
        validator.evaluate_evidence_set(
            evidence_set, now=datetime(2026, 7, 16, 13, tzinfo=UTC)
        )


def test_apple_first_login_cannot_be_waived(
    validator: Any, contract_examples: Any
) -> None:
    evidence_set = _passing_set(contract_examples)
    ios = next(item for item in evidence_set["evidence"] if item["platform"] == "ios")
    check = next(item for item in ios["checks"] if item["id"] == "apple_first_login_llm")
    check["outcome"] = "not_run"
    ios["outcome"] = "unavailable"
    ios["unavailable_reason"] = "The target platform was unavailable before product execution."
    ios["unavailability_observation"] = {
        "observation_id": "88888888-8888-4888-8888-888888888888",
        "failure_class": "platform_unavailable",
        "attempted_workflow": ios["workflow"],
        "target_runner_requirement": {
            **{key: ios["runner"][key] for key in ("os", "architecture", "runner_image", "runner_environment")},
            "labels": ["macos-26"],
        },
        "observed_at": "2026-07-16T12:00:00Z",
        "immutable_reference": "bundle://raw/ios-observation.json",
        "sha256": "d" * 64,
    }
    for item in ios["checks"]:
        item.update(outcome="not_run", duration_ms=None, detail_code="platform_unavailable", applicability_reason=None, measurements=[], evidence_artifacts=[])
    with pytest.raises(validator.PolicyError, match="non-waivable"):
        validator.evaluate_evidence_set(evidence_set, now=datetime(2026, 7, 16, tzinfo=UTC))


def test_fixed_field_approval_payload_hash_matches_tracked_fixture(validator: Any) -> None:
    receipt = validator.load_json_document(FIXTURE_ROOT / "receipts/approval-registration-a.json")
    assert validator.exception_approval_payload_sha256(receipt) == receipt["approval_payload_sha256"]
    mutated = copy.deepcopy(receipt)
    mutated["exception_artifact"]["artifact_id"] = "9999"
    assert validator.exception_approval_payload_sha256(mutated) != receipt["approval_payload_sha256"]
    with pytest.raises(validator.ProvenanceError, match="exception_artifact"):
        validator.exception_approval_payload_sha256({})


def test_protected_exception_approval_binds_request_bytes_and_exact_debt_snapshot(
    validator: Any, tmp_path: Path
) -> None:
    request_path = FIXTURE_ROOT / "requests/legal/windows-runner-unavailable-a.json"
    request = validator.load_json_document(request_path)
    receipt = validator.load_json_document(FIXTURE_ROOT / "receipts/approval-registration-a.json")
    reference = receipt["exception_artifact"]["immutable_reference"]
    resolver = validator.ArtifactResolver(
        bundle_root=tmp_path,
        resolved={reference: request_path},
    )
    ledger_path = receipt["ledger_entry_path"]
    ledger = validator.LedgerSnapshot(
        repository=receipt["ledger_repository"],
        ref=receipt["ledger_ref"],
        commit_sha=receipt["ledger_commit_sha"],
        tree_sha="a" * 40,
        snapshot_sha256="b" * 64,
        paths={ledger_path: receipt["ledger_entry_sha256"]},
        records={ledger_path: receipt["ledger_entry"]},
    )
    validator.validate_exception_approval(
        request,
        receipt,
        now=datetime(2026, 7, 16, tzinfo=UTC),
        resolver=resolver,
        ledger=ledger,
    )
    self_approved = copy.deepcopy(receipt)
    self_approved["reviewer_login"] = self_approved["requester_login"]
    with pytest.raises(validator.ProvenanceError, match="own request"):
        validator.validate_exception_approval(
            request,
            self_approved,
            now=datetime(2026, 7, 16, tzinfo=UTC),
            resolver=resolver,
            ledger=ledger,
        )

    mutations = []
    mismatched = copy.deepcopy(receipt)
    mismatched["candidate_sha"] = "b" * 40
    mutations.append((mismatched, ledger))
    expired = copy.deepcopy(receipt)
    expired["expires_at"] = expired["approved_at"]
    mutations.append((expired, ledger))
    missing_artifact = copy.deepcopy(receipt)
    missing_artifact["exception_artifact"] = None
    mutations.append((missing_artifact, ledger))
    wrong_payload = copy.deepcopy(receipt)
    wrong_payload["approval_payload_sha256"] = "0" * 64
    mutations.append((wrong_payload, ledger))
    wrong_path = copy.deepcopy(receipt)
    wrong_path["ledger_entry_path"] = "debts/wrong.json"
    mutations.append((wrong_path, ledger))
    absent_debt = copy.deepcopy(receipt)
    absent_debt["ledger_entry_sha256"] = "0" * 64
    mutations.append((absent_debt, ledger))
    wrong_reference = copy.deepcopy(receipt)
    wrong_reference["ledger_entry_immutable_reference"] = "ghgit://wrong"
    mutations.append((wrong_reference, ledger))
    for broken, snapshot in mutations:
        with pytest.raises(validator.ProvenanceError):
            validator.validate_exception_approval(
                request,
                broken,
                now=datetime(2026, 7, 16, tzinfo=UTC),
                resolver=resolver,
                ledger=snapshot,
            )

    wrong_debt = copy.deepcopy(receipt)
    wrong_debt["ledger_entry"]["platform"] = "android"
    wrong_debt_ledger = validator.LedgerSnapshot(
        repository=ledger.repository,
        ref=ledger.ref,
        commit_sha=ledger.commit_sha,
        tree_sha=ledger.tree_sha,
        snapshot_sha256=ledger.snapshot_sha256,
        paths=ledger.paths,
        records={ledger_path: wrong_debt["ledger_entry"]},
    )
    with pytest.raises(validator.ProvenanceError, match="registered debt platform"):
        validator.validate_exception_approval(
            request,
            wrong_debt,
            now=datetime(2026, 7, 16, tzinfo=UTC),
            resolver=resolver,
            ledger=wrong_debt_ledger,
        )

    wrong_reviewer = copy.deepcopy(receipt)
    wrong_reviewer["ledger_entry"]["reviewer_login"] = "another-owner"
    wrong_reviewer_ledger = validator.LedgerSnapshot(
        repository=ledger.repository,
        ref=ledger.ref,
        commit_sha=ledger.commit_sha,
        tree_sha=ledger.tree_sha,
        snapshot_sha256=ledger.snapshot_sha256,
        paths=ledger.paths,
        records={ledger_path: wrong_reviewer["ledger_entry"]},
    )
    with pytest.raises(validator.ProvenanceError, match="reviewer"):
        validator.validate_exception_approval(
            request,
            wrong_reviewer,
            now=datetime(2026, 7, 16, tzinfo=UTC),
            resolver=resolver,
            ledger=wrong_reviewer_ledger,
        )


def test_exception_history_requires_one_trusted_complete_resolution(
    validator: Any, contract_examples: Any
) -> None:
    debt_path = "debts/11111111-1111-4111-8111-111111111111.json"
    resolution_path = "resolutions/33333333-3333-4333-8333-333333333333.json"
    debt_file = FIXTURE_ROOT / "history/01-debt-a" / debt_path
    resolution_file = FIXTURE_ROOT / "history/02-resolution-a" / resolution_path
    debt = validator.load_json_document(debt_file)
    resolution = validator.load_json_document(resolution_file)
    receipt = validator.load_json_document(FIXTURE_ROOT / "receipts/debt-resolution-a.json")
    ledger = validator.LedgerSnapshot(
        repository="AstralDeep/AstralDeep",
        ref="refs/heads/release-evidence-debt",
        commit_sha="c" * 40,
        tree_sha="d" * 40,
        snapshot_sha256="e" * 64,
        paths={
            debt_path: hashlib.sha256(debt_file.read_bytes()).hexdigest(),
            resolution_path: hashlib.sha256(resolution_file.read_bytes()).hexdigest(),
        },
        records={debt_path: debt, resolution_path: resolution},
    )
    validator.validate_exception_history(
        ledger,
        evidence_set=_passing_set(contract_examples),
        approval_receipts=[],
        resolution_receipts=[receipt],
    )
    without_receipt = copy.deepcopy(receipt)
    without_receipt["resolution_id"] = "99999999-9999-4999-8999-999999999999"
    with pytest.raises(validator.LedgerError, match="trusted receipt"):
        validator.validate_exception_history(
            ledger,
            evidence_set=_passing_set(contract_examples),
            approval_receipts=[],
            resolution_receipts=[without_receipt],
        )


def test_windows_draft_provenance_rehashes_the_build_once_three_asset_lineage(
    validator: Any, tmp_path: Path
) -> None:
    executable = tmp_path / "AstralDeep.exe"
    executable.write_bytes(b"frozen-build-once-executable")
    executable_sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(f"{executable_sha}  AstralDeep.exe\n", encoding="utf-8")
    bundle = tmp_path / "cosign.bundle"
    bundle.write_bytes(b"detached-synthetic-bundle")
    refs = {
        "gh://AstralDeep/AstralDeep/releases/10/assets/11": executable,
        "gh://AstralDeep/AstralDeep/releases/10/assets/12": checksums,
        "gh://AstralDeep/AstralDeep/releases/10/assets/13": bundle,
    }
    resolver = validator.ArtifactResolver(bundle_root=tmp_path, resolved=refs)
    artifact = {
        "name": "AstralDeep.exe",
        "kind": "windows_exe",
        "immutable_reference": "gh://AstralDeep/AstralDeep/releases/10/assets/11",
        "sha256": executable_sha,
        "build_identity": "windows-build-once-6001",
    }
    decision = {
        "candidate_sha": "a" * 40,
        "release_id": "release-060-a",
        "readiness_evidence_set_id": "99999999-9999-4999-8999-999999999999",
    }
    document = {
        **decision,
        "release_version": "0.4.0",
        "trusted_decision": {"valid_until": "2026-07-17T00:00:00Z"},
        "protected_publisher": {
            "reviewer_login": "release-owner",
            "requester_login": "release-requester",
            "bridge_workflow_sha256": "f" * 64,
        },
        "signing": {"bridge_workflow_sha256": "f" * 64},
        "tested_executable": dict(artifact),
        "draft_executable": dict(artifact),
        "draft_checksum_manifest": {
            "immutable_reference": "gh://AstralDeep/AstralDeep/releases/10/assets/12",
            "sha256": hashlib.sha256(checksums.read_bytes()).hexdigest(),
        },
        "draft_signature_bundle": {
            "immutable_reference": "gh://AstralDeep/AstralDeep/releases/10/assets/13",
            "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        },
        "draft_release": {
            "tag": "v0.4.0",
            "release_name": "v0.4.0",
            "executable_asset_database_id": 11,
            "checksum_asset_database_id": 12,
            "signature_bundle_asset_database_id": 13,
        },
    }
    validator.validate_windows_draft_provenance(
        document,
        trusted_decision=decision,
        now=datetime(2026, 7, 16, tzinfo=UTC),
        resolver=resolver,
    )
    rebuilt = copy.deepcopy(document)
    rebuilt["draft_executable"]["build_identity"] = "rebuilt"
    with pytest.raises(validator.PolicyError, match="rebuilt"):
        validator.validate_windows_draft_provenance(
            rebuilt,
            trusted_decision=decision,
            now=datetime(2026, 7, 16, tzinfo=UTC),
            resolver=resolver,
        )


def test_bundle_resolver_rehashes_bytes_and_rejects_traversal_and_symlinks(
    validator: Any, tmp_path: Path
) -> None:
    root = tmp_path / "evidence"
    raw = root / "raw"
    raw.mkdir(parents=True)
    evidence = raw / "metrics.json"
    evidence.write_bytes(b'{"ok":true}\n')
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    resolver = validator.ArtifactResolver(bundle_root=root)
    assert resolver.resolve("bundle://raw/metrics.json", digest) == evidence.read_bytes()
    with pytest.raises(validator.ProvenanceError, match="traversal"):
        resolver.resolve("bundle://raw/%2e%2e/secret", digest)
    link = raw / "link.json"
    try:
        link.symlink_to(evidence)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(validator.ProvenanceError, match="symlink"):
        resolver.resolve("bundle://raw/link.json", digest)

    with pytest.raises(validator.ProvenanceError, match="malformed"):
        resolver.resolve("bundle://raw/metrics.json", "bad")
    with pytest.raises(validator.ProvenanceError, match="mutable or unknown"):
        resolver.resolve("https://example.invalid/metrics.json", digest)
    with pytest.raises(validator.ProvenanceError, match="pre-fetched"):
        resolver.resolve(
            "gh://AstralDeep/AstralDeep/releases/1/assets/2",
            digest,
        )
    with pytest.raises(validator.ProvenanceError, match="traversal"):
        resolver.resolve(
            "gh://AstralDeep/AstralDeep/runs/1/attempts/1/artifacts/2/"
            "members/raw/%2e%2e/secret.json",
            digest,
        )
    with pytest.raises(validator.ProvenanceError, match="repository path"):
        resolver.resolve(
            "oci://ghcr.io/astraldeep//astraldeep@sha256:" + "1" * 64,
            digest,
        )
    with pytest.raises(validator.ProvenanceError, match="ledger reference"):
        resolver.resolve(
            "ghgit://AstralDeep/AstralDeep/commits/" + "1" * 40 + "/paths/README.md",
            digest,
        )
    with pytest.raises(validator.ProvenanceError, match="digest mismatch"):
        resolver.resolve("bundle://raw/metrics.json", "0" * 64)

    oci_reference = "oci://ghcr.io/astraldeep/astraldeep@sha256:" + "1" * 64
    ledger_reference = "ghgit://AstralDeep/AstralDeep/commits/" + "2" * 40
    immutable = validator.ArtifactResolver(
        bundle_root=root,
        resolved={oci_reference: evidence, ledger_reference: evidence},
    )
    assert immutable.resolve(oci_reference, digest) == evidence.read_bytes()
    assert immutable.resolve(ledger_reference, digest) == evidence.read_bytes()

    external_link = tmp_path / "prefetched-link.json"
    try:
        external_link.symlink_to(evidence)
    except OSError:
        pytest.skip("symlinks are unavailable")
    prefetch = validator.ArtifactResolver(
        bundle_root=root,
        resolved={
            "oci://ghcr.io/astraldeep/astraldeep@sha256:" + "1" * 64: external_link
        },
    )
    with pytest.raises(validator.ProvenanceError, match="crosses a symlink"):
        prefetch.resolve(
            "oci://ghcr.io/astraldeep/astraldeep@sha256:" + "1" * 64,
            digest,
        )


@pytest.mark.parametrize(
    "value",
    ["2026-07-16T12:00:00z", "not-a-time", "2026-07-16T12:00:00"],
)
def test_timestamp_parser_rejects_noncanonical_invalid_and_naive_values(
    validator: Any, value: str
) -> None:
    with pytest.raises(validator.PolicyError):
        validator._parse_timestamp(value, field="observed_at")
    with pytest.raises(validator.SchemaDefinitionError, match="unsupported format"):
        validator._format_matches("value", "unsupported")


@pytest.mark.parametrize("platform", ["backend", "web"])
def test_oci_producer_binding_uses_the_attested_stage_product_identity(
    validator: Any,
    contract_examples: Any,
    tmp_path: Path,
    platform: str,
) -> None:
    report = contract_examples._platform_evidence(platform)
    report["workflow"] = contract_examples._workflow(f"{platform}-producer")
    report["artifact"] = {
        "name": f"astraldeep-{platform}-candidate",
        "kind": "container" if platform == "backend" else "web_deployment",
        "immutable_reference": (
            "oci://" + report["staging_environment"]["candidate_image_reference"]
        ),
        "sha256": report["staging_environment"]["candidate_image_sha256"],
        "build_identity": f"candidate-container:{contract_examples.GIT_SHA}",
    }
    manifest = contract_examples._trusted_workflow_provenance()
    manifest["workflow"] = copy.deepcopy(report["workflow"])
    manifest["runner"] = copy.deepcopy(report["runner"])
    report_bytes = (
        json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    )
    report_path = tmp_path / f"{platform}.json"
    report_path.write_bytes(report_bytes)
    member = contract_examples._trusted_member()
    member.update(
        artifact_id="701",
        artifact_name=f"evidence-{platform}-12345-1",
        member=f"{platform}.json",
        immutable_reference=(
            "gh://AstralDeep/AstralDeep/runs/12345/attempts/1/"
            f"artifacts/701/members/{platform}.json"
        ),
        sha256=hashlib.sha256(report_bytes).hexdigest(),
    )
    image_reference = report["staging_environment"]["candidate_image_reference"]
    registry_repository, digest = image_reference.rsplit("@", 1)
    registry, repository_path = registry_repository.split("/", 1)
    manifest["artifacts"] = [
        member,
        {
            "kind": "oci_manifest",
            "registry": registry,
            "repository_path": repository_path,
            "digest": digest,
            "immutable_reference": f"oci://{image_reference}",
            "sha256": digest.removeprefix("sha256:"),
        },
    ]
    stage = {"deployment": copy.deepcopy(report["staging_environment"])}
    validator.bind_report_to_producer(
        report,
        [manifest],
        repository="AstralDeep/AstralDeep",
        candidate_sha=contract_examples.GIT_SHA,
        protected_builder_sha=contract_examples.OTHER_GIT_SHA,
        protected_builder_identity="https://github.com/AstralDeep/AstralDeep",
        trusted_stage_deploy=stage,
        report_artifact_sha256=hashlib.sha256(report_bytes).hexdigest(),
    )

    spoofed_stage = copy.deepcopy(stage)
    spoofed_stage["deployment"]["candidate_image_sha256"] = "0" * 64
    with pytest.raises(validator.ProvenanceError, match="producer|stage"):
        validator.bind_report_to_producer(
            report,
            [manifest],
            repository="AstralDeep/AstralDeep",
            candidate_sha=contract_examples.GIT_SHA,
            protected_builder_sha=contract_examples.OTHER_GIT_SHA,
            protected_builder_identity="https://github.com/AstralDeep/AstralDeep",
            trusted_stage_deploy=spoofed_stage,
            report_artifact_sha256=hashlib.sha256(report_bytes).hexdigest(),
        )

    with pytest.raises(validator.ProvenanceError, match="producer|report"):
        validator.bind_report_to_producer(
            report,
            [manifest],
            repository="AstralDeep/AstralDeep",
            candidate_sha=contract_examples.GIT_SHA,
            protected_builder_sha=contract_examples.OTHER_GIT_SHA,
            protected_builder_identity="https://github.com/AstralDeep/AstralDeep",
            trusted_stage_deploy=stage,
            report_artifact_sha256="0" * 64,
        )


def test_windows_producer_binding_rehashes_unsigned_product_source(
    validator: Any,
    contract_examples: Any,
    tmp_path: Path,
) -> None:
    report = contract_examples._platform_evidence("windows")
    report["workflow"] = contract_examples._workflow("windows-producer")
    product = b"unsigned AstralDeep executable"
    report["artifact"].update(
        name="AstralDeep.exe",
        immutable_reference=(
            "gh://AstralDeep/AstralDeep/runs/12345/attempts/1/"
            "artifacts/703/members/AstralDeep.exe"
        ),
        sha256=hashlib.sha256(product).hexdigest(),
    )
    report_bytes = (
        json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    )

    def member(path: str, content: bytes, *, artifact_id: str, name: str) -> dict[str, Any]:
        claim = contract_examples._trusted_member()
        claim.update(
            artifact_id=artifact_id,
            artifact_name=name,
            member=path,
            immutable_reference=(
                "gh://AstralDeep/AstralDeep/runs/12345/attempts/1/"
                f"artifacts/{artifact_id}/members/{path}"
            ),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        return claim

    final_name = "evidence-windows-12345-1"
    source_name = (
        f"windows-unsigned-{contract_examples.GIT_SHA}-12345-1"
    )
    manifest = contract_examples._trusted_workflow_provenance()
    manifest["workflow"] = copy.deepcopy(report["workflow"])
    manifest["runner"] = copy.deepcopy(report["runner"])
    manifest["artifacts"] = [
        member("windows.json", report_bytes, artifact_id="702", name=final_name)
    ]
    source_payloads = {
        "AstralDeep.exe": product,
        "candidate-manifest.json": b"{}\n",
    }
    manifest["source_provenance"] = {
        "workflow": contract_examples._workflow("windows-candidate"),
        "runner": contract_examples._runner("windows"),
        "artifacts": [
            member(path, content, artifact_id="703", name=source_name)
            for path, content in source_payloads.items()
        ],
    }
    source_root = tmp_path / "windows-product"
    source_root.mkdir()
    for path, content in source_payloads.items():
        (source_root / path).write_bytes(content)

    assert validator.bind_report_to_producer(
        report,
        [manifest],
        repository="AstralDeep/AstralDeep",
        candidate_sha=contract_examples.GIT_SHA,
        protected_builder_sha=contract_examples.OTHER_GIT_SHA,
        protected_builder_identity="https://github.com/AstralDeep/AstralDeep",
        windows_product_source_root=source_root,
        report_artifact_sha256=hashlib.sha256(report_bytes).hexdigest(),
    ) is manifest

    (source_root / "AstralDeep.exe").write_bytes(b"mutated")
    with pytest.raises(validator.ProvenanceError, match="digest differs"):
        validator.bind_report_to_producer(
            report,
            [manifest],
            repository="AstralDeep/AstralDeep",
            candidate_sha=contract_examples.GIT_SHA,
            protected_builder_sha=contract_examples.OTHER_GIT_SHA,
            protected_builder_identity="https://github.com/AstralDeep/AstralDeep",
            windows_product_source_root=source_root,
            report_artifact_sha256=hashlib.sha256(report_bytes).hexdigest(),
        )

    (source_root / "AstralDeep.exe").write_bytes(product)
    (source_root / "unexpected.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(validator.ProvenanceError, match="members differ"):
        validator.bind_report_to_producer(
            report,
            [manifest],
            repository="AstralDeep/AstralDeep",
            candidate_sha=contract_examples.GIT_SHA,
            protected_builder_sha=contract_examples.OTHER_GIT_SHA,
            protected_builder_identity="https://github.com/AstralDeep/AstralDeep",
            windows_product_source_root=source_root,
            report_artifact_sha256=hashlib.sha256(report_bytes).hexdigest(),
        )


def test_apple_producer_binding_preserves_and_rehashes_raw_origin_chain(
    validator: Any,
    contract_examples: Any,
    tmp_path: Path,
) -> None:
    report = contract_examples._platform_evidence("ios")
    report["workflow"]["job_id"] = "ios-raw-producer"
    payloads = {
        "ios.json": b"raw platform report bytes\n",
        "artifacts/client.bin": b"tested app bytes",
        "raw/metrics.json": b"raw metric bytes",
        "coverage/raw/apple-ios.xcresult/Data/coverage.data": b"xccov archive bytes",
    }
    report["artifact"]["sha256"] = hashlib.sha256(
        payloads["artifacts/client.bin"]
    ).hexdigest()
    report["checks"][0]["evidence_artifacts"][0]["sha256"] = hashlib.sha256(
        payloads["raw/metrics.json"]
    ).hexdigest()
    payloads["ios.json"] = (
        json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    )

    def member(
        path: str,
        content: bytes,
        *,
        artifact_id: str,
        artifact_name: str,
    ) -> dict[str, Any]:
        return {
            "kind": "github_actions_artifact_member",
            "repository": "AstralDeep/AstralDeep",
            "run_id": "12345",
            "run_attempt": 1,
            "artifact_id": artifact_id,
            "artifact_name": artifact_name,
            "member": path,
            "immutable_reference": (
                "gh://AstralDeep/AstralDeep/runs/12345/attempts/1/"
                f"artifacts/{artifact_id}/members/{path}"
            ),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    raw_name = "raw-apple-evidence-ios-12345-1"
    final_name = "evidence-ios-12345-1"
    source_artifacts = [
        member(path, content, artifact_id="701", artifact_name=raw_name)
        for path, content in payloads.items()
    ]
    final_payloads = {
        key: value
        for key, value in payloads.items()
        if not key.startswith("coverage/raw/")
    }
    final_payloads["coverage/apple-ios-xccov.json"] = b"{}\n"
    final_artifacts = [
        member(path, content, artifact_id="702", artifact_name=final_name)
        for path, content in final_payloads.items()
    ]
    manifest = contract_examples._trusted_workflow_provenance()
    manifest["workflow"] = contract_examples._workflow("ios-producer")
    manifest["runner"] = contract_examples._runner("macos")
    manifest["artifacts"] = final_artifacts
    manifest["source_provenance"] = {
        "workflow": copy.deepcopy(report["workflow"]),
        "runner": copy.deepcopy(report["runner"]),
        "artifacts": source_artifacts,
    }
    source_root = tmp_path / "ios"
    for path, content in payloads.items():
        target = source_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    bound = validator.bind_report_to_producer(
        report,
        [manifest],
        repository="AstralDeep/AstralDeep",
        candidate_sha=contract_examples.GIT_SHA,
        protected_builder_sha=contract_examples.OTHER_GIT_SHA,
        protected_builder_identity="https://github.com/AstralDeep/AstralDeep",
        raw_apple_source_root=source_root,
    )
    assert bound is manifest

    mutated = copy.deepcopy(manifest)
    mutated["source_provenance"]["artifacts"][-1]["sha256"] = "0" * 64
    with pytest.raises(validator.ProvenanceError, match="differ|bytes"):
        validator.bind_report_to_producer(
            report,
            [mutated],
            repository="AstralDeep/AstralDeep",
            candidate_sha=contract_examples.GIT_SHA,
            protected_builder_sha=contract_examples.OTHER_GIT_SHA,
            protected_builder_identity="https://github.com/AstralDeep/AstralDeep",
            raw_apple_source_root=source_root,
        )

    (source_root / "raw" / "extra.json").write_text("extra", encoding="utf-8")
    with pytest.raises(validator.ProvenanceError, match="members differ"):
        validator.bind_report_to_producer(
            report,
            [manifest],
            repository="AstralDeep/AstralDeep",
            candidate_sha=contract_examples.GIT_SHA,
            protected_builder_sha=contract_examples.OTHER_GIT_SHA,
            protected_builder_identity="https://github.com/AstralDeep/AstralDeep",
            raw_apple_source_root=source_root,
        )


def test_source_provenance_is_forbidden_without_an_upstream_product_boundary(
    validator: Any, contract_examples: Any
) -> None:
    report = contract_examples._platform_evidence("android")
    report["workflow"] = contract_examples._workflow("android-producer")
    manifest = contract_examples._trusted_workflow_provenance()
    manifest["workflow"] = copy.deepcopy(report["workflow"])
    manifest["runner"] = copy.deepcopy(report["runner"])
    product_claim = contract_examples._trusted_member()
    product_claim.update(
        member="artifacts/client.bin",
        immutable_reference=(
            "gh://AstralDeep/AstralDeep/runs/12345/attempts/1/"
            "artifacts/67890/members/artifacts/client.bin"
        ),
        sha256=report["artifact"]["sha256"],
    )
    manifest["artifacts"] = [product_claim]
    manifest["source_provenance"] = {
        "workflow": copy.deepcopy(report["workflow"]),
        "runner": copy.deepcopy(report["runner"]),
        "artifacts": [contract_examples._trusted_member()],
    }
    with pytest.raises(validator.ProvenanceError, match="producer"):
        validator.bind_report_to_producer(
            report,
            [manifest],
            repository="AstralDeep/AstralDeep",
            candidate_sha=contract_examples.GIT_SHA,
            protected_builder_sha=contract_examples.OTHER_GIT_SHA,
            protected_builder_identity="https://github.com/AstralDeep/AstralDeep",
        )


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    return completed.stdout.strip()


def test_ledger_reader_binds_exact_commit_tree_snapshot_and_rejects_moved_head(
    validator: Any, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    _git(ledger, "init", "-q")
    _git(ledger, "config", "user.email", "fixture@example.invalid")
    _git(ledger, "config", "user.name", "Fixture")
    debt_source = FIXTURE_ROOT / "history/01-debt-a/debts/11111111-1111-4111-8111-111111111111.json"
    debt_target = ledger / "debts" / debt_source.name
    debt_target.parent.mkdir()
    debt_target.write_bytes(debt_source.read_bytes())
    _git(ledger, "add", ".")
    _git(ledger, "commit", "-qm", "debt")
    commit = _git(ledger, "rev-parse", "HEAD")
    tree = _git(ledger, "rev-parse", "HEAD^{tree}")
    snapshot = validator.read_ledger_snapshot(
        ledger,
        repository="AstralDeep/AstralDeep",
        ref="HEAD",
        commit=commit,
    )
    assert snapshot.commit_sha == commit
    assert snapshot.tree_sha == tree
    assert snapshot.paths == {f"debts/{debt_source.name}": hashlib.sha256(debt_source.read_bytes()).hexdigest()}

    (ledger / "README").write_text("moved\n", encoding="utf-8")
    _git(ledger, "add", ".")
    _git(ledger, "commit", "-qm", "move")
    with pytest.raises(validator.LedgerError, match="does not equal"):
        validator.read_ledger_snapshot(
            ledger,
            repository="AstralDeep/AstralDeep",
            ref="HEAD",
            commit=commit,
        )


def _trusted_member(
    *, artifact_id: str, member: str, sha256: str
) -> dict[str, Any]:
    return {
        "kind": "github_actions_artifact_member",
        "repository": "AstralDeep/AstralDeep",
        "run_id": "6001",
        "run_attempt": 1,
        "artifact_id": artifact_id,
        "artifact_name": f"artifact-{artifact_id}",
        "member": member,
        "immutable_reference": (
            "gh://AstralDeep/AstralDeep/runs/6001/attempts/1/artifacts/"
            f"{artifact_id}/members/{member}"
        ),
        "sha256": sha256,
    }


def test_loader_helpers_and_attestation_receipts_are_exact_and_bounded(
    validator: Any,
    contract_examples: Any,
    tmp_path: Path,
) -> None:
    document_dir = tmp_path / "documents"
    document_dir.mkdir()
    (document_dir / "one.json").write_text('{"value":1}\n', encoding="utf-8")
    assert validator._load_json_directory(None) == []
    assert validator._load_json_directory(document_dir) == [{"value": 1}]
    with pytest.raises(validator.DocumentError, match="does not exist"):
        validator._load_json_directory(tmp_path / "missing")

    artifact_bytes = tmp_path / "artifact.bin"
    artifact_bytes.write_bytes(b"immutable bytes")
    resolved_manifest = tmp_path / "resolved.json"
    resolved_manifest.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "immutable_reference": "gh://AstralDeep/AstralDeep/releases/1/assets/2",
                        "path": "artifact.bin",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert validator._load_resolved_map(None) == {}
    assert validator._load_resolved_map(resolved_manifest) == {
        "gh://AstralDeep/AstralDeep/releases/1/assets/2": artifact_bytes.resolve()
    }
    malformed = tmp_path / "malformed-resolved.json"
    malformed.write_text('{"artifacts":{}}', encoding="utf-8")
    with pytest.raises(validator.DocumentError, match="artifacts array"):
        validator._load_resolved_map(malformed)
    traversal = tmp_path / "traversal-resolved.json"
    traversal.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "immutable_reference": "gh://AstralDeep/AstralDeep/releases/1/assets/3",
                        "path": "../artifact.bin",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(validator.DocumentError, match="traversal"):
        validator._load_resolved_map(traversal)

    output = tmp_path / "nested" / "decision.json"
    validator._atomic_json(output, {"passed": True})
    assert validator.load_json_document(output) == {"passed": True}
    assert validator._parser().parse_args(
        [
            "--schema", "schema.json",
            "--trust-schema", "trust.json",
            "--deployment-profile-schema", "profile.json",
            "--evidence-dir", "evidence",
            "--base-sha", "b" * 40,
            "--candidate-sha", "a" * 40,
            "--trusted-provenance-dir", "provenance",
            "--trusted-stage-deploy", "stage.json",
            "--trusted-approvals-dir", "approvals",
            "--trusted-debt-resolutions-dir", "resolutions",
            "--attestation-verification-dir", "attestations",
            "--protected-builder-sha", "f" * 40,
            "--protected-builder-identity", "protected",
            "--protected-policy-sha", "e" * 64,
            "--exception-ledger-repository", "AstralDeep/AstralDeep",
            "--exception-ledger-commit", "d" * 40,
        ]
    ).candidate_sha == "a" * 40

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    evidence_set = _passing_set(contract_examples)
    (evidence_dir / "set.json").write_text(json.dumps(evidence_set), encoding="utf-8")
    (evidence_dir / "ignored.json").write_text('{"document_type":"not_release_input"}', encoding="utf-8")
    schema = validator.load_json_document(CONTRACT_ROOT / "release-evidence.schema.json")
    loaded_set, documents = validator._load_evidence_documents(evidence_dir, schema)
    assert loaded_set == evidence_set
    assert documents == [evidence_set]
    with pytest.raises(validator.DocumentError, match="does not exist"):
        validator._load_evidence_documents(tmp_path / "absent", schema)

    manifest = {"manifest_id": "11111111-1111-4111-8111-111111111111"}
    artifact = _trusted_member(artifact_id="7001", member="manifest.json", sha256="1" * 64)
    receipt = {
        "manifest_id": manifest["manifest_id"],
        "verification_outcome": "passed",
        "repository": "AstralDeep/AstralDeep",
        "candidate_sha": "a" * 40,
        "protected_builder_sha": "f" * 40,
        "protected_builder_identity": "protected-identity",
        "manifest_artifact": artifact,
    }
    assert validator._verify_attestation_receipts(
        [manifest],
        [receipt],
        repository="AstralDeep/AstralDeep",
        candidate_sha="a" * 40,
        builder_sha="f" * 40,
        builder_identity="protected-identity",
    ) == {manifest["manifest_id"]: receipt}
    with pytest.raises(validator.ProvenanceError, match="duplicate"):
        validator._verify_attestation_receipts(
            [manifest],
            [receipt, receipt],
            repository="AstralDeep/AstralDeep",
            candidate_sha="a" * 40,
            builder_sha="f" * 40,
            builder_identity="protected-identity",
        )


def test_protected_decision_generation_requires_context_and_schema_valid_inputs(
    validator: Any,
    contract_examples: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence_set = _passing_set(contract_examples)
    policy_result = validator.evaluate_evidence_set(
        evidence_set, now=datetime(2026, 7, 16, 12, tzinfo=UTC)
    )
    evidence_artifact = _trusted_member(
        artifact_id="7101", member="release-evidence-set.json", sha256="1" * 64
    )
    coverage_artifact = _trusted_member(
        artifact_id="7102", member="coverage.json", sha256="2" * 64
    )
    evidence_artifact_path = tmp_path / "evidence-artifact.json"
    coverage_artifact_path = tmp_path / "coverage-artifact.json"
    evidence_artifact_path.write_text(json.dumps(evidence_artifact), encoding="utf-8")
    coverage_artifact_path.write_text(json.dumps(coverage_artifact), encoding="utf-8")
    producer_artifact = _trusted_member(
        artifact_id="7201", member="producer-manifest.json", sha256="3" * 64
    )
    stage_artifact = _trusted_member(
        artifact_id="7202", member="stage-manifest.json", sha256="4" * 64
    )
    verification = {
        "11111111-1111-4111-8111-111111111111": {
            "role": "producer",
            "manifest_artifact": producer_artifact,
        },
        "22222222-2222-4222-8222-222222222222": {
            "role": "stage_deploy",
            "manifest_artifact": stage_artifact,
        },
    }
    ledger = validator.LedgerSnapshot(
        repository="AstralDeep/AstralDeep",
        ref="refs/heads/release-evidence-debt",
        commit_sha="d" * 40,
        tree_sha="e" * 40,
        snapshot_sha256="5" * 64,
        paths={},
        records={},
    )
    args = SimpleNamespace(
        repository="AstralDeep/AstralDeep",
        base_sha="b" * 40,
        candidate_sha="a" * 40,
        exception_ledger_repository="AstralDeep/AstralDeep",
        exception_ledger_ref="refs/heads/release-evidence-debt",
        protected_policy_sha="6" * 64,
        protected_builder_sha="f" * 40,
        protected_builder_identity=(
            "https://github.com/AstralDeep/AstralDeep/.github/workflows/"
            "release-trusted-builder.yml@refs/heads/main"
        ),
        protected_workflow_ref=(
            "AstralDeep/AstralDeep/.github/workflows/release-readiness.yml@"
            + "f" * 40
        ),
        coverage_percent=95.0,
        coverage_artifact=str(coverage_artifact_path),
        evidence_set_artifact=str(evidence_artifact_path),
        valid_until="2026-07-17T00:00:00Z",
    )
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_JOB": "protected-decision",
        "GITHUB_WORKFLOW": "release-trusted-builder",
        "GITHUB_RUN_ID": "6001",
        "GITHUB_RUN_ATTEMPT": "1",
        "RUNNER_NAME": "GitHub Actions 1",
        "ASTRAL_RUNNER_OS": "linux",
        "ASTRAL_RUNNER_ARCH": "x86_64",
        "ASTRAL_RUNNER_ENVIRONMENT": "github_hosted",
        "ImageOS": "ubuntu-24.04",
    }.items():
        monkeypatch.setenv(name, value)
    trust_schema = validator.load_json_document(CONTRACT_ROOT / "release-trust.schema.json")
    decision = validator._decision_manifest(
        args=args,
        evidence_set=evidence_set,
        policy_result=policy_result,
        ledger=ledger,
        verification_receipts=verification,
        approvals=[],
        now=datetime(2026, 7, 16, 12, tzinfo=UTC),
        trust_schema=trust_schema,
    )
    assert decision["decision"] == "passed"
    assert decision["coverage_percent"] == 95.0
    assert decision["workflow_ref"].endswith(
        "/.github/workflows/release-readiness.yml@" + "f" * 40
    )
    assert decision["trusted_builder"]["workflow_path"] == (
        ".github/workflows/release-trusted-builder.yml"
    )
    assert {item["role"] for item in decision["input_manifests"]} == {
        "producer",
        "stage_deploy",
    }
    invalid_args = SimpleNamespace(**vars(args))
    invalid_args.protected_workflow_ref = (
        "AstralDeep/AstralDeep/.github/workflows/release-trusted-builder.yml@"
        + "f" * 40
    )
    with pytest.raises(validator.ProvenanceError, match="protected workflow ref"):
        validator._decision_manifest(
            args=invalid_args,
            evidence_set=evidence_set,
            policy_result=policy_result,
            ledger=ledger,
            verification_receipts=verification,
            approvals=[],
            now=datetime(2026, 7, 16, 12, tzinfo=UTC),
            trust_schema=trust_schema,
        )

    invalid_args = SimpleNamespace(**vars(args))
    invalid_args.protected_workflow_ref = (
        "AstralDeep/AstralDeep/.github/workflows/release-readiness.yml@"
        + "e" * 40
    )
    with pytest.raises(validator.ProvenanceError, match="builder pin"):
        validator._decision_manifest(
            args=invalid_args,
            evidence_set=evidence_set,
            policy_result=policy_result,
            ledger=ledger,
            verification_receipts=verification,
            approvals=[],
            now=datetime(2026, 7, 16, 12, tzinfo=UTC),
            trust_schema=trust_schema,
        )

    invalid_args = SimpleNamespace(**vars(args))
    invalid_args.coverage_percent = None
    with pytest.raises(validator.PolicyError, match="coverage-percent"):
        validator._decision_manifest(
            args=invalid_args,
            evidence_set=evidence_set,
            policy_result=policy_result,
            ledger=ledger,
            verification_receipts=verification,
            approvals=[],
            now=datetime(2026, 7, 16, 12, tzinfo=UTC),
            trust_schema=trust_schema,
        )


def test_main_diagnostic_orchestrates_same_stage_without_authorizing_release(
    validator: Any,
    contract_examples: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_set = _passing_set(contract_examples)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    for name in ("provenance", "approvals", "resolutions", "attestations"):
        (tmp_path / name).mkdir()
    stage_path = tmp_path / "stage.json"
    stage = copy.deepcopy(evidence_set["evidence"][0]["staging_environment"])
    support_image = "registry.invalid/support@sha256:" + "9" * 64
    stage.update(
        request_namespace="astral060-request",
        capability_manifest_sha256="7" * 64,
        service_identity_sha256="8" * 64,
        service_image_references={
            "postgres": support_image,
            "keycloak-postgres": support_image,
            "keycloak": "registry.invalid/keycloak@sha256:" + "6" * 64,
            "livekit": stage["voice_runtime"]["livekit_image_reference"],
            "schema-baseline": "registry.invalid/baseline@sha256:" + "5" * 64,
            "astraldeep": stage["candidate_image_reference"],
            "voice-worker": stage["voice_runtime"][
                "voice_worker_image_reference"
            ],
        },
        livekit_turn_tls={
            "advertised_uri": (
                "turns:turn.stage-060.astraldeep.invalid:443?transport=tcp"
            ),
            "public_port": 443,
            "external_tls": True,
            "terminator_upstream_host": "127.0.0.1",
            "terminator_upstream_port": 15349,
            "livekit_listener_port": 5349,
        },
    )
    stage_document = {"deployment": stage}
    schemas = {
        "evidence-schema.json": validator.load_json_document(
            CONTRACT_ROOT / "release-evidence.schema.json"
        ),
        "trust-schema.json": validator.load_json_document(
            CONTRACT_ROOT / "release-trust.schema.json"
        ),
        "profile-schema.json": validator.load_json_document(
            CONTRACT_ROOT / "windows-deployment-profile.schema.json"
        ),
    }
    original_load = validator.load_json_document

    def load(path: str | Path) -> dict[str, Any]:
        name = Path(path).name
        if name == "stage.json":
            return stage_document
        if name in schemas:
            return schemas[name]
        return original_load(path)

    monkeypatch.setattr(validator, "load_json_document", load)
    monkeypatch.setattr(
        validator,
        "_load_evidence_documents",
        lambda _root, _schema: (evidence_set, [evidence_set]),
    )
    monkeypatch.setattr(validator, "_load_json_directory", lambda _path: [])
    monkeypatch.setattr(validator, "_verify_attestation_receipts", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(validator, "bind_report_to_producer", lambda *args, **kwargs: {})
    monkeypatch.setattr(validator, "validate_document", lambda *args, **kwargs: None)
    ledger = validator.LedgerSnapshot(
        repository="AstralDeep/AstralDeep",
        ref="refs/heads/release-evidence-debt",
        commit_sha="d" * 40,
        tree_sha="e" * 40,
        snapshot_sha256="9" * 64,
        paths={},
        records={},
    )
    monkeypatch.setattr(validator, "read_ledger_snapshot", lambda *args, **kwargs: ledger)
    monkeypatch.setattr(validator, "validate_exception_history", lambda *args, **kwargs: None)
    argv = [
        "--schema", "evidence-schema.json",
        "--trust-schema", "trust-schema.json",
        "--deployment-profile-schema", "profile-schema.json",
        "--evidence-dir", str(evidence_dir),
        "--base-sha", "b" * 40,
        "--candidate-sha", "a" * 40,
        "--repository", "AstralDeep/AstralDeep",
        "--trusted-provenance-dir", str(tmp_path / "provenance"),
        "--trusted-stage-deploy", str(stage_path),
        "--trusted-approvals-dir", str(tmp_path / "approvals"),
        "--trusted-debt-resolutions-dir", str(tmp_path / "resolutions"),
        "--attestation-verification-dir", str(tmp_path / "attestations"),
        "--protected-builder-sha", "f" * 40,
        "--protected-builder-identity", "protected-identity",
        "--protected-policy-sha", "6" * 64,
        "--exception-ledger-repository", "AstralDeep/AstralDeep",
        "--exception-ledger-ref", "refs/heads/release-evidence-debt",
        "--exception-ledger-commit", "d" * 40,
        "--exception-ledger-checkout", str(tmp_path),
        "--now", "2026-07-16T12:00:00Z",
    ]
    assert validator.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decision"] == "diagnostic_policy_passed"
    assert result["protected_release_authorization"] is False

    original_inventory_sha256 = stage["voice_runtime"]["speech_profile"][
        "inventory_sha256"
    ]
    stage["voice_runtime"]["speech_profile"]["inventory_sha256"] = "0" * 64
    assert validator.main(argv) == 2
    assert "staging differs from protected stage deploy" in capsys.readouterr().err
    stage["voice_runtime"]["speech_profile"][
        "inventory_sha256"
    ] = original_inventory_sha256

    rejected = list(argv)
    rejected[rejected.index("--base-sha") + 1] = "a" * 40
    assert validator.main(rejected) == 2
    assert "base-sha must differ" in capsys.readouterr().err


def test_cli_help_exposes_protected_inputs_and_never_has_network_fallback(validator: Any) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    for option in (
        "--schema",
        "--trust-schema",
        "--deployment-profile-schema",
        "--evidence-dir",
        "--raw-apple-evidence-dir",
        "--windows-product-evidence-dir",
        "--trusted-provenance-dir",
        "--trusted-stage-deploy",
        "--trusted-approvals-dir",
        "--trusted-debt-resolutions-dir",
        "--attestation-verification-dir",
        "--protected-builder-sha",
        "--protected-builder-identity",
        "--protected-policy-sha",
        "--exception-ledger-repository",
        "--exception-ledger-ref",
        "--exception-ledger-commit",
        "--exception-ledger-checkout",
        "--decision-output",
    ):
        assert option in completed.stdout
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "urllib.request" not in source
    assert "import requests" not in source
    assert "from requests" not in source
