"""Tests for shared/a2a_bridge.py's custom_card_to_a2a: agent ids come from a cleaned
slug or an explicit override, never a raw display name or a base URL's host:port.
"""

from shared.a2a_bridge import (
    a2a_card_to_custom,
    custom_card_to_a2a,
    _slugify_agent_id,
)
from shared.protocol import AgentCard as CustomAgentCard


def _custom(name: str, agent_id: str = "x") -> CustomAgentCard:
    return CustomAgentCard(
        name=name, description="d", agent_id=agent_id,
        version="1.0.0", skills=[], metadata={},
    )


def test_slugify_strips_punctuation_and_collapses():
    assert _slugify_agent_id("Windows Tools (code & system)") == "windows-tools-code-system"
    assert _slugify_agent_id("  A  B  ") == "a-b"
    assert _slugify_agent_id("") == "agent"
    assert _slugify_agent_id("Already-Clean-1") == "already-clean-1"


def test_no_interface_slugs_clean_id_not_raw_name():
    a2a = custom_card_to_a2a(_custom("Windows Tools (code & system)"), "")
    out = a2a_card_to_custom(a2a)
    assert out.agent_id == "windows-tools-code-system"
    assert "(" not in out.agent_id and "&" not in out.agent_id


def test_base_url_host_port_is_rejected():
    a2a = custom_card_to_a2a(
        _custom("Windows Tools (code & system)"), "http://host.docker.internal:8771"
    )
    out = a2a_card_to_custom(a2a)
    assert out.agent_id == "windows-tools-code-system"
    assert ":" not in out.agent_id and out.agent_id != "8771"


def test_clean_url_path_segment_is_used():
    a2a = custom_card_to_a2a(_custom("Some Agent"), "http://host:9003/some-agent-1")
    out = a2a_card_to_custom(a2a)
    assert out.agent_id == "some-agent-1"


def test_explicit_agent_id_override_wins():
    a2a = custom_card_to_a2a(_custom("Windows Tools (code & system)"), "http://host:8771")
    out = a2a_card_to_custom(a2a, agent_id="windows-tools-1")
    assert out.agent_id == "windows-tools-1"
