"""Single source of truth for which agent/skill tool pairs a user may see, shared by
chat and external protocol projections (mcp_projection.py) so no external surface can
expose a broader catalog than chat.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from orchestrator.agent_identity import identity_requirement_satisfied
from orchestrator.plane_repository_context import plane_source_from_orchestrator

ExclusionLogger = Callable[[str, str | None, str], None]

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
        # Fails closed here — an empty union asserts no scopes
        import logging

        logging.getLogger(__name__).warning(
            "enabled_scope_union failed user=%s: %s", user_id, exc)
        return []
    return [scope for scope in VALID_SCOPES if scope in union]


__all__ = ["eligible_tool_pairs", "enabled_scope_union"]
