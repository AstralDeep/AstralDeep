"""Persists detached Keycloak/OIDC identity claims and external-identity links, scoped
to the caller-supplied OIDC subject. Does not authenticate or authorize; used by
orchestrator/external_identity_links.py and repositories/agent_management.py.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from astralplane.contracts import Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _required_id,
    _row_value,
)


@dataclass(frozen=True, slots=True)
class IdentityRecord:
    owner_id: str
    email: str | None
    username: str | None
    display_name: str | None
    roles: tuple[str, ...]
    last_login_at: int | None
    created_at: int | None
    updated_at: int | None


@dataclass(frozen=True, slots=True)
class ExternalIdentityLinkRecord:
    owner_id: str
    agent_id: str
    provider: str
    subject: str
    issuer: str
    verified_at: int


class ExternalIdentityAlreadyLinkedError(RepositoryConflictError):
    default_code = "external_identity_already_linked"


class ExternalIdentityNonceReplayError(RepositoryConflictError):
    default_code = "external_identity_nonce_replay"


class IdentityRepository:
    def upsert_identity(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        observed_at: int,
        email: str | None = None,
        username: str | None = None,
        display_name: str | None = None,
        roles: Iterable[str] | None = None,
    ) -> IdentityRecord:
        owner_id = _required_id(owner_id, "owner_id")
        observed_at = _non_negative_int(observed_at, "observed_at")
        email = _optional_text(email, "email", 1024)
        username = _optional_text(username, "username", 512)
        display_name = _optional_text(display_name, "display_name", 1024)
        roles_json = None if roles is None else _roles_json(roles)
        row = transaction.fetch_one(
            """
            INSERT INTO users (
                id, email, username, display_name, roles,
                last_login_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                email = COALESCE(EXCLUDED.email, users.email),
                username = COALESCE(EXCLUDED.username, users.username),
                display_name = COALESCE(EXCLUDED.display_name, users.display_name),
                roles = COALESCE(EXCLUDED.roles, users.roles),
                last_login_at = EXCLUDED.last_login_at,
                updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            (
                owner_id,
                email,
                username,
                display_name,
                roles_json,
                observed_at,
                observed_at,
                observed_at,
            ),
        )
        if row is None:  # pragma: no cover
            raise RepositoryDataError("identity upsert returned no row")
        return _identity(row)

    def get_identity(self, transaction: Transaction, *, owner_id: str) -> IdentityRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        row = transaction.fetch_one("SELECT * FROM users WHERE id = %s", (owner_id,))
        return None if row is None else _identity(row)

    def list_identities_for_administration(
        self, transaction: Transaction, *, limit: int = 200
    ) -> tuple[IdentityRecord, ...]:
        limit = _bounded_limit(limit, maximum=1000)
        rows = transaction.fetch_all(
            """
            SELECT * FROM users
            ORDER BY last_login_at DESC NULLS LAST, id
            LIMIT %s
            """,
            (limit,),
        )
        return tuple(_identity(row) for row in rows)

    def store_verified_external_identity(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        provider: str,
        subject: str,
        issuer: str,
        state_nonce: str,
        observed_at: int,
        nonce_ttl_seconds: int = 300,
        nonce_cap: int = 10,
    ) -> ExternalIdentityLinkRecord:
        owner = _required_id(owner_id, "owner_id")
        agent = _required_id(agent_id, "agent_id", maximum=512)
        provider_name = _bounded_text(provider, "provider", maximum=128)
        external_subject = _bounded_text(subject, "subject", maximum=512)
        external_issuer = _bounded_text(issuer, "issuer", maximum=2048)
        nonce = _bounded_text(state_nonce, "state_nonce", maximum=2048)
        observed = _non_negative_int(observed_at, "observed_at")
        ttl = _bounded_positive(nonce_ttl_seconds, "nonce_ttl_seconds", maximum=86_400)
        cap = _bounded_positive(nonce_cap, "nonce_cap", maximum=100)
        transaction.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtext(%s)) AS locked",
            (f"external-identity:{provider_name}:{external_subject}",),
        )
        rows = transaction.fetch_all(
            """
            SELECT user_id, preferences FROM user_preferences
            WHERE user_id = %s
               OR (preferences::jsonb -> 'verified_external_identities' -> %s
                   ->> 'subject') = %s
            FOR UPDATE
            """,
            (owner, provider_name, external_subject),
        )
        target_preferences: dict[str, Any] = {}
        for row in rows:
            row_owner = str(_row_value(row, "user_id"))
            preferences = _preferences(row.get("preferences"))
            links = preferences.get("verified_external_identities")
            link = links.get(provider_name) if isinstance(links, Mapping) else None
            if (
                isinstance(link, Mapping)
                and link.get("subject") == external_subject
                and row_owner != owner
            ):
                raise ExternalIdentityAlreadyLinkedError(
                    "external identity is linked to another owner",
                    metadata={"provider": provider_name},
                )
            if row_owner == owner:
                target_preferences = preferences

        raw_links = target_preferences.get("verified_external_identities")
        links = dict(raw_links) if isinstance(raw_links, Mapping) else {}
        existing = links.get(provider_name)
        raw_nonces = existing.get("recent_link_nonces") if isinstance(existing, Mapping) else ()
        cutoff = max(0, observed - ttl)
        recent: list[dict[str, object]] = []
        if isinstance(raw_nonces, (list, tuple)):
            for item in raw_nonces:
                if not isinstance(item, Mapping):
                    continue
                prior_nonce = item.get("nonce")
                used_at = item.get("used_at")
                if (
                    isinstance(prior_nonce, str)
                    and isinstance(used_at, int)
                    and not isinstance(used_at, bool)
                    and used_at >= cutoff
                ):
                    recent.append({"nonce": prior_nonce, "used_at": used_at})
        if any(item["nonce"] == nonce for item in recent):
            raise ExternalIdentityNonceReplayError(
                "external identity state nonce was already used",
                metadata={"provider": provider_name},
            )
        recent.append({"nonce": nonce, "used_at": observed})
        links[provider_name] = {
            "issuer": external_issuer,
            "recent_link_nonces": recent[-cap:],
            "subject": external_subject,
            "verified_at": observed,
            "verified_by_agent": agent,
        }
        target_preferences["verified_external_identities"] = links
        result = transaction.execute(
            """
            INSERT INTO user_preferences (user_id, preferences, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                preferences = EXCLUDED.preferences,
                updated_at = EXCLUDED.updated_at
            """,
            (
                owner,
                json.dumps(
                    target_preferences,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                observed * 1000,
            ),
        )
        if result.rowcount != 1:
            raise RepositoryDataError(
                "external identity preference write did not affect exactly one row"
            )
        return ExternalIdentityLinkRecord(
            owner_id=owner,
            agent_id=agent,
            provider=provider_name,
            subject=external_subject,
            issuer=external_issuer,
            verified_at=observed,
        )

    def get_external_identity(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        provider: str,
    ) -> ExternalIdentityLinkRecord | None:
        owner = _required_id(owner_id, "owner_id")
        provider_name = _bounded_text(provider, "provider", maximum=128)
        row = transaction.fetch_one(
            "SELECT preferences FROM user_preferences WHERE user_id = %s",
            (owner,),
        )
        if row is None:
            return None
        links = _preferences(row.get("preferences")).get("verified_external_identities")
        entry = links.get(provider_name) if isinstance(links, Mapping) else None
        return None if entry is None else _external_link(owner, provider_name, entry)

    def list_external_identities(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        limit: int = 20,
    ) -> tuple[ExternalIdentityLinkRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        maximum = _bounded_limit(limit, maximum=100)
        row = transaction.fetch_one(
            "SELECT preferences FROM user_preferences WHERE user_id = %s",
            (owner,),
        )
        if row is None:
            return ()
        links = _preferences(row.get("preferences")).get("verified_external_identities")
        if links is None:
            return ()
        if not isinstance(links, Mapping):
            raise RepositoryDataError("persisted external identity links must be an object")
        selected = sorted(links.items(), key=lambda item: str(item[0]))[:maximum]
        return tuple(_external_link(owner, str(provider), entry) for provider, entry in selected)


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum=maximum, allow_empty=True)


def _roles_json(values: Iterable[str]) -> str:
    if isinstance(values, (str, bytes, bytearray)):
        raise RepositoryValidationError("roles must be an iterable of role names")
    roles: list[str] = []
    seen: set[str] = set()
    for value in values:
        role = _bounded_text(value, "role", maximum=256)
        if role not in seen:
            roles.append(role)
            seen.add(role)
    return json.dumps(roles, ensure_ascii=False, separators=(",", ":"))


def _identity(row: Mapping[str, Any]) -> IdentityRecord:
    raw_roles = row.get("roles")
    if raw_roles is None:
        roles: tuple[str, ...] = ()
    else:
        try:
            decoded = json.loads(raw_roles) if isinstance(raw_roles, str) else raw_roles
        except json.JSONDecodeError as exc:
            raise RepositoryDataError("persisted identity roles are not valid JSON") from exc
        if not isinstance(decoded, (list, tuple)) or any(
            not isinstance(value, str) for value in decoded
        ):
            raise RepositoryDataError("persisted identity roles must be a string array")
        roles = tuple(decoded)
    return IdentityRecord(
        owner_id=str(_row_value(row, "id")),
        email=None if row.get("email") is None else str(row["email"]),
        username=None if row.get("username") is None else str(row["username"]),
        display_name=(None if row.get("display_name") is None else str(row["display_name"])),
        roles=roles,
        last_login_at=_optional_int(row.get("last_login_at")),
        created_at=_optional_int(row.get("created_at")),
        updated_at=_optional_int(row.get("updated_at")),
    )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _preferences(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise RepositoryDataError("persisted user preferences are not valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise RepositoryDataError("persisted user preferences must be an object")
    return {str(key): item for key, item in decoded.items()}


def _external_link(
    owner_id: str, provider: str, value: object
) -> ExternalIdentityLinkRecord:
    if not isinstance(value, Mapping):
        raise RepositoryDataError("persisted external identity link must be an object")
    try:
        agent_id = _required_id(value.get("verified_by_agent"), "verified_by_agent")
        subject = _bounded_text(value.get("subject"), "subject", maximum=512)
        issuer = _bounded_text(value.get("issuer"), "issuer", maximum=2048)
        verified_at = _non_negative_int(value.get("verified_at"), "verified_at")
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted external identity link is invalid") from exc
    return ExternalIdentityLinkRecord(
        owner_id=owner_id,
        agent_id=agent_id,
        provider=provider,
        subject=subject,
        issuer=issuer,
        verified_at=verified_at,
    )


def _bounded_positive(value: object, field: str, *, maximum: int) -> int:
    result = _non_negative_int(value, field)
    if result < 1 or result > maximum:
        raise RepositoryValidationError(
            f"{field} must be between 1 and {maximum}", metadata={"maximum": maximum}
        )
    return result


__all__ = (
    "ExternalIdentityAlreadyLinkedError",
    "ExternalIdentityLinkRecord",
    "ExternalIdentityNonceReplayError",
    "IdentityRecord",
    "IdentityRepository",
)
