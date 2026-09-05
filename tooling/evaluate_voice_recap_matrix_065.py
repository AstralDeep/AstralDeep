#!/usr/bin/env python3
"""Run the fixed Feature 065 synthetic recap matrix without emitting content.

The evaluator exercises the production recap builder, sensitivity policy, and
lifecycle phrase selector.  Its command-line output is intentionally limited to
aggregate counts, rubric scores, and the fixture digest; synthetic source and
spoken text never enter logs or evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


EVALUATOR_VERSION = "1"
MAX_FIXTURE_BYTES = 128 * 1024
EXPECTED_DISTRIBUTION = {
    "authoritative_summary_success": 20,
    "committed_visible_fallback_success": 20,
    "failure": 20,
    "refusal": 15,
    "cancellation": 10,
    "sensitive_result": 15,
}
RUBRIC_DIMENSIONS = (
    "terminal_state_accuracy",
    "unsupported_claims",
    "material_caveat_preservation",
    "next_action_preservation",
    "fabricated_progress",
    "pre_consent_disclosure",
)
PROFILES = {
    "authoritative_summary_success": frozenset(
        {"plain", "markup", "long_primary", "duplicate", "unicode_whitespace"}
    ),
    "committed_visible_fallback_success": frozenset(
        {"direct", "nested", "sequences", "table", "sanitized"}
    ),
    "failure": frozenset({"fixed_terminal"}),
    "refusal": frozenset({"fixed_terminal"}),
    "cancellation": frozenset({"fixed_terminal"}),
    "sensitive_result": frozenset(
        {"classified_sensitive", "classification_unknown", "detector_failure"}
    ),
}
CASE_ID = re.compile(r"^(?:AUTH|FALL|FAIL|REF|CANCEL|SENS)-[0-9]{2}$")
WORD = re.compile(r"[a-z0-9]+")
EXPECTED_PREFIX = {
    "authoritative_summary_success": "AUTH-",
    "committed_visible_fallback_success": "FALL-",
    "failure": "FAIL-",
    "refusal": "REF-",
    "cancellation": "CANCEL-",
    "sensitive_result": "SENS-",
}
TERMINAL_EXPECTATIONS = {
    "failure": ("failed", "failure", "Request failed."),
    "refusal": ("refused", "refusal", "I can't help with that."),
    "cancellation": ("cancelled", "cancellation", "Request cancelled."),
}
TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "description",
        "correctness_threshold_percent",
        "rubric_dimensions",
        "expected_distribution",
        "case_groups",
    }
)


class MatrixValidationError(ValueError):
    """A content-free fixed-matrix validation error."""


@dataclass(frozen=True, slots=True)
class MatrixCase:
    """One fixed case identity and allowlisted input profile."""

    case_id: str
    category: str
    profile: str


@dataclass(frozen=True, slots=True)
class RuntimeBindings:
    """Production functions loaded after the repository root is resolved."""

    build_spoken_recap: Callable[..., Any]
    apply_sensitivity_policy: Callable[..., Any]
    sanitize_speakable_text: Callable[[str], str]
    sensitive_notice: str
    phrase_selector: Any


@dataclass(frozen=True, slots=True)
class CaseScore:
    """Boolean-only rubric outcome; no source or spoken body is retained."""

    terminal_state_accuracy: bool
    unsupported_claims: bool
    material_caveat_preservation: bool | None
    next_action_preservation: bool | None
    fabricated_progress: bool
    pre_consent_disclosure: bool | None

    def values(self) -> dict[str, bool | None]:
        return {
            "terminal_state_accuracy": self.terminal_state_accuracy,
            "unsupported_claims": self.unsupported_claims,
            "material_caveat_preservation": self.material_caveat_preservation,
            "next_action_preservation": self.next_action_preservation,
            "fabricated_progress": self.fabricated_progress,
            "pre_consent_disclosure": self.pre_consent_disclosure,
        }

    @property
    def correct(self) -> bool:
        return all(value is not False for value in self.values().values())


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    """Aggregate-only result suitable for non-content verification evidence."""

    fixture_sha256: str
    threshold_percent: int
    category_counts: Mapping[str, int]
    rubric_scores: Mapping[str, Mapping[str, int]]
    total_cases: int
    correct_cases: int

    @property
    def correctness_percent(self) -> float:
        if self.total_cases == 0:
            return 0.0
        return round(self.correct_cases * 100.0 / self.total_cases, 2)

    @property
    def fabricated_progress_violations(self) -> int:
        score = self.rubric_scores["fabricated_progress"]
        return score["applicable"] - score["passed"]

    @property
    def pre_consent_disclosure_violations(self) -> int:
        score = self.rubric_scores["pre_consent_disclosure"]
        return score["applicable"] - score["passed"]

    @property
    def meets_threshold(self) -> bool:
        return (
            self.total_cases == 100
            and dict(self.category_counts) == EXPECTED_DISTRIBUTION
            and self.correctness_percent >= self.threshold_percent
            and self.fabricated_progress_violations == 0
            and self.pre_consent_disclosure_violations == 0
        )

    def to_dict(self) -> dict[str, Any]:
        """Return only aggregate counters and fixture identity."""

        return {
            "category_counts": dict(self.category_counts),
            "correct_cases": self.correct_cases,
            "correctness_percent": self.correctness_percent,
            "evaluator_version": EVALUATOR_VERSION,
            "fabricated_progress_violations": self.fabricated_progress_violations,
            "fixture_sha256": self.fixture_sha256,
            "meets_threshold": self.meets_threshold,
            "pre_consent_disclosure_violations": (
                self.pre_consent_disclosure_violations
            ),
            "rubric_scores": {
                name: dict(score) for name, score in self.rubric_scores.items()
            },
            "threshold_percent": self.threshold_percent,
            "total_cases": self.total_cases,
        }


def _validation_error(code: str) -> MatrixValidationError:
    return MatrixValidationError(code)


def _reject_duplicate_pairs(pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
    output: dict[Any, Any] = {}
    for key, value in pairs:
        if key in output:
            raise _validation_error("duplicate_json_key")
        output[key] = value
    return output


def _reject_nonfinite(_value: str) -> None:
    raise _validation_error("nonfinite_json_number")


def load_matrix(path: Path) -> tuple[dict[str, Any], str]:
    """Load one bounded, duplicate-free fixture and return its byte digest."""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise _validation_error("matrix_unreadable") from exc
    if not payload or len(payload) > MAX_FIXTURE_BYTES:
        raise _validation_error("matrix_size_invalid")
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise _validation_error("matrix_json_invalid") from exc
    if not isinstance(document, dict):
        raise _validation_error("matrix_root_invalid")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    return document, digest


def validate_matrix(document: Mapping[str, Any]) -> tuple[MatrixCase, ...]:
    """Validate exact distribution, stable identities, and profile allowlists."""

    if frozenset(document) != TOP_LEVEL_KEYS:
        raise _validation_error("matrix_top_level_invalid")
    if document.get("schema_version") != "1":
        raise _validation_error("matrix_schema_invalid")
    description = document.get("description")
    if not isinstance(description, str) or "synthetic" not in description.lower():
        raise _validation_error("matrix_description_invalid")
    if "non-phi" not in description.lower():
        raise _validation_error("matrix_non_phi_marker_missing")
    threshold = document.get("correctness_threshold_percent")
    if isinstance(threshold, bool) or threshold != 95:
        raise _validation_error("matrix_threshold_invalid")
    rubric = document.get("rubric_dimensions")
    if not isinstance(rubric, list) or tuple(rubric) != RUBRIC_DIMENSIONS:
        raise _validation_error("matrix_rubric_invalid")
    distribution = document.get("expected_distribution")
    if not isinstance(distribution, dict) or distribution != EXPECTED_DISTRIBUTION:
        raise _validation_error("matrix_distribution_invalid")
    groups = document.get("case_groups")
    if not isinstance(groups, list) or not groups:
        raise _validation_error("matrix_groups_invalid")

    cases: list[MatrixCase] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {
            "category",
            "profile",
            "case_ids",
        }:
            raise _validation_error("matrix_group_invalid")
        category = group.get("category")
        profile = group.get("profile")
        case_ids = group.get("case_ids")
        if category not in EXPECTED_DISTRIBUTION:
            raise _validation_error("matrix_category_invalid")
        if profile not in PROFILES[category]:
            raise _validation_error("matrix_profile_invalid")
        if not isinstance(case_ids, list) or not case_ids:
            raise _validation_error("matrix_case_ids_invalid")
        for case_id in case_ids:
            if (
                not isinstance(case_id, str)
                or CASE_ID.fullmatch(case_id) is None
                or not case_id.startswith(EXPECTED_PREFIX[category])
                or case_id in seen
            ):
                raise _validation_error("matrix_case_id_invalid")
            seen.add(case_id)
            cases.append(MatrixCase(case_id, category, profile))

    actual = Counter(case.category for case in cases)
    if len(cases) != 100 or dict(actual) != EXPECTED_DISTRIBUTION:
        raise _validation_error("matrix_case_distribution_mismatch")
    return tuple(cases)


def load_runtime(repo_root: Path) -> RuntimeBindings:
    """Resolve production bindings without making tooling a runtime dependency."""

    backend_root = repo_root.resolve() / "backend"
    if not backend_root.is_dir():
        raise _validation_error("backend_root_missing")
    backend_text = str(backend_root)
    if backend_text not in sys.path:
        sys.path.insert(0, backend_text)
    try:
        from orchestrator.voice_coordinator import LifecyclePhraseSelector
        from orchestrator.voice_recap import (
            SENSITIVE_NOTICE,
            apply_sensitivity_policy,
            build_spoken_recap,
            sanitize_speakable_text,
        )
    except ImportError as exc:
        raise _validation_error("recap_runtime_unavailable") from exc
    return RuntimeBindings(
        build_spoken_recap=build_spoken_recap,
        apply_sensitivity_policy=apply_sensitivity_policy,
        sanitize_speakable_text=sanitize_speakable_text,
        sensitive_notice=SENSITIVE_NOTICE,
        phrase_selector=LifecyclePhraseSelector(),
    )


def _case_fragments(case_id: str) -> tuple[str, str, str, str, str]:
    conclusion = f"Synthetic conclusion {case_id} is ready"
    caveat = f"Caveat marker C-{case_id} remains"
    action = f"perform followup marker A-{case_id}"
    fallback_trap = f"Synthetic fallback trap {case_id}"
    progress_trap = f"Synthetic progress trap {case_id}"
    return conclusion, caveat, action, fallback_trap, progress_trap


def _authoritative_inputs(
    case: MatrixCase,
) -> tuple[str, list[dict[str, Any]], tuple[str, ...], tuple[str, ...]]:
    conclusion, caveat, action, fallback_trap, progress_trap = _case_fragments(
        case.case_id
    )
    conclusion_sentence = conclusion + "."
    caveat_sentence = "However, " + caveat + "."
    action_sentence = "Next, " + action + "."
    if case.profile == "plain":
        summary = " ".join((conclusion_sentence, caveat_sentence, action_sentence))
    elif case.profile == "markup":
        summary = (
            f"<b>{conclusion_sentence}</b> {caveat_sentence} "
            f"[Next action](https://example.invalid): {action_sentence}"
        )
    elif case.profile == "long_primary":
        summary = " ".join(
            (
                conclusion_sentence,
                ("Synthetic supporting detail. " * 90).strip(),
                caveat_sentence,
                action_sentence,
            )
        )
    elif case.profile == "duplicate":
        summary = " ".join(
            (
                conclusion_sentence,
                conclusion_sentence,
                caveat_sentence,
                caveat_sentence,
                action_sentence,
                action_sentence,
            )
        )
    elif case.profile == "unicode_whitespace":
        summary = (
            f"\t{conclusion_sentence}\n\n{caveat_sentence}\u00a0  {action_sentence}"
        )
    else:
        raise _validation_error("authoritative_profile_unhandled")
    components = [
        {
            "type": "card",
            "content": fallback_trap,
            "progress": progress_trap,
        }
    ]
    return (
        summary,
        components,
        (conclusion, caveat, action),
        (fallback_trap, progress_trap),
    )


def _fallback_inputs(
    case: MatrixCase,
) -> tuple[list[dict[str, Any]], tuple[str, ...], tuple[str, ...]]:
    conclusion, caveat, action, fallback_trap, progress_trap = _case_fragments(
        case.case_id
    )
    hidden_trap = f"Synthetic hidden trap {case.case_id}"
    if case.profile == "direct":
        components = [
            {
                "type": "card",
                "conclusion": conclusion,
                "caveat": "However, " + caveat,
                "next_action": "Next, " + action,
                "progress": progress_trap,
                "hidden_reasoning": hidden_trap,
            }
        ]
    elif case.profile == "nested":
        components = [
            {
                "type": "card",
                "sections": [
                    {"type": "text", "content": conclusion},
                    {"type": "alert", "warning": "However, " + caveat},
                    {"type": "text", "recommendation": "Next, " + action},
                    {"type": "text", "text": hidden_trap, "visible": False},
                    {"type": "html", "content": fallback_trap},
                ],
                "progress": progress_trap,
            }
        ]
    elif case.profile == "sequences":
        components = [
            {
                "type": "card",
                "summary": conclusion,
                "warnings": ["However, " + caveat],
                "next_steps": ["Next, " + action],
                "tool_calls": [{"content": fallback_trap}],
                "intermediate": progress_trap,
            }
        ]
    elif case.profile == "table":
        components = [
            {
                "type": "table",
                "rows": [{"Result": conclusion, "Status": "Ready"}],
                "limitation": "However, " + caveat,
                "next_step": "Next, " + action,
                "metadata": {"text": fallback_trap},
                "progress": progress_trap,
            }
        ]
    elif case.profile == "sanitized":
        components = [
            {
                "type": "card",
                "content": f"<b>{conclusion}</b>",
                "warning": f"However, {caveat} https://example.invalid",
                "recommendation": f"[Next](https://example.invalid), {action}",
                "raw_html": fallback_trap,
                "progress": progress_trap,
                "children": [
                    {"type": "text", "text": hidden_trap, "hidden": True}
                ],
            }
        ]
    else:
        raise _validation_error("fallback_profile_unhandled")
    return (
        components,
        (conclusion, caveat, action),
        (fallback_trap, progress_trap, hidden_trap),
    )


def _tokens(value: str) -> set[str]:
    return set(WORD.findall(value.casefold()))


def _contains_all(value: str, fragments: Sequence[str]) -> bool:
    normalized = value.casefold()
    return all(fragment.casefold() in normalized for fragment in fragments)


def _contains_none(value: str, fragments: Sequence[str]) -> bool:
    normalized = value.casefold()
    return all(fragment.casefold() not in normalized for fragment in fragments)


def _all_source_tokens(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        output = set().union(*(_tokens(str(key)) for key in value)) if value else set()
        for item in value.values():
            output.update(_all_source_tokens(item))
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        output: set[str] = set()
        for item in value:
            output.update(_all_source_tokens(item))
        return output
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return _tokens(str(value))
    return set()


def _score_authoritative(case: MatrixCase, runtime: RuntimeBindings) -> CaseScore:
    summary, components, expected, forbidden = _authoritative_inputs(case)
    recap = runtime.build_spoken_recap(
        authoritative_summary=summary,
        committed_components=components,
        detected_language="en-US",
    )
    lifecycle = runtime.phrase_selector.select(
        lifecycle="succeeded",
        stable_id=case.case_id,
        sequence=1,
        last_phrase_key=None,
    )
    sanitized_source = runtime.sanitize_speakable_text(summary)
    unsupported = (
        _tokens(recap.text) <= _tokens(sanitized_source)
        and _contains_none(recap.text, forbidden)
    )
    return CaseScore(
        terminal_state_accuracy=(
            lifecycle.kind == "result"
            and lifecycle.text is None
            and recap.source == "authoritative_summary"
        ),
        unsupported_claims=unsupported,
        material_caveat_preservation=_contains_all(recap.text, (expected[1],)),
        next_action_preservation=_contains_all(recap.text, (expected[2],)),
        fabricated_progress=_contains_none(recap.text, (forbidden[1],)),
        pre_consent_disclosure=None,
    )


def _score_fallback(case: MatrixCase, runtime: RuntimeBindings) -> CaseScore:
    components, expected, forbidden = _fallback_inputs(case)
    recap = runtime.build_spoken_recap(
        authoritative_summary=None,
        committed_components=components,
        detected_language="en",
    )
    lifecycle = runtime.phrase_selector.select(
        lifecycle="succeeded",
        stable_id=case.case_id,
        sequence=1,
        last_phrase_key=None,
    )
    unsupported = (
        _tokens(recap.text) <= _all_source_tokens(components)
        and _contains_none(recap.text, forbidden)
    )
    return CaseScore(
        terminal_state_accuracy=(
            lifecycle.kind == "result"
            and lifecycle.text is None
            and recap.source == "committed_visible_fallback"
        ),
        unsupported_claims=unsupported,
        material_caveat_preservation=_contains_all(recap.text, (expected[1],)),
        next_action_preservation=_contains_all(recap.text, (expected[2],)),
        fabricated_progress=_contains_none(recap.text, (forbidden[1],)),
        pre_consent_disclosure=None,
    )


def _score_terminal(case: MatrixCase, runtime: RuntimeBindings) -> CaseScore:
    lifecycle_state, expected_kind, expected_text = TERMINAL_EXPECTATIONS[case.category]
    lifecycle = runtime.phrase_selector.select(
        lifecycle=lifecycle_state,
        stable_id=case.case_id,
        sequence=1,
        last_phrase_key=None,
    )
    exact = lifecycle.kind == expected_kind and lifecycle.text == expected_text
    return CaseScore(
        terminal_state_accuracy=exact,
        unsupported_claims=exact,
        material_caveat_preservation=None,
        next_action_preservation=None,
        fabricated_progress=exact,
        pre_consent_disclosure=None,
    )


def _detector_failure(_text: str) -> bool:
    raise RuntimeError("synthetic_detector_failure")


def _detector_clear(_text: str) -> bool:
    return False


def _score_sensitive(case: MatrixCase, runtime: RuntimeBindings) -> CaseScore:
    conclusion, caveat, action, _fallback_trap, progress_trap = _case_fragments(
        case.case_id
    )
    summary = f"{conclusion}. However, {caveat}. Next, {action}."
    recap = runtime.build_spoken_recap(
        authoritative_summary=summary,
        committed_components=None,
        detected_language="en",
    )
    if case.profile == "classified_sensitive":
        confidentiality = "sensitive"
        detector: Callable[[str], bool] = _detector_clear
    elif case.profile == "classification_unknown":
        confidentiality = "unknown"
        detector = _detector_clear
    elif case.profile == "detector_failure":
        confidentiality = "non_sensitive"
        detector = _detector_failure
    else:
        raise _validation_error("sensitive_profile_unhandled")
    gated = runtime.apply_sensitivity_policy(
        recap,
        confidentiality=confidentiality,
        contains_phi=detector,
    )
    released = runtime.apply_sensitivity_policy(
        recap,
        confidentiality=confidentiality,
        contains_phi=detector,
        consent_granted=True,
    )
    lifecycle = runtime.phrase_selector.select(
        lifecycle="succeeded",
        stable_id=case.case_id,
        sequence=1,
        last_phrase_key=None,
    )
    detail_fragments = (conclusion, caveat, action, progress_trap)
    pre_consent_safe = (
        gated.text == runtime.sensitive_notice
        and _contains_none(gated.text, detail_fragments)
    )
    released_supported = (
        released.source == "authoritative_summary"
        and _tokens(released.text)
        <= _tokens(runtime.sanitize_speakable_text(summary))
    )
    return CaseScore(
        terminal_state_accuracy=(
            lifecycle.kind == "result"
            and lifecycle.text is None
            and gated.source == "sensitive_notice"
            and released.source == "authoritative_summary"
        ),
        unsupported_claims=pre_consent_safe and released_supported,
        material_caveat_preservation=_contains_all(released.text, (caveat,)),
        next_action_preservation=_contains_all(released.text, (action,)),
        fabricated_progress=(
            _contains_none(gated.text, (progress_trap,))
            and _contains_none(released.text, (progress_trap,))
        ),
        pre_consent_disclosure=pre_consent_safe,
    )


def score_case(case: MatrixCase, runtime: RuntimeBindings) -> CaseScore:
    """Exercise one production path and retain only its boolean rubric result."""

    if case.category == "authoritative_summary_success":
        return _score_authoritative(case, runtime)
    if case.category == "committed_visible_fallback_success":
        return _score_fallback(case, runtime)
    if case.category in TERMINAL_EXPECTATIONS:
        return _score_terminal(case, runtime)
    if case.category == "sensitive_result":
        return _score_sensitive(case, runtime)
    raise _validation_error("matrix_category_unhandled")


def _failed_score(case: MatrixCase) -> CaseScore:
    preserves_result_details = case.category in {
        "authoritative_summary_success",
        "committed_visible_fallback_success",
        "sensitive_result",
    }
    return CaseScore(
        terminal_state_accuracy=False,
        unsupported_claims=False,
        material_caveat_preservation=(False if preserves_result_details else None),
        next_action_preservation=(False if preserves_result_details else None),
        fabricated_progress=False,
        pre_consent_disclosure=(
            False if case.category == "sensitive_result" else None
        ),
    )


def evaluate_document(
    document: Mapping[str, Any],
    *,
    fixture_sha256: str,
    repo_root: Path,
) -> ReviewSummary:
    """Run all selected cases without dropping failures or retaining bodies."""

    cases = validate_matrix(document)
    runtime = load_runtime(repo_root)
    scores: list[CaseScore] = []
    for case in cases:
        try:
            scores.append(score_case(case, runtime))
        except Exception:
            scores.append(_failed_score(case))

    rubric_scores: dict[str, dict[str, int]] = {}
    for dimension in RUBRIC_DIMENSIONS:
        values = [score.values()[dimension] for score in scores]
        applicable = [value for value in values if value is not None]
        rubric_scores[dimension] = {
            "applicable": len(applicable),
            "passed": sum(value is True for value in applicable),
        }
    return ReviewSummary(
        fixture_sha256=fixture_sha256,
        threshold_percent=int(document["correctness_threshold_percent"]),
        category_counts=dict(Counter(case.category for case in cases)),
        rubric_scores=rubric_scores,
        total_cases=len(cases),
        correct_cases=sum(score.correct for score in scores),
    )


def evaluate_path(path: Path, *, repo_root: Path) -> ReviewSummary:
    """Load, validate, and evaluate the fixed matrix at ``path``."""

    document, digest = load_matrix(path)
    return evaluate_document(document, fixture_sha256=digest, repo_root=repo_root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate the aggregate Feature 065 synthetic recap matrix."
    )
    default_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--repo-root", type=Path, default=default_root)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=(
            default_root
            / "components/AstralProjection/contracts/fixtures/voice_065/recap_review_matrix.json"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Emit one aggregate JSON object and a threshold-sensitive exit code."""

    args = _parser().parse_args(argv)
    try:
        summary = evaluate_path(args.fixture, repo_root=args.repo_root)
    except MatrixValidationError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    return 0 if summary.meets_threshold else 1


if __name__ == "__main__":
    raise SystemExit(main())
