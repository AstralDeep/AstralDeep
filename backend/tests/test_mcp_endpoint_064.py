from __future__ import annotations

import json
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from orchestrator.mcp_server_endpoint import (
    _admitted_request,
    _decode_name_header,
    _parse_request,
    _record_metric,
    _structured,
    _tool_result,
    _wait_for_disconnect,
    _with_origin,
    install_mcp_server,
)
from orchestrator.mcp_projection import project_tools
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.tool_visibility import eligible_tool_pairs
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionCoordinator,
)
from shared.protocol import AgentCard, AgentSkill, MCPResponse


BASE = "http://mcp.test"
META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test-host", "version": "1"},
}


class _DB:
    def __init__(self):
        self.disabled: dict[str, list[str]] = {}

    def get_user_disabled_agents(self, user_id):
        return self.disabled.get(user_id, [])


class _Permissions:
    def __init__(self):
        self.allowed = True
        self.allowed_by_identity = {}

    def is_tool_allowed(self, user_id, agent_id, tool_name):
        return self.allowed_by_identity.get(
            (user_id, agent_id, tool_name),
            self.allowed,
        )

    def get_enabled_scope_names(self, user_id, agent_id):
        return ["tools:read"] if self.allowed else []


class _Orchestrator:
    def __init__(self):
        self.history = SimpleNamespace(db=_DB())
        self.tool_permissions = _Permissions()
        self.security_flags = {}
        self.agents = {"reader-1": object()}
        self.local_agents = {}
        self.agent_cards = {
            "reader-1": AgentCard(
                name="Reader",
                description="Reads things",
                agent_id="reader-1",
                skills=[
                    AgentSkill(
                        id="read_value",
                        name="Read value",
                        description="Return a value",
                        scope="tools:read",
                        input_schema={
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                        },
                        output_schema={
                            "type": "object",
                            "properties": {"echo": {"type": "string"}},
                        },
                    )
                ],
            )
        }
        configs = (
            AdmissionClassConfig(
                AdmissionClass.GLOBAL,
                None,
                2,
                0,
                None,
                "test",
            ),
            AdmissionClassConfig(
                AdmissionClass.MCP,
                AdmissionClass.GLOBAL,
                1,
                2,
                1000,
                "test",
            ),
        )
        self.work_admission = WorkAdmissionCoordinator(
            admission_classes=configs,
            repository=InMemoryWorkAdmissionRepository(),
            clock=lambda: datetime.now(UTC),
            slot_lease=timedelta(seconds=30),
        )
        self.runtime_observability = RuntimeObservability()
        self.calls = []

    def _is_draft_agent(self, agent_id):
        return False

    async def _call_work_admission(self, method, *args, **kwargs):
        return method(*args, **kwargs)

    async def _claim_personal_agent_operation(
        self,
        *,
        owner_user_id,
        operation_kind,
        idempotency_namespace,
        idempotency_key,
        normalized_identity,
        admission_class,
        request_generation,
        wait_seconds,
        **kwargs,
    ):
        del normalized_identity, wait_seconds, kwargs
        owner = OperationOwner(OwnerScope.USER, owner_user_id, None)
        admitted = self.work_admission.submit(
            OperationRequest(
                operation_kind=operation_kind,
                admission_class=admission_class,
                owner=owner,
                submission_id=uuid.uuid4(),
                idempotency_namespace=idempotency_namespace,
                idempotency_key=idempotency_key,
                normalized_input_digest="a" * 64,
                chat_id=None,
                parent_operation_id=None,
                connection_generation=None,
                request_generation=request_generation,
            )
        )
        if not admitted.accepted:
            return None
        claim = self.work_admission.claim_operation(admission_class, admitted.operation_id)
        return (owner, claim) if claim is not None else None

    async def execute_mcp_tool(self, **kwargs):
        self.calls.append(kwargs)
        return MCPResponse(
            result={"echo": kwargs["arguments"].get("value", "")},
            ui_components=[{"type": "text", "content": "Rendered result"}],
            responder_info={"name": "reader-1", "version": "1"},
        )


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.test/realms/astral")
    orchestrator = _Orchestrator()
    app = FastAPI()
    install_mcp_server(app, orchestrator, public_base_url=BASE)
    return TestClient(app), orchestrator


def _request(method, params=None, *, token="dev-mcp-token", headers=None, request_id=1):
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**(params or {}), "_meta": dict(META)},
    }
    merged = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    merged.update(headers or {})
    return body, merged


