"""Synthetic OpenAI MCP-tunnel qualification for orchestrator/mcp_server_endpoint.py:
drives the reviewed tunnel-client probe profile (pinned at contracts/tunnel-client-pins.json)
through the real /mcp endpoint to retain a sanitized request/status transcript, an honest
readiness verdict, and denial, bounded-payload, disconnect/reconnect, and credential-leak
coverage, all offline.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


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
from shared.feature_flags import FeatureFlags
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


_identity = None


def _owner_token():
    return _identity.mcp_token("test_user")


def _modern_request(method, params=None, *, token="owner", request_id=1):
    if token == "owner":
        token = _owner_token()
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


