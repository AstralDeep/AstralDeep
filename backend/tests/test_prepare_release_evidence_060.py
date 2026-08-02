"""Deterministic local pre-push evidence command contracts (T107)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "prepare_release_evidence.py"
CONTRACT_TEST_PATH = REPO_ROOT / "backend" / "tests" / "test_release_contract_schemas.py"
MATRIX_TEST_PATH = REPO_ROOT / "backend" / "tests" / "test_release_evidence_validator.py"

if not (
    (REPO_ROOT / "scripts").is_dir() and (REPO_ROOT / "specs").is_dir()
):  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )
BASE_SHA = "b" * 40  # Differs from the contract examples' GIT_SHA ("a" * 40).
NOW = "2026-07-16T12:00:00Z"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def prepare() -> Any:
    return _load_module("prepare_release_evidence_060", SCRIPT_PATH)


@pytest.fixture(scope="module")
def contract_examples() -> Any:
    return _load_module("release_contract_examples_060_t107", CONTRACT_TEST_PATH)


@pytest.fixture(scope="module")
def matrix_helpers() -> Any:
    return _load_module("release_evidence_matrix_helpers_060", MATRIX_TEST_PATH)


def _write_matrix(
    evidence_dir: Path, contract_examples: Any, matrix_helpers: Any
) -> list[dict[str, Any]]:
    """Write one passing per-platform report file per required target."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    targets = ["backend", "web", "windows", "android", "macos", "ios", "watchos", "docs"]
    reports = []
    for index, target in enumerate(targets, 1):
        report = contract_examples._platform_evidence(target)
        report["evidence_id"] = f"00000000-0000-4000-8000-{index:012d}"
        matrix_helpers._add_required_measurements(report)
        (evidence_dir / f"{target}.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        reports.append(report)
    return reports


def _exception_request(contract_examples: Any) -> dict[str, Any]:
    return {
        "document_type": "evidence_exception_request",
        "schema_version": 1,
        "exception_id": "33333333-3333-4333-8333-333333333333",
        "candidate_sha": contract_examples.GIT_SHA,
        "release_id": "release-060-1",
        "platform": "windows",
        "missing_checks": ["windows_deployment_validation"],
        "reason": "windows runner pool offline for this candidate",
        "requester_login": "fixture-author",
        "requested_at": "2026-07-16T11:00:00Z",
        "maximum_valid_days": 7,
        "blocks_next_release": True,
    }


def _run(
    prepare: Any,
    contract_examples: Any,
    evidence_dir: Path,
    output: Path,
    *extra: str,
) -> int:
    return prepare.main(
        [
            "--evidence-dir",
            str(evidence_dir),
            "--base-sha",
            BASE_SHA,
            "--candidate-sha",
            contract_examples.GIT_SHA,
            "--output",
            str(output),
            "--now",
            NOW,
            *extra,
        ]
    )


def test_cli_parse_errors_and_absent_decision_output_mode(prepare: Any) -> None:
    with pytest.raises(SystemExit) as missing:
        prepare.main([])
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as unknown:
        prepare.main(
            [
                "--base-sha",
                BASE_SHA,
                "--candidate-sha",
                "a" * 40,
                "--decision-output",
                "decision.json",
            ]
        )
    assert unknown.value.code == 2
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "--decision-output" not in source
    assert "uuid4" not in source


def test_malformed_or_equal_shas_are_rejected(
    prepare: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "diagnostic.json"
    assert (
        prepare.main(
            [
                "--evidence-dir",
                str(tmp_path),
                "--base-sha",
                BASE_SHA,
                "--candidate-sha",
                "not-a-sha",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert "release evidence rejected:" in capsys.readouterr().err
    assert (
        prepare.main(
            [
                "--evidence-dir",
                str(tmp_path),
                "--base-sha",
                BASE_SHA,
                "--candidate-sha",
                BASE_SHA,
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert "must differ" in capsys.readouterr().err
    assert not output.exists()


def test_missing_empty_or_unrecognized_evidence_directory_is_rejected(
    prepare: Any,
    contract_examples: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "diagnostic.json"
    assert _run(prepare, contract_examples, tmp_path / "absent", output) == 2
    assert "does not exist" in capsys.readouterr().err

    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run(prepare, contract_examples, empty, output) == 2
    assert "no release evidence documents" in capsys.readouterr().err

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "notes.json").write_text('{"decision": "x"}', encoding="utf-8")
    assert _run(prepare, contract_examples, unrelated, output) == 2
    assert "no release evidence documents" in capsys.readouterr().err
    assert not output.exists()


def test_failure_receipt_classifies_only_structural_missing_evidence(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
) -> None:
    output = tmp_path / "diagnostic.json"
    failure = tmp_path / "missing-failure.json"
    assert (
        _run(
            prepare,
            contract_examples,
            tmp_path / "absent",
            output,
            "--failure-output",
            str(failure),
        )
        == 2
    )
    missing = json.loads(failure.read_text(encoding="utf-8"))
    assert missing == {
        "document_type": "release_evidence_local_failure",
        "schema_version": 1,
        "error_code": "missing_provider_inputs",
        "error_message_sha256": missing["error_message_sha256"],
        "base_sha": BASE_SHA,
        "candidate_sha": contract_examples.GIT_SHA,
        "protected_release_authorization": False,
    }
    assert len(missing["error_message_sha256"]) == 64

    incomplete = tmp_path / "incomplete"
    _write_matrix(incomplete, contract_examples, matrix_helpers)
    (incomplete / "docs.json").unlink()
    rejected_failure = tmp_path / "rejected-failure.json"
    assert (
        _run(
            prepare,
            contract_examples,
            incomplete,
            output,
            "--failure-output",
            str(rejected_failure),
        )
        == 2
    )
    rejected = json.loads(rejected_failure.read_text(encoding="utf-8"))
    assert rejected["error_code"] == "release_evidence_rejected"
    assert rejected["protected_release_authorization"] is False


def test_passing_matrix_emits_the_diagnostic_contract(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_dir = tmp_path / "evidence"
    reports = _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    # A stray non-evidence JSON file (e.g. an earlier diagnostic) is ignored.
    (evidence_dir / "local-diagnostic.json").write_text(
        '{"decision": "stale"}', encoding="utf-8"
    )
    output = tmp_path / "out" / "diagnostic.json"
    assert _run(prepare, contract_examples, evidence_dir, output) == 0

    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["decision"] == "diagnostic_policy_passed"
    assert diagnostic["protected_release_authorization"] is False
    assert diagnostic["base_sha"] == BASE_SHA
    assert diagnostic["candidate_sha"] == contract_examples.GIT_SHA
    assert diagnostic["generated_at"] == NOW
    assert diagnostic["evidence_set_assembled"] is True
    assert uuid.UUID(diagnostic["evidence_set_id"]).version == 5
    assert diagnostic["staging_environment_id"] == "stage-060-request-1"
    assert diagnostic["used_exception_ids"] == []
    assert diagnostic["staging_outputs"] is None
    assert diagnostic["required_targets"] == list(prepare.VALIDATOR.REQUIRED_TARGETS)

    by_path = {entry["path"]: entry for entry in diagnostic["documents"]}
    assert len(diagnostic["documents"]) == len(reports)
    for report in reports:
        entry = by_path[f"{report['platform']}.json"]
        assert entry["document_type"] == "platform_evidence"
        assert entry["sha256"] == prepare.VALIDATOR.canonical_json_sha256(report)

    stdout = capsys.readouterr().out.strip()
    assert json.loads(stdout) == diagnostic


def test_assembly_is_deterministic_for_the_same_now(
    prepare: Any, contract_examples: Any, matrix_helpers: Any, tmp_path: Path
) -> None:
    evidence_dir = tmp_path / "evidence"
    _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert _run(prepare, contract_examples, evidence_dir, first) == 0
    assert _run(prepare, contract_examples, evidence_dir, second) == 0
    assert first.read_bytes() == second.read_bytes()

    # The assembled identity is content-derived, not time-derived.
    third = tmp_path / "third.json"
    assert (
        _run(
            prepare,
            contract_examples,
            evidence_dir,
            third,
            "--now",
            "2026-07-16T13:00:00Z",
        )
        == 0
    )
    identity = json.loads(first.read_text(encoding="utf-8"))["evidence_set_id"]
    assert json.loads(third.read_text(encoding="utf-8"))["evidence_set_id"] == identity


def test_single_existing_evidence_set_passes_through_without_assembly(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_dir = tmp_path / "evidence"
    _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    evidence_set = matrix_helpers._passing_set(contract_examples)
    (evidence_dir / "evidence-set.json").write_text(
        json.dumps(evidence_set), encoding="utf-8"
    )
    output = tmp_path / "diagnostic.json"
    assert _run(prepare, contract_examples, evidence_dir, output) == 0
    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["evidence_set_assembled"] is False
    assert diagnostic["evidence_set_id"] == evidence_set["evidence_set_id"]
    assert len(diagnostic["documents"]) == 9
    capsys.readouterr()

    (evidence_dir / "second-set.json").write_text(
        json.dumps(evidence_set), encoding="utf-8"
    )
    assert _run(prepare, contract_examples, evidence_dir, output) == 2
    assert "exactly one release_evidence_set" in capsys.readouterr().err


def test_policy_and_assembly_rejections_pass_through_as_exit_two(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "diagnostic.json"

    incomplete = tmp_path / "incomplete"
    _write_matrix(incomplete, contract_examples, matrix_helpers)
    (incomplete / "docs.json").unlink()
    assert _run(prepare, contract_examples, incomplete, output) == 2
    assert "required targets" in capsys.readouterr().err

    unused = tmp_path / "unused-request"
    _write_matrix(unused, contract_examples, matrix_helpers)
    (unused / "request.json").write_text(
        json.dumps(_exception_request(contract_examples)), encoding="utf-8"
    )
    assert _run(prepare, contract_examples, unused, output) == 2
    assert "unused exception request" in capsys.readouterr().err

    drift = tmp_path / "drift"
    reports = _write_matrix(drift, contract_examples, matrix_helpers)
    reports[0]["release_version"] = "0.4.1"
    (drift / "backend.json").write_text(json.dumps(reports[0]), encoding="utf-8")
    assert _run(prepare, contract_examples, drift, output) == 2
    assert "disagree" in capsys.readouterr().err

    complete = tmp_path / "complete"
    _write_matrix(complete, contract_examples, matrix_helpers)
    assert (
        prepare.main(
            [
                "--evidence-dir",
                str(complete),
                "--base-sha",
                BASE_SHA,
                "--candidate-sha",
                "c" * 40,
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    assert "differs from CLI candidate" in capsys.readouterr().err
    assert not output.exists()


def test_staging_outputs_must_bind_the_matrix_environment(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_dir = tmp_path / "evidence"
    _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    staging_outputs = tmp_path / "staging-outputs.json"
    staging_outputs.write_text(
        json.dumps({"environment_id": "stage-060-request-1", "endpoint": "x"}),
        encoding="utf-8",
    )
    output = tmp_path / "diagnostic.json"
    assert (
        _run(
            prepare,
            contract_examples,
            evidence_dir,
            output,
            "--staging-outputs",
            str(staging_outputs),
        )
        == 0
    )
    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["staging_outputs"]["environment_id"] == "stage-060-request-1"
    assert diagnostic["staging_outputs"]["sha256"] == (
        hashlib.sha256(staging_outputs.read_bytes()).hexdigest()
    )
    capsys.readouterr()

    staging_outputs.write_text(
        json.dumps({"environment_id": "some-other-environment"}), encoding="utf-8"
    )
    assert (
        _run(
            prepare,
            contract_examples,
            evidence_dir,
            output,
            "--staging-outputs",
            str(staging_outputs),
        )
        == 2
    )
    assert "differs from evidence matrix" in capsys.readouterr().err


def test_coverage_reports_are_inventoried_with_raw_digests(
    prepare: Any, contract_examples: Any, matrix_helpers: Any, tmp_path: Path
) -> None:
    evidence_dir = tmp_path / "evidence"
    _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    coverage_dir = tmp_path / "coverage"
    (coverage_dir / "node-v8").mkdir(parents=True)
    (coverage_dir / "tooling-python.xml").write_bytes(b"<coverage/>")
    (coverage_dir / "node-v8" / "web.json").write_bytes(b"{}")
    (coverage_dir / "notes.txt").write_bytes(b"ignored")
    output = tmp_path / "diagnostic.json"
    assert (
        _run(
            prepare,
            contract_examples,
            evidence_dir,
            output,
            "--coverage-dir",
            str(coverage_dir),
        )
        == 0
    )
    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["coverage_reports"] == [
        {
            "path": "node-v8/web.json",
            "sha256": hashlib.sha256(b"{}").hexdigest(),
        },
        {
            "path": "tooling-python.xml",
            "sha256": hashlib.sha256(b"<coverage/>").hexdigest(),
        },
    ]

    absent = tmp_path / "no-coverage"
    assert (
        _run(
            prepare,
            contract_examples,
            evidence_dir,
            output,
            "--coverage-dir",
            str(absent),
        )
        == 0
    )
    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["coverage_reports"] == []