def test_discover_and_protected_resource_metadata(endpoint):
    client, _ = endpoint
    metadata = client.get("/.well-known/oauth-protected-resource/mcp")
    assert metadata.status_code == 200
    assert metadata.json() == {
        "resource": f"{BASE}/mcp",
        "authorization_servers": ["https://idp.test/realms/astral"],
        "scopes_supported": [
            "mcp:discover",
            "mcp:tools:read",
            "mcp:tools:invoke",
        ],
        "bearer_methods_supported": ["header"],
    }
    body, headers = _request("server/discover")
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["supportedVersions"] == ["2026-07-28"]
    assert result["capabilities"] == {"tools": {}}
    assert result["ttlMs"] == 60_000
    assert result["cacheScope"] == "private"
    assert "extensions" not in result["capabilities"]


def test_list_is_permission_projected_private_and_recomputed(endpoint):
    client, orchestrator = endpoint
    body, headers = _request("tools/list")
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=60"
    tool = response.json()["result"]["tools"][0]
    assert tool["name"] == "read_value"
    assert tool["inputSchema"]["$schema"].endswith("2020-12/schema")
    assert tool["outputSchema"]["$schema"].endswith("2020-12/schema")
    assert tool["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
    }
    orchestrator.tool_permissions.allowed = False
    response = client.post("/mcp", json=body, headers=headers)
    assert response.json()["result"]["tools"] == []


def test_two_users_match_chat_visibility_without_cross_user_leakage(endpoint):
    _, orchestrator = endpoint
    orchestrator.agents["writer-1"] = object()
    orchestrator.agent_cards["writer-1"] = AgentCard(
        name="Writer",
        description="Writes things",
        agent_id="writer-1",
        skills=[
            AgentSkill(
                id="write_value",
                name="Write value",
                description="Persist a value",
                scope="tools:write",
                input_schema={"type": "object", "properties": {}},
            )
        ],
    )
    orchestrator.tool_permissions.allowed = False
    orchestrator.tool_permissions.allowed_by_identity = {
        ("reader-user", "reader-1", "read_value"): True,
        ("writer-user", "writer-1", "write_value"): True,
    }

    for user_id, expected in (
        ("reader-user", {("reader-1", "read_value")}),
        ("writer-user", {("writer-1", "write_value")}),
    ):
        chat_set = {
            (agent_id, skill.id)
            for agent_id, skill in eligible_tool_pairs(orchestrator, user_id)
        }
        mcp_set = {
            (tool.agent_id, tool.skill_id)
            for tool in project_tools(orchestrator, user_id)
        }
        assert chat_set == mcp_set == expected

    assert not (
        {tool.name for tool in project_tools(orchestrator, "reader-user")}
        & {tool.name for tool in project_tools(orchestrator, "writer-user")}
    )


def test_call_rechecks_permission_and_returns_content_plus_structured(endpoint):
    client, orchestrator = endpoint
    body, headers = _request(
        "tools/call",
        {"name": "read_value", "arguments": {"value": "hi"}},
        headers={"Mcp-Name": "read_value"},
    )
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["content"] == [{"type": "text", "text": "Rendered result"}]
    assert result["structuredContent"] == {"echo": "hi"}
    assert result["isError"] is False
    assert orchestrator.calls[0]["claims"].get("_raw_token") is None

    orchestrator.tool_permissions.allowed = False
    for _ in range(10):
        refused = client.post("/mcp", json=body, headers=headers)
        assert refused.json()["result"]["isError"] is True
    assert len(orchestrator.calls) == 1
    assert orchestrator.work_admission.inspect_admission_class(
        AdmissionClass.MCP
    ).active_count == 0


def test_raising_tool_is_a_redacted_completed_tool_error(endpoint):
    client, orchestrator = endpoint
    orchestrator.execute_mcp_tool = AsyncMock(
        side_effect=RuntimeError("patient-secret-must-not-escape")
    )
    body, headers = _request(
        "tools/call",
        {"name": "read_value", "arguments": {"value": "hi"}},
        headers={"Mcp-Name": "read_value"},
    )
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["result"] == {
        "resultType": "complete",
        "content": [{"type": "text", "text": "Tool execution failed"}],
        "structuredContent": {},
        "isError": True,
        "_meta": {
            "io.modelcontextprotocol/serverInfo": {
                "name": "AstralDeep",
                "version": "1.0.0",
            }
        },
    }
    assert "patient-secret-must-not-escape" not in response.text


