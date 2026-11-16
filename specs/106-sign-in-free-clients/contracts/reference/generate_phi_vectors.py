"""Generates the PHI detector vectors from fixed synthetic values and a seeded template corpus, and records each outcome the reference module gives.
`--check` fails when the committed file differs from a fresh render. The backend parity test reads a copy of the output, so no case reads a clock, the network or this Python's Unicode data."""

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
phi_detector = importlib.import_module("phi_detector")

OUTPUT = HERE.parent / "vectors" / "phi-detector-v1.json"
CLINICAL = HERE.parents[3] / "backend/tests/fixtures/phi-clinical-controls.json"
SEED = 0x106005

NAMES = ["Avery Quill", "Bram Okonjo", "Cleo Marsh", "Dara Ventis", "Emil Sorensby", "Faye Lindqvist"]
SEEDS = {
    "ssn": ["900-12-3456", "000-45-6789", "987-65-4320"],
    "phone": ["502-555-0123", "(502) 555-0147", "+1 502 555 0199", "502.555.0110", "5025550188", "(502)555-0123"],
    "email": ["avery.quill@example.com", "b.okonjo@example.org", "cleo_marsh+lab@example.com"],
    "mrn": ["MRN 0012345", "MRN: 4455667", "MRN#7788990", "MRN0012345"],
    "labeled": [
        "patient id: AB12345",
        "insurance id: XG-55120",
        "health plan id 77-1234",
        "driver's license D1234567",
        "medical record number: 7788123",
        "social security number 900-12-3456",
        "SSN: 98765432",
        "MRN: ABC-123_x",
    ],
    "dob": ["DOB: 04/12/1980", "date of birth: March 3, 1975", "born 1990-01-02", "birthdate=12/31/1999"],
    "labeled_phone": ["phone: 502 555 0123", "fax: +1 (502) 555-0100", "mobile 502.555.0177"],
    "date": ["2024-03-15", "3/15/2024", "1999/1/2", "03-15-2024", "12/1/24"],
    "digits": ["40012345", "123456789", "9001234567890"],
    "ip": ["192.0.2.10", "198.51.100.7"],
    "address": ["1200 Example Street", "42 Test Road", "7 Sample Ave", "88 North Fake Blvd", "310 Mock Lane", "5 Elm Dr"],
    "phrase": ["the medical record", "see medical record"],
    "loose_email": ["react@18.3.1", "x@y.z"],
    "name": [f"Patient: {NAMES[0]}", f"{NAMES[1]} was diagnosed with asthma", f"subject {NAMES[2]}"],
}
BENIGN = [
    "Clear tracheal secretions, continue suction.",
    "Luteal phase with progesterone effect.",
    "Prompt token count remains within budget.",
    "Seen 12 patients today by Dr. Smith",
    "Blood pressure stable, follow up in two weeks.",
    "The patient was admitted for observation.",
    "Review the lab panel before rounds.",
    "Use 0.9% saline at 125 mL per hour.",
    "Version 3.15 fixes the export bug.",
    "Meeting moved to room 204.",
    "La paciente refiere dolor leve.",
    "Der Befund ist unauffällig.",
    "Le patient se repose.",
    "Пациент стабилен.",
    "المريض في حالة مستقرة.",
    "रोगी की हालत स्थिर है।",
    "患者は安定しています。",
    "病人情况稳定。",
]
SURROUNDINGS = [
    ("", ""),
    ("Note for today: ", " as discussed."),
    ("Résumé du dossier : ", ""),
    ("患者メモ: ", " 以上"),
    ("Заметка: ", "."),
    ("José García wrote: ", ""),
]
GLUE = ["́", "ⓐ", "＿", "\u0001", "½", "ı", "%41", "&#65;", "＜", "　", "٣", "é"]
SECRETS = ["900-12-3456", "avery.quill@example.com", "Avery Quill"]


