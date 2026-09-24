"""Bounded, owner/revision-fenced HTTP export of a visible canvas capture via
ExportService and AstralProjection's webrender; never a committed result, audit
write, or authority claim. Registered by orchestrator/api.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from functools import wraps
from urllib.parse import urlsplit

from astralplane.async_runtime import AsyncPlaneRuntime
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import ClientDisconnect
from webrender.export_presentation import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    VERSION,
    PresentationError,
    render_presentation,
)

from orchestrator import auth
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from shared.feature_flags import flags

logger = logging.getLogger("Orchestrator.WorkspaceExport")
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer"}
_MAX_REVISION = 2**63 - 1
_READ_SECONDS = 10.0
_RENDER_SECONDS = 15.0


class ExportRefusal(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status


@dataclass(frozen=True, repr=False)
class ExportIdentity:
    owner: str
    credential: HTTPAuthorizationCredentials | None = field(repr=False)


async def export_identity(
    request: Request, credential: HTTPAuthorizationCredentials | None = Depends(auth.security),
) -> ExportIdentity:
    if "token" in request.query_params:
        raise ExportRefusal("presentation_query_token_refused", 403)
    claims = await auth.get_web_or_bearer_user_payload(request, credential)
    owner = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(owner, str) or not owner.strip() or not 1 <= len(owner) <= 512:
        raise HTTPException(401)
    await auth.verify_user(claims)
    return ExportIdentity(owner, credential)


def _origin(value: str, *, base: bool = False):
    try:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or (parts.path and not base)):
            raise ValueError
        return parts.scheme, parts.hostname, parts.port if parts.port is not None else (
            443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise ExportRefusal("presentation_origin_refused", 403) from exc


def _request_policy(request: Request, identity: ExportIdentity) -> int:
    types = request.headers.getlist("content-type")
    if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
        raise ExportRefusal("presentation_json_required", 415)
    encodings = request.headers.getlist("content-encoding")
    if encodings and (len(encodings) != 1 or encodings[0].lower() != "identity"):
        raise ExportRefusal("presentation_encoding_refused", 415)
    if identity.credential is None:
        origins = request.headers.getlist("origin")
        base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_PUBLIC_URL") or str(request.base_url)
        if len(origins) != 1 or _origin(origins[0]) != _origin(base, base=True):
            raise ExportRefusal("presentation_origin_refused", 403)
    if set(request.query_params) != {"render_revision"} or len(request.query_params.getlist("render_revision")) != 1:
        raise ExportRefusal("presentation_revision_invalid", 422)
    revision = request.query_params["render_revision"]
    if not re.fullmatch(r"0|[1-9][0-9]{0,18}", revision) or int(revision) > _MAX_REVISION:
        raise ExportRefusal("presentation_revision_invalid", 422)
    return int(revision)


def _enabled():
    if not flags.is_enabled("artifact_export"):
        raise ExportRefusal("presentation_not_found", 404)


async def _body(request: Request) -> bytes:
    lengths = request.headers.getlist("content-length")
    declared = None
    if lengths:
        if len(lengths) != 1 or not re.fullmatch(r"0|[1-9][0-9]{0,18}", lengths[0]):
            raise ExportRefusal("presentation_length_invalid", 400)
        declared = int(lengths[0])
        if declared > MAX_INPUT_BYTES:
            raise ExportRefusal("presentation_too_large", 413)
    data = bytearray()
    async with asyncio.timeout(_READ_SECONDS):
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_INPUT_BYTES:
                raise ExportRefusal("presentation_too_large", 413)
            data.extend(chunk)
    if declared is not None and declared != len(data):
        raise ExportRefusal("presentation_length_invalid", 400)
    return bytes(data)


class ExportService:
    def __init__(self, orchestrator):
        source = plane_source_from_orchestrator(orchestrator)
        self.orchestrator = orchestrator
        self.repository = source.plane_repositories.history.conversations
        self.runtime = AsyncPlaneRuntime(source.plane_runtime, maximum_concurrency=2,
                                         admission_timeout_seconds=1.0)
        self.capacity = threading.BoundedSemaphore(2)

    async def check(self, owner: str, chat: str, revision: int):
        _enabled()
        record = await self.runtime.run_in_transaction(lambda tx: self.repository.get(
            tx, owner_id=owner, conversation_id=chat))
        if record is None:
            raise ExportRefusal("presentation_not_found", 404)
        if type(record.render_revision) is not int or record.render_revision != revision:
            raise ExportRefusal("presentation_revision_changed", 409)

    async def present(self, request: Request, identity: ExportIdentity, chat: str, revision: int) -> bytes:
        if not self.capacity.acquire(blocking=False):
            raise ExportRefusal("presentation_busy", 429)
        worker = None
        deferred_release = False
        try:
            await self.check(identity.owner, chat, revision)
            payload = await _body(request)
            worker = asyncio.create_task(asyncio.to_thread(self._render, payload))
            async with asyncio.timeout(_RENDER_SECONDS):
                output = await asyncio.shield(worker)
            refreshed = await export_identity(request, identity.credential)
            if refreshed.owner != identity.owner:
                raise ExportRefusal("presentation_identity_changed", 401)
            await self.check(identity.owner, chat, revision)
            return output
        finally:
            if worker is not None and not worker.done():
                deferred_release = True
                worker.add_done_callback(self._release_finished)
            if not deferred_release:
                self.capacity.release()

    @staticmethod
    def _render(payload: bytes) -> bytes:
        output = json.dumps(render_presentation(payload), ensure_ascii=False,
                            allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(output) > MAX_OUTPUT_BYTES:
            raise ExportRefusal("presentation_output_too_large", 413)
        return output

    def _release_finished(self, worker):
        try:
            if not worker.cancelled():
                worker.exception()
        finally:
            self.capacity.release()


class PresentationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        @wraps(handler)
        async def safe(request):
            try:
                return await handler(request)
            except ExportRefusal as exc:
                code, status = exc.code, exc.status
            except PresentationError:
                code, status = "presentation_invalid", 422
            except HTTPException as exc:
                code, status = (("presentation_authentication_required", exc.status_code)
                                if exc.status_code in {401, 403} else ("presentation_unavailable", 503))
            except (TimeoutError, ClientDisconnect):
                code, status = "presentation_interrupted", 408
            except Exception:
                code, status = "presentation_unavailable", 503
            logger.info("Workspace presentation refused: %s", code)
            return Response(json.dumps({"error": code}), status_code=status,
                            media_type="application/json", headers=_HEADERS)

        return safe


workspace_export_router = APIRouter(route_class=PresentationRoute)


@workspace_export_router.post(
    "/canvas/{chat_id}/presentation", summary="Render an ephemeral visible canvas capture",
    description=("Independently authenticates the current owner and exact render_revision. "
                 "Accepts the closed astral.canvas-export/v1 representation (components, viewport, "
                 "thirteen-role theme, display_state and loaded PNG images). Never stores or attests "
                 "the input. Returns inert markup for an isolated native finalizer; this is not Share."),
    openapi_extra={"parameters": [{"name": "render_revision", "in": "query", "required": True,
                                  "schema": {"type": "string", "pattern": "^(0|[1-9][0-9]{0,18})$"},
                                  "description": "Exact captured server revision, at most 2^63-1."}],
                   "requestBody": {"required": True, "content": {"application/json": {
        "schema": {"type": "object", "required": ["version", "components", "viewport", "theme", "display_state", "images"],
                   "additionalProperties": False, "properties": {
                       "version": {"type": "string", "enum": [VERSION]}, "components": {"type": "array"},
                       "viewport": {"type": "object"}, "theme": {"type": "object"},
                       "display_state": {"type": "array"}, "images": {"type": "array"}}}}}}},
)
async def export_presentation(
    chat_id: str, request: Request, identity: ExportIdentity = Depends(export_identity),
):
    from orchestrator.api import _get_orchestrator

    revision = _request_policy(request, identity)
    if not chat_id.strip() or not 1 <= len(chat_id) <= 256:
        raise ExportRefusal("presentation_not_found", 404)
    _enabled()
    orch = _get_orchestrator(request)
    service = getattr(request.app.state, "workspace_export_service", None)
    if service is None or service.orchestrator is not orch:
        service = ExportService(orch)
        request.app.state.workspace_export_service = service
    output = await service.present(request, identity, chat_id, revision)
    if _get_orchestrator(request) is not orch:
        raise ExportRefusal("presentation_unavailable", 503)
    return Response(output, media_type="application/json",
                    headers={**_HEADERS, "X-Astral-Render-Revision": str(revision)})
