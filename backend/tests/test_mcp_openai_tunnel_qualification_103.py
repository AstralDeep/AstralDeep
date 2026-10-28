"""Synthetic OpenAI MCP-tunnel qualification for orchestrator/mcp_server_endpoint.py:
drives the reviewed tunnel-client probe profile (pinned at contracts/tunnel-client-pins.json)
through the real /mcp endpoint to retain a sanitized request/status transcript, an honest
readiness verdict, and denial, bounded-payload, disconnect/reconnect, and credential-leak
coverage, all offline.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import orchestrator.mcp_authz as mcp_authz_module
from orchestrator.mcp_server_endpoint import (
    MCP_BODY_LIMIT,
    _admitted_request,
    _wait_for_disconnect,
    install_mcp_server,
)
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionCoordinator,
)
from shared.feature_flags import FeatureFlags, flags
from shared.protocol import AgentCard, AgentSkill, MCPResponse

TUNNEL_CLIENT_PIN = "fda241e2343ddd49dd63a7da3e1afab2a6e91722"
GO_MCP_SDK_MODULE = "github.com/modelcontextprotocol/go-sdk"
PIN_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "specs"
    / "103-openai-mcp-tunnel-qualification"
    / "contracts"
    / "tunnel-client-pins.json"
)
BASE = "http://mcp.test"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {
        "name": "openai-tunnel-client",
        "version": TUNNEL_CLIENT_PIN[:12],
    },
}
_RECORDED_REQUEST_HEADERS = frozenset(
    {"content-type", "mcp-protocol-version", "mcp-method", "mcp-name"}
)


def _modern_request(method, params=None, *, token="dev-mcp-token", request_id=1):
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**(params or {}), "_meta": dict(MODERN_META)},
    }
    headers = {
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return body, headers


def _legacy_initialize(*, token=None):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "openai-tunnel-client", "version": TUNNEL_CLIENT_PIN[:12]},
        },
    }
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return body, headers


class _Transcript:
    def __init__(self):
        self.entries = []

    def record(self, step, path, headers, status):
        self.entries.append({
            "step": step,
            "request": {
                "path": path,
                "headers": sorted(
                    name for name in headers if name.lower() in _RECORDED_REQUEST_HEADERS
                ),
            },
            "status": status,
        })

    def serialized(self):
        return json.dumps(self.entries, sort_keys=True)


class TunnelProbeProfile:
    def __init__(self, client):
        self._client = client
        self.transcript = _Transcript()

    def probe_metadata(self):
        response = self._client.get(METADATA_PATH)
        self.transcript.record("metadata", METADATA_PATH, {}, response.status_code)
        return response

    def probe_modern_discovery(self, *, token=None):
        body, headers = _modern_request("server/discover", token=token)
        response = self._client.post("/mcp", json=body, headers=headers)
        self.transcript.record(
            "modern_discovery", "/mcp", headers, response.status_code
        )
        return response

    def probe_legacy_initialize(self, *, token=None):
        body, headers = _legacy_initialize(token=token)
        response = self._client.post("/mcp", content=json.dumps(body), headers=headers)
        self.transcript.record(
            "legacy_initialize", "/mcp", headers, response.status_code
        )
        return response

    def reviewed_startup_fallback(self, *, fallback_token=None):
        self.probe_metadata()
        modern = self.probe_modern_discovery()
        if modern.status_code == 200:
            return True
        self.probe_legacy_initialize()
        self.probe_legacy_initialize(token=fallback_token)
        return any(
            entry["request"]["path"] == "/mcp" and entry["status"] // 100 == 2
            for entry in self.transcript.entries
        )


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

    def list_disabled_agents(self, _user_id):
        return ()


class _Orchestrator:
    def __init__(self):
        self.history = SimpleNamespace(db=SimpleNamespace())
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
            AdmissionClassConfig(AdmissionClass.GLOBAL, None, 2, 0, None, "test"),
            AdmissionClassConfig(
                AdmissionClass.MCP, AdmissionClass.GLOBAL, 1, 2, 1000, "test"
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
def tunnel_endpoint(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.test/realms/astral")
    monkeypatch.setitem(flags._flags, "mcp_server", False)
    orchestrator = _Orchestrator()
    app = FastAPI()
    install_mcp_server(app, orchestrator, public_base_url=BASE)
    return TestClient(app), orchestrator


@pytest.fixture
def principals(tunnel_endpoint, monkeypatch):
    client, orchestrator = tunnel_endpoint
    claims_by_token = {
        "alice-token": {
            "sub": "alice",
            "iss": "https://idp.test/realms/astral",
            "aud": "astral-mcp",
            "scope": "mcp:discover mcp:tools:read mcp:tools:invoke",
            "realm_access": {"roles": ["user"]},
        },
        "bob-token": {
            "sub": "bob",
            "iss": "https://idp.test/realms/astral",
            "aud": "astral-mcp",
            "scope": "mcp:discover mcp:tools:read mcp:tools:invoke",
            "realm_access": {"roles": ["user"]},
        },
    }

    async def decode_deterministic_token(token):
        if token in claims_by_token:
            return dict(claims_by_token[token])
        raise ValueError("token rejected by the issuer")

    monkeypatch.setattr(
        mcp_authz_module, "decode_mcp_token", decode_deterministic_token
    )
    return client, orchestrator


def test_tunnel_client_pins_manifest_matches_reviewed_sources():
    manifest = json.loads(PIN_MANIFEST_PATH.read_text())
    assert manifest["tunnel_client"]["pin"] == TUNNEL_CLIENT_PIN
    assert manifest["tunnel_client"]["pin_type"] == "git-commit"
    assert manifest["tunnel_client"]["evidence_class"] == "source-review"
    assert manifest["tunnel_client"]["execution_status"].startswith("not executed")
    assert manifest["go_mcp_sdk"]["module"] == GO_MCP_SDK_MODULE
    assert manifest["go_mcp_sdk"]["pin_type"] == "transitive-go-mod-pin"
    assert manifest["go_mcp_sdk"]["binding"].endswith(TUNNEL_CLIENT_PIN)
    assert manifest["go_mcp_sdk"]["version"] == "v1.7.0"
    assert manifest["go_mcp_sdk"]["sum"] == "h1:yqjY2dsbKAC0LSuWZVBMrHgiG8ukXv6NRo0JiALay44="
    assert manifest["go_mcp_sdk"]["go_mod_sum"] == "h1:dL7u98E/zjJTGzEq+j30jQ8K2k1mb6LeAH4inEcSGts="
    sources = manifest["go_mcp_sdk"]["source_files"]
    assert sources["go.mod"]["url"].endswith(f"/{TUNNEL_CLIENT_PIN}/go.mod")
    assert sources["go.mod"]["sha256"] == "4e4824c574831a0a0b5b083a99a240e03bdcee6907212d6be18562a678cf3bfa"
    assert sources["go.sum"]["url"].endswith(f"/{TUNNEL_CLIENT_PIN}/go.sum")
    assert sources["go.sum"]["sha256"] == "8c6fe6af4c67b4c7f7b4cfb9c7c55f59a28f8c61201a2fc83e596416f0c03af3"
    assert manifest["source_retrieval"]["retrieval_date"] == "2026-10-28"
    assert manifest["astral_flag_posture"]["flag"] == "FF_MCP_SERVER"
    assert manifest["astral_flag_posture"]["default"] is False
    assert manifest["live_qualification"]["gating"] is False


def test_mcp_flag_stays_off_by_default(monkeypatch):
    monkeypatch.delenv("FF_MCP_SERVER", raising=False)
    assert FeatureFlags().is_enabled("mcp_server") is False


def test_reviewed_probe_sequence_leaves_legacy_fallback_not_ready(tunnel_endpoint):
    client, orchestrator = tunnel_endpoint
    profile = TunnelProbeProfile(client)

    ready = profile.reviewed_startup_fallback(fallback_token="dev-mcp-token")

    assert [entry["step"] for entry in profile.transcript.entries] == [
        "metadata",
        "modern_discovery",
        "legacy_initialize",
        "legacy_initialize",
    ]
    assert [entry["status"] for entry in profile.transcript.entries] == [200, 401, 400, 400]
    assert all(entry["status"] // 100 != 5 for entry in profile.transcript.entries)
    assert ready is False

    metadata = client.get(METADATA_PATH)
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == f"{BASE}/mcp"
    assert metadata.json()["authorization_servers"] == ["https://idp.test/realms/astral"]

    body, headers = _modern_request("server/discover", token=None)
    modern_unauthenticated = client.post("/mcp", json=body, headers=headers)
    assert modern_unauthenticated.status_code == 401
    challenge = modern_unauthenticated.headers["WWW-Authenticate"]
    assert f'resource_metadata="{BASE}{METADATA_PATH}"' in challenge
    assert 'scope="mcp:discover"' in challenge
    assert 'error="invalid_token"' in challenge
    assert modern_unauthenticated.json()["error"]["message"] == "MCP authorization failed"
    assert body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"

    assert orchestrator.calls == []
    assert "dev-mcp-token" not in profile.transcript.serialized()
    assert "Bearer" not in profile.transcript.serialized()
    assert "authorization" not in profile.transcript.serialized()


def test_legacy_initialize_is_refused_before_dispatch_with_or_without_credentials(
    tunnel_endpoint,
):
    client, orchestrator = tunnel_endpoint
    unauthenticated, unauthenticated_headers = _legacy_initialize()
    refused = client.post(
        "/mcp", content=json.dumps(unauthenticated), headers=unauthenticated_headers
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == -32602

    authenticated, authenticated_headers = _legacy_initialize(token="dev-mcp-token")
    still_refused = client.post(
        "/mcp", content=json.dumps(authenticated), headers=authenticated_headers
    )
    assert still_refused.status_code == 400
    assert still_refused.json()["error"]["code"] == -32602
    assert orchestrator.calls == []


def test_valid_owner_token_completes_modern_discovery(tunnel_endpoint):
    client, _ = tunnel_endpoint
    profile = TunnelProbeProfile(client)
    body, headers = _modern_request("server/discover")
    response = client.post("/mcp", json=body, headers=headers)
    profile.transcript.record("modern_discovery_authorized", "/mcp", headers, response.status_code)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == ["2026-07-28"]
    assert result["capabilities"] == {"tools": {}}
    assert result["cacheScope"] == "private"
    assert response.headers["MCP-Protocol-Version"] == "2026-07-28"


def test_expired_and_revoked_tokens_are_denied_with_challenge(principals):
    client, _ = principals
    for denied_token in ("expired-owner-token", "revoked-owner-token"):
        body, headers = _modern_request("server/discover", token=denied_token)
        response = client.post("/mcp", json=body, headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["message"] == "MCP authorization failed"
        challenge = response.headers["WWW-Authenticate"]
        assert f'resource_metadata="{BASE}{METADATA_PATH}"' in challenge
        assert 'error="invalid_token"' in challenge
        assert denied_token not in response.text
        assert "expired" not in response.text
        assert "revoked" not in response.text


def test_cross_owner_tool_projection_is_isolated(principals):
    client, orchestrator = principals
    orchestrator.tool_permissions.allowed = False
    orchestrator.tool_permissions.allowed_by_identity = {
        ("alice", "reader-1", "read_value"): True,
    }

    body, headers = _modern_request("tools/list", token="alice-token")
    alice_tools = client.post("/mcp", json=body, headers=headers)
    assert [tool["name"] for tool in alice_tools.json()["result"]["tools"]] == ["read_value"]

    body, headers = _modern_request("tools/list", token="bob-token")
    bob_tools = client.post("/mcp", json=body, headers=headers)
    assert bob_tools.json()["result"]["tools"] == []

    body, headers = _modern_request(
        "tools/call",
        {"name": "read_value", "arguments": {"value": "cross"}},
        token="bob-token",
        request_id=7,
    )
    headers["Mcp-Name"] = "read_value"
    denied_call = client.post("/mcp", json=body, headers=headers)
    assert denied_call.status_code == 200
    denied_result = denied_call.json()["result"]
    assert denied_result["isError"] is True
    assert denied_result["structuredContent"] == {}
    assert denied_result["content"][0]["text"] == "Tool is unavailable or no longer authorized"
    assert orchestrator.calls == []

    body, headers = _modern_request(
        "tools/call",
        {"name": "read_value", "arguments": {"value": "own"}},
        token="alice-token",
        request_id=8,
    )
    headers["Mcp-Name"] = "read_value"
    allowed_call = client.post("/mcp", json=body, headers=headers)
    assert allowed_call.status_code == 200
    assert allowed_call.json()["result"]["isError"] is False
    assert len(orchestrator.calls) == 1
    assert orchestrator.calls[0]["user_id"] == "alice"


def test_bounded_payload_and_error_refusals(tunnel_endpoint):
    client, orchestrator = tunnel_endpoint

    wrong_media = client.post(
        "/mcp",
        content=b"{}",
        headers={"Content-Type": "text/plain", "Authorization": "Bearer dev-mcp-token"},
    )
    assert wrong_media.status_code == 415

    oversized = client.post(
        "/mcp",
        content=b"0" * (MCP_BODY_LIMIT + 1),
        headers={"Content-Type": "application/json", "Authorization": "Bearer dev-mcp-token"},
    )
    assert oversized.status_code == 413
    assert "MCP request body exceeds 1 MiB" in oversized.json()["error"]["message"]

    malformed = client.post(
        "/mcp",
        content=b"{not json",
        headers={"Content-Type": "application/json", "Authorization": "Bearer dev-mcp-token"},
    )
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == -32600

    body, headers = _modern_request("ping")
    removed_method = client.post("/mcp", json=body, headers=headers)
    assert removed_method.status_code == 404
    assert removed_method.json()["error"]["code"] == -32601

    body, headers = _modern_request("subscriptions/listen", request_id=None)
    del body["id"]
    notified = client.post("/mcp", json=body, headers=headers)
    assert notified.status_code == 202
    assert notified.content == b""
    assert orchestrator.calls == []


@pytest.mark.asyncio
async def test_disconnect_cancels_without_replay_and_reconnect_recovers(tunnel_endpoint):
    client, orchestrator = tunnel_endpoint
    effects = []

    class _DisconnectedRequest:
        async def is_disconnected(self):
            return True

    async def inflight_tool():
        effects.append("started")
        await asyncio.Event().wait()

    with pytest.raises(asyncio.CancelledError):
        async with _admitted_request(
            orchestrator, user_id="test_user", method="tools/call"
        ):
            await _wait_for_disconnect(_DisconnectedRequest(), inflight_tool())

    assert effects == ["started"]
    assert orchestrator.calls == []
    status = orchestrator.work_admission.inspect_admission_class(AdmissionClass.MCP)
    assert status.active_count == 0
    assert status.queued_count == 0

    body, headers = _modern_request(
        "tools/call",
        {"name": "read_value", "arguments": {"value": "reconnected"}},
        request_id=9,
    )
    headers["Mcp-Name"] = "read_value"
    reconnect = client.post("/mcp", json=body, headers=headers)
    assert reconnect.status_code == 200
    assert reconnect.json()["result"]["isError"] is False
    assert len(orchestrator.calls) == 1
    assert orchestrator.calls[0]["arguments"] == {"value": "reconnected"}
    assert effects == ["started"]


def test_probe_transcript_never_carries_credentials_or_arguments(tunnel_endpoint):
    client, _ = tunnel_endpoint
    profile = TunnelProbeProfile(client)
    profile.probe_metadata()
    profile.probe_modern_discovery()
    profile.probe_legacy_initialize(token="dev-mcp-token")
    profile.probe_modern_discovery(token="dev-mcp-token")
    serialized = profile.transcript.serialized()
    assert "dev-mcp-token" not in serialized
    assert "Bearer" not in serialized
    assert "authorization" not in serialized
    assert all(
        set(entry["request"]["headers"]) <= {
            "Content-Type", "MCP-Protocol-Version", "Mcp-Method", "Mcp-Name",
        }
        for entry in profile.transcript.entries
    )


def test_modern_readiness_verdict_is_ready_for_conforming_clients(tunnel_endpoint):
    client, orchestrator = tunnel_endpoint
    profile = TunnelProbeProfile(client)
    authorized = profile.probe_modern_discovery(token="dev-mcp-token")
    assert authorized.status_code == 200
    modern_ready = any(
        entry["status"] // 100 == 2 for entry in profile.transcript.entries
    )
    assert modern_ready is True
    assert orchestrator.calls == []
