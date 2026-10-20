"""Owner-authenticated HTTP surface for personalization/federated_search.py, exposing one
bounded query across notes, memory, conversations, and Atlas with source/type/project filters,
stable cursors, sanitized snippets, and per-source availability reports.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from audit.hooks import record_generic
from orchestrator.auth import get_current_user_payload, require_user_id
from personalization.federated_search import (
    FederatedSearchService,
    get_search_service,
    SearchQueryInvalid,
    hit_payload,
    report_payload,
)

logger = logging.getLogger("Personalization.Search")

search_router = APIRouter(prefix="/api/search", tags=["Search"])


def _orchestrator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    return orch


def _service(request: Request) -> FederatedSearchService:
    orch = _orchestrator(request)
    if orch is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="search_unavailable",
        )
    try:
        return get_search_service(orch)
    except Exception:
        logger.warning("federated_search_service_unavailable", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="search_unavailable",
        ) from None


@search_router.get("")
async def search_personal_knowledge(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    source: str | None = Query(default=None, max_length=64),
    type: str | None = Query(default=None, max_length=256),
    project_id: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    service = _service(request)
    try:
        outcome = await service.search(
            owner_id=user_id,
            query=q,
            sources=source,
            types=type,
            project_id=project_id,
            limit=limit,
            cursor=cursor,
        )
    except SearchQueryInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.code,
        ) from None
    reports = [report_payload(report) for report in outcome.reports]
    if not outcome.any_available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "search_sources_unavailable", "reports": reports},
        )
    await record_generic(
        claims=payload,
        event_class="personalization",
        action_type="personalization.search",
        description="Searched notes, memory, conversations, and Atlas",
        inputs_meta={
            "sources": list(outcome.sources),
            "types": list(outcome.types),
            "project_filter": bool(outcome.project_id),
            "query_chars": len(outcome.query),
            "has_cursor": outcome.next_cursor is not None or bool(cursor),
        },
        outputs_meta={
            "count": len(outcome.hits),
            "total": outcome.total,
            "unavailable": [report.source for report in outcome.reports if not report.available],
            "incomplete": [report.source for report in outcome.reports if not report.complete],
        },
    )
    response: dict[str, Any] = {
        "query": outcome.query,
        "sources": list(outcome.sources),
        "types": list(outcome.types),
        "project_id": outcome.project_id,
        "count": len(outcome.hits),
        "total": outcome.total,
        "partial": outcome.partial,
        "hits": [hit_payload(hit) for hit in outcome.hits],
        "reports": reports,
        "next_cursor": outcome.next_cursor,
    }
    return response