def test_tracked_operation_stays_an_astral_result_not_an_mcp_task(endpoint):
    client, orchestrator = endpoint
    orchestrator.execute_mcp_tool = AsyncMock(
        return_value=MCPResponse(
            result={"operation_id": "job-123", "status": "working"},
            ui_components=[],
        )
    )
    body, headers = _request(
        "tools/call",
        {"name": "read_value", "arguments": {}},
        headers={"Mcp-Name": "read_value"},
    )
    result = client.post("/mcp", json=body, headers=headers).json()["result"]
    assert result["resultType"] == "complete"
    assert result["structuredContent"] == {
        "operation_id": "job-123",
        "status": "working",
    }
    assert "task" not in repr(result).lower()


def test_notification_empty_subscription_and_removed_methods(endpoint):
    client, _ = endpoint
    body, headers = _request("subscriptions/listen")
    listed = client.post("/mcp", json=body, headers=headers)
    assert listed.json()["result"]["notificationTypes"] == []
    body.pop("id")
    notified = client.post("/mcp", json=body, headers=headers)
    assert notified.status_code == 202
    assert not notified.content
    assert client.get("/mcp").status_code == 405
    assert client.delete("/mcp").status_code == 405


@pytest.mark.parametrize(
    ("mutate", "expected_status", "expected_code"),
    [
        (lambda body, headers: headers.pop("Mcp-Method"), 400, -32020),
        (lambda body, headers: headers.__setitem__("Mcp-Method", "tools/list"), 400, -32020),
        (lambda body, headers: headers.__setitem__("MCP-Protocol-Version", "2025-11-25"), 400, -32020),
        (lambda body, headers: body["params"]["_meta"].pop("io.modelcontextprotocol/clientCapabilities"), 400, -32602),
    ],
)
def test_mirror_and_required_metadata_refusals(endpoint, mutate, expected_status, expected_code):
    client, _ = endpoint
    body, headers = _request("server/discover")
    mutate(body, headers)
    response = client.post("/mcp", content=json.dumps(body), headers=headers)
    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


def test_unsupported_version_and_missing_capability(endpoint):
    client, _ = endpoint
    body, headers = _request("server/discover")
    body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = "2099-01-01"
    headers["MCP-Protocol-Version"] = "2099-01-01"
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": -32022,
        "message": "Unsupported MCP protocol version",
        "data": {"supported": ["2026-07-28"]},
    }

    body, headers = _request("server/discover", {"requiredCapabilities": ["sampling"]})
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32021
    assert response.json()["error"]["data"] == {"missing": ["sampling"]}


def test_auth_challenges_reject_web_cookie_and_query_credentials(endpoint):
    client, _ = endpoint
    body, headers = _request("server/discover", token="dev-token")
    responses = [client.post("/mcp", json=body, headers=headers) for _ in range(20)]
    assert all(response.status_code == 401 for response in responses)
    response = responses[-1]
    challenge = response.headers["www-authenticate"]
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert 'scope="mcp:discover"' in challenge
    assert 'error="invalid_token"' in challenge

    headers.pop("Authorization")
    client.cookies.set("session", "secret")
    cookie_only = client.post("/mcp", json=body, headers=headers)
    assert cookie_only.status_code == 401
    client.cookies.clear()
    query = client.post("/mcp?access_token=secret", json=body, headers=headers)
    assert query.status_code == 401


def test_origin_name_body_and_unknown_method_refusals(endpoint):
    client, _ = endpoint
    body, headers = _request("server/discover", headers={"Origin": "https://evil.test"})
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 403
    assert "access-control-allow-credentials" not in response.headers

    body, headers = _request(
        "tools/call",
        {"name": "café", "arguments": {}},
        headers={"Mcp-Name": "=?base64?Y2Fm6Q==?="},
    )
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020

    body, headers = _request("future/method")
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32601

    body, headers = _request("server/discover")
    oversized = " " * (1024 * 1024 + 1)
    response = client.post("/mcp", content=oversized, headers=headers)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == -32600