def splitmix(state: int) -> tuple[int, int]:
    state = (state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    mixed = state
    mixed = ((mixed ^ (mixed >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    mixed = ((mixed ^ (mixed >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return state, mixed ^ (mixed >> 31)


def html_numeric(text: str) -> str:
    return "".join(f"&#{ord(c)};" if c in "@.-: ()#/+" else c for c in text)


def html_hex(text: str) -> str:
    return "".join(f"&#x{ord(c):X};" if c in "@.-: ()#/+" else c for c in text)


def html_named(text: str) -> str:
    names = {"@": "&commat;", ".": "&period;", ":": "&colon;", "(": "&lpar;", ")": "&rpar;", " ": "&nbsp;", "/": "&sol;", "#": "&num;", "+": "&plus;"}
    return "".join(names.get(c, c) for c in text)


def html_bare(text: str) -> str:
    return text.replace("@", "&#64").replace(" ", "&nbsp")


def percent(text: str) -> str:
    return "".join(c if c.isascii() and c.isalnum() else "".join(f"%{b:02X}" for b in c.encode("utf-8")) for c in text)


def percent_all(text: str) -> str:
    return "".join(f"%{b:02X}" for b in text.encode("utf-8"))


def digits(text: str, zero: int) -> str:
    return "".join(chr(zero + int(c)) if c.isdigit() else c for c in text)


WRAPPERS = {
    "plain": lambda s: s,
    "html_numeric": html_numeric,
    "html_hex": html_hex,
    "html_named": html_named,
    "html_bare": html_bare,
    "percent": percent,
    "percent_all": percent_all,
    "percent_double": lambda s: percent(s).replace("%", "%25"),
    "url_query": lambda s: "https://portal.example.org/view?q=" + percent(s),
    "url_path": lambda s: "https://portal.example.org/" + s.replace(" ", "_"),
    "fullwidth_digits": lambda s: digits(s, 0xFF10),
    "arabic_digits": lambda s: digits(s, 0x0660),
    "devanagari_digits": lambda s: digits(s, 0x0966),
    "nbsp": lambda s: s.replace(" ", " "),
    "ideographic_space": lambda s: s.replace(" ", "　"),
    "turkish_i": lambda s: s.replace("i", "ı").replace("I", "İ"),
}
JSON_WRAPPERS = {
    "json_value": lambda s: {"note": s},
    "json_nested": lambda s: {"items": [{"value": s, "n": 1}], "kind": "note"},
}


def key_styles(pattern: str) -> dict[str, str]:
    words = pattern.split("_")
    camel = words[0] + "".join(w.title() for w in words[1:])
    return {
        "bare": pattern,
        "snake": "user_" + pattern,
        "camel": "user" + "".join(w.title() for w in words),
        "pascal": "".join(w.title() for w in words),
        "lower_camel": camel,
        "kebab": "user-" + pattern.replace("_", "-"),
        "dotted": "meta." + pattern.replace("_", "."),
        "upper": "USER_" + pattern.upper(),
        "glued": "user" + pattern.replace("_", ""),
    }


def field_patterns() -> tuple[list[str], list[str]]:
    rules = {rule["id"]: rule for rule in phi_detector.RULES["rules"]}
    specific = rules["field_key_phi"]["pattern"].removeprefix("(?i)").split("|")
    generic = ["_".join(run) for run in rules["field_key_generic"]["tokens"]]
    return specific, generic


def outcome(value: Any) -> dict[str, Any]:
    return phi_detector.detect(value)


def case(case_id: str, group: str, value: Any, form: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": case_id, "group": group}
    if form is not None:
        out[form] = value
        value = expand(out)
    elif isinstance(value, str):
        out["text"] = value
    else:
        out["json"] = value
    out["expect"] = outcome(value)
    return out


def expand(entry: dict[str, Any]) -> Any:
    if "text_repeat" in entry:
        spec = entry["text_repeat"]
        return spec["unit"] * spec["count"] + spec["tail"]
    spec = entry["json_repeat"]
    return [spec["item"]] * spec["count"]


def nested(depth: int) -> Any:
    value: Any = "x"
    for _ in range(depth):
        value = [value]
    return value


def nest(level: int, char: str) -> str:
    out = char
    for _ in range(level):
        out = "%" + "".join(f"{b:02X}" for b in out.encode("utf-8")) if out == char else out.replace("%", "%25")
    return out


def detect_cases() -> list[dict[str, Any]]:
    rows: list[tuple[str, str, Any]] = [
        ("ssn_plain", "rules", "SSN on file 900-12-3456."),
        ("ssn_glued_letter", "rules", "ref A900-12-3456"),
        ("email_plain", "rules", "write to avery.quill@example.com"),
        ("email_none", "rules", "the @ sign and example dot com"),
        ("phone_plain", "rules", "call (502) 555-0123 today"),
        ("phone_none", "rules", "build 502.555"),
        ("labeled_id_record_number", "rules", "medical record number: 7788123"),
        ("labeled_id_alnum", "rules", "insurance id: XG-55120"),
        ("labeled_id_no_digit", "rules", "patient id: pending"),
        ("labeled_id_digit_past_token", "rules", "MRN: ABC-123_x"),
        ("labeled_id_short", "rules", "SSN: A1"),
        ("labeled_dob_numeric", "rules", "DOB: 04/12/1980"),
        ("labeled_dob_month", "rules", "date of birth: March 3, 1975"),
        ("labeled_dob_none", "rules", "born in the spring"),
        ("labeled_phone_fax", "rules", "fax: +1 502 555 0100"),
        ("labeled_phone_none", "rules", "phone: call the desk"),
        ("street_full", "rules", "lives at 1200 Example Street"),
        ("street_abbrev", "rules", "lives at 42 Test Rd"),
        ("street_doctor_sentence", "rules", "Seen 12 patients today by Dr. Smith"),
        ("street_none", "rules", "Example Street is closed"),
        ("mrn_spaced", "rules", "MRN 0012345"),
        ("mrn_glued", "rules", "MRN0012345"),
        ("mrn_none", "rules", "MRN pending"),
        ("name_after_prefix", "names", f"Patient: {NAMES[0]}"),
        ("name_prefix_content_line", "names", f"patient\ncontent: {NAMES[1]}"),
        ("name_after_dear_patient", "names", f"Dear Patient {NAMES[0]}"),
        ("name_before_suffix", "names", f"{NAMES[0]} was diagnosed with asthma"),
        ("name_nfd", "names", "Patient: José García"),
        ("name_no_context", "names", f"{NAMES[0]} joined the call"),
        ("name_lowercase", "names", "patient avery quill"),
        ("name_one_word", "names", "Patient: Avery"),
        ("field_key_phi_value", "field_keys", {"patient_id": "A-100"}),
        ("field_key_phi_substring", "field_keys", {"insurance_id_hash": "f00"}),
        ("field_key_phi_case", "field_keys", {"Patient_ID": "x"}),
        ("field_key_phi_empty_string", "field_keys", {"dob": ""}),
        ("field_key_phi_zero", "field_keys", {"ssn": 0}),
        ("field_key_phi_null", "field_keys", {"dob": None}),
        ("field_key_phi_empty_array", "field_keys", {"dob": []}),
        ("field_key_phi_empty_object", "field_keys", {"dob": {}}),
        ("field_key_generic_snake", "field_keys", {"user_name": "x"}),
        ("field_key_generic_device_id", "field_keys", {"device_id": "x"}),
        ("field_key_generic_camel", "field_keys", {"deviceId": "x"}),
        ("field_key_generic_acronym", "field_keys", {"IPAddress": "x"}),
        ("field_key_generic_digit", "field_keys", {"address2": "x"}),
        ("field_key_generic_slash", "field_keys", {"user/name": "x"}),
        ("field_key_generic_null", "field_keys", {"user_name": None}),
        ("field_key_token_filename", "field_keys", {"filename": "report.pdf"}),
        ("field_key_token_username", "field_keys", {"username": "x"}),
        ("date_iso", "rules", "visit 2024-03-15"),
        ("date_slash", "rules", "seen on 3/15/2024"),
        ("date_ymd", "rules", "logged 1999/1/2"),
        ("date_mdy", "rules", "logged 03-15-2024"),
        ("date_none", "rules", "version 3.15"),
        ("digit_run", "rules", "account 40012345"),
        ("digit_run_none", "rules", "code 123456"),
        ("medical_record_phrase", "rules", "see the medical record"),
        ("medical_record_plural", "rules", "the medical records team"),
        ("ipv4", "rules", "host 192.0.2.10"),
        ("ipv4_none", "rules", "version 1.2.3"),
        ("phone_loose", "rules", "call (502)555-0123"),
        ("email_like", "rules", "pinned react@18.3.1"),
        ("email_like_none", "rules", "a @ b"),
        ("html_numeric_no_semicolon", "html", "a&#64example.com"),
        ("html_hex", "html", "a&#x40;example.com"),
        ("html_named", "html", "a&commat;example&period;com"),
        ("html_legacy_named", "html", "SSN&nbsp900-12-3456"),
        ("html_raw_glue", "html", "900-12-3456&#65;"),
        ("percent_encoded", "percent", "ssn%3D900%2D12%2D3456"),
        ("percent_nested", "percent", "900%252D12%252D3456"),
        ("percent_depth_five", "percent", "900" + nest(5, "-") + "12" + nest(5, "-") + "3456"),
        ("percent_depth_six", "percent", "900" + nest(6, "-") + "12-3456"),
        ("percent_invalid_utf8", "percent", "note %C3%28"),
        ("percent_raw_glue", "percent", "900-12-3456%41"),
        ("url_underscore", "url", "https://portal.example.org/p_9001234567"),
        ("url_non_ascii", "url", "https://例え.example/患者?id_9001234567"),
        ("url_control", "url", "https://h.example/a\u0001b_9001234567"),
        ("url_fullwidth_stop", "url", "https://h.example/a＜b_9001234567"),
        ("digits_fullwidth", "digits", "SSN " + digits("900-12-3456", 0xFF10)),
        ("digits_arabic_indic", "digits", "SSN " + digits("900-12-3456", 0x0660)),
        ("digits_devanagari", "digits", "SSN " + digits("900-12-3456", 0x0966)),
        ("digits_math", "digits", "SSN " + digits("900-12-3456", 0x1D7CE)),
        ("digits_superscript", "digits", "SSN ⁹⁰⁰-¹²-³⁴⁵⁶"),
        ("fold_mark_before", "fold", "é900-12-3456"),
        ("fold_mark_after", "fold", "900-12-3456́"),
        ("fold_circled_letter", "fold", "ⓐ900-12-3456"),
        ("fold_fullwidth_low_line", "fold", "＿900-12-3456"),
        ("fold_control", "fold", "\u0001900-12-3456"),
        ("fold_numeric_word", "fold", "½900-12-3456"),
        ("fold_letter_glued", "fold", "é900-12-3456"),
        ("fold_dotless_i", "fold", "patıent id: AB12345"),
        ("fold_dotted_capital_i", "fold", "PATİENT ID: AB12345"),
        ("fold_ideographic_space", "fold", "fax:　502　555　0100"),
        ("fold_fullwidth_email", "fold", "avery＠example．com"),
        ("multilingual_spanish", "multilingual", "La paciente refiere dolor leve y descansa."),
        ("multilingual_german", "multilingual", "Der Befund ist unauffällig."),
        ("multilingual_french", "multilingual", "Le patient se repose après le repas."),
        ("multilingual_russian", "multilingual", "Пациент стабилен, жалоб нет."),
        ("multilingual_arabic", "multilingual", "المريض في حالة مستقرة."),
        ("multilingual_hindi", "multilingual", "रोगी की हालत स्थिर है।"),
        ("multilingual_japanese", "multilingual", "患者は安定しています。"),
        ("multilingual_chinese", "multilingual", "病人情况稳定。"),
        ("depth_64", "limits", nested(64)),
        ("depth_65", "limits", nested(65)),
        ("json_float", "data_model", {"temp": "x", "value": 37.5}),
        ("json_big_int", "data_model", {"n": 9223372036854775808}),
        ("json_int_leaf", "json", {"n": 9001234567}),
        ("json_scalars", "json", {"a": True, "b": None, "c": 12}),
        ("json_paths", "json", {"notes": [{"text": "call 502-555-0123"}], "patient_id": "A-1"}),
        ("json_key_leaf", "json", {"avery.quill@example.com": True}),
        ("json_root_array", "json", ["clear", "MRN 0012345"]),
        ("empty_text", "text", ""),
        ("white_space_text", "text", "   "),
    ]
    clinical = json.loads(CLINICAL.read_text(encoding="utf-8"))
    for row in clinical["benign"]:
        rows.append((f"clinical_benign_{row['id']}", "clinical", row["input"]))
    for row in clinical["phi"]:
        rows.append((f"clinical_phi_{row['id']}", "clinical", row["input"]))
    out = [case(case_id, group, value) for case_id, group, value in rows]
    cap = phi_detector.MAX_BYTES
    out.append(case("size_near_cap_text", "limits", {"unit": "ab ", "count": cap // 3, "tail": "a"}, "text_repeat"))
    out.append(case("size_over_cap_text", "limits", {"unit": "ab ", "count": cap // 3, "tail": "ab"}, "text_repeat"))
    item = "ab " * 341
    out.append(case("size_near_cap_json", "limits", {"item": item, "count": 1022}, "json_repeat"))
    out.append(case("size_over_cap_json", "limits", {"item": item, "count": 1023}, "json_repeat"))
    return sorted(out, key=lambda entry: entry["id"])


def normalize_cases() -> list[dict[str, Any]]:
    rows = [
        ("html_amp_legacy", "a&ampb"),
        ("html_longest_prefix", "&notit;"),
        ("html_cp1252", "&#150;"),
        ("html_removed", "a&#1;b"),
        ("html_surrogate", "&#xD800;"),
        ("html_too_big", "&#99999999999;"),
        ("html_zero", "&#0;"),
        ("html_unknown", "&zzzz;"),
        ("html_long_decimal", "&#" + "0" * 40 + "65;"),
        ("html_carriage_return", "&#13;"),
        ("percent_plus_kept", "a+b%20c"),
        ("percent_euro", "%E2%82%AC"),
        ("percent_partial", "%E2%82"),
        ("percent_split_by_letter", "%C3é%A9"),
        ("fold_mixed", "Ｊｏé ٣ é ⓐ ＿ ½ ıİ 　|\u0001|\u001c"),
        ("url_tokens", "see https://a.example/x_y?z=1 and http://b.example:8080/p-q"),
        ("url_upper_scheme_not_tokenized", "HTTPS://a.example/x_y"),
        ("html_then_percent", "%26%2364%3B and &#37;41"),
    ]
    out = []
    for case_id, text in rows:
        try:
            expect = {"decoded": phi_detector.decode(text), "segments": list(phi_detector.normalize(text))}
        except phi_detector.EncodingFailure:
            expect = {"reason": "encoding"}
        out.append({"id": case_id, "text": text, "expect": expect})
    return out


def corpus_inputs() -> list[Any]:
    state = SEED
    values: list[Any] = []
    for kind in sorted(SEEDS):
        for seed in SEEDS[kind]:
            for name in sorted(WRAPPERS):
                for before, after in SURROUNDINGS:
                    values.append(before + WRAPPERS[name](seed) + after)
            for name in sorted(JSON_WRAPPERS):
                state, pick = splitmix(state)
                before, after = SURROUNDINGS[pick % len(SURROUNDINGS)]
                values.append(JSON_WRAPPERS[name](before + seed + after))
            for glue in GLUE:
                values.append(glue + seed)
                values.append(seed + glue)
                values.append("https://h.example/a" + glue + "b_" + seed.replace(" ", "_"))
    for sentence in BENIGN:
        for name in sorted(WRAPPERS):
            values.append(WRAPPERS[name](sentence))
        for name in sorted(JSON_WRAPPERS):
            values.append(JSON_WRAPPERS[name](sentence))
    specific, generic = field_patterns()
    for pattern in specific + generic:
        for style, key in sorted(key_styles(pattern).items()):
            state, pick = splitmix(state)
            choice = [NAMES[pick % len(NAMES)], None, "", {"x": 1}, [], "ok"][pick % 6]
            values.append({key: choice, "kind": style})
    return values


def corpus_cases() -> list[dict[str, Any]]:
    out = []
    for index, value in enumerate(corpus_inputs()):
        result = outcome(value)
        entry: dict[str, Any] = {"id": f"corpus_{index:05d}"}
        entry["text" if isinstance(value, str) else "json"] = value
        entry["verdict"] = result["verdict"]
        entry["rules"] = sorted({finding["rule"] for finding in result["findings"]})
        out.append(entry)
    return out


def token_differences(cases: list[dict[str, Any]]) -> list[str]:
    _, generic = field_patterns()
    runs = [tuple(pattern.split("_")) for pattern in generic]

    def differs(value: Any) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = key.lower()
                if phi_detector._present(child) and any(pattern in lowered for pattern in generic):
                    words = [word for word, _, _ in phi_detector.tokens(phi_detector.skeleton(key))]
                    hit = any(
                        tuple(words[at : at + len(run)]) == run
                        for run in runs
                        for at in range(len(words) - len(run) + 1)
                    )
                    if not hit:
                        return True
                if differs(child):
                    return True
        if isinstance(value, list):
            return any(differs(child) for child in value)
        return False

    out = []
    for entry in cases:
        verdict = entry["expect"]["verdict"] if "expect" in entry else entry["verdict"]
        if "json" in entry and verdict == "none" and differs(entry["json"]):
            out.append(entry["id"])
    return sorted(out)


def chunk_cases() -> list[dict[str, Any]]:
    rows = [
        ("short", {"unit": "ab ", "count": 3, "tail": ""}),
        ("three_chunks", {"unit": "ab ", "count": 3333, "tail": "a"}),
        ("over_budget", {"unit": "abcd", "count": 5000, "tail": ""}),
        ("euro_boundary", {"unit": "€", "count": 2000, "tail": ""}),
        ("euro_over_budget", {"unit": "€", "count": 6000, "tail": ""}),
        ("empty", {"unit": "", "count": 0, "tail": ""}),
    ]
    out = []
    for case_id, spec in rows:
        chunks = phi_detector.filter_chunks(spec["unit"] * spec["count"] + spec["tail"])
        sizes = [len(chunk.encode("utf-8")) for chunk in chunks]
        out.append({"id": case_id, "text_repeat": spec, "expect": {"chunk_bytes": sizes, "screened_bytes": sum(sizes)}})
    return out


def answer_cases() -> list[dict[str, Any]]:
    chunk = "Note: Avery  Quill came in on March 3.\nCall 502-555-0123."
    accented = "Patient José García seen today."

    def findings(*pairs: tuple[str, str]) -> str:
        return json.dumps({"findings": [{"kind": kind, "text": text} for kind, text in pairs]})

    rows = [
        ("name_counts", chunk, findings(("name", "Avery Quill"))),
        ("case_and_space_fold", chunk, findings(("name", "AVERY   QUILL"))),
        ("several_kinds", chunk, findings(("date", "March 3"), ("contact", "502-555-0123"), ("name", "Avery"))),
        ("empty_answer", chunk, findings()),
        ("text_not_in_chunk", chunk, findings(("name", "Bram Okonjo"))),
        ("only_white_space", chunk, findings(("other", "   "))),
        ("one_of_two_counts", chunk, findings(("name", "Bram Okonjo"), ("date", "march 3"))),
        ("accented_name", accented, findings(("name", "JOSÉ GARCÍA"))),
        ("unknown_kind", chunk, findings(("person", "Avery Quill"))),
        ("text_too_long", chunk, findings(("other", "a" * 65))),
        ("text_at_limit", chunk, findings(("other", "a" * 64))),
        ("text_empty", chunk, findings(("other", ""))),
        ("extra_field", chunk, json.dumps({"findings": [], "note": "none"})),
        ("finding_extra_field", chunk, json.dumps({"findings": [{"kind": "name", "text": "Avery", "score": 1}]})),
        ("too_many", chunk, findings(*[("name", "Avery")] * 33)),
        ("at_most", chunk, findings(*[("name", "Avery")] * 32)),
        ("not_json", chunk, "Sure, here are the findings."),
        ("float_value", chunk, '{"findings": [], "score": 0.5}'),
        ("duplicate_key", chunk, '{"findings": [], "findings": []}'),
        ("not_an_object", chunk, "[]"),
        ("text_not_string", chunk, '{"findings": [{"kind": "name", "text": 7}]}'),
    ]
    return [
        {"id": case_id, "chunk": text, "answer": answer, "expect": phi_detector.filter_answer(text, answer)}
        for case_id, text, answer in rows
    ]


def generate() -> dict[str, Any]:
    detect = detect_cases()
    corpus = corpus_cases()
    return {
        "constants": {
            "format": phi_detector.FORMAT,
            "fold_digest": phi_detector.digest("phi_fold.json"),
            "entities_digest": phi_detector.digest("html_entities.json"),
            "filter_digest": phi_detector.digest("phi_filter.json"),
            "rules_digest": phi_detector.digest("phi_rules.json"),
            "max_bytes": phi_detector.MAX_BYTES,
            "max_depth": phi_detector.MAX_DEPTH,
            "percent_passes": phi_detector.PERCENT_PASSES,
            "reasons": list(phi_detector.REASONS),
            "seed": SEED,
            "synthetic": {"names": NAMES, "seeds": SEEDS, "secrets": SECRETS},
            "token_rule_differences": token_differences(detect + corpus),
            "unicode": phi_detector.FOLD["unicode"],
        },
        "detect": detect,
        "normalize": normalize_cases(),
        "corpus": corpus,
        "filter_chunks": chunk_cases(),
        "filter_answer": answer_cases(),
    }


def render() -> str:
    return json.dumps(generate(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            raise SystemExit("phi-detector-v1 vectors differ")
    else:
        OUTPUT.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
