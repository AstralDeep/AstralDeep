"""LangChain StructuredTool wrapper over astral_sdk.client and integrations/generic.py;
only as_langchain_tools() imports langchain_core, and only on first call.
"""

from __future__ import annotations

from typing import Any

from astral_sdk.client import AstralClient
from astral_sdk.integrations.generic import FUNCTION_SCHEMAS, call_tool


def as_langchain_tools(client: AstralClient) -> list[Any]:
    try:
        from langchain_core.tools import StructuredTool
    except ImportError as exc:
        raise ImportError(
            "the `langchain-core` package is required for astral_sdk.integrations.langchain; "
            "install it with: pip install astral-sdk[langchain]"
        ) from exc

    def _make(schema: dict[str, Any]) -> Any:
        name = schema["name"]

        def _run(**arguments: Any) -> dict[str, Any]:
            return call_tool(client, name, arguments)

        return StructuredTool.from_function(
            func=_run, name=name, description=schema["description"],
            args_schema=None,
        )

    tools = [_make(schema) for schema in FUNCTION_SCHEMAS]
    for tool, schema in zip(tools, FUNCTION_SCHEMAS):
        tool.args_schema = None
        tool.args = schema["parameters"].get("properties", {})
    return tools


__all__ = ["as_langchain_tools"]
