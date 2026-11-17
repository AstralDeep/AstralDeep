"""SQL functions, parameter adapters and column converters that let SQLite answer the PostgreSQL
spellings Plane statements use (now(), digest(), greatest(), json helpers). Installed per connection
by database/sqlite.py. Timestamps are stored as fixed-width UTC text so text order is time order.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
import uuid
import zlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any, Final

TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"


def format_timestamp(value: datetime) -> str:
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def timeout_seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|min)?", value)
    if match is None:
        raise ValueError("timeout must be non-negative with a supported unit")
    return float(match[1]) * {None: 0.001, "ms": 0.001, "s": 1, "min": 60}[match[2]]


def _timestamp_is_valid(value: object) -> int:
    if not isinstance(value, str):
        return 0
    try:
        parse_timestamp(value)
    except ValueError:
        return 0
    return 1


def _adapt_datetime(value: datetime) -> str:
    return format_timestamp(value)


def _adapt_date(value: date) -> str:
    return value.isoformat()


def _adapt_uuid(value: uuid.UUID) -> str:
    return str(value)


def _adapt_decimal(value: Decimal) -> str:
    return str(value)


def _convert_timestamp(raw: bytes) -> datetime | str:
    text = raw.decode("utf-8")
    try:
        return parse_timestamp(text)
    except ValueError:
        return text


def _convert_boolean(raw: bytes) -> bool:
    if raw not in (b"0", b"1"):
        raise ValueError("invalid persisted boolean")
    return raw == b"1"


def _convert_json(raw: bytes) -> Any:
    return json.loads(raw)


def register_adapters() -> None:
    sqlite3.register_adapter(datetime, _adapt_datetime)
    sqlite3.register_adapter(date, _adapt_date)
    sqlite3.register_adapter(uuid.UUID, _adapt_uuid)
    sqlite3.register_adapter(Decimal, _adapt_decimal)
    sqlite3.register_converter("TIMESTAMPTZ", _convert_timestamp)
    sqlite3.register_converter("BOOLEAN", _convert_boolean)
    sqlite3.register_converter("JSONB", _convert_json)
    sqlite3.register_converter("JSONARRAY", _convert_json)


def bind_value(value: object) -> object:
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        items: object = sorted(value) if isinstance(value, (set, frozenset)) else value
        return json.dumps(items, separators=(",", ":"), allow_nan=False)
    return value


def _cast_integer(value: object, bits: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+\s*", value) is None:
        raise ValueError("invalid integer input")
    result = int(Decimal(str(value)).to_integral_value(rounding="ROUND_HALF_UP"))
    if not -(1 << (bits - 1)) <= result < (1 << (bits - 1)):
        raise ValueError("integer is out of range")
    return result


def _cast_uuid(value: object) -> str | None:
    return None if value is None else str(uuid.UUID(str(value)))


def _uuid_is_canonical(value: object) -> int:
    try:
        return int(isinstance(value, str) and str(uuid.UUID(value)) == value)
    except ValueError:
        return 0


def _cast_json(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(
        _strict_json(str(value)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _strict_json(raw: str) -> Any:
    value = json.loads(raw)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and "\x00" in item:
            raise ValueError("JSON strings must not contain NUL")
        if isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


def _jsonb_is_valid(value: object) -> int:
    try:
        _cast_json(value)
    except (ValueError, TypeError):
        return 0
    return int(value is not None)


def _array_items(value: str) -> list[str | None]:
    if not value.startswith("{") or not value.endswith("}"):
        raise ValueError("invalid array input")
    body = value[1:-1]
    if not body.strip():
        return []
    items = []
    index = 0
    while index < len(body):
        while index < len(body) and body[index].isspace():
            index += 1
        quoted = index < len(body) and body[index] == '"'
        if quoted:
            index += 1
        item = []
        closed = not quoted
        while index < len(body):
            character = body[index]
            if character == "\\":
                index += 1
                if index == len(body):
                    raise ValueError("invalid array escape")
                item.append(body[index])
            elif quoted and character == '"':
                index += 1
                closed = True
                break
            elif not quoted and character == ",":
                break
            elif not quoted and character in '{}"':
                raise ValueError("nested or malformed array input")
            else:
                item.append(character)
            index += 1
        if not closed:
            raise ValueError("unterminated array item")
        text = "".join(item) if quoted else "".join(item).strip()
        if not quoted and not text:
            raise ValueError("empty array item must be quoted")
        items.append(None if not quoted and text.upper() == "NULL" else text)
        while index < len(body) and body[index].isspace():
            index += 1
        if index == len(body):
            break
        if body[index] != "," or index + 1 == len(body):
            raise ValueError("invalid array separator")
        index += 1
    return items


def _cast_array(value: object, kind: str | None = None) -> str | None:
    if value is None:
        return None
    decoded = _array_items(str(value)) if str(value).startswith("{") else json.loads(str(value))
    if not isinstance(decoded, list):
        raise ValueError("array input must be a list")
    if kind is not None:

        def cast(item):
            if item is None:
                return None
            if isinstance(item, (list, dict)):
                raise ValueError("nested array casts require an explicit SQLite statement")
            if kind == "uuid":
                return _cast_uuid(item)
            if kind in {"int", "integer", "int4", "bigint", "int8", "smallint", "int2"}:
                bits = (
                    16 if kind in {"smallint", "int2"} else 64 if kind in {"bigint", "int8"} else 32
                )
                return _cast_integer(item, bits)
            if kind in {"boolean", "bool"}:
                return bool(_cast_boolean(item))
            if kind in {"text", "varchar", "char", "character"}:
                return str(item).lower() if isinstance(item, bool) else str(item)
            raise ValueError("unsupported array element type")

        decoded = [cast(item) for item in decoded]
    return json.dumps(decoded, separators=(",", ":"), allow_nan=False)


def _cast_boolean(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(bool(value))
    text = str(value).strip().lower()
    if text in {"true", "t", "yes", "y", "on", "1"}:
        return 1
    if text in {"false", "f", "no", "n", "off", "0"}:
        return 0
    raise ValueError("invalid boolean input")


def _json_has(value: object, key: object) -> int | None:
    if value is None or key is None:
        return None
    decoded = json.loads(str(value))
    return (
        int(str(key) in decoded) if isinstance(decoded, (dict, list)) else int(decoded == str(key))
    )


def _pg_input_is_valid(value: object, kind: object) -> int | None:
    if value is None:
        return None
    if kind not in {"json", "jsonb"}:
        raise ValueError("unsupported input-validation type")
    try:
        _cast_json(value)
    except ValueError:
        return 0
    return 1


def _to_jsonb(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _path_items(path: object) -> list[str]:
    rendered = str(path)
    keys = _array_items(rendered) if rendered.startswith("{") else json.loads(rendered)
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise ValueError("JSON path must contain text keys")
    return keys


def _json_path_text(raw: object, path: object) -> str | None:
    if raw is None or path is None:
        return None
    value = json.loads(str(raw))
    for key in _path_items(path):
        if isinstance(value, dict):
            if key not in value:
                return None
            value = value[key]
        elif isinstance(value, list):
            try:
                value = value[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    if value is None:
        return None
    return value if isinstance(value, str) else _to_jsonb(value)


def _jsonb_set(raw: object, path: object, replacement: object, create: object = 1) -> str | None:
    if raw is None or path is None or replacement is None:
        return None
    value = json.loads(str(raw))
    keys = _path_items(path)
    if not isinstance(value, (dict, list)):
        raise ValueError("cannot set a path in a JSON scalar")
    nested = value
    for key in keys[:-1]:
        if isinstance(nested, dict):
            if key not in nested:
                return _to_jsonb(value)
            nested = nested[key]
        elif isinstance(nested, list):
            index = int(key)
            if not -len(nested) <= index < len(nested):
                return _to_jsonb(value)
            nested = nested[index]
        else:
            return _to_jsonb(value)
    if keys:
        key = keys[-1]
        replacement_value = json.loads(str(replacement))
        if isinstance(nested, dict) and (create or key in nested):
            nested[key] = replacement_value
        elif isinstance(nested, list):
            index = int(key)
            if -len(nested) <= index < len(nested):
                nested[index] = replacement_value
            elif create:
                nested.insert(0, replacement_value) if index < 0 else nested.append(
                    replacement_value
                )
    return "null" if value is None else _to_jsonb(value)


def _right(value: object, count: object) -> str | None:
    if value is None or count is None:
        return None
    text = str(value)
    size = int(count)
    if size >= 0:
        return text[len(text) - size :] if size else ""
    return text[-size:]


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8")
    return str(value)


def _bytes(value: object) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return str(value).encode("utf-8")


def _epoch(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_timestamp(str(value)).timestamp()


def _greatest(*values: object) -> object:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _least(*values: object) -> object:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _split_part(value: object, delimiter: object, position: object) -> str | None:
    if value is None or delimiter is None or position is None:
        return None
    index = int(position)
    if index == 0:
        raise ValueError("split field position must not be zero")
    if delimiter == "":
        return str(value) if index in {1, -1} else ""
    parts = str(value).split(str(delimiter))
    return (
        parts[index - 1]
        if 1 <= index <= len(parts)
        else parts[index]
        if -len(parts) <= index < 0
        else ""
    )


def _digest(data: object, algorithm: object) -> bytes | None:
    if data is None or algorithm is None:
        return None
    return hashlib.new(str(algorithm), _bytes(data)).digest()


def _encode(data: object, fmt: object) -> str | None:
    if data is None:
        return None
    raw = _bytes(data)
    kind = str(fmt)
    if kind == "hex":
        return raw.hex()
    if kind == "base64":
        return base64.b64encode(raw).decode("ascii")
    raise ValueError("unsupported encoding")


def _decode(text: object, fmt: object) -> bytes | None:
    if text is None:
        return None
    kind = str(fmt)
    try:
        if kind == "hex":
            return bytes.fromhex(str(text))
        if kind == "base64":
            return base64.b64decode(str(text), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid encoded value") from exc
    raise ValueError("unsupported encoding")


def _hashtext(value: object) -> int | None:
    if value is None:
        return None
    checksum = zlib.crc32(str(value).encode("utf-8"))
    return checksum - (1 << 32) if checksum >= (1 << 31) else checksum


def _json_value(raw: object) -> Any:
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


def _jsonb_typeof(raw: object) -> str | None:
    if raw is None:
        return None
    value = _json_value(raw)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _jsonb_array_length(raw: object) -> int | None:
    if raw is None:
        return None
    value = _json_value(raw)
    if not isinstance(value, list):
        raise ValueError("cannot get array length of a non-array")
    return len(value)


def _jsonb_build_object(*pairs: object) -> str:
    if len(pairs) % 2:
        raise ValueError("jsonb_build_object needs key and value pairs")
    if any(key is None for key in pairs[::2]):
        raise ValueError("JSON object keys must not be null")
    return json.dumps(
        {str(pairs[index]): pairs[index + 1] for index in range(0, len(pairs), 2)},
        separators=(",", ":"),
        sort_keys=True,
    )


def _to_timestamp(seconds: object) -> str | None:
    if seconds is None:
        return None
    return format_timestamp(datetime.fromtimestamp(float(seconds), UTC))  # type: ignore[arg-type]


_POSIX_CLASSES: Final = {"[:cntrl:]": "\\x00-\\x1f\\x7f", "[:digit:]": "0-9", "[:alpha:]": "A-Za-z"}


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    for posix, replacement in _POSIX_CLASSES.items():
        pattern = pattern.replace(posix, replacement)
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = f"{pattern[:-1]}\\Z"
    return re.compile(pattern)


def _regexp(pattern: object, value: object) -> int | None:
    if pattern is None or value is None:
        return None
    return 1 if _compiled(str(pattern)).search(str(value)) else 0


_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._:@/-]{0,126}[A-Za-z0-9])?")
_BLOB_OWNER: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._@-]{0,254}|__verif__[A-Za-z0-9][A-Za-z0-9._-]{0,245}"
)
_ATTACHMENT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,254}")
_RESERVED_DEVICE_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)
_UUID4: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")


def _reserved(component: str) -> bool:
    return component.lower().split(".", 1)[0] in _RESERVED_DEVICE_NAMES


def _identifier_is_canonical(value: object) -> int | None:
    if value is None:
        return None
    return int(_IDENTIFIER.fullmatch(str(value)) is not None)


def _blob_owner_is_canonical(value: object) -> int | None:
    if value is None:
        return None
    text = str(value)
    return int(
        _BLOB_OWNER.fullmatch(text) is not None
        and text == text.rstrip(" .")
        and not _reserved(text)
    )


def _blob_storage_key_is_canonical(value: object) -> int | None:
    if value is None:
        return None
    text = str(value)
    components = text.split("/")
    if not 1 <= len(text) <= 4096 or text.startswith("/") or "\\" in text:
        return 0
    if not 1 <= len(components) <= 32:
        return 0
    for component in components:
        if (
            not 1 <= len(component) <= 255
            or component in {".", ".."}
            or component.startswith(".astralplane-")
            or component != component.rstrip(" .")
            or re.search(r'[<>:"|?*]', component)
            or re.search(r"[\x00-\x1f\x7f]", component)
            or _reserved(component)
        ):
            return 0
    return 1


def _attachment_id_is_canonical(value: object) -> int | None:
    if value is None:
        return None
    text = str(value)
    return int(
        _ATTACHMENT.fullmatch(text) is not None
        and "/" not in text
        and _blob_storage_key_is_canonical(text) == 1
    )


def _capabilities_are_canonical(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        items = json.loads(raw)  # type: ignore[arg-type]
    except ValueError:
        return 0
    if not isinstance(items, list) or not items:
        return 0
    for position, item in enumerate(items):
        if not isinstance(item, str) or not _identifier_is_canonical(item):
            return 0
        if position and items[position - 1].encode() >= item.encode():
            return 0
    return 1


def _positive_unique_int_array(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        items = json.loads(raw)  # type: ignore[arg-type]
    except ValueError:
        return 0
    if not isinstance(items, list) or not items:
        return 0
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in items):
        return 0
    return int(len(set(items)) == len(items))


def _exact_keys(value: object, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _valid_selected_input(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)  # type: ignore[arg-type]
    except ValueError:
        return 0
    top = {
        "version",
        "expansion_version",
        "references",
        "agent",
        "binding_key_id",
        "combined_binding",
    }
    if not _exact_keys(value, top):
        return 0
    rendered = json.dumps(
        value,
        separators=(", ", ": "),
        ensure_ascii=False,
        sort_keys=True,
    )
    if len(rendered.encode("utf-8")) > 16384:
        return 0
    for name in ("version", "expansion_version"):
        if isinstance(value[name], bool) or value[name] != 1 or not isinstance(value[name], int):
            return 0
    key_id, binding, references = (
        value["binding_key_id"],
        value["combined_binding"],
        value["references"],
    )
    if not isinstance(key_id, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key_id) is None:
        return 0
    if not isinstance(binding, str) or _HEX64.fullmatch(binding) is None:
        return 0
    if not isinstance(references, list) or len(references) > 28:
        return 0
    seen: set[tuple[str, str]] = set()
    skills = notes = 0
    for item in references:
        if not _exact_keys(item, {"kind", "resource_id", "revision"}):
            return 0
        kind, resource, revision = item["kind"], item["resource_id"], item["revision"]
        if kind not in {"skill", "note"} or not isinstance(resource, str):
            return 0
        if _UUID4.fullmatch(resource) is None:
            return 0
        if isinstance(revision, bool) or not isinstance(revision, int):
            return 0
        if not 1 <= revision <= 9007199254740991:
            return 0
        if (kind, resource) in seen:
            return 0
        seen.add((kind, resource))
        skills += kind == "skill"
        notes += kind == "note"
    if skills > 20 or notes > 8:
        return 0
    agent = value["agent"]
    if agent is None:
        return int(skills + notes > 0)
    if not _exact_keys(agent, {"agent_id", "revision_id", "definition_digest", "kind"}):
        return 0
    agent_id = agent["agent_id"]
    return int(
        agent["kind"] == "declarative"
        and isinstance(agent_id, str)
        and 1 <= len(agent_id) <= 255
        and agent_id.strip() == agent_id
        and re.search(r"[\x00-\x1f\x7f]", agent_id) is None
        and isinstance(agent["revision_id"], str)
        and _UUID4.fullmatch(agent["revision_id"]) is not None
        and isinstance(agent["definition_digest"], str)
        and _HEX64.fullmatch(agent["definition_digest"]) is not None
    )


class _StringAggregate:
    def __init__(self) -> None:
        self._values: list[str] = []

    def step(self, value: object, separator: object) -> None:
        if value is not None:
            if self._values:
                self._values.append("" if separator is None else str(separator))
            self._values.append(str(value))

    def finalize(self) -> str | None:
        return "".join(self._values) if self._values else None


class _BoolAnd:
    def __init__(self) -> None:
        self._result: bool | None = None

    def step(self, value: object) -> None:
        if value is not None:
            current = bool(_cast_boolean(value))
            self._result = current if self._result is None else self._result and current

    def finalize(self) -> int | None:
        return None if self._result is None else int(self._result)


class _BoolOr:
    def __init__(self) -> None:
        self._result: bool | None = None

    def step(self, value: object) -> None:
        if value is not None:
            current = bool(_cast_boolean(value))
            self._result = current if self._result is None else self._result or current

    def finalize(self) -> int | None:
        return None if self._result is None else int(self._result)


def install_functions(
    connection: sqlite3.Connection,
    *,
    clock: Callable[[], datetime],
    transaction_clock: Callable[[], datetime],
    settings: dict[str, str],
    statement_clock: Callable[[], datetime] | None = None,
    session_settings: dict[str, str] | None = None,
    reserve_writer: Callable[[], bool] | None = None,
) -> None:
    persistent_settings = {} if session_settings is None else session_settings

    def transaction_lock(*_keys: object) -> None:
        if (
            reserve_writer is None
            or not connection.in_transaction
            or connection.execute("PRAGMA query_only").fetchone()[0]
            or not reserve_writer()
        ):
            raise ValueError("transaction locks require SQLite's writer reservation")

    def try_transaction_lock(*keys: object) -> int:
        if reserve_writer is not None:
            return int(reserve_writer())
        raise ValueError("transaction locks require SQLite's writer reservation")

    def now() -> str:
        return format_timestamp(transaction_clock())

    def wall() -> str:
        return format_timestamp(clock())

    def current_setting(name: object, missing_ok: object = None) -> str | None:
        if str(name) in settings:
            return settings[str(name)]
        if str(name) in persistent_settings:
            return persistent_settings[str(name)]
        if name in {"lock_timeout", "statement_timeout"}:
            return "0"
        if missing_ok:
            return None
        raise ValueError(f"unrecognized configuration parameter {name}")

    def set_config(name: object, value: object, _local: object = None) -> str | None:
        if name not in {"lock_timeout", "statement_timeout", "audit.allow_purge"}:
            raise ValueError("unsupported SQLite connection setting")
        if value is None:
            settings.pop(str(name), None)
            return None
        target = settings if _local else persistent_settings
        target[str(name)] = str(value)
        if name in {"lock_timeout", "statement_timeout"}:
            timeout_seconds(str(value))
        return str(value)

    simple: tuple[tuple[str, int, Callable[..., Any]], ...] = (
        ("now", 0, now),
        ("transaction_timestamp", 0, now),
        ("clock_timestamp", 0, wall),
        ("statement_timestamp", 0, lambda: format_timestamp((statement_clock or clock)())),
        ("gen_random_uuid", 0, lambda: str(uuid.uuid4())),
        ("astral_epoch", 1, _epoch),
        ("astral_integer", 2, _cast_integer),
        ("astral_uuid", 1, _cast_uuid),
        ("astral_json", 1, _cast_json),
        ("astral_array", 1, _cast_array),
        ("astral_array", 2, _cast_array),
        ("astral_any_array", 1, lambda value: "[null]" if value is None else value),
        ("astral_boolean", 1, _cast_boolean),
        ("astral_json_has", 2, _json_has),
        ("pg_input_is_valid", 2, _pg_input_is_valid),
        ("valid_assignment_selected_input", 1, _valid_selected_input),
        ("to_jsonb", 1, _to_jsonb),
        ("jsonb_set", 3, _jsonb_set),
        ("jsonb_set", 4, _jsonb_set),
        ("astral_json_path_text", 2, _json_path_text),
        (
            "astral_left",
            2,
            lambda value, count: (
                None if value is None or count is None else str(value)[: int(count)]
            ),
        ),
        (
            "array_position",
            2,
            lambda raw, value: (
                None
                if raw is None or value not in json.loads(raw)
                else json.loads(raw).index(value) + 1
            ),
        ),
        (
            "astral_timestamp",
            1,
            lambda value: None if value is None else format_timestamp(parse_timestamp(str(value))),
        ),
        ("char_length", 1, lambda value: None if value is None else len(str(value))),
        ("octet_length", 1, lambda value: None if value is None else len(_bytes(value))),
        ("split_part", 3, _split_part),
        (
            "repeat",
            2,
            lambda value, count: (
                None if value is None or count is None else str(value) * max(0, int(count))
            ),
        ),
        ("btrim", 1, lambda value: None if value is None else str(value).strip()),
        ("md5", 1, lambda value: None if value is None else hashlib.md5(_bytes(value)).hexdigest()),
        ("digest", 2, _digest),
        ("encode", 2, _encode),
        ("decode", 2, _decode),
        ("hashtext", 1, _hashtext),
        ("hashtextextended", 2, lambda value, seed: _hashtext(f"{seed}:{value}")),
        ("current_schema", 0, lambda: "main"),
        ("jsonb_typeof", 1, _jsonb_typeof),
        ("jsonb_array_length", 1, _jsonb_array_length),
        ("cardinality", 1, _jsonb_array_length),
        ("to_timestamp", 1, _to_timestamp),
        (
            "astral_add_seconds",
            2,
            lambda moment, seconds: (
                None
                if moment is None or seconds is None
                else format_timestamp(
                    parse_timestamp(str(moment)) + timedelta(seconds=float(seconds))
                )
            ),
        ),
        ("current_setting", 1, current_setting),
        ("current_setting", 2, current_setting),
        (
            "astral_timeout_cap",
            2,
            lambda value, cap: (
                f"{min(timeout_seconds(str(value)) * 1000 or float(cap), float(cap)):g}ms"
            ),
        ),
        ("set_config", 3, set_config),
        ("pg_advisory_xact_lock", 1, transaction_lock),
        ("pg_advisory_xact_lock", 2, transaction_lock),
        ("pg_advisory_xact_lock_shared", 1, transaction_lock),
        ("pg_advisory_xact_lock_shared", 2, transaction_lock),
        ("pg_try_advisory_xact_lock", 1, try_transaction_lock),
        ("pg_try_advisory_xact_lock", 2, try_transaction_lock),
        ("pg_try_advisory_xact_lock_shared", 1, try_transaction_lock),
        ("pg_try_advisory_xact_lock_shared", 2, try_transaction_lock),
        ("pg_is_in_recovery", 0, lambda: 0),
        ("right", 2, _right),
    )
    for name, arity, function in simple:
        connection.create_function(name, arity, function)
    for name, function in (
        ("regexp", _regexp),
        ("astral_identifier_is_canonical", _identifier_is_canonical),
        ("astral_blob_owner_is_canonical", _blob_owner_is_canonical),
        ("astral_blob_storage_key_is_canonical", _blob_storage_key_is_canonical),
        ("astral_attachment_id_is_canonical", _attachment_id_is_canonical),
        ("astral_capabilities_are_canonical", _capabilities_are_canonical),
        ("astral_positive_unique_int_array", _positive_unique_int_array),
        ("astral_valid_selected_input", _valid_selected_input),
        ("astral_timestamp_is_valid", _timestamp_is_valid),
        ("astral_jsonb_is_valid", _jsonb_is_valid),
        ("astral_uuid_is_canonical", _uuid_is_canonical),
    ):
        arity = 2 if name == "regexp" else 1
        connection.create_function(name, arity, function, deterministic=True)
    for arity in range(2, 9):
        connection.create_function("greatest", arity, _greatest)
        connection.create_function("least", arity, _least)
    for arity in range(0, 21, 2):
        connection.create_function("jsonb_build_object", arity, _jsonb_build_object)
    connection.create_aggregate("string_agg", 2, _StringAggregate)  # type: ignore[arg-type]
    connection.create_aggregate("bool_and", 1, _BoolAnd)  # type: ignore[arg-type]
    connection.create_aggregate("bool_or", 1, _BoolOr)  # type: ignore[arg-type]


__all__ = (
    "TIMESTAMP_FORMAT",
    "bind_value",
    "format_timestamp",
    "install_functions",
    "parse_timestamp",
    "register_adapters",
    "timeout_seconds",
)
