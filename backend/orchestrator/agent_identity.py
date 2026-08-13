"""Verified external identity projection for restricted agents.

Agent cards may declare ``metadata.required_identity_claims``. Astral projects
only those claims from the already-verified Keycloak access-token payload into
the MCP envelope; it never copies the bearer token or arbitrary token claims.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

ORCID_CLAIM = "orcid"
_SUPPORTED_IDENTITY_CLAIMS = frozenset({ORCID_CLAIM})
_CLAIM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ORCID_PATTERN = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
_INVALID_REQUIREMENT = "__invalid_identity_requirement__"
IDENTITY_TRUST_ENV = "IDENTITY_CLAIM_TRUSTED_AGENTS"


def normalize_orcid(value: Any) -> str | None:
    """Return a canonical, checksum-valid ORCID iD or ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    if not _ORCID_PATTERN.fullmatch(candidate):
        return None

    digits = candidate.replace("-", "")
    total = 0
    for digit in digits[:15]:
        total = (total + int(digit)) * 2
    remainder = (12 - (total % 11)) % 11
    expected = "X" if remainder == 10 else str(remainder)
    return candidate if digits[-1] == expected else None


def required_identity_claims(card: Any) -> tuple[str, ...]:
    """Return a card's declared requirements, malformed declarations included.

    Invalid declarations become an unsupported sentinel so every downstream
    decision fails closed instead of accidentally treating them as unrestricted.
    """
    metadata = getattr(card, "metadata", None) or {}
    if not isinstance(metadata, Mapping):
        return (_INVALID_REQUIREMENT,)
    raw = metadata.get("required_identity_claims")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        return (_INVALID_REQUIREMENT,)

    claims: list[str] = []
    for claim in raw:
        if not isinstance(claim, str) or not _CLAIM_NAME.fullmatch(claim):
            return (_INVALID_REQUIREMENT,)
        if claim not in claims:
            claims.append(claim)
    return tuple(claims)


def identity_projection_trusted(card: Any) -> bool:
    """Whether this agent is operator-approved to receive identity claims."""
    agent_id = getattr(card, "agent_id", None)
    if not isinstance(agent_id, str) or not agent_id:
        return False
    trusted = {
        value.strip()
        for value in os.getenv(IDENTITY_TRUST_ENV, "").split(",")
        if value.strip()
    }
    return agent_id in trusted


def verified_identity_for(card: Any, token_claims: Any) -> dict[str, str] | None:
    """Project required claims, or return ``None`` when any is unavailable."""
    required = required_identity_claims(card)
    if not required:
        return {}
    if not identity_projection_trusted(card):
        return None
    if not isinstance(token_claims, Mapping):
        return None

    projected: dict[str, str] = {}
    for claim in required:
        if claim not in _SUPPORTED_IDENTITY_CLAIMS:
            return None
        if claim == ORCID_CLAIM:
            value = normalize_orcid(token_claims.get(claim))
        else:  # pragma: no cover - guarded by the supported set
            value = None
        if value is None:
            return None
        projected[claim] = value
    return projected


def identity_requirement_satisfied(card: Any, token_claims: Any) -> bool:
    return verified_identity_for(card, token_claims) is not None


def identity_access_message(card: Any) -> str:
    """Stable user-facing refusal without exposing claim contents."""
    name = str(getattr(card, "name", None) or "This agent")
    required = required_identity_claims(card)
    if required == (ORCID_CLAIM,):
        return (
            f"{name} requires a linked ORCID iD. Sign out and back in after an "
            "administrator links it to your Astral account."
        )
    return f"{name} requires a verified external identity that is not available."


__all__ = [
    "identity_access_message",
    "identity_projection_trusted",
    "identity_requirement_satisfied",
    "normalize_orcid",
    "required_identity_claims",
    "verified_identity_for",
]
