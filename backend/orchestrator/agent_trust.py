"""Feature 040 (US2) — owner-approved "safe" agent trust marker.

A safe marker is a per-agent, owner-approved provenance record (stored in the
``agent_trust`` table) that flips the per-call permission baseline from
deny→allow for that agent (consumed by
``tool_permissions.ToolPermissionManager.is_tool_allowed``). It is **not** a
runtime bypass: an explicit per-user opt-out always wins, and hard
security-flag blocks (orchestrator dispatch gate) are never cleared by it.

Marking/unmarking is admin/owner-gated server-side and audited as an
``agent_lifecycle`` event. A revision through the 027 agent-revision path
resets the marker (re-approval required), since a revision can reintroduce
un-reviewed code.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional, Protocol, Sequence

logger = logging.getLogger("AgentTrust")

#: Roles permitted to mark an agent safe (server-side gate).
PRIVILEGED_ROLES = ("admin", "owner")


class AgentTrustStore(Protocol):
    """Deep policy boundary backed by the application Plane agent catalog."""

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
    """True when ``roles`` contains an admin/owner role."""
    return any(r in (roles or []) for r in PRIVILEGED_ROLES)


def is_safe(store: AgentTrustStore, agent_id: str) -> bool:
    """Whether ``agent_id`` carries the owner-approved safe marker."""
    try:
        return bool(store.get_agent_is_safe(agent_id))
    except Exception:  # noqa: BLE001 — fail closed
        return False


async def mark_safe(store: AgentTrustStore, agent_id: str, safe: bool, actor_user: str, roles,
                    chat_id: Optional[str] = None) -> Dict[str, Any]:
    """Admin/owner-gated safe-marking. Returns a result dict.

    Returns ``{"ok": False, "error": "forbidden"}`` when ``roles`` lacks an
    admin/owner role; otherwise upserts the marker, emits an audited
    ``agent_lifecycle`` event, and returns ``{"ok": True, "agent_id", "is_safe",
    "prior"}``.
    """
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
    """Reset the safe marker when a previously-safe agent is revised.

    No-op (and no audit event) when the agent was not safe. Otherwise clears
    the marker (re-approval required) and emits a ``safe_reset`` audit event.
    """
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
    """Boot seed: mark each bundled built-in agent safe, idempotently.

    Emits one ``marked_safe`` audit event per *newly* seeded agent; agents
    already marked safe are skipped (no write, no event), so repeated boots are
    no-ops. Returns the list of agent ids newly seeded this run.
    """
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
        except Exception:  # noqa: BLE001 — never block boot on a seed failure
            logger.debug("seed_safe failed for %s", aid, exc_info=True)
    if seeded:
        logger.info("Feature 040: seeded %d built-in agent(s) safe: %s", len(seeded), seeded)
    return seeded


async def _emit_audit(actor: str, action: str, agent_id: str, prior: bool,
                      new: bool, chat_id: Optional[str]) -> None:
    """Best-effort ``agent_lifecycle`` audit event (never raises)."""
    try:
        from orchestrator.agentic_creation import _audit
        await _audit(
            user_id=actor or "system",
            action_type=action,
            description=f"agent {agent_id} {action} (prior_safe={bool(prior)} -> {bool(new)})",
            # audit_events.correlation_id is a PostgreSQL UUID. The previous
            # human-readable ``safe:<agent>`` value passed Pydantic but failed
            # at insertion time, leaving every trust transition in the retry
            # queue instead of the append-only audit chain.
            correlation_id=str(uuid.uuid4()),
            agent_id=agent_id,
            chat_id=chat_id,
            inputs_meta={"prior_state": bool(prior), "is_safe": bool(new)},
        )
    except Exception:  # noqa: BLE001
        logger.debug("agent_trust audit failed (%s)", action, exc_info=True)
