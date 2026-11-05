"""Covers owner-scoped connector storage, schema enforcement, and normal MCP session calls."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from orchestrator import remote_mcp_connectors as service


class CredentialManager:
    def __init__(self):
        self.values = {}

    def set_credential(self, owner, agent, key, value, e2e=False):
        self.values[(owner, agent, key)] = base64.b64encode(value.encode()).decode()

    def get_credential(self, owner, agent, key):
        value = self.values.get((owner, agent, key))
        return base64.b64decode(value).decode() if value else value

    def list_credential_keys(self, owner, agent):
        return [key for current_owner, current_agent, key in self.values
                if (owner, agent) == (current_owner, current_agent)]

    def delete_credential(self, owner, agent, key):
        self.values.pop((owner, agent, key), None)


class FakeTransport:
    def __init__(self):
        self.requests = []

    async def post(self, request):
        payload = json.loads(request.body)
        self.requests.append((request.path, dict(request.headers), payload))
        return SimpleNamespace(status=202, headers={}, body=b"")

    async def post_response_stream(self, request):
        payload = json.loads(request.body)
        self.requests.append((request.path, dict(request.headers), payload))
        if payload["method"] == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {},
                      "serverInfo": {"name": "fixture", "version": "1"}}
        elif payload["method"] == "tools/list":
            result = {"tools": [{
                "name": "echo", "description": "Echo a message",
                "inputSchema": {"type": "object", "properties": {
                    "message": {"type": "string", "maxLength": 80}}, "required": ["message"],
                    "additionalProperties": False},
            }]}
        elif payload["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": payload["params"]["arguments"]["message"]}]}
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode()
        yield SimpleNamespace(status=200, headers={"content-type": "application/json", "mcp-session-id": "session-1"}, body=b"")
        yield SimpleNamespace(status=200, headers={}, body=body)


@pytest.fixture
def world():
    manager = CredentialManager()
    orch = SimpleNamespace(credential_manager=manager)
    saved = service.save_connector(orch, "owner-a", {
        "name": "Fixture", "url": "https://mcp.example/mcp", "transport": "streamable_http",
        "auth_type": "none", "tools": {"echo": True}, "tools_catalog": [],
    })
    return orch, saved["id"]


def test_connector_records_are_owner_scoped_and_never_list_secrets(world):
    orch, connector_id = world
    service.save_connector(orch, "owner-a", {
        "id": connector_id, "name": "Fixture", "url": "https://mcp.example/mcp",
        "transport": "streamable_http", "auth_type": "bearer", "token": "private-token",
    })

    assert service.list_connectors(orch, "owner-b") == []
    listed = service.list_connectors(orch, "owner-a")
    assert listed[0]["id"] == connector_id
    assert listed[0]["has_credential"] is True
    assert listed[0]["token"] == ""
    assert "private-token" not in json.dumps(listed)
    assert "private-token" not in " ".join(orch.credential_manager.values.values())
    assert all("private-token" not in value for value in orch.credential_manager.values.values())


def test_connector_endpoint_must_be_credential_free_https():
    for value in ("http://mcp.example/mcp", "https://user:pass@mcp.example/mcp",
                  "https://mcp.example/mcp?redirect=https://elsewhere", "https://mcp.example/mcp#f"):
        with pytest.raises(service.RemoteMCPError):
            service._validate_url(value)


def test_new_credential_survives_auth_change_and_old_secret_is_not_reused(world):
    orch, connector_id = world
    service.save_connector(orch, "owner-a", {
        "id": connector_id, "name": "Fixture", "url": "https://mcp.example/mcp",
        "transport": "streamable_http", "auth_type": "bearer", "token": "new-token",
    })
    assert service.get_connector(orch, "owner-a", connector_id)["token"] == "new-token"
    stale = service.get_connector(orch, "owner-a", connector_id)
    stale["url"] = "https://new.example/mcp"
    with pytest.raises(service.RemoteMCPError, match="bearer token"):
        service.save_connector(orch, "owner-a", stale)
    assert service.get_connector(orch, "owner-a", connector_id)["token"] == "new-token"
    service.save_connector(orch, "owner-a", {
        "id": connector_id, "name": "Fixture", "url": "https://mcp.example/mcp",
        "transport": "streamable_http", "auth_type": "bearer", "token": "",
    })
    assert service.get_connector(orch, "owner-a", connector_id)["token"] == "new-token"
    service.save_connector(orch, "owner-a", {
        "id": connector_id, "name": "Fixture", "url": "https://new.example/mcp",
        "transport": "streamable_http", "auth_type": "bearer", "token": "replacement",
    })
    assert service.get_connector(orch, "owner-a", connector_id)["token"] == "replacement"


def test_changing_oauth_client_invalidates_tokens_and_metadata(world):
    orch, connector_id = world
    service.save_connector(orch, "owner-a", {
        "id": connector_id, "name": "Fixture", "url": "https://mcp.example/mcp",
        "transport": "streamable_http", "auth_type": "oauth", "oauth_client_id": "client-a",
        "oauth_client_secret": "secret-a", "access_token": "access-a", "refresh_token": "refresh-a",
        "token_endpoint": "https://auth.example/token", "authorization_endpoint": "https://auth.example/login",
        "token_expires_at": 1234567890,
    })

    service.save_connector(orch, "owner-a", {
        "id": connector_id, "name": "Fixture", "url": "https://mcp.example/mcp",
        "transport": "streamable_http", "auth_type": "oauth", "oauth_client_id": "client-b",
        "oauth_client_secret": "secret-b",
    })

    connector = service.get_connector(orch, "owner-a", connector_id)
    assert connector["oauth_client_id"] == "client-b"
    assert connector["oauth_client_secret"] == "secret-b"
    assert "access_token" not in connector
    assert "refresh_token" not in connector
    assert "token_endpoint" not in connector
    assert "token_expires_at" not in connector


def test_oauth_endpoints_allow_query_and_empty_expiry_is_omitted(world):
    orch, connector_id = world
    service.save_connector(orch, "owner-a", {
        "id": connector_id, "name": "Fixture", "url": "https://mcp.example/mcp",
        "transport": "streamable_http", "auth_type": "oauth", "oauth_client_id": "client-a",
        "token_endpoint": "https://auth.example/token?tenant=one",
        "authorization_endpoint": "https://auth.example/authorize?tenant=one",
        "token_expires_at": None,
    })

    saved = service.get_connector(orch, "owner-a", connector_id)
    assert saved["token_endpoint"] == "https://auth.example/token?tenant=one"
    assert saved["authorization_endpoint"] == "https://auth.example/authorize?tenant=one"
    assert "token_expires_at" not in saved


def test_oauth_http_request_preserves_raw_authentication_status(monkeypatch):
    response = SimpleNamespace(status=401, headers={
        "www-authenticate": 'Bearer resource_metadata="https://auth.example/resource"',
    }, body=b"")
    calls = []

    class StatusTransport:
        async def request_status(self, method, request):
            calls.append((method, request.path))
            return response

    monkeypatch.setattr(service.external_http, "fixed_origin_transport_for_external_url",
                        lambda url, limits: (StatusTransport(), "/resource"))
    status, headers, body = service._run(service.http_request(
        "https://mcp.example/resource", "GET", max_response_bytes=128,
    ))

    assert status == 401
    assert headers["www-authenticate"].startswith("Bearer resource_metadata=")
    assert body == b""
    assert calls == [("GET", "/resource")]


def test_schema_references_and_executable_remote_keywords_are_rejected():
    assert service._sanitize_schema({"type": "object", "$ref": "https://evil.invalid/schema"}) is None
    assert service._sanitize_schema({"type": "object", "properties": {
        "value": {"type": "string", "pattern": "^(.*)$"}}}) is None
    sdk_schema = {"type": "object", "title": "EchoArguments", "properties": {
        "message": {"type": "string", "title": "Message"}}, "required": ["message"]}
    assert service._sanitize_schema(sdk_schema) == {
        "type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}


def test_streamable_call_uses_negotiated_session_and_validates_arguments(world, monkeypatch):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector["tools_catalog"] = [{"name": "echo", "inputSchema": {"type": "object", "properties": {}}}]
    connector["tools"] = {"echo": True}
    service.save_connector(orch, "owner-a", connector)
    fake = FakeTransport()
    audit_events = []

    async def record(**event):
        audit_events.append(event)

    monkeypatch.setattr("audit.hooks.record_generic", record)

    with pytest.raises(service.RemoteMCPError, match="arguments"):
        service.call_tool(orch, "owner-a", connector_id, "echo", {"message": 4},
                          transport_factory=lambda *_args, **_kwargs: fake)
    result = service.call_tool(orch, "owner-a", connector_id, "echo", {"message": "hello"},
                               transport_factory=lambda *_args, **_kwargs: fake)

    call = next(item for item in fake.requests if item[2]["method"] == "tools/call")
    assert result["result"]["content"][0]["text"] == "hello"
    assert call[1]["mcp-session-id"] == "session-1"
    assert call[1]["mcp-protocol-version"] == "2025-11-25"
    assert audit_events[-1]["action_type"] == "remote_mcp.tool.dispatch"
    assert audit_events[-1]["inputs_meta"]["connector_id"] == connector_id
    assert audit_events[-1]["inputs_meta"]["tool_name"] == "echo"
    assert "hello" not in json.dumps(audit_events[-1])


def test_owner_cannot_call_disabled_connector_or_tool(world):
    orch, connector_id = world
    with pytest.raises(service.RemoteMCPError, match="not found"):
        service.call_tool(orch, "owner-b", connector_id, "echo", {})
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector["tools"] = {"echo": False}
    service.save_connector(orch, "owner-a", connector)
    with pytest.raises(service.RemoteMCPError, match="not enabled"):
        service.call_tool(orch, "owner-a", connector_id, "echo", {})


@pytest.mark.parametrize("value", [None, "", " https://mcp.example/mcp", "https://mcp.example:444/mcp",
                                    "https://mcp.example/mcp?q=1", "https://mcp.example/mcp#frag",
                                    "https://mcp.example/with space"])
def test_invalid_remote_endpoint_forms_are_rejected(value):
    with pytest.raises(service.RemoteMCPError):
        service._validate_url(value)


def test_config_rejects_unknown_fields_invalid_secrets_limits_and_auth(world):
    for update in (
        {"extra": "value"}, {"name": " "}, {"transport": "http"},
        {"auth_type": "basic"}, {"token": "x" * 8193}, {"oauth_client_id": "x" * 513},
        {"token_endpoint": "http://auth.example/token"}, {"token_expires_at": True},
        {"scope": "x" * 513}, {"tools": {str(index): True for index in range(129)}},
        {"tools_catalog": ["x" for _ in range(129)]},
    ):
        base = {"name": "Fixture", "url": "https://mcp.example/mcp",
                "transport": "streamable_http", "auth_type": "none"}
        base.update(update)
        with pytest.raises(service.RemoteMCPError):
            service._validate_config(base)
    with pytest.raises(service.RemoteMCPError, match="bearer token"):
        service._validate_config({"name": "Fixture", "url": "https://mcp.example/mcp",
                                  "transport": "sse", "auth_type": "bearer"})


def test_connector_storage_corruption_and_ownerless_access_fail_closed(world):
    orch, connector_id = world
    assert service.list_connectors(orch, "") == []
    with pytest.raises(service.RemoteMCPError, match="storage"):
        service.list_connectors(SimpleNamespace(), "owner-a")
    manager = orch.credential_manager
    manager.set_credential("owner-a", "__remote_mcp_connections__", "other", "not-json")
    manager.set_credential("owner-a", "__remote_mcp_connections__", "remote_mcp_" + "f" * 32, "{")
    assert len(service.list_connectors(orch, "owner-a")) == 1
    with pytest.raises(service.RemoteMCPError, match="not found"):
        service.get_connector(orch, "owner-a", "invalid")
    with pytest.raises(service.RemoteMCPError, match="could not be read"):
        service.get_connector(orch, "owner-a", "f" * 32)
    service.delete_connector(orch, "owner-a", connector_id)
    assert service.list_connectors(orch, "owner-a") == []


def test_connector_limit_and_url_change_clear_tool_catalog(world):
    orch, connector_id = world
    original = service.get_connector(orch, "owner-a", connector_id)
    original.update({"tools_catalog": [{"name": "echo", "description": "safe",
                                        "inputSchema": {"type": "object"}}],
                     "tools": {"echo": True}})
    service.save_connector(orch, "owner-a", original)
    changed = dict(original, url="https://other.example/mcp")
    saved = service.save_connector(orch, "owner-a", changed)
    current = service.get_connector(orch, "owner-a", connector_id)
    assert saved["tools_catalog"] == []
    assert current["tools"] == {}
    for index in range(31):
        service.save_connector(orch, "owner-a", {
            "name": f"Connector {index}", "url": f"https://server{index}.example/mcp",
            "transport": "streamable_http", "auth_type": "none",
        })
    with pytest.raises(service.RemoteMCPError, match="limit"):
        service.save_connector(orch, "owner-a", {
            "name": "One too many", "url": "https://last.example/mcp",
            "transport": "streamable_http", "auth_type": "none",
        })


def test_catalog_selection_and_schema_validation_bounds(world):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector.update({"tools_catalog": [{"name": "echo", "description": "ok",
                                         "inputSchema": {"type": "object"}}],
                      "tools": {"echo": True}})
    service.save_connector(orch, "owner-a", connector)
    assert service.list_enabled_tools(orch, "owner-a")[0]["name"] == "echo"
    assert service._validate_schema({"type": "array", "maxItems": 1, "items": {"type": "string"}}, ["a"])
    assert not service._validate_schema({"type": "array", "maxItems": 1, "items": {"type": "string"}}, ["a", "b"])
    assert service._validate_schema({"type": "integer", "minimum": 2, "maximum": 4}, 3)
    assert not service._validate_schema({"type": "integer", "minimum": 2}, True)
    assert service._validate_schema({"type": "number"}, 2.5)
    assert service._validate_schema({"type": "boolean"}, False)
    assert service._validate_schema({"type": "null"}, None)
    assert service._validate_schema({}, "primitive")
    assert not service._validate_schema({"type": "object", "required": ["x"]}, {})
    assert service._sanitize_schema({"type": "object", "required": [1]}) is None
    assert service._sanitize_schema({"type": "object", "enum": [["nested"]]}) is None
    assert service._sanitize_schema({"type": "array", "items": {"$ref": "#"}}) is None


class FakeSSETransport:
    def __init__(self):
        self.posts = []
        self.closed = False

    async def get_stream(self, request):
        assert request.path == "/sse"
        try:
            yield SimpleNamespace(status=200, headers={"content-type": "text/event-stream"}, body=b"")
            events = (
                b"event: endpoint\ndata: /messages/?session_id=abc\n\n"
                b'event: message\ndata: {"jsonrpc":"2.0","id":"init","result":{"protocolVersion":"2025-11-25"}}\n\n'
                b'event: message\ndata: {"jsonrpc":"2.0","id":"tools","result":{"tools":[{"name":"echo","inputSchema":{"type":"object"}}]}}\n\n'
            )
            yield SimpleNamespace(status=200, headers={}, body=events)
        finally:
            self.closed = True

    async def post(self, request):
        self.posts.append((request.path, json.loads(request.body), request.headers))
        return SimpleNamespace(status=202, headers={}, body=b"")


@pytest.mark.parametrize("endpoint", ["https://evil.example/messages", "//evil.example/messages",
                                       "/../messages", "/messages#fragment", "", "/messages?" + "x" * 2049])
def test_sse_server_message_endpoint_must_stay_bounded_and_same_origin(endpoint):
    session = service._SSESession({"url": "https://mcp.example/sse"}, object(), "/sse")
    with pytest.raises(service.RemoteMCPError):
        session._same_origin_endpoint(endpoint)


def test_legacy_sse_discovery_uses_same_origin_session_endpoint_and_closes_stream(world):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector["transport"] = "sse"
    connector["url"] = "https://mcp.example/sse"
    fake = FakeSSETransport()
    tools = service._run(service.discover_tools(
        connector, transport_factory=lambda *_args, **_kwargs: fake))
    assert [item["name"] for item in tools] == ["echo"]
    assert fake.posts[0][0] == "/messages/?session_id=abc"
    assert fake.posts[0][1]["method"] == "initialize"
    assert fake.closed is True


def test_sse_message_parsing_and_schema_rejections():
    assert service._sse_message(b"event: message\ndata: {\"id\":1}\n\n") == {"id": 1}
    with pytest.raises(ValueError):
        service._sse_message(b"event: ping\ndata: pong\n\n")
    with pytest.raises(ValueError):
        service._sse_message(b"data: \xff\n\n")
    with pytest.raises(service.RemoteMCPError, match="large"):
        service._sse_message(b"x" * (1024 * 1024 + 1))


class FakeEventTransport:
    def __init__(self, content_type, messages):
        self.content_type = content_type
        self.messages = messages
        self.closed = False
        self.requests = []

    async def post_response_stream(self, request):
        payload = json.loads(request.body)
        self.requests.append(payload)
        try:
            yield SimpleNamespace(status=200, headers={"content-type": self.content_type}, body=b"")
            for message in self.messages(payload):
                yield SimpleNamespace(status=200, headers={}, body=message)
        finally:
            self.closed = True


@pytest.mark.parametrize("bad_endpoint", ["https://evil.example/messages", "//evil.example/path",
                                          "/../escape", "/messages#frag"])
def test_sse_absolute_endpoint_rejected_and_same_origin_absolute_allowed(bad_endpoint):
    session = service._SSESession({"url": "https://mcp.example/sse"}, object(), "/sse")
    with pytest.raises(service.RemoteMCPError):
        session._same_origin_endpoint(bad_endpoint)
    assert session._same_origin_endpoint("https://mcp.example/messages/?session_id=one") == "/messages/?session_id=one"


@pytest.mark.asyncio
async def test_streamable_http_event_response_matches_jsonrpc_id_and_closes(world):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)
    payload = {"jsonrpc": "2.0", "id": "request-1", "method": "tools/list"}

    def messages(_payload):
        return [b'data: {"jsonrpc":"2.0","id":"other","result":{}}\n\n',
                b'data: {"jsonrpc":"2.0","id":"request-1","result":{"tools":[]}}\n\n']

    fake = FakeEventTransport("text/event-stream", messages)
    result, session_id = await service._mcp_request(
        connector, payload, transport_factory=lambda *_args, **_kwargs: fake)
    assert result["result"]["tools"] == []
    assert session_id == ""
    assert fake.closed is True


@pytest.mark.asyncio
async def test_streamable_http_response_types_and_bounds_fail_closed(world):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    bad_type = FakeEventTransport("text/plain", lambda _payload: [b"body"])
    with pytest.raises(service.RemoteMCPError, match="response type"):
        await service._mcp_request(connector, payload, transport_factory=lambda *_a, **_kw: bad_type)
    bad_json = FakeEventTransport("application/json", lambda _payload: [b"not-json"])
    with pytest.raises(ValueError):
        await service._mcp_request(connector, payload, transport_factory=lambda *_a, **_kw: bad_json)
    huge = FakeEventTransport("application/json", lambda _payload: [b"x" * (1024 * 1024 + 1)])
    with pytest.raises(service.RemoteMCPError, match="too large"):
        await service._mcp_request(connector, payload, transport_factory=lambda *_a, **_kw: huge)
    with pytest.raises(service.RemoteMCPError, match="SSE session"):
        await service._mcp_request(dict(connector, transport="sse"), payload, "missing")


def test_legacy_sse_events_parse_and_invalid_streams_close():
    session = service._SSESession({"url": "https://mcp.example/sse", "token": "secret"}, object(), "/sse")
    assert session.headers["authorization"] == "Bearer secret"
    assert service._run(_sse_event(session)) == {"event": "message", "data": "{\"id\":1}"}
    with pytest.raises(service.RemoteMCPError, match="size"):
        service._run(_sse_overflow(session))


async def _sse_event(session):
    async def events():
        yield SimpleNamespace(body=b"event: message\r\ndata: {\"id\":1}\r\n\r\n")
    session.events = events().__aiter__()
    return await session._next_event()


async def _sse_overflow(session):
    async def events():
        yield SimpleNamespace(body=b"x" * 65537)
    session.events = events().__aiter__()
    await session._next_event()


@pytest.mark.parametrize("schema,args,expected", [
    ({"type": "string", "minLength": 2, "maxLength": 4}, "abc", True),
    ({"type": "string", "minLength": 2}, "a", False),
    ({"type": "number", "minimum": 2, "maximum": 4}, 1, False),
    ({"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": False},
     {"x": "ok", "extra": True}, False),
    ({"type": "object", "properties": {"x": {"type": "integer"}}, "additionalProperties": {"type": "boolean"}},
     {"extra": True}, True),
    ({"$ref": "#"}, {}, False),
])
def test_recursive_argument_schema_rules(schema, args, expected):
    assert service._validate_schema(schema, args) is expected


def test_sanitize_schema_rejects_remote_executable_or_oversized_metadata():
    for schema in (
        {"type": "object", "patternProperties": {}},
        {"type": "object", "properties": {"x": {"type": "string", "pattern": "x"}}},
        {"type": "object", "required": ["x"] * 65},
        {"type": "object", "properties": {str(i): {"type": "string"} for i in range(65)}},
        {"type": "string", "minLength": -1},
        {"type": "string", "minimum": "1"},
        {"type": "string", "enum": ["x"] * 129},
    ):
        assert service._sanitize_schema(schema) is None
    safe = service._sanitize_schema({"type": "string", "description": "<b>useful</b>\n text"})
    assert safe["description"] == "Untrusted remote metadata: useful text"


@pytest.mark.parametrize("response,match", [
    ({"error": {"message": "private"}}, "call failed"),
    ({"result": {"isError": True}}, "call failed"),
    ({"result": {"content": "x" * 70000}}, "allowed size"),
])
def test_tool_call_server_failures_are_audited_without_output_leak(world, monkeypatch, response, match):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector.update({"tools": {"echo": True}, "tools_catalog": []})
    service.save_connector(orch, "owner-a", connector)
    events = []

    async def record(**event):
        events.append(event)

    async def initialize(_connector, **_kwargs):
        return ([{"name": "echo", "description": "safe", "inputSchema": {"type": "object"}}], None)

    async def request(*_args, **_kwargs):
        return response, ""

    monkeypatch.setattr("audit.hooks.record_generic", record)
    monkeypatch.setattr(service, "_initialize", initialize)
    monkeypatch.setattr(service, "_mcp_request", request)
    with pytest.raises(service.RemoteMCPError, match=match):
        service.call_tool(orch, "owner-a", connector_id, "echo", {})
    assert events[-1]["outcome"] == "failure"
    assert "private" not in json.dumps(events)


def test_tool_call_rechecks_enabled_state_discovery_and_argument_bounds(world, monkeypatch):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector.update({"tools": {"echo": True}, "tools_catalog": []})
    service.save_connector(orch, "owner-a", connector)

    async def empty(_connector, **_kwargs):
        return ([{"name": "echo", "description": "safe", "inputSchema": {"type": "object"}}], None)

    monkeypatch.setattr(service, "_initialize", empty)
    async def absent(_connector, **_kwargs):
        return [], None

    monkeypatch.setattr(service, "_initialize", absent)
    with pytest.raises(service.RemoteMCPError, match="no longer offers"):
        service.call_tool(orch, "owner-a", connector_id, "echo", {})
    monkeypatch.setattr(service, "_initialize", empty)
    with pytest.raises(service.RemoteMCPError, match="too large"):
        service.call_tool(orch, "owner-a", connector_id, "echo", {"payload": "x" * 33000})
    with pytest.raises(service.RemoteMCPError, match="valid server tool"):
        service.call_tool(orch, "owner-a", connector_id, "bad name", {})

    connector["enabled"] = False
    service.save_connector(orch, "owner-a", connector)
    with pytest.raises(service.RemoteMCPError, match="disabled"):
        service.call_tool(orch, "owner-a", connector_id, "echo", {})


def test_tool_call_sse_session_is_closed_and_success_is_returned(world, monkeypatch):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector.update({"transport": "sse", "tools": {"echo": True}, "tools_catalog": []})
    service.save_connector(orch, "owner-a", connector)
    session = service._SSESession({"url": "https://mcp.example/sse"}, object(), "/sse")
    closed = []

    async def close():
        closed.append(True)

    session.close = close

    async def initialize(_connector, **_kwargs):
        return ([{"name": "echo", "description": "safe", "inputSchema": {"type": "object"}}], session)

    async def request(_connector, payload, _session, **_kwargs):
        assert payload["method"] == "tools/call"
        return {"result": {"content": [{"type": "text", "text": "done"}]}}, session

    monkeypatch.setattr(service, "_initialize", initialize)
    monkeypatch.setattr(service, "_mcp_request", request)
    monkeypatch.setattr("audit.hooks.record_generic", _record_event)
    result = service.call_tool(orch, "owner-a", connector_id, "echo", {})
    assert result["result"]["content"][0]["text"] == "done"
    assert closed == [True]


async def _record_event(**_event):
    return None


def test_validation_and_transport_factory_edge_contracts(world):
    orch, connector_id = world
    with pytest.raises(service.RemoteMCPError, match="invalid port"):
        service._validate_url("https://mcp.example:bad/mcp")
    with pytest.raises(service.RemoteMCPError, match="invalid"):
        service._validate_oauth_endpoint("https://auth.example:bad/token")
    with pytest.raises(service.RemoteMCPError, match="ID"):
        service._validate_config({"id": "bad", "name": "x", "url": "https://mcp.example",
                                  "transport": "sse", "auth_type": "none"})
    with pytest.raises(service.RemoteMCPError, match="configuration"):
        service.save_connector(orch, "owner-a", [])
    with pytest.raises(service.RemoteMCPError, match="ID"):
        service.save_connector(orch, "owner-a", {"id": "bad"})
    connector = service.get_connector(orch, "owner-a", connector_id)
    fake = object()
    origins = []
    transport, path = service._transport(
        connector, transport_factory=lambda origin, **_kwargs: (origins.append(origin) or fake))
    assert transport == fake
    assert path == "/mcp"
    assert origins == ["https://mcp.example"]


def test_transport_factory_uses_path_query_and_none_auth_stores_no_secrets(world):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector["url"] = "https://mcp.example/prefix/mcp"
    connector["token"] = "old-secret"
    connector["auth_type"] = "none"
    service.save_connector(orch, "owner-a", connector)
    saved = service.get_connector(orch, "owner-a", connector_id)
    assert "token" not in saved
    origins = []
    _transport, path = service._transport(saved, transport_factory=lambda origin, limits: (origins.append(origin) or object()))
    assert origins == ["https://mcp.example"]
    assert path == "/prefix/mcp"


@pytest.mark.asyncio
async def test_request_notify_carries_bearer_and_session_headers(world):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector.update({"token": "secret", "_protocol_version": "2025-06-18"})
    fake = FakeTransport()
    await service._mcp_notify(connector, {"method": "notifications/initialized"}, "session-x",
                              transport_factory=lambda *_a, **_kw: fake)
    request = fake.requests[-1]
    assert request[1]["authorization"] == "Bearer secret"
    assert request[1]["mcp-session-id"] == "session-x"
    assert request[1]["mcp-protocol-version"] == "2025-06-18"
    with pytest.raises(service.RemoteMCPError, match="SSE session"):
        await service._mcp_notify(dict(connector, transport="sse"), {}, "missing")


def test_legacy_sse_request_matches_response_and_fails_after_bounded_noise():
    class ResponseTransport:
        def __init__(self, matches):
            self.matches = matches
            self.paths = []

        async def post(self, request):
            self.paths.append(request.path)
            return SimpleNamespace(status=202, headers={}, body=b"")

    async def response_run(matches):
        transport = ResponseTransport(matches)
        session = service._SSESession({"url": "https://mcp.example/sse"}, transport, "/sse")
        session.endpoint = "/messages/?session_id=abc"

        async def events():
            for payload in matches:
                yield SimpleNamespace(body=("event: message\ndata: " + payload + "\n\n").encode())
        session.events = events().__aiter__()
        response = await session.request({"id": "expected"})
        return response, transport

    response, transport = service._run(response_run([
        "invalid-json", '{"id":"other"}', '{"id":"expected","result":{}}',
    ]))
    assert response["id"] == "expected"
    assert transport.paths == ["/messages/?session_id=abc"]


def test_legacy_sse_request_and_notify_error_on_no_matching_response():
    async def fail():
        session = service._SSESession({"url": "https://mcp.example/sse"}, SimpleNamespace(
            post=unreachable_post), "/sse")
        session.endpoint = "/messages/"

        async def events():
            for index in range(32):
                yield SimpleNamespace(body=(f'data: {{"id":{index}}}\n\n').encode())
        session.events = events().__aiter__()
        with pytest.raises(service.RemoteMCPError, match="did not answer"):
            await session.request({"id": "expected"})

    async def unreachable_post(_request):
        return SimpleNamespace(status=202, headers={}, body=b"")

    service._run(fail())


def test_initialize_negotiation_failures_and_bad_tool_catalogue(world, monkeypatch):
    orch, connector_id = world
    connector = service.get_connector(orch, "owner-a", connector_id)

    async def reject(_connector, payload, _session="", **_kwargs):
        if payload["method"] == "initialize":
            return {"error": {"code": -1}}, "session"
        return {}, "session"

    monkeypatch.setattr(service, "_mcp_request", reject)
    with pytest.raises(service.RemoteMCPError, match="rejected initialization"):
        service._run(service._initialize(connector))

    async def unsupported(_connector, payload, _session="", **_kwargs):
        if payload["method"] == "initialize":
            return {"result": {"protocolVersion": "future"}}, "session"
        return {}, "session"

    monkeypatch.setattr(service, "_mcp_request", unsupported)
    with pytest.raises(service.RemoteMCPError, match="unsupported protocol"):
        service._run(service._initialize(connector))

    async def invalid_tools(_connector, payload, _session="", **_kwargs):
        if payload["method"] == "initialize":
            return {"result": {}}, "session"
        return {"result": {"tools": "bad"}}, "session"

    monkeypatch.setattr(service, "_mcp_request", invalid_tools)
    monkeypatch.setattr(service, "_mcp_notify", _noop)
    with pytest.raises(service.RemoteMCPError, match="invalid tool list"):
        service._run(service._initialize(connector))


async def _noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_refresh_tools_requires_enabled_connector_and_oauth_refresh(monkeypatch, world):
    orch, connector_id = world
    disabled = service.get_connector(orch, "owner-a", connector_id)
    disabled["enabled"] = False
    service.save_connector(orch, "owner-a", disabled)
    with pytest.raises(service.RemoteMCPError, match="Enable the connector"):
        service.refresh_tools(orch, "owner-a", connector_id)

    enabled = service.get_connector(orch, "owner-a", connector_id)
    enabled.update({"enabled": True, "auth_type": "oauth", "oauth_client_id": "client",
                    "refresh_token": "refresh", "token_expires_at": 1})
    service.save_connector(orch, "owner-a", enabled)
    monkeypatch.setattr("orchestrator.remote_mcp_oauth.refresh_authorization", _refresh_true)
    monkeypatch.setattr(service, "discover_tools", _empty_tools)
    monkeypatch.setattr(service, "_ensure_oauth", lambda *_a, **_kw: enabled)
    assert service.refresh_tools(orch, "owner-a", connector_id) == []


async def _refresh_true(*_args, **_kwargs):
    return True


async def _empty_tools(*_args, **_kwargs):
    return []


@pytest.mark.asyncio
async def test_run_uses_worker_when_called_inside_event_loop():
    assert service._run(asyncio.sleep(0, result="completed")) == "completed"
