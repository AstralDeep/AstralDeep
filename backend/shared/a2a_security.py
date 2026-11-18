"""Validates Bearer tokens on incoming A2A requests: A2ASecurityValidator accepts only node-minted delegation tokens agent-inbound
and refuses every orchestrator-inbound bearer.
"""

import logging
from typing import Optional, Dict, List, Any

from shared import node_identity

NODE_BEARER_REFUSED = "node.bearer_refused"
AGENT_AUDIENCE = "astral-agent-service"

logger = logging.getLogger("A2ASecurity")

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
        self.require_first_party_user = require_first_party_user

    async def validate_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        if self.require_first_party_user:
            logger.warning("A2A token rejected (orchestrator inbound): %s", NODE_BEARER_REFUSED)
            return None
        return self._validate_node_token(token)

    def _validate_node_token(self, token: str) -> Optional[Dict[str, Any]]:
        try:
            payload = node_identity.load_node_identity(create=False).verify_token(
                token, audience=AGENT_AUDIENCE
            )
            node_identity.enforce_owner(str(payload.get("sub") or ""))
        except node_identity.NodeIdentityError as exc:
            logger.warning("A2A node token rejected: %s", exc)
            return None
        return payload

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
