"""Feature 064 Phase A: additive MCP envelope and dialect regressions."""

from __future__ import annotations

import asyncio
import inspect
import json
from importlib import import_module
from types import SimpleNamespace

import pytest

from shared.feature_flags import FeatureFlags
from shared.local_transport import FencedTunnelSocket
from shared.protocol import (
    AgentCard,
    AgentSkill,
    MCP_PROTOCOL_VERSION,
    MCP_UNSUPPORTED_PROTOCOL_VERSION,
    MCPProtocolError,
    MCPRequest,
    MCPResponse,
    Message,
    ProtocolValidationError,
)


def test_mcp_response_ignores_unknown_additive_field() -> None:
    parsed = Message.from_json(
        json.dumps(
            {
                "type": "mcp_response",
                "request_id": "req-064",
                "result": {"ok": True},
                "result_type": "complete",
                "responder_info": {"name": "peer", "version": "1.0.0"},
                "future_field": {"safe": "to ignore"},
            }
        )
    )

    assert isinstance(parsed, MCPResponse)
    assert parsed.result == {"ok": True}
    assert parsed.result_type == "complete"
    assert not hasattr(parsed, "future_field")


def test_network_request_decoder_tolerates_unknown_field_repeatedly() -> None:
    frame = json.dumps(
        {
            "type": "mcp_request",
            "request_id": "req-064",
            "method": "tools/list",
            "params": {},
            "protocol_version": MCP_PROTOCOL_VERSION,
            "caller_capabilities": {},
            "caller_info": {"name": "orchestrator", "version": "1.0.0"},
            "next_revision_field": True,
        }
    )

    for _ in range(20):
        parsed = Message.from_json(frame)
        assert isinstance(parsed, MCPRequest)
        parsed.validate_protocol_metadata(allow_legacy=False)


@pytest.mark.asyncio
async def test_malformed_correlated_response_fails_future_promptly() -> None:
    # This exercises the swallowed-exception seam directly. Before feature
    # 064, the future remained pending and its caller waited 30 seconds.
    from orchestrator.orchestrator import Orchestrator

    pending = asyncio.get_running_loop().create_future()
    fake = SimpleNamespace(
        pending_requests={"req-064": pending},
        _response_is_from_dispatch_target=lambda _rid, _socket: True,
    )

    await Orchestrator.handle_agent_message(
        fake,
        object(),
        json.dumps(
            {
                "type": "mcp_response",
                "request_id": "req-064",
                "result_type": "",
            }
        ),
    )

    assert pending.done()
    with pytest.raises(ProtocolValidationError, match="malformed MCP response"):
        pending.result()


def test_agent_card_round_trip_derives_skill_fields_and_ignores_unknowns() -> None:
    card = AgentCard(
        name="Test",
        description="Test card",
        agent_id="test-1",
        skills=[
            AgentSkill(
                id="read",
                name="read",
                description="Read",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                scope="tools:read",
            )
        ],
    )
    payload = card.to_dict()
    payload["future_card_field"] = True
    payload["skills"][0]["future_skill_field"] = True

    restored = AgentCard.from_dict(payload)

    assert restored.to_dict()["skills"][0]["output_schema"] == {"type": "object"}
    assert not hasattr(restored, "future_card_field")
    assert not hasattr(restored.skills[0], "future_skill_field")


def test_declared_unsupported_version_is_refused_with_supported_list() -> None:
    request = MCPRequest(
        request_id="req-064",
        method="tools/list",
        protocol_version="2099-01-01",
        caller_capabilities={},
    )

    with pytest.raises(MCPProtocolError) as caught:
        request.validate_protocol_metadata(allow_legacy=False)

    assert caught.value.code == MCP_UNSUPPORTED_PROTOCOL_VERSION
    assert caught.value.data == {"supported": [MCP_PROTOCOL_VERSION]}


def test_modern_request_requires_capabilities_but_legacy_internal_frame_survives() -> None:
    MCPRequest(request_id="legacy", method="tools/list").validate_protocol_metadata(
        allow_legacy=True
    )
    with pytest.raises(MCPProtocolError, match="caller_capabilities"):
        MCPRequest(
            request_id="modern",
            method="tools/list",
            protocol_version=MCP_PROTOCOL_VERSION,
        ).validate_protocol_metadata(allow_legacy=False)


