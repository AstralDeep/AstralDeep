"""Framework-agnostic function-calling schemas and dispatch.

Every other adapter in this package (OpenAI, Anthropic, LangChain, ...) is a
thin re-shaping of what is defined here — this module has no third-party
import at all, lazy or otherwise.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from astral_sdk.client import AstralClient
from astral_sdk.tools import TOOL_NAMES, all_function_schemas, tools_for_scopes

#: ``[{"name", "description", "parameters"}, ...]`` — one per Astral Work tool,
#: in the exact shape most function-calling APIs expect for a single tool.
FUNCTION_SCHEMAS: list[dict[str, Any]] = all_function_schemas()


def schemas_for_scopes(scopes) -> list[dict[str, Any]]:
    """The subset of :data:`FUNCTION_SCHEMAS` a credential's scopes actually unlock."""
    granted = tools_for_scopes(scopes)
    return [schema for schema in FUNCTION_SCHEMAS if schema["name"] in granted]


def call_tool(client: AstralClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one Astral tool by name against a live client; return a plain dict.

    This is the single execution path every framework adapter below routes
    through — a framework's own "tool executed" callback need only call this
    and hand the dict back to its model.
    """
    if name not in TOOL_NAMES:
        raise KeyError(f"unknown Astral tool: {name}")
    dispatch = {
        "astral_submit_operation": lambda: client.submit_operation(**arguments),
        "astral_get_operation": lambda: client.get_operation(arguments["operation_id"]),
        "astral_list_operations": lambda: client.list_operations(
            **{k: v for k, v in arguments.items() if k in ("limit", "after_id")}),
        "astral_get_operation_events": lambda: client.poll_operation(
            arguments["operation_id"], after_revision=arguments.get("after_revision")),
        "astral_cancel_operation": lambda: client.cancel_operation(
            arguments["operation_id"], submission_id=arguments.get("submission_id"),
            expected_revision=arguments["expected_revision"]),
        "astral_pause_operation": lambda: client.pause_operation(
            arguments["operation_id"], submission_id=arguments.get("submission_id"),
            expected_revision=arguments["expected_revision"]),
        "astral_get_artifact": lambda: client.get_artifact(arguments["operation_id"]),
    }[name]
    return asdict(dispatch())


__all__ = ["FUNCTION_SCHEMAS", "schemas_for_scopes", "call_tool"]
