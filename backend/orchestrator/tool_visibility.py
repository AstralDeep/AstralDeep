"""Single-source tool visibility used by chat and external projections."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from orchestrator.agent_identity import identity_requirement_satisfied

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
                    orchestrator.history.db,
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


__all__ = ["eligible_tool_pairs"]
