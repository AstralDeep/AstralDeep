"""The shared derivation seam (MachineTurnAuthority.derive) that scheduled jobs, parser
replays, draft self-tests and mesh requests use for consent-derived root authority, plus
ChainBudget's per-turn hop/depth/wall-clock ceiling over nested delegation.
A mesh whole-turn binding also carries the exact tools its mesh grants cover, and a caller-ticket call binds the one tool it names, and orchestrator.py admits only those.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from orchestrator.agent_authority import DEFAULT_MAX_DELEGATION_DEPTH

logger = logging.getLogger("orchestrator.chain_authority")

DEFAULT_MAX_CHAIN_HOPS = int(os.getenv("CHAIN_MAX_HOPS", "12"))
DEFAULT_CHAIN_WALL_CLOCK_S = float(os.getenv("CHAIN_WALL_CLOCK_SECONDS", "120"))

MACHINE_TURN_CLASSES = (
    "scheduled_job", "parser_replay", "draft_self_test", "persistent_assignment",
    "mesh_request",
)

_MACHINE_SCOPE_KEY = "_machine_authority_scopes"
_MACHINE_TOOLS_KEY = "_machine_authority_tools"
_STANDING_CONSENT_CLASSES = ("parser_replay", "draft_self_test")
_TOOL_LISTED_CLASSES = ("mesh_request",)
_CALLER_BOUND_KEY = "_caller_bound_tool"


def bind_caller_tool(session: Dict[str, Any], agent_id: str, tool: str, definition: Optional[str], scope: Optional[str]) -> None:
    session[_CALLER_BOUND_KEY] = (agent_id, tool, definition, scope)


def caller_bound(session: Dict[str, Any]) -> bool:
    return _CALLER_BOUND_KEY in session


def _caller_binding(session: Dict[str, Any]) -> Optional[Tuple[str, str, str, str]]:
    bound = session.get(_CALLER_BOUND_KEY)
    if isinstance(bound, tuple) and len(bound) == 4 and all(isinstance(part, str) and part for part in bound):
        return bound
    return None


def machine_scope_ceiling(session: Dict[str, Any]) -> Optional[frozenset[str]]:
    if caller_bound(session):
        from orchestrator.tool_permissions import VALID_SCOPES

        bound = _caller_binding(session)
        return frozenset({bound[3]}) if bound and bound[3] in VALID_SCOPES else frozenset()
    if "machine_class" not in session and _MACHINE_SCOPE_KEY not in session:
        return None
    from orchestrator.tool_permissions import VALID_SCOPES

    scopes = session.get(_MACHINE_SCOPE_KEY)
    if (
        session.get("machine_class") in _STANDING_CONSENT_CLASSES
        and _MACHINE_SCOPE_KEY in session
        and scopes is None
    ):
        return None
    if (
        session.get("machine_class") not in MACHINE_TURN_CLASSES
        or not isinstance(scopes, tuple)
        or any(not isinstance(scope, str) or scope not in VALID_SCOPES for scope in scopes)
    ):
        return frozenset()
    return frozenset(scopes)


def machine_tool_listed(session: Dict[str, Any]) -> bool:
    if caller_bound(session):
        return True
    return session.get("machine_class") in _TOOL_LISTED_CLASSES and session.get("_machine_authority_agent") is None


def _valid_tools(tools: Any) -> bool:
    return isinstance(tools, tuple) and all(
        isinstance(entry, tuple) and len(entry) == 3
        and all(isinstance(part, str) and part for part in entry)
        for entry in tools
    )


def machine_tool_admitted(orch: Any, session: Dict[str, Any], agent_id: str, tool: str) -> bool:
    if not machine_tool_listed(session):
        return True
    if caller_bound(session):
        bound = _caller_binding(session)
        if bound is None or (agent_id, tool) != bound[:2]:
            return False
        from orchestrator.node_mesh_calls import current_definition

        return current_definition(orch, agent_id, tool) == bound[2]
    tools = session.get(_MACHINE_TOOLS_KEY)
    if not _valid_tools(tools):
        return False
    from orchestrator.node_mesh_calls import current_definition

    definition = current_definition(orch, agent_id, tool)
    return definition is not None and (agent_id, tool, definition) in tools


def machine_meta_refused(session: Dict[str, Any]) -> bool:
    return session.get("machine_class") in _TOOL_LISTED_CLASSES


def machine_session_binding(authority: MachineAuthority) -> Dict[str, Any]:
    from orchestrator.tool_permissions import VALID_SCOPES

    scopes = authority.allowed_scopes
    tools = authority.allowed_tools
    if (
        authority.turn_class not in MACHINE_TURN_CLASSES
        or not authority.user_id
        or not isinstance(authority.scope_ceiling_required, bool)
        or (not authority.scope_ceiling_required and (
            authority.turn_class not in _STANDING_CONSENT_CLASSES or scopes
        ))
        or (authority.agent_id is not None and (
            not isinstance(authority.agent_id, str) or not authority.agent_id
        ))
        or not isinstance(scopes, (list, tuple))
        or any(not isinstance(scope, str) or scope not in VALID_SCOPES for scope in scopes)
        or (tools is not None and (
            authority.turn_class not in _TOOL_LISTED_CLASSES
            or authority.agent_id is not None
            or not _valid_tools(tools)
        ))
    ):
        raise ValueError("invalid machine authority binding")
    binding = {
        **authority.machine_claims(),
        "_raw_token": authority.access_token,
        _MACHINE_SCOPE_KEY: tuple(scopes) if authority.scope_ceiling_required else None,
        "_machine_authority_agent": authority.agent_id,
    }
    if tools is not None:
        binding[_MACHINE_TOOLS_KEY] = tools
    return binding


@dataclass
class ChainBudget:
    turn_id: str
    chat_id: Optional[str] = None
    max_depth: int = DEFAULT_MAX_DELEGATION_DEPTH
    max_hops: int = DEFAULT_MAX_CHAIN_HOPS
    wall_clock_s: float = DEFAULT_CHAIN_WALL_CLOCK_S
    spent_hops: int = 0
    started_at: float = field(default_factory=time.monotonic)
    parent: Optional["ChainBudget"] = None

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def exhausted(self) -> Optional[str]:
        if self.spent_hops >= self.max_hops:
            return "hop_budget_exhausted"
        if self.elapsed_s() >= self.wall_clock_s:
            return "wall_clock_exhausted"
        if self.parent is not None:
            return self.parent.exhausted()
        return None

    def charge(self, depth: int = 1) -> Optional[str]:
        if depth > self.max_depth:
            return "depth_exceeded"
        reason = self.exhausted()
        if reason is not None:
            return reason
        self.spent_hops += 1
        if self.parent is not None:
            parent_reason = self.parent.charge(depth)
            if parent_reason is not None:
                self.spent_hops -= 1
                return parent_reason
        return None

    def slice(self, *, max_hops: Optional[int] = None,
              wall_clock_s: Optional[float] = None) -> "ChainBudget":
        return ChainBudget(
            turn_id=self.turn_id,
            chat_id=self.chat_id,
            max_depth=self.max_depth,
            max_hops=min(max_hops if max_hops is not None else self.max_hops,
                         self.max_hops),
            wall_clock_s=min(
                wall_clock_s if wall_clock_s is not None else self.wall_clock_s,
                max(self.wall_clock_s - self.elapsed_s(), 0.0)),
            parent=self,
        )


@dataclass(frozen=True)
class MachineAuthority:
    access_token: str
    allowed_scopes: List[str]
    principal: str
    user_id: str
    consent_ref: str
    turn_class: str
    agent_id: Optional[str] = None
    scope_ceiling_required: bool = True
    allowed_tools: Optional[Tuple[Tuple[str, str, str], ...]] = None

    def machine_claims(self) -> Dict[str, Any]:
        return {
            "sub": self.user_id,
            "machine_class": self.turn_class,
            "consent_ref": self.consent_ref,
        }


@dataclass(frozen=True)
class AuthoritySkip:
    reason: str
    detail: str = ""


class MachineTurnAuthority:
    def __init__(self, orchestrator) -> None:
        self.orch = orchestrator

    async def derive(
        self, *, user_id: str, agent_id: Optional[str],
        consented_scopes: Optional[List[str]], grant_id: Optional[str],
        turn_class: str,
    ) -> Union[MachineAuthority, AuthoritySkip]:
        self._log_skip(turn_class, user_id, "missing_consent")
        return AuthoritySkip("missing_consent",
                             "no durable consent exists on this node, so no machine turn can start")

    @staticmethod
    def _log_skip(turn_class: str, user_id: str, reason: str) -> None:
        logger.warning("machine_turn.skip class=%s user=%s reason=%s",
                       turn_class, user_id, reason)
