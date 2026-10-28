"""Deterministically validates model-proposed note extractions against the fixed
reviewed schema: note identity and digest binding, verbatim unique evidence offsets,
and type/enum/unit/range/date checks, withholding any value that fails a check.
Pure functions only; the extraction tool and the clinician-review diagnostic
both consume this module so the acceptance logic cannot drift between them.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from agents.fhir.extraction_schema import (
    ABSENCE_BASIS,
    ABSTENTION_REASONS,
    ASSERTIONS,
    CHECK_ABSTENTION_CONFLICT,
    CHECK_DATE,
    CHECK_DIGEST,
    CHECK_DUPLICATE,
    CHECK_EVIDENCE_REQUIRED,
    CHECK_ENUM,
    CHECK_FINDING_CAP,
    CHECK_NOTE_ID,
    CHECK_OVERSIZE,
    CHECK_SCHEMA,
    CHECK_SCHEMA_VERSION,
    CHECK_SPAN_AMBIGUOUS,
    CHECK_SPAN_EMPTY,
    CHECK_SPAN_MISMATCH,
    CHECK_SPAN_OVERSIZE,
    CHECK_SPAN_RANGE,
    CHECK_TYPE,
    CHECK_UNIT,
    CHECK_UNKNOWN_KEY,
    CHECK_VALUE_RANGE,
    CHECK_VALUE_REQUIRED,
    EVIDENCE_KEYS,
    EXPERIENCERS,
    FINDING_KEYS,
    FINDINGS_KEYS,
    MAX_FINDINGS,
    MAX_QUOTE_CHARS,
    MAX_RESPONSE_CHARS,
    MAX_STRING_VALUE_CHARS,
    MAX_TERM_CHARS,
    OUTCOME_VALIDATED,
    QUANTITIES,
    OUTCOME_REJECTED,
    SCALAR_FIELDS,
    SCHEMA_VERSION,
    SLOT_KEYS,
    STATUS_ABSENT,
    STATUS_EXTRACTED,
    STATUS_FAILED_CHECK,
    STATUS_NOT_EVALUATED,
    TEMPORALITIES,
    TOP_LEVEL_KEYS,
    VALUE_TYPES,
)

DATE_FORMAT = "%Y-%m-%d"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_term(term: str) -> str:
    return " ".join(term.split()).casefold()


@dataclass
class ValidationResult:
    outcome: str
    reasons: List[str] = field(default_factory=list)
    scalars: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    findings: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def records(self) -> List[Dict[str, Any]]:
        return [*self.scalars.values(), *self.findings]

    def counts(self) -> Dict[str, int]:
        counts = {
            STATUS_EXTRACTED: 0,
            STATUS_ABSENT: 0,
            STATUS_FAILED_CHECK: 0,
            STATUS_NOT_EVALUATED: 0,
        }
        for record in self.records:
            counts[record["status"]] += 1
        return counts


def _rejected(reasons: List[str]) -> ValidationResult:
    scalars = {
        name: {"field": name, "status": STATUS_NOT_EVALUATED, "value": None,
               "evidence": None, "abstention_reason": None, "failures": list(reasons)}
        for name in SCALAR_FIELDS
    }
    return ValidationResult(OUTCOME_REJECTED, list(reasons), scalars, [])


def check_evidence(note_text: str, evidence: Any) -> Dict[str, Any]:
    result = {
        "attempted": evidence is not None,
        "in_range": False,
        "verbatim": False,
        "unique": False,
        "failures": [],
    }
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        result["failures"].append(CHECK_EVIDENCE_REQUIRED)
        return result
    start, end, quote = evidence.get("start"), evidence.get("end"), evidence.get("quote")
    if type(start) is not int or type(end) is not int:
        result["failures"].append(CHECK_SPAN_RANGE)
        return result
    if not isinstance(quote, str) or not quote:
        result["failures"].append(CHECK_SPAN_EMPTY)
        return result
    if len(quote) > MAX_QUOTE_CHARS:
        result["failures"].append(CHECK_SPAN_OVERSIZE)
        return result
    if not (0 <= start < end <= len(note_text)):
        result["failures"].append(CHECK_SPAN_RANGE)
        return result
    result["in_range"] = True
    if note_text[start:end] != quote:
        result["failures"].append(CHECK_SPAN_MISMATCH)
        return result
    result["verbatim"] = True
    first = note_text.find(quote)
    if first != start or note_text.find(quote, first + 1) != -1:
        result["failures"].append(CHECK_SPAN_AMBIGUOUS)
        return result
    result["unique"] = True
    return result


def _bound_evidence(evidence: Any, span: Dict[str, Any], digest: str, note_id: str) -> Optional[Dict[str, Any]]:
    if not span.get("verbatim") or not span.get("unique"):
        return None
    return {
        "note_id": note_id,
        "digest": digest,
        "start": evidence["start"],
        "end": evidence["end"],
        "quote": evidence["quote"],
    }


def _abstention_conflict(slot: Dict[str, Any]) -> bool:
    has_reason = isinstance(slot.get("abstention_reason"), str) and bool(slot.get("abstention_reason"))
    return (slot.get("value") is None) != has_reason


def _validate_scalar(name: str, slot: Any, note_text: str, digest: str, note_id: str) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "field": name, "status": STATUS_FAILED_CHECK, "value": None,
        "evidence": None, "abstention_reason": None, "failures": [],
    }
    if not isinstance(slot, dict) or set(slot) != SLOT_KEYS:
        record["failures"].append(CHECK_SCHEMA)
        return record
    if _abstention_conflict(slot):
        record["failures"].append(CHECK_ABSTENTION_CONFLICT)
        return record
    if slot.get("value") is None:
        reason = slot.get("abstention_reason")
        if reason not in ABSTENTION_REASONS:
            record["failures"].append(CHECK_ENUM)
            return record
        if slot.get("evidence") is not None:
            record["failures"].append(CHECK_ABSTENTION_CONFLICT)
            return record
        record["status"] = STATUS_ABSENT
        record["abstention_reason"] = reason
        record["absence_basis"] = ABSENCE_BASIS
        return record
    value = slot["value"]
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_STRING_VALUE_CHARS:
        record["failures"].append(CHECK_TYPE)
        return record
    if name == "note_date":
        if not DATE_PATTERN.match(value):
            record["failures"].append(CHECK_DATE)
            return record
        try:
            datetime.strptime(value, DATE_FORMAT)
        except ValueError:
            record["failures"].append(CHECK_DATE)
            return record
    span = check_evidence(note_text, slot.get("evidence"))
    record["span_checks"] = span
    record["failures"].extend(span["failures"])
    if span["failures"]:
        return record
    record["status"] = STATUS_EXTRACTED
    record["value"] = value
    record["evidence"] = _bound_evidence(slot["evidence"], span, digest, note_id)
    return record


def _quantity_failure(entry: Dict[str, Any]) -> Optional[str]:
    value_type = entry.get("value_type")
    value = entry.get("value")
    unit = entry.get("unit")
    if value_type == "none":
        if value is not None or unit is not None:
            return CHECK_TYPE
        return None
    if value_type not in QUANTITIES:
        return CHECK_ENUM
    if not isinstance(unit, str) or unit not in QUANTITIES[value_type]:
        return CHECK_UNIT
    if value is None:
        return CHECK_VALUE_REQUIRED
    if type(value) is not int and type(value) is not float:
        return CHECK_TYPE
    if isinstance(value, float) and not math.isfinite(value):
        return CHECK_TYPE
    low, high, _ = QUANTITIES[value_type][unit]
    if not (low <= value <= high):
        return CHECK_VALUE_RANGE
    return None


def _finding_signature(entry: Dict[str, Any]) -> tuple:
    return (
        normalize_term(str(entry.get("term", ""))),
        entry.get("assertion"), entry.get("experiencer"), entry.get("temporality"),
        entry.get("value_type"), entry.get("value"), entry.get("unit"),
    )


def _validate_finding(
    entry: Any, note_text: str, digest: str, note_id: str, seen: set, overflow: bool,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "term": None, "status": STATUS_FAILED_CHECK, "assertion": None,
        "experiencer": None, "temporality": None, "value_type": None,
        "value": None, "unit": None, "unit_code": None, "unit_code_status": None,
        "evidence": None, "contradiction": False, "failures": [],
    }
    if overflow:
        record["failures"].append(CHECK_FINDING_CAP)
        return record
    if not isinstance(entry, dict) or set(entry) != FINDING_KEYS:
        record["failures"].append(CHECK_SCHEMA)
        return record
    term = entry.get("term")
    if not isinstance(term, str) or not term.strip() or len(term) > MAX_TERM_CHARS:
        record["failures"].append(CHECK_TYPE)
        return record
    record["term"] = term
    for key, allowed in (
        ("assertion", ASSERTIONS), ("experiencer", EXPERIENCERS),
        ("temporality", TEMPORALITIES), ("value_type", VALUE_TYPES),
    ):
        if entry.get(key) not in allowed:
            record["failures"].append(CHECK_ENUM)
            return record
        record[key] = entry[key]
    quantity_failure = _quantity_failure(entry)
    if quantity_failure is not None:
        record["failures"].append(quantity_failure)
        return record
    record["value"] = entry["value"]
    record["unit"] = entry["unit"]
    span = check_evidence(note_text, entry.get("evidence"))
    record["span_checks"] = span
    record["failures"].extend(span["failures"])
    if span["failures"]:
        return record
    signature = _finding_signature(entry)
    if signature in seen:
        record["failures"].append(CHECK_DUPLICATE)
        return record
    seen.add(signature)
    record["status"] = STATUS_EXTRACTED
    record["evidence"] = _bound_evidence(entry["evidence"], span, digest, note_id)
    return record


def _mark_contradictions(findings: List[Dict[str, Any]]) -> None:
    groups: Dict[tuple, set] = {}
    for record in findings:
        if record["status"] != STATUS_EXTRACTED:
            continue
        key = (normalize_term(record["term"]), record["experiencer"])
        groups.setdefault(key, set()).add((record["assertion"], record["temporality"]))
    for record in findings:
        if record["status"] != STATUS_EXTRACTED:
            continue
        key = (normalize_term(record["term"]), record["experiencer"])
        if len(groups.get(key, ())) > 1:
            record["contradiction"] = True


def validate_extraction(
    note_text: str,
    note_id: str,
    digest: str,
    payload: Any,
    *,
    response_chars: int = 0,
) -> ValidationResult:
    if response_chars > MAX_RESPONSE_CHARS:
        return _rejected([CHECK_OVERSIZE])
    if not isinstance(payload, dict):
        return _rejected([CHECK_SCHEMA])
    if set(payload) != TOP_LEVEL_KEYS:
        return _rejected([CHECK_UNKNOWN_KEY, CHECK_SCHEMA])
    if payload.get("schema_version") != SCHEMA_VERSION:
        return _rejected([CHECK_SCHEMA_VERSION])
    if payload.get("note_id") != note_id:
        return _rejected([CHECK_NOTE_ID])
    if payload.get("note_digest") != digest:
        return _rejected([CHECK_DIGEST])
    result = ValidationResult(OUTCOME_VALIDATED)
    for name in SCALAR_FIELDS:
        result.scalars[name] = _validate_scalar(
            name, payload.get(name), note_text, digest, note_id)
    findings_slot = payload.get("findings")
    if not isinstance(findings_slot, dict) or set(findings_slot) != FINDINGS_KEYS:
        result.scalars["findings"] = {
            "field": "findings", "status": STATUS_FAILED_CHECK, "entries": [],
            "abstention_reason": None, "failures": [CHECK_SCHEMA],
        }
        return result
    entries = findings_slot.get("entries")
    if not isinstance(entries, list):
        result.scalars["findings"] = {
            "field": "findings", "status": STATUS_FAILED_CHECK, "entries": [],
            "abstention_reason": None, "failures": [CHECK_SCHEMA],
        }
        return result
    if not entries:
        reason = findings_slot.get("abstention_reason")
        if reason in ABSTENTION_REASONS:
            result.scalars["findings"] = {
                "field": "findings", "status": STATUS_ABSENT, "entries": [],
                "abstention_reason": reason, "absence_basis": ABSENCE_BASIS, "failures": [],
            }
        else:
            result.scalars["findings"] = {
                "field": "findings", "status": STATUS_FAILED_CHECK, "entries": [],
                "abstention_reason": None,
                "failures": [CHECK_ABSTENTION_CONFLICT, CHECK_ENUM],
            }
        return result
    if findings_slot.get("abstention_reason") is not None:
        result.scalars["findings"] = {
            "field": "findings", "status": STATUS_FAILED_CHECK, "entries": [],
            "abstention_reason": None, "failures": [CHECK_ABSTENTION_CONFLICT],
        }
        return result
    seen: set = set()
    for index, entry in enumerate(entries):
        result.findings.append(
            _validate_finding(entry, note_text, digest, note_id, seen, index >= MAX_FINDINGS))
    _mark_contradictions(result.findings)
    return result
