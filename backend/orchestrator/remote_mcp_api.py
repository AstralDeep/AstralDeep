"""Completes one-time owner-bound remote MCP OAuth callbacks without accepting a web
session or bearer token at the provider redirect boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from orchestrator.api import _get_orchestrator

remote_mcp_router = APIRouter(prefix="/api/remote-mcp", tags=["Remote MCP"])
_CALLBACK_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
}


@remote_mcp_router.get("/oauth/callback", response_class=HTMLResponse, include_in_schema=False)
async def remote_mcp_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse("<p>Authorization was not completed. You can close this window.</p>",
                            status_code=400, headers=_CALLBACK_HEADERS)
    try:
        from orchestrator.remote_mcp_oauth import complete_authorization

        await complete_authorization(_get_orchestrator(request), code, state)
    except Exception:  # noqa: BLE001
        return HTMLResponse("<p>Authorization failed or expired. Return to AstralDeep and try again.</p>",
                            status_code=400, headers=_CALLBACK_HEADERS)
    return HTMLResponse(
        "<p>Authorization completed. You can close this window.</p>",
        headers=_CALLBACK_HEADERS,
    )
