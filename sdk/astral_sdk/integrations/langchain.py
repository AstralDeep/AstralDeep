"""LangChain ``StructuredTool`` wrappers (extra: ``langchain``).

Nothing here imports ``langchain_core``/``langchain`` at module load — only
:func:`as_langchain_tools` does, on first call.
"""
from __future__ import annotations

from typing import Any

from astral_sdk.client import AstralClient
from astral_sdk.integrations.generic import FUNCTION_SCHEMAS, call_tool


def as_langchain_tools(client: AstralClient) -> list[Any]:
    """One ``langchain_core.tools.StructuredTool`` per Astral Work tool, bound to ``client``.

    Each tool's ``func`` closes over ``client`` and the tool's own name, and
    forwards to :func:`astral_sdk.integrations.generic.call_tool` — the exact
    same dispatch every other adapter in this package uses.
    """
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
            args_schema=None,  # JSON Schema is passed straight through below.
        )

    tools = [_make(schema) for schema in FUNCTION_SCHEMAS]
    # ``StructuredTool.from_function`` infers a schema from the Python
    # signature (which is just ``**arguments`` here); overwrite it with
    # Astral's own JSON Schema so the model sees the real parameter contract.
    for tool, schema in zip(tools, FUNCTION_SCHEMAS):
        tool.args_schema = None
        tool.args = schema["parameters"].get("properties", {})
    return tools


__all__ = ["as_langchain_tools"]
