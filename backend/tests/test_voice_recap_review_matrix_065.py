"""Deterministic, content-isolated tests for the Feature 065 recap matrix."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
EVALUATOR_PATH = REPO_ROOT / "tooling/evaluate_voice_recap_matrix_065.py"
FIXTURE_PATH = (
    REPO_ROOT / "backend/tests/fixtures/voice_065/recap_review_matrix.json"
)


def _load_evaluator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "evaluate_voice_recap_matrix_065",
        EVALUATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("recap matrix evaluator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evaluator = _load_evaluator()


@pytest.fixture(scope="module")
def matrix_document() -> dict[str, Any]:
    document, _digest = evaluator.load_matrix(FIXTURE_PATH)
    return document


@pytest.fixture(scope="module")
def summary() -> Any:
    return evaluator.evaluate_path(FIXTURE_PATH, repo_root=REPO_ROOT)


def test_fixed_matrix_has_exact_non_droppable_distribution(
    matrix_document: dict[str, Any],
) -> None:
    cases = evaluator.validate_matrix(matrix_document)

    assert len(cases) == 100
    assert len({case.case_id for case in cases}) == 100
    assert dict(matrix_document["expected_distribution"]) == {
        "authoritative_summary_success": 20,
        "committed_visible_fallback_success": 20,
        "failure": 20,
        "refusal": 15,
        "cancellation": 10,
        "sensitive_result": 15,
    }
    assert all(evaluator.CASE_ID.fullmatch(case.case_id) for case in cases)


def test_production_recap_paths_pass_the_fixed_aggregate_rubric(summary: Any) -> None:
    assert summary.total_cases == 100
    assert summary.correct_cases == 100
    assert summary.correctness_percent == 100.0
    assert summary.meets_threshold is True
    assert summary.fabricated_progress_violations == 0
    assert summary.pre_consent_disclosure_violations == 0
    assert summary.rubric_scores == {
        "terminal_state_accuracy": {"applicable": 100, "passed": 100},
        "unsupported_claims": {"applicable": 100, "passed": 100},
        "material_caveat_preservation": {"applicable": 55, "passed": 55},
        "next_action_preservation": {"applicable": 55, "passed": 55},
        "fabricated_progress": {"applicable": 100, "passed": 100},
        "pre_consent_disclosure": {"applicable": 15, "passed": 15},
    }


def test_aggregate_output_contains_no_synthetic_source_or_spoken_body(
    summary: Any,
) -> None:
    payload = summary.to_dict()
    serialized = json.dumps(payload, sort_keys=True).casefold()

    assert set(payload) == {
        "category_counts",
        "correct_cases",
        "correctness_percent",
        "evaluator_version",
        "fabricated_progress_violations",
        "fixture_sha256",
        "meets_threshold",
        "pre_consent_disclosure_violations",
        "rubric_scores",
        "threshold_percent",
        "total_cases",
    }
    assert payload["fixture_sha256"].startswith("sha256:")
    assert len(payload["fixture_sha256"]) == 71
    for forbidden in (
        "synthetic conclusion",
        "synthetic caveat",
        "synthetic action",
        "fallback trap",
        "progress trap",
        "sensitive result is ready",
        "couldn't complete",
    ):
        assert forbidden not in serialized


def test_evaluator_detects_an_unsupported_authoritative_claim() -> None:
    runtime = evaluator.load_runtime(REPO_ROOT)
    original = runtime.build_spoken_recap

    def compromised_builder(**kwargs: Any) -> Any:
        recap = original(**kwargs)
        return replace(recap, text=recap.text + " invented-marker")

    compromised = replace(runtime, build_spoken_recap=compromised_builder)
    score = evaluator.score_case(
        evaluator.MatrixCase(
            "AUTH-01",
            "authoritative_summary_success",
            "plain",
        ),
        compromised,
    )

    assert score.unsupported_claims is False
    assert score.correct is False


def test_evaluator_detects_a_pre_consent_sensitive_disclosure() -> None:
    runtime = evaluator.load_runtime(REPO_ROOT)

    def ungated_policy(recap: Any, **_kwargs: Any) -> Any:
        return recap

    compromised = replace(runtime, apply_sensitivity_policy=ungated_policy)
    score = evaluator.score_case(
        evaluator.MatrixCase(
            "SENS-01",
            "sensitive_result",
            "classified_sensitive",
        ),
        compromised,
    )

    assert score.pre_consent_disclosure is False
    assert score.terminal_state_accuracy is False
    assert score.correct is False


def _missing_top_level(document: dict[str, Any]) -> None:
    document.pop("description")


def _wrong_schema(document: dict[str, Any]) -> None:
    document["schema_version"] = "2"


def _missing_synthetic_marker(document: dict[str, Any]) -> None:
    document["description"] = "Fixed non-PHI matrix"


def _missing_non_phi_marker(document: dict[str, Any]) -> None:
    document["description"] = "Fixed synthetic matrix"


def _wrong_threshold(document: dict[str, Any]) -> None:
    document["correctness_threshold_percent"] = 94


def _wrong_rubric(document: dict[str, Any]) -> None:
    document["rubric_dimensions"] = document["rubric_dimensions"][:-1]


def _wrong_distribution(document: dict[str, Any]) -> None:
    document["expected_distribution"]["failure"] = 19


def _missing_groups(document: dict[str, Any]) -> None:
    document["case_groups"] = []


def _malformed_group(document: dict[str, Any]) -> None:
    document["case_groups"][0]["unexpected"] = True


def _unknown_category(document: dict[str, Any]) -> None:
    document["case_groups"][0]["category"] = "unknown"


def _unknown_profile(document: dict[str, Any]) -> None:
    document["case_groups"][0]["profile"] = "unknown"


def _empty_case_ids(document: dict[str, Any]) -> None:
    document["case_groups"][0]["case_ids"] = []


def _invalid_case_id(document: dict[str, Any]) -> None:
    document["case_groups"][0]["case_ids"][0] = "OTHER-01"


def _duplicate_case_id(document: dict[str, Any]) -> None:
    document["case_groups"][0]["case_ids"][1] = "AUTH-01"


def _drop_selected_case(document: dict[str, Any]) -> None:
    document["case_groups"][0]["case_ids"].pop()


@pytest.mark.parametrize(
    ("mutator", "error_code"),
    (
        (_missing_top_level, "matrix_top_level_invalid"),
        (_wrong_schema, "matrix_schema_invalid"),
        (_missing_synthetic_marker, "matrix_description_invalid"),
        (_missing_non_phi_marker, "matrix_non_phi_marker_missing"),
        (_wrong_threshold, "matrix_threshold_invalid"),
        (_wrong_rubric, "matrix_rubric_invalid"),
        (_wrong_distribution, "matrix_distribution_invalid"),
        (_missing_groups, "matrix_groups_invalid"),
        (_malformed_group, "matrix_group_invalid"),
        (_unknown_category, "matrix_category_invalid"),
        (_unknown_profile, "matrix_profile_invalid"),
        (_empty_case_ids, "matrix_case_ids_invalid"),
        (_invalid_case_id, "matrix_case_id_invalid"),
        (_duplicate_case_id, "matrix_case_id_invalid"),
        (_drop_selected_case, "matrix_case_distribution_mismatch"),
    ),
)
def test_matrix_validation_fails_closed(
    matrix_document: dict[str, Any],
    mutator: Callable[[dict[str, Any]], None],
    error_code: str,
) -> None:
    malformed = copy.deepcopy(matrix_document)
    mutator(malformed)

    with pytest.raises(evaluator.MatrixValidationError, match=error_code):
        evaluator.validate_matrix(malformed)


@pytest.mark.parametrize(
    ("payload", "error_code"),
    (
        (b"", "matrix_size_invalid"),
        (b"\xff", "matrix_json_invalid"),
        (b"[]", "matrix_root_invalid"),
        (b'{"a": 1, "a": 2}', "duplicate_json_key"),
        (b'{"a": NaN}', "nonfinite_json_number"),
    ),
)
def test_matrix_loader_rejects_malformed_documents(
    tmp_path: Path,
    payload: bytes,
    error_code: str,
) -> None:
    path = tmp_path / "matrix.json"
    path.write_bytes(payload)

    with pytest.raises(evaluator.MatrixValidationError, match=error_code):
        evaluator.load_matrix(path)


def test_matrix_loader_rejects_missing_and_oversized_files(tmp_path: Path) -> None:
    with pytest.raises(
        evaluator.MatrixValidationError,
        match="matrix_unreadable",
    ):
        evaluator.load_matrix(tmp_path / "missing.json")

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (evaluator.MAX_FIXTURE_BYTES + 1))
    with pytest.raises(
        evaluator.MatrixValidationError,
        match="matrix_size_invalid",
    ):
        evaluator.load_matrix(oversized)


def test_evaluation_retains_a_failed_case_in_the_denominator(
    matrix_document: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = evaluator.score_case
    attempted = 0

    def fail_once(case: Any, runtime: Any) -> Any:
        nonlocal attempted
        attempted += 1
        if attempted == 1:
            raise RuntimeError("synthetic failure")
        return original(case, runtime)

    monkeypatch.setattr(evaluator, "score_case", fail_once)
    result = evaluator.evaluate_document(
        matrix_document,
        fixture_sha256="sha256:" + "0" * 64,
        repo_root=REPO_ROOT,
    )

    assert attempted == 100
    assert result.total_cases == 100
    assert result.correct_cases == 99
    assert result.correctness_percent == 99.0
    assert result.rubric_scores["material_caveat_preservation"]["applicable"] == 55
    assert result.rubric_scores["next_action_preservation"]["applicable"] == 55
    assert result.rubric_scores["pre_consent_disclosure"]["applicable"] == 15
    assert result.meets_threshold is False


def test_zero_case_summary_is_fail_closed() -> None:
    rubric = {
        dimension: {"applicable": 0, "passed": 0}
        for dimension in evaluator.RUBRIC_DIMENSIONS
    }
    result = evaluator.ReviewSummary(
        fixture_sha256="sha256:" + "0" * 64,
        threshold_percent=95,
        category_counts={},
        rubric_scores=rubric,
        total_cases=0,
        correct_cases=0,
    )

    assert result.correctness_percent == 0.0
    assert result.meets_threshold is False


def test_cli_emits_only_aggregate_json(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = evaluator.main(
        [
            "--repo-root",
            str(REPO_ROOT),
            "--fixture",
            str(FIXTURE_PATH),
        ]
    )
    output = capsys.readouterr().out
    parsed = json.loads(output)

    assert exit_code == 0
    assert parsed["total_cases"] == 100
    assert parsed["meets_threshold"] is True
    assert "synthetic conclusion" not in output.casefold()


def test_cli_reports_content_free_validation_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")

    assert evaluator.main(["--fixture", str(invalid)]) == 2
    assert json.loads(capsys.readouterr().out) == {"error": "matrix_root_invalid"}


def test_runtime_and_unhandled_profiles_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(
        evaluator.MatrixValidationError,
        match="backend_root_missing",
    ):
        evaluator.load_runtime(tmp_path)
    runtime = evaluator.load_runtime(REPO_ROOT)
    invalid_cases = (
        evaluator.MatrixCase(
            "AUTH-01", "authoritative_summary_success", "unknown"
        ),
        evaluator.MatrixCase(
            "FALL-01", "committed_visible_fallback_success", "unknown"
        ),
        evaluator.MatrixCase("SENS-01", "sensitive_result", "unknown"),
    )
    scorers = (
        evaluator._score_authoritative,
        evaluator._score_fallback,
        evaluator._score_sensitive,
    )
    for scorer, case in zip(scorers, invalid_cases, strict=True):
        with pytest.raises(evaluator.MatrixValidationError):
            scorer(case, runtime)
    with pytest.raises(
        evaluator.MatrixValidationError,
        match="matrix_category_unhandled",
    ):
        evaluator.score_case(
            evaluator.MatrixCase("FAIL-01", "unknown", "fixed_terminal"),
            runtime,
        )
