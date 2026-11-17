"""Read-only, query-budgeted projections joining agent, trust, and identity data for
agent-management UI surfaces. Rendering and authorization stay with the caller; built
on repositories/agents.py, identity.py, and tool_policy.py.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from astralplane.contracts import QueryExecutor
from astralplane.repositories import (
    RepositoryDataError,
    _bounded_limit,
    _required_id,
)
from astralplane.repositories.agents import AgentOwnershipRecord
from astralplane.repositories.identity import ExternalIdentityLinkRecord
from astralplane.repositories.tool_policy import ScopeState, ToolOverrideState


@dataclass(frozen=True, slots=True)
class AgentManagementListContext:
    owner_id: str
    email: str | None
    disabled_agent_ids: tuple[str, ...]
    ownership: tuple[AgentOwnershipRecord, ...]


@dataclass(frozen=True, slots=True)
class AgentManagementDetailContext:
    owner_id: str
    agent_id: str
    email: str | None
    disabled: bool
    ownership: AgentOwnershipRecord | None
    is_safe: bool
    safe_known: bool
    credential_keys: tuple[str, ...]
    scope_states: tuple[ScopeState, ...]
    tool_override_states: tuple[ToolOverrideState, ...]
    external_identity_links: tuple[ExternalIdentityLinkRecord, ...]


class AgentManagementRepository:
    def get_list_context(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        ownership_limit: int = 5000,
    ) -> AgentManagementListContext:
        owner = _required_id(owner_id, "owner_id")
        maximum = _bounded_limit(ownership_limit, maximum=5000)
        context = query.fetch_one(
            """
            WITH target AS (SELECT %s::text AS owner_id)
            SELECT identity.email, preferences.preferences
            FROM target
            LEFT JOIN users AS identity ON identity.id = target.owner_id
            LEFT JOIN user_preferences AS preferences
              ON preferences.user_id = target.owner_id
            """,
            (owner,),
        )
        if context is None:  # pragma: no cover
            raise RepositoryDataError("agent-management list context returned no row")
        preferences = _preferences(context.get("preferences"))
        rows = query.fetch_all(
            """
            SELECT agent_id, owner_email, is_public, created_at, updated_at
            FROM agent_ownership
            ORDER BY agent_id
            LIMIT %s
            """,
            (maximum + 1,),
        )
        if len(rows) > maximum:
            raise RepositoryDataError(
                "agent ownership inventory exceeds the configured bound"
            )
        return AgentManagementListContext(
            owner_id=owner,
            email=_optional_string(context.get("email"), "email"),
            disabled_agent_ids=_disabled_agents(preferences),
            ownership=tuple(_ownership(row) for row in rows),
        )

    def get_detail_context(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        agent_id: str,
        credential_limit: int = 1000,
        scope_limit: int = 256,
        tool_override_limit: int = 5000,
        external_identity_limit: int = 100,
    ) -> AgentManagementDetailContext:
        owner = _required_id(owner_id, "owner_id")
        agent = _required_id(agent_id, "agent_id", maximum=512)
        credential_max = _bounded_limit(credential_limit, maximum=1000)
        scope_max = _bounded_limit(scope_limit, maximum=1000)
        override_max = _bounded_limit(tool_override_limit, maximum=5000)
        external_max = _bounded_limit(external_identity_limit, maximum=100)
        context = query.fetch_one(
            """
            WITH target AS (
                SELECT %s::text AS owner_id, %s::text AS agent_id
            )
            SELECT identity.email, preferences.preferences,
                   ownership.agent_id AS ownership_agent_id,
                   ownership.owner_email, ownership.is_public,
                   ownership.created_at AS ownership_created_at,
                   ownership.updated_at AS ownership_updated_at,
                   trust.agent_id AS trust_agent_id, trust.is_safe
            FROM target
            LEFT JOIN users AS identity ON identity.id = target.owner_id
            LEFT JOIN user_preferences AS preferences
              ON preferences.user_id = target.owner_id
            LEFT JOIN agent_ownership AS ownership
              ON ownership.agent_id = target.agent_id
            LEFT JOIN agent_trust AS trust ON trust.agent_id = target.agent_id
            """,
            (owner, agent),
        )
        if context is None:  # pragma: no cover
            raise RepositoryDataError("agent-management detail context returned no row")
        credentials = query.fetch_all(
            """
            SELECT credential_key
            FROM user_credentials
            WHERE user_id = %s AND agent_id = %s
            ORDER BY credential_key, id
            LIMIT %s
            """,
            (owner, agent, credential_max + 1),
        )
        if len(credentials) > credential_max:
            raise RepositoryDataError("agent credential-key inventory exceeds its bound")
        policy_rows = query.fetch_all(
            """
            WITH bounded_scopes AS (
                SELECT 'scope'::text AS state_kind,
                       user_id, agent_id, scope,
                       NULL::text AS tool_name,
                       NULL::text AS permission_kind,
                       enabled, updated_at
                FROM agent_scopes
                WHERE user_id = %s AND agent_id = %s
                ORDER BY scope
                LIMIT %s
            ),
            bounded_overrides AS (
                SELECT 'override'::text AS state_kind,
                       user_id, agent_id, NULL::text AS scope,
                       tool_name, permission_kind, enabled, updated_at
                FROM tool_overrides
                WHERE user_id = %s AND agent_id = %s
                ORDER BY tool_name, permission_kind NULLS FIRST
                LIMIT %s
            )
            SELECT state_kind, user_id, agent_id, scope, tool_name,
                   permission_kind, enabled, updated_at
            FROM bounded_scopes
            UNION ALL
            SELECT state_kind, user_id, agent_id, scope, tool_name,
                   permission_kind, enabled, updated_at
            FROM bounded_overrides
            ORDER BY state_kind, scope NULLS FIRST, tool_name NULLS FIRST,
                     permission_kind NULLS FIRST
            """,
            (
                owner,
                agent,
                scope_max + 1,
                owner,
                agent,
                override_max + 1,
            ),
        )
        scopes = [row for row in policy_rows if row.get("state_kind") == "scope"]
        overrides = [
            row for row in policy_rows if row.get("state_kind") == "override"
        ]
        if len(scopes) + len(overrides) != len(policy_rows):
            raise RepositoryDataError("agent policy inventory returned an invalid row kind")
        if len(scopes) > scope_max:
            raise RepositoryDataError("agent scope inventory exceeds its bound")
        if len(overrides) > override_max:
            raise RepositoryDataError("agent tool-override inventory exceeds its bound")
        preferences = _preferences(context.get("preferences"))
        ownership = (
            None
            if context.get("ownership_agent_id") is None
            else AgentOwnershipRecord(
                agent_id=str(context["ownership_agent_id"]),
                owner_email=str(context["owner_email"]),
                is_public=bool(context["is_public"]),
                created_at=_optional_integer(
                    context.get("ownership_created_at"), "ownership_created_at"
                ),
                updated_at=_optional_integer(
                    context.get("ownership_updated_at"), "ownership_updated_at"
                ),
            )
        )
        trust_known = context.get("trust_agent_id") is not None
        disabled = agent in _disabled_agents(preferences)
        return AgentManagementDetailContext(
            owner_id=owner,
            agent_id=agent,
            email=_optional_string(context.get("email"), "email"),
            disabled=disabled,
            ownership=ownership,
            is_safe=bool(context.get("is_safe")) if trust_known else False,
            safe_known=trust_known,
            credential_keys=tuple(
                str(row["credential_key"]) for row in credentials
            ),
            scope_states=tuple(
                _scope(row, owner_id=owner, agent_id=agent) for row in scopes
            ),
            tool_override_states=tuple(
                _tool_override(row, owner_id=owner, agent_id=agent)
                for row in overrides
            ),
            external_identity_links=_external_links(
                owner,
                preferences,
                limit=external_max,
            ),
        )


def _preferences(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise RepositoryDataError("persisted user preferences are not valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise RepositoryDataError("persisted user preferences must be an object")
    return dict(decoded)


def _disabled_agents(preferences: Mapping[str, Any]) -> tuple[str, ...]:
    value = preferences.get("disabled_agents")
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(agent_id, str) or not agent_id for agent_id in value
    ):
        raise RepositoryDataError("persisted disabled_agents must be a string array")
    return tuple(sorted(set(value)))


def _ownership(row: Mapping[str, Any]) -> AgentOwnershipRecord:
    return AgentOwnershipRecord(
        agent_id=str(row["agent_id"]),
        owner_email=str(row["owner_email"]),
        is_public=bool(row["is_public"]),
        created_at=_optional_integer(row.get("created_at"), "created_at"),
        updated_at=_optional_integer(row.get("updated_at"), "updated_at"),
    )


def _scope(
    row: Mapping[str, Any], *, owner_id: str, agent_id: str
) -> ScopeState:
    enabled = row.get("enabled")
    if not isinstance(enabled, bool):
        raise RepositoryDataError("persisted agent scope state must be boolean")
    if row.get("user_id") != owner_id or row.get("agent_id") != agent_id:
        raise RepositoryDataError("agent scope state crossed its owner fence")
    return ScopeState(
        owner_id=owner_id,
        agent_id=agent_id,
        scope=_required_string(row.get("scope"), "scope"),
        enabled=enabled,
        updated_at=_optional_integer(row.get("updated_at"), "updated_at"),
    )


def _tool_override(
    row: Mapping[str, Any], *, owner_id: str, agent_id: str
) -> ToolOverrideState:
    enabled = row.get("enabled")
    if not isinstance(enabled, bool):
        raise RepositoryDataError("persisted tool override state must be boolean")
    if row.get("user_id") != owner_id or row.get("agent_id") != agent_id:
        raise RepositoryDataError("tool override state crossed its owner fence")
    return ToolOverrideState(
        owner_id=owner_id,
        agent_id=agent_id,
        tool_name=_required_string(row.get("tool_name"), "tool_name"),
        permission_kind=_optional_string(
            row.get("permission_kind"), "permission_kind"
        ),
        enabled=enabled,
        updated_at=_optional_integer(row.get("updated_at"), "updated_at"),
    )


def _external_links(
    owner_id: str,
    preferences: Mapping[str, Any],
    *,
    limit: int,
) -> tuple[ExternalIdentityLinkRecord, ...]:
    raw = preferences.get("verified_external_identities")
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise RepositoryDataError("persisted external identity links must be an object")
    if len(raw) > limit:
        raise RepositoryDataError("external identity link inventory exceeds its bound")
    links: list[ExternalIdentityLinkRecord] = []
    for provider, value in sorted(raw.items(), key=lambda item: str(item[0])):
        if not isinstance(provider, str) or not provider or not isinstance(value, Mapping):
            raise RepositoryDataError("persisted external identity link is invalid")
        subject = value.get("subject")
        issuer = value.get("issuer")
        agent_id = value.get("verified_by_agent")
        verified_at = value.get("verified_at")
        if (
            not isinstance(subject, str)
            or not subject
            or not isinstance(issuer, str)
            or not issuer
            or not isinstance(agent_id, str)
            or not agent_id
            or isinstance(verified_at, bool)
            or not isinstance(verified_at, int)
            or verified_at < 0
        ):
            raise RepositoryDataError("persisted external identity link is invalid")
        links.append(
            ExternalIdentityLinkRecord(
                owner_id=owner_id,
                agent_id=agent_id,
                provider=provider,
                subject=subject,
                issuer=issuer,
                verified_at=verified_at,
            )
        )
    return tuple(links)


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RepositoryDataError(f"persisted {field} must be a string")
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RepositoryDataError(f"persisted {field} must be a non-empty string")
    return value


def _optional_integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepositoryDataError(f"persisted {field} must be a non-negative integer")
    return value


__all__ = (
    "AgentManagementDetailContext",
    "AgentManagementListContext",
    "AgentManagementRepository",
)
