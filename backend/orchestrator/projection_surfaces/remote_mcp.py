"""Composes the owner settings surface for remote MCP server connectors and binds its
management actions to the encrypted connector service and OAuth authorization flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote

from webrender.chrome import esc, notice_block

from orchestrator import remote_mcp_connectors as connectors

TITLE = "Remote MCP connectors"
ICON = "⇄"
SURFACE_KEY = "remote_mcp"
_INPUT = "rounded-lg bg-white/10 border border-white/10 px-3 py-2 text-sm text-astral-text w-full"
logger = logging.getLogger("Orchestrator.Chrome.RemoteMCP")
_TRANSPORT_LABELS = {"streamable_http": "Streamable HTTP", "sse": "Legacy SSE"}
_AUTH_LABELS = {"none": "None", "bearer": "Bearer token", "oauth": "OAuth"}


def _enabled() -> bool:
    from shared.feature_flags import flags
    return flags.is_enabled("remote_mcp")


def _fields(payload: Any) -> dict[str, str]:
    raw = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {key: str(value).strip() for key, value in raw.items()
            if isinstance(key, str) and value is not None and not isinstance(value, (dict, list))}


def _select(name: str, options: tuple[tuple[str, str], ...], current: str) -> str:
    values = "".join(f'<option value="{esc(value)}"{" selected" if value == current else ""}>{esc(label)}</option>'
                     for value, label in options)
    return f'<label>{esc(name.replace("_", " ").title())}<select name="{esc(name)}" class="{_INPUT}">{values}</select></label>'


def _text(name: str, label: str, value: str = "", input_type: str = "text") -> str:
    return (f'<label class="block text-sm">{esc(label)}'
            f'<input name="{esc(name)}" type="{input_type}" value="{esc(value)}" autocomplete="off" class="{_INPUT}"></label>')


def _notice_for_error(error: Exception) -> str:
    from orchestrator.remote_mcp_oauth import RemoteMCPOAuthError

    if isinstance(error, (connectors.RemoteMCPError, RemoteMCPOAuthError)):
        message = str(error)[:300]
    else:
        logger.warning("Remote MCP surface action failed type=%s", type(error).__name__)
        message = "The connector action failed. Check its settings and try again."
    return notice_block("error", esc(message))


def _disabled_result():
    if _enabled():
        return None
    return (SURFACE_KEY, {}, notice_block("error", "Remote MCP connectors are disabled on this server."))


async def render(orch: Any, user_id: str, roles: Any, params: Any) -> str:
    if not _enabled():
        return '<p class="text-sm text-astral-muted">Remote MCP connectors are disabled on this server.</p>'
    rows = await asyncio.to_thread(connectors.list_connectors, orch, user_id)
    params = params if isinstance(params, dict) else {}
    edit_id = params.get("edit") if isinstance(params.get("edit"), str) else ""
    edit_config = None
    if edit_id:
        try:
            edit_config = await asyncio.to_thread(connectors.get_connector, orch, user_id, edit_id)
        except connectors.RemoteMCPError:
            edit_id = ""
    cards = []
    for connector in rows:
        cid = esc(connector["id"])
        auth_button = (
            f'<button data-ui-action="chrome_remote_mcp_oauth_start" '
            f'data-ui-payload="{{&quot;connector_id&quot;:&quot;{cid}&quot;}}">Authorize</button>'
            if connector.get("auth_type") == "oauth" else ""
        )
        tools = []
        for tool in connector.get("tools_catalog", []):
            name = tool.get("name", "")
            checked = " checked" if connector.get("tools", {}).get(name) else ""
            tools.append(
                '<div class="flex justify-between gap-2 border-t border-white/10 py-2">'
                f'<span>{esc(name)}<small class="block text-astral-muted">{esc(tool.get("description", ""))}</small></span>'
                f'<button data-ui-action="chrome_remote_mcp_toggle_tool" data-ui-payload="{esc(json.dumps({"connector_id": connector["id"], "tool_name": name, "enabled": not bool(checked)}))}">'
                f'{"Disable" if checked else "Enable"}</button></div>')
        cards.append(
            '<section class="rounded-lg border border-white/10 p-3 space-y-2">'
            f'<div class="font-medium">{esc(connector["name"])}</div>'
            f'<div class="text-xs text-astral-muted">{esc(connector["url"])} · '
            f'{esc(_TRANSPORT_LABELS.get(connector["transport"], connector["transport"]))} · '
            f'{esc(_AUTH_LABELS.get(connector["auth_type"], connector["auth_type"]))}</div>'
            f'<div>{"Saved credential present" if connector.get("has_credential") else "No credential"}</div>'
            f'{notice_block("error", esc(connector.get("last_error", ""))) if connector.get("last_error") else ""}'
            f'<div>{"".join(tools) or "No tools loaded. Test the connection to load available tools."}</div>'
            f'<button data-ui-action="chrome_remote_mcp_test" data-ui-payload="{{&quot;connector_id&quot;:&quot;{cid}&quot;}}">Test and refresh tools</button>'
            f'{auth_button}'
            f'<button data-ui-action="chrome_remote_mcp_edit" data-ui-payload="{{&quot;connector_id&quot;:&quot;{cid}&quot;}}">Edit</button>'
            f'<button data-ui-action="chrome_remote_mcp_toggle_connector" data-ui-payload="{{&quot;connector_id&quot;:&quot;{cid}&quot;,&quot;enabled&quot;:{str(not connector.get("enabled")).lower()}}}">{"Disable" if connector.get("enabled") else "Enable"}</button>'
            f'<button data-ui-action="chrome_remote_mcp_delete" data-ui-payload="{{&quot;connector_id&quot;:&quot;{cid}&quot;}}">Delete connector</button>'
            '</section>')
    form = (
        '<form data-ui-form class="space-y-3 rounded-lg border border-white/10 p-3">'
        '<h3>Add or update an MCP server</h3>'
        f'<input type="hidden" name="connector_id" value="{esc(edit_id)}">'
        f'{_text("name", "Name", edit_config.get("name", "") if edit_config else "")}'
        f'{_text("url", "HTTPS server URL", edit_config.get("url", "https://") if edit_config else "https://")}'
        f'{_select("transport", (("streamable_http", "Streamable HTTP"), ("sse", "Legacy SSE")), edit_config.get("transport", "streamable_http") if edit_config else "streamable_http")}'
        f'{_select("auth_type", (("none", "None"), ("bearer", "Bearer token"), ("oauth", "OAuth")), edit_config.get("auth_type", "none") if edit_config else "none")}'
        f'{_text("token", "Bearer token", input_type="password")}'
        f'{_text("oauth_client_id", "OAuth client ID (optional when dynamic registration is supported)", edit_config.get("oauth_client_id", "") if edit_config else "")}'
        f'{_text("oauth_client_secret", "OAuth client secret", input_type="password")}'
        + ('<label class="block text-sm"><input name="clear_oauth_client_credentials" type="checkbox" value="true"> Clear saved OAuth client credentials and use dynamic registration</label>'
           if edit_config and edit_config.get("auth_type") == "oauth" else "")
        + '<button data-ui-action="chrome_remote_mcp_save" data-ui-collect="true">Save server</button>'
        '</form>')
    oauth_url = params.get("oauth_url") if isinstance(params.get("oauth_url"), str) else ""
    oauth_link = (f'<a rel="noopener noreferrer" target="_blank" href="{esc(oauth_url)}">Open OAuth authorization page</a>'
                  if oauth_url else "")
    return ("<div class=\"space-y-4\"><p class=\"text-sm text-astral-muted\">"
            "Remote servers provide tools to your account. Configure and enable each server tool before chat can call it. "
            "Calls go through normal permissions, policy, and audit controls.</p>"
            + oauth_link + "".join(cards) + form + "</div>")


async def components(orch: Any, user_id: str, roles: Any, params: Any):
    from webrender.chrome.surfaces import _sdui

    if not _enabled():
        return [_sdui.alert("Remote MCP connectors are disabled on this server.", "warning")]
    rows = await asyncio.to_thread(connectors.list_connectors, orch, user_id)
    out = [_sdui.text("Remote servers provide tools to your account. Enable each server tool before chat can call it; calls use normal permissions and audit.", "caption")]
    if isinstance(params, dict) and isinstance(params.get("oauth_url"), str):
        encoded_url = quote(params["oauth_url"], safe=":/?&=%")
        out.append(_sdui.text(f'[Open OAuth authorization page](<{encoded_url}>)'))
        out.append(_sdui.text("Authorization URL (copy if the link does not open): " + params["oauth_url"], "caption"))
    for connector in rows:
        entries = [{"label": "Endpoint", "value": connector["url"]},
                   {"label": "Transport", "value": _TRANSPORT_LABELS.get(connector["transport"], connector["transport"])},
                   {"label": "Authentication", "value": _AUTH_LABELS.get(connector["auth_type"], connector["auth_type"])},
                   {"label": "Credential", "value": "Saved" if connector.get("has_credential") else "None"}]
        buttons = [
            _sdui.button("Test and refresh tools", "chrome_remote_mcp_test", {"connector_id": connector["id"]}),
            _sdui.button("Edit", "chrome_remote_mcp_edit", {"connector_id": connector["id"]}),
            _sdui.button("Disable" if connector.get("enabled") else "Enable",
                         "chrome_remote_mcp_toggle_connector",
                         {"connector_id": connector["id"], "enabled": not connector.get("enabled")}),
            _sdui.button("Delete connector", "chrome_remote_mcp_delete", {"connector_id": connector["id"]}, "danger"),
        ]
        if connector.get("auth_type") == "oauth":
            buttons.insert(0, _sdui.button("Authorize", "chrome_remote_mcp_oauth_start",
                                           {"connector_id": connector["id"]}))
        tool_rows = []
        for tool in connector.get("tools_catalog", []):
            tool_rows.append(_sdui.container([
                _sdui.text(f'{tool["name"]}: {tool.get("description", "")}', "caption"),
                _sdui.button("Disable" if connector.get("tools", {}).get(tool["name"]) else "Enable",
                              "chrome_remote_mcp_toggle_tool",
                              {"connector_id": connector["id"], "tool_name": tool["name"],
                               "enabled": not connector.get("tools", {}).get(tool["name"])})], direction="row"))
        out.append(_sdui.card(connector["name"], [_sdui.key_value(entries), *tool_rows,
                                                 _sdui.container(buttons, direction="row")]))
    edit_id = params.get("edit") if isinstance(params, dict) and isinstance(params.get("edit"), str) else ""
    edit_config = None
    if edit_id:
        try:
            edit_config = await asyncio.to_thread(connectors.get_connector, orch, user_id, edit_id)
        except connectors.RemoteMCPError:
            edit_id = ""
    fields = [
        _sdui.field("name", "Name", default=edit_config.get("name") if edit_config else None),
        _sdui.field("url", "HTTPS server URL", default=edit_config.get("url") if edit_config else None),
        _sdui.field("transport", "Transport", "select", default=edit_config.get("transport", "streamable_http") if edit_config else "streamable_http",
                    options=[{"value": "streamable_http", "label": "Streamable HTTP"},
                             {"value": "sse", "label": "Legacy SSE"}]),
        _sdui.field("auth_type", "Authentication", "select", default=edit_config.get("auth_type", "none") if edit_config else "none",
                    options=[{"value": "none", "label": "None"},
                             {"value": "bearer", "label": "Bearer token"},
                             {"value": "oauth", "label": "OAuth"}]),
        _sdui.field("token", "Bearer token", "password",
                    visible_when={"field": "auth_type", "equals": "bearer", "default": "none"}),
        _sdui.field("oauth_client_id", "OAuth client ID", default=edit_config.get("oauth_client_id") if edit_config else None,
                    visible_when={"field": "auth_type", "equals": "oauth", "default": "none"}),
        _sdui.field("oauth_client_secret", "OAuth client secret", "password",
                    visible_when={"field": "auth_type", "equals": "oauth", "default": "none"}),
    ]
    if edit_config and edit_config.get("auth_type") == "oauth":
        fields.append(_sdui.field(
            "clear_oauth_client_credentials", "Clear saved OAuth client credentials and use dynamic registration",
            "boolean", default=False, visible_when={"field": "auth_type", "equals": "oauth"}))
    out.append(_sdui.form(fields, submit_action="chrome_remote_mcp_save", submit_label="Save server",
                          submit_payload={"connector_id": edit_id} if edit_id else {},
                          title="Edit MCP server" if edit_id else "Add MCP server"))
    return out


async def _save(orch, websocket, user_id, roles, payload):
    if disabled := _disabled_result():
        return disabled
    fields = _fields(payload)
    connector_id = fields.get("connector_id") or (payload.get("connector_id") if isinstance(payload, dict) else "")
    config = {"name": fields.get("name"), "url": fields.get("url"),
              "transport": fields.get("transport"), "auth_type": fields.get("auth_type"),
              "token": fields.get("token", ""), "oauth_client_id": fields.get("oauth_client_id", ""),
              "oauth_client_secret": fields.get("oauth_client_secret", ""), "enabled": True}
    clear_oauth_client_credentials = fields.get("clear_oauth_client_credentials", "").casefold() == "true"
    if connector_id:
        config["id"] = connector_id
    try:
        await asyncio.to_thread(connectors.save_connector, orch, user_id, config,
                                 clear_oauth_client_credentials=clear_oauth_client_credentials,
                                 require_existing=bool(connector_id))
    except Exception as error:  # noqa: BLE001
        return (SURFACE_KEY, {}, _notice_for_error(error))
    return (SURFACE_KEY, {}, notice_block("success", "Remote MCP server saved."))


async def _test(orch, websocket, user_id, roles, payload):
    if disabled := _disabled_result():
        return disabled
    connector_id = payload.get("connector_id") if isinstance(payload, dict) else None
    try:
        discovered = await asyncio.to_thread(connectors.refresh_tools, orch, user_id, connector_id)
        return (SURFACE_KEY, {}, notice_block("success", f"Connected. Found {len(discovered)} tools; tools start disabled."))
    except Exception as error:  # noqa: BLE001
        return (SURFACE_KEY, {}, _notice_for_error(error))


async def _toggle(orch, websocket, user_id, roles, payload):
    if disabled := _disabled_result():
        return disabled
    try:
        await asyncio.to_thread(connectors.set_tool_selection, orch, user_id,
                                 payload.get("connector_id"), payload.get("tool_name"),
                                 payload.get("enabled") is True)
        return (SURFACE_KEY, {}, notice_block("success", "Tool selection saved."))
    except Exception as error:  # noqa: BLE001
        return (SURFACE_KEY, {}, _notice_for_error(error))


async def _delete(orch, websocket, user_id, roles, payload):
    if disabled := _disabled_result():
        return disabled
    try:
        await asyncio.to_thread(connectors.delete_connector, orch, user_id, payload.get("connector_id"))
        return (SURFACE_KEY, {}, notice_block("success", "Connector deleted."))
    except Exception as error:  # noqa: BLE001
        return (SURFACE_KEY, {}, _notice_for_error(error))


async def _edit(orch, websocket, user_id, roles, payload):
    if disabled := _disabled_result():
        return disabled
    connector_id = payload.get("connector_id") if isinstance(payload, dict) else None
    try:
        await asyncio.to_thread(connectors.get_connector, orch, user_id, connector_id)
        return (SURFACE_KEY, {"edit": connector_id}, "")
    except Exception as error:  # noqa: BLE001
        return (SURFACE_KEY, {}, _notice_for_error(error))


async def _toggle_connector(orch, websocket, user_id, roles, payload):
    if disabled := _disabled_result():
        return disabled
    try:
        await asyncio.to_thread(connectors.set_connector_enabled, orch, user_id,
                                payload.get("connector_id"), payload.get("enabled") is True)
        return (SURFACE_KEY, {}, notice_block("success", "Connector status saved."))
    except Exception as error:  # noqa: BLE001
        return (SURFACE_KEY, {}, _notice_for_error(error))


async def _oauth_start(orch, websocket, user_id, roles, payload):
    if disabled := _disabled_result():
        return disabled
    connector_id = payload.get("connector_id") if isinstance(payload, dict) else None
    try:
        from orchestrator.mcp_authz import canonical_public_base_url
        from orchestrator.remote_mcp_oauth import begin_authorization

        redirect_uri = canonical_public_base_url() + "/api/remote-mcp/oauth/callback"
        url = await begin_authorization(orch, user_id, connector_id, redirect_uri)
        link = f'<a rel="noopener noreferrer" target="_blank" href="{esc(url)}">Open authorization page</a>'
        return (SURFACE_KEY, {"oauth_url": url}, notice_block("info", link))
    except Exception as error:  # noqa: BLE001
        return (SURFACE_KEY, {}, _notice_for_error(error))


HANDLERS = {
    "chrome_remote_mcp_save": _save,
    "chrome_remote_mcp_test": _test,
    "chrome_remote_mcp_toggle_tool": _toggle,
    "chrome_remote_mcp_delete": _delete,
    "chrome_remote_mcp_edit": _edit,
    "chrome_remote_mcp_toggle_connector": _toggle_connector,
    "chrome_remote_mcp_oauth_start": _oauth_start,
}
