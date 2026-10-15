"""Owner-scoped REST routes for the Atlas page lifecycle: creates, reads, bounded
revisions, backlinks, and deletions over atlas_service.py, rejecting generated-import
payloads and mapping lifecycle failures to precise HTTP outcomes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from audit.hooks import record_generic
from orchestrator.auth import get_current_user_payload, require_user_id
from personalization.atlas_pages import GLOBAL_PROJECT, AtlasPageError
from personalization.atlas_service import (
    AtlasConflict,
    AtlasNotFound,
    AtlasRefused,
    AtlasUnavailable,
    AtlasPageService,
    PageInput,
)

logger = logging.getLogger("Personalization.Atlas")

atlas_router = APIRouter(prefix="/api/atlas", tags=["Atlas"])

_GENERATED_IMPORT_KEYS = frozenset(
    {"import", "imports", "generated", "generated_import", "from_generated"}
)


class _NoGeneratedImports(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_generated_imports(cls, values: Any) -> Any:
        if isinstance(values, dict) and set(values) & _GENERATED_IMPORT_KEYS:
            raise ValueError("generated_imports_unavailable")
        return values


class PageCreateRequest(_NoGeneratedImports):
    slug: str = Field(min_length=2, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=8192)
    sources: list[str] = Field(default_factory=list, max_length=8)
    links: list[str] = Field(default_factory=list, max_length=16)


class PageUpdateRequest(_NoGeneratedImports):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    body: str | None = Field(default=None, min_length=1, max_length=8192)
    sources: list[str] | None = Field(default=None, max_length=8)
    links: list[str] | None = Field(default=None, max_length=16)


def _orchestrator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    return orch


def _service(request: Request) -> AtlasPageService:
    orch = _orchestrator(request)
    plane = getattr(getattr(orch, "runtime_composition", None), "plane", None)
    fernet = getattr(getattr(orch, "credential_manager", None), "_fernet", None)
    try:
        return AtlasPageService(
            plane_runtime=getattr(plane, "runtime", None),
            plane_repositories=getattr(plane, "repositories", None),
            audit_repo=getattr(orch, "audit_repo", None),
            fernet=fernet,
        )
    except AtlasUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="atlas_unavailable",
        ) from None


def _project_scope(project_id: str) -> str | None:
    return None if project_id == GLOBAL_PROJECT else project_id


def _summary(value) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "page_id": value.page_id,
        "owner_id": value.owner_id,
        "project_id": value.project_id,
        "slug": value.slug,
        "title": value.title,
        "revision": value.revision,
        "sources": list(value.sources),
        "created_at": value.created_at,
        "updated_at": value.updated_at,
        "deleted": value.deleted,
    }
    if value.fingerprint is not None:
        payload["fingerprint"] = value.fingerprint
    if value.body is not None:
        payload["body"] = value.body
    if value.revisions_removed:
        payload["revisions_removed"] = value.revisions_removed
    if value.links_removed:
        payload["links_removed"] = value.links_removed
    return payload


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, AtlasPageError):
        return HTTPException(status_code=exc.status_code, detail=exc.code)
    if isinstance(exc, AtlasNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.code)
    if isinstance(exc, AtlasConflict):
        content: dict[str, Any] = {"error": exc.code}
        if exc.current_revision is not None:
            content["current_revision"] = exc.current_revision
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=content)
    if isinstance(exc, AtlasRefused):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": exc.code, "reason": "protected health information"},
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="atlas_unavailable"
    )


async def _audit_view(payload: dict, *, action: str, description: str, outputs: dict) -> None:
    await record_generic(
        claims=payload,
        event_class="memory",
        action_type=action,
        description=description,
        outputs_meta=outputs,
    )


@atlas_router.get("/projects/{project_id}/pages")
async def list_pages(
    project_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    service = _service(request)
    try:
        pages = service.list_pages(
            owner_id=user_id, project_id=_project_scope(project_id), limit=limit
        )
    except (
        AtlasPageError,
        AtlasNotFound,
        AtlasConflict,
        AtlasRefused,
        AtlasUnavailable,
    ) as exc:
        raise _translate(exc) from None
    await _audit_view(
        payload,
        action="atlas_pages_view",
        description="Viewed Atlas page list",
        outputs={"project": project_id, "count": len(pages)},
    )
    return {"pages": [_summary(page) for page in pages], "count": len(pages)}


@atlas_router.post("/projects/{project_id}/pages", status_code=status.HTTP_201_CREATED)
async def create_page(
    project_id: str,
    body: PageCreateRequest,
    request: Request,
    user_id: str = Depends(require_user_id),
):
    service = _service(request)
    try:
        page = service.create_page(
            owner_id=user_id,
            page=PageInput(
                slug=body.slug,
                title=body.title,
                body=body.body,
                sources=tuple(body.sources),
                links=tuple(body.links),
                project_id=_project_scope(project_id),
            ),
        )
    except (
        AtlasPageError,
        AtlasNotFound,
        AtlasConflict,
        AtlasRefused,
        AtlasUnavailable,
    ) as exc:
        raise _translate(exc) from None
    return _summary(page)


@atlas_router.get("/projects/{project_id}/pages/{page_ref}")
async def get_page(
    project_id: str,
    page_ref: str,
    request: Request,
    revision: int | None = Query(default=None, ge=1),
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    service = _service(request)
    try:
        page = service.get_page(
            owner_id=user_id,
            project_id=_project_scope(project_id),
            reference=page_ref,
            revision=revision,
        )
    except (
        AtlasPageError,
        AtlasNotFound,
        AtlasConflict,
        AtlasRefused,
        AtlasUnavailable,
    ) as exc:
        raise _translate(exc) from None
    await _audit_view(
        payload,
        action="atlas_page_view",
        description="Viewed Atlas page",
        outputs={"page_id": page.page_id},
    )
    return _summary(page)


@atlas_router.put("/projects/{project_id}/pages/{page_ref}")
async def update_page(
    project_id: str,
    page_ref: str,
    body: PageUpdateRequest,
    request: Request,
    user_id: str = Depends(require_user_id),
):
    service = _service(request)
    try:
        page = service.update_page(
            owner_id=user_id,
            project_id=_project_scope(project_id),
            reference=page_ref,
            title=body.title,
            body=body.body,
            sources=tuple(body.sources) if body.sources is not None else None,
            links=tuple(body.links) if body.links is not None else None,
            expected_revision=body.expected_revision,
        )
    except (
        AtlasPageError,
        AtlasNotFound,
        AtlasConflict,
        AtlasRefused,
        AtlasUnavailable,
    ) as exc:
        raise _translate(exc) from None
    return _summary(page)


@atlas_router.delete("/projects/{project_id}/pages/{page_ref}")
async def delete_page(
    project_id: str,
    page_ref: str,
    request: Request,
    expected_revision: int = Query(ge=1),
    user_id: str = Depends(require_user_id),
):
    service = _service(request)
    try:
        page = service.delete_page(
            owner_id=user_id,
            project_id=_project_scope(project_id),
            reference=page_ref,
            expected_revision=expected_revision,
        )
    except (
        AtlasPageError,
        AtlasNotFound,
        AtlasConflict,
        AtlasRefused,
        AtlasUnavailable,
    ) as exc:
        raise _translate(exc) from None
    return _summary(page)


@atlas_router.get("/projects/{project_id}/pages/{page_ref}/revisions")
async def page_revisions(
    project_id: str,
    page_ref: str,
    request: Request,
    user_id: str = Depends(require_user_id),
):
    service = _service(request)
    try:
        history = service.page_history(
            owner_id=user_id,
            project_id=_project_scope(project_id),
            reference=page_ref,
        )
    except (
        AtlasPageError,
        AtlasNotFound,
        AtlasConflict,
        AtlasRefused,
        AtlasUnavailable,
    ) as exc:
        raise _translate(exc) from None
    return {"revisions": [_summary(entry) for entry in history], "count": len(history)}


@atlas_router.get("/projects/{project_id}/pages/{page_ref}/links")
async def page_links(
    project_id: str,
    page_ref: str,
    request: Request,
    user_id: str = Depends(require_user_id),
):
    service = _service(request)
    try:
        links = service.page_links(
            owner_id=user_id,
            project_id=_project_scope(project_id),
            reference=page_ref,
        )
    except (
        AtlasPageError,
        AtlasNotFound,
        AtlasConflict,
        AtlasRefused,
        AtlasUnavailable,
    ) as exc:
        raise _translate(exc) from None
    return {"links": [_summary(entry) for entry in links], "count": len(links)}