def test_flag_off_app_has_no_surface(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    app = FastAPI()
    client = TestClient(app)
    assert client.post("/mcp").status_code == 404
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 404


def test_metrics_are_low_cardinality_and_payload_free(endpoint):
    client, orchestrator = endpoint
    body, headers = _request("tools/call", {"name": "read_value", "arguments": {"value": "patient-secret"}}, headers={"Mcp-Name": "read_value"})
    headers["Authorization"] = "Bearer dev-mcp-token"
    assert client.post("/mcp", json=body, headers=headers).status_code == 200
    snapshot = orchestrator.runtime_observability.snapshot()
    serialized = repr(snapshot)
    assert "patient-secret" not in serialized
    assert "dev-mcp-token" not in serialized
    assert all(
        set(sample.labels).issubset(
            {"deployment_instance", "operation_kind", "phase", "result_code"}
        )
        for sample in snapshot
    )


@pytest.mark.asyncio
async def test_cancellation_releases_mcp_admission_slot(endpoint):
    _, orchestrator = endpoint
    with pytest.raises(asyncio.CancelledError):
        async with _admitted_request(
            orchestrator,
            user_id="test_user",
            method="tools/call",
        ):
            raise asyncio.CancelledError
    status = orchestrator.work_admission.inspect_admission_class(AdmissionClass.MCP)
    assert status.active_count == 0
    assert status.queued_count == 0


@pytest.mark.asyncio
async def test_disconnect_cancels_inflight_tool_and_releases_admission(endpoint):
    _, orchestrator = endpoint
    cancelled = asyncio.Event()

    class _DisconnectedRequest:
        async def is_disconnected(self):
            return True

    async def slow_tool():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(asyncio.CancelledError):
        async with _admitted_request(
            orchestrator,
            user_id="test_user",
            method="tools/call",
        ):
            await _wait_for_disconnect(_DisconnectedRequest(), slow_tool())

    assert cancelled.is_set()
    status = orchestrator.work_admission.inspect_admission_class(AdmissionClass.MCP)
    assert status.active_count == 0
    assert status.queued_count == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain-name", "plain-name"),
        ("=?base64?Y2Fmw6k=?=", "café"),
    ],
)
def test_name_header_decoding_accepts_plain_ascii_and_utf8_sentinel(value, expected):
    assert _decode_name_header(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "=?base64?!!!?=",
        "=?base64??=",
        " leading",
        "trailing ",
        "café",
        "=?base64?not-actually-encoded",
        "looks-like?=",
    ],
)
def test_name_header_decoding_rejects_ambiguous_or_unsafe_values(value):
    with pytest.raises(ValueError):
        _decode_name_header(value)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"jsonrpc":"1.0","method":"x"}',
        b'{"jsonrpc":"2.0","result":{},"method":"x"}',
        b'{"jsonrpc":"2.0","method":3}',
        b'{"jsonrpc":"2.0","method":"x","id":true}',
        b'{"jsonrpc":"2.0","method":"x","id":[]}',
        b'{"jsonrpc":"2.0","method":"x","params":[]}',
    ],
)
def test_json_rpc_parser_rejects_malformed_client_envelopes(payload):
    with pytest.raises(ValueError):
        _parse_request(payload)


def test_json_rpc_parser_marks_notifications_and_preserves_null_id():
    payload, request_id, notification = _parse_request(
        b'{"jsonrpc":"2.0","method":"ping"}'
    )
    assert payload["method"] == "ping"
    assert request_id is None and notification is True
    _, request_id, notification = _parse_request(
        b'{"jsonrpc":"2.0","method":"ping","id":null}'
    )
    assert request_id is None and notification is False


def test_tool_result_covers_error_scalar_and_json_fallbacks():
    error = _tool_result(MCPResponse(error={"message": "x" * 70_000}))
    assert error["isError"] is True
    assert len(error["content"][0]["text"]) == 65_536

    scalar = _tool_result(MCPResponse(result=7, ui_components=[]))
    assert scalar["structuredContent"] == {"value": 7}
    assert scalar["content"] == [{"type": "text", "text": '{"value": 7}'}]
    assert _structured(None) == {}
    mapping = {"ok": True}
    assert _structured(mapping) is mapping


