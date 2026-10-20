"""Single server policy for A2A discovery and callable eligibility: published, safe,
connected catalog capabilities with owner/admin management checks. It gates the public
card, authenticated tool listing, direct A2A admission, and the publication REST API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from shared.a2a_publication import A2APublicationRegistry, PublicationRecord

logger = logging.getLogger("A2APublicationPolicy")


@dataclass(frozen=True, slots=True)
class PublicationAdmission:
    publication_id: str
    agent_id: str
    audiences: tuple[str, ...]
    expires_at: Optional[float]


class A2APublicationPolicy:
    def __init__(
        self,
        orchestrator: Any,
        registry: A2APublicationRegistry,
        *,
        catalog: Callable[[], Iterable[str]],
        owner_lookup: Optional[Callable[[str], Optional[str]]] = None,
        managed: Optional[Callable[[], Iterable[str]]] = None,
    ):
        self.orchestrator = orchestrator
        self.registry = registry
        self._catalog = catalog
        self._owner_lookup = owner_lookup
        self._managed = managed or catalog
        self._managed_failed = False

    def catalog_ids(self) -> tuple[str, ...]:
        try:
            return tuple(dict.fromkeys(str(item) for item in self._catalog()))
        except Exception:
            logger.warning("A2A publication catalog could not be resolved", exc_info=True)
            return ()

    def managed_ids(self) -> frozenset[str]:
        try:
            self._managed_failed = False
            return frozenset(str(item) for item in self._managed())
        except Exception:
            self._managed_failed = True
            logger.warning("A2A publication managed surfaces could not be resolved", exc_info=True)
            return frozenset()

    def published_records(self) -> tuple[PublicationRecord, ...]:
        catalog = set(self.catalog_ids())
        try:
            active = self.registry.active()
        except Exception:
            logger.warning("A2A publication state could not be read", exc_info=True)
            return ()
        return tuple(record for record in active if record.agent_id in catalog
                     and any(scope in self._registered_scopes(record.agent_id)
                             for scope in record.audiences)
                     and (record.owner is None or record.owner == self.owner_of(record.agent_id)))

    def published_agent_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(record.agent_id for record in self.published_records()))

    def admit(self, agent_id: str, skill_id: Optional[str] = None) -> Optional[PublicationAdmission]:
        from orchestrator.mcp_projection import FRAMEWORK_WORK_AGENT_ID

        if agent_id == FRAMEWORK_WORK_AGENT_ID:
            return PublicationAdmission(
                publication_id="framework-work-tools",
                agent_id=agent_id,
                audiences=(),
                expires_at=None,
            )
        managed = self.managed_ids()
        if self._managed_failed:
            return None
        if agent_id not in managed:
            return PublicationAdmission(
                publication_id="unmanaged-a2a-surface",
                agent_id=agent_id,
                audiences=(),
                expires_at=None,
            )
        record = self.registry.active_publication(agent_id)
        if (record is None or agent_id not in set(self.catalog_ids())
                or (record.owner is not None and record.owner != self.owner_of(agent_id))):
            return None
        scopes = tuple(scope for scope in record.audiences if scope in self._registered_scopes(agent_id))
        if not scopes or (skill_id is not None and self._skill_scope(agent_id, skill_id) not in scopes):
            return None
        return PublicationAdmission(
            publication_id=record.publication_id,
            agent_id=record.agent_id,
            audiences=scopes,
            expires_at=record.expires_at,
        )

    def _registered_scopes(self, agent_id: str) -> tuple[str, ...]:
        card = (getattr(self.orchestrator, "agent_cards", {}) or {}).get(agent_id)
        return tuple(str(skill.scope) for skill in getattr(card, "skills", ()) or ()
                     if getattr(skill, "scope", None))

    def _skill_scope(self, agent_id: str, skill_id: str) -> Optional[str]:
        card = (getattr(self.orchestrator, "agent_cards", {}) or {}).get(agent_id)
        for skill in getattr(card, "skills", ()) or ():
            if skill.id == skill_id:
                return getattr(skill, "scope", None) or None
        return None

    def filter_projected_tools(self, tools: Iterable[Any]) -> tuple[Any, ...]:
        return tuple(tool for tool in tools if self.admit(tool.agent_id, tool.skill_id) is not None)

    def owner_of(self, agent_id: str) -> Optional[str]:
        if self._owner_lookup is None:
            return None
        try:
            owner = self._owner_lookup(agent_id)
        except Exception:
            logger.warning("A2A publication owner lookup failed", exc_info=True)
            return None
        return None if owner is None else str(owner)

    def can_manage(self, actor_emails: Iterable[str], agent_id: str, *, is_admin: bool = False) -> bool:
        if is_admin:
            return agent_id in set(self.catalog_ids())
        if agent_id not in set(self.catalog_ids()):
            return False
        owner = self.owner_of(agent_id)
        return bool(owner and owner in {str(item) for item in actor_emails})

    def record_denial(self, *, actor: str, agent_id: str, reason: str) -> None:
        self.registry.record_event({
            "action": "invocation_denied",
            "agent_id": str(agent_id or ""),
            "actor": str(actor or ""),
            "reason": str(reason or "")[:160],
        })