"""Single-source tool visibility used by chat and external projections."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from orchestrator.agent_identity import identity_requirement_satisfied
from orchestrator.plane_repository_context import plane_source_from_orchestrator

ExclusionLogger = Callable[[str, str | None, str], None]

# The remote-compute catalog is 18 verbs (~2,700 prompt tokens); 17 of them
# dead-end at "this machine is not in your inventory" for a user with no
# registered machine. Only list_machines stays visible machineless — it is
# the discovery verb whose empty-state reply points at Settings → Remote
# machines (registration itself is a chrome surface, not a tool).
_REMOTE_COMPUTE_AGENT_ID = "remote-compute-1"
_REMOTE_DISCOVERY_SKILL_IDS = frozenset({"list_machines"})


def eligible_tool_pairs(
    orchestrator: Any,
    user_id: str,
    *,
    disabled_agents: Iterable[str] = (),
    draft_agent_id: str | None = None,
    selected_tools: set[str] | None = None,
    identity_claims: dict[str, Any] | None = None,
    log_exclusion: ExclusionLogger | None = None,
) -> list[tuple[str, Any]]:
    """Return the live agent/skill pairs that chat may offer to ``user_id``.

    This is deliberately synchronous because callers run database-backed
    permission evaluation off the event loop. External protocol projections
    use the same predicate so they cannot become a broader catalog than chat.

    Args:
        orchestrator: Runtime holding registered agents and authorization state.
        user_id: Human principal whose permissions are evaluated.
        disabled_agents: Agents the principal disabled in preferences.
        draft_agent_id: Optional draft under owner-isolated self-test.
        selected_tools: Optional chat picker restriction; it only subtracts.
        identity_claims: Verified access-token claims for identity-bound agents.
        log_exclusion: Optional callback receiving agent, skill, and reason.

    Returns:
        Ordered ``(agent_id, skill)`` pairs that survive every visibility gate.
    """

    disabled = set(disabled_agents)
    eligible: list[tuple[str, Any]] = []

    def excluded(agent_id: str, skill_id: str | None, reason: str) -> None:
        if log_exclusion is not None:
            log_exclusion(agent_id, skill_id, reason)

    machineless: bool | None = None

    def remote_machineless() -> bool:
        nonlocal machineless
        if machineless is None:
            from orchestrator import remote_machines

            try:
                machineless = not remote_machines.owns_any_machine(
                    plane_source_from_orchestrator(orchestrator),
                    user_id,
                )
            except Exception:
                # Fail open to the full catalog: this subtraction is a prompt
                # cost optimization, and the dispatch permission gate still
                # runs. A transient DB error must not blank the tool list.
                machineless = False
        return machineless

    for agent_id, card in orchestrator.agent_cards.items():
        if agent_id not in orchestrator.agents and agent_id not in orchestrator.local_agents:
            excluded(agent_id, None, "not_connected")
            continue
        if draft_agent_id and agent_id != draft_agent_id:
            excluded(agent_id, None, "outside_draft_test")
            continue
        if not draft_agent_id and orchestrator._is_draft_agent(agent_id):
            excluded(agent_id, None, "draft_not_live")
            continue
        if agent_id in disabled:
            excluded(agent_id, None, "user_disabled_agent")
            continue

        if not identity_requirement_satisfied(card, identity_claims):
            excluded(agent_id, None, "missing_required_identity")
            continue

        draft_self_test = draft_agent_id is not None and agent_id == draft_agent_id
        agent_flags = orchestrator.security_flags.get(agent_id, {})
        for skill in card.skills:
            skill_id = getattr(skill, "id", None)
            if not skill_id:
                excluded(agent_id, None, "missing_skill_id")
                continue
            if agent_flags.get(skill_id, {}).get("blocked"):
                excluded(agent_id, skill_id, "system_blocked")
                continue
            if (
                agent_id == _REMOTE_COMPUTE_AGENT_ID
                and skill_id not in _REMOTE_DISCOVERY_SKILL_IDS
                and remote_machineless()
            ):
                excluded(agent_id, skill_id, "no_registered_machine")
                continue
            if not draft_self_test and not orchestrator.tool_permissions.is_tool_allowed(
                user_id,
                agent_id,
                skill_id,
            ):
                excluded(agent_id, skill_id, "scope_or_override")
                continue
            if selected_tools is not None and skill_id not in selected_tools:
                excluded(agent_id, skill_id, "user_selection")
                continue
            eligible.append((agent_id, skill))
    return eligible


def enabled_scope_union(orchestrator: Any, user_id: str) -> list[str]:
    """Return the union of EFFECTIVE scopes across every agent chat may offer.

    An agent-less machine turn (a scheduled job proposed without a specific
    agent) runs as an ordinary assistant turn, so the per-tool permission gate
    routes its tool calls across ALL of the user's eligible agents. The consent
    it needs — and the containment ``MachineTurnAuthority.derive`` computes —
    is therefore the union of ``get_enabled_scope_names`` over exactly the
    agents :func:`eligible_tool_pairs` admits for this user: live, non-draft,
    connected, not user-disabled, nothing identity-bound (a machine turn
    carries no verified identity claims). Fail-closed to ``[]`` on any error,
    ordered by ``VALID_SCOPES`` so the list is stable for audit rows.
    """
    from orchestrator.tool_permissions import VALID_SCOPES

    try:
        disabled = set(orchestrator.tool_permissions.list_disabled_agents(user_id))
        pairs = eligible_tool_pairs(
            orchestrator, user_id, disabled_agents=disabled, identity_claims=None)
        agent_ids: list[str] = []
        for agent_id, _skill in pairs:
            if agent_id not in agent_ids:
                agent_ids.append(agent_id)
        union: set[str] = set()
        for agent_id in agent_ids:
            names = orchestrator.tool_permissions.get_enabled_scope_names(
                user_id, agent_id) or []
            union.update(str(name) for name in names)
    except Exception as exc:
        # Fail-closed: consent/containment must never widen on an error.
        # The empty union captures no scopes and asserts none at run time.
        import logging

        logging.getLogger(__name__).warning(
            "enabled_scope_union failed user=%s: %s", user_id, exc)
        return []
    return [scope for scope in VALID_SCOPES if scope in union]


__all__ = ["eligible_tool_pairs", "enabled_scope_union"]
