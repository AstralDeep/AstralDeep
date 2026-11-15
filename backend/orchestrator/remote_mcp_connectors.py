"""Stores owner-scoped remote MCP connector state and uses bounded fixed-origin egress
to discover and invoke server tools from the remote_mcp bundled agent.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import math
import re
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema import exceptions as jsonschema_exceptions
from orchestrator import remote_mcp_pins as pins
from shared import external_http
from shared.streaming_egress import EgressLimits

_AGENT_ID = "__remote_mcp_connections__"
_KEY_PREFIX = "remote_mcp_"
_MAX_TOOLS = 128
_MAX_CONNECTORS = 32
_MAX_RESULT_BYTES = 64 * 1024
_MAX_SCHEMA_BYTES = 32 * 1024
_MAX_SCHEMA_NODES = 512
_MAX_SCHEMA_DEPTH = 8
_MAX_SCHEMA_BRANCHES = 16
_MAX_ARGUMENT_NODES = 1024
_SUPPORTED_PROTOCOLS = frozenset({"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"})
_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_UNSUPPORTED_TOOL_REASON = "Some tool names or input schemas are unsupported or exceed safety limits."
_ID = re.compile(r"[0-9a-f]{32}\Z")
_TOOL = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum",
                          "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum",
                          "description", "title", "default", "examples", "$schema",
                          "anyOf", "oneOf", "allOf"})


class RemoteMCPError(ValueError):
    pass


def _validate_url(value: Any) -> str:
    if type(value) is not str or not 1 <= len(value) <= 2048 or value != value.strip():
        raise RemoteMCPError("Enter a valid HTTPS endpoint URL.")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise RemoteMCPError("The endpoint URL has an invalid port.") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or port not in (None, 443) or parsed.query
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise RemoteMCPError("Remote MCP endpoints must use HTTPS without credentials, query strings, or fragments.")
    return value.rstrip("/")


def _validate_oauth_endpoint(value: Any) -> str:
    if type(value) is not str or not 1 <= len(value) <= 2048 or value != value.strip():
        raise RemoteMCPError("An OAuth endpoint URL is invalid.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise RemoteMCPError("An OAuth endpoint URL is invalid.") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or port not in (None, 443)
            or any(ord(character) < 33 or ord(character) > 126 for character in value)):
        raise RemoteMCPError("An OAuth endpoint URL is invalid.")
    return value


def _validate_config(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) - {
        "id", "name", "url", "transport", "auth_type", "token", "oauth_client_id",
        "oauth_client_secret", "oauth_token_endpoint_auth_method", "access_token", "refresh_token", "token_endpoint",
        "authorization_endpoint", "token_expires_at", "scope", "tools", "enabled",
        "tools_catalog", "last_error", "unsupported_tool_count", "unsupported_tool_reason",
        "tool_pins", "tool_changes",
    }:
        raise RemoteMCPError("Connector configuration is invalid.")
    name = value.get("name")
    if type(name) is not str or not 1 <= len(name.strip()) <= 80:
        raise RemoteMCPError("Enter a connector name of 1 to 80 characters.")
    transport = value.get("transport")
    if transport not in {"streamable_http", "sse"}:
        raise RemoteMCPError("Choose Streamable HTTP or legacy SSE.")
    auth_type = value.get("auth_type")
    if auth_type not in {"none", "bearer", "oauth"}:
        raise RemoteMCPError("Choose no authentication, bearer token, or OAuth.")
    for key in ("token", "oauth_client_secret", "access_token", "refresh_token"):
        secret = value.get(key, "")
        if type(secret) is not str or len(secret) > 8192:
            raise RemoteMCPError("A connector credential is invalid or too large.")
    for key in ("oauth_client_id",):
        identifier = value.get(key, "")
        if type(identifier) is not str or len(identifier) > 512:
            raise RemoteMCPError("The OAuth client ID is invalid or too large.")
    method = value.get("oauth_token_endpoint_auth_method", "")
    if (type(method) is not str
            or method not in {"", "none", "client_secret_basic", "client_secret_post"}):
        raise RemoteMCPError("The OAuth token authentication method is invalid.")
    for key in ("token_endpoint", "authorization_endpoint"):
        endpoint = value.get(key)
        if endpoint:
            _validate_oauth_endpoint(endpoint)
    expiry = value.get("token_expires_at")
    if expiry is not None and (type(expiry) not in (int, float) or expiry < 0 or expiry > 10**13):
        raise RemoteMCPError("OAuth token expiry is invalid.")
    scope = value.get("scope", "")
    if type(scope) is not str or len(scope) > 512:
        raise RemoteMCPError("OAuth scope is invalid.")
    result = dict(value)
    if "id" in value and (not isinstance(value["id"], str) or not _ID.fullmatch(value["id"])):
        raise RemoteMCPError("Connector ID is invalid.")
    result["id"] = value.get("id") or uuid.uuid4().hex
    result["name"] = name.strip()
    result["url"] = _validate_url(value.get("url"))
    result["enabled"] = value.get("enabled", True) is True
    if auth_type == "bearer" and not (value.get("token") or value.get("access_token")):
        raise RemoteMCPError("Enter a bearer token.")
    tools = value.get("tools", {})
    if type(tools) is not dict or len(tools) > _MAX_TOOLS:
        raise RemoteMCPError("The saved tool selection is invalid.")
    result["tools"] = {name: enabled is True for name, enabled in tools.items()
                        if type(name) is str and _TOOL.fullmatch(name)}
    catalog = value.get("tools_catalog", [])
    if type(catalog) is not list or len(catalog) > _MAX_TOOLS or len(json.dumps(catalog)) > _MAX_SCHEMA_BYTES * 2:
        raise RemoteMCPError("The saved tool catalogue is invalid.")
    result["tools_catalog"] = catalog
    tool_pins = value.get("tool_pins", {})
    if (type(tool_pins) is not dict or len(tool_pins) > _MAX_TOOLS * 2
            or any(type(k) is not str or not _TOOL.fullmatch(k) or not pins.is_pin(v) for k, v in tool_pins.items())):
        raise RemoteMCPError("The saved tool approvals are invalid.")
    result["tool_pins"] = dict(tool_pins)
    tool_changes = value.get("tool_changes", {})
    if (type(tool_changes) is not dict or len(tool_changes) > _MAX_TOOLS * 2
            or any(type(k) is not str or not _TOOL.fullmatch(k) or type(v) is not dict
                   or v.get("status") not in (pins.CHANGED, pins.REMOVED) for k, v in tool_changes.items())):
        raise RemoteMCPError("The saved tool change records are invalid.")
    result["tool_changes"] = {k: {"status": v["status"], "pinned": str(v.get("pinned", "")),
                                  "current": str(v.get("current", ""))} for k, v in tool_changes.items()}
    result["last_error"] = str(value.get("last_error", ""))[:300]
    skipped = value.get("unsupported_tool_count", 0)
    if type(skipped) is not int or not 0 <= skipped <= _MAX_TOOLS:
        raise RemoteMCPError("The saved unsupported-tool count is invalid.")
    result["unsupported_tool_count"] = skipped
    result["unsupported_tool_reason"] = _UNSUPPORTED_TOOL_REASON if skipped else ""
    return result


def _repository(orch: Any):
    manager = getattr(orch, "credential_manager", None)
    if manager is None:
        raise RemoteMCPError("Connector storage is unavailable.")
    return manager


def list_connectors(orch: Any, owner_id: str) -> list[dict[str, Any]]:
    if not isinstance(owner_id, str) or not owner_id:
        return []
    manager = _repository(orch)
    items = []
    for key in manager.list_credential_keys(owner_id, _AGENT_ID):
        if not key.startswith(_KEY_PREFIX):
            continue
        raw = manager.get_credential(owner_id, _AGENT_ID, key)
        if not raw:
            continue
        try:
            connector = _validate_config(json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        connector["id"] = key.removeprefix(_KEY_PREFIX)
        connector["has_credential"] = bool(connector.get("token") or connector.get("access_token")
                                           or connector.get("refresh_token")
                                           or connector.get("oauth_client_secret"))
        connector["token"] = ""
        connector.pop("access_token", None)
        connector.pop("refresh_token", None)
        connector.pop("oauth_client_secret", None)
        items.append(connector)
        if len(items) >= _MAX_CONNECTORS:
            break
    return sorted(items, key=lambda item: item["name"].casefold())


def list_enabled_tools(orch: Any, owner_id: str) -> list[dict[str, Any]]:
    tools = []
    for connector in list_connectors(orch, owner_id):
        if not connector.get("enabled"):
            continue
        for tool in connector.get("tools_catalog", []):
            if (connector.get("tools", {}).get(tool.get("name")) is True
                    and pins.verdict(connector.get("tool_pins", {}), tool) == "ok"):
                tools.append({"connector_id": connector["id"], "connector": connector["name"],
                              "name": tool["name"], "description": tool["description"],
                              "inputSchema": tool["inputSchema"]})
    return tools


def get_connector(orch: Any, owner_id: str, connector_id: str) -> dict[str, Any]:
    if not isinstance(connector_id, str) or not _ID.fullmatch(connector_id):
        raise RemoteMCPError("Connector was not found.")
    raw = _repository(orch).get_credential(owner_id, _AGENT_ID, _KEY_PREFIX + connector_id)
    if not raw:
        raise RemoteMCPError("Connector was not found.")
    try:
        connector = _validate_config(json.loads(raw))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise RemoteMCPError("Connector configuration could not be read.") from None
    connector["id"] = connector_id
    return connector


def review_tool_identifiers(orch: Any, owner_id: str, connector_id: str,
                             tool_name: str) -> dict[str, str]:
    if not isinstance(tool_name, str) or not _TOOL.fullmatch(tool_name):
        raise RemoteMCPError("Choose a valid server tool.")
    connector = get_connector(orch, owner_id, connector_id)
    _check_tool_authority(connector, connector, tool_name)
    if not any(isinstance(tool, dict) and tool.get("name") == tool_name
               for tool in connector.get("tools_catalog", [])):
        raise RemoteMCPError("Refresh the server tool list before reviewing this tool.")
    return {"connector_id": connector_id, "tool_name": tool_name}


def save_connector(orch: Any, owner_id: str, values: dict[str, Any], *,
                   clear_oauth_client_credentials: bool = False,
                   require_existing: bool = False) -> dict[str, Any]:
    if type(values) is not dict:
        raise RemoteMCPError("Connector configuration is invalid.")
    if type(clear_oauth_client_credentials) is not bool:
        raise RemoteMCPError("OAuth client credential clearing must be a boolean.")
    if type(require_existing) is not bool:
        raise RemoteMCPError("Existing connector selection must be a boolean.")
    connector_id = values.get("id")
    if require_existing and not connector_id:
        raise RemoteMCPError("Connector was not found.")
    if connector_id is not None and (not isinstance(connector_id, str) or not _ID.fullmatch(connector_id)):
        raise RemoteMCPError("Connector ID is invalid.")
    connector_id = connector_id or uuid.uuid4().hex
    key = _KEY_PREFIX + connector_id
    existing = _repository(orch).get_credential(owner_id, _AGENT_ID, key)
    if require_existing and not existing:
        raise RemoteMCPError("Connector was not found.")
    candidate = dict(values)
    candidate["id"] = connector_id
    candidate.pop("unsupported_tool_count", None)
    candidate.pop("unsupported_tool_reason", None)
    if clear_oauth_client_credentials:
        candidate.pop("oauth_client_id", None)
        candidate.pop("oauth_client_secret", None)
        for field in ("oauth_token_endpoint_auth_method", "access_token", "refresh_token", "token_endpoint", "authorization_endpoint",
                      "token_expires_at", "scope"):
            candidate.pop(field, None)
    if existing:
        old = json.loads(existing)
        if require_existing:
            candidate["enabled"] = old.get("enabled", True)
            candidate["tools"] = old.get("tools", {})
            candidate["tools_catalog"] = old.get("tools_catalog", [])
            candidate["tool_pins"] = old.get("tool_pins", {})
            candidate["tool_changes"] = old.get("tool_changes", {})
        same_authority = old.get("url") == candidate.get("url") and old.get("auth_type") == candidate.get("auth_type")
        if same_authority:
            candidate["unsupported_tool_count"] = old.get("unsupported_tool_count", 0)
        else:
            candidate["unsupported_tool_count"] = 0
        changed_oauth_client = (
            same_authority and candidate.get("auth_type") == "oauth"
            and (clear_oauth_client_credentials or any(
                candidate.get(key) and candidate.get(key) != old.get(key)
                for key in ("oauth_client_id", "oauth_client_secret")))
        )
        changed_bearer_token = (
            same_authority and candidate.get("auth_type") == "bearer"
            and candidate.get("token") and candidate.get("token") != old.get("token")
        )
        credential_fields = (
            "token", "oauth_client_id", "oauth_client_secret", "oauth_token_endpoint_auth_method", "access_token", "refresh_token",
            "token_endpoint", "authorization_endpoint", "token_expires_at", "scope",
        )
        if same_authority and not changed_oauth_client:
            for secret in credential_fields:
                if not candidate.get(secret):
                    old_value = old.get(secret)
                    if secret == "token_expires_at" and old_value is None:
                        candidate.pop(secret, None)
                    else:
                        candidate[secret] = old_value or ""
        else:
            invalidated = credential_fields[3:] if changed_oauth_client else credential_fields
            for secret in invalidated:
                if candidate.get(secret) == old.get(secret):
                    candidate.pop(secret, None)
        if not same_authority or changed_oauth_client or changed_bearer_token:
            candidate["tools"] = {}
            candidate["tools_catalog"] = []
            candidate["tool_pins"] = {}
            candidate["tool_changes"] = {}
            candidate["unsupported_tool_count"] = 0
            candidate["unsupported_tool_reason"] = ""
    elif len(list_connectors(orch, owner_id)) >= _MAX_CONNECTORS:
        raise RemoteMCPError("This account has reached the connector limit.")
    connector = _validate_config(candidate)
    if connector.get("token_expires_at") is None:
        connector.pop("token_expires_at", None)
    if connector["auth_type"] == "none":
        for secret in ("token", "oauth_client_id", "oauth_client_secret", "oauth_token_endpoint_auth_method", "access_token", "refresh_token",
                       "token_endpoint", "authorization_endpoint", "token_expires_at", "scope"):
            connector.pop(secret, None)
    if existing:
        if not _repository(orch).compare_and_set_credential(
            owner_id, _AGENT_ID, key, existing, json.dumps(connector),
        ):
            raise RemoteMCPError("The connector changed. Reload its settings and try again.")
    else:
        _repository(orch).set_credential(owner_id, _AGENT_ID, key, json.dumps(connector), e2e=False)
    return {key: value for key, value in connector.items()
            if key not in {"token", "oauth_client_secret", "access_token", "refresh_token"}}


def delete_connector(orch: Any, owner_id: str, connector_id: str) -> None:
    if not isinstance(connector_id, str) or not _ID.fullmatch(connector_id):
        raise RemoteMCPError("Connector was not found.")
    _repository(orch).delete_credential(owner_id, _AGENT_ID, _KEY_PREFIX + connector_id)


def _same_oauth_authority(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all((current.get(field) or "") == (expected.get(field) or "") for field in (
        "url", "transport", "auth_type", "oauth_client_id", "oauth_client_secret",
        "oauth_token_endpoint_auth_method", "token_endpoint",
    ))


def update_oauth_credentials(orch: Any, owner_id: str, expected: dict[str, Any],
                             updates: dict[str, Any]) -> dict[str, Any]:
    fields = {"oauth_client_id", "oauth_client_secret", "oauth_token_endpoint_auth_method", "access_token", "refresh_token",
              "token_endpoint", "authorization_endpoint", "token_expires_at", "scope"}
    if set(updates) - fields:
        raise RemoteMCPError("The OAuth credential update is invalid.")
    manager = _repository(orch)
    key = _KEY_PREFIX + expected["id"]
    for _ in range(5):
        raw = manager.get_credential(owner_id, _AGENT_ID, key)
        if not raw:
            raise RemoteMCPError("Connector was not found.")
        current = _validate_config(json.loads(raw))
        if (not _same_oauth_authority(current, expected)
                or any((current.get(field) or "") != (expected.get(field) or "")
                       for field in fields)):
            raise RemoteMCPError("The connector changed during OAuth authorization. Start again.")
        current.update(updates)
        current = _validate_config(current)
        if manager.compare_and_set_credential(owner_id, _AGENT_ID, key, raw, json.dumps(current)):
            return current
    raise RemoteMCPError("The connector changed. Reload its settings and try again.")


def _update_tool_catalog(orch: Any, owner_id: str, expected: dict[str, Any],
                         tools: list[dict[str, Any]], unsupported_tool_count: int = 0) -> list[dict[str, str]]:
    manager = _repository(orch)
    key = _KEY_PREFIX + expected["id"]
    for _ in range(5):
        raw = manager.get_credential(owner_id, _AGENT_ID, key)
        if not raw:
            raise RemoteMCPError("Connector was not found.")
        current = _validate_config(json.loads(raw))
        if not _same_oauth_authority(current, expected):
            raise RemoteMCPError("The connector changed while loading its tools. Try again.")
        current["tools_catalog"] = tools
        current["tools"], current["tool_changes"], events = pins.reconcile(
            current.get("tool_pins", {}), current.get("tools", {}), current.get("tool_changes", {}), tools)
        current["unsupported_tool_count"] = max(0, min(_MAX_TOOLS, unsupported_tool_count))
        current["unsupported_tool_reason"] = _UNSUPPORTED_TOOL_REASON if current["unsupported_tool_count"] else ""
        current["last_error"] = ""
        current = _validate_config(current)
        if manager.compare_and_set_credential(owner_id, _AGENT_ID, key, raw, json.dumps(current)):
            return events
    raise RemoteMCPError("The connector changed. Reload its settings and try again.")


def _check_tool_authority(current: dict[str, Any], expected: dict[str, Any], tool_name: str) -> None:
    if not current.get("enabled"):
        raise RemoteMCPError("This connector is disabled.")
    if current.get("tools", {}).get(tool_name) is not True:
        raise RemoteMCPError("This server tool is not enabled.")
    expected_pin = expected.get("tool_pins", {}).get(tool_name)
    if not pins.is_pin(expected_pin) or current.get("tool_pins", {}).get(tool_name) != expected_pin:
        raise RemoteMCPError("The tool approval changed before the tool call. Try again.")
    if (not _same_oauth_authority(current, expected)
            or any(current.get(field) != expected.get(field) for field in ("access_token", "token"))):
        raise RemoteMCPError("The connector changed before the tool call. Try again.")


def _transport(connector: dict[str, Any], *, transport_factory=None):
    limits = EgressLimits(
        total_timeout=30, read_timeout=20, max_request_bytes=1024 * 1024,
        max_response_bytes=1024 * 1024,
    )
    if transport_factory is not None:
        parsed = urlsplit(connector["url"])
        transport = transport_factory(f"{parsed.scheme}://{parsed.netloc}", limits=limits)
        path = parsed.path.rstrip("/") or "/"
        path += f"?{parsed.query}" if parsed.query else ""
        return transport, path
    return external_http.fixed_origin_transport_for_external_url(connector["url"], limits=limits)


async def _mcp_request(connector: dict[str, Any], payload: dict[str, Any], session_id: str = "",
                       *, transport_factory=None) -> tuple[dict[str, Any], str]:
    if connector.get("transport") == "sse":
        if not isinstance(session_id, _SSESession):
            raise RemoteMCPError("The legacy SSE session is unavailable.")
        return await session_id.request(payload), session_id
    transport, path = _transport(connector, transport_factory=transport_factory)
    bearer = connector.get("access_token") or connector.get("token") or ""
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream",
               "mcp-protocol-version": connector.get("_protocol_version", "2025-11-25")}
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    if session_id:
        headers["mcp-session-id"] = session_id
    request = type("Request", (), {
        "path": path, "headers": headers, "body": json.dumps(payload).encode(),
        "max_response_bytes": 1024 * 1024, "timeout_seconds": 30.0,
    })()
    source = transport.post_response_stream(request).__aiter__()
    try:
        first = await anext(source)
        next_session = first.headers.get("mcp-session-id", session_id)
        content_type = first.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            data = await _first_sse_response(source, payload.get("id"))
        elif "json" in content_type or not content_type:
            body = bytearray()
            async for chunk in source:
                body.extend(chunk.body)
                if len(body) > 1024 * 1024:
                    raise RemoteMCPError("The MCP server response was too large.")
            data = json.loads(body) if body else {}
        else:
            raise RemoteMCPError("The MCP server returned an unsupported response type.")
    finally:
        await source.aclose()
    if not isinstance(data, dict):
        raise RemoteMCPError("The MCP server response was invalid.")
    return data, next_session


async def _mcp_notify(connector: dict[str, Any], payload: dict[str, Any], session_id: str = "",
                      *, transport_factory=None) -> None:
    if connector.get("transport") == "sse":
        if not isinstance(session_id, _SSESession):
            raise RemoteMCPError("The legacy SSE session is unavailable.")
        await session_id.notify(payload)
        return
    transport, path = _transport(connector, transport_factory=transport_factory)
    bearer = connector.get("access_token") or connector.get("token") or ""
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream",
               "mcp-protocol-version": connector.get("_protocol_version", "2025-11-25")}
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    if session_id:
        headers["mcp-session-id"] = session_id
    request = type("Request", (), {
        "path": path, "headers": headers, "body": json.dumps(payload).encode(),
        "max_response_bytes": 1024 * 1024, "timeout_seconds": 30.0,
    })()
    await transport.post(request)


class _SSESession:
    def __init__(self, connector: dict[str, Any], transport: Any, base_path: str):
        self.connector = connector
        self.transport = transport
        self.base_path = base_path
        self.headers = {"accept": "text/event-stream"}
        bearer = connector.get("access_token") or connector.get("token") or ""
        if bearer:
            self.headers["authorization"] = f"Bearer {bearer}"
        self.events = None
        self.endpoint = ""
        self.buffer = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")

    async def open(self):
        request = type("Request", (), {
            "path": self.base_path, "headers": self.headers, "body": b"",
            "max_response_bytes": 1024 * 1024, "timeout_seconds": 30.0,
        })()
        self.events = self.transport.get_stream(request).__aiter__()
        first = await anext(self.events)
        if "text/event-stream" not in first.headers.get("content-type", ""):
            raise RemoteMCPError("The legacy MCP SSE endpoint returned an invalid event stream.")
        while not self.endpoint:
            event = await self._next_event()
            if event.get("event") == "endpoint":
                self.endpoint = self._same_origin_endpoint(event.get("data", ""))
        return self

    def _same_origin_endpoint(self, value: str) -> str:
        if not isinstance(value, str) or len(value) > 4096:
            raise RemoteMCPError("The SSE server returned an invalid message endpoint.")
        base = urlsplit(self.connector["url"])
        candidate = urlsplit(value)
        if candidate.scheme or candidate.netloc:
            if (candidate.scheme != base.scheme or candidate.netloc != base.netloc
                    or candidate.fragment or candidate.username or candidate.password):
                raise RemoteMCPError("The SSE message endpoint must remain on the configured origin.")
            path, query = candidate.path, candidate.query
        else:
            if value.startswith("//") or "#" in value:
                raise RemoteMCPError("The SSE message endpoint is invalid.")
            path, _, query = value.partition("?")
        if (not path.startswith("/") or len(query) > 2048
                or any(part in {".", ".."} for part in path.split("/"))):
            raise RemoteMCPError("The SSE message endpoint is invalid.")
        return path + (f"?{query}" if query else "")

    async def _next_event(self) -> dict[str, str]:
        while "\n\n" not in self.buffer:
            try:
                chunk = await anext(self.events)
            except StopAsyncIteration:
                self.decoder.decode(b"", final=True)
                raise
            self.buffer = (self.buffer + self.decoder.decode(chunk.body)).replace("\r\n", "\n")
            if len(self.buffer) > 65536:
                raise RemoteMCPError("The SSE event exceeded the allowed size.")
        raw, self.buffer = self.buffer.split("\n\n", 1)
        event: dict[str, str] = {}
        data = []
        for line in raw.splitlines():
            if line.startswith("event:"):
                event["event"] = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        event["data"] = "\n".join(data)
        return event

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = type("Request", (), {
            "path": self.endpoint, "headers": {**self.headers, "content-type": "application/json"},
            "body": json.dumps(payload).encode(), "max_response_bytes": 1024 * 1024,
            "timeout_seconds": 30.0,
        })()
        await self.transport.post(request)
        expected = payload.get("id")
        for _ in range(32):
            event = await self._next_event()
            if not event.get("data"):
                continue
            try:
                message = json.loads(event["data"])
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == expected:
                return message
        raise RemoteMCPError("The legacy MCP server did not answer the request.")

    async def notify(self, payload: dict[str, Any]) -> None:
        request = type("Request", (), {
            "path": self.endpoint, "headers": {**self.headers, "content-type": "application/json"},
            "body": json.dumps(payload).encode(), "max_response_bytes": 1024 * 1024,
            "timeout_seconds": 30.0,
        })()
        await self.transport.post(request)

    async def close(self):
        if self.events is not None:
            await self.events.aclose()


def _sse_message(body: bytes) -> dict[str, Any]:
    if len(body) > 1024 * 1024:
        raise RemoteMCPError("The MCP server response was too large.")
    for event in body.decode("utf-8", errors="strict").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
        if data:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                return parsed
    raise RemoteMCPError("The MCP server sent no JSON-RPC response.")


def _sanitize_schema(schema: Any, depth: int = 0, budget: list[int] | None = None) -> dict[str, Any] | None:
    budget = budget if budget is not None else [0]
    budget[0] += 1
    try:
        encoded_size = len(json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    if (depth > _MAX_SCHEMA_DEPTH or budget[0] > _MAX_SCHEMA_NODES or type(schema) is not dict
            or set(schema) - _SCHEMA_KEYS or encoded_size > _MAX_SCHEMA_BYTES):
        return None
    if "$schema" in schema and schema["$schema"] != _SCHEMA_DIALECT:
        return None
    type_names = {"object", "array", "string", "integer", "number", "boolean", "null"}
    kind = schema.get("type")
    if type(kind) is list:
        if (not 1 <= len(kind) <= len(type_names) or any(type(value) is not str for value in kind)
                or len(set(kind)) != len(kind) or not set(kind) <= type_names):
            return None
    elif kind is not None and (type(kind) is not str or kind not in type_names):
        return None
    result: dict[str, Any] = {}
    if kind is not None:
        result["type"] = kind[:]
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            value = schema[key]
            bound = 256 if key in {"minItems", "maxItems"} else 4096
            if type(value) is not int or value < 0 or value > bound:
                return None
            result[key] = value
    for key in ("minimum", "maximum"):
        if key in schema:
            value = schema[key]
            if (type(value) not in (int, float)
                    or type(value) is float and not math.isfinite(value)):
                return None
            result[key] = value
    if "description" in schema:
        description = re.sub(r"<[^>]*>", " ", str(schema["description"]))
        description = " ".join("".join(c for c in description if ord(c) >= 32).split())
        if description.startswith("Untrusted remote metadata: "):
            description = description.removeprefix("Untrusted remote metadata: ")
        result["description"] = "Untrusted remote metadata: " + description[:400]
    if "required" in schema:
        required = schema["required"]
        if (type(required) is not list or len(required) > 64
                or any(type(item) is not str or len(item) > 128 for item in required)):
            return None
        result["required"] = required[:]
    if "enum" in schema:
        choices = schema["enum"]
        try:
            enum_size = len(json.dumps(choices, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, OverflowError):
            return None
        if (type(choices) is not list or len(choices) > 128 or enum_size > 4096
                or any(not _bounded_json_value(item) for item in choices)):
            return None
        result["enum"] = choices[:]
    if "properties" in schema:
        properties = schema["properties"]
        if type(properties) is not dict or len(properties) > 64:
            return None
        safe_properties = {}
        for name, child in properties.items():
            if type(name) is not str or len(name) > 128:
                return None
            safe_child = _sanitize_schema(child, depth + 1, budget)
            if safe_child is None:
                return None
            safe_properties[name] = safe_child
        result["properties"] = safe_properties
    if "items" in schema:
        item_schema = _sanitize_schema(schema["items"], depth + 1, budget)
        if item_schema is None:
            return None
        result["items"] = item_schema
    if "additionalProperties" in schema:
        value = schema["additionalProperties"]
        if type(value) is bool:
            result["additionalProperties"] = value
        else:
            additional = _sanitize_schema(value, depth + 1, budget)
            if additional is None:
                return None
            result["additionalProperties"] = additional
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            branches = schema[key]
            if type(branches) is not list or not 1 <= len(branches) <= _MAX_SCHEMA_BRANCHES:
                return None
            sanitized = [_sanitize_schema(branch, depth + 1, budget) for branch in branches]
            if any(branch is None for branch in sanitized):
                return None
            result[key] = sanitized
    try:
        Draft202012Validator.check_schema(result)
    except (jsonschema_exceptions.SchemaError, RecursionError):
        return None
    return result


def _schema_allows_object(schema: dict[str, Any]) -> bool:
    if "enum" in schema and not any(type(choice) is dict for choice in schema["enum"]):
        return False
    kind = schema.get("type")
    if kind is not None and "object" not in (kind if type(kind) is list else [kind]):
        return False
    if any(not _schema_allows_object(branch) for branch in schema.get("allOf", [])):
        return False
    for key in ("anyOf", "oneOf"):
        if key in schema and not any(_schema_allows_object(branch) for branch in schema[key]):
            return False
    return True


async def _first_sse_response(source, request_id: Any) -> dict[str, Any]:
    buffer = ""
    size = 0
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    while size <= 1024 * 1024:
        try:
            chunk = await anext(source)
        except StopAsyncIteration:
            decoder.decode(b"", final=True)
            raise
        size += len(chunk.body)
        buffer = (buffer + decoder.decode(chunk.body)).replace("\r\n", "\n")
        while "\n\n" in buffer:
            raw, buffer = buffer.split("\n\n", 1)
            data = "\n".join(line[5:].lstrip() for line in raw.splitlines() if line.startswith("data:"))
            if not data:
                continue
            message = json.loads(data)
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
    raise RemoteMCPError("The MCP event response exceeded the allowed size.")


def _bounded_json_value(value: Any, depth: int = 0, budget: list[int] | None = None) -> bool:
    budget = budget if budget is not None else [0]
    budget[0] += 1
    if depth > _MAX_SCHEMA_DEPTH or budget[0] > _MAX_ARGUMENT_NODES:
        return False
    if type(value) in (type(None), bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is str:
        return len(value) <= 4096
    if type(value) is list:
        return len(value) <= 256 and all(_bounded_json_value(item, depth + 1, budget) for item in value)
    if type(value) is dict:
        return len(value) <= 256 and all(
            type(key) is str and len(key) <= 128 and _bounded_json_value(item, depth + 1, budget)
            for key, item in value.items()
        )
    return False


def _validate_schema(schema: Any, arguments: Any, depth: int = 0) -> bool:
    if depth > _MAX_SCHEMA_DEPTH or type(schema) is not dict or not _bounded_json_value(arguments):
        return False
    try:
        encoded_size = len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        safe_schema = _sanitize_schema(schema)
        return (encoded_size <= _MAX_SCHEMA_BYTES and safe_schema is not None
                and Draft202012Validator(safe_schema).is_valid(arguments))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False


async def _initialize(connector: dict[str, Any], *, transport_factory=None):
    session = ""
    try:
        if connector.get("transport") == "sse":
            transport, path = _transport(connector, transport_factory=transport_factory)
            session = _SSESession(connector, transport, path)
            await session.open()
        response, session = await _mcp_request(connector, {
            "jsonrpc": "2.0", "id": "init", "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "AstralDeep", "version": "1.0"}},
        }, session, transport_factory=transport_factory)
        if response.get("error"):
            raise RemoteMCPError("The MCP server rejected initialization.")
        result = response.get("result")
        negotiated = result.get("protocolVersion") if isinstance(result, dict) else None
        if negotiated:
            if negotiated not in _SUPPORTED_PROTOCOLS:
                raise RemoteMCPError("The MCP server negotiated an unsupported protocol version.")
            connector["_protocol_version"] = negotiated
            if isinstance(session, _SSESession):
                session.headers["mcp-protocol-version"] = negotiated
        await _mcp_notify(connector, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session,
                          transport_factory=transport_factory)
        response, _ = await _mcp_request(connector, {
            "jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {},
        }, session, transport_factory=transport_factory)
        tools = response.get("result", {}).get("tools") if isinstance(response.get("result"), dict) else None
        if not isinstance(tools, list) or len(tools) > _MAX_TOOLS:
            raise RemoteMCPError("The MCP server returned an invalid tool list.")
        safe = []
        unsupported = 0
        for item in tools:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not _TOOL.fullmatch(item["name"]):
                unsupported += 1
                continue
            schema = _sanitize_schema(item.get("inputSchema", {"type": "object", "properties": {}}))
            if not isinstance(schema, dict) or not _schema_allows_object(schema):
                unsupported += 1
                continue
            description = re.sub(r"<[^>]*>", " ", str(item.get("description", "")))
            description = " ".join("".join(c for c in description if ord(c) >= 32).split())[:500]
            entry = {"name": item["name"], "description": "Untrusted remote metadata: " + description,
                     "inputSchema": schema}
            if annotations := pins.sanitize_annotations(item.get("annotations")):
                entry["annotations"] = annotations
            safe.append(entry)
        connector["_unsupported_tool_count"] = unsupported
        return safe, session
    except Exception:
        if isinstance(session, _SSESession):
            await session.close()
        raise


async def discover_tools(connector: dict[str, Any], *, transport_factory=None) -> list[dict[str, Any]]:
    tools, session = await _initialize(connector, transport_factory=transport_factory)
    if isinstance(session, _SSESession):
        await session.close()
    return tools


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result(timeout=35)


async def http_request(url: str, method: str, body: bytes | None = None,
                       headers: dict[str, str] | None = None,
                       max_response_bytes: int = 1024 * 1024):
    if type(url) is not str or len(url) > 4096 or method not in {"GET", "POST"}:
        raise RemoteMCPError("The OAuth endpoint request is invalid.")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in (None, 443)):
        raise RemoteMCPError("OAuth metadata and token endpoints must use HTTPS.")
    if type(max_response_bytes) is not int or not 0 < max_response_bytes <= 1024 * 1024:
        raise RemoteMCPError("The OAuth response limit is invalid.")
    transport, path = external_http.fixed_origin_transport_for_external_url(
        url, limits=EgressLimits(total_timeout=20, read_timeout=15,
                                 max_request_bytes=256 * 1024,
                                 max_response_bytes=max_response_bytes),
    )
    request = type("Request", (), {
        "path": path, "headers": headers or {"accept": "application/json"},
        "body": body or b"", "max_response_bytes": max_response_bytes,
        "timeout_seconds": 20.0,
    })()
    response = await transport.request_status(method, request)
    return response.status, dict(response.headers), response.body


def refresh_tools(orch: Any, owner_id: str, connector_id: str, *, transport_factory=None) -> list[dict[str, Any]]:
    connector = get_connector(orch, owner_id, connector_id)
    if not connector.get("enabled"):
        raise RemoteMCPError("Enable the connector before loading its tools.")
    connector = _ensure_oauth(orch, owner_id, connector)
    tools = _run(discover_tools(connector, transport_factory=transport_factory))
    events = _update_tool_catalog(orch, owner_id, connector, tools,
                                  connector.get("_unsupported_tool_count", 0))
    _run(_audit_tool_changes(owner_id, connector_id, events, "refresh"))
    return tools


def _ensure_oauth(orch: Any, owner_id: str, connector: dict[str, Any]) -> dict[str, Any]:
    expires = connector.get("token_expires_at")
    if connector.get("auth_type") == "oauth" and connector.get("refresh_token") and (
        not isinstance(expires, (int, float)) or expires <= time.time() + 30
    ):
        from orchestrator.remote_mcp_oauth import refresh_authorization

        if not _run(refresh_authorization(orch, owner_id, connector["id"])):
            raise RemoteMCPError("OAuth sign-in expired. Authorize this connector again.")
        connector = get_connector(orch, owner_id, connector["id"])
    return connector


def _update_connector_control(orch: Any, owner_id: str, connector_id: str,
                               enabled: bool, tool_name: str | None = None) -> dict[str, Any]:
    if not isinstance(connector_id, str) or not _ID.fullmatch(connector_id):
        raise RemoteMCPError("Connector was not found.")
    manager = _repository(orch)
    key = _KEY_PREFIX + connector_id
    for _ in range(5):
        approved = ""
        raw = manager.get_credential(owner_id, _AGENT_ID, key)
        if not raw:
            raise RemoteMCPError("Connector was not found.")
        connector = _validate_config(json.loads(raw))
        if tool_name is None:
            connector["enabled"] = enabled is True
        else:
            if not any(item.get("name") == tool_name for item in connector.get("tools_catalog", [])):
                raise RemoteMCPError("Refresh the server tool list before changing this tool.")
            entry = next(item for item in connector["tools_catalog"] if item.get("name") == tool_name)
            connector.setdefault("tools", {})[tool_name] = enabled is True
            if enabled is True:
                approved = pins.definition_hash(entry)
                connector.setdefault("tool_pins", {})[tool_name] = approved
                connector.setdefault("tool_changes", {}).pop(tool_name, None)
        if manager.compare_and_set_credential(owner_id, _AGENT_ID, key, raw, json.dumps(connector)):
            if approved:
                _run(_audit_tool_changes(owner_id, connector_id, [
                    {"tool": tool_name, "status": "approved", "pinned": approved, "current": approved}], "approve"))
            return {key: value for key, value in connector.items()
                    if key not in {"token", "oauth_client_secret", "access_token", "refresh_token"}}
    raise RemoteMCPError("The connector changed. Reload its settings and try again.")


def set_tool_selection(orch: Any, owner_id: str, connector_id: str, tool_name: str,
                       enabled: bool) -> dict[str, Any]:
    if not isinstance(connector_id, str) or not _ID.fullmatch(connector_id):
        raise RemoteMCPError("Connector was not found.")
    if not isinstance(tool_name, str) or not _TOOL.fullmatch(tool_name):
        raise RemoteMCPError("Choose a valid server tool.")
    return _update_connector_control(orch, owner_id, connector_id, enabled, tool_name)


def set_connector_enabled(orch: Any, owner_id: str, connector_id: str, enabled: bool) -> dict[str, Any]:
    return _update_connector_control(orch, owner_id, connector_id, enabled)


async def _audit_remote_tool(owner_id: str, connector_id: str, tool_name: str,
                             arguments: dict[str, Any], outcome: str, result_bytes: int = 0) -> None:
    from audit.hooks import record_generic

    await record_generic(
        claims={"sub": owner_id}, event_class="agent_tool_call",
        action_type="remote_mcp.tool.dispatch",
        description="Owner-enabled remote MCP tool invocation",
        inputs_meta={"connector_id": connector_id, "tool_name": tool_name,
                     "argument_count": len(arguments) if type(arguments) is dict else 0},
        outputs_meta={"result_bytes": result_bytes}, outcome=outcome,
    )


def _disable_tool(orch: Any, owner_id: str, connector_id: str, tool_name: str) -> None:
    manager = _repository(orch)
    key = _KEY_PREFIX + connector_id
    for _ in range(5):
        raw = manager.get_credential(owner_id, _AGENT_ID, key)
        if not raw:
            return
        current = _validate_config(json.loads(raw))
        current["tools"][tool_name] = False
        current["tool_changes"][tool_name] = {"status": pins.REMOVED, "pinned": current["tool_pins"].get(tool_name, ""),
                                              "current": ""}
        if manager.compare_and_set_credential(owner_id, _AGENT_ID, key, raw, json.dumps(current)):
            return


async def _audit_tool_changes(owner_id: str, connector_id: str, events: list[dict[str, str]],
                              source: str) -> None:
    from audit.hooks import record_generic

    for event in events:
        await record_generic(
            claims={"sub": owner_id}, event_class="agent_tool_call",
            action_type=f"remote_mcp.tool.definition_{event['status']}",
            description="Remote MCP tool definition " + ("approved and pinned" if event["status"] == "approved"
                                                         else f"{event['status']} since approval"),
            inputs_meta={"connector_id": connector_id, "tool_name": event["tool"], "source": source},
            outputs_meta={"pinned": event["pinned"], "current": event["current"]},
            outcome="success" if event["status"] == "approved" else "failure",
        )


def _flag_changed_tool(orch: Any, owner_id: str, connector_id: str, tool: dict[str, Any]) -> dict[str, str] | None:
    manager = _repository(orch)
    key = _KEY_PREFIX + connector_id
    for _ in range(5):
        raw = manager.get_credential(owner_id, _AGENT_ID, key)
        if not raw:
            return None
        current = _validate_config(json.loads(raw))
        pin = current.get("tool_pins", {}).get(tool["name"])
        if not pins.is_pin(pin):
            return None
        record = {"status": pins.CHANGED, "pinned": pin, "current": pins.definition_hash(tool)}
        current["tools"][tool["name"]] = False
        current["tool_changes"][tool["name"]] = record
        current["tools_catalog"] = [tool if item.get("name") == tool["name"] else item
                                    for item in current["tools_catalog"]]
        if manager.compare_and_set_credential(owner_id, _AGENT_ID, key, raw, json.dumps(current)):
            return {"tool": tool["name"], **record}
    return None


def call_tool(orch: Any, owner_id: str, connector_id: str, tool_name: str,
              arguments: dict[str, Any], *, transport_factory=None) -> dict[str, Any]:
    try:
        if not isinstance(connector_id, str) or not _ID.fullmatch(connector_id):
            raise RemoteMCPError("Connector was not found.")
        if not isinstance(tool_name, str) or not _TOOL.fullmatch(tool_name):
            raise RemoteMCPError("Choose a valid server tool.")
        connector = get_connector(orch, owner_id, connector_id)
        if not connector.get("enabled"):
            raise RemoteMCPError("This connector is disabled.")
        if connector.get("tools", {}).get(tool_name) is not True:
            raise RemoteMCPError("This server tool is not enabled.")
        if not pins.is_pin(connector.get("tool_pins", {}).get(tool_name)):
            raise RemoteMCPError("This server tool has not been approved. Enable it again after reviewing it.")
        connector = _ensure_oauth(orch, owner_id, connector)
        _check_tool_authority(connector, connector, tool_name)

        async def invoke():
            tools, session = await _initialize(connector, transport_factory=transport_factory)
            try:
                match = next((item for item in tools if item["name"] == tool_name), None)
                if match is None:
                    removed = {"tool": tool_name, "status": pins.REMOVED,
                               "pinned": connector["tool_pins"][tool_name], "current": ""}
                    await asyncio.to_thread(_disable_tool, orch, owner_id, connector_id, tool_name)
                    await _audit_tool_changes(owner_id, connector_id, [removed], "dispatch")
                    raise RemoteMCPError("The server no longer offers this tool.")
                if pins.verdict(connector["tool_pins"], match) != "ok":
                    changed = await asyncio.to_thread(_flag_changed_tool, orch, owner_id, connector_id, match)
                    await _audit_tool_changes(owner_id, connector_id, [changed] if changed else [
                        {"tool": tool_name, "status": pins.CHANGED, "pinned": connector["tool_pins"][tool_name],
                         "current": pins.definition_hash(match)}], "dispatch")
                    raise RemoteMCPError("This server tool changed since it was approved. Review it and enable it again.")
                if type(arguments) is not dict or len(json.dumps(arguments)) > _MAX_SCHEMA_BYTES:
                    raise RemoteMCPError("Tool arguments are invalid or too large.")
                if not _validate_schema(match.get("inputSchema", {"type": "object", "properties": {}}), arguments):
                    raise RemoteMCPError("Tool arguments do not match the current server schema.")
                current = await asyncio.to_thread(get_connector, orch, owner_id, connector_id)
                _check_tool_authority(current, connector, tool_name)
                return await _mcp_request(connector, {
                    "jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                }, session, transport_factory=transport_factory)
            finally:
                if isinstance(session, _SSESession):
                    await session.close()

        response, _ = _run(invoke())
        if response.get("error") or (isinstance(response.get("result"), dict)
                                      and response["result"].get("isError") is True):
            raise RemoteMCPError("The remote MCP tool call failed.")
        result = response.get("result")
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > _MAX_RESULT_BYTES:
            raise RemoteMCPError("The remote MCP tool result exceeded the allowed size.")
        _run(_audit_remote_tool(owner_id, connector_id, tool_name, arguments, "success", len(encoded.encode())))
        return {"connector_id": connector_id, "tool_name": tool_name, "result": result}
    except Exception:
        if isinstance(connector_id, str) and _ID.fullmatch(connector_id) and isinstance(tool_name, str) and _TOOL.fullmatch(tool_name):
            safe_arguments = arguments if isinstance(arguments, dict) else {}
            _run(_audit_remote_tool(owner_id, connector_id, tool_name, safe_arguments, "failure"))
        raise
