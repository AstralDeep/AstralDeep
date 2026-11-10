"""Starts remote MCP OAuth only in a browser whose web_auth.py session is the connector
owner, binding the flow to that browser with a single-use cookie, and completes the
one-time provider callback through remote_mcp_oauth.py only when that signed-in browser returns.
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from orchestrator.api import _get_orchestrator

remote_mcp_router = APIRouter(prefix="/api/remote-mcp", tags=["Remote MCP"])
logger = logging.getLogger("Orchestrator.RemoteMCP.OAuth")
_CALLBACK_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
}
_BINDING_COOKIE_PREFIX = "astral_mcp_oauth_"
_BINDING_COOKIE_PATH = "/api/remote-mcp/oauth"


def _binding_cookie(state: str) -> str:
    return _BINDING_COOKIE_PREFIX + hashlib.sha256(state.encode("utf-8")).hexdigest()[:32]


def _refusal(error: Exception) -> str:
    from orchestrator.remote_mcp_oauth import RemoteMCPOAuthError

    return str(error) if isinstance(error, RemoteMCPOAuthError) else type(error).__name__


@remote_mcp_router.get("/oauth/start", include_in_schema=False)
async def remote_mcp_oauth_start(request: Request, state: str = ""):
    try:
        from fastapi.security import HTTPAuthorizationCredentials

        from orchestrator import auth, web_auth
        from orchestrator.remote_mcp_oauth import _STATE_TTL, bind_authorization

        session = await web_auth.ensure_session(request)
        if not session or not session.get("access_token"):
            target = web_auth._validate_next(request.url.path + "?" + urlencode({"state": state}))
            return RedirectResponse("/auth/login?next=" + quote(target, safe=""),
                                    status_code=303, headers=_CALLBACK_HEADERS)
        claims = await auth.get_current_user_payload(
            request, HTTPAuthorizationCredentials(scheme="Bearer", credentials=session["access_token"]))
        await auth.verify_user(claims)
        authorization_url, binding = await bind_authorization(
            _get_orchestrator(request), claims.get("sub"), state)
    except Exception as error:  # noqa: BLE001
        logger.warning("Remote MCP OAuth start refused: %s", _refusal(error))
        return HTMLResponse("<p>Authorization could not start or expired. Return to AstralDeep and try again.</p>",
                            status_code=400, headers=_CALLBACK_HEADERS)
    response = RedirectResponse(authorization_url, status_code=303, headers=_CALLBACK_HEADERS)
    response.set_cookie(_binding_cookie(state), binding, max_age=_STATE_TTL, path=_BINDING_COOKIE_PATH,
                        secure=True, httponly=True, samesite="lax")
    return response


@remote_mcp_router.get("/oauth/callback", response_class=HTMLResponse, include_in_schema=False)
async def remote_mcp_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    cookie = _binding_cookie(state)
    if error:
        response = HTMLResponse("<p>Authorization was not completed. You can close this window.</p>",
                                status_code=400, headers=_CALLBACK_HEADERS)
    else:
        try:
            from orchestrator import web_auth
            from orchestrator.remote_mcp_oauth import complete_authorization

            session = await web_auth.ensure_session(request)
            await complete_authorization(
                _get_orchestrator(request), code, state, binding=request.cookies.get(cookie),
                session_owner=str(session.get("sub") or "") if session else None)
            response = HTMLResponse("<p>Authorization completed. You can close this window.</p>",
                                    headers=_CALLBACK_HEADERS)
        except Exception as error:  # noqa: BLE001
            logger.warning("Remote MCP OAuth callback refused: %s", _refusal(error))
            response = HTMLResponse("<p>Authorization failed or expired. Return to AstralDeep and try again.</p>",
                                    status_code=400, headers=_CALLBACK_HEADERS)
    response.delete_cookie(cookie, path=_BINDING_COOKIE_PATH, secure=True, httponly=True, samesite="lax")
    return response
