"""Checks the verified Cresco control dialect and rejects unavailable effect transports.
Literal request/reply records follow the pinned Java handler and Python caller sources;
loopback exchanges qualify compatibility, never a deployed fabric.
"""

import pytest

from agents.cresco.mcp_server import MCPServer
from agents.cresco.wsapi_client import FabricFault, build_request, parse_frame, response_matches
from shared.protocol import MCPRequest


@pytest.fixture
def server(bridge_env):
    return MCPServer()


def test_controller_request_uses_string_valued_upstream_parameters():
    assert build_request("listagents", {"region": "r1"}, message_id="rpc-1") == {
        "message_info": {"message_type": "global_controller_msgevent",
                         "message_event_type": "EXEC", "is_rpc": "true"},
        "message_payload": {"action": "listagents", "action_region": "r1", "client_rpc_id": "rpc-1"},
    }


def test_agent_request_uses_config_route_without_invented_target_payload():
    assert build_request("agentinfo", {"region_agent": "r1_a1"}, message_id="rpc-2") == {
        "message_info": {"message_type": "global_agent_msgevent",
                         "message_event_type": "CONFIG", "is_rpc": "true",
                         "dst_region": "r1", "dst_agent": "a1"},
        "message_payload": {"action": "getagentinfo", "client_rpc_id": "rpc-2"},
    }


def test_sysinfo_uses_verified_controller_resourceinfo_query():
    request = build_request("getsysinfo", {"region_agent": "r1_a1"}, message_id="rpc-3")
    assert request["message_info"]["message_type"] == "global_controller_msgevent"
    assert request["message_payload"] == {"action": "resourceinfo", "action_region": "r1",
                                          "action_agent": "a1", "client_rpc_id": "rpc-3"}


def test_plain_response_params_correlate_without_response_envelope():
    frame = parse_frame('{"action":"getagentinfo","client_rpc_id":"rpc-2","agent-data":"cresco-data"}')
    assert response_matches(frame, "rpc-2")
    assert not response_matches(frame, "other")
    assert not response_matches({"work_id": "rpc-2"}, "rpc-2")


def test_unverified_message_actions_are_refused_before_connection():
    with pytest.raises(FabricFault) as refused:
        build_request("submitwork", {"region_agent": "r1_a1"})
    assert refused.value.state == "unavailable"
    assert refused.value.after_send is False


def test_generic_work_never_claims_completion_or_dials(server, staged_fabric, captured_audit):
    response = server.process_request(MCPRequest(request_id="unsupported", method="tools/call", params={
        "name": "cresco_dispatch_work", "arguments": {"target": "r1_a1", "work_kind": "status", "user_id": "owner"},
    }))
    assert response.error and response.error["data"]["detail"] == "dispatch_protocol_unavailable"
    assert staged_fabric.requests == []
    assert captured_audit[-1]["outcome"] == "failure"


def test_upstream_topology_identifiers_render_as_targets():
    server = MCPServer()
    regions = server._completed_components("cresco_list_regions", {"regions": [{"name": "r1", "type": "local", "agents": "1"}]})
    agents = server._completed_components("cresco_list_agents", {"agents": [{"region_id": "r1", "agent_id": "a1"}]})
    assert regions[0]["rows"] == [["r1"]]
    assert agents[0]["rows"] == [["r1_a1"]]


def test_fabric_echoed_secret_keys_are_scrubbed_from_results_and_ui(server, staged_fabric):
    import json

    from agents.cresco.wsapi_client import encode_bulk
    from .conftest import SERVICE_KEY

    staged_fabric.bulk_override = encode_bulk({"agentresourceinfo": [{"perf": json.dumps({SERVICE_KEY: "sensitive-label"})}]})
    response = server.process_request(MCPRequest(request_id="secret-key", method="tools/call", params={
        "name": "cresco_get_sysinfo", "arguments": {"target": "r1_a1", "user_id": "owner"},
    }))
    assert response.error is None
    assert SERVICE_KEY not in json.dumps(response.result)
    assert SERVICE_KEY not in json.dumps(response.ui_components)


@pytest.mark.parametrize("path", ["/api/dataplane", "/api/logstreamer", "/other", "/api/apisocket?secret=hidden"])
def test_only_control_endpoint_is_admitted_before_credentials(path):
    from agents.cresco.wsapi_client import FabricConfig, WsapiClient

    with pytest.raises(FabricFault) as refused:
        WsapiClient(FabricConfig(wsapi_url="wss://127.0.0.1:8282" + path,
            service_key="synthetic-secret", allow_private_host="127.0.0.1")).validate_egress()
    assert refused.value.state == "protocol"


@pytest.mark.parametrize("failure", ["absent", "exception", "queued"])
def test_audit_must_persist_before_any_fabric_read(server, staged_fabric, monkeypatch, failure):
    from types import SimpleNamespace

    from audit import recorder

    def failed_insert(event):
        if failure == "exception":
            raise RuntimeError("synthetic audit storage failure")
        return None

    monkeypatch.setattr(recorder, "get_recorder", lambda: None if failure == "absent" else SimpleNamespace(record_blocking=failed_insert))
    response = server.process_request(MCPRequest(request_id="audit-denied", method="tools/call", params={
        "name": "cresco_list_regions", "arguments": {"user_id": "owner"},
    }))
    assert response.error and response.error["data"]["detail"] == "audit_unavailable"
    assert staged_fabric.requests == []


