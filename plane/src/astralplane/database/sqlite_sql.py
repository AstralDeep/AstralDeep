"""Rewrites the portable subset of Plane's PostgreSQL statements into SQLite statements: placeholder
style, row-lock suffixes, type casts, array membership and a few spellings. It scans outside string
literals, quoted identifiers and comments, and refuses constructs it cannot express safely.
Used by database/sqlite.py for every statement a SQLite transaction runs.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Final

from astralplane.errors import SQLContractError


class UnsupportedSQLiteConstructError(SQLContractError):
    pass


_ROW_LOCK: Final = re.compile(
    r"\bFOR\s+(?:NO\s+KEY\s+UPDATE|KEY\s+SHARE|UPDATE|SHARE)"
    r"(?:\s+OF\s+[\w.]+(?:\s*,\s*[\w.]+)*)?"
    r"(?:\s+NOWAIT|\s+SKIP\s+LOCKED)?",
    re.IGNORECASE,
)
_CAST: Final = re.compile(
    r"::\s*(?:pg_catalog\.)?"
    r"(?P<kind>text|uuid|bigint|int8|integer|int4|int|smallint|int2|boolean|bool|jsonb|json|numeric"
    r"|timestamptz|timestamp|bytea|float8|float4|float|real|date|varchar|char|character)"
    r"(?P<size>\s*\(\s*\d+\s*\))?(?P<array>\s*\[\s*\])?(?!\w)",
    re.IGNORECASE,
)
_ARRAY_PARAMETER: Final = r"((?:\?|:\w+)|astral_array\((?:\?|:\w+)(?:,\s*'\w+')?\))"
_ANY_ARRAY: Final = re.compile(rf"=\s*ANY\s*\(\s*{_ARRAY_PARAMETER}\s*\)", re.IGNORECASE)
_NOT_ALL_ARRAY: Final = re.compile(
    rf"(?:<>|!=)\s*ALL\s*\(\s*{_ARRAY_PARAMETER}\s*\)", re.IGNORECASE
)
_EXTRACT_EPOCH: Final = re.compile(r"\bextract\s*\(\s*epoch\s+from\s+", re.IGNORECASE)
_COLLATE_C: Final = re.compile(r'\bCOLLATE\s+"C"', re.IGNORECASE)
_CURRENT_TIMESTAMP: Final = re.compile(r"\bCURRENT_TIMESTAMP\b(?!\s*\()", re.IGNORECASE)
_TIMESTAMP_PROJECTION: Final = re.compile(
    r"\b((?:(?:clock_timestamp|now|statement_timestamp|transaction_timestamp)\(\)"
    r"|(?:astral_add_seconds|astral_timestamp)\((?:[^()]|\([^()]*\))*\)))"
    r"\s+AS\s+([a-z_]\w*)",
    re.IGNORECASE,
)
_ON_CONSTRAINT: Final = re.compile(r"\bON\s+CONFLICT\s+ON\s+CONSTRAINT\b", re.IGNORECASE)
_PLACEHOLDER: Final = re.compile(r"%%|%\((\w+)\)s|%s")
_UNSUPPORTED: Final = (
    (re.compile(r"\bDISTINCT\s+ON\s*\(", re.IGNORECASE), "DISTINCT ON"),
    (re.compile(r"\bLATERAL\b", re.IGNORECASE), "LATERAL joins"),
    (re.compile(r"\bWITH\s+ORDINALITY\b", re.IGNORECASE), "WITH ORDINALITY"),
    (re.compile(r"\bunnest\s*\(", re.IGNORECASE), "unnest"),
    (re.compile(r"\bgenerate_series\s*\(", re.IGNORECASE), "generate_series"),
    (re.compile(r"\bLOCK\s+TABLE\b", re.IGNORECASE), "LOCK TABLE"),
    (re.compile(r"\bARRAY\s*\[", re.IGNORECASE), "ARRAY constructors"),
    (re.compile(r"::\s*(?:reg\w+|oid|name)\b", re.IGNORECASE), "catalog casts"),
    (re.compile(r"\bpg_catalog\.", re.IGNORECASE), "pg_catalog references"),
    (re.compile(r"\binterval\s+'", re.IGNORECASE), "interval literals"),
)
_CONTROL: Final = (
    (
        re.compile(
            r"^\s*BEGIN\s+ISOLATION\s+LEVEL\s+"
            r"(?:READ COMMITTED|REPEATABLE READ|SERIALIZABLE)\s*;?\s*$",
            re.IGNORECASE,
        ),
        "begin",
    ),
    (
        re.compile(r"^\s*SET\s+LOCAL\s+audit\.allow_purge\s*=\s*'true'\s*;?\s*$", re.IGNORECASE),
        "audit_purge",
    ),
)


def _segments(statement: str) -> list[tuple[bool, str]]:
    parts: list[tuple[bool, str]] = []
    length = len(statement)
    index = 0
    start = 0

    def flush(end: int) -> None:
        if end > start:
            parts.append((True, statement[start:end]))

    while index < length:
        character = statement[index]
        if character == "'":
            literal_start = index
            escaped = index > 0 and statement[index - 1] in "eE"
            index += 1
            while index < length:
                if escaped and statement[index] == "\\":
                    index += 2
                    continue
                if statement[index] == "'":
                    if index + 1 < length and statement[index + 1] == "'":
                        index += 2
                        continue
                    break
                index += 1
            flush(literal_start)
            index = min(index + 1, length)
            parts.append((False, statement[literal_start:index]))
            start = index
        elif character == '"':
            quoted_start = index
            index += 1
            while index < length and statement[index] != '"':
                index += 1
            flush(quoted_start)
            index = min(index + 1, length)
            parts.append((False, statement[quoted_start:index]))
            start = index
        elif statement.startswith("--", index):
            comment_start = index
            newline = statement.find("\n", index)
            index = length if newline < 0 else newline
            flush(comment_start)
            parts.append((False, statement[comment_start:index]))
            start = index
        elif statement.startswith("/*", index):
            comment_start = index
            close = statement.find("*/", index + 2)
            index = length if close < 0 else close + 2
            flush(comment_start)
            parts.append((False, statement[comment_start:index]))
            start = index
        elif character == "$" and re.match(r"\$(?:\w*)\$", statement[index:]):
            raise UnsupportedSQLiteConstructError(
                "dollar-quoted bodies are not available on SQLite"
            )
        else:
            index += 1
    flush(length)
    return parts


def _rewrite_code(code: str, *, parameterized: bool) -> str:
    for pattern, label in _UNSUPPORTED:
        if pattern.search(code):
            raise UnsupportedSQLiteConstructError(f"{label} are not available on SQLite")
    if _ON_CONSTRAINT.search(code):
        raise UnsupportedSQLiteConstructError(
            "ON CONFLICT ON CONSTRAINT is not available on SQLite"
        )

    def placeholder(match: re.Match[str]) -> str:
        if match.group(0) == "%%":
            return "%"
        return "?" if match.group(1) is None else f":{match.group(1)}"

    if parameterized:
        code = _PLACEHOLDER.sub(placeholder, code)
    code = re.sub(
        r"\b(?:TIMESTAMPTZ|TIMESTAMP)\s*(\x00\d+\x00)",
        r"astral_timestamp(\1)",
        code,
        flags=re.IGNORECASE,
    )
    code = _ROW_LOCK.sub("", code)
    code = _rewrite_casts(code)
    code = _ANY_ARRAY.sub(r" IN (SELECT value FROM json_each(astral_any_array(\1)))", code)
    code = _NOT_ALL_ARRAY.sub(r" NOT IN (SELECT value FROM json_each(astral_any_array(\1)))", code)
    code = _EXTRACT_EPOCH.sub("astral_epoch(", code)
    code = _COLLATE_C.sub("COLLATE BINARY", code)
    return _CURRENT_TIMESTAMP.sub("now()", code)


_MASK: Final = re.compile(r"\x00(\d+)\x00")
_OPERAND: Final = r"(?:\x00\d+\x00|[\w.\"]+(?:\([^()]*\))?|\((?:[^()]|\([^()]*\))*\))"
_REGEX_OPERATOR: Final = re.compile(rf"({_OPERAND})\s*(!?)~(\*)?\s*(\x00\d+\x00|\?|:\w+)")
_JSON_EXISTS: Final = re.compile(rf"({_OPERAND})\s+\?\s+(\x00\d+\x00|\?|:\w+)")


def _operand_start(code: str, end: int) -> int:
    end = len(code[:end].rstrip())
    index = end - 1
    if index < 0:
        raise UnsupportedSQLiteConstructError("cast has no operand")
    if code[index] == ")":
        depth = 1
        index -= 1
        while index >= 0 and depth:
            if code[index] == ")":
                depth += 1
            elif code[index] == "(":
                depth -= 1
            index -= 1
        if depth:
            raise UnsupportedSQLiteConstructError("cast operand parentheses are unbalanced")
        while index >= 0 and (code[index].isalnum() or code[index] in "_."):
            index -= 1
        return index + 1
    if code[index] == "\x00":
        return code.rfind("\x00", 0, index)
    while index >= 0 and (code[index].isalnum() or code[index] in "_.?:"):
        index -= 1
    if index + 1 == end:
        raise UnsupportedSQLiteConstructError("cast operand cannot be expressed on SQLite")
    return index + 1


def _rewrite_casts(code: str) -> str:
    while (match := _CAST.search(code)) is not None:
        start = _operand_start(code, match.start())
        operand = code[start : match.start()].rstrip()
        kind = match["kind"].lower()
        if match["size"]:
            raise UnsupportedSQLiteConstructError(
                "bounded casts require an explicit SQLite statement"
            )
        if match["array"]:
            if kind not in {
                "text",
                "uuid",
                "bigint",
                "int8",
                "integer",
                "int4",
                "int",
                "smallint",
                "int2",
                "boolean",
                "bool",
                "varchar",
                "char",
                "character",
            }:
                raise UnsupportedSQLiteConstructError(
                    f"{kind} array casts require an explicit SQLite statement"
                )
            converted = f"astral_array({operand}, '{kind}')"
        elif kind == "uuid":
            converted = f"astral_uuid({operand})"
        elif kind in {"text", "varchar", "char", "character"}:
            converted = f"CAST({operand} AS TEXT)"
        elif kind in {"int", "integer", "int4", "bigint", "int8", "smallint", "int2"}:
            bits = 16 if kind in {"smallint", "int2"} else 64 if kind in {"bigint", "int8"} else 32
            converted = f"astral_integer({operand}, {bits})"
        elif kind in {"jsonb", "json"}:
            converted = f"astral_json({operand})"
        elif kind in {"timestamp", "timestamptz"}:
            converted = f"astral_timestamp({operand})"
        elif kind in {"boolean", "bool"}:
            converted = f"astral_boolean({operand})"
        elif kind == "bytea":
            converted = f"CAST({operand} AS BLOB)"
        else:
            raise UnsupportedSQLiteConstructError(
                f"{kind} casts require an explicit SQLite statement"
            )
        code = code[:start] + converted + code[match.end() :]
    return code


def _regex_operator(code: str) -> str:
    def replace(match: re.Match[str]) -> str:
        negate = "NOT " if match.group(2) else ""
        pattern = f"('(?i)' || {match[4]})" if match[3] else match[4]
        return f"{negate}regexp({pattern}, {match[1]})"

    return _REGEX_OPERATOR.sub(replace, code)


def _rewrite_intervals(code: str, literals: list[str]) -> str:
    interval = re.compile(r"\binterval\s+(\x00(\d+)\x00)", re.IGNORECASE)
    units = {"millisecond": 0.001, "second": 1, "minute": 60, "hour": 3600, "day": 86400}
    while (match := interval.search(code)) is not None:
        value = re.fullmatch(
            r"'([+-]?\d+(?:\.\d+)?)\s+(milliseconds?|seconds?|minutes?|hours?|days?)'",
            literals[int(match[2])],
            re.IGNORECASE,
        )
        if value is None:
            raise UnsupportedSQLiteConstructError(
                "interval unit requires an explicit SQLite statement"
            )
        unit = value[2].lower().removesuffix("s")
        seconds = float(value[1]) * units[unit]
        operand_end = len(code[: match.start()].rstrip())
        factor = "1"
        wrapped = False
        if code[operand_end - 1 : operand_end] == "*":
            factor_end = len(code[: operand_end - 1].rstrip())
            factor_start = _operand_start(code, factor_end)
            factor = code[factor_start:factor_end]
            operator_end = len(code[:factor_start].rstrip())
            if code[operator_end - 1 : operator_end] == "(":
                wrapped = True
                operator_end = len(code[: operator_end - 1].rstrip())
        else:
            operator_end = operand_end
        sign = code[operator_end - 1 : operator_end]
        if sign not in {"+", "-"}:
            raise UnsupportedSQLiteConstructError(
                "interval arithmetic requires an addition or subtraction"
            )
        start = _operand_start(code, operator_end - 1)
        moment = code[start : operator_end - 1].rstrip()
        end = match.end()
        if wrapped:
            tail = re.match(r"\s*\)", code[end:])
            if tail is None:
                raise UnsupportedSQLiteConstructError("interval multiplier is unbalanced")
            end += tail.end()
        number = seconds if sign == "+" else -seconds
        code = code[:start] + f"astral_add_seconds({moment}, ({factor}) * {number!r})" + code[end:]
    return code


def _rewrite_delete_using(code: str) -> str:
    match = re.search(
        r"\bDELETE\s+FROM\s+(\w+)\s+AS\s+(\w+)\s+USING\s+"
        r"(\w+(?:\s+AS\s+\w+)?)\s+WHERE\s+(.+?)(\s+RETURNING\s+.+)?$",
        code,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return code
    table, alias, source, predicate, returning = match.groups()
    replacement = (
        f"DELETE FROM {table} AS {alias} WHERE EXISTS "
        f"(SELECT 1 FROM {source} WHERE {predicate}){returning or ''}"
    )
    return code[: match.start()] + replacement


def _rewrite_returning(code: str) -> str:
    target = re.search(r"\b(?:UPDATE|DELETE\s+FROM)\s+(\w+)\s+AS\s+(\w+)", code, re.IGNORECASE)
    returning = re.search(r"\bRETURNING\b", code, re.IGNORECASE)
    if target is None or returning is None:
        return code
    prefix, suffix = code[: returning.end()], code[returning.end() :]
    suffix = re.sub(rf"\b{re.escape(target[2])}\.", "", suffix)
    return prefix + suffix


def _annotate_projection(code: str, pattern: re.Pattern[str], kind: str) -> str:
    depths = []
    depth = 0
    for executable, text in _segments(code):
        for character in text:
            depths.append(depth)
            if executable and character == "(":
                depth += 1
            elif executable and character == ")":
                depth -= 1

    def annotate(match: re.Match[str]) -> str:
        if depths[match.start()] != 0:
            return match[0]
        return f'{match[1]} AS "{match[2]} [{kind}]"'

    return pattern.sub(annotate, code)


@lru_cache(maxsize=4096)
def translate(statement: str, parameterized: bool = True) -> str:
    masked: list[str] = []
    pieces: list[str] = []
    for is_code, text in _segments(statement):
        if is_code:
            pieces.append(text)
        else:
            masked.append(text.replace("%%", "%") if parameterized else text)
            pieces.append(f"\x00{len(masked) - 1}\x00")
    code = _regex_operator(_rewrite_code("".join(pieces), parameterized=parameterized))
    code = re.sub(
        r"\bCOLLATE\s+\x00(\d+)\x00",
        lambda match: "COLLATE BINARY" if masked[int(match[1])] == '"C"' else match[0],
        code,
        flags=re.IGNORECASE,
    )
    code = _JSON_EXISTS.sub(r"astral_json_has(\1, \2)", code)
    code = re.sub(
        rf"({_OPERAND})\s*#>>\s*(\x00\d+\x00|\?|:\w+)", r"astral_json_path_text(\1, \2)", code
    )
    code = _rewrite_intervals(code, masked)
    code = _rewrite_returning(_rewrite_delete_using(code))
    code = _annotate_projection(code, _TIMESTAMP_PROJECTION, "TIMESTAMPTZ")
    code = _annotate_projection(
        code,
        re.compile(
            r"\b((?:NOT\s+)?EXISTS\s*\((?:[^()]|\([^()]*\))*\))\s+AS\s+(\w+)",
            re.IGNORECASE,
        ),
        "BOOLEAN",
    )
    for functions, kind in (
        ("astral_json|jsonb_set|jsonb_build_object|to_jsonb|jsonb_agg", "JSONB"),
        ("astral_array", "JSONARRAY"),
        ("astral_boolean", "BOOLEAN"),
    ):
        code = _annotate_projection(
            code,
            re.compile(
                rf"\b((?:{functions})\((?:[^()]|\([^()]*\))*\))\s+AS\s+(\w+)",
                re.IGNORECASE,
            ),
            kind,
        )
    code = _annotate_projection(
        code,
        re.compile(
            r"\b((?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*(?:\s*->\s*(?:\x00\d+\x00|\d+))+?)"
            r"\s+AS\s+(\w+)",
            re.IGNORECASE,
        ),
        "JSONB",
    )
    code = re.sub(r"\bleft\s*\(", "astral_left(", code, flags=re.IGNORECASE)
    code = _annotate_projection(
        code,
        re.compile(
            r"\b((?:valid_assignment_selected_input|pg_input_is_valid|pg_try_advisory_xact_lock(?:_shared)?|pg_is_in_recovery)\([^;]+?\))\s+AS\s+(\w+)",
            re.IGNORECASE,
        ),
        "BOOLEAN",
    )
    return _MASK.sub(lambda match: masked[int(match.group(1))], code)


def classify_control(statement: str) -> str | None:
    for pattern, kind in _CONTROL:
        if pattern.match(statement):
            return kind
    timeout = re.fullmatch(
        r"\s*SET\s+LOCAL\s+(lock_timeout|statement_timeout)\s*=\s*"
        r"'([0-9]+(?:\.[0-9]+)?(?:ms|s|min)?)'\s*;?\s*",
        statement,
        re.IGNORECASE,
    )
    if timeout is not None:
        return f"setting:{timeout[1].lower()}:{timeout[2]}"
    if re.match(r"^\s*(?:SET|SHOW)\b", statement, re.IGNORECASE):
        raise UnsupportedSQLiteConstructError("unsupported SQLite connection control statement")
    return None


def statement_access(statement: str) -> tuple[bool, bool, bool, bool]:
    code = " ".join(text for is_code, text in _segments(statement) if is_code)
    operation = None
    depth = 0
    for token in re.findall(r"[A-Za-z_]\w*|[()]", code):
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0 and token.upper() in {
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "REPLACE",
            "CREATE",
            "DROP",
            "ALTER",
            "PRAGMA",
            "SAVEPOINT",
            "RELEASE",
            "ROLLBACK",
        }:
            operation = token.upper()
            break
    locked = (
        _ROW_LOCK.search(code) is not None
        or re.search(r"\bpg_advisory_xact_lock(?:_shared)?\s*\(", code, re.IGNORECASE) is not None
    )
    write = locked or operation not in {"SELECT", "SAVEPOINT", "RELEASE", "ROLLBACK"}
    scalar = operation == "SELECT" and not re.search(
        r"\b(?:FROM|JOIN|pg_advisory\w*|pg_try_advisory\w*)\b", code, re.IGNORECASE
    )
    locking_select = operation == "SELECT" and _ROW_LOCK.search(code) is not None
    return (
        bool(write),
        bool(scalar),
        locking_select,
        bool(re.search(r"\bNOWAIT\b", code, re.IGNORECASE)),
    )


def is_try_lock(statement: str) -> bool:
    code = " ".join(text for is_code, text in _segments(statement) if is_code)
    pattern = r"\bpg_try_advisory_xact_lock(?:_shared)?\s*\("
    return re.search(pattern, code, re.IGNORECASE) is not None


__all__ = ("UnsupportedSQLiteConstructError", "classify_control", "is_try_lock", "translate")
