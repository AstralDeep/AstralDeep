"""Reviews only the host-installed canonical implementations that scanner rules flag for code execution:
the isolated SSH terminal agent and the in-process computer-use agent's run_command. Registration records
the host binding; terminal_security.py and the computer-use tools revalidate it without granting any target.
"""

from dataclasses import dataclass
import json
import logging
from typing import Callable

from shared.feature_flags import flags

_AGENT = "terminal-shell-1"
_TOOLS = frozenset({"start_terminal", "terminal_write"})
_REASON = "Code execution on a host-reviewed isolated SSH implementation; current operator profile and human approval required"
_COMPUTER_AGENT = "computer-use-1"
_COMPUTER_TOOLS = frozenset({"run_command"})
_COMPUTER_REASON = ("Local command execution on the owner's own Mac through the exact in-process computer-use "
                    "implementation; exact or task-granted human approval required")
_AGENT_OVERRIDES = ("agent_id", "service_name", "description", "skill_tags", "examples", "card_metadata",
                    "_build_agent_card", "handle_mcp_request")
logger = logging.getLogger("TerminalHostReview")


@dataclass(frozen=True)
class _HostReview:
    host: object
    agent: object
    manager: object
    card: str
    findings: str


@dataclass(frozen=True)
class _Implementation:
    agent: str
    tools: frozenset
    reason: str
    marker: str
    identity: Callable


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_card(orch, agent, agent_id):
    from shared.base_agent import BaseA2AAgent
    from shared.schema_validation import validate_tool_schema

    if any(name in vars(agent) for name in _AGENT_OVERRIDES) or "process_request" in vars(agent.mcp_server):
        return None
    expected = BaseA2AAgent._build_agent_card(agent)
    for skill in expected.skills:
        skill.input_schema = validate_tool_schema(skill.input_schema, skill.id, require_object_root=True)
        if skill.output_schema is not None:
            skill.output_schema = validate_tool_schema(skill.output_schema, skill.id, require_object_root=False)
    runtime = getattr(orch, "_governed_dispatch_runtimes", {}).get(agent_id)
    expected.metadata["astral_dispatch_posture"] = runtime.dispatch_posture if runtime else "dispatch_mediated_only"
    expected.metadata["protected_executor"] = bool(runtime.executor_conformant) if runtime else False
    if _wire(agent.card.to_dict()) != _wire(expected.to_dict()):
        return None
    return _wire(agent.card.to_dict())


def _in_process(orch, agent_id, flag):
    from orchestrator.orchestrator import Orchestrator

    if (type(orch) is not Orchestrator or flags.is_enabled(flag) is not True
            or flags.is_enabled("inprocess_agents") is not True):
        return None
    local = getattr(orch, "local_agents", None)
    cards = getattr(orch, "agent_cards", None)
    if (type(local) is not dict or type(cards) is not dict or agent_id in getattr(orch, "agents", {})
            or agent_id in getattr(orch, "a2a_clients", {})):
        return None
    agent = local.get(agent_id)
    return agent if agent is not None and cards.get(agent_id) is agent.card else None


def _identity(orch):
    from agents.terminal_shell import mcp_tools
    from agents.terminal_shell.mcp_server import MCPServer
    from agents.terminal_shell.terminal_shell_agent import TerminalShellAgent
    from orchestrator.terminal_sessions import TerminalSessionManager

    agent = _in_process(orch, _AGENT, "terminal_shell")
    manager = getattr(orch, "terminal_sessions", None)
    if (type(agent) is not TerminalShellAgent or agent._terminal_orchestrator is not orch
            or type(manager) is not TerminalSessionManager or manager._orchestrator is not orch
            or type(agent.mcp_server) is not MCPServer
            or agent.mcp_server.tools is not mcp_tools.TOOL_REGISTRY or mcp_tools._ORCH is not orch):
        return None
    card = _canonical_card(orch, agent, _AGENT)
    return None if card is None else (agent, manager, card)


def _computer_identity(orch):
    from agents.computer_use import mcp_tools
    from agents.computer_use.computer_use_agent import ComputerUseAgent
    from agents.computer_use.mcp_server import MCPServer
    from orchestrator.computer_sessions import ComputerSessionManager

    agent = _in_process(orch, _COMPUTER_AGENT, "computer_use")
    manager = getattr(orch, "computer_sessions", None)
    if (type(agent) is not ComputerUseAgent or type(manager) is not ComputerSessionManager
            or manager._orch is not orch or type(agent.mcp_server) is not MCPServer
            or agent.mcp_server.tools is not mcp_tools.TOOL_REGISTRY or mcp_tools._ORCH is not orch
            or mcp_tools.TOOL_REGISTRY.get("run_command", {}).get("function") is not mcp_tools.run_command):
        return None
    card = _canonical_card(orch, agent, _COMPUTER_AGENT)
    return None if card is None else (agent, manager, card)


_TERMINAL = _Implementation(_AGENT, _TOOLS, _REASON, "_terminal_host_review", _identity)
_COMPUTER = _Implementation(_COMPUTER_AGENT, _COMPUTER_TOOLS, _COMPUTER_REASON, "_computer_use_host_review",
                            _computer_identity)


def _review(orch, implementation: _Implementation) -> bool:
    from orchestrator.tool_security import ToolSecurityAnalyzer

    try:
        identity = implementation.identity(orch)
        if identity is None:
            return False
        agent, manager, card = identity
        expected = {name: finding.to_dict() for name, finding in ToolSecurityAnalyzer().analyze_agent(agent.card).items()}
        current = getattr(orch, "security_flags", {}).get(implementation.agent)
        if (set(expected) != implementation.tools or current != expected
                or any(value["category"] != "CODE_EXECUTION" or value["blocked"] is not True
                       for value in expected.values())):
            return False
        reviewed = {name: {**value, "blocked": False, "reason": implementation.reason}
                    for name, value in expected.items()}
        setattr(orch, implementation.marker, _HostReview(orch, agent, manager, card, _wire(reviewed)))
        orch.security_flags[implementation.agent] = reviewed
        logger.info("Host reviewed implementation: agent=%s tools=%s", implementation.agent,
                    sorted(implementation.tools))
        return True
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _current(orch, implementation: _Implementation) -> bool:
    try:
        review = getattr(orch, implementation.marker, None)
        identity = implementation.identity(orch)
        return (type(review) is _HostReview and review.host is orch and identity is not None
                and identity[0] is review.agent and identity[1] is review.manager and identity[2] == review.card
                and _wire(getattr(orch, "security_flags", {}).get(implementation.agent)) == review.findings)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def review_registered_terminal(orch):
    return _review(orch, _TERMINAL)


def review_registered_computer_use(orch):
    return _review(orch, _COMPUTER)


def computer_use_review_current(orch) -> bool:
    return _current(orch, _COMPUTER)


def assert_current_review(orch):
    from orchestrator.terminal_security import TerminalError

    if not _current(orch, _TERMINAL):
        raise TerminalError("terminal_security_blocked", 403)