def test_cors_preflight_and_same_origin_response_never_allow_credentials(endpoint):
    client, _ = endpoint
    preflight = client.options(
        "/mcp",
        headers={
            "Origin": BASE,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == BASE
    assert "access-control-allow-credentials" not in preflight.headers

    body, headers = _request("server/discover", headers={"Origin": BASE})
    response = client.post("/mcp", json=body, headers=headers)
    assert response.headers["access-control-allow-origin"] == BASE
    assert "access-control-allow-credentials" not in response.headers

    metadata = client.get(
        "/.well-known/oauth-protected-resource/mcp",
        headers={"Origin": BASE},
    )
    assert metadata.headers["access-control-allow-origin"] == BASE


def test_non_mcp_routes_pass_through_cors_middleware(endpoint):
    client, _ = endpoint
    assert client.get("/unrelated").status_code == 404


def test_endpoint_rejects_media_type_and_malformed_json(endpoint):
    client, _ = endpoint
    body, headers = _request("server/discover")
    headers["Content-Type"] = "text/plain"
    assert client.post("/mcp", content=json.dumps(body), headers=headers).status_code == 415

    headers["Content-Type"] = "application/json"
    response = client.post("/mcp", content="{", headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body, headers: body.__setitem__("params", []),
        lambda body, headers: body["params"].__setitem__(
            "_meta", {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
        ),
        lambda body, headers: body["params"]["_meta"].__setitem__(
            "io.modelcontextprotocol/clientCapabilities", []
        ),
        lambda body, headers: body["params"].__setitem__("requiredCapabilities", "x"),
    ],
)
def test_endpoint_rejects_additional_invalid_metadata_shapes(endpoint, mutate):
    client, _ = endpoint
    body, headers = _request("server/discover")
    mutate(body, headers)
    response = client.post("/mcp", content=json.dumps(body), headers=headers)
    assert response.status_code == 400


@pytest.mark.parametrize(
    "params",
    [
        {"arguments": {}},
        {"name": "read_value", "arguments": []},
    ],
)
def test_tool_call_requires_valid_name_and_argument_object(endpoint, params):
    client, _ = endpoint
    body, headers = _request(
        "tools/call",
        params,
        headers={"Mcp-Name": "read_value"},
    )
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


def test_tool_call_requires_matching_name_header(endpoint):
    client, _ = endpoint
    body, headers = _request("tools/call", {"name": "read_value", "arguments": {}})
    assert client.post("/mcp", json=body, headers=headers).status_code == 400
    headers["Mcp-Name"] = "different"
    assert client.post("/mcp", json=body, headers=headers).status_code == 400


def test_capacity_refusal_is_retryable_transport_response(endpoint, monkeypatch):
    client, orchestrator = endpoint
    orchestrator._claim_personal_agent_operation = AsyncMock(return_value=None)
    body, headers = _request("server/discover")
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["error"]["message"] == "MCP request capacity is exhausted"


def test_missing_subject_and_internal_failure_are_redacted(endpoint, monkeypatch):
    import orchestrator.mcp_server_endpoint as endpoint_module

    client, orchestrator = endpoint

    async def no_subject(**_kwargs):
        return {"scope": "mcp:discover"}

    monkeypatch.setattr(endpoint_module, "authorize_mcp_request", no_subject)
    body, headers = _request("server/discover")
    assert client.post("/mcp", json=body, headers=headers).status_code == 401

    async def authorized(**_kwargs):
        return {"sub": "u1", "scope": "mcp:discover"}

    monkeypatch.setattr(endpoint_module, "authorize_mcp_request", authorized)
    orchestrator._claim_personal_agent_operation = AsyncMock(
        side_effect=RuntimeError("patient-secret")
    )
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 500
    assert "patient-secret" not in response.text


@pytest.mark.asyncio
async def test_wait_for_disconnect_returns_completed_operation():
    class _ConnectedRequest:
        async def is_disconnected(self):
            return False

    async def complete():
        return "done"

    assert await _wait_for_disconnect(_ConnectedRequest(), complete()) == "done"


@pytest.mark.asyncio
async def test_admitted_request_terminalizes_failures(endpoint):
    _, orchestrator = endpoint
    with pytest.raises(RuntimeError, match="boom"):
        async with _admitted_request(
            orchestrator,
            user_id="test_user",
            method="server/discover",
        ):
            raise RuntimeError("boom")
    status = orchestrator.work_admission.inspect_admission_class(AdmissionClass.MCP)
    assert status.active_count == 0


def test_metric_without_observer_and_origin_header_cleanup():
    _record_metric(object(), "requested", phase="discover", result_code="received")
    response = Response(
        headers={
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Origin": "https://wrong.test",
        }
    )
    cleaned = _with_origin(response, BASE, BASE)
    assert cleaned.headers["access-control-allow-origin"] == BASE
    assert "access-control-allow-credentials" not in cleaned.headers
