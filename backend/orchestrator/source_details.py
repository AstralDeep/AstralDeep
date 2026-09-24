"""Keeps retrieved research material in closed source disclosures while preserving its content and identity.
Tool delivery and saved conversation presentation use the same existing collapsible primitive.
"""

from collections.abc import Mapping
from typing import Any


def present_source_details(component: Mapping[str, Any]) -> Mapping[str, Any]:
    if component.get("type") != "card" or component.get("_source_agent") != "web-research-1":
        return component
    tool = str(component.get("_source_tool", "")).rsplit("__", 1)[-1]
    if tool not in {"fetch_page", "web_search"}:
        return component
    return {**component, "type": "collapsible", "default_open": False,
            "title": component.get("title") or ("Search results" if tool == "web_search" else "Source details")}


def prioritize_completed_dashboard(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def walk(node: Any):
        if not isinstance(node, Mapping):
            return
        yield node
        for key in ("content", "children", "tabs"):
            if isinstance(node.get(key), list):
                for child in node[key]:
                    yield from walk(child)

    def tool_is(node: Mapping[str, Any], agent: str, tool: str) -> bool:
        return node.get("_source_agent") == agent and str(node.get("_source_tool", "")).rsplit("__", 1)[-1] == tool

    dashboard = -1
    for position, component in enumerate(components):
        if any(tool_is(node, "connectors-1", "interactive_artifacts") for node in walk(component)):
            dashboard = position
    if dashboard < 0:
        return components
    primary, details = [], []
    for position, component in enumerate(components):
        if (isinstance(component, Mapping)
                and component.get("type") in {"card", "collapsible"}
                and ((position < dashboard and tool_is(component, "weather-1", "get_weekly_forecast"))
                     or tool_is(component, "web-research-1", "fetch_page")
                     or tool_is(component, "web-research-1", "web_search"))
                and not any(node.get("type") == "alert" and node.get("variant") in {"warning", "error"} for node in walk(component))):
            details.append({**component, "type": "collapsible", "default_open": False})
        else:
            primary.append(component)
    return primary + details
