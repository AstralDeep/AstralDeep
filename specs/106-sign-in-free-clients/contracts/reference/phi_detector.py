"""Reference PHI detector floor: decodes HTML and percent escapes as today's Python gate does, folds each leaf to an ASCII skeleton through phi_fold.json, and runs the rule table in phi_rules.json to a verdict of rule ids, tiers and spans.
It also holds the on-device filter's chunking and answer check from phi_filter.json, reuses the preserved mesh reference for LETS-CJ/1 bytes, and the vectors in vectors/phi-detector-v1.json pin it for every other implementation.
It reads only committed tables, so its results never depend on this Python's Unicode or HTML data."""

from __future__ import annotations

import bisect
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[3]
        / "105-decentralized-personal-mesh/contracts/reference"
    ),
)
from astral_mesh_ref.cj import canonical, strict_loads
from astral_mesh_ref.errors import MeshError

HERE = Path(__file__).resolve().parent
FORMAT = "astral.phi-detector/v1"
TABLES = ("phi_rules.json", "phi_fold.json", "html_entities.json", "phi_filter.json")
RULES = json.loads((HERE / "phi_rules.json").read_text(encoding="utf-8"))
FOLD = json.loads((HERE / "phi_fold.json").read_text(encoding="utf-8"))
ENTITIES = json.loads((HERE / "html_entities.json").read_text(encoding="utf-8"))
FILTER = json.loads((HERE / "phi_filter.json").read_text(encoding="utf-8"))
MAX_BYTES = RULES["limits"]["max_bytes"]
MAX_DEPTH = RULES["limits"]["max_depth"]
PERCENT_PASSES = RULES["normalize"]["percent_passes"]
REASONS = ("too_large", "encoding", "detector_failed")
VERDICTS = {"clear": "phi", "uncertain": "uncertain"}
INT_RANGE = (-(1 << 63), (1 << 63) - 1)
FLOAT_BYTES = RULES["limits"]["float_bytes"]


class EncodingFailure(Exception):
    pass


class TooLarge(Exception):
    pass


def digest(name: str) -> str:
    return "sha256:" + hashlib.sha256((HERE / name).read_bytes()).hexdigest()


_RUN_STARTS = [low for low, _, _ in FOLD["runs"]]


class _Fold(dict):
    def __missing__(self, point: int) -> str:
        value = FOLD["runs"][bisect.bisect_right(_RUN_STARTS, point) - 1][2]
        self[point] = value
        return value


_FOLD_MAP = _Fold()


def skeleton(text: str) -> str:
    return text.translate(_FOLD_MAP)


_CHARREF = re.compile(ENTITIES["pattern"])
_NAMED = ENTITIES["named"]
_REPLACED = {int(point): text for point, text in ENTITIES["numeric_replaced"].items()}
_REMOVED = ENTITIES["numeric_removed"]
_REMOVED_STARTS = [low for low, _ in _REMOVED]


def _removed(point: int) -> bool:
    at = bisect.bisect_right(_REMOVED_STARTS, point) - 1
    return at >= 0 and point <= _REMOVED[at][1]


def _numeric(digits: str, base: int) -> int:
    significant = digits.lstrip("0")
    if len(significant) > (7 if base == 10 else 6):
        return 0x110000
    return int(significant or "0", base)


def _charref(match: re.Match[str]) -> str:
    body = match.group(1)
    if body[0] == "#":
        if body[1] in "xX":
            point = _numeric(body[2:].rstrip(";"), 16)
        else:
            point = _numeric(body[1:].rstrip(";"), 10)
        if point in _REPLACED:
            return _REPLACED[point]
        if 0xD800 <= point <= 0xDFFF or point > 0x10FFFF:
            return "\ufffd"
        if _removed(point):
            return ""
        return chr(point)
    if body in _NAMED:
        return _NAMED[body]
    for size in range(len(body) - 1, 1, -1):
        if body[:size] in _NAMED:
            return _NAMED[body[:size]] + body[size:]
    return "&" + body


def html_unescape(text: str) -> str:
    if "&" not in text:
        return text
    return _CHARREF.sub(_charref, text)


_PERCENT = re.compile(RULES["normalize"]["percent"], re.ASCII)


def _unquote(text: str) -> str:
    out = bytearray()
    at = 0
    for match in _PERCENT.finditer(text):
        out += text[at : match.start()].encode("utf-8")
        out.append(int(match.group()[1:], 16))
        at = match.end()
    out += text[at:].encode("utf-8")
    return out.decode("utf-8")


def percent_decode(text: str) -> str:
    for _ in range(PERCENT_PASSES):
        if not _PERCENT.search(text):
            return text
        try:
            text = _unquote(text)
        except UnicodeDecodeError as exc:
            raise EncodingFailure("invalid UTF-8 after percent decoding") from exc
    if _PERCENT.search(text):
        raise EncodingFailure("percent escapes left after the last pass")
    return text


