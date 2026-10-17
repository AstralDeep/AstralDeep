"""Reviews only the host-installed canonical terminal implementation for isolated SSH execution.
Registration records the host binding; terminal authority revalidates it without granting any owner target or permission.
"""

from dataclasses import dataclass
import json
import logging

from shared.feature_flags import flags

_AGENT = "terminal-shell-1"
_TOOLS = frozenset({"start_terminal", "terminal_write"})
_REASON = "Code execution on a host-reviewed isolated SSH implementation; current operator profile and human approval required"
logger = logging.getLogger("TerminalHostReview")


@dataclass(frozen=True)
class _HostReview:
    host: object
    agent: object
    manager: object
    card: str
    findings: str


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(orch):
    from agents.terminal_shell import mcp_tools
    from agents.terminal_shell.mcp_server import MCPServer
    from agents.terminal_shell.terminal_shell_agent import TerminalShellAgent
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.terminal_sessions import TerminalSessionManager
    from shared.base_agent import BaseA2AAgent
    from shared.schema_validation import validate_tool_schema

    if (type(orch) is not Orchestrator or flags.is_enabled("terminal_shell") is not True
            or flags.is_enabled("inprocess_agents") is not True):
        return None
    local = getattr(orch, "local_agents", None)
    cards = getattr(orch, "agent_cards", None)
    if type(local) is not dict or type(cards) is not dict:
        return None
    agent, manager = local.get(_AGENT), getattr(orch, "terminal_sessions", None)
    if (type(agent) is not TerminalShellAgent or agent._terminal_orchestrator is not orch
            or type(manager) is not TerminalSessionManager or manager._orchestrator is not orch
            or cards.get(_AGENT) is not agent.card or type(agent.mcp_server) is not MCPServer
            or agent.mcp_server.tools is not mcp_tools.TOOL_REGISTRY or mcp_tools._ORCH is not orch
            or _AGENT in getattr(orch, "agents", {}) or _AGENT in getattr(orch, "a2a_clients", {})):
        return None
    if any(name in vars(agent) for name in (
        "agent_id", "service_name", "description", "skill_tags", "examples", "card_metadata",
        "_build_agent_card", "handle_mcp_request",
    )) or "process_request" in vars(agent.mcp_server):
        return None
    expected = BaseA2AAgent._build_agent_card(agent)
    for skill in expected.skills:
        skill.input_schema = validate_tool_schema(skill.input_schema, skill.id, require_object_root=True)
        if skill.output_schema is not None:
            skill.output_schema = validate_tool_schema(skill.output_schema, skill.id, require_object_root=False)
    runtime = getattr(orch, "_governed_dispatch_runtimes", {}).get(_AGENT)
    expected.metadata["astral_dispatch_posture"] = runtime.dispatch_posture if runtime else "dispatch_mediated_only"
    expected.metadata["protected_executor"] = bool(runtime.executor_conformant) if runtime else False
    if _wire(agent.card.to_dict()) != _wire(expected.to_dict()):
        return None
    return agent, manager, _wire(agent.card.to_dict())


def review_registered_terminal(orch):
    from orchestrator.tool_security import ToolSecurityAnalyzer

    try:
        identity = _identity(orch)
        if identity is None:
            return False
        agent, manager, card = identity
        expected = {name: finding.to_dict() for name, finding in ToolSecurityAnalyzer().analyze_agent(agent.card).items()}
        current = getattr(orch, "security_flags", {}).get(_AGENT)
        if (set(expected) != _TOOLS or current != expected
                or any(value["category"] != "CODE_EXECUTION" or value["blocked"] is not True
                       for value in expected.values())):
            return False
        reviewed = {name: {**value, "blocked": False, "reason": _REASON} for name, value in expected.items()}
        orch._terminal_host_review = _HostReview(orch, agent, manager, card, _wire(reviewed))
        orch.security_flags[_AGENT] = reviewed
        logger.info("Host reviewed isolated terminal implementation: agent=%s tools=%s", _AGENT, sorted(_TOOLS))
        return True
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def assert_current_review(orch):
    from orchestrator.terminal_security import TerminalError

    try:
        review = getattr(orch, "_terminal_host_review", None)
        identity = _identity(orch)
        if (type(review) is not _HostReview or review.host is not orch or identity is None
                or identity[0] is not review.agent or identity[1] is not review.manager
                or identity[2] != review.card
                or _wire(getattr(orch, "security_flags", {}).get(_AGENT)) != review.findings):
            raise ValueError()
    except (AttributeError, KeyError, TypeError, ValueError):
        raise TerminalError("terminal_security_blocked", 403) from None
