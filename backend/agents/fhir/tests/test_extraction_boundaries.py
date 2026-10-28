"""Checks untrusted extraction JSON and overlapping evidence ambiguity.
These deterministic denials do not establish clinical correctness or provider qualification.
"""

import pytest

from agents.fhir.extraction import _parse_response
from agents.fhir.extraction_schema import CHECK_SPAN_AMBIGUOUS
from agents.fhir.extraction_validate import check_evidence


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
