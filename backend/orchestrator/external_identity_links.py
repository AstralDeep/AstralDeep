"""Browser-mediated external identity links for restricted agents.

Astral authentication remains owned by Keycloak.  A trusted external agent may
separately verify an identity (currently an ORCID iD) and return a short-lived,
signed assertion.  The resulting link is stored inside the user's existing
preferences row so this feature is additive and does not require a schema
migration.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Mapping
from typing import Any

from astralplane.repositories.identity import (
    ExternalIdentityAlreadyLinkedError,
    ExternalIdentityNonceReplayError,
)
from orchestrator.agent_identity import normalize_orcid
from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from

LINK_SECRETS_ENV = "EXTERNAL_IDENTITY_LINK_SECRETS"
PREFERENCES_KEY = "verified_external_identities"
INTERNAL_CLAIMS_KEY = "_verified_external_identities"
STATE_TYPE = "identity-link-state"
ASSERTION_TYPE = "identity-link-assertion"
TOKEN_VERSION = 1
STATE_LIFETIME_SECONDS = 300
ASSERTION_LIFETIME_SECONDS = 120


class IdentityLinkError(ValueError):
    """A link request or assertion is invalid and must fail closed."""


class IdentityAlreadyLinkedError(IdentityLinkError):
    """The verified identity belongs to a different Astral account."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise IdentityLinkError("Invalid identity-link token")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise IdentityLinkError("Invalid identity-link token") from exc


def parse_link_secrets(raw: str | None) -> dict[str, bytes]:
    """Parse the operator-owned ``agent_id -> secret`` JSON map strictly."""
    if raw is None or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}

    result: dict[str, bytes] = {}
    for agent_id, secret in decoded.items():
        if (
            not isinstance(agent_id, str)
            or not agent_id
            or agent_id != agent_id.strip()
            or len(agent_id) > 128
            or not isinstance(secret, str)
            or len(secret.encode("utf-8")) < 32
        ):
            return {}
        result[agent_id] = secret.encode("utf-8")
    return result


def link_secret_for(agent_id: str) -> bytes | None:
    return parse_link_secrets(os.getenv(LINK_SECRETS_ENV)).get(agent_id)


