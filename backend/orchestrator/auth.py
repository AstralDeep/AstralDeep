"""
BFF (Backend for Frontend) Auth Proxy.

Proxies OIDC token exchange requests to Keycloak, injecting the
client_secret server-side so it never reaches the browser.

Accepts requests in application/x-www-form-urlencoded format
(as sent by oidc-client-ts) and forwards them to Keycloak with
the client_secret appended.
"""
import asyncio
import os
import logging
import json
import time
from typing import Dict, List, Optional

import aiohttp
from fastapi import APIRouter, Request
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, FileResponse
from jose import jwt as jose_jwt

import shared  # noqa: F401 — normalizes USE_MOCK_AUTH/KEYCLOAK_* env aliases before the import-time read below

logger = logging.getLogger("AuthProxy")

if os.getenv("USE_MOCK_AUTH", "").lower() == "true":
    logger.info("Mock auth ENABLED — all tokens accepted as test_user with roles [admin, user]")
else:
    logger.info("Mock auth disabled — Keycloak JWKS validation active")

# =============================================================================
# APIRouter for Auth & File endpoints (included in main app for OpenAPI docs)
# =============================================================================

auth_router = APIRouter()


def _get_keycloak_config():
    """Read Keycloak settings from environment."""
    authority = os.getenv("KEYCLOAK_AUTHORITY", "")
    client_id = os.getenv("KEYCLOAK_CLIENT_ID", "")
    client_secret = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
    return authority, client_id, client_secret


# --- /auth/token hardening -------------------------------------------------
# The proxy injects the confidential client_secret, so an unconstrained body
# would let any unauthenticated caller speak AS that client — including the
# token-exchange grant DelegationService uses. The only live caller is the
# Windows client in legacy BFF mode, which sends exactly these two grants and
# these fields with NO credential of its own, so the allow-lists below are
# non-breaking by construction.
_TOKEN_GRANT_TYPES = frozenset({"authorization_code", "refresh_token"})
_TOKEN_FIELDS = (
    "grant_type", "code", "redirect_uri", "code_verifier", "refresh_token", "scope",
)
_TOKEN_WINDOW_SECONDS = 60
_TOKEN_MAX_PER_WINDOW = int(os.getenv("AUTH_TOKEN_PROXY_RATE", "20"))
_TOKEN_HITS: Dict[str, List[float]] = {}


def reset_token_proxy_state() -> None:
    """Clear the /auth/token rate-limit window (tests)."""
    _TOKEN_HITS.clear()


def _check_token_rate(ip: str) -> bool:
    """Fixed-window per-IP limiter, same shape as device_login._check_start_rate."""
    now = time.time()
    hits = [t for t in _TOKEN_HITS.get(ip, []) if now - t < _TOKEN_WINDOW_SECONDS]
    if len(hits) >= _TOKEN_MAX_PER_WINDOW:
        _TOKEN_HITS[ip] = hits
        return False
    hits.append(now)
    _TOKEN_HITS[ip] = hits
    return True


async def _audit_token_proxy(action: str, description: str) -> None:
    """auth-class row for a /auth/token refusal.

    The HTTP audit middleware skips every ``/auth/`` path (narrowing that
    prefix would audit /auth/session on every page load), so refusals here are
    recorded directly — the same pattern device_login._audit uses.
    """
    try:
        from audit.hooks import record_auth_event
        await record_auth_event(
            claims={"sub": "anonymous"},
            action=action,
            description=description,
            outcome="failure",
        )
    except Exception:
        logger.debug("token proxy: audit hook unavailable for %s", action, exc_info=True)


