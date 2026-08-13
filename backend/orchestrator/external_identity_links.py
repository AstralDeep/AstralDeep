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

from orchestrator.agent_identity import normalize_orcid

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


def claims_with_saved_identities(db: Any, user_id: str, claims: Any) -> dict[str, Any]:
    preferences = db.get_user_preferences(user_id)
    return claims_with_identity_preferences(preferences, claims)


def claims_with_identity_preferences(preferences: Any, claims: Any) -> dict[str, Any]:
    safe_claims = dict(claims) if isinstance(claims, Mapping) else {}
    links = _preferences_dict(preferences).get(PREFERENCES_KEY)
    if isinstance(links, Mapping):
        safe_claims[INTERNAL_CLAIMS_KEY] = dict(links)
    else:
        safe_claims.pop(INTERNAL_CLAIMS_KEY, None)
    return safe_claims


def store_verified_identity(
    db: Any,
    *,
    user_id: str,
    agent_id: str,
    provider: str,
    subject: str,
    issuer: str,
    state_nonce: str,
    now: int | None = None,
) -> None:
    """Atomically store a one-to-one verified link without replacing preferences."""
    canonical = normalize_orcid(subject) if provider == "orcid" else None
    if canonical is None or not isinstance(state_nonce, str) or not state_nonce:
        raise IdentityLinkError("Invalid external identity")
    timestamp = int(time.time()) if now is None else int(now)

    connection, pooled = db._borrow()
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"external-identity:{provider}:{canonical}",),
        )
        cursor.execute(
            "SELECT user_id, preferences FROM user_preferences FOR UPDATE"
        )
        rows = cursor.fetchall()
        target_preferences: dict[str, Any] = {}
        for row in rows:
            prefs = _preferences_dict(row.get("preferences"))
            entry = linked_identity_from_preferences(
                prefs, agent_id=agent_id, provider=provider
            )
            if entry and entry["subject"] == canonical and row["user_id"] != user_id:
                raise IdentityAlreadyLinkedError(
                    "This external identity is linked to another Astral account"
                )
            if row["user_id"] == user_id:
                target_preferences = prefs

        links = target_preferences.get(PREFERENCES_KEY)
        links = dict(links) if isinstance(links, Mapping) else {}
        existing_entry = links.get(provider)
        used_nonces = (
            existing_entry.get("recent_link_nonces")
            if isinstance(existing_entry, Mapping)
            else []
        )
        recent_nonces = [
            item
            for item in used_nonces
            if isinstance(item, Mapping)
            and isinstance(item.get("nonce"), str)
            and isinstance(item.get("used_at"), int)
            and item["used_at"] >= timestamp - STATE_LIFETIME_SECONDS
        ]
        if any(item["nonce"] == state_nonce for item in recent_nonces):
            raise IdentityLinkError("This identity-link request was already used")
        recent_nonces.append({"nonce": state_nonce, "used_at": timestamp})
        links[provider] = {
            "subject": canonical,
            "issuer": issuer,
            "verified_by_agent": agent_id,
            "verified_at": timestamp,
            "recent_link_nonces": recent_nonces[-10:],
        }
        target_preferences[PREFERENCES_KEY] = links
        encoded = json.dumps(target_preferences, separators=(",", ":"), sort_keys=True)
        updated_ms = timestamp * 1000
        cursor.execute(
            """INSERT INTO user_preferences (user_id, preferences, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT(user_id) DO UPDATE
               SET preferences = EXCLUDED.preferences, updated_at = EXCLUDED.updated_at""",
            (user_id, encoded, updated_ms),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        db._release(connection, pooled)


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
    "store_verified_identity",
    "verify_link_handoff",
]
