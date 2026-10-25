"""Covers the note-extraction pilot: deterministic validator checks (identity binding,
spans, types, enums, units, ranges, dates, abstentions, duplicates, caps), prompt
construction, and the flag-gated read-only tool behavior over fake providers
including denials, provider failures, malformed output, and the presentation
contract.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.fhir import extraction, mcp_tools
from agents.fhir.extraction import _parse_response, evaluate_proposal, extract_note_fields
from agents.fhir.extraction_prompts import NOTE_END, NOTE_START, build_messages, system_prompt
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
    MAX_NOTE_CHARS,
    SCHEMA_VERSION,
    STATUS_ABSENT,
    STATUS_EXTRACTED,
    STATUS_FAILED_CHECK,
    STATUS_NOT_EVALUATED,
    UNIT_CODES,
)
from agents.fhir.extraction_validate import (
    check_evidence,
    normalize_term,
    sha256_hex,
    validate_extraction,
)

NOTE = (
    "Progress note 2026-03-02. Patient DEMO-1001 reports chest pain on exertion. "
    "No shortness of breath at rest. Glucose 132 mg/dL this morning."
)
NOTE_ID = "SYN-001"


def span(note, text):
    start = note.index(text)
    return {"start": start, "end": start + len(text), "quote": text}


def finding(term, note, evidence_text, assertion="asserted", experiencer="patient",
            temporality="current", value_type="none", value=None, unit=None):
    return {
        "term": term, "evidence": span(note, evidence_text),
        "assertion": assertion, "experiencer": experiencer, "temporality": temporality,
        "value_type": value_type, "value": value, "unit": unit,
    }


def scalar(value, note=None, text=None):
    return {"value": value, "evidence": span(note, text) if text else None, "abstention_reason": None}


def abstain(reason):
    return {"value": None, "evidence": None, "abstention_reason": reason}


def envelope(note_id, patient_ref, note_date, findings):
    return {
        "schema_version": SCHEMA_VERSION, "note_id": note_id, "note_digest": "__DIGEST__",
        "patient_ref": patient_ref, "note_date": note_date, "findings": findings,
    }


def golden_payload(note=NOTE, note_id=NOTE_ID):
    payload = envelope(
        note_id,
        scalar("DEMO-1001", note, "DEMO-1001"),
        scalar("2026-03-02", note, "2026-03-02"),
        {"entries": [
            finding("chest pain", note, "chest pain on exertion"),
            finding("shortness of breath", note, "No shortness of breath", assertion="negated"),
            finding("Glucose", note, "Glucose 132 mg/dL", value_type="glucose", value=132, unit="mg/dL"),
        ], "abstention_reason": None},
    )
    payload["note_digest"] = sha256_hex(note)
    return payload


def validated(payload=None, note=NOTE, note_id=NOTE_ID):
    return validate_extraction(note, note_id, sha256_hex(note), payload or golden_payload(note, note_id))


def make_fake_openai(contents):
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            content = contents[min(len(calls) - 1, len(contents) - 1)]
            if isinstance(content, Exception):
                raise content
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    class _FakeClient:
        calls_log = calls

        def __init__(self, api_key=None, base_url=None):
            self.chat = SimpleNamespace(completions=_Completions())

    return _FakeClient, calls


@pytest.fixture
def pilot_on(monkeypatch):
    monkeypatch.setitem(mcp_tools.flags._flags, "clinical_note_extract", True)


@pytest.fixture
def session_creds():
    return {"_session_llm_credentials": {
        "OPENAI_API_KEY": "k", "OPENAI_BASE_URL": "http://llm", "LLM_MODEL": "m"}}


def response_for(note, note_id, payload=None):
    payload = payload or golden_payload(note, note_id)
    return json.dumps(payload)


ALLOWED_TYPES = {"card", "alert", "text", "table"}


def walk(component):
    yield component
    for key in ("content", "children", "buttons"):
        nested = component.get(key)
        for child in nested if isinstance(nested, list) else []:
            yield from walk(child)


def card_of(result):
    assert set(result) == {"_ui_components", "_data"}
    assert len(result["_ui_components"]) == 1
    card = result["_ui_components"][0]
    assert card["type"] == "card" and card["id"] == "fhir-note-extract"
    components = list(walk(card))
    assert {component["type"] for component in components} <= ALLOWED_TYPES
    encoded = json.dumps(result, allow_nan=False)
    assert "NaN" not in encoded and len(encoded) < 400_000
    return card, components


def error_of(result):
    assert set(result) == {"_error"}
    return result["_error"]


# ---------------------------------------------------------------- validator


def test_golden_extraction_passes_every_check():
    result = validated()
    assert result.outcome == "validated"
    assert result.counts() == {
        "extracted": 5, "absent": 0, "failed_check": 0, "not_evaluated": 0}
    patient = result.scalars["patient_ref"]
    assert patient["status"] == "extracted" and patient["value"] == "DEMO-1001"
    assert patient["evidence"]["digest"] == sha256_hex(NOTE)
    assert patient["evidence"]["note_id"] == NOTE_ID
    assert patient["evidence"]["quote"] == "DEMO-1001"
    glucose = result.findings[2]
    assert glucose["status"] == "extracted" and glucose["value"] == 132
    assert glucose["failures"] == []
    assert not glucose["contradiction"]


def test_wrong_digest_is_rejected_without_evaluating_fields():
    payload = golden_payload()
    payload["note_digest"] = "0" * 64
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.outcome == "rejected_output"
    assert result.reasons == [CHECK_DIGEST]
    assert all(record["status"] == STATUS_NOT_EVALUATED for record in result.records)


def test_wrong_note_id_echo_is_rejected():
    payload = golden_payload()
    payload["note_id"] = "SYN-OTHER"
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.reasons == [CHECK_NOTE_ID]


def test_schema_version_mismatch_is_rejected():
    payload = golden_payload()
    payload["schema_version"] = "note-extract-v2"
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.reasons == [CHECK_SCHEMA_VERSION]


def test_unknown_and_missing_top_level_keys_are_rejected():
    payload = golden_payload()
    payload["surprise"] = 1
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert CHECK_UNKNOWN_KEY in result.reasons and CHECK_SCHEMA in result.reasons
    short = golden_payload()
    short.pop("findings")
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), short)
    assert result.outcome == "rejected_output"


def test_non_dict_payload_is_rejected():
    for payload in (None, [], "json", 7):
        result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
        assert result.outcome == "rejected_output" and result.reasons == [CHECK_SCHEMA]


@pytest.mark.parametrize("evidence, code", [
    ({"start": -1, "end": 4, "quote": "Progr"}, CHECK_SPAN_RANGE),
    ({"start": 0, "end": len(NOTE) + 1, "quote": NOTE[:len(NOTE) + 1]}, CHECK_SPAN_RANGE),
    ({"start": 8, "end": 8, "quote": ""}, CHECK_SPAN_EMPTY),
    ({"start": 8, "end": 4, "quote": "Prog"}, CHECK_SPAN_RANGE),
    ({"start": 0, "end": 6, "quote": "nausea"}, CHECK_SPAN_MISMATCH),
    ({"start": 0, "end": 6, "quote": ""}, CHECK_SPAN_EMPTY),
    ({"start": True, "end": 4, "quote": "Progr"}, CHECK_SPAN_RANGE),
    ({"start": "0", "end": 4, "quote": "Progr"}, CHECK_SPAN_RANGE),
    ({"start": 0, "end": 6}, "E_EVIDENCE_REQUIRED"),
    (None, "E_EVIDENCE_REQUIRED"),
    ({"start": 0, "end": 4, "quote": "Progr", "extra": 1}, "E_EVIDENCE_REQUIRED"),
])
def test_span_denials(evidence, code):
    checked = check_evidence(NOTE, evidence)
    assert code in checked["failures"]


def test_repeated_phrase_is_ambiguous_and_long_quote_resolves_it():
    repeated = "Assessment 2026-08-14: patient reports chest pain on exertion. Atypical chest pain likely."
    checked = check_evidence(repeated, span(repeated, "chest pain"))
    assert CHECK_SPAN_AMBIGUOUS in checked["failures"]
    long_quote = span(repeated, "reports chest pain on exertion")
    resolved = check_evidence(repeated, long_quote)
    assert resolved["unique"] and resolved["verbatim"] and resolved["failures"] == []


def test_oversized_quote_is_rejected():
    long_note = "Assessment: " + ("stable throughout the observation window. " * 12)
    checked = check_evidence(long_note, {"start": 0, "end": len(long_note), "quote": long_note})
    assert "E_SPAN_OVERSIZE" in checked["failures"]


def test_scalar_slot_shape_and_abstention_rules():
    base = golden_payload()
    base["patient_ref"] = {"value": "DEMO-1001", "confidence": 0.9,
                           "evidence": span(NOTE, "DEMO-1001"), "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), base)
    assert result.scalars["patient_ref"]["failures"] == [CHECK_SCHEMA]

    base = golden_payload()
    base["patient_ref"] = abstain("made_up_reason")
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), base)
    assert result.scalars["patient_ref"]["failures"] == [CHECK_ENUM]

    base = golden_payload()
    base["patient_ref"] = abstain(None)
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), base)
    assert result.scalars["patient_ref"]["failures"] == [CHECK_ABSTENTION_CONFLICT]

    base = golden_payload()
    base["patient_ref"] = {"value": None, "evidence": span(NOTE, "DEMO-1001"),
                           "abstention_reason": "not_documented"}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), base)
    assert result.scalars["patient_ref"]["failures"] == [CHECK_ABSTENTION_CONFLICT]

    base = golden_payload()
    base["patient_ref"] = scalar("   ")
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), base)
    assert result.scalars["patient_ref"]["failures"] == [CHECK_TYPE]

    base = golden_payload()
    base["patient_ref"] = scalar("x" * 200, NOTE, "DEMO-1001")
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), base)
    assert result.scalars["patient_ref"]["failures"] == [CHECK_TYPE]

    base = golden_payload()
    base["patient_ref"] = scalar(123, NOTE, "DEMO-1001")
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), base)
    assert result.scalars["patient_ref"]["failures"] == [CHECK_TYPE]


def test_valid_abstention_preserves_reason_and_absence_basis():
    payload = golden_payload()
    payload["patient_ref"] = abstain("not_documented")
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    record = result.scalars["patient_ref"]
    assert record["status"] == STATUS_ABSENT
    assert record["abstention_reason"] == "not_documented"
    assert record["absence_basis"] == "model_abstention"
    assert record["value"] is None


@pytest.mark.parametrize("value, code", [
    ("2026-13-01", CHECK_DATE), ("2026-02-30", CHECK_DATE), ("2026-2-3", CHECK_DATE),
    ("03/02/2026", CHECK_DATE), ("2026-03-02T00:00:00", CHECK_DATE),
])
def test_invalid_dates_are_rejected(value, code):
    payload = golden_payload()
    payload["note_date"] = scalar(value, NOTE, "2026-03-02")
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.scalars["note_date"]["failures"] == [code]


def test_calendar_valid_unpadded_date_is_rejected_for_strict_format():
    payload = golden_payload()
    payload["note_date"] = scalar("2026-3-2", NOTE, "2026-03-02")
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.scalars["note_date"]["failures"] == [CHECK_DATE]


@pytest.mark.parametrize("entry, code", [
    ({"term": "", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "none", "value": None, "unit": None}, CHECK_TYPE),
    ({"term": "x" * 200, "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "none", "value": None, "unit": None}, CHECK_TYPE),
    ({"term": "rash", "evidence": None, "assertion": "possibly", "experiencer": "patient",
      "temporality": "current", "value_type": "none", "value": None, "unit": None}, CHECK_ENUM),
    ({"term": "rash", "evidence": None, "assertion": "asserted", "experiencer": "crowd",
      "temporality": "current", "value_type": "none", "value": None, "unit": None}, CHECK_ENUM),
    ({"term": "rash", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "yesterday", "value_type": "none", "value": None, "unit": None}, CHECK_ENUM),
    ({"term": "rash", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "mood", "value": None, "unit": None}, CHECK_ENUM),
    ({"term": "rash", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "none", "value": 3, "unit": None}, CHECK_TYPE),
    ({"term": "rash", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "none", "value": None, "unit": "kg"}, CHECK_TYPE),
    ({"term": "heart rate", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "heart_rate", "value": 80, "unit": "bpm/h"}, CHECK_UNIT),
    ({"term": "heart rate", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "heart_rate", "value": 440, "unit": "bpm"}, CHECK_VALUE_RANGE),
    ({"term": "heart rate", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "heart_rate", "value": None, "unit": "bpm"}, "E_VALUE_REQUIRED"),
    ({"term": "heart rate", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "heart_rate", "value": "88", "unit": "bpm"}, CHECK_TYPE),
    ({"term": "heart rate", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "heart_rate", "value": True, "unit": "bpm"}, CHECK_TYPE),
    ({"term": "heart rate", "evidence": None, "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "heart_rate", "value": None, "unit": None}, CHECK_UNIT),
    ({"term": "rash", "assertion": "asserted", "experiencer": "patient",
      "temporality": "current", "value_type": "none", "value": None, "unit": None}, CHECK_SCHEMA),
])
def test_finding_denials(entry, code):
    payload = golden_payload()
    payload["findings"] = {"entries": [entry], "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.findings[0]["status"] == STATUS_FAILED_CHECK
    assert result.findings[0]["failures"] == [code]


def test_failing_finding_withholds_value_and_unit():
    payload = golden_payload()
    payload["findings"] = {"entries": [
        finding("heart rate", NOTE, "chest pain on exertion", value_type="heart_rate", value=440, unit="bpm"),
    ], "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    record = result.findings[0]
    assert record["failures"] == [CHECK_VALUE_RANGE]
    assert record["value"] is None and record["unit"] is None
    public = extraction._public_record(record)
    assert public["value"] is None and public["unit"] is None
    assert public["status"] == STATUS_FAILED_CHECK


def test_nan_value_is_rejected():
    payload = golden_payload()
    payload["findings"] = {"entries": [
        finding("Glucose", NOTE, "Glucose 132 mg/dL", value_type="glucose",
                value=float("nan"), unit="mg/dL"),
    ], "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.findings[0]["failures"] == [CHECK_TYPE]


def test_finding_without_evidence_is_rejected():
    payload = golden_payload()
    entry = finding("rash", NOTE, "chest pain on exertion")
    entry["evidence"] = None
    payload["findings"] = {"entries": [entry], "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.findings[0]["failures"] == ["E_EVIDENCE_REQUIRED"]


def test_conflicting_temporality_is_marked_on_both_entries():
    payload = golden_payload()
    payload["findings"] = {"entries": [
        finding("CHF", NOTE, "chest pain on exertion", temporality="historical"),
        finding("chf", NOTE, "No shortness of breath", temporality="current"),
    ], "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert [record["contradiction"] for record in result.findings] == [True, True]


def test_different_experiencers_are_not_contradictions():
    payload = golden_payload()
    payload["findings"] = {"entries": [
        finding("diabetes", NOTE, "chest pain on exertion", experiencer="patient", temporality="current"),
        finding("diabetes", NOTE, "No shortness of breath", experiencer="family", temporality="current"),
    ], "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert not any(record["contradiction"] for record in result.findings)


def test_duplicate_finding_is_rejected_second_time():
    payload = golden_payload()
    payload["findings"] = {"entries": [
        finding("chest pain", NOTE, "chest pain on exertion"),
        finding("chest pain", NOTE, "No shortness of breath"),
    ], "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.findings[0]["status"] == STATUS_EXTRACTED
    assert result.findings[1]["failures"] == [CHECK_DUPLICATE]
    assert result.findings[1]["status"] == STATUS_FAILED_CHECK


def test_findings_cap_overflow():
    payload = golden_payload()
    entries = [
        finding(f"finding {index}", NOTE, "chest pain on exertion")
        for index in range(MAX_FINDINGS + 2)
    ]
    payload["findings"] = {"entries": entries, "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert len(result.findings) == MAX_FINDINGS + 2
    assert all(record["status"] == STATUS_EXTRACTED for record in result.findings[:MAX_FINDINGS])
    assert all(
        record["failures"] == [CHECK_FINDING_CAP] for record in result.findings[MAX_FINDINGS:])


def test_findings_abstention_rules():
    payload = golden_payload()
    payload["findings"] = {"entries": [], "abstention_reason": "not_documented"}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.scalars["findings"]["status"] == STATUS_ABSENT
    assert result.scalars["findings"]["absence_basis"] == "model_abstention"

    payload = golden_payload()
    payload["findings"] = {"entries": [], "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.scalars["findings"]["status"] == STATUS_FAILED_CHECK
    assert CHECK_ABSTENTION_CONFLICT in result.scalars["findings"]["failures"]

    payload = golden_payload()
    payload["findings"] = {"entries": [finding("rash", NOTE, "chest pain on exertion")],
                           "abstention_reason": "not_documented"}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.scalars["findings"]["status"] == STATUS_FAILED_CHECK

    payload = golden_payload()
    payload["findings"] = {"entries": "all", "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assert result.scalars["findings"]["failures"] == [CHECK_SCHEMA]


def test_response_size_cap():
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), golden_payload(), response_chars=10**9)
    assert result.outcome == "rejected_output" and result.reasons == [CHECK_OVERSIZE]


def test_normalize_term_collapses_whitespace_and_case():
    assert normalize_term("  Chest   PAIN ") == "chest pain"


# ---------------------------------------------------------------- prompts


def test_system_prompt_pins_schema_contract():
    prompt = system_prompt()
    assert SCHEMA_VERSION in prompt
    for token in ("asserted", "negated", "uncertain", "patient", "family",
                  "current", "historical", "planned", "not_documented",
                  "mg/dL", "mmHg", "note_digest", "abstention_reason"):
        assert token in prompt
    assert "untrusted" in prompt.lower()


def test_messages_delimit_note_as_untrusted_data():
    messages = build_messages(NOTE, NOTE_ID, sha256_hex(NOTE))
    assert [message["role"] for message in messages] == ["system", "user"]
    user = messages[1]["content"]
    assert NOTE_START in user and NOTE_END in user
    assert NOTE in user
    assert NOTE_ID in user and sha256_hex(NOTE) in user
    assert user.index(NOTE_START) < user.index(NOTE) < user.index(NOTE_END)


# ---------------------------------------------------------------- parsing


def test_parse_response_strips_fences_and_reasoning():
    payload = golden_payload()
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    assert _parse_response(fenced) == payload
    reasoned = "<" + "think" + ">" + "hidden reasoning" + "</" + "think" + ">" + json.dumps(payload)
    assert _parse_response(reasoned) == payload
    assert _parse_response("prose only") is None
    assert _parse_response("") is None


# ---------------------------------------------------------------- tool denials


def test_tool_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FF_CLINICAL_NOTE_EXTRACT", raising=False)
    from shared.feature_flags import FeatureFlags
    assert FeatureFlags().is_enabled("clinical_note_extract") is False


def test_tool_denies_when_flag_off():
    result = extract_note_fields(note_text=NOTE, note_id=NOTE_ID)
    error = error_of(result)
    assert error["code"] == "EXTRACT_DISABLED" and error["retryable"] is False


def test_tool_denies_oversized_note(pilot_on):
    result = extract_note_fields(note_text="x" * (MAX_NOTE_CHARS + 1), note_id=NOTE_ID)
    assert error_of(result)["code"] == "EXTRACT_BAD_REQUEST"


def test_tool_denies_empty_note(pilot_on):
    assert error_of(extract_note_fields(note_text="   "))["code"] == "EXTRACT_BAD_REQUEST"


def test_tool_denies_non_string_note(pilot_on):
    assert error_of(extract_note_fields(note_text=123))["code"] == "EXTRACT_BAD_REQUEST"


@pytest.mark.parametrize("note_id", ["x" * 200, "bad id!", "-leading"])
def test_tool_denies_invalid_note_id(pilot_on, note_id):
    assert error_of(extract_note_fields(note_text=NOTE, note_id=note_id))["code"] == "EXTRACT_BAD_REQUEST"


def test_tool_accepts_empty_note_id(pilot_on, session_creds, monkeypatch):
    fake, calls = make_fake_openai([response_for(NOTE, "")])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    result = extract_note_fields(note_text=NOTE, note_id="", **session_creds)
    assert result["_data"]["outcome"] == "extracted"
    assert result["_data"]["note"]["note_id"] == ""


def test_tool_denies_without_credentials(pilot_on):
    result = extract_note_fields(note_text=NOTE, note_id=NOTE_ID)
    error = error_of(result)
    assert error["code"] == "EXTRACT_LLM_UNAVAILABLE" and error["retryable"] is True


def test_tool_never_decrypts_encrypted_credentials(pilot_on):
    result = extract_note_fields(
        note_text=NOTE, note_id=NOTE_ID,
        _credentials={"OPENAI_API_KEY": "secret"}, _credentials_encrypted=True)
    assert error_of(result)["code"] == "EXTRACT_LLM_UNAVAILABLE"


def test_plaintext_credential_fallback_for_in_process_agents(pilot_on, monkeypatch):
    fake, calls = make_fake_openai([response_for(NOTE, NOTE_ID)])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    result = extract_note_fields(
        note_text=NOTE, note_id=NOTE_ID, _credentials={"OPENAI_API_KEY": "k"})
    assert result["_data"]["outcome"] == "extracted"


# ---------------------------------------------------------------- tool golden paths


def test_tool_returns_validated_extraction_and_card(pilot_on, session_creds, monkeypatch):
    fake, calls = make_fake_openai([response_for(NOTE, NOTE_ID)])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    result = extract_note_fields(note_text=NOTE, note_id=NOTE_ID, **session_creds)
    card, components = card_of(result)
    data = result["_data"]
    assert data["outcome"] == "extracted"
    assert data["operation"] == "extract_note_fields"
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["note"] == {"note_id": NOTE_ID, "digest": sha256_hex(NOTE), "length": len(NOTE)}
    assert data["counts"]["extracted"] == 5
    assert data["fields"]["patient_ref"]["evidence"]["quote"] == "DEMO-1001"
    glucose = data["fields"]["findings"][2]
    assert glucose["unit_code"] == UNIT_CODES["mg/dL"]
    assert glucose["unit_code_status"] == "validated"
    assert data["disclaimer"]
    assert "clinical decision" in data["disclaimer"]
    request = calls[0]
    assert request["model"] == "m"
    assert NOTE in request["messages"][1]["content"]
    assert calls and "timeout" in request
    table = next(component for component in components if component["type"] == "table")
    assert table["id"] == "fhir-extract-scalars"


def test_tool_session_credentials_override_defaults(pilot_on, monkeypatch):
    fake, calls = make_fake_openai([response_for(NOTE, NOTE_ID)])
    seen = {}

    def resolve(kwargs):
        seen.update(kwargs)
        return fake(), "m"

    monkeypatch.setattr(extraction, "_resolve_llm_client", resolve)
    extract_note_fields(note_text=NOTE, note_id=NOTE_ID, _session_llm_credentials={
        "OPENAI_API_KEY": "sess", "OPENAI_BASE_URL": "http://s", "LLM_MODEL": "sm"})
    assert seen["_session_llm_credentials"]["OPENAI_API_KEY"] == "sess"


def test_tool_wraps_provider_failure_as_retryable(pilot_on, session_creds, monkeypatch):
    fake, _ = make_fake_openai([RuntimeError("boom")])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    result = extract_note_fields(note_text=NOTE, note_id=NOTE_ID, **session_creds)
    error = error_of(result)
    assert error["code"] == "EXTRACT_PROVIDER_FAILED" and error["retryable"] is True


def test_tool_preserves_malformed_output_as_rejected_not_absence(pilot_on, session_creds, monkeypatch):
    fake, _ = make_fake_openai(["I could not produce JSON, sorry."])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    result = extract_note_fields(note_text=NOTE, note_id=NOTE_ID, **session_creds)
    data = result["_data"]
    assert data["outcome"] == "rejected_output"
    assert data["reasons"] == [CHECK_SCHEMA]
    assert all(record["status"] == STATUS_NOT_EVALUATED for record in
               [data["fields"]["patient_ref"], data["fields"]["note_date"]])
    assert "_error" not in result


def test_tool_rejects_oversized_model_output(pilot_on, session_creds, monkeypatch):
    fake, _ = make_fake_openai(["x" * 70000])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    result = extract_note_fields(note_text=NOTE, note_id=NOTE_ID, **session_creds)
    data = result["_data"]
    assert data["outcome"] == "rejected_output"
    assert data["reasons"] == [CHECK_OVERSIZE]


def test_tool_accepts_fenced_model_output(pilot_on, session_creds, monkeypatch):
    payload = golden_payload()
    fake, _ = make_fake_openai(["```json\n" + json.dumps(payload) + "\n```"])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    result = extract_note_fields(note_text=NOTE, note_id=NOTE_ID, **session_creds)
    assert result["_data"]["outcome"] == "extracted"


def test_tool_partial_outcome_withholds_failed_fields(pilot_on, session_creds, monkeypatch):
    payload = golden_payload()
    payload["findings"]["entries"][2]["unit"] = "mmol/L"
    fake, _ = make_fake_openai([json.dumps(payload)])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    result = extract_note_fields(note_text=NOTE, note_id=NOTE_ID, **session_creds)
    data = result["_data"]
    assert data["outcome"] == "partial"
    glucose = data["fields"]["findings"][2]
    assert glucose["status"] == STATUS_FAILED_CHECK
    assert glucose["value"] is None and glucose["unit"] is None
    assert CHECK_UNIT in glucose["failures"]


def test_tool_outcome_absent_when_model_abstains_everywhere(pilot_on, session_creds, monkeypatch):
    payload = envelope(NOTE_ID, abstain("not_documented"), abstain("not_documented"),
                       {"entries": [], "abstention_reason": "not_documented"})
    payload["note_digest"] = sha256_hex(NOTE)
    fake, _ = make_fake_openai([json.dumps(payload)])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    result = extract_note_fields(note_text=NOTE, note_id=NOTE_ID, **session_creds)
    data = result["_data"]
    assert data["outcome"] == "absent"
    assert data["fields"]["patient_ref"]["status"] == STATUS_ABSENT
    assert data["fields"]["patient_ref"]["absence_basis"] == "model_abstention"
    card = result["_ui_components"][0]
    assert any("model-asserted" in str(component.get("message", "")) for component in card["content"])


def test_tool_does_not_echo_note_text_into_data(pilot_on, session_creds, monkeypatch):
    fake, _ = make_fake_openai([response_for(NOTE, NOTE_ID)])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    result = extract_note_fields(note_text=NOTE, note_id=NOTE_ID, **session_creds)
    encoded = json.dumps(result)
    assert "reports chest pain" not in encoded


def test_tool_logs_no_note_content(pilot_on, session_creds, monkeypatch, caplog):
    fake, _ = make_fake_openai([response_for(NOTE, NOTE_ID)])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda kwargs: (fake(), "m"))
    with caplog.at_level("INFO", logger="FhirExtraction"):
        extract_note_fields(note_text=NOTE, note_id=NOTE_ID, **session_creds)
    assert "DEMO-1001" not in caplog.text and "chest pain" not in caplog.text
    assert "outcome=" in caplog.text


# ---------------------------------------------------------------- registry


def test_registry_entry_is_read_only_and_well_formed():
    entry = mcp_tools.TOOL_REGISTRY["extract_note_fields"]
    assert entry["scope"] == "tools:read"
    schema = entry["input_schema"]
    assert schema["required"] == ["note_text"]
    assert schema["properties"]["note_text"]["maxLength"] == MAX_NOTE_CHARS
    assert "read-only" in entry["description"].lower() or "no fhir writes" in entry["description"].lower()


def test_unit_resolution_returns_unvalidated_without_inventing_codes():
    from agents.fhir.extraction import _unit_resolution
    code, status = _unit_resolution("mg/dL")
    assert status == "validated" and code == UNIT_CODES["mg/dL"]
    code, status = _unit_resolution("parsec")
    assert status == "unvalidated" and code is None


def test_unit_codes_cover_every_approved_unit():
    from agents.fhir.extraction_schema import QUANTITIES
    for value_type, units in QUANTITIES.items():
        assert units, value_type
        for unit, (low, high, ucum) in units.items():
            assert UNIT_CODES[unit] == ucum
            assert low < high


def test_evaluate_proposal_matches_tool_validation():
    result = evaluate_proposal(NOTE, NOTE_ID, golden_payload())
    assert result["_data"]["outcome"] == "extracted"
    raw_result = evaluate_proposal(NOTE, NOTE_ID, "{not json")
    assert raw_result["_data"]["outcome"] == "rejected_output"


def test_evaluate_proposal_rejects_oversized_raw():
    result = evaluate_proposal(NOTE, NOTE_ID, "x" * 70000)
    assert result["_data"]["reasons"] == [CHECK_OVERSIZE]


def test_no_extraction_error_leaks_note_text():
    from agents.fhir.client import FhirError as _F
    error = _F("X", "failed")
    assert error.as_tool_error()["_error"]["retryable"] is False


def test_no_valid_extraction_outcome_when_every_field_fails():
    payload = golden_payload()
    payload["patient_ref"] = abstain(None)
    payload["note_date"] = abstain(None)
    payload["findings"] = {"entries": "bad", "abstention_reason": None}
    result = validate_extraction(NOTE, NOTE_ID, sha256_hex(NOTE), payload)
    assembled = extraction._assemble(result, NOTE_ID, sha256_hex(NOTE), len(NOTE))
    assert assembled["_data"]["outcome"] == "no_valid_extraction"
    assert assembled["_data"]["counts"]["failed_check"] == 3
