"""End-to-end tests for the leak-detection + alert-injection pipeline.

Exercises the same chain the chat-message handler uses:
    raw_content
        → orch._diagnose_leaked_tool_calls(...) → list of Alert dicts
        → _sanitize_text_response(raw_content) → cleaned text
"""
from unittest.mock import MagicMock


from orchestrator.orchestrator import (
    Orchestrator,
    _LEAKED_TOOL_CALL_PATTERNS,
    _sanitize_text_response,
)
from shared.protocol import AgentCard, AgentSkill


def _orch_with(tool_to_agent: dict, *,
               disabled_agents: list = None,
               saved_selection: dict = None,
               chat_to_agent: dict = None) -> Orchestrator:
    cards = {}
    for tool_name, agent_id in tool_to_agent.items():
        if agent_id not in cards:
            cards[agent_id] = AgentCard(
                name=agent_id, description="", agent_id=agent_id, skills=[],
            )
        cards[agent_id].skills.append(
            AgentSkill(id=tool_name, name=tool_name, description="", input_schema={})
        )

    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards = cards
    orch.security_flags = {}
    orch.history = MagicMock()
    orch.history.get_chat_agent.side_effect = (
        lambda chat_id, *, user_id: (chat_to_agent or {}).get(chat_id)
    )
    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    orch.tool_permissions.list_disabled_agents.return_value = tuple(
        disabled_agents or ()
    )
    orch.tool_permissions.get_tool_selection.side_effect = (
        lambda user_id, agent_id: (saved_selection or {}).get(
            (user_id, agent_id)
        )
    )
    return orch


DSML_BLOB = (
    'I will check the spreadsheet first. '
    '<｜DSML｜tool_calls> <｜DSML｜invoke name="read_spreadsheet"> '
    '<｜DSML｜parameter name="attachment_id">7f628827</｜DSML｜parameter> '
    '</｜DSML｜invoke> </｜DSML｜tool_calls>'
)


def test_dsml_pattern_matches() -> None:
    """Regex regression — the new DSML pattern must fire on real markup."""
    matched = any(p.search(DSML_BLOB) for p in _LEAKED_TOOL_CALL_PATTERNS)
    assert matched, "DSML markup must be matched by at least one leak pattern"


def test_dsml_stripped_from_assistant_text() -> None:
    cleaned = _sanitize_text_response(DSML_BLOB)
    assert "DSML" not in cleaned
    assert "<｜" not in cleaned
    assert "I will check the spreadsheet first." in cleaned


def test_diagnose_returns_alert_for_disabled_picker() -> None:
    """The user has disabled `read_spreadsheet` in the picker → warning alert."""
    orch = _orch_with(
        tool_to_agent={"read_spreadsheet": "general-1", "ocr": "general-1"},
        chat_to_agent={"chat-1": "general-1"},
        saved_selection={("alice", "general-1"): ["ocr"]},  # read_spreadsheet excluded
    )
    alerts = orch._diagnose_leaked_tool_calls(DSML_BLOB, "alice", "chat-1")
    assert len(alerts) == 1
    a = alerts[0]
    assert a["type"] == "alert"
    assert a["variant"] == "warning"
    assert "read_spreadsheet" in a["message"]
    assert "tool picker" in a["message"]


def test_diagnose_returns_alert_for_unknown_tool() -> None:
    """Tool name extracted but no agent owns it → error alert (unknown tool)."""
    orch = _orch_with(tool_to_agent={"ocr": "general-1"})
    alerts = orch._diagnose_leaked_tool_calls(DSML_BLOB, "alice", "chat-1")
    assert len(alerts) == 1
    assert alerts[0]["variant"] == "error"
    assert "no installed agent" in alerts[0]["message"]


def test_diagnose_info_alert_when_tool_is_actually_enabled() -> None:
    """If the tool exists and is enabled, the user is told their model emitted bad markup."""
    orch = _orch_with(
        tool_to_agent={"read_spreadsheet": "general-1"},
    )
    alerts = orch._diagnose_leaked_tool_calls(DSML_BLOB, "alice", "chat-1")
    assert len(alerts) == 1
    a = alerts[0]
    assert a["variant"] == "info"
    assert "tool calling" in a["message"] or "tool-call" in a["message"]


def test_diagnose_returns_empty_when_no_leak() -> None:
    orch = _orch_with(tool_to_agent={"read_spreadsheet": "general-1"})
    assert orch._diagnose_leaked_tool_calls("plain reply, no markup", "alice", "chat-1") == []


def test_diagnose_returns_empty_when_leak_but_no_extractable_name() -> None:
    """A bare leak token with no name → no alert (silent strip preserved)."""
    orch = _orch_with(tool_to_agent={"foo": "general-1"})
    blob = "<|tool_call|>"  # dangling open tag, no JSON, no name
    # Sanitizer still strips it.
    assert "tool_call" not in _sanitize_text_response(blob)
    # Diagnostic returns no alerts since no name was extractable.
    assert orch._diagnose_leaked_tool_calls(blob, "alice", "chat-1") == []


def test_diagnose_dedupes_multiple_invokes_of_same_tool() -> None:
    blob = (
        '<｜DSML｜invoke name="read_spreadsheet"></｜DSML｜invoke> '
        '<｜DSML｜invoke name="read_spreadsheet"></｜DSML｜invoke>'
    )
    orch = _orch_with(
        tool_to_agent={"read_spreadsheet": "general-1"},
        chat_to_agent={"chat-1": "general-1"},
        saved_selection={("alice", "general-1"): []},  # empty selection — does NOT filter
    )
    alerts = orch._diagnose_leaked_tool_calls(blob, "alice", "chat-1")
    # An empty saved selection means "default", not "filter to nothing" — so the
    # tool is enabled; we expect ONE info alert (deduped from two invokes).
    assert len(alerts) == 1


def test_diagnose_emits_distinct_alerts_for_distinct_tools() -> None:
    blob = (
        '<｜DSML｜invoke name="read_spreadsheet"></｜DSML｜invoke> '
        '<｜DSML｜invoke name="ocr"></｜DSML｜invoke>'
    )
    orch = _orch_with(
        tool_to_agent={"read_spreadsheet": "general-1", "ocr": "general-1"},
        chat_to_agent={"chat-1": "general-1"},
        # User disabled both tools in their picker.
        saved_selection={("alice", "general-1"): ["something_else"]},
    )
    alerts = orch._diagnose_leaked_tool_calls(blob, "alice", "chat-1")
    assert len(alerts) == 2
    msgs = [a["message"] for a in alerts]
    assert any("read_spreadsheet" in m for m in msgs)
    assert any("ocr" in m for m in msgs)
