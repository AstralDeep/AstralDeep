"""Tests for Orchestrator._diagnose_disabled_tool and _alert_for_disabled_tool."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.orchestrator import (
    Orchestrator,
    ToolDiagnostic,
    ToolDiagnosticStatus,
    _tool_names_from_leak,
)
from orchestrator.plane_repository_context import ApplicationPlaneSource
from shared.protocol import AgentCard, AgentSkill


def _make_card(agent_id: str, tools: list, display_name: str = None) -> AgentCard:
    return AgentCard(
        name=display_name or agent_id,
        description="test agent",
        agent_id=agent_id,
        skills=[AgentSkill(id=t, name=t, description="", input_schema={}) for t in tools],
        metadata={},
    )


class _MachineRepository:
    def __init__(self, owns_machine: bool, *, fail: bool = False) -> None:
        self.owns_machine = owns_machine
        self.fail = fail

    def list_machines(self, _transaction, *, owner_id: str, limit: int):
        assert owner_id
        assert limit == 1
        if self.fail:
            raise RuntimeError("machine inventory unavailable")
        return (object(),) if self.owns_machine else ()


class _PlaneRuntime:
    def __init__(self, remote: _MachineRepository) -> None:
        self.repositories = SimpleNamespace(remote=remote)

    @contextmanager
    def transaction(self):
        yield object()


def _make_orch(cards: dict, *,
               disabled_agents: list = None,
               security_flags: dict = None,
               tool_permission_allowed: bool = True,
               saved_selection: dict = None,
               chat_to_agent: dict = None,
               owns_remote_machine: bool = True,
               remote_probe_fails: bool = False) -> Orchestrator:
    """Build a partially-initialized Orchestrator for unit tests."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards = cards
    orch.security_flags = security_flags or {}

    orch.history = MagicMock()
    orch.history.get_chat_agent.side_effect = (
        lambda chat_id, *, user_id: (chat_to_agent or {}).get(chat_id)
    )

    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=tool_permission_allowed)
    orch.tool_permissions.list_disabled_agents.return_value = tuple(
        disabled_agents or ()
    )
    orch.tool_permissions.get_tool_selection.side_effect = (
        lambda user_id, agent_id: (saved_selection or {}).get(
            (user_id, agent_id)
        )
    )
    remote = _MachineRepository(
        owns_remote_machine,
        fail=remote_probe_fails,
    )
    runtime = _PlaneRuntime(remote)
    orch.plane_repository_source = ApplicationPlaneSource(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    return orch


# -- _find_tool_owner ---------------------------------------------------------


def test_find_tool_owner_returns_owning_agent() -> None:
    orch = _make_orch({
        "general-1": _make_card("general-1", ["read_spreadsheet", "ocr"], display_name="General"),
        "weather-1": _make_card("weather-1", ["forecast"], display_name="Weather"),
    })
    assert orch._find_tool_owner("read_spreadsheet") == "general-1"
    assert orch._find_tool_owner("forecast") == "weather-1"


def test_find_tool_owner_returns_none_for_unknown() -> None:
    orch = _make_orch({"general-1": _make_card("general-1", ["ocr"])})
    assert orch._find_tool_owner("nonexistent_tool") is None


def test_find_tool_owner_handles_empty() -> None:
    orch = _make_orch({})
    assert orch._find_tool_owner("") is None
    assert orch._find_tool_owner(None) is None


# -- _diagnose_disabled_tool --------------------------------------------------


def test_diagnose_unknown_tool() -> None:
    orch = _make_orch({"general-1": _make_card("general-1", ["ocr"])})
    diag = orch._diagnose_disabled_tool("never_heard_of_it", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.UNKNOWN_TOOL
    assert diag.agent_id is None


def test_diagnose_agent_disabled_by_user() -> None:
    cards = {"general-1": _make_card("general-1", ["read_spreadsheet"], display_name="General")}
    orch = _make_orch(cards, disabled_agents=["general-1"])
    diag = orch._diagnose_disabled_tool("read_spreadsheet", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.AGENT_DISABLED_BY_USER
    assert diag.agent_id == "general-1"
    assert diag.agent_display_name == "General"


def test_diagnose_security_blocked_carries_reason() -> None:
    cards = {"general-1": _make_card("general-1", ["read_spreadsheet"])}
    orch = _make_orch(
        cards,
        security_flags={"general-1": {"read_spreadsheet": {"blocked": True, "reason": "PII leak"}}},
    )
    diag = orch._diagnose_disabled_tool("read_spreadsheet", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.SECURITY_BLOCKED
    assert diag.reason == "PII leak"


def test_diagnose_permission_denied() -> None:
    cards = {"general-1": _make_card("general-1", ["read_spreadsheet"])}
    orch = _make_orch(cards, tool_permission_allowed=False)
    diag = orch._diagnose_disabled_tool("read_spreadsheet", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.PERMISSION_DENIED


def test_diagnose_disabled_in_picker() -> None:
    cards = {"general-1": _make_card("general-1", ["read_spreadsheet", "ocr"], display_name="General")}
    orch = _make_orch(
        cards,
        chat_to_agent={"chat-1": "general-1"},
        # The user picked only "ocr" for this chat; read_spreadsheet is excluded.
        saved_selection={("alice", "general-1"): ["ocr"]},
    )
    diag = orch._diagnose_disabled_tool("read_spreadsheet", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.DISABLED_IN_PICKER
    assert diag.agent_id == "general-1"


def test_diagnose_enabled_when_picker_includes_tool() -> None:
    cards = {"general-1": _make_card("general-1", ["read_spreadsheet", "ocr"])}
    orch = _make_orch(
        cards,
        chat_to_agent={"chat-1": "general-1"},
        saved_selection={("alice", "general-1"): ["read_spreadsheet", "ocr"]},
    )
    diag = orch._diagnose_disabled_tool("read_spreadsheet", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.ENABLED


def test_diagnose_enabled_when_no_filters_apply() -> None:
    cards = {"general-1": _make_card("general-1", ["read_spreadsheet"])}
    orch = _make_orch(cards)  # no disable, no flag, allowed, no saved selection
    diag = orch._diagnose_disabled_tool("read_spreadsheet", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.ENABLED


def test_priority_user_disable_beats_picker() -> None:
    """If the agent is wholly disabled, that beats a per-chat picker subset."""
    cards = {"general-1": _make_card("general-1", ["read_spreadsheet", "ocr"])}
    orch = _make_orch(
        cards,
        disabled_agents=["general-1"],
        chat_to_agent={"chat-1": "general-1"},
        saved_selection={("alice", "general-1"): ["ocr"]},
    )
    diag = orch._diagnose_disabled_tool("read_spreadsheet", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.AGENT_DISABLED_BY_USER


def test_diagnose_machineless_remote_verb() -> None:
    """A remote verb hidden by the machineless subtraction gets the honest
    diagnosis (register a machine), not the ENABLED format-mismatch alert."""
    cards = {"remote-compute-1": _make_card(
        "remote-compute-1", ["job_status", "list_machines"], display_name="Remote Compute"
    )}
    orch = _make_orch(cards, owns_remote_machine=False)
    diag = orch._diagnose_disabled_tool("job_status", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.NO_REGISTERED_MACHINE
    assert diag.agent_id == "remote-compute-1"


def test_diagnose_machineless_list_machines_stays_enabled() -> None:
    cards = {"remote-compute-1": _make_card("remote-compute-1", ["list_machines"])}
    orch = _make_orch(cards, owns_remote_machine=False)
    diag = orch._diagnose_disabled_tool("list_machines", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.ENABLED


def test_diagnose_machine_owner_remote_verb_enabled() -> None:
    cards = {"remote-compute-1": _make_card("remote-compute-1", ["job_status"])}
    orch = _make_orch(cards, owns_remote_machine=True)
    diag = orch._diagnose_disabled_tool("job_status", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.ENABLED


def test_diagnose_machineless_probe_error_falls_through() -> None:
    cards = {"remote-compute-1": _make_card("remote-compute-1", ["job_status"])}
    orch = _make_orch(cards, remote_probe_fails=True)
    diag = orch._diagnose_disabled_tool("job_status", "alice", "chat-1")
    assert diag.status is ToolDiagnosticStatus.ENABLED


# -- _alert_for_disabled_tool -------------------------------------------------


@pytest.mark.parametrize(
    "status, expected_variant, expected_keyword",
    [
        (ToolDiagnosticStatus.DISABLED_IN_PICKER, "warning", "tool picker"),
        (ToolDiagnosticStatus.AGENT_DISABLED_BY_USER, "warning", "Agents settings"),
        (ToolDiagnosticStatus.PERMISSION_DENIED, "warning", "permissions"),
        (ToolDiagnosticStatus.SECURITY_BLOCKED, "error", "system-blocked"),
        (ToolDiagnosticStatus.UNKNOWN_TOOL, "error", "no installed agent"),
        (ToolDiagnosticStatus.NO_REGISTERED_MACHINE, "warning", "Remote machines"),
        (ToolDiagnosticStatus.ENABLED, "info", "tool calling"),
    ],
)
def test_alert_for_disabled_tool_variants(status, expected_variant, expected_keyword) -> None:
    diag = ToolDiagnostic(
        status=status,
        agent_id="general-1",
        agent_display_name="General",
        reason="PII leak" if status is ToolDiagnosticStatus.SECURITY_BLOCKED else None,
    )
    alert = Orchestrator._alert_for_disabled_tool(diag, "read_spreadsheet")
    assert alert.variant == expected_variant
    assert "read_spreadsheet" in alert.message
    assert expected_keyword in alert.message


def test_alert_security_includes_reason() -> None:
    diag = ToolDiagnostic(
        status=ToolDiagnosticStatus.SECURITY_BLOCKED,
        agent_id="general-1",
        agent_display_name="General",
        reason="known-malicious upstream",
    )
    alert = Orchestrator._alert_for_disabled_tool(diag, "read_spreadsheet")
    assert "known-malicious upstream" in alert.message


def test_alert_unknown_tool_does_not_require_agent_label() -> None:
    diag = ToolDiagnostic(
        status=ToolDiagnosticStatus.UNKNOWN_TOOL,
        agent_id=None,
        agent_display_name=None,
        reason=None,
    )
    alert = Orchestrator._alert_for_disabled_tool(diag, "wat_tool")
    assert "wat_tool" in alert.message
    assert alert.variant == "error"


# -- _tool_names_from_leak ----------------------------------------------------


def test_tool_names_from_dsml_invoke() -> None:
    blob = (
        '<｜DSML｜tool_calls> <｜DSML｜invoke name="read_spreadsheet"> '
        '<｜DSML｜parameter name="attachment_id">x</｜DSML｜parameter> '
        '</｜DSML｜invoke> </｜DSML｜tool_calls>'
    )
    assert _tool_names_from_leak(blob) == ["read_spreadsheet"]


def test_tool_names_from_openai_leak() -> None:
    blob = '<|tool_call|>{"name":"read_spreadsheet","arguments":{}}</|tool_call|>'
    assert _tool_names_from_leak(blob) == ["read_spreadsheet"]


def test_tool_names_dedupe_across_patterns() -> None:
    blob = (
        '<|tool_call|>{"name":"foo"}</|tool_call|> '
        '<｜DSML｜invoke name="foo"></｜DSML｜invoke>'
    )
    assert _tool_names_from_leak(blob) == ["foo"]


def test_tool_names_multiple_distinct() -> None:
    blob = (
        '<｜DSML｜invoke name="alpha"></｜DSML｜invoke> '
        '<｜DSML｜invoke name="beta"></｜DSML｜invoke>'
    )
    assert _tool_names_from_leak(blob) == ["alpha", "beta"]


def test_tool_names_empty_when_no_match() -> None:
    assert _tool_names_from_leak("just plain text") == []
    assert _tool_names_from_leak("") == []


def test_tool_names_qwen_xml_form() -> None:
    blob = "<tool_call><name>do_it</name><arguments>{}</arguments></tool_call>"
    assert _tool_names_from_leak(blob) == ["do_it"]
