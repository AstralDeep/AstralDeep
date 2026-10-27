"""Exercises orchestrator/terminal_host_review.py for the computer-use agent: only the exact in-process
implementation's run_command is cleared after real registration, and renamed, extra, replaced or
externally hosted run_command tools stay blocked by tool_security.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents.computer_use import mcp_tools
from orchestrator import local_agents
from orchestrator import terminal_host_review as review
from orchestrator.computer_hosts import ComputerHostRegistry
from orchestrator.computer_sessions import ComputerSessionManager
from orchestrator.orchestrator import GateRefusal, Orchestrator
from orchestrator.tool_security import ToolSecurityAnalyzer
from shared import attachment_materializer, attachment_resolver
from shared.feature_flags import flags
from shared.protocol import RegisterAgent
from tests.test_agent_key_enforcement import _make_fake
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

AGENT = "computer-use-1"


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_tools, "_ORCH", mcp_tools._ORCH)
    monkeypatch.setattr(mcp_tools, "_LOOP", mcp_tools._LOOP)
    monkeypatch.setenv("AGENT_API_KEY", "synthetic-registration-key")
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "agent.pem"))
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: [])
    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *a: True)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *a: True)
    for name in ("remote_compute", "cresco", "gaiakeep", "fhir", "evidence_context", "terminal_shell"):
        monkeypatch.setitem(flags._flags, name, False)
    monkeypatch.setitem(flags._flags, "computer_use", True)
    monkeypatch.setitem(flags._flags, "inprocess_agents", True)

    async def register(orch_type=Orchestrator):
        orch = orch_type.__new__(orch_type)
        orch.__dict__.update(vars(_make_fake()))
        orch.register_agent = Orchestrator.register_agent.__get__(orch)
        orch.local_agents = {}
        orch.security_analyzer = ToolSecurityAnalyzer()
        orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
            runtime=object(), repositories=object(), blobs=object(), attachment_materializer=object()))
        orch.computer_hosts = ComputerHostRegistry(orch)
        orch.computer_sessions = ComputerSessionManager(orch, orch.computer_hosts)
        assert await local_agents.register_built_ins(orch) == [AGENT]
        return orch

    return register


async def test_exact_in_process_implementation_clears_only_run_command(setup):
    orch = await setup()
    assert orch.security_flags[AGENT]["run_command"]["blocked"] is True
    assert review.computer_use_review_current(orch) is False
    assert review.review_registered_computer_use(orch) is True
    assert set(orch.security_flags[AGENT]) == {"run_command"}
    finding = orch.security_flags[AGENT]["run_command"]
    assert finding["category"] == "CODE_EXECUTION" and finding["blocked"] is False
    assert review.computer_use_review_current(orch) is True
    assert review.review_registered_terminal(orch) is False


async def test_registration_declares_end_session_a_host_owned_de_escalation(setup):
    orch = await setup()
    assert orch._source_bound_de_escalations == {(AGENT, "end_session")}
    card = deepcopy(orch.agent_cards[AGENT])
    await orch.register_agent(object(), RegisterAgent(agent_card=card, api_key="synthetic-registration-key"))
    assert orch._source_bound_de_escalations == set()


async def test_a_rescoped_end_session_is_not_declared_a_de_escalation(setup, monkeypatch):
    monkeypatch.setitem(mcp_tools.TOOL_REGISTRY, "end_session",
                        {**mcp_tools.TOOL_REGISTRY["end_session"], "scope": "tools:execute"})
    orch = await setup()
    assert not getattr(orch, "_source_bound_de_escalations", set())


@pytest.mark.parametrize("change", ["extra_run_shell", "renamed_run_shell"])
async def test_a_renamed_or_extra_command_tool_keeps_everything_blocked(setup, monkeypatch, change):
    entry = dict(mcp_tools.TOOL_REGISTRY["run_command"])
    monkeypatch.setitem(mcp_tools.TOOL_REGISTRY, "run_shell", entry)
    if change == "renamed_run_shell":
        monkeypatch.delitem(mcp_tools.TOOL_REGISTRY, "run_command")
    orch = await setup()
    assert review.review_registered_computer_use(orch) is False
    flagged = orch.security_flags[AGENT]
    assert "run_shell" in flagged and all(value["blocked"] is True for value in flagged.values())
    assert review.computer_use_review_current(orch) is False


async def test_an_externally_hosted_run_command_stays_blocked(setup):
    orch = await setup()
    assert review.review_registered_computer_use(orch) is True
    card = deepcopy(orch.agent_cards[AGENT])
    card.metadata["host_security_review"] = True
    await orch.register_agent(object(), RegisterAgent(agent_card=card, api_key="synthetic-registration-key"))
    assert orch.security_flags[AGENT]["run_command"]["blocked"] is True
    assert review.computer_use_review_current(orch) is False
    assert review.review_registered_computer_use(orch) is False
    other = deepcopy(card)
    other.agent_id = "foreign-runner-1"
    await orch.register_agent(object(), RegisterAgent(agent_card=other, api_key="synthetic-registration-key"))
    assert orch.security_flags["foreign-runner-1"]["run_command"]["blocked"] is True


@pytest.mark.parametrize("loss", ["function", "server_method", "agent_method", "remote", "flag", "inprocess",
                                  "manager", "deps", "card", "flags_forged", "marker"])
async def test_changed_binding_invalidates_the_review(setup, monkeypatch, loss):
    orch = await setup()
    assert review.review_registered_computer_use(orch) is True
    agent = orch.local_agents[AGENT]
    if loss == "function":
        monkeypatch.setitem(mcp_tools.TOOL_REGISTRY, "run_command",
                            {**mcp_tools.TOOL_REGISTRY["run_command"], "function": lambda **_: {}})
    elif loss == "server_method":
        agent.mcp_server.process_request = lambda *a: None
    elif loss == "agent_method":
        agent.handle_mcp_request = lambda *a: None
    elif loss == "remote":
        orch.agents[AGENT] = object()
    elif loss == "flag":
        monkeypatch.setitem(flags._flags, "computer_use", False)
    elif loss == "inprocess":
        monkeypatch.setitem(flags._flags, "inprocess_agents", False)
    elif loss == "manager":
        orch.computer_sessions = ComputerSessionManager(object(), orch.computer_hosts)
    elif loss == "deps":
        monkeypatch.setattr(mcp_tools, "_ORCH", object())
    elif loss == "card":
        agent.card.skills[0].scope = "tools:system"
    elif loss == "flags_forged":
        orch.security_flags[AGENT]["run_command"]["reason"] = "self-approved"
    else:
        del orch._computer_use_host_review
    assert review.computer_use_review_current(orch) is False


async def test_a_subclass_host_is_never_reviewed(setup):
    class DerivedHost(Orchestrator):
        pass

    orch = await setup(DerivedHost)
    assert review.review_registered_computer_use(orch) is False
    assert orch.security_flags[AGENT]["run_command"]["blocked"] is True


async def test_the_gate_refuses_an_unreviewed_run_command(gate_orchestrator):
    orch, websocket = gate_orchestrator
    finding = ToolSecurityAnalyzer().analyze_tool("run_command", mcp_tools.TOOL_REGISTRY["run_command"]["description"])
    orch.security_flags = {AGENT: {"run_command": finding.to_dict()}}
    refusal = await orch._authorize_and_prepare(websocket, AGENT, "run_command", {"command": "ls", "cwd": "/x"},
                                                "chat-a", "owner-a", auto_subscribe_stream=False)
    assert isinstance(refusal, GateRefusal) and "system-blocked" in refusal.response.error["message"]


def test_startup_reviews_the_computer_use_implementation_after_built_in_registration():
    import inspect

    source = inspect.getsource(Orchestrator)
    registration = source.index("await local_agents.register_built_ins(self)")
    assert source.index("review_registered_computer_use(self)", registration) - registration < 200
