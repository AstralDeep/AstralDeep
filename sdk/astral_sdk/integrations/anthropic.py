"""Anthropic Messages API tool-use adapter over astral_sdk.client and
integrations/generic.py; require_anthropic() is the only place the anthropic package
is imported, and only on first call.
"""

from __future__ import annotations

from typing import Any

from astral_sdk.client import AstralClient
from astral_sdk.integrations.generic import FUNCTION_SCHEMAS, call_tool


def require_anthropic() -> Any:
    try:
        import anthropic  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "the `anthropic` package is required for astral_sdk.integrations.anthropic; "
            "install it with: pip install astral-sdk[anthropic]"
        ) from exc
    return anthropic


def as_anthropic_tools() -> list[dict[str, Any]]:
    return [
        {"name": schema["name"], "description": schema["description"],
        "input_schema": schema["parameters"]}
        for schema in FUNCTION_SCHEMAS
    ]


def execute_tool_use_block(client: AstralClient, block: Any) -> dict[str, Any]:
    name = getattr(block, "name", None) or block["name"]
    arguments = getattr(block, "input", None)
    if arguments is None:
        arguments = block["input"]
    return call_tool(client, name, dict(arguments))


def tool_result_block(tool_use_id: str, result: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    import json

    return {
        "type": "tool_result", "tool_use_id": tool_use_id,
        "content": json.dumps(result, default=str), "is_error": is_error,
    }


__all__ = ["require_anthropic", "as_anthropic_tools", "execute_tool_use_block", "tool_result_block"]
