"""Scores frozen clinician-reviewed extraction cases offline: semantic field meaning
is compared against rationale-bearing labels separately from span and check pass
rates, and every disagreement or model-asserted absence is disclosed. Pure
functions over the frozen fixture; live-model qualification happens separately
in staging.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.fhir.extraction import evaluate_proposal
from agents.fhir.extraction_schema import (
    STATUS_ABSENT,
    STATUS_EXTRACTED,
    STATUS_FAILED_CHECK,
    STATUS_NOT_EVALUATED,
)
from agents.fhir.extraction_validate import normalize_term

CASES_PATH = Path(__file__).resolve().parent / "tests" / "fixtures" / "extraction_cases.json"
DIGEST_TOKEN = "__NOTE_DIGEST__"

SEMANTIC_MATCHED = "matched"
SEMANTIC_AGREE_ABSENT = "agree_absent"
SEMANTIC_MISSED = "missed"
SEMANTIC_MISMATCHED = "mismatched"
SEMANTIC_UNEVALUATED = "unevaluated"

_SCALAR_KEYS = ("patient_ref", "note_date")


def load_cases(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    document = json.loads((path or CASES_PATH).read_text(encoding="utf-8"))
    return document["cases"]


def _values_equal(left: Any, right: Any) -> bool:
    if (
        isinstance(left, (int, float)) and not isinstance(left, bool)
        and isinstance(right, (int, float)) and not isinstance(right, bool)
    ):
        return float(left) == float(right)
    if isinstance(left, str) and isinstance(right, str):
        return normalize_term(left) == normalize_term(right)
    return left == right


def _with_digest(payload: Dict[str, Any], digest: str) -> Dict[str, Any]:
    if payload.get("note_digest") == DIGEST_TOKEN:
        patched = dict(payload)
        patched["note_digest"] = digest
        return patched
    return payload


def _scalar_semantics(record: Dict[str, Any], label: Optional[Dict[str, Any]]) -> str:
    if record["status"] == STATUS_NOT_EVALUATED:
        return SEMANTIC_UNEVALUATED
    expected_value = label.get("value") if label else None
    if record["status"] == STATUS_EXTRACTED:
        if expected_value is None or not _values_equal(record.get("value"), expected_value):
            return SEMANTIC_MISMATCHED
        return SEMANTIC_MATCHED
    if record["status"] == STATUS_FAILED_CHECK:
        return SEMANTIC_MISSED if expected_value is not None else SEMANTIC_MISMATCHED
    if record["status"] == STATUS_ABSENT:
        return SEMANTIC_AGREE_ABSENT if expected_value is None else SEMANTIC_MISSED
    return SEMANTIC_UNEVALUATED


def _finding_attributes(record: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        record.get("assertion"), record.get("experiencer"), record.get("temporality"),
        record.get("value_type"), record.get("value"), record.get("unit"),
    )


def _label_attributes(label: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        label.get("assertion"), label.get("experiencer"), label.get("temporality"),
        label.get("value_type"), label.get("value"), label.get("unit"),
    )


def _finding_semantics(
    result_findings: List[Dict[str, Any]], label_findings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    extracted = [record for record in result_findings if record["status"] == STATUS_EXTRACTED]
    labeled_terms = {normalize_term(str(item.get("term", ""))) for item in label_findings}
    matched, missed_items, mismatched_items = 0, [], []
    for item in label_findings:
        term = normalize_term(str(item.get("term", "")))
        candidates = [
            record for record in extracted
            if normalize_term(record.get("term") or "") == term
        ]
        if not candidates:
            missed_items.append(item)
        elif any(_finding_attributes(record) == _label_attributes(item) for record in candidates):
            matched += 1
        else:
            mismatched_items.append(item)
    unexpected_items = [
        record for record in extracted
        if normalize_term(record.get("term") or "") not in labeled_terms
    ]
    return {
        "expected": len(label_findings),
        "matched": matched,
        "mismatched": len(mismatched_items),
        "missed": len(missed_items),
        "unexpected": len(unexpected_items),
        "missed_items": missed_items,
        "mismatched_items": mismatched_items,
        "unexpected_items": unexpected_items,
    }


def _disagreements(
    scalar_states: Dict[str, str],
    finding_semantics: Dict[str, Any],
    label: Dict[str, Any],
) -> List[Dict[str, Any]]:
    disclosures: List[Dict[str, Any]] = []
    for name, state in scalar_states.items():
        if state in (SEMANTIC_MATCHED, SEMANTIC_AGREE_ABSENT, SEMANTIC_UNEVALUATED):
            continue
        disclosures.append({
            "kind": f"{name}:{state}",
            "rationale": (label.get(name) or {}).get("rationale", ""),
        })
    for item in finding_semantics["missed_items"] + finding_semantics["mismatched_items"]:
        disclosures.append({
            "kind": f"finding:{normalize_term(str(item.get('term', '')))}:unresolved",
            "rationale": item.get("rationale", ""),
        })
    for record in finding_semantics["unexpected_items"]:
        disclosures.append({
            "kind": f"finding:{normalize_term(record.get('term') or '')}:unexpected",
            "rationale": "No clinician label covers this extracted finding",
        })
    return disclosures


def _span_records(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [data["fields"]["patient_ref"], data["fields"]["note_date"], *data["fields"]["findings"]]


def score_proposal(case: Dict[str, Any], proposal: Dict[str, Any], digest: str) -> Dict[str, Any]:
    note_id = case["note_id"]
    response = proposal["response"]
    if isinstance(response, dict) and set(response) == {"raw"}:
        evaluated = evaluate_proposal(case["note"], note_id, response["raw"])
    else:
        evaluated = evaluate_proposal(case["note"], note_id, _with_digest(response, digest))
    data = evaluated["_data"]
    label = case["label"]
    scalar_states = {
        name: _scalar_semantics(data["fields"][name], label.get(name))
        for name in _SCALAR_KEYS
    }
    finding_semantics = _finding_semantics(data["fields"]["findings"], label.get("findings", []))
    attempted = [
        record for record in _span_records(data)
        if record.get("span_checks", {}).get("attempted")
    ]
    in_range = sum(1 for record in attempted if record["span_checks"]["in_range"])
    verbatim = sum(1 for record in attempted if record["span_checks"]["verbatim"])
    unique = sum(1 for record in attempted if record["span_checks"]["unique"])
    return {
        "case_id": case["id"],
        "proposal": proposal["name"],
        "outcome": data["outcome"],
        "reasons": data["reasons"],
        "statuses": {
            "patient_ref": data["fields"]["patient_ref"]["status"],
            "note_date": data["fields"]["note_date"]["status"],
            "findings": [record["status"] for record in data["fields"]["findings"]],
            "findings_field": (
                data["findings_field"]["status"] if data.get("findings_field") else None),
        },
        "semantic": {
            "scalars": scalar_states,
            "expected": finding_semantics["expected"],
            "matched": finding_semantics["matched"],
            "mismatched": finding_semantics["mismatched"],
            "missed": finding_semantics["missed"],
            "unexpected": finding_semantics["unexpected"],
            "disagreements": _disagreements(scalar_states, finding_semantics, label),
        },
        "spans": {
            "proposed": len(attempted),
            "in_range": in_range,
            "verbatim": verbatim,
            "unique": unique,
            "pass_rate": round(unique / len(attempted), 4) if attempted else None,
        },
        "checks": {
            "proposed_fields": len(_span_records(data)),
            "failed": sum(
                1 for record in _span_records(data) if record["status"] == STATUS_FAILED_CHECK),
        },
        "contradictions": sum(
            1 for record in data["fields"]["findings"] if record.get("contradiction")),
        "codes": sorted({
            code
            for record in [*_span_records(data), *(  
                [data["findings_field"]] if data.get("findings_field") else [])]
            for code in record.get("failures", [])
        }),
    }


def score_case(case: Dict[str, Any], digest: str) -> List[Dict[str, Any]]:
    return [score_proposal(case, proposal, digest) for proposal in case.get("proposals", [])]


def summarize(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals = ("expected", "matched", "mismatched", "missed", "unexpected")
    semantic = {name: sum(report["semantic"][name] for report in reports) for name in totals}
    spans = {
        "proposed": sum(report["spans"]["proposed"] for report in reports),
        "unique": sum(report["spans"]["unique"] for report in reports),
    }
    return {
        "semantic": semantic,
        "spans": spans,
        "semantic_agreement_rate": (
            round(semantic["matched"] / semantic["expected"], 4) if semantic["expected"] else None),
        "span_pass_rate": (
            round(spans["unique"] / spans["proposed"], 4) if spans["proposed"] else None),
    }
