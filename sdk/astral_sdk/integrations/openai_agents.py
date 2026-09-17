"""OpenAI function-calling / Assistants-style tool definitions (extra: ``openai``).

Nothing in this module imports ``openai`` at module load time — only
:func:`require_openai` does, on first call, so simply importing
``astral_sdk.integrations.openai_agents`` never requires the package.
"""
from __future__ import annotations

from typing import Any

from astral_sdk.client import AstralClient
from astral_sdk.integrations.generic import FUNCTION_SCHEMAS, call_tool


def require_openai() -> Any:
    try:
        import openai  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "the `openai` package is required for astral_sdk.integrations.openai_agents; "
            "install it with: pip install astral-sdk[openai]"
        ) from exc
    return openai


def as_openai_tools() -> list[dict[str, Any]]:
    """Astral's tools as OpenAI Chat Completions ``tools=[...]`` entries.

    Pure data shaping — does not require ``openai`` to be installed, since the
    Chat Completions tool schema has no client-side type to import.
    """
    return [{"type": "function", "function": schema} for schema in FUNCTION_SCHEMAS]


def execute_tool_call(client: AstralClient, tool_call: Any) -> dict[str, Any]:
    """Run one OpenAI ``tool_call`` (Chat Completions or Assistants shape) against Astral.

    Accepts either object form (``tool_call.function.name`` /
    ``tool_call.function.arguments`` as a JSON string) or a plain dict with the
    same keys — callers on either the Chat Completions or Assistants API
    surface pass slightly different shapes for the same data.
    """
    import json

    function = getattr(tool_call, "function", None) or tool_call["function"]
    name = getattr(function, "name", None) or function["name"]
    raw_arguments = getattr(function, "arguments", None)
    if raw_arguments is None:
        raw_arguments = function["arguments"]
    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
    return call_tool(client, name, arguments)


__all__ = ["require_openai", "as_openai_tools", "execute_tool_call"]
