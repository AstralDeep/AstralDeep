"""Stores owner-scoped remote MCP connector state and uses bounded fixed-origin egress
to discover and invoke server tools from the remote_mcp bundled agent.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from shared import external_http
from shared.streaming_egress import EgressLimits

_AGENT_ID = "__remote_mcp_connections__"
_KEY_PREFIX = "remote_mcp_"
_MAX_TOOLS = 128
_MAX_CONNECTORS = 32
_MAX_RESULT_BYTES = 64 * 1024
_MAX_SCHEMA_BYTES = 32 * 1024
_SUPPORTED_PROTOCOLS = frozenset({"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"})
_ID = re.compile(r"[0-9a-f]{32}\Z")
_TOOL = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum",
                          "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum",
                          "description", "title", "default", "examples", "$schema"})


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
        "oauth_client_secret", "access_token", "refresh_token", "token_endpoint",
        "authorization_endpoint", "token_expires_at", "scope", "tools", "enabled",
        "tools_catalog", "last_error",
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
    result["last_error"] = str(value.get("last_error", ""))[:300]
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
            if connector.get("tools", {}).get(tool.get("name")) is True:
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


def save_connector(orch: Any, owner_id: str, values: dict[str, Any], *,
                   clear_oauth_client_credentials: bool = False) -> dict[str, Any]:
    if type(values) is not dict:
        raise RemoteMCPError("Connector configuration is invalid.")
    if type(clear_oauth_client_credentials) is not bool:
        raise RemoteMCPError("OAuth client credential clearing must be a boolean.")
    connector_id = values.get("id")
    if connector_id is not None and (not isinstance(connector_id, str) or not _ID.fullmatch(connector_id)):
        raise RemoteMCPError("Connector ID is invalid.")
    connector_id = connector_id or uuid.uuid4().hex
    key = _KEY_PREFIX + connector_id
    existing = _repository(orch).get_credential(owner_id, _AGENT_ID, key)
    candidate = dict(values)
    candidate["id"] = connector_id
    if clear_oauth_client_credentials:
        candidate.pop("oauth_client_id", None)
        candidate.pop("oauth_client_secret", None)
        for field in ("access_token", "refresh_token", "token_endpoint", "authorization_endpoint",
                      "token_expires_at", "scope"):
            candidate.pop(field, None)
    if existing:
        old = json.loads(existing)
        same_authority = old.get("url") == candidate.get("url") and old.get("auth_type") == candidate.get("auth_type")
        changed_oauth_client = (
            same_authority and candidate.get("auth_type") == "oauth"
            and (clear_oauth_client_credentials or any(
                candidate.get(key) and candidate.get(key) != old.get(key)
                for key in ("oauth_client_id", "oauth_client_secret")))
        )
        credential_fields = (
            "token", "oauth_client_id", "oauth_client_secret", "access_token", "refresh_token",
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
            if not same_authority and old.get("url") != candidate.get("url"):
                candidate["tools"] = {}
                candidate["tools_catalog"] = []
    elif len(list_connectors(orch, owner_id)) >= _MAX_CONNECTORS:
        raise RemoteMCPError("This account has reached the connector limit.")
    connector = _validate_config(candidate)
    if connector.get("token_expires_at") is None:
        connector.pop("token_expires_at", None)
    if connector["auth_type"] == "none":
        for secret in ("token", "oauth_client_id", "oauth_client_secret", "access_token", "refresh_token",
                       "token_endpoint", "authorization_endpoint", "token_expires_at", "scope"):
            connector.pop(secret, None)
    _repository(orch).set_credential(owner_id, _AGENT_ID, key, json.dumps(connector), e2e=False)
    return {key: value for key, value in connector.items()
            if key not in {"token", "oauth_client_secret", "access_token", "refresh_token"}}


def delete_connector(orch: Any, owner_id: str, connector_id: str) -> None:
    if not isinstance(connector_id, str) or not _ID.fullmatch(connector_id):
        raise RemoteMCPError("Connector was not found.")
    _repository(orch).delete_credential(owner_id, _AGENT_ID, _KEY_PREFIX + connector_id)


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
            chunk = await anext(self.events)
            self.buffer = (self.buffer + chunk.body.decode("utf-8", errors="strict")).replace("\r\n", "\n")
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


def _sanitize_schema(schema: Any, depth: int = 0) -> dict[str, Any] | None:
    if (depth > 8 or type(schema) is not dict or set(schema) - _SCHEMA_KEYS
            or len(json.dumps(schema)) > _MAX_SCHEMA_BYTES):
        return None
    kind = schema.get("type")
    if kind not in {None, "object", "array", "string", "integer", "number", "boolean", "null"}:
        return None
    result: dict[str, Any] = {}
    if kind is not None:
        result["type"] = kind
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            value = schema[key]
            if type(value) is not int or value < 0 or value > 4096:
                return None
            result[key] = value
    for key in ("minimum", "maximum"):
        if key in schema:
            value = schema[key]
            if type(value) not in (int, float):
                return None
            result[key] = value
    if "description" in schema:
        description = re.sub(r"<[^>]*>", " ", str(schema["description"]))
        description = " ".join("".join(c for c in description if ord(c) >= 32).split())[:400]
        result["description"] = "Untrusted remote metadata: " + description
    if "required" in schema:
        required = schema["required"]
        if type(required) is not list or len(required) > 64 or any(type(item) is not str for item in required):
            return None
        result["required"] = required[:]
    if "enum" in schema:
        choices = schema["enum"]
        if type(choices) is not list or len(choices) > 128 or len(json.dumps(choices)) > 4096:
            return None
        if any(type(item) not in (str, int, float, bool, type(None)) for item in choices):
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
            safe_child = _sanitize_schema(child, depth + 1)
            if safe_child is None:
                return None
            safe_properties[name] = safe_child
        result["properties"] = safe_properties
    if "items" in schema:
        item_schema = _sanitize_schema(schema["items"], depth + 1)
        if item_schema is None:
            return None
        result["items"] = item_schema
    if "additionalProperties" in schema:
        value = schema["additionalProperties"]
        if type(value) is bool:
            result["additionalProperties"] = value
        else:
            additional = _sanitize_schema(value, depth + 1)
            if additional is None:
                return None
            result["additionalProperties"] = additional
    return result


async def _first_sse_response(source, request_id: Any) -> dict[str, Any]:
    buffer = ""
    size = 0
    while size <= 1024 * 1024:
        chunk = await anext(source)
        size += len(chunk.body)
        buffer = (buffer + chunk.body.decode("utf-8", errors="strict")).replace("\r\n", "\n")
        while "\n\n" in buffer:
            raw, buffer = buffer.split("\n\n", 1)
            data = "\n".join(line[5:].lstrip() for line in raw.splitlines() if line.startswith("data:"))
            if not data:
                continue
            message = json.loads(data)
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
    raise RemoteMCPError("The MCP event response exceeded the allowed size.")


def _validate_schema(schema: Any, arguments: Any, depth: int = 0) -> bool:
    if depth > 8 or type(schema) is not dict or any(
        key in schema for key in ("$ref", "$dynamicRef", "if", "then", "else", "not", "patternProperties")
    ):
        return False
    if "enum" in schema and (type(schema["enum"]) is not list or arguments not in schema["enum"]):
        return False
    kind = schema.get("type")
    if kind == "object":
        if type(arguments) is not dict:
            return False
        props = schema.get("properties", {})
        required = schema.get("required", [])
        if (type(props) is not dict or len(props) > 64 or type(required) is not list
                or any(key not in arguments for key in required)):
            return False
        if schema.get("additionalProperties", False) is not False and not isinstance(schema.get("additionalProperties", False), dict):
            return False
        if schema.get("additionalProperties", False) is False and any(key not in props for key in arguments):
            return False
        additional = schema.get("additionalProperties", False)
        return all(
            _validate_schema(props[key], value, depth + 1) if key in props
            else isinstance(additional, dict) and _validate_schema(additional, value, depth + 1)
            for key, value in arguments.items()
        )
    if kind == "array":
        return (type(arguments) is list and len(arguments) <= min(256, int(schema.get("maxItems", 256)))
                and all(_validate_schema(schema.get("items", {}), item, depth + 1)
                        for item in arguments))
    if kind == "string":
        return (type(arguments) is str and len(arguments) <= min(4096, int(schema.get("maxLength", 4096)))
                and len(arguments) >= int(schema.get("minLength", 0))
                and ("enum" not in schema or arguments in schema["enum"]))
    if kind == "integer":
        return (type(arguments) is int and arguments >= schema.get("minimum", arguments)
                and arguments <= schema.get("maximum", arguments))
    if kind == "number":
        return (type(arguments) in (int, float) and arguments >= schema.get("minimum", arguments)
                and arguments <= schema.get("maximum", arguments))
    if kind == "boolean":
        return type(arguments) is bool
    if kind == "null":
        return arguments is None
    if kind is None:
        return (type(arguments) in (dict, list, str, int, float, bool, type(None))
                and ("enum" not in schema or arguments in schema["enum"]))
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
        for item in tools:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not _TOOL.fullmatch(item["name"]):
                continue
            schema = _sanitize_schema(item.get("inputSchema", {"type": "object", "properties": {}}))
            if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                continue
            description = re.sub(r"<[^>]*>", " ", str(item.get("description", "")))
            description = " ".join("".join(c for c in description if ord(c) >= 32).split())[:500]
            safe.append({"name": item["name"], "description": "Untrusted remote metadata: " + description,
                         "inputSchema": schema})
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
    return _run(discover_tools(connector, transport_factory=transport_factory))


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


def set_tool_selection(orch: Any, owner_id: str, connector_id: str, tool_name: str,
                       enabled: bool) -> dict[str, Any]:
    connector = get_connector(orch, owner_id, connector_id)
    if not isinstance(tool_name, str) or not _TOOL.fullmatch(tool_name):
        raise RemoteMCPError("Choose a valid server tool.")
    if not any(item.get("name") == tool_name for item in connector.get("tools_catalog", [])):
        raise RemoteMCPError("Refresh the server tool list before changing this tool.")
    connector.setdefault("tools", {})[tool_name] = enabled is True
    return save_connector(orch, owner_id, connector)


def set_connector_enabled(orch: Any, owner_id: str, connector_id: str, enabled: bool) -> dict[str, Any]:
    connector = get_connector(orch, owner_id, connector_id)
    connector["enabled"] = enabled is True
    return save_connector(orch, owner_id, connector)


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
        connector = _ensure_oauth(orch, owner_id, connector)

        async def invoke():
            tools, session = await _initialize(connector, transport_factory=transport_factory)
            try:
                match = next((item for item in tools if item["name"] == tool_name), None)
                if match is None:
                    raise RemoteMCPError("The server no longer offers this tool.")
                if type(arguments) is not dict or len(json.dumps(arguments)) > _MAX_SCHEMA_BYTES:
                    raise RemoteMCPError("Tool arguments are invalid or too large.")
                if not _validate_schema(match.get("inputSchema", {"type": "object", "properties": {}}), arguments):
                    raise RemoteMCPError("Tool arguments do not match the current server schema.")
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