def test_response_cannot_mix_protocol_error_and_renderable_components() -> None:
    with pytest.raises(ProtocolValidationError, match="cannot carry an error"):
        MCPResponse(
            request_id="req-064",
            error={"message": "failed"},
            ui_components=[{"type": "Text", "text": "must not render"}],
        )

    with pytest.raises(ProtocolValidationError, match="error with a result"):
        MCPResponse(
            request_id="req-064-result",
            error={"message": "failed"},
            result={"value": 1},
        )


def test_mcp_server_flag_is_fail_closed_and_read_from_documented_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FF_MCP_SERVER", raising=False)
    assert FeatureFlags().is_enabled("mcp_server") is False
    monkeypatch.setenv("FF_MCP_SERVER", "true")
    assert FeatureFlags().is_enabled("mcp_server") is True


def test_protocol_error_serialization_includes_structured_data_only_when_present() -> None:
    assert MCPProtocolError(-1, "plain").to_error() == {
        "code": -1,
        "message": "plain",
        "retryable": False,
    }
    assert MCPProtocolError(-2, "detail", data={"supported": ["v1"]}).to_error()[
        "data"
    ] == {"supported": ["v1"]}


@pytest.mark.parametrize("payload", ["{", "[]"])
def test_message_parser_rejects_invalid_json_or_non_object(payload: str) -> None:
    with pytest.raises(ProtocolValidationError):
        Message.from_json(payload)


@pytest.mark.parametrize(
    "mcp_request",
    [
        MCPRequest(
            request_id="caps",
            method="tools/list",
            protocol_version=MCP_PROTOCOL_VERSION,
            caller_capabilities=[],
        ),
        MCPRequest(
            request_id="info",
            method="tools/list",
            protocol_version=MCP_PROTOCOL_VERSION,
            caller_capabilities={},
            caller_info="bad",
        ),
    ],
)
def test_modern_request_metadata_must_use_objects(mcp_request: MCPRequest) -> None:
    with pytest.raises(MCPProtocolError):
        mcp_request.validate_protocol_metadata(allow_legacy=False)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"responder_info": "bad"},
        {"error": "bad"},
        {"ui_components": "bad"},
    ],
)
def test_response_metadata_shapes_are_validated(kwargs: dict) -> None:
    with pytest.raises(ProtocolValidationError):
        MCPResponse(**kwargs)


def test_agent_card_parser_rejects_non_object_and_non_list_skills() -> None:
    with pytest.raises(ProtocolValidationError, match="must be an object"):
        AgentCard.from_dict([])
    with pytest.raises(ProtocolValidationError, match="skills must be a list"):
        AgentCard.from_dict({"name": "x", "skills": {}})


def test_non_network_request_paths_do_not_reparse_additive_request_fields() -> None:
    from orchestrator.orchestrator import Orchestrator

    in_process = inspect.getsource(Orchestrator._execute_in_process)
    tunnel = inspect.getsource(FencedTunnelSocket.send_fenced)
    assert "Message.from_json" not in in_process
    assert "agent.handle_mcp_request(loopback, request)" in in_process
    assert "Message.from_json" not in tunnel
    assert '"frame": frame' in tunnel


def test_protocol_metadata_never_enters_splat_tool_arguments() -> None:
    modules = (
        "dice_roller",
        "general",
        "journal_review",
        "medical",
        "ml_services",
        "remote_compute",
        "summarizer",
        "weather",
        "web_research",
    )
    for module_name in modules:
        server = import_module(f"agents.{module_name}.mcp_server").MCPServer()
        captured = {}

        def probe(**kwargs):
            captured.update(kwargs)
            return {"ok": True}

        server.tools = {
            "probe": {
                "function": probe,
                "description": "probe",
                "input_schema": {"type": "object"},
            }
        }
        response = server.process_request(
            MCPRequest(
                request_id=f"probe-{module_name}",
                method="tools/call",
                params={"name": "probe", "arguments": {"value": module_name}},
                protocol_version=MCP_PROTOCOL_VERSION,
                caller_capabilities={"future": True},
                caller_info={"name": "test", "version": "1"},
            )
        )
        assert response.error is None
        assert captured == {"value": module_name}
