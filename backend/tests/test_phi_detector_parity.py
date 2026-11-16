"""Proves the portable PHI detector floor flags every input that today's Python rules flag, by running the live prefilter, sharing identifiers with their decoding, redactor value patterns and redactor field keys over the contract's vectors and corpus.
It reads byte copies of the contract's vectors and rule table from fixtures/phi_detector, so it needs neither Presidio nor the specs folder, and it checks those copies against the contract when the folder is present.
It also proves the contract's lookahead-free labeled identifier pattern matches the inputs today's lookahead pattern matches."""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest

from personalization.phi_gate import _PREFILTER_PATTERNS, _SHARE_IDENTIFIERS, PHIGate
from shared.phi_redactor import PHI_FIELD_PATTERNS, PHI_VALUE_PATTERNS, _mask_value, _matches_phi_field_key

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "phi_detector"
CONTRACT = Path(__file__).resolve().parents[2] / "specs" / "106-sign-in-free-clients" / "contracts"
COPIES = {
    "phi-detector-v1.json": CONTRACT / "vectors" / "phi-detector-v1.json",
    "phi_rules.json": CONTRACT / "reference" / "phi_rules.json",
}
VECTORS = json.loads((FIXTURES / "phi-detector-v1.json").read_text(encoding="utf-8"))
RULES = {rule["id"]: rule for rule in json.loads((FIXTURES / "phi_rules.json").read_text(encoding="utf-8"))["rules"]}
EXEMPT = set(VECTORS["constants"]["token_rule_differences"])


class _NoFindings:
    def analyze(self, text: str, language: str, entities: list[str], score_threshold: float) -> list[Any]:
        return []


GATE = PHIGate(analyzer=_NoFindings())


def expand(case: dict[str, Any]) -> Any:
    if "text" in case:
        return case["text"]
    if "json" in case:
        return case["json"]
    if "text_repeat" in case:
        spec = case["text_repeat"]
        return spec["unit"] * spec["count"] + spec["tail"]
    spec = case["json_repeat"]
    return [spec["item"]] * spec["count"]


CASES = [(case["id"], expand(case), case["expect"]["verdict"]) for case in VECTORS["detect"]]
CASES += [(case["id"], expand(case), case["verdict"]) for case in VECTORS["corpus"]]


def text_flags(text: str) -> bool:
    if GATE.contains_phi(text) or GATE.contains_phi_for_sharing(text):
        return True
    return any(pattern.search(text) for pattern, _ in PHI_VALUE_PATTERNS)


def key_flags(key: str, value: Any, fields: tuple[str, ...] | None = None) -> bool:
    if fields is None:
        hit = _matches_phi_field_key(key)
    else:
        hit = any(field in key.lower() for field in fields)
    return hit and _mask_value(value) != value


def value_flags(value: Any, fields: tuple[str, ...] | None = None) -> bool:
    if isinstance(value, str):
        return text_flags(value)
    if isinstance(value, dict):
        return any(
            key_flags(key, child, fields) or text_flags(key) or value_flags(child, fields)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(value_flags(child, fields) for child in value)
    return False


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, child in value.items() for text in [key, *strings(child)]]
    if isinstance(value, list):
        return [text for child in value for text in strings(child)]
    return []


def test_parity_runs_over_vectors_and_a_corpus_of_at_least_5000_inputs() -> None:
    assert len(VECTORS["corpus"]) >= 5000
    assert len({case_id for case_id, _, _ in CASES}) == len(CASES)


def test_every_input_today_flags_is_phi_or_uncertain_in_the_floor() -> None:
    misses = [
        case_id
        for case_id, value, verdict in CASES
        if case_id not in EXEMPT and verdict == "none" and value_flags(value)
    ]
    assert misses == []


def test_each_listed_difference_is_a_generic_field_key_alone() -> None:
    phi_key = re.compile(RULES["field_key_phi"]["pattern"], re.ASCII)
    specific = tuple(field for field in PHI_FIELD_PATTERNS if phi_key.fullmatch(field))
    generic = tuple(field for field in PHI_FIELD_PATTERNS if field not in specific)
    assert len(specific) == 11
    assert RULES["field_key_phi_blank"]["pattern"] == RULES["field_key_phi"]["pattern"]
    by_id = {case_id: (value, verdict) for case_id, value, verdict in CASES}
    assert EXEMPT <= set(by_id)
    for case_id in sorted(EXEMPT):
        value, verdict = by_id[case_id]
        assert verdict == "none", case_id
        assert value_flags(value), case_id
        assert not value_flags(value, specific), case_id
        assert value_flags(value, generic), case_id


def test_the_corpus_exercises_every_pattern_of_today() -> None:
    texts = [text for _, value, _ in CASES for text in strings(value) if len(text) < 65536]
    keys = [key for _, value, _ in CASES if isinstance(value, dict) for key in value]
    for pattern in _PREFILTER_PATTERNS:
        assert any(pattern.search(text) for text in texts), pattern.pattern
    for pattern, _ in PHI_VALUE_PATTERNS:
        assert any(pattern.search(text) for text in texts), pattern.pattern
    for alternative in top_level_alternatives(_SHARE_IDENTIFIERS.pattern):
        compiled = re.compile(alternative, re.IGNORECASE)
        assert any(compiled.search(text) for text in texts), alternative
    for field in PHI_FIELD_PATTERNS:
        assert any(field in key.lower() for key in keys), field


def top_level_alternatives(pattern: str) -> list[str]:
    parts, depth, in_class, escaped, current = [], 0, False, False, ""
    for char in pattern:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return parts


ORIGINAL = re.compile(
    next(part for part in top_level_alternatives(_SHARE_IDENTIFIERS.pattern) if "(?=" in part),
    re.IGNORECASE | re.ASCII,
)
REWRITE = re.compile(RULES["labeled_id"]["pattern"], re.ASCII)


def same_matches(text: str) -> bool:
    old = [(m.start(), m.end()) for m in ORIGINAL.finditer(text)]
    new = [(m.start(), m.end()) for m in REWRITE.finditer(text)]
    if [start for start, _ in old] != [start for start, _ in new]:
        return False
    return all(
        old_end == new_end or (new_end > old_end and text[new_end - 1] == "_")
        for (_, old_end), (_, new_end) in zip(old, new, strict=True)
    )


def test_lookahead_rewrite_matches_the_original_on_the_corpus() -> None:
    texts = [text for _, value, _ in CASES for text in strings(value) if len(text) < 65536]
    checked = 0
    for text in texts:
        assert same_matches(text), text
        checked += bool(ORIGINAL.search(text))
    assert checked >= 100


def test_lookahead_rewrite_matches_the_original_on_every_short_identifier() -> None:
    for label in ("MRN: ", "insurance id ", "SSN=", "patient id:-"):
        for size in range(8):
            for parts in itertools.product("A1-_ ", repeat=size):
                assert same_matches(label + "".join(parts)), label + "".join(parts)


def test_the_original_needs_the_digit_past_the_token() -> None:
    assert ORIGINAL.search("MRN: ABC-123_x").group() == "MRN: ABC-"
    assert REWRITE.search("MRN: ABC-123_x").group() == "MRN: ABC-123_"
    assert ORIGINAL.search("MRN: ABC-DEF") is None
    assert REWRITE.search("MRN: ABC-DEF") is None


@pytest.mark.skipif(not CONTRACT.is_dir(), reason="the specs folder is not in this checkout")
@pytest.mark.parametrize("name", sorted(COPIES))
def test_fixture_copies_match_the_contract(name: str) -> None:
    assert (FIXTURES / name).read_bytes() == COPIES[name].read_bytes()