def decode(text: str) -> str:
    return percent_decode(html_unescape(text))


_URL = re.compile(RULES["normalize"]["url"], re.ASCII)
_URL_SEPARATORS = re.compile(RULES["normalize"]["url_separators"], re.ASCII)


def normalize(text: str) -> tuple[str, ...]:
    decoded = decode(text)
    first = skeleton(decoded)
    urls = _URL.findall(first)
    if urls:
        first += "\n" + "\n".join(_URL_SEPARATORS.sub(" ", url) for url in urls)
    if decoded == text:
        return (first,)
    return (first, skeleton(text))


class _Rule:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.id = spec["id"]
        self.tier = spec["tier"]
        self.on_key = spec["on"] == "key"
        self.value = spec.get("value")
        self.regex = re.compile(spec["pattern"], re.ASCII) if "pattern" in spec else None
        self.tokens = [tuple(run) for run in spec.get("tokens", [])]
        self.glued = tuple(spec.get("glued", []))


_RULES = [_Rule(spec) for spec in RULES["rules"]]


def rule_regex(rule_id: str) -> re.Pattern[str]:
    return next(rule.regex for rule in _RULES if rule.id == rule_id)


def _is_upper(char: str) -> bool:
    return "A" <= char <= "Z"


def _is_lower(char: str) -> bool:
    return "a" <= char <= "z"


def _split(char_before: str, char: str, char_after: str) -> bool:
    if _is_lower(char_before) and _is_upper(char):
        return True
    if char_before.isdigit() != char.isdigit():
        return True
    return _is_upper(char_before) and _is_upper(char) and _is_lower(char_after)


def tokens(segment: str) -> list[tuple[str, int, int]]:
    out = []
    for run in re.finditer(r"[A-Za-z0-9]+", segment, re.ASCII):
        word, base = run.group(), run.start()
        start = 0
        for at in range(1, len(word)):
            after = word[at + 1] if at + 1 < len(word) else ""
            if _split(word[at - 1], word[at], after):
                out.append((word[start:at].lower(), base + start, base + at))
                start = at
        out.append((word[start:].lower(), base + start, base + len(word)))
    return out


def _token_runs(segment: str, patterns: list[tuple[str, ...]], glued: tuple[str, ...]) -> list[tuple[int, int]]:
    found = tokens(segment)
    words = [word for word, _, _ in found]
    spans = set()
    for pattern in patterns:
        size = len(pattern)
        for at in range(len(words) - size + 1):
            if tuple(words[at : at + size]) == pattern:
                spans.add((found[at][1], found[at + size - 1][2]))
    for word, start, end in found:
        if any(part in word for part in glued):
            spans.add((start, end))
    return sorted(spans)


def _scan_leaf(text: str, is_key: bool, value_class: str | None) -> list[tuple[int, str, int, str]]:
    found = []
    offset = 0
    for segment in normalize(text):
        for rule in _RULES:
            if rule.on_key and not is_key:
                continue
            if rule.value is not None and rule.value != value_class:
                continue
            if rule.regex is not None:
                spans = [(m.start(), m.end()) for m in rule.regex.finditer(segment)]
            else:
                spans = _token_runs(segment, rule.tokens, rule.glued)
            for start, end in spans:
                found.append((offset + start, rule.id, offset + end, rule.tier))
        offset += len(segment) + 1
    return sorted(set(found))


def value_class(value: Any) -> str:
    if value is None or (type(value) in (list, dict) and not value):
        return "absent"
    if value is False or value == "" or (type(value) is int and value == 0):
        return "blank"
    return "present"


def _check_value(value: Any) -> None:
    deepest = 0
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            depth += 1
            for key, child in item.items():
                if not isinstance(key, str):
                    raise EncodingFailure("object key is not text")
                _check_text(key)
                stack.append((child, depth))
        elif isinstance(item, list):
            depth += 1
            stack.extend((child, depth) for child in item)
        elif isinstance(item, str):
            _check_text(item)
        elif isinstance(item, bool) or item is None:
            pass
        elif isinstance(item, float):
            if item != item or item in (float("inf"), float("-inf")):
                raise EncodingFailure("float that is not finite")
        elif isinstance(item, int):
            if not INT_RANGE[0] <= item <= INT_RANGE[1]:
                raise EncodingFailure("integer outside signed 64 bits")
        else:
            raise EncodingFailure("not a LETS-CJ/1 value")
        deepest = max(deepest, depth)
    if deepest > MAX_DEPTH:
        raise TooLarge("nested deeper than the limit")


