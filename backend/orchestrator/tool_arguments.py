"""Turns the model's raw tool arguments into validated arguments or a precise error, repairing only what is unambiguous.
Orchestrator._prepare_tool_calls runs it before a call is recorded and dispatched.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from jsonschema import Draft202012Validator

MAX_DETAIL_CHARS = 200

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*[ \t]*\r?\n?(.*?)\r?\n?[ \t]*```\s*$", re.DOTALL)
_INTEGER = re.compile(r"-?\d+")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_PYTHON_LITERALS = {"True": "true", "False": "false", "None": "null"}
_LITERAL_WORD = re.compile(r"\b(?:True|False|None)\b")
_EMPTY_BY_TYPE: Dict[str, Callable[[], Any]] = {
    "string": lambda: "<string>", "integer": lambda: 0, "number": lambda: 0,
    "boolean": lambda: False, "array": list, "object": dict,
}


@dataclass(frozen=True)
class ArgumentError:
    code: str
    field: str
    message: str
    example: Optional[dict]


@dataclass(frozen=True)
class ArgumentResult:
    args: Optional[dict]
    repaired: bool
    error: Optional[ArgumentError]


def _parse(text: str) -> Tuple[bool, Any]:
    try:
        return True, json.loads(text, strict=False)
    except ValueError:
        return False, None


def _string_mask(text: str) -> List[bool]:
    inside = [False] * len(text)
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            inside[index] = True
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            inside[index] = True
    return inside


def _strip_fence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group(1) if match else text


def _first_balanced_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return text


def _drop_trailing_commas(text: str) -> str:
    inside = _string_mask(text)
    kept: List[str] = []
    for index, char in enumerate(text):
        if char == "," and not inside[index]:
            rest = text[index + 1:].lstrip()
            if rest[:1] in ("}", "]"):
                continue
        kept.append(char)
    return "".join(kept)


def _replace_python_literals(text: str) -> str:
    inside = _string_mask(text)

    def swap(match: "re.Match[str]") -> str:
        return match.group(0) if inside[match.start()] else _PYTHON_LITERALS[match.group(0)]

    return _LITERAL_WORD.sub(swap, text)


_REPAIRS: Tuple[Callable[[str], str], ...] = (
    _strip_fence, _first_balanced_object, _drop_trailing_commas, _replace_python_literals)


def _load(raw: str) -> Tuple[bool, Any, bool, str]:
    ok, value = _parse(raw)
    if ok:
        return True, value, False, ""
    text = raw
    for repair in _REPAIRS:
        text = repair(text)
        ok, value = _parse(text)
        if ok:
            return True, value, True, ""
    try:
        json.loads(raw, strict=False)
    except ValueError as exc:
        return False, None, False, str(exc).splitlines()[0]
    return False, None, False, "The arguments are not valid JSON."


def _as_container(text: str, kind: type) -> Optional[Any]:
    ok, value = _parse(text)
    return value if ok and isinstance(value, kind) else None


def _coerce_scalar(value: Any, kind: str) -> Tuple[Any, bool]:
    if kind == "integer" and isinstance(value, str) and _INTEGER.fullmatch(value):
        return int(value), True
    if kind == "number" and isinstance(value, str) and _NUMBER.fullmatch(value):
        number = int(value) if _INTEGER.fullmatch(value) else float(value)
        return (number, True) if math.isfinite(number) else (value, False)
    if kind == "boolean" and value in ("true", "false"):
        return value == "true", True
    if kind == "string" and isinstance(value, int) and not isinstance(value, bool):
        return str(value), True
    return value, False


def _coerce(value: Any, schema: Any) -> Tuple[Any, bool]:
    if not isinstance(schema, dict):
        return value, False
    kind = schema.get("type")
    changed = False
    if kind == "object":
        if isinstance(value, str):
            parsed = _as_container(value, dict)
            if parsed is None:
                return value, False
            value, changed = parsed, True
        if isinstance(value, dict):
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            for name in list(value):
                if name in properties:
                    value[name], inner = _coerce(value[name], properties[name])
                    changed = changed or inner
                elif schema.get("additionalProperties") is False:
                    del value[name]
                    changed = True
        return value, changed
    if kind == "array":
        if isinstance(value, str):
            parsed = _as_container(value, list)
            if parsed is None:
                return value, False
            value, changed = parsed, True
        if isinstance(value, list) and isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                value[index], inner = _coerce(item, schema["items"])
                changed = changed or inner
        return value, changed
    if isinstance(kind, str):
        return _coerce_scalar(value, kind)
    return value, False


def _example_value(prop: Any) -> Any:
    if not isinstance(prop, dict):
        return "<value>"
    if isinstance(prop.get("enum"), list) and prop["enum"]:
        return prop["enum"][0]
    if isinstance(prop.get("examples"), list) and prop["examples"]:
        return prop["examples"][0]
    if "default" in prop:
        return prop["default"]
    kind = prop.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    factory = _EMPTY_BY_TYPE.get(kind) if isinstance(kind, str) else None
    return factory() if factory else "<value>"


def example_for(schema: dict) -> dict:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return {name: _example_value(properties.get(name)) for name in required if isinstance(name, str)}


def _path(error: Any) -> str:
    return "/".join(str(part) for part in error.absolute_path)


def _describe(error: Any, schema: dict) -> ArgumentError:
    example = example_for(schema)
    path = _path(error)
    if error.validator == "required":
        missing = next((n for n in error.validator_value if n not in error.instance), "")
        return ArgumentError("schema_mismatch", missing, f"missing required field '{missing}'.", example)
    if error.validator == "type":
        wanted = error.validator_value
        wanted = " or ".join(wanted) if isinstance(wanted, list) else wanted
        return ArgumentError("schema_mismatch", path, f"field '{path}' must be {wanted}.", example)
    if error.validator == "enum":
        return ArgumentError(
            "schema_mismatch", path, f"field '{path}' must be one of {json.dumps(error.validator_value)}.", example)
    return ArgumentError("schema_mismatch", path, error.message[:MAX_DETAIL_CHARS], example)


def _usable_schema(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        return False
    return True


def prepare_arguments(raw: Any, schema: Optional[dict]) -> ArgumentResult:
    repaired = False
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        value: Any = {}
    elif isinstance(raw, dict):
        value = copy.deepcopy(raw)
    else:
        ok, value, repaired, detail = _load(raw if isinstance(raw, str) else str(raw))
        if not ok:
            return ArgumentResult(None, False, ArgumentError("invalid_json", "", detail, None))
        if isinstance(value, str):
            inner = _as_container(value, dict)
            if inner is not None:
                value, repaired = inner, True
    if not isinstance(value, dict):
        return ArgumentResult(None, False, ArgumentError(
            "not_an_object", "", "The arguments must be a JSON object.", None))
    if _usable_schema(schema):
        value, coerced = _coerce(value, schema)
        repaired = repaired or coerced
        errors = sorted(Draft202012Validator(schema).iter_errors(value),
                        key=lambda e: ([str(p) for p in e.absolute_path], str(e.validator)))
        if errors:
            return ArgumentResult(None, False, _describe(errors[0], schema))
    return ArgumentResult(value, repaired, None)


def model_error_text(tool_name: str, error: ArgumentError) -> str:
    text = f"The arguments for '{tool_name}' were not run: {error.message}"
    if error.example is not None:
        text += f" Send the call again with arguments like {json.dumps(error.example)}."
    return text
