"""Owner-scoped REST routes for the Atlas page lifecycle and explicit source promotion:
creates, reads, bounded revisions, backlinks, deletions, owner-reviewed promotion proposals,
and single-use approvals over atlas_service.py, rejecting generated-import payloads and
mapping lifecycle failures to precise HTTP outcomes.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from audit.hooks import record_generic
from orchestrator.auth import get_current_user_payload, require_user_id
from personalization.atlas_pages import GLOBAL_PROJECT, AtlasPageError
from personalization.atlas_promotions import (
    MAX_PROMOTION_SOURCES,
    MAX_PROMOTION_TOKEN_BYTES,
    PromotionSourceInput,
)
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


class PromotionSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: Literal["note", "memory"] = Field(
        validation_alias=AliasChoices("kind", "type", "source_type")
    )
    id: str = Field(validation_alias=AliasChoices("id", "source_id"))
    revision: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("revision", "source_revision"),
    )


class PromotionProposeRequest(_NoGeneratedImports):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: PromotionSourceRequest | None = None
    sources: list[PromotionSourceRequest] | None = Field(
        default=None, max_length=MAX_PROMOTION_SOURCES
    )
    slug: str = Field(
        min_length=2,
        max_length=128,
        validation_alias=AliasChoices("slug", "destination_slug", "page_slug"),
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("title", "destination_title", "page_title"),
    )
    mode: Literal["auto", "create", "append"] = "auto"
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _flatten_destination(cls, values):
        if not isinstance(values, dict) or "destination" not in values:
            return values
        values = dict(values)
        destination = values.pop("destination")
        if isinstance(destination, str):
            values.setdefault("slug", destination)
            return values
        if not isinstance(destination, dict):
            raise ValueError("promotion destination is invalid")
        for key in ("slug", "title", "mode", "expected_revision"):
            if key not in destination:
                continue
            if key in values and values[key] != destination[key]:
                raise ValueError("promotion destination is ambiguous")
            values[key] = destination[key]
        return values

    @model_validator(mode="after")
    def _selected_sources(self):
        values = list(self.sources or ())
        if self.source is not None:
            values.insert(0, self.source)
        if not values:
            raise ValueError("promotion source is required")
        if len(values) > MAX_PROMOTION_SOURCES:
            raise ValueError("too many promotion sources")
        identities = [(item.kind, item.id) for item in values]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate promotion source")
        return self

    def source_inputs(self) -> tuple[PromotionSourceInput, ...]:
        values = list(self.sources or ())
        if self.source is not None:
            values.insert(0, self.source)
        return tuple(
            PromotionSourceInput(item.kind, item.id, item.revision) for item in values
        )


class PromotionApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    approval_token: str = Field(
        min_length=1,
        max_length=MAX_PROMOTION_TOKEN_BYTES * 2,
        validation_alias=AliasChoices("approval_token", "token"),
    )
    proposal_digest: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    approved: bool = True


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


def _source_identity(value) -> dict[str, Any]:
    return {
        "kind": value.kind,
        "id": value.source_id,
        "revision": value.revision,
        "category": value.category,
        "digest": value.digest,
    }


def _proposal_response(value) -> dict[str, Any]:
    sources = [_source_identity(source) for source in value.sources]
    return {
        "proposal_id": value.proposal_id,
        "proposal_digest": value.proposal_digest,
        "approval_token": value.approval_token,
        "expires_at": value.expires_at,
        "owner_id": value.owner_id,
        "project_id": value.project_id,
        "action": value.action,
        "source": sources[0] if len(sources) == 1 else None,
        "sources": sources,
        "destination": {
            "page_id": value.destination.page_id,
            "slug": value.destination.slug,
            "revision": value.destination.revision,
            "fingerprint": value.destination.fingerprint,
        },
        "intended_changes": {
            "title": value.title,
            "body": value.body,
            "sources": list(value.source_references),
            "expected_revision": value.destination.revision,
        },
    }


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
    cursor: str | None = Query(default=None, min_length=1, max_length=4096),
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    service = _service(request)
    try:
        batch = service.list_page_batch(
            owner_id=user_id, project_id=_project_scope(project_id), limit=limit, cursor=cursor
        )
        pages = batch.pages
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
    return {"pages": [_summary(page) for page in pages], "count": len(pages),
            "next_cursor": batch.next_cursor}


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


@atlas_router.post(
    "/projects/{project_id}/promotions", status_code=status.HTTP_201_CREATED
)
@atlas_router.post(
    "/projects/{project_id}/promotions/propose",
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def propose_promotion(
    project_id: str,
    body: PromotionProposeRequest,
    request: Request,
    user_id: str = Depends(require_user_id),
):
    service = _service(request)
    try:
        proposal = service.propose_promotion(
            owner_id=user_id,
            project_id=_project_scope(project_id),
            sources=body.source_inputs(),
            slug=body.slug,
            title=body.title,
            mode=body.mode,
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
    return _proposal_response(proposal)


@atlas_router.post("/projects/{project_id}/promotions/{proposal_id}/approve")
@atlas_router.post(
    "/projects/{project_id}/promotions/{proposal_id}/apply",
    include_in_schema=False,
)
async def approve_promotion(
    project_id: str,
    proposal_id: str,
    body: PromotionApproveRequest,
    request: Request,
    user_id: str = Depends(require_user_id),
):
    service = _service(request)
    try:
        applied = service.approve_promotion(
            owner_id=user_id,
            project_id=_project_scope(project_id),
            proposal_id=proposal_id,
            approval_token=body.approval_token,
            proposal_digest=body.proposal_digest,
            approved=body.approved,
        )
    except (
        AtlasPageError,
        AtlasNotFound,
        AtlasConflict,
        AtlasRefused,
        AtlasUnavailable,
    ) as exc:
        raise _translate(exc) from None
    payload = _summary(applied.page)
    payload["proposal_id"] = applied.proposal_id
    payload["applied"] = True
    payload["promotion_sources"] = list(applied.sources)
    return payload


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
