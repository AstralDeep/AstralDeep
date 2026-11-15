"""Validates Bearer tokens on incoming A2A requests via Keycloak JWKS (or mock decode in
dev) and enforces RFC 8693 delegation scopes; A2ASecurityValidator serves an
agent-inbound posture and a stricter orchestrator-inbound one.
"""

import os
import base64
import json
import logging
from typing import Optional, Dict, List, Any

from shared import node_identity
from shared.token_verify import TokenRefused, check_idp_audience, verify_idp_token
from shared.auth_clients import (
    agent_service_client_id,
    allowed_azps,
    is_first_party_user_claims,
)

logger = logging.getLogger("A2ASecurity")

ENTRY_ROLES = frozenset({"user", "admin"})

_DEFAULT_MOCK_CLAIMS = {
    "sub": "test_user",
    "aud": "account",
    "preferred_username": "test_user",
    "email": "test_user@local",
    "realm_access": {"roles": ["admin", "user"]},
    "resource_access": {"astral-frontend": {"roles": ["admin", "user"]}},
}


def token_roles(payload: Dict[str, Any]) -> List[str]:
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
    def __init__(self, *, require_first_party_user: bool = False):
        self.mock_auth = os.getenv("USE_MOCK_AUTH", "false").lower() == "true"
        self.authority = os.getenv("KEYCLOAK_AUTHORITY", "")
        self.client_id = os.getenv("KEYCLOAK_CLIENT_ID", "")
        self.require_first_party_user = require_first_party_user

    async def validate_token(self, token: str) -> Optional[Dict[str, Any]]:
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

    def _validate_mock_token(self, token: str) -> Optional[Dict[str, Any]]:
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
            return None
        return dict(_DEFAULT_MOCK_CLAIMS)

    def _accepted_azps(self) -> set:
        accepted = set(allowed_azps())
        agent_client = agent_service_client_id()
        if agent_client:
            accepted.add(agent_client)
        return accepted

    async def _validate_keycloak_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not self.authority or not self.client_id:
            logger.error("Keycloak not configured for A2A token validation")
            return None
        if node_identity.is_personal() and node_identity.peek_algorithm(token) == "EdDSA":
            return self._validate_node_token(token)
        try:
            return await verify_idp_token(
                token,
                extra_azps=self._accepted_azps(),
                extra_issuers=[self.authority],
                extra_audiences=() if self.require_first_party_user else [agent_service_client_id()],
                jwks_getter=self._jwks_for,
            )
        except TokenRefused as exc:
            logger.warning("A2A token rejected: %s", exc.reason)
            return None
        except Exception as exc:
            logger.error(f"A2A token validation failed: {exc}")
            return None

    def _validate_node_token(self, token: str) -> Optional[Dict[str, Any]]:
        try:
            payload = node_identity.load_node_identity(create=False).verify_token(
                token, audience=agent_service_client_id()
            )
            node_identity.enforce_owner(str(payload.get("sub") or ""))
        except node_identity.NodeIdentityError as exc:
            logger.warning("A2A node token rejected: %s", exc)
            return None
        return payload

    async def _jwks_for(self, url: str, token: str) -> dict:
        if url == f"{self.authority.rstrip('/')}/protocol/openid-connect/certs":
            return await self._get_jwks(token)
        from shared.jwks_cache import get_jwks

        return await get_jwks(url, token=token)

    async def _get_jwks(self, token: Optional[str] = None) -> dict:
        from shared.jwks_cache import get_jwks

        jwks_url = f"{self.authority}/protocol/openid-connect/certs"
        return await get_jwks(jwks_url, token=token)

    def first_party_user_refusal(self, payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict) or not isinstance(payload.get("sub"), str):
            return "malformed_claims"
        ok, reason = is_first_party_user_claims(payload)
        if not ok:
            return reason
        try:
            check_idp_audience(payload)
        except TokenRefused as refusal:
            return refusal.reason
        if not ENTRY_ROLES.intersection(token_roles(payload)):
            return "missing_entry_role"
        return ""

    def extract_user_id(self, payload: Dict[str, Any]) -> Optional[str]:
        return payload.get("sub")

    def extract_scopes(self, payload: Dict[str, Any]) -> List[str]:
        scope_str = payload.get("scope", "")
        return scope_str.split() if scope_str else []

    def is_delegation_token(self, payload: Dict[str, Any]) -> bool:
        return "act" in payload

    def get_actor(self, payload: Dict[str, Any]) -> Optional[str]:
        act = payload.get("act", {})
        return act.get("sub") if act else None