def encode_signed_payload(payload: Mapping[str, Any], secret: bytes) -> str:
    body = json.dumps(
        dict(payload), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    encoded = _b64encode(body)
    signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def decode_signed_payload(
    token: str,
    secret: bytes,
    *,
    expected_type: str,
    now: int | None = None,
) -> dict[str, Any]:
    try:
        encoded, supplied_signature = token.split(".", 1)
    except (AttributeError, ValueError) as exc:
        raise IdentityLinkError("Invalid identity-link token") from exc
    expected_signature = hmac.new(
        secret, encoded.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_b64decode(supplied_signature), expected_signature):
        raise IdentityLinkError("Invalid identity-link token")
    try:
        payload = json.loads(_b64decode(encoded))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityLinkError("Invalid identity-link token") from exc
    if not isinstance(payload, dict):
        raise IdentityLinkError("Invalid identity-link token")

    current = int(time.time()) if now is None else int(now)
    maximum_lifetime = (
        ASSERTION_LIFETIME_SECONDS
        if expected_type == ASSERTION_TYPE
        else STATE_LIFETIME_SECONDS
    )
    if (
        payload.get("v") != TOKEN_VERSION
        or payload.get("type") != expected_type
        or not isinstance(payload.get("iat"), int)
        or not isinstance(payload.get("exp"), int)
        or payload["iat"] > current + 30
        or payload["exp"] < current
        or payload["exp"] - payload["iat"] > maximum_lifetime
    ):
        raise IdentityLinkError("Expired or invalid identity-link token")
    return payload


def create_link_state(
    *, agent_id: str, provider: str, user_id: str, secret: bytes, now: int | None = None
) -> str:
    issued = int(time.time()) if now is None else int(now)
    return encode_signed_payload(
        {
            "v": TOKEN_VERSION,
            "type": STATE_TYPE,
            "agent_id": agent_id,
            "provider": provider,
            "user_id": user_id,
            "nonce": secrets.token_urlsafe(24),
            "iat": issued,
            "exp": issued + STATE_LIFETIME_SECONDS,
        },
        secret,
    )


def verify_link_handoff(
    *,
    agent_id: str,
    provider: str,
    user_id: str,
    state_token: str,
    assertion_token: str,
    secret: bytes,
    now: int | None = None,
) -> dict[str, str]:
    state = decode_signed_payload(
        state_token, secret, expected_type=STATE_TYPE, now=now
    )
    assertion = decode_signed_payload(
        assertion_token, secret, expected_type=ASSERTION_TYPE, now=now
    )
    if (
        state.get("agent_id") != agent_id
        or state.get("provider") != provider
        or state.get("user_id") != user_id
        or assertion.get("agent_id") != agent_id
        or assertion.get("provider") != provider
        or assertion.get("state") != state_token
    ):
        raise IdentityLinkError("Identity-link handoff does not match this account")
    if provider != "orcid":
        raise IdentityLinkError("Unsupported external identity provider")
    subject = normalize_orcid(assertion.get("subject"))
    if subject is None or assertion.get("issuer") != "https://orcid.org":
        raise IdentityLinkError("Invalid ORCID assertion")
    nonce = state.get("nonce")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 128:
        raise IdentityLinkError("Invalid identity-link state")
    return {
        "provider": provider,
        "subject": subject,
        "issuer": "https://orcid.org",
        "state_nonce": nonce,
    }


def _preferences_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def linked_identity_from_preferences(
    preferences: Any, *, agent_id: str, provider: str
) -> dict[str, Any] | None:
    links = _preferences_dict(preferences).get(PREFERENCES_KEY)
    if not isinstance(links, Mapping):
        return None
    entry = links.get(provider)
    if not isinstance(entry, Mapping) or entry.get("verified_by_agent") != agent_id:
        return None
    subject = normalize_orcid(entry.get("subject")) if provider == "orcid" else None
    if subject is None:
        return None
    return {
        "provider": provider,
        "subject": subject,
        "issuer": entry.get("issuer"),
        "verified_by_agent": agent_id,
        "verified_at": entry.get("verified_at"),
    }


def claims_with_saved_identities(
    db: Any | None,
    user_id: str,
    claims: Any,
    *,
    plane_runtime: Any | None = None,
    plane_repositories: Any | None = None,
) -> dict[str, Any]:
    context = _identity_context(
        db,
        plane_runtime=plane_runtime,
        plane_repositories=plane_repositories,
    )
    records = context.call(
        context.repository.list_external_identities,
        owner_id=user_id,
        limit=100,
    )
    safe_claims = dict(claims) if isinstance(claims, Mapping) else {}
    if records:
        safe_claims[INTERNAL_CLAIMS_KEY] = {
            record.provider: {
                "subject": record.subject,
                "issuer": record.issuer,
                "verified_by_agent": record.agent_id,
                "verified_at": record.verified_at,
            }
            for record in records
        }
    else:
        safe_claims.pop(INTERNAL_CLAIMS_KEY, None)
    return safe_claims


def claims_with_identity_preferences(preferences: Any, claims: Any) -> dict[str, Any]:
    safe_claims = dict(claims) if isinstance(claims, Mapping) else {}
    links = _preferences_dict(preferences).get(PREFERENCES_KEY)
    if isinstance(links, Mapping):
        safe_claims[INTERNAL_CLAIMS_KEY] = dict(links)
    else:
        safe_claims.pop(INTERNAL_CLAIMS_KEY, None)
    return safe_claims


def public_user_preferences(preferences: Any) -> dict[str, Any]:
    """Remove server-verified authorization state from the client payload."""
    public = _preferences_dict(preferences)
    public.pop(PREFERENCES_KEY, None)
    return public


def store_verified_identity(
    db: Any | None,
    *,
    user_id: str,
    agent_id: str,
    provider: str,
    subject: str,
    issuer: str,
    state_nonce: str,
    now: int | None = None,
    plane_runtime: Any | None = None,
    plane_repositories: Any | None = None,
) -> None:
    """Atomically store a one-to-one verified link without replacing preferences."""
    canonical = normalize_orcid(subject) if provider == "orcid" else None
    if canonical is None or not isinstance(state_nonce, str) or not state_nonce:
        raise IdentityLinkError("Invalid external identity")
    timestamp = int(time.time()) if now is None else int(now)

    context = _identity_context(
        db,
        plane_runtime=plane_runtime,
        plane_repositories=plane_repositories,
    )
    try:
        with context.transaction() as transaction:
            context.repository.store_verified_external_identity(
                transaction,
                owner_id=user_id,
                agent_id=agent_id,
                provider=provider,
                subject=canonical,
                issuer=issuer,
                state_nonce=state_nonce,
                observed_at=timestamp,
                nonce_ttl_seconds=STATE_LIFETIME_SECONDS,
                nonce_cap=10,
            )
    except ExternalIdentityAlreadyLinkedError as exc:
        raise IdentityAlreadyLinkedError(
            "This external identity is linked to another Astral account"
        ) from exc
    except ExternalIdentityNonceReplayError as exc:
        raise IdentityLinkError("This identity-link request was already used") from exc


def _identity_context(
    db: Any | None,
    *,
    plane_runtime: Any | None = None,
    plane_repositories: Any | None = None,
) -> PlaneRepositoryContext:
    repository, runtime = repository_from(
        "identity",
        plane_runtime=plane_runtime,
        repositories=plane_repositories,
        legacy_database=db,
    )
    return PlaneRepositoryContext(
        repository=repository,
        plane_runtime=runtime,
        legacy_database=db,
    )


__all__ = [
    "ASSERTION_TYPE",
    "INTERNAL_CLAIMS_KEY",
    "IdentityAlreadyLinkedError",
    "IdentityLinkError",
    "LINK_SECRETS_ENV",
    "PREFERENCES_KEY",
    "STATE_TYPE",
    "claims_with_saved_identities",
    "claims_with_identity_preferences",
    "create_link_state",
    "decode_signed_payload",
    "encode_signed_payload",
    "link_secret_for",
    "linked_identity_from_preferences",
    "parse_link_secrets",
    "public_user_preferences",
    "store_verified_identity",
    "verify_link_handoff",
]