def _check_text(text: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EncodingFailure("lone surrogate") from exc


def _leaves(value: Any, path: list[int], out: list[tuple[list[int] | None, str, str, str | None]]) -> None:
    if isinstance(value, dict):
        for index, (key, child) in enumerate(sorted(value.items(), key=lambda member: member[0])):
            out.append((path + [index], "key", key, value_class(child)))
            _leaves(child, path + [index], out)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _leaves(child, path + [index], out)
    elif isinstance(value, str):
        out.append((path, "value", value, None))


def _without_floats(value: Any) -> tuple[Any, int]:
    if isinstance(value, float):
        return 0, 1
    if isinstance(value, dict):
        pairs = [(key, _without_floats(child)) for key, child in value.items()]
        return {key: child for key, (child, _) in pairs}, sum(count for _, (_, count) in pairs)
    if isinstance(value, list):
        items = [_without_floats(child) for child in value]
        return [child for child, _ in items], sum(count for _, count in items)
    return value, 0


def _failed(reason: str) -> dict[str, Any]:
    return {"v": 1, "verdict": "phi", "reason": reason, "findings": []}


def _detect(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        _check_text(value)
        size = len(value.encode("utf-8"))
    else:
        _check_value(value)
        plain, floats = _without_floats(value)
        try:
            size = len(canonical(plain)) + floats * (FLOAT_BYTES - 1)
        except MeshError as exc:
            raise EncodingFailure("not a LETS-CJ/1 value") from exc
    if size > MAX_BYTES:
        raise TooLarge("over the size cap")
    leaves: list[tuple[list[int] | None, str, str, str | None]] = []
    if isinstance(value, str):
        leaves.append((None, "text", value, None))
    else:
        _leaves(value, [], leaves)
    findings = []
    for path, part, text, member_value in leaves:
        for start, rule_id, end, tier in _scan_leaf(text, part == "key", member_value):
            findings.append(
                {"rule": rule_id, "tier": tier, "path": path, "part": part, "start": start, "end": end}
            )
    tiers = {finding["tier"] for finding in findings}
    verdict = "phi" if "clear" in tiers else "uncertain" if tiers else "none"
    return {"v": 1, "verdict": verdict, "reason": None, "findings": findings}


def detect(value: Any) -> dict[str, Any]:
    try:
        return _detect(value)
    except EncodingFailure:
        return _failed("encoding")
    except TooLarge:
        return _failed("too_large")
    except Exception:
        return _failed("detector_failed")


def _cut(data: bytes, limit: int) -> int:
    if len(data) <= limit:
        return len(data)
    at = limit
    while at > 0 and data[at] & 0xC0 == 0x80:
        at -= 1
    return at


def filter_chunks(text: str) -> list[str]:
    limits = FILTER["limits"]
    data = text.encode("utf-8")
    data = data[: _cut(data, limits["budget_bytes"])]
    chunks = []
    while data:
        at = _cut(data, limits["chunk_bytes"])
        chunks.append(data[:at].decode("utf-8"))
        data = data[at:]
    return chunks


_MARKED_LETTER = re.compile(r"[A-Za-z]~+", re.ASCII)


def _match_form(text: str) -> str:
    folded = []
    for mapped in _MARKED_LETTER.sub("x", skeleton(text)):
        if mapped in " \t\n\v\f\r":
            folded.append(" ")
        else:
            folded.append(mapped.lower() if "A" <= mapped <= "Z" else mapped)
    return re.sub(" +", " ", "".join(folded)).strip(" ")


def _answer_findings(answer: str) -> list[dict[str, str]]:
    limits = FILTER["limits"]
    parsed = strict_loads(answer)
    if not isinstance(parsed, dict) or set(parsed) != {"findings"}:
        raise ValueError("answer shape")
    findings = parsed["findings"]
    if not isinstance(findings, list) or len(findings) > limits["max_findings"]:
        raise ValueError("findings shape")
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"kind", "text"}:
            raise ValueError("finding shape")
        if finding["kind"] not in FILTER["kinds"] or not isinstance(finding["text"], str):
            raise ValueError("finding value")
        if not limits["text_min"] <= len(finding["text"]) <= limits["text_max"]:
            raise ValueError("finding text length")
    return findings


def filter_answer(chunk: str, answer: str) -> dict[str, Any]:
    try:
        findings = _answer_findings(answer)
    except (MeshError, ValueError, TypeError):
        return {"verdict": "phi", "reason": "detector_failed", "kinds": []}
    screened = _match_form(chunk)
    kinds = sorted(
        {
            finding["kind"]
            for finding in findings
            if _match_form(finding["text"]) and _match_form(finding["text"]) in screened
        }
    )
    return {"verdict": "uncertain" if kinds else "none", "reason": None, "kinds": kinds}
