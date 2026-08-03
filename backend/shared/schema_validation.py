"""Bounded, offline JSON Schema validation for agent tool contracts.

Astral accepts JSON Schema 2020-12 at the registration boundary. Validation
never retrieves a URI: remote references are rejected and local references
must resolve inside the submitted document. This is deliberately a structural
validator, not a runtime instance validator, and uses only the standard
library so feature 064 adds no dependency.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit


JSON_SCHEMA_2020_12 = "https://json-schema.org/draft/2020-12/schema"
_KNOWN_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_SCHEMA_MAP_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SCHEMA_LIST_KEYWORDS = frozenset(
    {"allOf", "anyOf", "oneOf", "prefixItems"}
)


class ToolSchemaError(ValueError):
    """A tool schema is unsafe or not valid enough to register."""


def _label(tool_name: str, reason: str) -> ToolSchemaError:
    return ToolSchemaError(f"tool {tool_name!r}: {reason}")


def _resolve_local_ref(root: Any, ref: str, tool_name: str) -> None:
    if not ref.startswith("#"):
        parts = urlsplit(ref)
        if parts.scheme or parts.netloc or ref.startswith("//"):
            raise _label(tool_name, f"network or external $ref is forbidden: {ref}")
        raise _label(tool_name, f"non-local $ref is forbidden: {ref}")
    if ref == "#":
        return
    if not ref.startswith("#/"):
        raise _label(tool_name, f"unsupported local $ref fragment: {ref}")
    current = root
    for raw_token in ref[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and token in current:
            current = current[token]
            continue
        if isinstance(current, list):
            try:
                current = current[int(token)]
                continue
            except (ValueError, IndexError):
                pass
        raise _label(tool_name, f"unresolvable local $ref: {ref}")


def validate_tool_schema(
    schema: Any,
    tool_name: str,
    *,
    require_object_root: bool,
    max_depth: int = 32,
    max_subschemas: int = 512,
    max_bytes: int = 262_144,
) -> dict[str, Any]:
    """Validate without mutation, then return a dialect-declared deep copy."""

    if not isinstance(tool_name, str) or not tool_name:
        raise ToolSchemaError("tool name is required for schema validation")
    if not isinstance(schema, Mapping):
        raise _label(tool_name, "schema must be an object")
    try:
        encoded = json.dumps(schema, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise _label(tool_name, "schema must contain only finite JSON values") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise _label(tool_name, f"schema exceeds the {max_bytes}-byte limit")

    dialect = schema.get("$schema")
    if dialect is not None and dialect != JSON_SCHEMA_2020_12:
        raise _label(tool_name, f"unsupported JSON Schema dialect: {dialect}")
    if require_object_root and schema.get("type") != "object":
        raise _label(tool_name, "input schema root type must be object")

    seen = 0

    def walk(node: Any, path: str, depth: int) -> None:
        nonlocal seen
        if depth > max_depth:
            raise _label(tool_name, f"schema depth exceeds {max_depth} at {path}")
        if isinstance(node, bool):
            return
        if not isinstance(node, Mapping):
            raise _label(tool_name, f"subschema at {path} must be an object or boolean")
        seen += 1
        if seen > max_subschemas:
            raise _label(tool_name, f"schema exceeds {max_subschemas} subschemas")

        declared_type = node.get("type")
        if declared_type is not None:
            types = declared_type if isinstance(declared_type, list) else [declared_type]
            if (
                not types
                or any(not isinstance(item, str) or item not in _KNOWN_TYPES for item in types)
            ):
                raise _label(tool_name, f"invalid type declaration at {path}")

        if "required" in node:
            required = node["required"]
            if (
                not isinstance(required, list)
                or any(not isinstance(item, str) for item in required)
                or len(set(required)) != len(required)
            ):
                raise _label(tool_name, f"required at {path} must be unique strings")

        ref = node.get("$ref")
        if ref is not None:
            if not isinstance(ref, str):
                raise _label(tool_name, f"$ref at {path} must be a string")
            _resolve_local_ref(schema, ref, tool_name)

        for keyword in ("properties", "patternProperties", "$defs", "dependentSchemas"):
            children = node.get(keyword)
            if children is None:
                continue
            if not isinstance(children, Mapping):
                raise _label(tool_name, f"{keyword} at {path} must be an object")
            for name, child in children.items():
                if not isinstance(name, str):
                    raise _label(tool_name, f"{keyword} names at {path} must be strings")
                walk(child, f"{path}/{keyword}/{name}", depth + 1)

        for keyword in _SCHEMA_MAP_KEYWORDS:
            if keyword in node:
                walk(node[keyword], f"{path}/{keyword}", depth + 1)
        for keyword in _SCHEMA_LIST_KEYWORDS:
            children = node.get(keyword)
            if children is None:
                continue
            if (
                not isinstance(children, Sequence)
                or isinstance(children, (str, bytes, bytearray))
                or not children
            ):
                raise _label(tool_name, f"{keyword} at {path} must be a non-empty array")
            for index, child in enumerate(children):
                walk(child, f"{path}/{keyword}/{index}", depth + 1)

    walk(schema, "#", 0)
    validated = copy.deepcopy(dict(schema))
    validated.setdefault("$schema", JSON_SCHEMA_2020_12)
    return validated

