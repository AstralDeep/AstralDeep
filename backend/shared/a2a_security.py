"""
A2A Security — Authentication and authorization for incoming A2A JSON-RPC requests.

Extracts Bearer tokens from Authorization headers, validates them via
Keycloak JWKS (production) or mock decode (development), and enforces
RFC 8693 delegation scopes on tool execution.

Two postures share one validator:

* **Agent inbound** (default, ``require_first_party_user=False``): an agent's
  own ``/a2a`` mount, reached by the orchestrator's outbound client with a
  per-call RFC 8693 delegation token. Delegation tokens (``act`` claim,
  ``delegation`` flag, agent-service audience) are the EXPECTED credential
  here, so they are accepted.
* **Orchestrator inbound** (``require_first_party_user=True``): the
  orchestrator's OWN ``/a2a`` server, an entry gate for external callers. It
  is held to at least the web entry gate's rules: issuer bound to the realm,
  ``azp`` present and allow-listed, audience (when present) naming this
  deployment, a ``user``/``admin`` realm role, and NO delegation-shaped token
  (a token the orchestrator minted for a non-user purpose must never be
  replayed inbound as its on-behalf-of human).
"""
import os
import base64
import json
import logging
from typing import Optional, Dict, List, Any

from jose import jwt as jose_jwt

from shared.auth_clients import (
    agent_service_client_id,
    allowed_azps,
    audience_set,
    is_first_party_user_claims,
)

logger = logging.getLogger("A2ASecurity")

#: Keycloak's default audience for confidential-client access tokens. Mirrors
#: the web gate's reasoning (orchestrator/auth.py): ``aud`` is not the client
#: id there, so an audience check must admit it.
KEYCLOAK_DEFAULT_AUDIENCE = "account"

#: The realm roles that admit an interactive principal at the web entry gate
#: (web_auth.auth_callback). The orchestrator-inbound A2A posture requires the
#: same.
ENTRY_ROLES = frozenset({"user", "admin"})

_DEFAULT_MOCK_CLAIMS = {
    "sub": "test_user",
    "preferred_username": "test_user",
    "email": "test_user@local",
    "realm_access": {"roles": ["admin", "user"]},
    "resource_access": {"astral-frontend": {"roles": ["admin", "user"]}},
}


def token_roles(payload: Dict[str, Any]) -> List[str]:
    """Realm + client roles from decoded claims (mirrors web_auth._roles_from_token)."""
    if not isinstance(payload, dict):
        return []
    realm = payload.get("realm_access") or {}
    roles = list(realm.get("roles", []) or []) if isinstance(realm, dict) else []
    resource = payload.get("resource_access") or {}
    if isinstance(resource, dict):
        for client in resource.values():
            if isinstance(client, dict):
                roles.extend(client.get("roles", []) or [])
    return [str(r) for r in roles]


