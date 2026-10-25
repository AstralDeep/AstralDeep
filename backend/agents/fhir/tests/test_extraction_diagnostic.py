"""Covers the clinician-review diagnostic over the frozen fixture: fixture integrity
(case count, categories, rationale-bearing labels), every pinned proposal expectation,
separation of semantic agreement from span/check pass rates, disagreement disclosure,
and the digest-token invariant that wrong-note cases never auto-bind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.fhir import extraction_diagnostic as diagnostic
from agents.fhir.extraction_schema import (
    CHECK_ABSTENTION_CONFLICT,
    CHECK_DATE,
    CHECK_DIGEST,
    CHECK_DUPLICATE,
    CHECK_ENUM,
    CHECK_FINDING_CAP,
    CHECK_NOTE_ID,
    CHECK_OVERSIZE,
    CHECK_SCHEMA,
    CHECK_SCHEMA_VERSION,
    CHECK_SPAN_AMBIGUOUS,
    CHECK_SPAN_EMPTY,
    CHECK_SPAN_MISMATCH,
    CHECK_SPAN_RANGE,
    CHECK_TYPE,
    CHECK_UNIT,
    CHECK_UNKNOWN_KEY,
    CHECK_VALUE_RANGE,
    MAX_FINDINGS,
    SCHEMA_VERSION,
)
from agents.fhir.extraction_validate import sha256_hex

KNOWN_CODES = {
    CHECK_ABSTENTION_CONFLICT, CHECK_DATE, CHECK_DIGEST, CHECK_DUPLICATE, CHECK_ENUM,
    CHECK_FINDING_CAP, CHECK_NOTE_ID, CHECK_OVERSIZE, CHECK_SCHEMA, CHECK_SCHEMA_VERSION,
    CHECK_SPAN_AMBIGUOUS, CHECK_SPAN_EMPTY, CHECK_SPAN_MISMATCH, CHECK_SPAN_RANGE,
    CHECK_TYPE, CHECK_UNIT, CHECK_UNKNOWN_KEY, CHECK_VALUE_RANGE, "E_EVIDENCE_REQUIRED",
    "E_SPAN_OVERSIZE", "E_VALUE_REQUIRED",
}

REQUIRED_CATEGORIES = {
    "family_history", "negation", "uncertainty", "chronology", "units",
    "repeated_phrase", "missing_data", "prompt_injection",
}

CASES = diagnostic.load_cases()


def case_reports():
    for case in CASES:
        digest = sha256_hex(case["note"])
        for proposal, report in zip(case["proposals"], diagnostic.score_case(case, digest)):
            yield case, proposal, report


ALL_REPORTS = list(case_reports())


def test_fixture_is_frozen_with_enough_reviewed_cases():
    assert len(CASES) >= 30
    assert len({case["id"] for case in CASES}) == len(CASES)


def test_every_case_carries_required_categories_and_rationale_labels():
    categories = {case["category"] for case in CASES}
    assert REQUIRED_CATEGORIES <= categories
    for case in CASES:
        assert case["id"] and case["note"] and case["note_id"]
        label = case["label"]
        for name in ("patient_ref", "note_date"):
            if label.get(name) is not None:
                assert str(label[name].get("rationale", "")).strip(), (case["id"], name)
        for item in label.get("findings", []):
            assert str(item.get("rationale", "")).strip(), (case["id"], item.get("term"))
            assert item["assertion"] in ("asserted", "negated", "uncertain")
            assert item["experiencer"] in ("patient", "family")
            assert item["temporality"] in ("current", "historical", "planned")


def test_fixture_documents_the_digest_token_and_schema():
    document = json.loads(
        (Path(diagnostic.CASES_PATH)).read_text(encoding="utf-8"))
    assert document["schema_version"] == SCHEMA_VERSION
    assert "__NOTE_DIGEST__" in document["review"]["basis"]


@pytest.mark.parametrize("case, proposal, report", ALL_REPORTS)
def test_every_frozen_expectation_holds(case, proposal, report):
    expected = proposal["expected"]
    assert report["case_id"] == case["id"] and report["proposal"] == proposal["name"]
    assert report["outcome"] == expected["outcome"], (case["id"], report["reasons"])
    if "patient_ref" in expected:
        assert report["statuses"]["patient_ref"] == expected["patient_ref"], case["id"]
    if "note_date" in expected:
        assert report["statuses"]["note_date"] == expected["note_date"], case["id"]
    if "findings_statuses" in expected:
        assert report["statuses"]["findings"] == expected["findings_statuses"], case["id"]
    if expected.get("semantic_scalars"):
        assert report["semantic"]["scalars"] == expected["semantic_scalars"], case["id"]
    if expected.get("findings_semantic"):
        for key in ("expected", "matched", "mismatched", "missed", "unexpected"):
            assert report["semantic"][key] == expected["findings_semantic"][key], (
                case["id"], key)
    if expected.get("codes") is not None:
        assert report["codes"] == expected["codes"], case["id"]
        assert set(report["codes"]) <= KNOWN_CODES
    if expected.get("disagreements") is not None:
        assert len(report["semantic"]["disagreements"]) == expected["disagreements"], case["id"]
    if expected.get("contradictions") is not None:
        assert report["contradictions"] == expected["contradictions"], case["id"]
    if expected.get("statuses"):
        for key, value in expected["statuses"].items():
            assert report["statuses"].get(key) == value, (case["id"], key)
    for disclosure in report["semantic"]["disagreements"]:
        assert disclosure["kind"] and isinstance(disclosure["rationale"], str)


def test_wrong_digest_rejection_uses_a_stale_literal_digest():
    wrong = [case for case in CASES if case["id"] == "case-018-wrong-digest"]
    assert wrong
    response = wrong[0]["proposals"][0]["response"]
    digest = response["note_digest"]
    assert digest != diagnostic.DIGEST_TOKEN
    assert len(digest) == 64 and digest != sha256_hex(wrong[0]["note"])
    for case in CASES:
        if case["id"] == "case-018-wrong-digest":
            continue
        for proposal in case["proposals"]:
            response = proposal["response"]
            if isinstance(response, dict) and "raw" not in response:
                if response.get("note_digest") == diagnostic.DIGEST_TOKEN:
                    assert CHECK_DIGEST not in proposal["expected"].get("codes", []), case["id"]


def test_semantic_meaning_is_scored_separately_from_span_rates():
    reports_034 = [report for case, _, report in ALL_REPORTS if case["id"] == "case-034-absence-not-confirmed"]
    report = reports_034[0]
    assert report["spans"]["proposed"] > 0 and report["spans"]["pass_rate"] == 1.0
    assert report["semantic"]["scalars"]["patient_ref"] == "missed"
    assert report["semantic"]["disagreements"]
    reports_008 = [report for case, _, report in ALL_REPORTS if case["id"] == "case-008-unit-violation"]
    report = reports_008[0]
    assert report["codes"] == [CHECK_UNIT]
    assert report["checks"]["failed"] >= 1
    assert report["semantic"]["matched"] == 1


def test_checks_and_spans_are_reported_for_every_proposal():
    for case, proposal, report in ALL_REPORTS:
        assert set(report["spans"]) == {"proposed", "in_range", "verbatim", "unique", "pass_rate"}
        assert set(report["checks"]) == {"proposed_fields", "failed"}
        assert report["checks"]["proposed_fields"] >= report["checks"]["failed"] >= 0
        assert report["spans"]["unique"] <= report["spans"]["verbatim"] <= report["spans"]["in_range"]


def test_summarize_aggregates_semantic_and_span_views_independently():
    summary = diagnostic.summarize([report for _, _, report in ALL_REPORTS])
    semantic = summary["semantic"]
    assert semantic["expected"] > 0
    assert semantic["matched"] + semantic["mismatched"] <= semantic["expected"]
    assert 0.0 <= summary["semantic_agreement_rate"] <= 1.0
    assert 0.0 <= summary["span_pass_rate"] <= 1.0
    assert summary["spans"]["unique"] <= summary["spans"]["proposed"]


def test_disagreements_disclose_model_asserted_absence():
    reports = [report for case, _, report in ALL_REPORTS if case["id"] == "case-034-absence-not-confirmed"]
    kinds = [item["kind"] for item in reports[0]["semantic"]["disagreements"]]
    assert "patient_ref:missed" in kinds


def test_injection_cases_stay_within_schema():
    injection = [case for case in CASES if case["category"] == "prompt_injection"]
    assert len(injection) >= 2
    for case, _, report in [(c, p, r) for c, p, r in ALL_REPORTS if c["category"] == "prompt_injection"]:
        assert report["outcome"] in ("extracted", "rejected_output")


def test_fixture_stores_each_note_exactly_once_with_no_duplicate_copies():
    def _contains_note(value, note):
        if isinstance(value, str):
            return note in value
        if isinstance(value, dict):
            return any(_contains_note(item, note) for item in value.values())
        if isinstance(value, list):
            return any(_contains_note(item, note) for item in value)
        return False

    for case in CASES:
        note = case["note"]
        for key, value in case.items():
            if key == "note":
                continue
            assert not _contains_note(value, note), (case["id"], key)
    scenario_notes = {}
    for case in CASES:
        scenario_notes.setdefault(case["note"], []).append(case["category"])
    for note, categories in scenario_notes.items():
        if len(categories) > 1:
            assert set(categories) <= {
                "wrong_note", "evidence_integrity", "malformed",
                "deterministic_checks", "absence_integrity", "prompt_injection"}, categories


def test_no_requirement_outside_the_reviewed_cap():
    for case in CASES:
        for proposal in case["proposals"]:
            response = proposal["response"]
            if isinstance(response, dict) and "entries" in response.get("findings", {}):
                assert len(response["findings"]["entries"]) <= MAX_FINDINGS + 2


def test_values_equal_compares_numbers_and_terms():
    assert diagnostic._values_equal(132, 132.0) is True
    assert diagnostic._values_equal("  Chest Pain ", "chest pain") is True
    assert diagnostic._values_equal("132", 132) is False
    assert diagnostic._values_equal(None, None) is True


def test_unexpected_finding_is_disclosed_without_label():
    case = {
        "id": "synthetic-unexpected", "note_id": "SYN-X",
        "note": "Clinic note 2026-01-01: patient reports rash.",
        "label": {
            "patient_ref": {"value": None, "rationale": "not stated"},
            "note_date": {"value": "2026-01-01", "rationale": "header"},
            "findings": [],
        },
        "proposals": [],
    }
    proposal = {
        "name": "extra",
        "response": {
            "schema_version": SCHEMA_VERSION, "note_id": "SYN-X",
            "note_digest": sha256_hex(case["note"]),
            "patient_ref": {"value": None, "evidence": None, "abstention_reason": "not_documented"},
            "note_date": {"value": "2026-01-01", "abstention_reason": None,
                          "evidence": {"start": 13, "end": 23, "quote": "2026-01-01"}},
            "findings": {"entries": [{
                "term": "rash", "evidence": {"start": 40, "end": 44, "quote": "rash"},
                "assertion": "asserted", "experiencer": "patient", "temporality": "current",
                "value_type": "none", "value": None, "unit": None}], "abstention_reason": None},
        },
    }
    report = diagnostic.score_proposal(case, proposal, sha256_hex(case["note"]))
    assert report["semantic"]["unexpected"] == 1
    kinds = [item["kind"] for item in report["semantic"]["disagreements"]]
    assert "finding:rash:unexpected" in kinds
    assert report["semantic"]["scalars"]["patient_ref"] == "agree_absent"


def test_with_digest_only_binds_the_token():
    assert diagnostic._with_digest({"note_digest": diagnostic.DIGEST_TOKEN}, "abc") == {"note_digest": "abc"}
    assert diagnostic._with_digest({"note_digest": "stale"}, "abc") == {"note_digest": "stale"}
