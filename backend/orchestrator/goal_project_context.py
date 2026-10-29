"""Builds read-only active-project folder guidance for goal planning from authenticated host authority."""

from orchestrator.computer_hosts import ComputerHostError
from orchestrator.project_authority import (
    ProjectOrigin,
    origin_current,
    resolve_project_origin,
)

_SETUP_ADVISORY = "Run project setup, dependency installation, tests, and startup from this local project folder."
_NO_FOLDER_ADVISORY = "Choose folders for this project on this Mac before using its local files."
_AUTHORITY_ADVISORY = "Reconnect this Mac to refresh the project's folder authority."


def _authenticated_socket(orch, websocket, owner_id):
    from orchestrator.async_tasks import DurableUserTurnWebSocket

    if type(websocket) is DurableUserTurnWebSocket:
        if websocket.llm_context_user_id != owner_id or websocket._closed:
            return None
        websocket = websocket._origin
    claims = getattr(orch, "ui_sessions", {}).get(websocket) or {}
    return websocket if claims.get("sub") == owner_id else None


def _context(project_id, host=None, roots=(), *, folder_status, advisory):
    roots = [dict(root) for root in roots]
    return {
        "project_id": project_id,
        "computer_id": getattr(host, "host_id", None),
        "computer_name": getattr(host, "name", None),
        "workspace_roots": roots,
        "primary_root": roots[0]["path"] if roots else None,
        "folder_status": folder_status,
        "advisory": advisory,
    }


async def project_goal_context(orch, websocket, owner_id: str, chat_id: str) -> dict | None:
    socket = _authenticated_socket(orch, websocket, owner_id)
    if socket is None:
        return None
    registry = getattr(orch, "computer_hosts", None)
    host = registry.host_for_socket(socket) if registry is not None else None
    if host is None or host.websocket is not socket or host.owner_sub != owner_id:
        return None
    if not getattr(host, "project_mode", False):
        return None
    claims = getattr(orch, "ui_sessions", {}).get(socket) or {}
    if claims.get("_project_workspaces_v1") is not True:
        return _context(None, host, folder_status="authority_unavailable", advisory=_AUTHORITY_ADVISORY)
    try:
        origin = await resolve_project_origin(orch, owner_id, chat_id, host)
    except ComputerHostError:
        return _context(None, host, folder_status="authority_unavailable", advisory=_AUTHORITY_ADVISORY)
    if not isinstance(origin, ProjectOrigin) or not origin_current(orch, origin):
        return _context(None, host, folder_status="authority_unavailable", advisory=_AUTHORITY_ADVISORY)
    if not origin.roots:
        return _context(origin.context.project_id, host, folder_status="folder_grant_required",
                        advisory=_NO_FOLDER_ADVISORY)
    return _context(origin.context.project_id, host, origin.roots, folder_status="granted",
                    advisory=_SETUP_ADVISORY)