class A2ASecurityValidator:
    """Validates Bearer tokens on incoming A2A requests.

    Reuses the same validation logic as orchestrator/auth.py but
    decoupled from FastAPI Depends() so it can be called from the
    AgentExecutor context.
    """

    def __init__(self, *, require_first_party_user: bool = False):
        self.mock_auth = os.getenv("USE_MOCK_AUTH", "false").lower() == "true"
        self.authority = os.getenv("KEYCLOAK_AUTHORITY", "")
        self.client_id = os.getenv("KEYCLOAK_CLIENT_ID", "")
        self.require_first_party_user = require_first_party_user

    async def validate_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Validate a Bearer token and return the decoded payload.

        Returns None if the token is invalid or missing.
        """
        if not token:
            return None

        if self.mock_auth:
            payload = self._validate_mock_token(token)
        else:
            payload = await self._validate_keycloak_token(token)

        if payload is None:
            return None
        if self.require_first_party_user:
            reason = self.first_party_user_refusal(payload)
            if reason:
                logger.warning(f"A2A token rejected (orchestrator inbound): {reason}")
                return None
        return payload

    # ------------------------------------------------------------------ mock

    def _validate_mock_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Decode a mock JWT without cryptographic verification."""
        if token == "dev-token":
            return dict(_DEFAULT_MOCK_CLAIMS)
        try:
            parts = token.split(".")
            if len(parts) == 3:
                payload_b64 = parts[1]
                payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
                payload_json = base64.b64decode(payload_b64).decode("utf-8")
                decoded = json.loads(payload_json)
                if isinstance(decoded, dict):
                    return decoded
        except Exception as e:
            logger.debug(f"A2A mock JWT decode failed, falling back to default test_user: {e}")
        if self.require_first_party_user:
            # The entry-gate posture never widens an undecodable credential
            # into the permissive default principal.
            return None
        return dict(_DEFAULT_MOCK_CLAIMS)

    # -------------------------------------------------------------- keycloak

    def _accepted_azps(self) -> set:
        """Web client + KEYCLOAK_ALLOWED_AZP + the RFC 8693 agent-service client."""
        accepted = set(allowed_azps())
        agent_client = agent_service_client_id()
        if agent_client:
            accepted.add(agent_client)
        return accepted

    async def _validate_keycloak_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Validate a token against Keycloak JWKS."""
        if not self.authority or not self.client_id:
            logger.error("Keycloak not configured for A2A token validation")
            return None

        try:
            jwks = await self._get_jwks(token)
            # verify_aud stays off at decode time for the same reason as the
            # web gate: Keycloak confidential clients set aud="account". The
            # orchestrator-inbound posture checks the audience explicitly in
            # first_party_user_refusal.
            payload = jose_jwt.decode(
                token,
                jwks,
                algorithms=["RS256"],
                options={"verify_aud": False, "verify_at_hash": False},
            )
        except Exception as e:
            logger.error(f"A2A token validation failed: {e}")
            return None

        # Bind the token to our realm (tolerant of a trailing-slash diff).
        iss = payload.get("iss")
        if iss is not None and str(iss).rstrip("/") != self.authority.rstrip("/"):
            logger.warning("A2A token rejected: issuer mismatch")
            return None
        if iss is None and self.require_first_party_user:
            logger.warning("A2A token rejected: missing issuer")
            return None

        azp = payload.get("azp")
        if azp:
            if azp not in self._accepted_azps():
                logger.warning(f"A2A token rejected: invalid azp={azp}")
                return None
        elif self.require_first_party_user:
            logger.warning("A2A token rejected: missing azp")
            return None
        return payload

    async def _get_jwks(self, token: Optional[str] = None) -> dict:
        """Fetch Keycloak JWKS through the shared TTL + kid-miss cache."""
        from shared.jwks_cache import get_jwks

        jwks_url = f"{self.authority}/protocol/openid-connect/certs"
        return await get_jwks(jwks_url, token=token)

    # --------------------------------------------------- entry-gate posture

    def first_party_user_refusal(self, payload: Dict[str, Any]) -> str:
        """Why ``payload`` may NOT act as an interactive user at the A2A entry gate.

        Returns "" when acceptable. Applied to both mock and Keycloak claims so
        the development posture cannot be looser in claim SHAPE than
        production.
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("sub"), str):
            return "malformed_claims"
        ok, reason = is_first_party_user_claims(payload)
        if not ok:
            return reason
        aud = audience_set(payload)
        if aud:
            accepted = {KEYCLOAK_DEFAULT_AUDIENCE} | set(allowed_azps())
            if self.client_id:
                accepted.add(self.client_id)
            if not (aud & accepted):
                return "audience_mismatch"
        if not ENTRY_ROLES.intersection(token_roles(payload)):
            return "missing_entry_role"
        return ""

    # --------------------------------------------------------------- helpers

    def extract_user_id(self, payload: Dict[str, Any]) -> Optional[str]:
        """Extract user_id from a validated token payload."""
        return payload.get("sub")

    def extract_scopes(self, payload: Dict[str, Any]) -> List[str]:
        """Extract scope list from token payload."""
        scope_str = payload.get("scope", "")
        return scope_str.split() if scope_str else []

    def is_delegation_token(self, payload: Dict[str, Any]) -> bool:
        """Check if the token is an RFC 8693 delegation token (has 'act' claim)."""
        return "act" in payload

    def get_actor(self, payload: Dict[str, Any]) -> Optional[str]:
        """Get the actor identity from a delegation token's 'act' claim."""
        act = payload.get("act", {})
        return act.get("sub") if act else None
