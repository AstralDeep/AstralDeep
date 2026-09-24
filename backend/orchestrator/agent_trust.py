"""Owner-approved safe-agent marker flipping
tool_permissions.ToolPermissionManager.is_tool_allowed's per-call baseline from deny
to allow for one agent; admin/owner-gated, audited, and reset whenever
agentic_creation revises the agent.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional, Protocol, Sequence

logger = logging.getLogger("AgentTrust")

PRIVILEGED_ROLES = ("admin", "owner")


class AgentTrustStore(Protocol):
    def get_agent_is_safe(self, agent_id: str) -> bool: ...

    def upsert_agent_safe(
        self,
        agent_id: str,
        is_safe: bool,
        *,
        marked_by: str,
    ) -> bool: ...

    def reset_agent_safe(self, agent_id: str, *, marked_by: str) -> bool: ...


def is_privileged(roles) -> bool:
    return any(r in (roles or []) for r in PRIVILEGED_ROLES)


def is_safe(store: AgentTrustStore, agent_id: str) -> bool:
    try:
        return bool(store.get_agent_is_safe(agent_id))
    except Exception:  # noqa: BLE001
        return False


async def mark_safe(store: AgentTrustStore, agent_id: str, safe: bool, actor_user: str, roles,
                    chat_id: Optional[str] = None) -> Dict[str, Any]:
    if not is_privileged(roles):
        logger.warning("mark_safe denied: user=%s lacks privilege (agent=%s)", actor_user, agent_id)
        return {"ok": False, "error": "forbidden"}
    prior = await asyncio.to_thread(
        store.upsert_agent_safe,
        agent_id,
        bool(safe),
        marked_by=actor_user or "unknown",
    )
    action = "marked_safe" if safe else "unmarked_safe"
    await _emit_audit(actor_user, action, agent_id, prior, bool(safe), chat_id)
    return {"ok": True, "agent_id": agent_id, "is_safe": bool(safe), "prior": bool(prior)}


async def reset_on_revision(store: AgentTrustStore, agent_id: str, actor_user: str = "system",
                            chat_id: Optional[str] = None) -> Dict[str, Any]:
    if not await asyncio.to_thread(is_safe, store, agent_id):
        return {"ok": True, "reset": False}
    prior = await asyncio.to_thread(
        store.reset_agent_safe,
        agent_id,
        marked_by=actor_user or "system",
    )
    await _emit_audit(actor_user, "safe_reset", agent_id, prior, False, chat_id)
    return {"ok": True, "reset": True, "prior": bool(prior)}


async def seed_safe(
    store: AgentTrustStore,
    agent_ids: Sequence[str],
    marked_by: str = "system",
) -> list:
    seeded = []
    for aid in agent_ids:
        try:
            if await asyncio.to_thread(store.get_agent_is_safe, aid):
                continue
            await asyncio.to_thread(
                store.upsert_agent_safe,
                aid,
                True,
                marked_by=marked_by,
            )
            await _emit_audit(marked_by, "marked_safe", aid, False, True, None)
            seeded.append(aid)
        except Exception:  # noqa: BLE001
            logger.debug("seed_safe failed for %s", aid, exc_info=True)
    if seeded:
        logger.info("Feature 040: seeded %d built-in agent(s) safe: %s", len(seeded), seeded)
    return seeded


async def _emit_audit(actor: str, action: str, agent_id: str, prior: bool,
                      new: bool, chat_id: Optional[str]) -> None:
    try:
        from orchestrator.agentic_creation import _audit
        await _audit(
            user_id=actor or "system",
            action_type=action,
            description=f"agent {agent_id} {action} (prior_safe={bool(prior)} -> {bool(new)})",
            correlation_id=str(uuid.uuid4()),
            agent_id=agent_id,
            chat_id=chat_id,
            inputs_meta={"prior_state": bool(prior), "is_safe": bool(new)},
        )
    except Exception:  # noqa: BLE001
        logger.debug("agent_trust audit failed (%s)", action, exc_info=True)
