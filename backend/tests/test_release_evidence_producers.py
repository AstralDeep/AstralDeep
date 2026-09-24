"""Contract tests pinning the artifact-report shape that Windows, Android, macOS, iOS
and watchOS release-evidence producers must emit, validated through
scripts/validate_release_evidence.py's real rejection paths, not reimplemented
policy.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "validate_release_evidence.py"
CONTRACT_TEST_PATH = REPO_ROOT / "backend" / "tests" / "test_release_contract_schemas.py"
CONTRACT_ROOT = REPO_ROOT / "specs" / "060-runtime-reliability-hardening" / "contracts"

if not (
    (REPO_ROOT / "scripts").is_dir() and (REPO_ROOT / "specs").is_dir()
):
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )

NOW = datetime(2026, 7, 16, 12, 30, tzinfo=UTC)
CLIENT_PLATFORMS = ("windows", "android", "macos", "ios", "watchos")
CLIENT_ARTIFACT_KINDS = {
    "windows": "windows_exe",
    "android": "android_apk",
    "macos": "macos_app",
    "ios": "ios_app",
    "watchos": "watchos_app",
}
COMMON_CLIENT_CHECKS = {
    "sign_in",
    "rendered_chat",
    "reconnect_resume",
    "agent_lifecycle",
    "accessibility_semantics",
    "personal_agent",
}
FALSE_HOST_CAPABILITY = {
    "supported": False,
    "runtime_contract_versions": [],
    "source_feature": None,
    "source": "candidate_capability_map",
    "manifest_sha256": "1" * 64,
}
SUPPORTED_HOST_CAPABILITY = {
    "supported": True,
    "runtime_contract_versions": [2],
    "source_feature": "059",
    "source": "candidate_capability_map",
    "manifest_sha256": "2" * 64,
}
_REMOVED = object()


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def validator() -> Any:
    return _load_module("release_evidence_validator_060_producers", SCRIPT_PATH)


@pytest.fixture(scope="module")
def contract_examples() -> Any:
    return _load_module("release_contract_examples_060_producers", CONTRACT_TEST_PATH)


@pytest.fixture(scope="module")
def evidence_schema(validator: Any) -> dict[str, Any]:
    return validator.load_json_document(CONTRACT_ROOT / "release-evidence.schema.json")


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


def _raw_reference(name: str) -> dict[str, Any]:
    return {
        "name": f"{name}.json",
        "kind": "json_metrics",
        "immutable_reference": f"bundle://raw/{name}.json",
        "sha256": "d" * 64,
    }


def _apply_producer_shape(report: dict[str, Any]) -> None:
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
    for check in report["checks"]:
        if check["outcome"] == "passed":
            check["evidence_artifacts"] = [
                _raw_reference(f"{report['platform']}_{check['id']}")
            ]


def _producer_matrix(contract_examples: Any) -> dict[str, Any]:
    targets = ["backend", "web", "windows", "android", "macos", "ios", "watchos", "docs"]
    evidence = []
    for index, target in enumerate(targets, 1):
        report = contract_examples._platform_evidence(target)
        report["evidence_id"] = f"00000000-0000-4000-8000-{index:012d}"
        _apply_producer_shape(report)
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


def _report(evidence_set: dict[str, Any], platform: str) -> dict[str, Any]:
    return next(item for item in evidence_set["evidence"] if item["platform"] == platform)


def _check_row(report: dict[str, Any], check_id: str) -> dict[str, Any]:
    return next(item for item in report["checks"] if item["id"] == check_id)


def _set_capability(evidence_set: dict[str, Any], capability: Any) -> None:
    for report in evidence_set["evidence"]:
        if report["platform"] == "docs":
            continue
        staging = report["staging_environment"]
        if capability is _REMOVED:
            staging.pop("macos_personal_agent_host", None)
        else:
            staging["macos_personal_agent_host"] = copy.deepcopy(capability)


def _mark_supported_host(evidence_set: dict[str, Any]) -> dict[str, Any]:
    _set_capability(evidence_set, SUPPORTED_HOST_CAPABILITY)
    host_check = _check_row(_report(evidence_set, "macos"), "macos_personal_agent_host")
    host_check.update(outcome="passed", applicability_reason=None)
    host_check["evidence_artifacts"] = [
        _raw_reference("macos_host_structured_v2_registration"),
        _raw_reference("macos_agent_host_registered_ack"),
    ]
    return host_check


def _validate_report(
    validator: Any, evidence_schema: dict[str, Any], report: dict[str, Any]
) -> None:
    validator.validate_document(
        report,
        evidence_schema["$defs"]["platform_evidence"],
        root_schema=evidence_schema,
    )


def _rejected(validator: Any, evidence_set: dict[str, Any], message: str) -> None:
    with pytest.raises(validator.PolicyError, match=message):
        validator.evaluate_evidence_set(evidence_set, now=NOW)


def test_five_client_reports_bind_one_candidate_release_and_exact_artifact(
    validator: Any, contract_examples: Any, evidence_schema: dict[str, Any]
) -> None:
    evidence_set = _producer_matrix(contract_examples)
    validator.validate_document(evidence_set, evidence_schema)
    for platform in CLIENT_PLATFORMS:
        report = _report(evidence_set, platform)
        _validate_report(validator, evidence_schema, report)
        assert report["candidate_sha"] == evidence_set["candidate_sha"]
        assert report["release_id"] == evidence_set["release_id"]
        assert report["release_version"] == evidence_set["release_version"]
        assert report["artifact"] is not None
        assert report["artifact"]["kind"] == CLIENT_ARTIFACT_KINDS[platform]
    result = validator.evaluate_evidence_set(evidence_set, now=NOW)
    assert result.required_targets == tuple(evidence_set["required_targets"])
    assert result.staging_environment_id == "stage-060-request-1"
    assert result.used_exception_ids == ()


def test_client_mandatory_check_sets_are_pinned_to_the_validator(validator: Any) -> None:
    assert validator.REQUIRED_CHECKS["windows"] == COMMON_CLIENT_CHECKS | {
        "windows_deployment_validation",
        "windows_clean_profile_no_dialog",
        "windows_frozen_worker",
        "windows_upgrade_from_0_3_0",
        "dependency_lock_reproducibility",
    }
    assert validator.REQUIRED_CHECKS["android"] == COMMON_CLIENT_CHECKS | {
        "android_next_toolchain_readiness"
    }
    assert validator.REQUIRED_CHECKS["macos"] == COMMON_CLIENT_CHECKS | {
        "apple_first_login_llm",
        "macos_personal_agent_host",
    }
    assert validator.REQUIRED_CHECKS["ios"] == COMMON_CLIENT_CHECKS | {
        "apple_first_login_llm"
    }
    assert validator.REQUIRED_CHECKS["watchos"] == COMMON_CLIENT_CHECKS


@pytest.mark.parametrize("platform", CLIENT_PLATFORMS)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_sha", "b" * 40),
        ("release_id", "release-060-other"),
        ("release_version", "0.4.1"),
    ],
)
def test_drifted_candidate_or_release_identity_in_any_client_report_is_rejected(
    validator: Any, contract_examples: Any, platform: str, field: str, value: str
) -> None:
    evidence_set = _producer_matrix(contract_examples)
    _report(evidence_set, platform)[field] = value
    _rejected(validator, evidence_set, f"{platform} {field} differs from evidence set")


@pytest.mark.parametrize("platform", CLIENT_PLATFORMS)
def test_null_wrong_kind_or_mutable_artifact_is_schema_rejected(
    validator: Any,
    contract_examples: Any,
    evidence_schema: dict[str, Any],
    platform: str,
) -> None:
    evidence_set = _producer_matrix(contract_examples)
    baseline = _report(evidence_set, platform)
    _validate_report(validator, evidence_schema, baseline)

    null_artifact = copy.deepcopy(baseline)
    null_artifact["artifact"] = None
    with pytest.raises(validator.SchemaValidationError, match=r"artifact: expected type"):
        _validate_report(validator, evidence_schema, null_artifact)

    kinds = [CLIENT_ARTIFACT_KINDS[item] for item in CLIENT_PLATFORMS]
    wrong = kinds[(kinds.index(CLIENT_ARTIFACT_KINDS[platform]) + 1) % len(kinds)]
    wrong_kind = copy.deepcopy(baseline)
    wrong_kind["artifact"]["kind"] = wrong
    with pytest.raises(
        validator.SchemaValidationError,
        match=r"artifact\.kind: value does not equal const",
    ):
        _validate_report(validator, evidence_schema, wrong_kind)

    mutable_reference = copy.deepcopy(baseline)
    mutable_reference["artifact"]["immutable_reference"] = (
        "https://example.invalid/latest-artifact"
    )
    with pytest.raises(
        validator.SchemaValidationError, match="oneOf matched 0 branches"
    ):
        _validate_report(validator, evidence_schema, mutable_reference)


@pytest.mark.parametrize("platform", CLIENT_PLATFORMS)
def test_reconnect_resume_floors_bind_every_client_report(
    validator: Any, contract_examples: Any, platform: str
) -> None:
    floors = validator.METRIC_REQUIREMENTS["reconnect_resume"]
    assert floors["trial_count"].threshold == 20
    assert floors["trial_count"].comparator == "gte"
    assert floors["resume_success_rate"].threshold == 100
    assert floors["resume_success_rate"].comparator == "gte"

    evidence_set = _producer_matrix(contract_examples)
    check = _check_row(_report(evidence_set, platform), "reconnect_resume")

    missing = copy.deepcopy(evidence_set)
    missing_check = _check_row(_report(missing, platform), "reconnect_resume")
    missing_check["measurements"] = [
        item for item in missing_check["measurements"] if item["metric"] != "trial_count"
    ]
    _rejected(
        validator,
        missing,
        "required measurement 'trial_count' is missing from reconnect_resume",
    )

    under_trials = copy.deepcopy(evidence_set)
    _check_row(_report(under_trials, platform), "reconnect_resume")["measurements"][0][
        "value"
    ] = 19
    _rejected(
        validator,
        under_trials,
        "measurement 'trial_count' in reconnect_resume misses its threshold",
    )

    imperfect_rate = copy.deepcopy(evidence_set)
    _check_row(_report(imperfect_rate, platform), "reconnect_resume")["measurements"][1][
        "value"
    ] = 99
    _rejected(
        validator,
        imperfect_rate,
        "measurement 'resume_success_rate' in reconnect_resume misses its threshold",
    )

    assert check["measurements"][0]["metric"] == "trial_count"
    noncanonical = copy.deepcopy(evidence_set)
    _check_row(_report(noncanonical, platform), "reconnect_resume")["measurements"][0][
        "threshold"
    ] = 10
    _rejected(
        validator,
        noncanonical,
        "measurement 'trial_count' in reconnect_resume has noncanonical semantics",
    )


@pytest.mark.parametrize("platform", ["macos", "ios"])
def test_apple_first_login_floors_bind_macos_and_ios_reports(
    validator: Any, contract_examples: Any, platform: str
) -> None:
    floors = validator.METRIC_REQUIREMENTS["apple_first_login_llm"]
    assert floors["trial_count"].threshold == 30
    assert floors["acknowledgement_p95_ms"].threshold == 250
    assert floors["success_within_five_seconds_percent"].threshold == 95
    assert floors["terminal_max_ms"].threshold == 10000
    assert floors["responsive_interaction_p95_ms"].threshold == 250

    evidence_set = _producer_matrix(contract_examples)
    validator.evaluate_evidence_set(evidence_set, now=NOW)

    under_trials = copy.deepcopy(evidence_set)
    _check_row(_report(under_trials, platform), "apple_first_login_llm")["measurements"][
        0
    ]["value"] = 29
    _rejected(
        validator,
        under_trials,
        "measurement 'trial_count' in apple_first_login_llm misses its threshold",
    )

    slow_ack = copy.deepcopy(evidence_set)
    _check_row(_report(slow_ack, platform), "apple_first_login_llm")["measurements"][1][
        "value"
    ] = 251
    _rejected(
        validator,
        slow_ack,
        "measurement 'acknowledgement_p95_ms' in apple_first_login_llm misses its threshold",
    )

    unresponsive = copy.deepcopy(evidence_set)
    check = _check_row(_report(unresponsive, platform), "apple_first_login_llm")
    check["measurements"] = [
        item
        for item in check["measurements"]
        if item["metric"] != "responsive_interaction_p95_ms"
    ]
    _rejected(
        validator,
        unresponsive,
        "required measurement 'responsive_interaction_p95_ms' is missing from "
        "apple_first_login_llm",
    )


@pytest.mark.parametrize("platform", ["windows", "android", "watchos"])
def test_apple_first_login_check_belongs_only_to_macos_and_ios_reports(
    validator: Any, contract_examples: Any, platform: str
) -> None:
    evidence_set = _producer_matrix(contract_examples)
    donor = _check_row(_report(evidence_set, "macos"), "apple_first_login_llm")
    _report(evidence_set, platform)["checks"].append(copy.deepcopy(donor))
    _rejected(
        validator, evidence_set, f"{platform} check set is incomplete or contains extras"
    )

    trimmed = _producer_matrix(contract_examples)
    ios = _report(trimmed, "ios")
    ios["checks"] = [
        item for item in ios["checks"] if item["id"] != "apple_first_login_llm"
    ]
    _rejected(validator, trimmed, "ios check set is incomplete or contains extras")


@pytest.mark.parametrize("platform", CLIENT_PLATFORMS)
def test_failed_or_not_run_check_without_exception_rejects_the_report(
    validator: Any, contract_examples: Any, platform: str
) -> None:
    failed = _producer_matrix(contract_examples)
    _check_row(_report(failed, platform), "agent_lifecycle").update(
        outcome="failed", detail_code="assertion_failure", measurements=[]
    )
    _rejected(validator, failed, f"passed {platform} report contains failed check")

    skipped = _producer_matrix(contract_examples)
    _check_row(_report(skipped, platform), "rendered_chat").update(
        outcome="not_run", detail_code="not_executed", measurements=[]
    )
    _rejected(validator, skipped, f"passed {platform} report contains not_run check")

    failed_report = _producer_matrix(contract_examples)
    _report(failed_report, platform)["outcome"] = "failed"
    _rejected(validator, failed_report, f"{platform} contains failed product evidence")


def test_only_the_watch_personal_agent_check_is_always_not_applicable(
    validator: Any, contract_examples: Any, evidence_schema: dict[str, Any]
) -> None:
    baseline = _producer_matrix(contract_examples)
    watch = _report(baseline, "watchos")
    assert _check_row(watch, "personal_agent")["outcome"] == "not_applicable"
    validator.evaluate_evidence_set(baseline, now=NOW)

    exercised = copy.deepcopy(watch)
    _check_row(exercised, "personal_agent").update(
        outcome="passed", applicability_reason=None
    )
    with pytest.raises(
        validator.SchemaValidationError, match="no item matching contains"
    ):
        _validate_report(validator, evidence_schema, exercised)

    other_watch_check = _producer_matrix(contract_examples)
    _check_row(_report(other_watch_check, "watchos"), "sign_in").update(
        outcome="not_applicable",
        applicability_reason="unsupported by canonical capability map",
        measurements=[],
        evidence_artifacts=[],
    )
    _rejected(
        validator,
        other_watch_check,
        "illegal not_applicable outcome for watchos/sign_in",
    )


@pytest.mark.parametrize("platform", ["windows", "android", "macos", "ios"])
def test_personal_agent_authoring_is_applicable_on_every_non_watch_client(
    validator: Any, contract_examples: Any, platform: str
) -> None:
    evidence_set = _producer_matrix(contract_examples)
    _check_row(_report(evidence_set, platform), "personal_agent").update(
        outcome="not_applicable",
        applicability_reason="unsupported by canonical capability map",
        measurements=[],
        evidence_artifacts=[],
    )
    _rejected(
        validator,
        evidence_set,
        f"illegal not_applicable outcome for {platform}/personal_agent",
    )


@pytest.mark.parametrize(
    "capability",
    [
        pytest.param(SUPPORTED_HOST_CAPABILITY, id="supported_true"),
        pytest.param(
            {**FALSE_HOST_CAPABILITY, "runtime_contract_versions": [1]},
            id="versions_not_empty",
        ),
        pytest.param(
            {**FALSE_HOST_CAPABILITY, "source_feature": "059"}, id="source_feature_set"
        ),
        pytest.param(
            {**FALSE_HOST_CAPABILITY, "source": "client_declared_boolean"},
            id="wrong_source",
        ),
        pytest.param("refused", id="malformed_scalar"),
        pytest.param(_REMOVED, id="missing"),
    ],
)
def test_macos_host_na_is_legal_only_for_the_exact_false_capability(
    validator: Any, contract_examples: Any, capability: Any
) -> None:
    baseline = _producer_matrix(contract_examples)
    macos = _report(baseline, "macos")
    assert (
        macos["staging_environment"]["macos_personal_agent_host"]
        == FALSE_HOST_CAPABILITY
    )
    assert _check_row(macos, "macos_personal_agent_host")["outcome"] == "not_applicable"
    validator.evaluate_evidence_set(baseline, now=NOW)

    mutated = _producer_matrix(contract_examples)
    _set_capability(mutated, capability)
    _rejected(
        validator,
        mutated,
        "illegal not_applicable outcome for macos/macos_personal_agent_host",
    )


def test_missing_or_malformed_capability_is_schema_rejected(
    validator: Any, contract_examples: Any, evidence_schema: dict[str, Any]
) -> None:
    evidence_set = _producer_matrix(contract_examples)
    macos = _report(evidence_set, "macos")

    missing = copy.deepcopy(macos)
    del missing["staging_environment"]["macos_personal_agent_host"]
    with pytest.raises(
        validator.SchemaValidationError,
        match="missing required property macos_personal_agent_host",
    ):
        _validate_report(validator, evidence_schema, missing)

    malformed = copy.deepcopy(macos)
    malformed["staging_environment"]["macos_personal_agent_host"]["supported"] = "yes"
    with pytest.raises(
        validator.SchemaValidationError, match=r"supported: expected type"
    ):
        _validate_report(validator, evidence_schema, malformed)


def test_supported_capability_requires_a_passed_v2_acknowledged_host_check(
    validator: Any, contract_examples: Any, evidence_schema: dict[str, Any]
) -> None:
    supported = _producer_matrix(contract_examples)
    host_check = _mark_supported_host(supported)
    assert {item["name"] for item in host_check["evidence_artifacts"]} == {
        "macos_host_structured_v2_registration.json",
        "macos_agent_host_registered_ack.json",
    }
    _validate_report(validator, evidence_schema, _report(supported, "macos"))
    result = validator.evaluate_evidence_set(supported, now=NOW)
    assert result.staging_environment_id == "stage-060-request-1"

    unexercised = _producer_matrix(contract_examples)
    _set_capability(unexercised, SUPPORTED_HOST_CAPABILITY)
    _rejected(
        validator,
        unexercised,
        "illegal not_applicable outcome for macos/macos_personal_agent_host",
    )

    wrong_feature = _producer_matrix(contract_examples)
    _mark_supported_host(wrong_feature)
    _set_capability(
        wrong_feature, {**SUPPORTED_HOST_CAPABILITY, "source_feature": "060"}
    )
    _rejected(
        validator,
        wrong_feature,
        "supported macOS host lacks v2 acknowledged passing evidence",
    )

    wrong_version = _producer_matrix(contract_examples)
    _mark_supported_host(wrong_version)
    _set_capability(
        wrong_version, {**SUPPORTED_HOST_CAPABILITY, "runtime_contract_versions": [1]}
    )
    _rejected(
        validator,
        wrong_version,
        "supported macOS host lacks v2 acknowledged passing evidence",
    )

    refused = _producer_matrix(contract_examples)
    refused_check = _mark_supported_host(refused)
    refused_check.update(
        outcome="failed", detail_code="agent_host_registration_refused", measurements=[]
    )
    _rejected(validator, refused, "passed macos report contains failed check")


@pytest.mark.parametrize("branch", ["capability_false", "capability_supported"])
def test_authoring_and_continuity_stay_applicable_in_both_capability_branches(
    validator: Any, contract_examples: Any, branch: str
) -> None:
    def matrix() -> dict[str, Any]:
        evidence_set = _producer_matrix(contract_examples)
        if branch == "capability_supported":
            _mark_supported_host(evidence_set)
        return evidence_set

    validator.evaluate_evidence_set(matrix(), now=NOW)

    waived_authoring = matrix()
    _check_row(_report(waived_authoring, "macos"), "personal_agent").update(
        outcome="not_applicable",
        applicability_reason="unsupported by canonical capability map",
        measurements=[],
        evidence_artifacts=[],
    )
    _rejected(
        validator,
        waived_authoring,
        "illegal not_applicable outcome for macos/personal_agent",
    )

    skipped_continuity = matrix()
    _check_row(_report(skipped_continuity, "macos"), "reconnect_resume").update(
        outcome="not_run", detail_code="not_executed", measurements=[]
    )
    _rejected(
        validator, skipped_continuity, "passed macos report contains not_run check"
    )


def test_all_five_client_reports_project_one_identical_staging_identity(
    validator: Any, contract_examples: Any
) -> None:
    evidence_set = _producer_matrix(contract_examples)
    projections = {
        validator._canonical_staging(_report(evidence_set, platform)["staging_environment"])
        for platform in CLIENT_PLATFORMS
    }
    assert len(projections) == 1
    assert projections == {
        validator._canonical_staging(_report(evidence_set, "backend")["staging_environment"])
    }
    validator.evaluate_evidence_set(evidence_set, now=NOW)


@pytest.mark.parametrize("platform", CLIENT_PLATFORMS)
def test_one_drifted_staging_endpoint_rejects_the_whole_matrix(
    validator: Any, contract_examples: Any, platform: str
) -> None:
    evidence_set = _producer_matrix(contract_examples)
    _report(evidence_set, platform)["staging_environment"]["endpoint"] = (
        "https://stage-060-drifted.astraldeep.invalid"
    )
    _rejected(validator, evidence_set, f"{platform} staging identity differs from matrix")


def test_quantitative_checks_carry_immutable_raw_references(
    validator: Any, contract_examples: Any, evidence_schema: dict[str, Any]
) -> None:
    evidence_set = _producer_matrix(contract_examples)
    for platform in CLIENT_PLATFORMS:
        report = _report(evidence_set, platform)
        for check in report["checks"]:
            if check["measurements"]:
                assert check["evidence_artifacts"], (
                    f"{platform}/{check['id']} lacks a raw evidence reference"
                )
        _validate_report(validator, evidence_schema, report)

    mutable = copy.deepcopy(_report(evidence_set, "windows"))
    _check_row(mutable, "reconnect_resume")["evidence_artifacts"][0][
        "immutable_reference"
    ] = "https://example.invalid/latest.json"
    with pytest.raises(
        validator.SchemaValidationError, match="oneOf matched 0 branches"
    ):
        _validate_report(validator, evidence_schema, mutable)

    unhashed = copy.deepcopy(_report(evidence_set, "android"))
    _check_row(unhashed, "reconnect_resume")["evidence_artifacts"][0]["sha256"] = "xyz"
    with pytest.raises(
        validator.SchemaValidationError, match="does not match pattern"
    ):
        _validate_report(validator, evidence_schema, unhashed)
