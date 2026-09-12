"""Fresh owner-authenticated Work reads and bounded lifecycle controls."""
from functools import wraps
import os
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from orchestrator.auth import get_web_or_bearer_user_payload, verify_user
from orchestrator.work_controls import WorkControlRequest, WorkControlService, WorkDeleteRequest
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


def _origin(value, *, base=False):
    try:
        if not isinstance(value, str) or value != value.strip():
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or (parsed.path and not base)):
            raise ValueError
        port = parsed.port
        return parsed.scheme, parsed.hostname, port if port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise AssignmentError("work_origin_refused", 403) from exc


async def _write_owner(request: Request, owner_id: str = _OWNER):
    # Query tokens remain an existing read contract, never write credentials.
    if "token" in request.query_params:
        raise AssignmentError("work_query_token_refused", 403)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise AssignmentError("work_json_required", 415)
    authorization = request.headers.get("authorization", "").split(None, 1)
    if len(authorization) == 2 and authorization[0].lower() == "bearer":
        # The normal dependency already verified this explicit credential.
        return owner_id
    origins = request.headers.getlist("origin")
    if len(origins) != 1:
        raise AssignmentError("work_origin_refused", 403)
    public_base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_PUBLIC_URL") or str(request.base_url)
    if _origin(origins[0]) != _origin(public_base, base=True):
        raise AssignmentError("work_origin_refused", 403)
    return owner_id


_WRITE_OWNER = Depends(_write_owner)


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
                    "work_control_invalid", "work_control_unavailable", "work_origin_refused",
                    "work_query_token_refused", "work_json_required",
                    "assignment_revision_conflict",
                    "assignment_idempotency_conflict", "assignment_not_active",
                    "assignment_not_terminal", "assignment_action_uncertain",
                    "assignment_version_unsupported", "assignment_owner_retired",
                }
                unavailable = "work_read_unavailable" if request.method == "GET" else "work_control_unavailable"
                return _json({"error": exc.code if exc.code in known else unavailable}, exc.status_code)
            except RequestValidationError:
                return _json({"error": "work_query_invalid" if request.method == "GET" else "work_control_invalid"}, 422)
            except HTTPException as exc:
                headers = {key: value for key, value in (exc.headers or {}).items()
                           if key.lower() in {"location", "www-authenticate"}}
                return _json({"error": "work_authentication_required"}, exc.status_code, headers)
        return safe


# api.operation_router supplies the single /api prefix.
work_router = APIRouter(prefix="/work/v1/operations", tags=["Work"], route_class=WorkReadRoute)


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


@work_router.post("/{identity}/pause")
async def pause_work(identity: str, body: WorkControlRequest, request: Request,
                     owner_id: str = _WRITE_OWNER, claims: dict = _CLAIMS):
    """Pause future execution without discarding already issued effects."""
    return _json(await WorkControlService(_service(request).assignments).control(
        owner_id, claims, identity, "pause", body))


@work_router.post("/{identity}/cancel")
async def cancel_work(identity: str, body: WorkControlRequest, request: Request,
                      owner_id: str = _WRITE_OWNER, claims: dict = _CLAIMS):
    """Stop future execution; issued or uncertain effects still require settlement."""
    return _json(await WorkControlService(_service(request).assignments).control(
        owner_id, claims, identity, "cancel", body))


@work_router.delete("/{identity}")
async def delete_work(identity: str, body: WorkDeleteRequest, request: Request,
                      owner_id: str = _WRITE_OWNER, claims: dict = _CLAIMS):
    """Delete only settled terminal work. Repeated/absent/foreign IDs return 404."""
    return _json(await WorkControlService(_service(request).assignments).delete(owner_id, claims, identity, body))