@pytest.mark.parametrize("url", ["wss://[invalid", "wss://host:invalid", "wss://host:99999", "wss://", "wss://host#fragment", "wss://user:secret@host"])
def test_malformed_control_urls_fail_as_typed_faults_without_connecting(url):
    from agents.cresco.wsapi_client import FabricConfig, WsapiClient

    with pytest.raises(FabricFault) as refused:
        WsapiClient(FabricConfig(wsapi_url=url, service_key="synthetic-secret")).validate_egress()
    assert refused.value.state in ("protocol", "tls")


def test_audit_disposition_failure_returns_no_result_or_success(server, staged_fabric, monkeypatch):
    from types import SimpleNamespace

    from audit import recorder

    rows = []

    def persist_only_intent(event):
        rows.append(event)
        return object() if event.outcome == "in_progress" else None

    monkeypatch.setattr(recorder, "get_recorder", lambda: SimpleNamespace(record_blocking=persist_only_intent))
    response = server.process_request(MCPRequest(request_id="audit-uncertain", method="tools/call", params={
        "name": "cresco_list_regions", "arguments": {"user_id": "owner"},
    }))
    assert response.error and response.error["data"]["detail"] == "audit_unavailable"
    assert response.result is None
    assert response.error["retryable"] is False
    assert len(staged_fabric.requests) == 1
    assert rows[0].outcome == "in_progress"


@pytest.mark.asyncio
async def test_audit_is_durable_when_invoked_from_a_running_event_loop(server, staged_fabric, captured_audit):
    response = server.process_request(MCPRequest(request_id="audit-loop", method="tools/call", params={
        "name": "cresco_list_regions", "arguments": {"user_id": "owner"},
    }))
    assert response.error is None
    assert [row["outcome"] for row in captured_audit] == ["in_progress", "success"]


def test_real_audit_chain_commits_intent_before_dial_and_disposition_before_result(server, staged_fabric, monkeypatch, tmp_path):
    import uuid

    from audit import recorder
    from audit.recorder import Recorder
    from audit.repository import AuditRepository
    from agents.cresco.wsapi_client import WsapiClient
    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    owner = "cresco-audit-" + uuid.uuid4().hex
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-cresco-audit-secret")
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "synthetic-cresco-key")
    with isolated_plane_runtime("cresco_audit") as runtime:
        repo = AuditRepository(plane_runtime=runtime, plane_repositories=runtime.repositories)
        audit_recorder = Recorder(repo, retry_queue=tmp_path / "retry.jsonl")
        monkeypatch.setattr(recorder, "get_recorder", lambda: audit_recorder)
        original_dial = WsapiClient.dial

        def inspect_intent_before_dial(client, **kwargs):
            rows, _ = repo.list_for_user(owner)
            assert len(rows) == 1 and rows[0].outcome == "in_progress"
            assert repo.verify_chain(owner) is None
            return original_dial(client, **kwargs)

        monkeypatch.setattr(WsapiClient, "dial", inspect_intent_before_dial)
        response = server.process_request(MCPRequest(request_id="durable-audit", method="tools/call", params={
            "name": "cresco_agent_info", "arguments": {"target": "r1_a1", "user_id": owner},
        }))
        assert response.error is None
        rows, _ = repo.list_for_user(owner)
        assert len(rows) == 2 and {row.outcome for row in rows} == {"in_progress", "success"}
        assert all(row.inputs_meta["cresco_target"] == "r1_a1" for row in rows)
        assert repo.verify_chain(owner) is None
        assert not (tmp_path / "retry.jsonl").exists()


def test_unavailable_operation_feedback_does_not_suggest_configuring_an_invented_transport(server):
    from orchestrator.tool_feedback import tool_failure_message

    response = server.process_request(MCPRequest(request_id="unavailable-feedback", method="tools/call", params={
        "name": "cresco_dispatch_work", "arguments": {"target": "r1_a1", "work_kind": "status", "user_id": "owner"},
    }))
    assert response.error["code"] == "CRESCO_OPERATION_UNAVAILABLE"
    assert "unavailable" in tool_failure_message("cresco_dispatch_work", response.error)


def test_failed_audit_feedback_distinguishes_withheld_results(server, monkeypatch):
    from audit import recorder
    from orchestrator.tool_feedback import tool_failure_message

    monkeypatch.setattr(recorder, "get_recorder", lambda: None)
    response = server.process_request(MCPRequest(request_id="audit-feedback", method="tools/call", params={
        "name": "cresco_list_regions", "arguments": {"user_id": "owner"},
    }))
    assert response.error["code"] == "CRESCO_AUDIT_UNAVAILABLE"
    assert "audit" in tool_failure_message("cresco_list_regions", response.error)
