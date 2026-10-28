"""Checks untrusted extraction JSON and overlapping evidence ambiguity.
These deterministic denials do not establish clinical correctness or provider qualification.
"""

import json

import pytest

from agents.fhir import extraction
from agents.fhir.extraction import _parse_response, extract_note_fields
from agents.fhir.extraction_schema import CHECK_SPAN_AMBIGUOUS
from agents.fhir.extraction_validate import check_evidence, sha256_hex
from agents.fhir.tests.test_extraction import NOTE, NOTE_ID, golden_payload, make_fake_openai


@pytest.mark.parametrize("note,quote", [("aaaa", "aaa"), ("ababa", "aba"), ("ééé", "éé")])
def test_overlapping_quotes_are_ambiguous(note, quote):
    checked = check_evidence(note, {"start": 0, "end": len(quote), "quote": quote})
    assert checked["verbatim"] is True
    assert checked["unique"] is False
    assert checked["failures"] == [CHECK_SPAN_AMBIGUOUS]


@pytest.mark.parametrize(
    "raw",
    [
        '{"note_id":"first","note_id":"second"}',
        '{"field":{"value":null,"value":"second"}}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":1e999}',
        '{"value":' + '[' * 20 + '0' + ']' * 20 + '}',
        '{"value":' + '[' * 1500 + '0' + ']' * 1500 + '}',
    ],
)
def test_noncanonical_or_excessively_nested_json_is_refused(raw):
    assert _parse_response(raw) is None


def test_bounded_finite_json_remains_supported():
    assert _parse_response('{"value":[1.25,true,null]}') == {"value": [1.25, True, None]}


def test_accepted_note_preserves_exact_text_digest_and_offsets(monkeypatch):
    monkeypatch.setitem(extraction.flags._flags, "clinical_note_extract", True)
    note = " \n" + NOTE + "\t"
    provider, _ = make_fake_openai([json.dumps(golden_payload(note, NOTE_ID))])
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda _: (provider(), "m"))
    data = extract_note_fields(note_text=note, note_id=NOTE_ID)["_data"]
    assert data["outcome"] == "extracted"
    assert data["note"]["digest"] == sha256_hex(note)
    assert data["note"]["length"] == len(note)
    evidence = data["fields"]["patient_ref"]["evidence"]
    assert evidence["start"] == note.index("DEMO-1001")
    assert note[evidence["start"] : evidence["end"]] == evidence["quote"]


@pytest.mark.parametrize("note,note_id", [("\ud800", NOTE_ID), (NOTE, NOTE_ID + "\n")])
def test_invalid_note_encoding_or_identifier_is_refused_before_provider(note, note_id, monkeypatch):
    monkeypatch.setitem(extraction.flags._flags, "clinical_note_extract", True)
    calls = []
    monkeypatch.setattr(extraction, "_resolve_llm_client", lambda _: (calls.append(True), "m"))
    result = extract_note_fields(note_text=note, note_id=note_id)
    assert result["_error"]["code"] == "EXTRACT_BAD_REQUEST"
    assert calls == []
