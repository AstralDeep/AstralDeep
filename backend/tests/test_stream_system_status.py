"""Tests for legacy streamable system-status tools in orchestrator/orchestrator.py:
_build_agent_card injects a default streaming_kind for dict-form tools while
preserving explicit push metadata, and source params tag only the top-level
component.
"""

import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.base_agent import BaseA2AAgent
from shared.protocol import validate_streaming_metadata


class _StubMCPServer:
    def __init__(self, tools):
        self.tools = tools

    def process_request(self, request):  # pragma: no cover
        raise NotImplementedError


class _StubAgent(BaseA2AAgent):
    def __init__(self, tools):
        self.mcp_server = _StubMCPServer(tools)
        self.service_name = "stub"
        self.agent_id = "stub-1"
        self.description = "stub agent for tests"
        self.skill_tags = ["test"]
        self.host = "localhost"
        self.port = 9999
        self.card_metadata = {}
        self._public_key_jwk = {"kty": "EC", "crv": "P-256", "x": "", "y": ""}


def _tool(fn, *, streamable=None, metadata=None, scope="tools:system"):
    entry = {"function": fn, "description": "desc", "scope": scope,
             "input_schema": {"type": "object", "properties": {}}}
    if streamable is not None:
        entry["streamable"] = streamable
    if metadata is not None:
        entry["metadata"] = metadata
    return entry


def _noop():  # pragma: no cover
    return {}


class TestLegacyStreamableRegistration:
    def test_legacy_poll_tool_passes_validator(self):
        agent = _StubAgent({
            "get_system_status": _tool(
                _noop,
                streamable={"default_interval": 2, "min_interval": 1, "max_interval": 30},
            ),
        })
        card = agent._build_agent_card()
        skill = next(s for s in card.skills if s.id == "get_system_status")
        assert skill.metadata.get("streaming_kind") == "poll"
        validate_streaming_metadata(skill.metadata)

    def test_push_tool_metadata_unchanged(self):
        agent = _StubAgent({
            "live_system_metrics": _tool(
                _noop,
                metadata={
                    "streamable": True,
                    "streaming_kind": "push",
                    "max_fps": 2,
                    "min_fps": 1,
                    "max_chunk_bytes": 65536,
                },
            ),
        })
        card = agent._build_agent_card()
        skill = next(s for s in card.skills if s.id == "live_system_metrics")
        assert skill.metadata["streaming_kind"] == "push"
        assert skill.metadata["max_fps"] == 2
        validate_streaming_metadata(skill.metadata)

    def test_explicit_kind_wins_over_legacy_default(self):
        agent = _StubAgent({
            "weird_tool": _tool(
                _noop,
                streamable={"default_interval": 2, "min_interval": 1, "max_interval": 30},
                metadata={"streamable": True, "streaming_kind": "push",
                          "max_fps": 5, "min_fps": 1, "max_chunk_bytes": 4096},
            ),
        })
        card = agent._build_agent_card()
        skill = next(s for s in card.skills if s.id == "weird_tool")
        assert skill.metadata["streaming_kind"] == "push"


class TestOrchestratorSourceParamsTagging:
    def test_source_params_tagged_top_level_only(self):
        from orchestrator.orchestrator import Orchestrator  # noqa: F401
        def _tag_source(comp, agent_id, tool_name, tool_params=None):
            if not isinstance(comp, dict):
                return
            comp["_source_agent"] = agent_id
            comp["_source_tool"] = tool_name
            if tool_params is not None:
                comp["_source_params"] = tool_params
            for key in ("content", "children"):
                nested = comp.get(key)
                if isinstance(nested, list):
                    for child in nested:
                        _tag_source(child, agent_id, tool_name)

        comp = {
            "type": "Card",
            "content": [
                {"type": "MetricCard", "value": "42%"},
                {"type": "Text", "content": "hi"},
            ],
        }
        _tag_source(comp, "general-1", "live_system_metrics",
                    tool_params={"interval_s": 5})
        assert comp["_source_params"] == {"interval_s": 5}
        assert comp["_source_tool"] == "live_system_metrics"
        for child in comp["content"]:
            assert child["_source_tool"] == "live_system_metrics"
            assert "_source_params" not in child
