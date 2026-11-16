"""Pins every PHI detector vector: each rule with a positive and a negative case, each failure reason, the normalization steps, the corpus verdicts and the on-device filter checks.
It also proves the reference reproduces Python's HTML and percent decoders, that the rule table stays inside the portable dialect, and that a verdict never carries input text.
A failure means phi-detector.md, the committed tables and vectors, and phi_detector.py disagree."""

import html
import itertools
import json
import re
import sys
import unicodedata
import urllib.parse
from pathlib import Path

import phi_detector
import pytest

CONTRACTS = Path(__file__).resolve().parents[2]
VECTORS = CONTRACTS / "vectors" / "phi-detector-v1.json"
DOC = json.loads(VECTORS.read_text(encoding="utf-8"))
DETECT = {case["id"]: case for case in DOC["detect"]}
NORMALIZE = {case["id"]: case for case in DOC["normalize"]}
CHUNKS = {case["id"]: case for case in DOC["filter_chunks"]}
ANSWERS = {case["id"]: case for case in DOC["filter_answer"]}
CLINICAL = json.loads(
    (CONTRACTS.parents[2] / "backend/tests/fixtures/phi-clinical-controls.json").read_text(
        encoding="utf-8"
    )
)

EXPECTED = {
    "ssn_plain": "phi",
    "ssn_glued_letter": "none",
    "email_plain": "phi",
    "email_none": "none",
    "phone_plain": "phi",
    "phone_none": "none",
    "labeled_id_record_number": "phi",
    "labeled_id_alnum": "phi",
    "labeled_id_no_digit": "none",
    "labeled_id_digit_past_token": "phi",
    "labeled_id_short": "none",
    "labeled_dob_numeric": "phi",
    "labeled_dob_month": "phi",
    "labeled_dob_none": "none",
    "labeled_phone_fax": "phi",
    "labeled_phone_none": "none",
    "street_full": "phi",
    "street_abbrev": "uncertain",
    "street_doctor_sentence": "uncertain",
    "street_none": "none",
    "mrn_spaced": "phi",
    "mrn_glued": "phi",
    "mrn_none": "none",
    "name_after_prefix": "phi",
    "name_prefix_content_line": "phi",
    "name_after_dear_patient": "phi",
    "name_before_suffix": "phi",
    "name_nfd": "phi",
    "name_no_context": "none",
    "name_lowercase": "none",
    "name_one_word": "none",
    "field_key_phi_value": "phi",
    "field_key_phi_substring": "phi",
    "field_key_phi_case": "phi",
    "field_key_phi_empty_string": "phi",
    "field_key_phi_zero": "phi",
    "field_key_phi_null": "none",
    "field_key_phi_empty_array": "none",
    "field_key_phi_empty_object": "none",
    "field_key_generic_snake": "uncertain",
    "field_key_generic_device_id": "uncertain",
    "field_key_generic_camel": "uncertain",
    "field_key_generic_acronym": "uncertain",
    "field_key_generic_digit": "uncertain",
    "field_key_generic_slash": "uncertain",
    "field_key_generic_null": "uncertain",
    "field_key_token_filename": "none",
    "field_key_token_username": "none",
    "date_iso": "uncertain",
    "date_slash": "uncertain",
    "date_ymd": "uncertain",
    "date_mdy": "uncertain",
    "date_none": "none",
    "digit_run": "uncertain",
    "digit_run_none": "none",
    "medical_record_phrase": "uncertain",
    "medical_record_plural": "none",
    "ipv4": "uncertain",
    "ipv4_none": "none",
    "phone_loose": "uncertain",
    "email_like": "uncertain",
    "email_like_none": "none",
    "html_numeric_no_semicolon": "phi",
    "html_hex": "phi",
    "html_named": "phi",
    "html_legacy_named": "phi",
    "html_raw_glue": "phi",
    "percent_encoded": "phi",
    "percent_nested": "phi",
    "percent_depth_five": "phi",
    "percent_depth_six": "phi/encoding",
    "percent_invalid_utf8": "phi/encoding",
    "percent_raw_glue": "phi",
    "url_underscore": "uncertain",
    "url_non_ascii": "uncertain",
    "url_control": "uncertain",
    "url_fullwidth_stop": "uncertain",
    "digits_fullwidth": "phi",
    "digits_arabic_indic": "phi",
    "digits_devanagari": "phi",
    "digits_math": "phi",
    "digits_superscript": "phi",
    "fold_mark_before": "phi",
    "fold_mark_after": "phi",
    "fold_circled_letter": "phi",
    "fold_fullwidth_low_line": "phi",
    "fold_control": "phi",
    "fold_numeric_word": "none",
    "fold_letter_glued": "none",
    "fold_dotless_i": "phi",
    "fold_dotted_capital_i": "phi",
    "fold_ideographic_space": "phi",
    "fold_fullwidth_email": "phi",
    "multilingual_spanish": "none",
    "multilingual_german": "none",
    "multilingual_french": "none",
    "multilingual_russian": "none",
    "multilingual_arabic": "none",
    "multilingual_hindi": "none",
    "multilingual_japanese": "none",
    "multilingual_chinese": "none",
    "size_near_cap_text": "none",
    "size_over_cap_text": "phi/too_large",
    "size_near_cap_json": "none",
    "size_over_cap_json": "phi/too_large",
    "depth_64": "none",
    "depth_65": "phi/too_large",
    "json_float": "phi/encoding",
    "json_big_int": "phi/encoding",
    "json_int_leaf": "uncertain",
    "json_scalars": "none",
    "json_paths": "phi",
    "json_key_leaf": "phi",
    "json_root_array": "phi",
    "empty_text": "none",
    "white_space_text": "none",
}
EXPECTED.update({f"clinical_benign_{row['id']}": "none" for row in CLINICAL["benign"]})
EXPECTED.update({f"clinical_phi_{row['id']}": "phi" for row in CLINICAL["phi"]})

