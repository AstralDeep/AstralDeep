"""Bearer refusals and the require_user_id/verify_admin dependencies for the REST API; every bearer is refused."""

import os
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, FileResponse
from jose import jwt as jose_jwt  # noqa: F401  (read as auth.jose_jwt by human_request_authority)

import shared  # noqa: F401

logger = logging.getLogger("AuthProxy")

NODE_BEARER_REFUSED = "node.bearer_refused"
NODE_CALLER_SCHEME = "AstralCaller"


def node_caller_challenge(realm: str, error: str = NODE_BEARER_REFUSED) -> str:
    return f'{NODE_CALLER_SCHEME} realm="{realm}", error="{error}"'

auth_router = APIRouter()


def refuse_node_bearer() -> None:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=NODE_BEARER_REFUSED)


security = HTTPBearer(auto_error=False)


async def get_current_user_payload(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    refuse_node_bearer()


async def verify_production_token(token: str) -> dict:
    refuse_node_bearer()


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
