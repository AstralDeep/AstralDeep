"""Authenticated owner REST boundary reporting the effective managed-policy ceilings for
the owner's online computer hosts and the source of each narrowing, for the Mac
Diagnostics pane; mounted by orchestrator.py alongside the other routers.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from orchestrator.api import _get_orchestrator
from orchestrator.auth import verify_user
from orchestrator.computer_managed_policy import (
    MANAGED_POLICY_SWITCHES,
    owner_role_ceilings,
    policy_digest,
)

computer_policy_router = APIRouter(prefix="/api/computer", tags=["Computers"])


def _owner_id(payload: dict = Depends(verify_user)) -> str:
    user_id = payload.get("sub") if payload else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _host_row(sessions: Any, host: Any) -> Dict[str, Any]:
    ceilings, sources = sessions.effective_ceilings(host)
    return {"host_id": host.host_id, "name": host.name, "platform": host.platform,
            "policy_digest": policy_digest(host.descriptor),
            "ceilings": ceilings.as_dict(),
            "sources": {switch: sources[switch] for switch in MANAGED_POLICY_SWITCHES
                        if switch in sources}}


@computer_policy_router.get("/managed-policy")
async def managed_policy(request: Request, owner_id: str = Depends(_owner_id)) -> dict:
    orch = _get_orchestrator(request)
    sessions = getattr(orch, "computer_sessions", None)
    hosts = getattr(orch, "computer_hosts", None)
    if sessions is None or hosts is None:
        raise HTTPException(status_code=503, detail="Remote control is not available")
    from shared.feature_flags import flags

    role_value, role_source = owner_role_ceilings(sessions, owner_id)
    return {
        "computer_use_enabled": flags.is_enabled("computer_use"),
        "roles": {"source": role_source,
                  "denied": [switch for switch in MANAGED_POLICY_SWITCHES
                             if role_value is None or not getattr(role_value, switch)]},
        "hosts": [_host_row(sessions, host) for host in hosts.online_for_owner(owner_id)],
    }