EXPECTED_RULES = {
    "ssn_plain": {"ssn"},
    "email_plain": {"email", "email_like"},
    "phone_plain": {"phone", "phone_loose"},
    "labeled_id_digit_past_token": {"labeled_id"},
    "street_abbrev": {"street_address_abbrev"},
    "street_doctor_sentence": {"street_address_abbrev"},
    "street_full": {"street_address"},
    "mrn_glued": {"mrn"},
    "name_after_prefix": {"name_after_prefix"},
    "name_after_dear_patient": {"name_after_prefix"},
    "name_before_suffix": {"name_before_suffix"},
    "field_key_phi_value": {"field_key_phi"},
    "field_key_generic_snake": {"field_key_generic"},
    "date_iso": {"date_iso", "date_ymd"},
    "ipv4": {"ipv4"},
    "phone_loose": {"phone_loose"},
    "email_like": {"email_like"},
    "html_raw_glue": {"ssn"},
    "percent_raw_glue": {"ssn"},
    "url_underscore": {"digit_run", "phone_loose"},
}

EXPECTED_NORMALIZE = {
    "html_amp_legacy": "a&b",
    "html_longest_prefix": "¬it;",
    "html_cp1252": "–",
    "html_removed": "ab",
    "html_surrogate": "�",
    "html_too_big": "�",
    "html_zero": "�",
    "html_unknown": "&zzzz;",
    "html_long_decimal": "A",
    "html_carriage_return": "\r",
    "percent_plus_kept": "a+b c",
    "percent_euro": "€",
    "percent_partial": "encoding",
    "percent_split_by_letter": "encoding",
}

PROBES = [
    "a&#64example.com",
    "&amp &amp; &AMP &ampx &copy2024 &notit; &notin; &#x1F600; &#X41 &#65 &#9;",
    "&#128;&#129;&#141;&#150;&#159;&#160;&#0;&#13;&#127;&#1;&#11;&#xFFFE;&#x10FFFF;&#x110000;",
    "&" + "a" * 40 + ";" + "&lt" + "&GT;" + "&#" + "9" * 30 + ";",
    "&é; &zwj; &‍; &&amp;; &# &#x; &#xg; &;",
    "plain text without any reference",
    "&acE; &NotEqualTilde; &ThickSpace; &fjlig; &nGt;",
]


def run_input(case: dict):
    if "text" in case:
        return case["text"]
    if "json" in case:
        return case["json"]
    if "text_repeat" in case:
        spec = case["text_repeat"]
        return spec["unit"] * spec["count"] + spec["tail"]
    spec = case["json_repeat"]
    return [spec["item"]] * spec["count"]


