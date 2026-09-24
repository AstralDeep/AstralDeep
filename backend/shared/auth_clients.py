"""Resolves and validates accepted OIDC `azp` client ids — the web frontend plus any
operator-configured KEYCLOAK_ALLOWED_AZP entries — since Keycloak confidential-client
tokens all share aud="account" and can't be checked by audience alone.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Set, Tuple

# Duplicated on purpose — shared can't import orchestrator
MCP_AUDIENCE = "astral-mcp"


def _primary_client_id() -> str:
    return (
        os.getenv("KEYCLOAK_CLIENT_ID")
        or os.getenv("KEYCLOAK_CLIENT_ID")
        or ""
    ).strip()


def allowed_azps() -> Set[str]:
    ids = {_primary_client_id()}
    for raw in os.getenv("KEYCLOAK_ALLOWED_AZP", "").split(","):
        cid = raw.strip()
        if cid:
            ids.add(cid)
    return {cid for cid in ids if cid}


def is_azp_allowed(azp: str) -> bool:
    if not azp:
        return True
    return azp in allowed_azps()


def agent_service_client_id() -> str:
    return (os.getenv("AGENT_SERVICE_CLIENT_ID", "astral-agent-service") or "").strip()


def audience_set(payload: Dict[str, Any]) -> Set[str]:
    aud = payload.get("aud")
    if isinstance(aud, str):
        values = [aud]
    elif isinstance(aud, (list, tuple, set)):
        values = list(aud)
    else:
        return set()
    return {str(a).strip() for a in values if str(a).strip()}


def is_first_party_user_claims(payload: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "malformed_claims"
    if "act" in payload:
        return False, "delegated_actor_claim"
    if payload.get("delegation"):
        return False, "delegation_flag"
    if MCP_AUDIENCE in audience_set(payload):
        return False, "mcp_audience"
    return True, ""
