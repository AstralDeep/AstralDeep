"""Binds owner-managed A2A publication to the application Plane journal and transactional audit.
The executor uses the attached registry and policy for discovery and admission.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from shared.a2a_publication import A2APublicationRegistry

from orchestrator.a2a_publication_policy import A2APublicationPolicy
from orchestrator.a2a_publication_store import PlaneA2APublicationStore

logger = logging.getLogger("A2APublicationWiring")


def registered_audiences(orchestrator, agent_id: str) -> tuple[str, ...]:
    card = (getattr(orchestrator, "agent_cards", {}) or {}).get(agent_id)
    scopes: list[str] = []
    for skill in getattr(card, "skills", None) or []:
        scope = str(getattr(skill, "scope", "") or "")
        if scope and scope not in scopes:
            scopes.append(scope)
    return tuple(scopes)


def owner_lookup(orchestrator) -> Optional[Any]:
    owners = getattr(orchestrator, "a2a_agent_owners", None)
    if owners is None:
        owners = getattr(orchestrator, "agent_owners", None)
    if owners is not None:
        if callable(owners):
            return owners
        if isinstance(owners, dict):
            return lambda agent_id: owners.get(agent_id)
    try:
        from orchestrator.plane_repository_context import plane_source_from_orchestrator

        source = plane_source_from_orchestrator(orchestrator)
    except Exception:
        return None
    runtime = source.plane_runtime
    repositories = source.plane_repositories

    def lookup(agent_id: str) -> Optional[str]:
        with runtime.transaction() as transaction:
            record = repositories.agents.get_ownership(
                transaction,
                agent_id=agent_id,
            )
        return None if record is None else record.owner_email

    return lookup


def _plane_store(orchestrator) -> PlaneA2APublicationStore:
    from orchestrator.plane_repository_context import plane_source_from_orchestrator

    source = plane_source_from_orchestrator(orchestrator)
    return PlaneA2APublicationStore(
        runtime=source.plane_runtime,
        repositories=source.plane_repositories,
        audit=getattr(orchestrator, "audit_repo", None),
    )


def attach_publication_registry(orchestrator) -> A2APublicationRegistry:
    from orchestrator.a2a_orchestrator_executor import public_catalog_agent_ids
    from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS

    existing = getattr(orchestrator, "a2a_publications", None)
    if isinstance(existing, A2APublicationRegistry):
        _attach_policy(orchestrator, existing, public_catalog_agent_ids, FIRST_PARTY_PUBLIC_AGENT_IDS)
        return existing

    registry = A2APublicationRegistry(
        catalog=lambda: public_catalog_agent_ids(orchestrator),
        audiences=lambda agent_id: registered_audiences(orchestrator, agent_id),
        store=_plane_store(orchestrator),
    )
    registry.refresh()
    orchestrator.a2a_publications = registry
    _attach_policy(orchestrator, registry, public_catalog_agent_ids, FIRST_PARTY_PUBLIC_AGENT_IDS)
    return registry


def _attach_policy(orchestrator, registry, public_catalog_agent_ids, managed_ids) -> None:
    if isinstance(getattr(orchestrator, "a2a_publication_policy", None), A2APublicationPolicy):
        return

    def catalog():
        return public_catalog_agent_ids(orchestrator)

    orchestrator.a2a_publication_policy = A2APublicationPolicy(
        orchestrator,
        registry,
        catalog=catalog,
        owner_lookup=owner_lookup(orchestrator),
        managed=lambda: managed_ids,
    )