def verdict_label(out: dict) -> str:
    return out["verdict"] if out["reason"] is None else f"{out['verdict']}/{out['reason']}"


def test_every_detect_vector_has_a_stated_expectation() -> None:
    assert set(DETECT) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_detect_vector(name: str) -> None:
    case = DETECT[name]
    out = phi_detector.detect(run_input(case))
    assert verdict_label(out) == EXPECTED[name]
    assert out == case["expect"]


@pytest.mark.parametrize("name", sorted(EXPECTED_RULES))
def test_detect_vector_rules(name: str) -> None:
    out = phi_detector.detect(run_input(DETECT[name]))
    assert {finding["rule"] for finding in out["findings"]} == EXPECTED_RULES[name]


def test_every_rule_has_a_positive_and_a_negative_vector() -> None:
    fired: set[str] = set()
    for case in DOC["detect"]:
        fired |= {finding["rule"] for finding in case["expect"]["findings"]}
    assert fired == {rule["id"] for rule in phi_detector.RULES["rules"]}
    negatives = {name for name, label in EXPECTED.items() if label == "none"}
    for rule in ("ssn", "email", "phone", "labeled_id", "labeled_dob", "labeled_phone"):
        assert any(name.startswith(rule) for name in negatives), rule
    for prefix in ("street", "mrn", "name", "field_key_phi", "date", "digit_run", "ipv4"):
        assert any(name.startswith(prefix) for name in negatives), prefix
    assert {"medical_record_plural", "email_like_none", "field_key_token_filename"} <= negatives


def test_every_reason_has_a_vector() -> None:
    reasons = {case["expect"]["reason"] for case in DOC["detect"]}
    assert reasons == {None, "too_large", "encoding"}
    assert DOC["constants"]["reasons"] == ["too_large", "encoding", "detector_failed"]


