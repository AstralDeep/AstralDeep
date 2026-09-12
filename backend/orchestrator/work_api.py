"""Fresh-authentication reads only; no durable-work mutation API is advertised."""
from functools import wraps
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from orchestrator.auth import get_web_or_bearer_user_payload, verify_user
from orchestrator.work_service import WorkService
from persistent_agents.models import AssignmentError

_CLAIMS = Depends(get_web_or_bearer_user_payload)


async def _read_owner(claims: dict = _CLAIMS):
    # Authentication already resolved the current credential and audit claims.
    # Read delivery must not invoke require_user_id's synchronous profile write.
    owner_id = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(owner_id, str) or not owner_id:
        raise HTTPException(401, "Not authenticated")
    await verify_user(claims)
    return owner_id


_OWNER = Depends(_read_owner)


def _json(value, status=200, headers=None):
    return JSONResponse(value, status_code=status, headers={**(headers or {}), "Cache-Control": "no-store"})


class WorkReadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        @wraps(handler)
        async def safe(request):
            try:
                return await handler(request)
            except AssignmentError as exc:
                known = {
                    "work_not_found", "work_query_invalid", "work_repository_unavailable",
                    "work_read_unavailable", "assignment_feature_disabled",
                    "assignment_owner_required", "assignment_human_required",
                }
                return _json({"error": exc.code if exc.code in known else "work_read_unavailable"}, exc.status_code)
            except RequestValidationError:
                return _json({"error": "work_query_invalid"}, 422)
            except HTTPException as exc:
                headers = {key: value for key, value in (exc.headers or {}).items()
                           if key.lower() in {"location", "www-authenticate"}}
                return _json({"error": "work_authentication_required"}, exc.status_code, headers)
        return safe


# api.operation_router supplies the single /api prefix.
work_router = APIRouter(prefix="/work/v1/operations", tags=["Work reads"], route_class=WorkReadRoute)


def _service(request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root = getattr(request.app, "_root_app", request.app)
        orch = getattr(root.state, "orchestrator", None)
    backing = getattr(orch, "persistent_assignments", None)
    if backing is None:
        raise AssignmentError("work_read_unavailable", 503)
    return WorkService(backing)


def _query_integer(value, maximum=2**63 - 1):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise AssignmentError("work_query_invalid", 422)
    parsed = int(value)
    if parsed > maximum:
        raise AssignmentError("work_query_invalid", 422)
    return parsed


@work_router.get("")
async def list_work(request: Request, limit: str = Query("50"), after_id: str | None = None,
                    owner_id: str = _OWNER, claims: dict = _CLAIMS):
    return _json(await _service(request).list(owner_id, claims, limit=_query_integer(limit, 100), after_id=after_id))


@work_router.get("/{identity}")
async def get_work(identity: str, request: Request, owner_id: str = _OWNER, claims: dict = _CLAIMS):
    return _json({"operation": await _service(request).get(owner_id, claims, identity)})


@work_router.get("/{identity}/poll")
async def poll_work(identity: str, request: Request, after_revision: str | None = None,
                    owner_id: str = _OWNER, claims: dict = _CLAIMS):
    # An immediate poll is a fresh HTTP request through the same institutional
    # bearer/cookie dependency. Never retain an identity for later deliveries.
    revision = None if after_revision is None else _query_integer(after_revision)
    return _json(await _service(request).poll(owner_id, claims, identity, after_revision=revision))
