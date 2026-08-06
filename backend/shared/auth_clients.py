"""Accepted OIDC ``azp`` (authorized party) client ids.

The orchestrator does not enforce a strict ``aud`` (Keycloak confidential
clients set ``aud="account"``); it validates the *authorized party* (``azp``)
of the access token instead — the client the token was minted for. The web
client (``KEYCLOAK_CLIENT_ID``, default ``astral-frontend``) is always
accepted. Additional first-party clients are accepted when listed in
``KEYCLOAK_ALLOWED_AZP`` (comma-separated).

This is the configurable allow-list the native desktop client's production
posture requires (RFC 8252 / OAuth 2.0 for Native Apps): the Windows client
authenticates with its OWN dedicated *public* Keycloak client
(``astral-desktop``), so its tokens carry ``azp=astral-desktop`` rather than the
web client's id. Listing it here lets the desktop and web auth surfaces stay
isolated while both register over the same WebSocket / REST gates.

Empty/unset ``KEYCLOAK_ALLOWED_AZP`` ⇒ only the primary web client is accepted
(identical to the pre-allow-list single-``azp`` check — fully backwards
compatible).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Set, Tuple

# Mirrors ``orchestrator.mcp_authz.MCP_AUDIENCE``; duplicated as a literal
# because ``shared`` must not import from ``orchestrator`` (layering). The two
# are pinned equal by tests/test_first_party_claims.py.
MCP_AUDIENCE = "astral-mcp"


def _primary_client_id() -> str:
    # shared/__init__ normalizes the VITE_-prefixed aliases both directions, so
    # either name resolves here.
    return (
        os.getenv("KEYCLOAK_CLIENT_ID")
        or os.getenv("KEYCLOAK_CLIENT_ID")
        or ""
    ).strip()


def allowed_azps() -> Set[str]:
    """The set of accepted ``azp`` client ids (primary web client + allow-list)."""
    ids = {_primary_client_id()}
    for raw in os.getenv("KEYCLOAK_ALLOWED_AZP", "").split(","):
        cid = raw.strip()
        if cid:
            ids.add(cid)
    return {cid for cid in ids if cid}


def is_azp_allowed(azp: str) -> bool:
    """True when ``azp`` is acceptable for this deployment.

    A missing/empty ``azp`` is allowed (some token flows omit it) — matching the
    historical ``if azp and azp != client_id`` semantics. A present ``azp`` must
    be in :func:`allowed_azps`.
    """
    if not azp:
        return True
    return azp in allowed_azps()


def agent_service_client_id() -> str:
    """The Keycloak client the RFC 8693 delegation exchange targets."""
    return (os.getenv("AGENT_SERVICE_CLIENT_ID", "astral-agent-service") or "").strip()


def audience_set(payload: Dict[str, Any]) -> Set[str]:
    """The token's ``aud`` claim as a set — the claim is a string OR a list."""
    aud = payload.get("aud")
    if isinstance(aud, str):
        values = [aud]
    elif isinstance(aud, (list, tuple, set)):
        values = list(aud)
    else:
        return set()
    return {str(a).strip() for a in values if str(a).strip()}


def is_first_party_user_claims(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """True when ``payload`` may act as an interactive first-party USER session.

    A DENYLIST, deliberately: Keycloak confidential-client access tokens carry
    ``aud="account"``, so requiring a positive audience (or re-enabling
    ``verify_aud``) would refuse every real login. What this refuses is the set
    of tokens the orchestrator itself mints or obtains for a NON-user purpose
    and which would otherwise pass the signature/azp/role gates unchanged:

    * ``act`` — the RFC 8693 actor claim on a delegated token;
    * ``delegation`` — this repo's own delegated-token flag;
    * ``aud`` containing :data:`MCP_AUDIENCE` — scoped to the MCP endpoint,
      which does its own ``audience=``-checked decode in ``mcp_authz``.

    NOT refused: ``aud`` containing :func:`agent_service_client_id`. That looks
    like the obvious discriminator for a Keycloak-exchanged delegation token,
    and it is wrong here — verified against a live realm, where an ORDINARY
    interactive login decodes to
    ``aud=["astral-agent-service","realm-management","account"]`` because the
    delegation setup adds that audience to the frontend client's tokens by
    protocol mapper. Refusing it locks every real user out of chat and /api.
    A Keycloak-exchanged delegation token carries neither ``act`` nor
    ``delegation`` either, so nothing in the token distinguishes it from a user
    token in that realm configuration. Closing that replay path needs a REALM
    change (stop granting the agent-service audience to interactive tokens, or
    stamp exchanged tokens with a claim of their own), not a code change —
    see ``docs/keycloak_agent_delegation_setup.md``.

    Returns ``(ok, reason)``; ``reason`` is "" when ``ok``.
    """
    if not isinstance(payload, dict):
        return False, "malformed_claims"
    if "act" in payload:
        return False, "delegated_actor_claim"
    if payload.get("delegation"):
        return False, "delegation_flag"
    if MCP_AUDIENCE in audience_set(payload):
        return False, "mcp_audience"
    return True, ""
