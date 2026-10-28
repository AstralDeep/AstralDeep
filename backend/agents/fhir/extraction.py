"""Implements the read-only extract_note_fields pilot tool: bounded note text in,
one model proposal over the owner's configured provider, deterministic validation,
and an authorized card plus structured result out. Disabled unless the
clinical_note_extract flag is on; never writes FHIR records and never logs note
text or extracted values.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from astralprims import Alert, Card, Table, Text
from openai import OpenAI

from shared.feature_flags import flags
from shared.llm_text import strip_reasoning_markup

from agents.fhir.client import FhirError
from agents.fhir.extraction_prompts import build_messages
from agents.fhir.extraction_schema import (
    CHECK_OVERSIZE,
    MAX_NOTE_CHARS,
    MAX_NOTE_ID_CHARS,
    MAX_RESPONSE_CHARS,
    MIN_NOTE_CHARS,
    NOTE_ID_PATTERN,
    OUTCOME_ABSENT,
    OUTCOME_EXTRACTED,
    OUTCOME_NO_VALID_EXTRACTION,
    OUTCOME_PARTIAL,
    OUTCOME_REJECTED,
    SCHEMA_VERSION,
    STATUS_ABSENT,
    STATUS_EXTRACTED,
    STATUS_FAILED_CHECK,
    STATUS_NOT_EVALUATED,
    UNIT_CODES,
)
from agents.fhir.extraction_validate import (
    ValidationResult,
    sha256_hex,
    validate_extraction,
)

logger = logging.getLogger("FhirExtraction")

PROVIDER_TIMEOUT_SECONDS = 60
LLM_MODEL_DEFAULT = "gpt-4o"

FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)

OUTCOME_MESSAGES = {
    OUTCOME_EXTRACTED: "All proposed fields passed deterministic checks.",
    OUTCOME_PARTIAL: "Some proposed fields failed deterministic checks and were withheld.",
    OUTCOME_ABSENT: "The model abstained on every field; absence is model-asserted, not verified.",
    OUTCOME_NO_VALID_EXTRACTION: "No proposed field passed the deterministic checks.",
    OUTCOME_REJECTED: "The proposal was rejected before field evaluation.",
}


def _resolve_llm_client(kwargs: Dict[str, Any]) -> Tuple[Optional[OpenAI], str]:
    session = kwargs.get("_session_llm_credentials") or {}
    creds = kwargs.get("_credentials", {}) or {}
    api_key = (
        session.get("OPENAI_API_KEY")
        or (creds.get("OPENAI_API_KEY") if not kwargs.get("_credentials_encrypted") else None)
    )
    base_url = (
        session.get("OPENAI_BASE_URL")
        or (creds.get("OPENAI_BASE_URL") if not kwargs.get("_credentials_encrypted") else None)
    )
    model = session.get("LLM_MODEL") or LLM_MODEL_DEFAULT
    if not api_key:
        return None, model
    return OpenAI(api_key=api_key, base_url=base_url), model


def _parse_response(raw: str) -> Any:
    if type(raw) is not str or len(raw) > MAX_RESPONSE_CHARS:
        return None
    cleaned = (raw or "").strip()
    fence = FENCE.match(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    cleaned = strip_reasoning_markup(cleaned).strip()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
        return result

    try:
        result = json.loads(cleaned, object_pairs_hook=pairs, parse_constant=nonfinite,
                            parse_float=finite_float)
        pending = [(result, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 16:
                return None
            if type(value) is dict:
                pending.extend((item, depth + 1) for item in value.values())
            elif type(value) is list:
                pending.extend((item, depth + 1) for item in value)
        return result
    except (ValueError, RecursionError, OverflowError):
        return None


def _unit_resolution(unit: Any) -> Tuple[Optional[str], str]:
    if not isinstance(unit, str) or unit not in UNIT_CODES:
        return None, "unvalidated"
    return UNIT_CODES[unit], "validated"


def _attach_unit_codes(result: ValidationResult) -> None:
    for record in result.findings:
        if record["status"] != STATUS_EXTRACTED:
            continue
        code, status = _unit_resolution(record.get("unit"))
        record["unit_code"] = code
        record["unit_code_status"] = status


def _field_outcome(result: ValidationResult) -> str:
    counts = result.counts()
    if counts[STATUS_EXTRACTED] and counts[STATUS_FAILED_CHECK]:
        return OUTCOME_PARTIAL
    if counts[STATUS_EXTRACTED]:
        return OUTCOME_EXTRACTED
    if counts[STATUS_FAILED_CHECK] or counts[STATUS_NOT_EVALUATED]:
        return OUTCOME_NO_VALID_EXTRACTION
    return OUTCOME_ABSENT


def _identity_row(note_id: str, digest: str, length: int) -> Dict[str, Any]:
    return {"note_id": note_id, "digest": digest, "length": length}


def _rows_for_scalars(result: ValidationResult) -> List[List[str]]:
    rows = []
    for name in ("patient_ref", "note_date"):
        record = result.scalars[name]
        value = str(record.get("value")) if record["status"] == STATUS_EXTRACTED else "—"
        if record["status"] == STATUS_ABSENT:
            value = f"not documented ({record.get('abstention_reason')})"
        rows.append([name, record["status"], value, ", ".join(record["failures"]) or "—"])
    return rows


def _finding_rows(result: ValidationResult) -> List[List[str]]:
    rows = []
    for record in result.findings:
        term = record.get("term") or "—"
        value = "—"
        if record["status"] == STATUS_EXTRACTED and record.get("value") is not None:
            value = f"{record['value']} {record.get('unit')}"
        rows.append([
            term, record.get("assertion") or "—", record.get("experiencer") or "—",
            record.get("temporality") or "—", value, record["status"],
            "contradiction" if record.get("contradiction") else "—",
            ", ".join(record["failures"]) or "—",
        ])
    return rows or [["—", "—", "—", "—", "—", "—", "—", "—"]]


def _card(result: ValidationResult, outcome: str, identity: Dict[str, Any]) -> Card:
    findings_note = (
        "Values are model proposals held to verbatim unique source spans; review the note "
        "before any clinical use. This pilot extracts text only and makes no clinical decision."
    )
    content: List[Any] = [
        Alert(
            message=OUTCOME_MESSAGES.get(outcome, OUTCOME_MESSAGES[OUTCOME_NO_VALID_EXTRACTION]),
            variant="info" if outcome == OUTCOME_EXTRACTED else "warning",
            title="Extraction outcome",
        ),
        Text(
            content=(
                f"note_id: {identity['note_id'] or '(unlabeled)'} · "
                f"digest {identity['digest'][:12]}… · {identity['length']} characters"
            ),
            variant="caption",
        ),
        Table(
            headers=["Field", "Status", "Value", "Failed checks"],
            rows=_rows_for_scalars(result),
            id="fhir-extract-scalars",
        ),
        Table(
            headers=["Finding", "Assertion", "Experiencer", "Temporality", "Value", "Status", "Conflict", "Failed checks"],
            rows=_finding_rows(result),
            id="fhir-extract-findings",
        ),
        Text(content=findings_note, variant="caption"),
    ]
    return Card(title="Note field extraction (pilot)", id="fhir-note-extract", content=content)


def _public_record(record: Dict[str, Any]) -> Dict[str, Any]:
    public = dict(record)
    if record.get("status") != STATUS_EXTRACTED:
        for key in ("value", "unit", "unit_code", "unit_code_status", "evidence"):
            public[key] = None
    return public


def _assemble(result: ValidationResult, note_id: str, digest: str, length: int) -> Dict[str, Any]:
    outcome = OUTCOME_REJECTED if result.outcome == OUTCOME_REJECTED else _field_outcome(result)
    identity = _identity_row(note_id, digest, length)
    data = {
        "operation": "extract_note_fields",
        "schema_version": SCHEMA_VERSION,
        "outcome": outcome,
        "reasons": list(result.reasons),
        "note": identity,
        "fields": {
            "patient_ref": _public_record(result.scalars["patient_ref"]),
            "note_date": _public_record(result.scalars["note_date"]),
            "findings": [_public_record(record) for record in result.findings],
        },
        "findings_field": (
            _public_record(result.scalars["findings"]) if "findings" in result.scalars else None
        ),
        "counts": result.counts(),
        "disclaimer": (
            "Model-proposed fields verified against verbatim note spans only; semantic "
            "correctness requires clinician review. Not a clinical decision."
        ),
    }
    return {"_ui_components": [_card(result, outcome, identity).to_dict()], "_data": data}


def _finish(note_text: str, note_id: str, digest: str, raw: str, payload: Any) -> Dict[str, Any]:
    if len(raw) > MAX_RESPONSE_CHARS:
        result = validate_extraction(note_text, note_id, digest, None)
        result.reasons = [CHECK_OVERSIZE]
        for record in result.records:
            record["failures"] = [CHECK_OVERSIZE]
    else:
        result = validate_extraction(note_text, note_id, digest, payload, response_chars=len(raw))
    _attach_unit_codes(result)
    counts = result.counts()
    logger.info(
        "note extraction outcome=%s extracted=%d absent=%d failed=%d unevaluated=%d",
        result.outcome, counts[STATUS_EXTRACTED], counts[STATUS_ABSENT],
        counts[STATUS_FAILED_CHECK], counts[STATUS_NOT_EVALUATED])
    return _assemble(result, note_id, digest, len(note_text))


def evaluate_proposal(note_text: str, note_id: str, response: Any) -> Dict[str, Any]:
    digest = sha256_hex(note_text)
    if isinstance(response, str):
        payload = _parse_response(response)
        return _finish(note_text, note_id, digest, response, payload)
    raw = json.dumps(response)
    return _finish(note_text, note_id, digest, raw, response)


def _extract(note_text: str, note_id: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    if not flags.is_enabled("clinical_note_extract"):
        raise FhirError(
            "EXTRACT_DISABLED",
            "Note extraction pilot is disabled; enable the clinical_note_extract flag first")
    if not isinstance(note_text, str) or not isinstance(note_id, str):
        raise FhirError("EXTRACT_BAD_REQUEST", "note_text and note_id must be strings")
    note_text = note_text.strip()
    note_id = note_id.strip()
    if not MIN_NOTE_CHARS <= len(note_text) <= MAX_NOTE_CHARS:
        raise FhirError(
            "EXTRACT_BAD_REQUEST",
            f"A clinical note between {MIN_NOTE_CHARS} and {MAX_NOTE_CHARS} characters is required")
    if len(note_id) > MAX_NOTE_ID_CHARS or (note_id and not NOTE_ID_PATTERN.match(note_id)):
        raise FhirError(
            "EXTRACT_BAD_REQUEST",
            "note_id must be a short label of letters, digits, dots, dashes, slashes, colons or spaces")
    digest = sha256_hex(note_text)
    client, model = _resolve_llm_client(kwargs)
    if client is None:
        raise FhirError(
            "EXTRACT_LLM_UNAVAILABLE",
            "No LLM credentials are configured for this session, so the note could not be extracted",
            retryable=True)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=build_messages(note_text, note_id, digest),
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )
        raw = response.choices[0].message.content or ""
    except Exception:  # noqa: BLE001
        logger.info("note extraction provider call failed outcome=%s", "provider_error")
        raise FhirError(
            "EXTRACT_PROVIDER_FAILED",
            "The configured model provider failed or timed out during extraction",
            retryable=True)
    return _finish(note_text, note_id, digest, raw, _parse_response(raw) if len(raw) <= MAX_RESPONSE_CHARS else None)


def extract_note_fields(note_text: str = "", note_id: str = "", **kwargs: Any) -> Dict[str, Any]:
    try:
        return _extract(note_text, note_id, kwargs)
    except FhirError as error:
        return error.as_tool_error()
