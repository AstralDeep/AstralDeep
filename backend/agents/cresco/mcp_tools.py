"""Read-only Cresco topology tools: run bounded wsapi reads against the configured fabric
and render results as astralprims primitives. mcp_server.py routes tool calls to this
registry; every typed client failure becomes an Alert and no mutating surface exists.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Tuple

from astralprims import Alert, Card, KeyValue, Table, Text

from agents.cresco.wsapi_client import (
    CrescoAuthError,
    CrescoError,
    CrescoInvalidArgument,
    CrescoProtocolError,
    CrescoTLSError,
    CrescoUnavailable,
    CrescoUnreachable,
    FabricConfig,
    IDENTIFIER,
    WsapiClient,
)
from shared.feature_flags import flags

logger = logging.getLogger("CrescoTools")

MAX_TABLE_ROWS = 200
MAX_AGENT_PLUGINS = 64
MAX_CELL_CHARS = 256
SCOPE = "tools:read"
CANVAS_NOTE = (
    "A card with this information is already on the user's canvas. Reply with one or two plain "
    "sentences; do not repeat the data as a table and do not output UI components or JSON."
)

FAILURE_SUMMARIES: Tuple[Tuple[type, str, bool], ...] = (
    (CrescoUnavailable, "Cresco fabric not configured.", False),
    (CrescoUnreachable, "Cresco fabric unreachable.", True),
    (CrescoAuthError, "Cresco authentication failed.", False),
    (CrescoTLSError, "Cresco TLS verification failed.", False),
    (CrescoProtocolError, "Unexpected fabric response.", False),
)


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text[:MAX_CELL_CHARS]


def _rows(entries: Any, keys: List[str]) -> List[List[str]]:
    if not isinstance(entries, list):
        raise CrescoProtocolError("Cresco reply did not carry the expected list.")
    rows = []
    for entry in entries[:MAX_TABLE_ROWS]:
        if isinstance(entry, dict):
            rows.append([_cell(entry.get(key)) for key in keys])
        else:
            rows.append([_cell(entry)] + [""] * (len(keys) - 1))
    return rows


def _table(keys: List[str], rows: List[List[str]], total: int) -> Table:
    return Table(headers=keys, rows=rows, total_rows=total, source_tool="cresco")


def _truncation_note(rows: List[List[str]], total: int, noun: str) -> Text:
    if total > len(rows):
        return Text(content=f"Showing the first {len(rows)} of {total} {noun}; ask for a narrower filter to see more.")
    if rows:
        return Text(content=f"{total} {noun}(s) reported by the fabric.")
    return Text(content=f"The fabric reported no {noun}.")


def _failure_result(exc: CrescoError) -> Dict[str, Any]:
    for error_type, summary, retryable in FAILURE_SUMMARIES:
        if isinstance(exc, error_type):
            alert = Alert(message=summary, variant="error", title="Cresco read failed")
            return {
                "_ui_components": [alert.to_dict()],
                "_data": {
                    "summary": summary,
                    "retryable": bool(getattr(exc, "retryable", False) and retryable),
                    "detail": str(exc)[:MAX_CELL_CHARS],
                    "presentation": CANVAS_NOTE,
                },
            }
    logger.warning("Unclassified Cresco tool failure: %s", type(exc).__name__)
    alert = Alert(message="Unexpected fabric response.", variant="error", title="Cresco read failed")
    return {
        "_ui_components": [alert.to_dict()],
        "_data": {"summary": "Unexpected fabric response.", "retryable": False, "presentation": CANVAS_NOTE},
    }


def _disabled_result() -> Dict[str, Any]:
    alert = Alert(message="Cresco tools are disabled by feature flag.", variant="error", title="Cresco read failed")
    return {
        "_ui_components": [alert.to_dict()],
        "_data": {
            "summary": "Cresco tools are disabled by feature flag.",
            "retryable": False,
            "presentation": CANVAS_NOTE,
        },
    }


def _invalid_result(exc: CrescoInvalidArgument) -> Dict[str, Any]:
    alert = Alert(message=str(exc)[:MAX_CELL_CHARS], variant="error", title="Invalid Cresco request")
    return {
        "_ui_components": [alert.to_dict()],
        "_data": {"summary": str(exc)[:MAX_CELL_CHARS], "retryable": False, "presentation": CANVAS_NOTE},
    }


def _result(components: List[Any], data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "_ui_components": [component.to_dict() for component in components],
        "_data": {**data, "presentation": CANVAS_NOTE},
    }


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise CrescoInvalidArgument(
            f"The {label} identifier must be 1-128 letters, digits, dots, dashes or underscores."
        )
    return value


def _read(tool_name: str, operation: Callable[[WsapiClient], Dict[str, Any]]) -> Dict[str, Any]:
    if not flags.is_enabled("cresco"):
        return _disabled_result()
    try:
        config = FabricConfig.from_environment()
        if not config.configured:
            return _failure_result(CrescoUnavailable("Cresco fabric not configured."))
        return operation(WsapiClient(config))
    except CrescoInvalidArgument as exc:
        return _invalid_result(exc)
    except CrescoError as exc:
        logger.info("Cresco tool '%s' failed: %s", tool_name, type(exc).__name__)
        return _failure_result(exc)


def cresco_list_regions(**kwargs: Any) -> Dict[str, Any]:
    def operation(client: WsapiClient) -> Dict[str, Any]:
        reply = client.call("listregions")
        keys = ["region", "agents", "status"]
        entries = reply.get("regions")
        rows = _rows(entries, keys)
        total = len(entries) if isinstance(entries, list) else 0
        return _result(
            [_table(keys, rows, total), _truncation_note(rows, total, "region")],
            {"summary": f"Cresco fabric reports {total} region(s).", "regions": total},
        )

    return _read("cresco_list_regions", operation)


def cresco_list_agents(**kwargs: Any) -> Dict[str, Any]:
    def operation(client: WsapiClient) -> Dict[str, Any]:
        region = _identifier(kwargs.get("region") or "", "region") if kwargs.get("region") else ""
        reply = client.call("listagents", {"region": region} if region else {}, region=region)
        keys = ["region", "agent", "status"]
        entries = reply.get("agents")
        rows = _rows(entries, keys)
        total = len(entries) if isinstance(entries, list) else 0
        scope = f" in region '{region}'" if region else ""
        return _result(
            [_table(keys, rows, total), _truncation_note(rows, total, "agent")],
            {"summary": f"Cresco fabric reports {total} agent(s){scope}.", "agents": total, "region": region},
        )

    return _read("cresco_list_agents", operation)


def cresco_agent_info(**kwargs: Any) -> Dict[str, Any]:
    def operation(client: WsapiClient) -> Dict[str, Any]:
        region = _identifier(kwargs.get("region"), "region")
        agent = _identifier(kwargs.get("agent"), "agent")
        reply = client.call(
            "getagentinfo", {"region": region, "agent": agent}, region=region, agent=agent
        )
        state = _cell(reply.get("agent_status"))
        keys = ["plugin", "state", "health"]
        plugins = reply.get("plugins")
        rows = _rows(plugins, keys)[:MAX_AGENT_PLUGINS]
        plugin_total = len(plugins) if isinstance(plugins, list) else 0
        summary = Card(
            title=f"Agent {agent} in region {region}",
            content=[KeyValue(title="Reported state", items=[{"label": "Status", "value": state or "unknown"}])],
        )
        return _result(
            [summary, _table(keys, rows, plugin_total)],
            {
                "summary": (
                    f"Agent '{agent}' in region '{region}' reports status {state or 'unknown'} "
                    f"with {plugin_total} plugin(s)."
                ),
                "region": region,
                "agent": agent,
            },
        )

    return _read("cresco_agent_info", operation)


def cresco_get_sysinfo(**kwargs: Any) -> Dict[str, Any]:
    def operation(client: WsapiClient) -> Dict[str, Any]:
        region = _identifier(kwargs.get("region"), "region")
        agent = _identifier(kwargs.get("agent"), "agent")
        reply = client.call(
            "getsysinfo", {"region": region, "agent": agent}, region=region, agent=agent
        )
        sysinfo = reply.get("sysinfo")
        if not isinstance(sysinfo, dict):
            raise CrescoProtocolError("Cresco reply did not carry host system information.")
        items = [
            {"label": _cell(key)[:64], "value": _cell(value)}
            for key, value in list(sysinfo.items())[:MAX_AGENT_PLUGINS]
        ]
        panel = KeyValue(title=f"Host system information for {agent} in {region}", items=items)
        return _result(
            [panel],
            {
                "summary": (
                    f"Collected host system information for agent '{agent}' in region '{region}'."
                ),
                "region": region,
                "agent": agent,
            },
        )

    return _read("cresco_get_sysinfo", operation)


def _identifier_property(description: str) -> Dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": "^[A-Za-z0-9_.-]{1,128}$",
        "description": description,
    }


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "cresco_list_regions": {
        "function": cresco_list_regions,
        "scope": SCOPE,
        "description": (
            "List the regions reported by the configured Cresco edge fabric, including per-region "
            "agent counts and status. Read-only topology information."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "cresco_list_agents": {
        "function": cresco_list_agents,
        "scope": SCOPE,
        "description": (
            "List the agents registered in the Cresco edge fabric with their region and status. "
            "Pass region to narrow the listing. Read-only topology information."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"region": _identifier_property("Optional region identifier to filter by")},
            "additionalProperties": False,
        },
    },
    "cresco_agent_info": {
        "function": cresco_agent_info,
        "scope": SCOPE,
        "description": (
            "Show one Cresco agent's reported state and its installed plugins with health. "
            "Requires region and agent identifiers. Read-only topology information."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "region": _identifier_property("Region identifier of the agent"),
                "agent": _identifier_property("Agent identifier within the region"),
            },
            "required": ["region", "agent"],
            "additionalProperties": False,
        },
    },
    "cresco_get_sysinfo": {
        "function": cresco_get_sysinfo,
        "scope": SCOPE,
        "description": (
            "Read the host system information (sysinfo) published by one Cresco agent. "
            "Requires region and agent identifiers. Read-only topology information."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "region": _identifier_property("Region identifier of the agent"),
                "agent": _identifier_property("Agent identifier within the region"),
            },
            "required": ["region", "agent"],
            "additionalProperties": False,
        },
    },
}