def test_a_failure_inside_the_detector_is_phi(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(phi_detector, "_scan_leaf", broken)
    out = phi_detector.detect("nothing to see")
    assert out == {"v": 1, "verdict": "phi", "reason": "detector_failed", "findings": []}


def test_values_outside_the_data_model_are_encoding_failures() -> None:
    for value in ("lone \ud800 surrogate", {"k": "\udfff"}, {"\ud800": 1}, {1: "a"}, (1, 2), b"bytes", 1.0):
        out = phi_detector.detect(value)
        assert (out["verdict"], out["reason"]) == ("phi", "encoding"), value


def test_near_cap_vectors_use_short_tokens() -> None:
    for name in ("size_near_cap_text", "size_over_cap_text"):
        spec = DETECT[name]["text_repeat"]
        assert len(spec["unit"]) <= 4
    assert len(run_input(DETECT["size_near_cap_text"]).encode("utf-8")) == phi_detector.MAX_BYTES
    assert len(run_input(DETECT["size_over_cap_text"]).encode("utf-8")) == phi_detector.MAX_BYTES + 1


def test_findings_are_sorted_and_carry_positional_paths() -> None:
    out = DETECT["json_paths"]["expect"]
    assert [(f["path"], f["part"]) for f in out["findings"]][:1] == [([0, 0, 0], "value")]
    assert all(all(type(step) is int for step in f["path"]) for f in out["findings"])
    key = DETECT["json_key_leaf"]["expect"]["findings"]
    assert {(tuple(f["path"]), f["part"]) for f in key} == {((0,), "key")}
    text = DETECT["ssn_plain"]["expect"]["findings"]
    assert all(f["path"] is None and f["part"] == "text" for f in text)


def test_a_verdict_never_carries_input_text() -> None:
    secrets = ["900-12-3456", "avery.quill@example.com", "Avery Quill", "A-100", "XG-55120"]
    values = [
        "Patient: Avery Quill SSN 900-12-3456",
        {"avery.quill@example.com": {"patient_id": "A-100", "Avery Quill": "XG-55120"}},
        ["insurance id: XG-55120", {"900-12-3456": True}],
    ]
    for value in values:
        out = phi_detector.detect(value)
        rendered = json.dumps(out) + repr(out) + str(out)
        assert out["verdict"] == "phi"
        for secret in secrets:
            assert secret not in rendered


@pytest.mark.parametrize("name", sorted(EXPECTED_NORMALIZE))
def test_normalize_vector(name: str) -> None:
    case = NORMALIZE[name]
    expected = EXPECTED_NORMALIZE[name]
    if expected == "encoding":
        assert case["expect"] == {"reason": "encoding"}
        with pytest.raises(phi_detector.EncodingFailure):
            phi_detector.normalize(case["text"])
        return
    assert case["expect"]["decoded"] == expected
    assert phi_detector.decode(case["text"]) == expected
    assert list(phi_detector.normalize(case["text"])) == case["expect"]["segments"]


@pytest.mark.parametrize("name", sorted(NORMALIZE))
def test_every_normalize_vector_matches(name: str) -> None:
    case = NORMALIZE[name]
    if case["expect"] == {"reason": "encoding"}:
        with pytest.raises(phi_detector.EncodingFailure):
            phi_detector.normalize(case["text"])
    else:
        assert phi_detector.decode(case["text"]) == case["expect"]["decoded"]
        assert list(phi_detector.normalize(case["text"])) == case["expect"]["segments"]


def test_url_tokens_are_appended_after_the_skeleton() -> None:
    first, *rest = phi_detector.normalize("see https://例.example/p_1?q=2 now")
    assert rest == []
    assert first == "see https://x.example/p_1?q=2 now\nhttps x example p 1 q 2"


def test_the_undecoded_skeleton_is_a_second_segment() -> None:
    assert list(phi_detector.normalize("900-12-3456%41")) == ["900-12-3456A", "900-12-3456%41"]
    assert list(phi_detector.normalize("no change")) == ["no change"]


def test_normalized_text_is_ascii_only() -> None:
    texts = [case["text"] for case in DOC["normalize"] if "segments" in case["expect"]]
    texts += [case["text"] for case in DOC["detect"] if "text" in case and case["expect"]["reason"] is None]
    texts.append("".join(chr(point) for point in range(0x20, 0x3000)))
    for text in texts:
        for segment in phi_detector.normalize(text):
            assert segment.isascii()


def test_corpus_verdicts() -> None:
    assert len(DOC["corpus"]) >= 5000
    for case in DOC["corpus"]:
        out = phi_detector.detect(run_input(case))
        assert out["verdict"] == case["verdict"], case["id"]
        assert sorted({f["rule"] for f in out["findings"]}) == case["rules"], case["id"]


def test_html_decoding_matches_python() -> None:
    for probe in PROBES + [case["text"] for case in DOC["normalize"]]:
        assert phi_detector.html_unescape(probe) == html.unescape(probe), probe
    for point in itertools.chain(range(0, 0x300), range(0xD7F0, 0xE010), range(0xFFF0, 0x10010)):
        for form in (f"&#{point};", f"&#x{point:X}", f"&#{point}x"):
            assert phi_detector.html_unescape(form) == html.unescape(form), form
    for name in phi_detector.ENTITIES["named"]:
        for form in ("&" + name, "&" + name + "x", "&" + name + ";"):
            assert phi_detector.html_unescape(form) == html.unescape(form), form


def test_html_tables_equal_this_pythons_module() -> None:
    assert phi_detector.ENTITIES["named"] == html.entities.html5
    assert phi_detector.ENTITIES["pattern"] == html._charref.pattern


def test_percent_decoding_matches_python_strict_unquote() -> None:
    probes = ["%41", "%4", "%%41", "%4%41", "a%20b+c", "%E2%82%AC", "%e2%82%ac", "%C3%28", "%C3é%A9", "%F0%9F%98%80", "%ED%A0%80", "%C0%AF", "é%41é", "%zz%2"]
    for probe in probes:
        try:
            expected = urllib.parse.unquote(probe, errors="strict")
        except UnicodeDecodeError:
            with pytest.raises(UnicodeDecodeError):
                phi_detector._unquote(probe)
            continue
        assert phi_detector._unquote(probe) == expected, probe


def test_fold_table_covers_every_code_point_once() -> None:
    runs = phi_detector.FOLD["runs"]
    assert runs[0][0] == 0 and runs[-1][1] == 0x10FFFF
    assert all(a[1] + 1 == b[0] for a, b in itertools.pairwise(runs))
    assert all(len(value) == 1 and value.isascii() for _, _, value in runs)
    assert phi_detector.FOLD["unicode"] == "16.0.0"


def test_fold_keeps_the_class_python_gives_each_character() -> None:
    pairs = {
        "́": "~", "ⓐ": "~", "＿": "~", "\u0001": "\u0001", "\u001c": " ",
        "½": "x", "Ⅱ": "x", "é": "x", "一": "x", "ı": "i",
        "İ": "I", "ſ": "s", "K": "K", "１": "1", "٣": "3",
        "²": "2", " ": " ", "　": " ", "\u0085": " ", " ": " ",
        "＜": "~", "＂": "~", "＠": "@", "\U0001f600": "~", "\U0001d7cf": "1",
    }
    for char, expected in pairs.items():
        assert phi_detector.skeleton(char) == expected, hex(ord(char))


def test_fold_agrees_with_this_pythons_classes_on_shared_unicode() -> None:
    word = re.compile(r"\w")
    space = re.compile(r"\s")
    skipped = 0
    for point in range(0x110000):
        if 0xD800 <= point <= 0xDFFF:
            continue
        char = chr(point)
        if unicodedata.category(char) == "Cn":
            skipped += 1
            continue
        mapped = phi_detector.skeleton(char)
        assert bool(word.match(char)) == bool(re.match(r"\w", mapped, re.ASCII)), hex(point)
        assert bool(space.match(char)) == bool(re.match(r"\s", mapped, re.ASCII)), hex(point)
    assert skipped > 0


def test_fold_generator_needs_unicode_16() -> None:
    import generate_phi_fold

    if unicodedata.unidata_version == generate_phi_fold.UNICODE:
        generate_phi_fold.main(["--check"])
        return
    with pytest.raises(SystemExit, match="needs Unicode 16.0.0"):
        generate_phi_fold.main(["--check"])


def test_entity_generator_needs_python_314() -> None:
    import generate_html_entities

    if sys.version_info[:2] == generate_html_entities.PYTHON:
        generate_html_entities.main(["--check"])
        return
    with pytest.raises(SystemExit, match="needs Python 3.14"):
        generate_html_entities.main(["--check"])


FORBIDDEN = [r"\(\?[=!<>P]", r"\(\?[a-hj-z]", r"\\[1-9]", r"\\[wWDSBAZzGpPkKQE]", r"[*+?}][+]"]


def test_rule_patterns_stay_inside_the_dialect() -> None:
    patterns = [rule["pattern"] for rule in phi_detector.RULES["rules"] if "pattern" in rule]
    patterns += [phi_detector.RULES["normalize"][key] for key in ("percent", "url", "url_separators")]
    for pattern in patterns:
        for forbidden in FORBIDDEN:
            assert not re.search(forbidden, pattern), (forbidden, pattern)
        bare = re.sub(r"\\.", "", pattern)
        bare = re.sub(r"\[[^\]]*\]", "", bare)
        assert "$" not in bare and "^" not in bare, pattern
        assert "(" not in bare.replace("(?:", "").replace("(?i:", "").replace("(?i)", ""), pattern
        assert re.compile(pattern, re.ASCII).fullmatch("") is None, pattern


def test_rule_table_shape() -> None:
    rules = phi_detector.RULES["rules"]
    ids = [rule["id"] for rule in rules]
    assert len(ids) == len(set(ids))
    for rule in rules:
        assert rule["tier"] in ("clear", "uncertain")
        assert rule["on"] in ("leaf", "key")
        assert ("pattern" in rule) != ("tokens" in rule)
        assert rule["sources"]
    assert DOC["constants"]["rules_digest"] == phi_detector.digest("phi_rules.json")


LABEL = re.compile(
    r"\b(?:MRN|medical\s+record\s+(?:number|no\.?|id)|patient\s+id|"
    r"social\s+security(?:\s+number)?|SSN|health\s+plan\s+id|insurance\s+id|"
    r"(?:driver'?s?|medical)\s+licen[cs]e(?:\s+(?:number|no\.?))?)"
    r"\s*[:#=\s-]+(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{2,}\b",
    re.IGNORECASE | re.ASCII,
)


def rewrite_agrees(text: str) -> bool:
    rewrite = phi_detector.rule_regex("labeled_id")
    old = [(m.start(), m.end()) for m in LABEL.finditer(text)]
    new = [(m.start(), m.end()) for m in rewrite.finditer(text)]
    if [start for start, _ in old] != [start for start, _ in new]:
        return False
    for (_, old_end), (_, new_end) in zip(old, new, strict=True):
        if old_end != new_end and not (new_end > old_end and text[new_end - 1] == "_"):
            return False
    return True


def test_lookahead_rewrite_matches_the_original_on_every_short_string() -> None:
    alphabet = ["A", "1", "-", "_", " "]
    for prefix in ("MRN: ", "SSN ", "patient id=", "MRN:-"):
        for size in range(8):
            for parts in itertools.product(alphabet, repeat=size):
                assert rewrite_agrees(prefix + "".join(parts)), prefix + "".join(parts)


def test_lookahead_rewrite_matches_the_original_on_vector_skeletons() -> None:
    texts = [case["text"] for case in DOC["detect"] if "text" in case and len(case["text"]) < 4096]
    texts += [case["text"] for case in DOC["corpus"] if "text" in case]
    for text in texts:
        try:
            segments = phi_detector.normalize(text)
        except phi_detector.EncodingFailure:
            continue
        for segment in segments:
            assert rewrite_agrees(segment), segment


def test_filter_table_shape() -> None:
    table = phi_detector.FILTER
    assert table["format"] == "astral.phi-filter/v1"
    limits = table["limits"]
    assert limits["budget_bytes"] == 16384
    assert (limits["max_findings"], limits["text_min"], limits["text_max"]) == (32, 1, 64)
    assert table["kinds"] == sorted(["name", "date", "contact", "identifier", "address", "other"])
    items = table["schema"]["properties"]["findings"]
    assert items["maxItems"] == 32
    assert items["items"]["properties"]["kind"]["enum"] == table["kinds"]
    assert not re.search(r"[;–—]", table["instructions"])
    assert DOC["constants"]["filter_digest"] == phi_detector.digest("phi_filter.json")


@pytest.mark.parametrize("name", sorted(CHUNKS))
def test_filter_chunk_vector(name: str) -> None:
    case = CHUNKS[name]
    chunks = phi_detector.filter_chunks(run_input(case))
    assert [len(chunk.encode("utf-8")) for chunk in chunks] == case["expect"]["chunk_bytes"]
    assert sum(case["expect"]["chunk_bytes"]) == case["expect"]["screened_bytes"]
    assert "".join(chunks) == run_input(case).encode("utf-8")[: case["expect"]["screened_bytes"]].decode("utf-8")


@pytest.mark.parametrize("name", sorted(ANSWERS))
def test_filter_answer_vector(name: str) -> None:
    case = ANSWERS[name]
    assert phi_detector.filter_answer(case["chunk"], case["answer"]) == case["expect"]


def test_filter_answer_vectors_cover_each_outcome() -> None:
    outcomes = {(case["expect"]["verdict"], case["expect"]["reason"]) for case in DOC["filter_answer"]}
    assert outcomes == {("uncertain", None), ("none", None), ("phi", "detector_failed")}


def test_vectors_regenerate_identically() -> None:
    import generate_phi_vectors

    assert generate_phi_vectors.render() == VECTORS.read_text(encoding="utf-8")


def test_document_lists_the_token_rule_differences() -> None:
    text = (CONTRACTS / "phi-detector.md").read_text(encoding="utf-8")
    section = text[text.index("The token rule differences are") :].split("\n", 1)[0]
    listed = re.findall(r"`((?:corpus_\d{5})|(?:field_key_token_[a-z]+))`", section)
    assert sorted(listed) == DOC["constants"]["token_rule_differences"]


def test_document_names_a_vector_for_each_fold_change() -> None:
    text = (CONTRACTS / "phi-detector.md").read_text(encoding="utf-8")
    table = text[text.index("| Change from the plan's fold") : text.index("### 3.2")]
    named = re.findall(r"`([a-z_]+)`", table.split("\n", 2)[2])
    vectors = [name for name in named if name in DETECT]
    assert len(vectors) >= 10
    assert all(DETECT[name]["expect"]["verdict"] != "none" or name == "fold_numeric_word" for name in vectors)
