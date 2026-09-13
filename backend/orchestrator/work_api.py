"""Fresh owner-authenticated Work reads and bounded lifecycle controls."""
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
import math
import os
import re
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from orchestrator import auth
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


def _read_expiry(claims):
    value = claims.get("exp") if isinstance(claims, dict) else None
    try:
        live = type(value) in (int, float) and math.isfinite(value) and time.time() < value
    except OverflowError:
        live = False
    if not live:
        raise HTTPException(401, "Not authenticated")
    return value


@dataclass(frozen=True, repr=False)
class _ReadDelivery:
    """Request-private original IAM identity; never refreshed or serialized."""

    owner_id: str
    expires_at: float
    token: str = field(repr=False)
    cookie_session: tuple[str, str] | None = field(repr=False)

    @classmethod
    def capture(cls, request, owner_id, claims):
        token = getattr(request.state, "delegation_subject_token", None)
        cookie = getattr(request.state, "_authenticated_cookie_session", None)
        if (not isinstance(token, str) or not token or claims.get("sub") != owner_id
                or (cookie is not None and (type(cookie) is not tuple or len(cookie) != 2
                    or any(not isinstance(value, str) or not value for value in cookie)))):
            raise HTTPException(401, "Not authenticated")
        return cls(owner_id, _read_expiry(claims), token, cookie)

    async def verify(self, service):
        if time.time() >= self.expires_at:
            raise HTTPException(401, "Not authenticated")
        # Reuse the exact normal production JWT/issuer/client/role policy, with
        # the original token. Calling ensure_session here could adopt a refresh.
        claims = await verify_user(await auth.verify_production_token(self.token))
        if claims.get("sub") != self.owner_id or _read_expiry(claims) != self.expires_at:
            raise HTTPException(401, "Not authenticated")
        if self.cookie_session is not None:
            await service.assert_read_session(self.owner_id, claims, self.cookie_session)
        service._owner(self.owner_id, claims)
        if time.time() >= self.expires_at:
            raise HTTPException(401, "Not authenticated")


class WorkReadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        @wraps(handler)
        async def safe(request):
            original = request
            if request.method == "GET":
                # Freeze credential selection before normal IAM's first await.
                # Incoming middleware state cannot substitute a private token.
                scope = dict(request.scope)
                scope.update(headers=[(bytes(key), bytes(value)) for key, value in scope["headers"]],
                             query_string=bytes(scope.get("query_string", b"")), state={})
                request = Request(scope, receive=request.receive)
            try:
                return await handler(request)
            except AssignmentError as exc:
                known = {
                    "work_not_found", "work_query_invalid", "work_repository_unavailable",
                    "work_read_unavailable", "assignment_feature_disabled",
                    "work_authentication_required",
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
            finally:
                if request is not original:
                    # Existing HTTP audit middleware owns the outer request.
                    # Preserve only verified attribution, never the private token.
                    claims = getattr(request.state, "audit_claims", None)
                    if isinstance(claims, dict):
                        original.state.audit_claims = deepcopy(claims)
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


async def _read(request, owner_id, claims, method, **kwargs):
    service = _service(request)
    delivery = _ReadDelivery.capture(request, owner_id, claims)
    value = await getattr(service, method)(owner_id, claims, **kwargs)
    await delivery.verify(service)
    if _service(request).assignments is not service.assignments:
        raise AssignmentError("work_read_unavailable", 503)
    return value


@work_router.get("")
async def list_work(request: Request, limit: str = Query("50"), after_id: str | None = None,
                    owner_id: str = _OWNER, claims: dict = _CLAIMS):
    return _json(await _read(request, owner_id, claims, "list", limit=_query_integer(limit, 100), after_id=after_id))


@work_router.get("/{identity}")
async def get_work(identity: str, request: Request, owner_id: str = _OWNER, claims: dict = _CLAIMS):
    return _json({"operation": await _read(request, owner_id, claims, "get", identity=identity)})


@work_router.get("/{identity}/poll")
async def poll_work(identity: str, request: Request, after_revision: str | None = None,
                    owner_id: str = _OWNER, claims: dict = _CLAIMS):
    # An immediate poll is a fresh HTTP request through the same institutional
    # bearer/cookie dependency. Never retain an identity for later deliveries.
    revision = None if after_revision is None else _query_integer(after_revision)
    return _json(await _read(request, owner_id, claims, "poll", identity=identity, after_revision=revision))


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
