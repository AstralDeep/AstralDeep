"""BFF auth proxy and JWT verification for the REST API: exchanges OIDC tokens with
Keycloak without exposing the client_secret to the browser, and supplies the
require_user_id/verify_admin dependencies and the personal-node bearer and sign-in refusals.
"""

import os
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, FileResponse
from jose import jwt as jose_jwt  # noqa: F401  (read as auth.jose_jwt by human_request_authority)

import shared  # noqa: F401
from shared import node_identity

logger = logging.getLogger("AuthProxy")

NODE_BEARER_REFUSED = "node.bearer_refused"
NODE_CALLER_SCHEME = "AstralCaller"


def node_caller_challenge(realm: str, error: str = NODE_BEARER_REFUSED) -> str:
    return f'{NODE_CALLER_SCHEME} realm="{realm}", error="{error}"'

auth_router = APIRouter()


def refuse_node_bearer() -> None:
    if node_identity.is_personal():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=NODE_BEARER_REFUSED)


def _get_keycloak_config():
    authority = os.getenv("KEYCLOAK_AUTHORITY", "")
    client_id = os.getenv("KEYCLOAK_CLIENT_ID", "")
    client_secret = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
    return authority, client_id, client_secret


security = HTTPBearer(auto_error=False)


def _reject_non_first_party(payload: dict) -> None:
    from shared.auth_clients import is_first_party_user_claims
    ok, reason = is_first_party_user_claims(payload)
    if not ok:
        logger.warning(f"Rejecting non-first-party token at REST auth: {reason}")
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_payload(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    refuse_node_bearer()
    if request.method == "OPTIONS":
        return {}
        
    token = None
    if credentials:
        token = credentials.credentials
    else:
        token_param = request.query_params.get("token")
        if token_param:
            token = token_param
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        request.state.delegation_subject_token = token
    except Exception:
        pass
    payload = await verify_production_token(token)
    try:
        request.state.audit_claims = payload
    except Exception:
        pass
    return payload


async def verify_production_token(token: str) -> dict:
    refuse_node_bearer()
    authority, client_id, _ = _get_keycloak_config()
    if not authority or not client_id:
        raise HTTPException(status_code=500, detail="Auth not configured")
    try:
        from shared.token_verify import verify_idp_token
        payload = await verify_idp_token(token, extra_issuers=[authority])
        _reject_non_first_party(payload)
        return payload
    except HTTPException:
        raise
    except Exception:
        logger.error("Token validation failed in auth wrapper")
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_id(payload: dict = Depends(get_current_user_payload)) -> Optional[str]:
    if not payload:
        return None
    return payload.get("sub")


async def require_user_id(
    request: Request,
    payload: dict = Depends(get_current_user_payload),
) -> str:
    user_id = payload.get("sub") if payload else None
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    try:
        orch = getattr(request.app.state, "orchestrator", None)
        if not orch:
            root_app = getattr(request.app, "_root_app", None) or request.app
            orch = getattr(root_app.state, "orchestrator", None)
        if orch:
            orch._save_user_profile(payload)
    except Exception:
        pass
    return user_id


async def get_download_user_payload(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    refuse_node_bearer()
    return await get_current_user_payload(request, credentials)


async def require_download_user_id(
    request: Request,
    payload: dict = Depends(get_download_user_payload),
) -> str:
    return await require_user_id(request, payload)


async def get_web_or_bearer_user_payload(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    refuse_node_bearer()
    return await get_current_user_payload(request, credentials)


async def require_user_id_or_web_session(
    request: Request,
    payload: dict = Depends(get_web_or_bearer_user_payload),
) -> str:
    return await require_user_id(request, payload)


def _extract_roles(user_data: dict) -> list:
    roles = user_data.get("realm_access", {}).get("roles", [])
    if isinstance(roles, list):
        roles = roles.copy()
    if "resource_access" in user_data:
        client_id = os.getenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
        if client_id in user_data["resource_access"]:
            client_roles = user_data["resource_access"][client_id].get("roles", [])
            roles.extend(client_roles)
        if "account" in user_data["resource_access"]:
            account_roles = user_data["resource_access"]["account"].get("roles", [])
            roles.extend(account_roles)
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
        logger.warning("verify_admin: empty principal — denying (fail closed)")
        raise HTTPException(status_code=403, detail="Not authorized (Requires 'admin' role)")
    roles = _extract_roles(user_data)
    logger.debug(f"verify_admin: extracted roles = {roles}")
    if "admin" not in roles:
        logger.warning(f"verify_admin: admin role missing, roles = {roles}")
        raise HTTPException(status_code=403, detail="Not authorized (Requires 'admin' role)")
    logger.debug("verify_admin: admin role present")
    user_data["is_admin"] = True
    return user_data


@auth_router.get(
    "/api/download/{session_id}/{filename}",
    tags=["Files"],
    summary="Download a file",
    description=(
        "Download a previously uploaded or generated file by session ID and filename."
    ),
)
async def download_file(session_id: str, filename: str, user_id: str = Depends(require_download_user_id)):
    try:
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        download_dir = os.path.join(backend_dir, "tmp", user_id, session_id)
        file_path = os.path.join(download_dir, filename)

        if not os.path.exists(file_path):
            logger.error(f"File not found for user {user_id}: {file_path}")
            return JSONResponse(status_code=404, content={"error": "File not found"})

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


def validate_agent_api_key(api_key: str) -> bool:
    configured_key = os.getenv("AGENT_API_KEY", "")
    if not configured_key:
        from orchestrator.boot_posture import is_dev_mode
        if is_dev_mode():
            return True
        logger.warning(
            "AGENT_API_KEY is not configured and ASTRAL_ENV is not 'development' — "
            "refusing unauthenticated agent connection (fail closed, 028 FR-016)"
        )
        return False
    import hmac
    return hmac.compare_digest(api_key or "", configured_key)