@auth_router.post(
    "/auth/token",
    tags=["Auth"],
    summary="[DEPRECATED] Proxy token request to Keycloak",
    description=(
        "DEPRECATED (feature 028): the React-era BFF proxy for oidc-client-ts. "
        "The web app has used the server-side OIDC flow in web_auth.py since "
        "026/028; the one remaining caller is the Windows desktop client in "
        "legacy BFF mode (auth_mode=keycloak_bff), which posts a bare form "
        "with no credential of its own. Constrained accordingly: only the "
        "authorization_code and refresh_token grants, an allow-listed field "
        "set, a server-pinned client_id, and a per-IP rate limit."
    ),
    deprecated=True,
)
async def proxy_token(request: Request):
    """
    DEPRECATED (028 D10) — React-era BFF proxy; web_auth.py owns the flow now.

    Proxy token requests to Keycloak's token endpoint.

    Accepts the application/x-www-form-urlencoded body the Windows BFF client
    sends (grant_type, code, redirect_uri, code_verifier, refresh_token) and
    injects the client_secret before forwarding. The caller presents no
    credential of its own, so the request is constrained instead: an
    authorization_code/refresh_token grant allow-list, a field allow-list, a
    server-pinned client_id, and a per-IP rate limit.
    """
    authority, client_id, client_secret = _get_keycloak_config()

    if not authority or not client_id or not client_secret:
        return JSONResponse(
            status_code=500,
            content={
                "error": "server_error",
                "error_description": "Keycloak not configured on backend",
            },
        )

    ip = request.client.host if request.client else "unknown"
    if not _check_token_rate(ip):
        logger.warning("Token proxy rate limit hit")
        await _audit_token_proxy(
            "token_proxy_rate_limited",
            "POST /auth/token refused: per-IP rate limit exceeded",
        )
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limited",
                "error_description": "too many token requests; retry later",
            },
        )

    token_url = f"{authority}/protocol/openid-connect/token"

    form = await request.form()

    grant_type = str(form.get("grant_type") or "")
    if grant_type not in _TOKEN_GRANT_TYPES:
        logger.warning(f"Token proxy refused grant_type '{grant_type}'")
        await _audit_token_proxy(
            "token_proxy_grant_refused",
            f"POST /auth/token refused unsupported grant_type '{grant_type}'",
        )
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_grant_type",
                "error_description": (
                    "only authorization_code and refresh_token are proxied"
                ),
            },
        )

    # Field allow-list: everything else (subject_token, audience,
    # requested_token_type, username, password, …) is dropped, never forwarded.
    form_data = {}
    for field in _TOKEN_FIELDS:
        value = form.get(field)
        if isinstance(value, str) and value:
            form_data[field] = value

    # The injected secret belongs to the confidential primary client alone, so
    # the caller never chooses the identity it speaks as.
    supplied_client_id = str(form.get("client_id") or "").strip()
    if supplied_client_id and supplied_client_id != client_id:
        logger.warning(
            f"Token proxy overriding caller client_id '{supplied_client_id}' "
            f"with the configured confidential client"
        )
    form_data["client_id"] = client_id
    form_data["client_secret"] = client_secret

    logger.info(f"Proxying {grant_type} request to Keycloak")

    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, data=form_data) as resp:
            body = await resp.json()
            if resp.status != 200:
                logger.error(f"Token request failed ({grant_type}): {resp.status} {body}")
                return JSONResponse(status_code=resp.status, content=body)
            logger.info(f"Token request successful ({grant_type})")
            return JSONResponse(content=body)


# =============================================================================
# Auth Dependencies (used by REST API and file endpoints)
# =============================================================================

security = HTTPBearer(auto_error=False)


def _reject_non_first_party(payload: dict) -> None:
    """401 when ``payload`` is a delegated/agent-service/MCP token.

    Those tokens are minted by this orchestrator for a non-user purpose but
    otherwise satisfy every gate this dependency applies (signature, azp,
    issuer), so nothing else would stop one being replayed at /api/* as its
    on-behalf-of human.
    """
    from shared.auth_clients import is_first_party_user_claims
    ok, reason = is_first_party_user_claims(payload)
    if not ok:
        logger.warning(f"Rejecting non-first-party token at REST auth: {reason}")
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_payload(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    if request.method == "OPTIONS":
        return {}
        
    token = None
    if credentials:
        token = credentials.credentials
    else:
        # Check for token in query parameter (for SSE endpoints where EventSource can't set headers)
        token_param = request.query_params.get("token")
        if token_param:
            token = token_param
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if os.getenv("USE_MOCK_AUTH", "").lower() == "true":
        # Accept any token for mock auth (for testing)
        if token == "dev-token":
            mock_payload = {
                "sub": "test_user",
                "preferred_username": "test_user",
                "email": "test_user@local",
                "realm_access": {"roles": ["admin", "user"]},
                "resource_access": {
                    "astral-frontend": {"roles": ["admin", "user"]}
                }
            }
            try:
                request.state.audit_claims = mock_payload
            except Exception:
                pass
            return mock_payload
        decoded = None
        try:
            import base64
            parts = token.split('.')
            if len(parts) == 3:
                payload_b64 = parts[1]
                payload_b64 += '=' * ((4 - len(payload_b64) % 4) % 4)
                payload_json = base64.b64decode(payload_b64).decode('utf-8')
                decoded = json.loads(payload_json)
        except Exception as e:
            logger.debug(f"Mock JWT decode failed, falling back to default test_user: {e}")
        if isinstance(decoded, dict):
            # Outside the try: a refusal must NOT fall through to the
            # permissive test_user below.
            _reject_non_first_party(decoded)
            try:
                request.state.audit_claims = decoded
            except Exception:
                pass
            return decoded
        fallback = {
            "sub": "test_user",
            "preferred_username": "test_user",
            "email": "test_user@local",
            "realm_access": {"roles": ["admin", "user"]},
            "resource_access": {
                "astral-frontend": {"roles": ["admin", "user"]}
            }
        }
        try:
            request.state.audit_claims = fallback
        except Exception:
            pass
        return fallback
    
    authority, client_id, _ = _get_keycloak_config()
    if not authority or not client_id:
        raise HTTPException(status_code=500, detail="Auth not configured")
        
    try:
        # Feature 028 D8: cached JWKS (kid-miss refetch) replaces per-request fetch.
        jwks_url = f"{authority}/protocol/openid-connect/certs"
        from shared.jwks_cache import get_jwks
        jwks = await get_jwks(jwks_url, token=token)

        # verify_aud stays off: Keycloak confidential clients set aud="account",
        # not the client_id. azp is validated instead, and the audience is
        # denylisted in _reject_non_first_party below.
        payload = jose_jwt.decode(
            token, jwks, algorithms=["RS256"],
            options={"verify_aud": False, "verify_at_hash": False}
        )
        # Bind the token to our realm: reject when the issuer claim is present
        # and does not match the configured authority (tolerant of a
        # trailing-slash diff) — same check Orchestrator.validate_token applies
        # on the WebSocket path.
        iss = payload.get("iss")
        if iss and authority and iss.rstrip("/") != authority.rstrip("/"):
            raise HTTPException(status_code=401, detail="Invalid issuer")
        # Accept the web client (client_id) plus any first-party clients in the
        # KEYCLOAK_ALLOWED_AZP allow-list (e.g. the native desktop's dedicated
        # public client astral-desktop). Empty allow-list ⇒ web client only.
        azp = payload.get("azp")
        from shared.auth_clients import is_azp_allowed
        if azp and not is_azp_allowed(azp):
             raise HTTPException(status_code=401, detail="Invalid client")
        _reject_non_first_party(payload)
        try:
            request.state.audit_claims = payload
        except Exception:
            pass
        return payload
    except Exception as e:
        logger.error(f"Token validation failed in auth wrapper: {e}")
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_id(payload: dict = Depends(get_current_user_payload)) -> Optional[str]:
    """Extract user_id from JWT token."""
    if not payload:
        return None
    return payload.get("sub")  # Keycloak sub claim


async def require_user_id(
    request: Request,
    payload: dict = Depends(get_current_user_payload),
) -> str:
    """Require a valid user_id or raise 401. Also persists user profile to DB."""
    user_id = payload.get("sub") if payload else None
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    # Persist user profile on each authenticated request (upsert is cheap)
    try:
        orch = getattr(request.app.state, "orchestrator", None)
        if not orch:
            root_app = getattr(request.app, "_root_app", None) or request.app
            orch = getattr(root_app.state, "orchestrator", None)
        if orch:
            orch._save_user_profile(payload)
    except Exception:
        pass  # Never block a request for profile persistence
    return user_id


# =============================================================================
# Feature 044 — native-client sign-out (FR-005 / SC-004)
# =============================================================================

@auth_router.post(
    "/api/auth/logout",
    tags=["Auth"],
    summary="Native-client sign-out: revoke the refresh credential server-side",
    description=(
        "The token-holding native clients' twin of the cookie-bound web "
        "/auth/logout — identical semantics: RFC 7009 refresh-token revocation "
        "with the offline-tolerant retry queue, feature-025 offline-grant "
        "revocation, and an auth.logout audit record. The body's client_id must "
        "be an allow-listed first-party client (KEYCLOAK_ALLOWED_AZP) because "
        "Keycloak only revokes a token for its issuing client."
    ),
)
async def native_logout(request: Request,
                        payload: dict = Depends(get_current_user_payload)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    refresh_token = str(body.get("refresh_token") or "")
    client_id = str(body.get("client_id") or "").strip()

    # Accept ONLY the public native clients here — NOT the confidential web
    # client. Otherwise this endpoint would apply the server's confidential
    # client_secret to a caller-supplied refresh_token (a secret-backed
    # revocation oracle); native clients (astral-desktop/mobile) are public and
    # carry no secret. The web app revokes via the cookie-bound /auth/logout.
    from shared.auth_clients import _primary_client_id, allowed_azps
    native_clients = allowed_azps() - {_primary_client_id()}
    if not refresh_token or not client_id or client_id not in native_clients:
        raise HTTPException(
            status_code=400,
            detail="refresh_token and a public native client_id are required",
        )

    user_id = (payload or {}).get("sub") or "unknown"
    app_state = getattr(getattr(request, "app", None), "state", None)
    voice_services = getattr(
        getattr(app_state, "orchestrator", None), "voice_services", None
    )
    if voice_services is not None:
        try:
            await voice_services.end_user_voice_session(
                user_id=user_id,
                reason="logout",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("native logout: voice cleanup failed", exc_info=True)
    from orchestrator import web_auth
    outcome = await web_auth._revoke_or_queue(user_id, refresh_token, client_id=client_id)

    # Feature-025 offline grants die with the sign-out, matching web logout.
    try:
        from orchestrator.offline_grant import OfflineGrantStore
        await asyncio.to_thread(lambda: OfflineGrantStore().revoke_for_user(user_id))
    except Exception:
        logger.debug("native logout: offline-grant revocation failed", exc_info=True)

    # Feature-063 machine credentials die too (FR-015) — fail-open like the grants.
    await web_auth._destroy_machine_credentials(user_id, "native sign-out")

    try:
        from audit.hooks import record_auth_event
        await record_auth_event(
            claims=payload or {},
            action="logout",
            description=f"Native sign-out ({client_id}); refresh credential {outcome}",
            outcome="success" if outcome in ("revoked", "queued") else "failure",
        )
    except Exception:
        logger.debug("native logout: audit record failed", exc_info=True)

    return {"outcome": outcome,
            "revoked": outcome == "revoked",
            "queued": outcome == "queued"}


# =============================================================================
# Feature 051 — watch QR sign-in: RFC 8628 device-login broker endpoints.
# Pre-auth surface (no token yet); the broker is fail-closed + rate-limited
# (FF_DEVICE_LOGIN, orchestrator/device_login.py; contract in
# specs/051-apple-native-clients/contracts/device-login.md).
# =============================================================================

def _device_login_http_error(exc) -> HTTPException:
    return HTTPException(
        status_code=getattr(exc, "status", 500),
        detail={"error": getattr(exc, "code", "device_login_error"), "detail": str(exc)},
    )


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


@auth_router.post(
    "/api/auth/device/start",
    tags=["Auth"],
    summary="Begin a device (watch) sign-in: backend-generated QR + short code",
)
async def device_login_start(request: Request):
    from orchestrator import device_login
    body = await _json_body(request)
    ip = request.client.host if request.client else "unknown"
    try:
        return await device_login.start(str(body.get("client", "")), ip)
    except device_login.DeviceLoginError as exc:
        raise _device_login_http_error(exc)


@auth_router.post(
    "/api/auth/device/poll",
    tags=["Auth"],
    summary="Poll a pending device sign-in (server-authoritative pacing)",
)
async def device_login_poll(request: Request):
    from orchestrator import device_login
    body = await _json_body(request)
    ip = request.client.host if request.client else "unknown"
    try:
        return await device_login.poll(str(body.get("handle", "")), ip)
    except device_login.DeviceLoginError as exc:
        raise _device_login_http_error(exc)


@auth_router.post(
    "/api/auth/device/refresh",
    tags=["Auth"],
    summary="Proxy a refresh-token grant for a device-grant client (watch)",
)
async def device_login_refresh(request: Request):
    from orchestrator import device_login
    body = await _json_body(request)
    try:
        return await device_login.refresh(
            str(body.get("client", "")), str(body.get("refresh_token", "")))
    except device_login.DeviceLoginError as exc:
        raise _device_login_http_error(exc)


async def get_download_user_payload(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Auth for the download route ONLY: Bearer/``?token=`` first (unchanged
    contract), then a GET-only fallback to the ``astral_session`` cookie so
    plain browser anchor clicks — which send cookies but cannot attach an
    Authorization header — can download files.

    The cookie session's access token is validated through the exact same
    JWT path as a Bearer token (:func:`get_current_user_payload`, mock/JWKS),
    which also sets ``request.state.audit_claims`` for audit attribution.
    """
    has_token = bool(credentials) or bool(request.query_params.get("token"))
    if has_token or request.method != "GET":
        # Existing behavior (including the OPTIONS short-circuit and the
        # 401 raised when no token is present on non-GET methods).
        return await get_current_user_payload(request, credentials)

    # Lazy import to avoid an import cycle at module load.
    from orchestrator.web_auth import ensure_session
    try:
        session = await ensure_session(request)
    except Exception as e:
        logger.warning(f"Download cookie-session resolution failed: {e}")
        session = None
    access_token = (session or {}).get("access_token", "")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    cookie_credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=access_token
    )
    return await get_current_user_payload(request, cookie_credentials)


async def require_download_user_id(
    request: Request,
    payload: dict = Depends(get_download_user_payload),
) -> str:
    """:func:`require_user_id` with the download route's GET-only cookie fallback."""
    return await require_user_id(request, payload)


async def get_web_or_bearer_user_payload(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Auth for browser-openable REST routes (055 export/share management):
    Bearer/``?token=`` first (unchanged contract), then the ``astral_session``
    cookie — native clients open export URLs in the system browser and web
    users can middle-click/new-tab, and a browser navigation sends only the
    feature-028 session cookie.

    The cookie session's access token is validated through the exact same
    JWT path as a Bearer token (:func:`get_current_user_payload`, mock/JWKS),
    which also sets ``request.state.audit_claims`` for audit attribution.
    With neither credential, a browser NAVIGATION (GET whose Accept prefers
    text/html) is 302-redirected to login with a validated ``next=`` of
    path+query — the same construction as ``web_auth.shell_gate`` — instead
    of dead-ending on 401 JSON; API callers keep today's 401.
    """
    has_token = bool(credentials) or bool(request.query_params.get("token"))
    if has_token or request.method == "OPTIONS":
        # Existing behavior, including the OPTIONS short-circuit.
        return await get_current_user_payload(request, credentials)

    # Lazy import to avoid an import cycle at module load.
    from orchestrator.web_auth import _validate_next, ensure_session
    try:
        session = await ensure_session(request)
    except Exception as e:
        logger.warning(f"Cookie-session resolution failed: {e}")
        session = None
    access_token = (session or {}).get("access_token", "")
    if access_token:
        cookie_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=access_token
        )
        return await get_current_user_payload(request, cookie_credentials)

    accept = request.headers.get("accept", "")
    prefers_html = accept.split(",")[0].strip().lower().startswith("text/html")
    if request.method == "GET" and prefers_html:
        path = request.url.path or "/"
        query = ("?" + str(request.url.query)) if request.url.query else ""
        from urllib.parse import quote
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            detail="Login required",
            headers={"Location":
                     f"/auth/login?next={quote(_validate_next(path + query), safe='')}"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_user_id_or_web_session(
    request: Request,
    payload: dict = Depends(get_web_or_bearer_user_payload),
) -> str:
    """:func:`require_user_id` that also accepts the feature-028 web session
    cookie (browser navigations / same-origin fetches without a Bearer)."""
    return await require_user_id(request, payload)


def _extract_roles(user_data: dict) -> list:
    logger.debug(f"Extracting roles from user_data: {json.dumps(user_data, indent=2)}")
    roles = user_data.get("realm_access", {}).get("roles", [])
    if "resource_access" in user_data:
        client_id = os.getenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
        logger.debug(f"Client ID: {client_id}")
        if client_id in user_data["resource_access"]:
            client_roles = user_data["resource_access"][client_id].get("roles", [])
            roles.extend(client_roles)
            logger.debug(f"Client roles: {client_roles}")
        if "account" in user_data["resource_access"]:
            account_roles = user_data["resource_access"]["account"].get("roles", [])
            roles.extend(account_roles)
            logger.debug(f"Account roles: {account_roles}")
    logger.debug(f"Final extracted roles: {roles}")
    return roles

async def verify_user(user_data: dict = Depends(get_current_user_payload)):
    if not user_data:
        return {}
    roles = _extract_roles(user_data)
        
    if "user" not in roles and "admin" not in roles:
        raise HTTPException(status_code=403, detail="Not authorized (Requires 'user' or 'admin' role)")
    return user_data

async def verify_admin(user_data: dict = Depends(get_current_user_payload)):
    if not user_data:
        # Fail closed: an empty principal must be denied, not allowed through
        # with an empty dict (a 403 here is the same shape callers already
        # handle for the missing-admin-role case below).
        logger.warning("verify_admin: empty principal — denying (fail closed)")
        raise HTTPException(status_code=403, detail="Not authorized (Requires 'admin' role)")
    roles = _extract_roles(user_data)
    logger.debug(f"verify_admin: extracted roles = {roles}")
    if "admin" not in roles:
        logger.warning(f"verify_admin: admin role missing, roles = {roles}")
        raise HTTPException(status_code=403, detail="Not authorized (Requires 'admin' role)")
    logger.debug("verify_admin: admin role present")
    # Add is_admin flag for downstream use
    user_data["is_admin"] = True
    return user_data


# =============================================================================
# File Upload/Download Endpoints
# =============================================================================

# NOTE: POST /api/upload moved to backend/orchestrator/attachments/router.py
# (feature 002-file-uploads) — supports the expanded type set, 30 MB cap,
# user-scoped storage, and content-type sniffing.

@auth_router.get(
    "/api/download/{session_id}/{filename}",
    tags=["Files"],
    summary="Download a file",
    description=(
        "Download a previously uploaded or generated file by session ID and filename. "
        "Auth: Bearer token, ?token= query param, or the astral_session cookie "
        "(browser anchor clicks)."
    ),
)
async def download_file(session_id: str, filename: str, user_id: str = Depends(require_download_user_id)):
    """
    Serve files from the downloads directory for a specific session.
    """
    try:
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        # User-specific download directory
        download_dir = os.path.join(backend_dir, "tmp", user_id, session_id)
        file_path = os.path.join(download_dir, filename)

        if not os.path.exists(file_path):
            logger.error(f"File not found for user {user_id}: {file_path}")
            return JSONResponse(status_code=404, content={"error": "File not found"})

        # Security: check that the file is actually inside the download_dir
        if not os.path.abspath(file_path).startswith(os.path.abspath(download_dir)):
            logger.error(f"Security violation: path traversal attempt by user {user_id} for {filename}")
            return JSONResponse(status_code=403, content={"error": "Forbidden"})

        return FileResponse(
            path=file_path,
            filename=filename,
            media_type='application/octet-stream'
        )
    except Exception as e:
        logger.error(f"Download failed for user {user_id}: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# =============================================================================
# A2A Agent Authentication
# =============================================================================

def validate_agent_api_key(api_key: str) -> bool:
    """
    Validate an API key for Agent-to-Agent (A2A) communication.

    Remote agents connecting to the orchestrator can authenticate
    using an API key configured in the AGENT_API_KEY environment variable.
    This is used for server-to-server communication between the
    orchestrator and agents running on remote servers.

    Feature 028 (FR-016): an UNSET key fails closed outside explicitly
    declared development mode — production deployments must configure
    AGENT_API_KEY (or rely on JWT-validated A2A) for agent connections.
    Pre-028 this returned True when unset, silently allowing
    unauthenticated agent connections in any environment.
    """
    configured_key = os.getenv("AGENT_API_KEY", "")
    if not configured_key:
        from orchestrator.session_store import is_dev_mode
        if is_dev_mode():
            return True  # keyless local dev remains supported (spec A13)
        logger.warning(
            "AGENT_API_KEY is not configured and ASTRAL_ENV is not 'development' — "
            "refusing unauthenticated agent connection (fail closed, 028 FR-016)"
        )
        return False
    import hmac
    # Constant-time comparison so a timing side-channel can't reveal the key
    # byte-by-byte. configured_key is guaranteed non-empty here; coerce a
    # possibly-None presented key to "" so compare_digest never sees None.
    return hmac.compare_digest(api_key or "", configured_key)
