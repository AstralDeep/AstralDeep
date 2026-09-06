"""FastAPI router for the Attachment REST surface (feature 002-file-uploads).

Implements the contract from ``specs/002-file-uploads/contracts/upload-api.md``:

* ``POST   /api/upload``                  (replaces the legacy implementation in auth.py)
* ``GET    /api/attachments``             (list current user's live attachments)
* ``GET    /api/attachments/{id}``        (one attachment's metadata)
* ``DELETE /api/attachments/{id}``        (soft-delete)

All endpoints are gated by the existing ``require_user_id`` dependency. Non-owner
reads return ``404`` (we do not confirm or deny the existence of foreign rows).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Literal, Optional

from astralplane import BlobSizeLimitError
from astralplane.errors import PlaneError
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse
from orchestrator.attachments import content_type as ct
from orchestrator.attachments.materialization import (
    AttachmentContentTypeMismatchError,
    materialization_service_from_orchestrator,
)
from orchestrator.attachments.purge import AccountRetirementNeedsReconciliation
from orchestrator.attachments.repository import AttachmentRepository
from orchestrator.auth import require_user_id
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from pydantic import BaseModel

logger = logging.getLogger("AttachmentsAPI")

# Legacy alias: kept for any downstream imports. The real per-upload cap now
# comes from ``content_type.max_bytes_for_category(category)``.
MAX_UPLOAD_BYTES = ct.MAX_BYTES_BY_CATEGORY["document"]

# Stream upload in modest chunks so we can short-circuit oversize files without
# buffering them in memory. Medical uploads can run into the GBs, so the central
# pending-first materialization service stages each chunk under Plane's durable
# lease rather than collecting the request in a list.
_CHUNK_SIZE = 1024 * 256  # 256 KiB
_SNIFF_BYTES = 8192


def _format_cap_mb(cap_bytes: int) -> str:
    """Render a byte cap as a human-friendly '30 MB' / '2 GB' string."""
    if cap_bytes >= 1024 * 1024 * 1024:
        return f"{cap_bytes // (1024 * 1024 * 1024)} GB"
    return f"{cap_bytes // (1024 * 1024)} MB"

attachments_router = APIRouter(tags=["Files"])


class AccountRetirementRequest(BaseModel):
    """Deliberate confirmation for the destructive self-service event."""

    confirmation: Literal["retire-my-account"]


def _get_orchestrator(request: Request):
    """Resolve the orchestrator instance from app state (or its root app)."""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    return orch


def _get_repository(request: Request) -> AttachmentRepository:
    """Resolve the AttachmentRepository from the orchestrator on app state."""
    orch = _get_orchestrator(request)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialised")
    injected = getattr(orch, "attachment_repository", None)
    if injected is not None:
        return injected
    try:
        return AttachmentRepository.from_plane_source(plane_source_from_orchestrator(orch))
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Attachment persistence is not initialised",
        ) from exc


def _get_materialization_service(request: Request):
    orch = _get_orchestrator(request)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialised")
    try:
        return materialization_service_from_orchestrator(orch)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Attachment materialization is not initialised",
        ) from exc


def _get_purge_coordinator(request: Request):
    orch = _get_orchestrator(request)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialised")
    try:
        from orchestrator.attachments.purge import (
            purge_coordinator_from_orchestrator,
        )

        return purge_coordinator_from_orchestrator(orch)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Attachment deletion is not initialised",
        ) from exc


def _attachment_to_response(att) -> dict:
    created_at = att.created_at
    if isinstance(created_at, (int, float)):
        created_at = datetime.fromtimestamp(created_at / 1000.0, tz=UTC)
    return {
        "attachment_id": att.attachment_id,
        "filename": att.filename,
        "category": att.category,
        "extension": att.extension,
        "content_type": att.content_type,
        "size_bytes": att.size_bytes,
        "sha256": att.sha256,
        "created_at": created_at.isoformat() if created_at else None,
    }


# ---------------------------------------------------------------------------
# POST /api/upload
# ---------------------------------------------------------------------------


@attachments_router.post(
    "/api/upload",
    summary="Upload a file",
    description=(
        "Upload a single file. Returns the new attachment's metadata plus a "
        "`parser_status` (covered | preparing | pending_admin_approval | "
        "unavailable) describing whether a reader exists for this type. "
        "Size caps are per-category: 30 MB for documents / spreadsheets / "
        "presentations / text / images; 100 MB for data / archive; 2 GB for "
        "medical imaging formats (DICOM, NIfTI, CZI, NRRD/MHA/MHD, OME-TIFF, "
        "SVS, NDPI). The accepted-type list is broad (feature 031); an accepted "
        "type with no reader eagerly triggers safe, admin-approved auto-creation "
        "of one. Files are user-scoped and visible across the user's chats."
    ),
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    user_id: str = Depends(require_user_id),
):
    raw_filename = file.filename or ""
    safe_filename = os.path.basename(raw_filename)
    if not safe_filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    extension = ct.normalise_extension(safe_filename)
    category = ct.category_for_extension(extension)
    if category is None or extension in ct.LEGACY_BINARY_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file extension '.{extension or '?'}'. "
                "Supported: documents (pdf, docx, rtf, odt), spreadsheets "
                "(xlsx, xls, ods, tsv, csv), presentations (pptx, odp), "
                "text/code, images, and medical imaging formats (dcm, nii, "
                "nii.gz, czi, nrrd, mha, mhd, ome.tif, tif, tiff, svs, ndpi)."
            ),
        )

    attachment_id = str(uuid.uuid4())
    max_bytes = ct.max_bytes_for_category(category)
    materializations = _get_materialization_service(request)

    async def _stream_chunks():
        while True:
            chunk = await file.read(_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

    try:
        def _resolve_content_type(prefix: bytes) -> str:
            sniffed = ct.sniff_content_type(prefix[:_SNIFF_BYTES])
            if not ct.is_consistent(extension, sniffed):
                raise AttachmentContentTypeMismatchError(sniffed)
            return sniffed

        attachment = await materializations.materialize_stream(
            owner_id=user_id,
            attachment_id=attachment_id,
            filename=safe_filename,
            category=category,
            extension=extension,
            chunks=_stream_chunks(),
            max_bytes=max_bytes,
            resolve_content_type=_resolve_content_type,
        )
    except BlobSizeLimitError:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"{safe_filename} exceeds the {_format_cap_mb(max_bytes)} "
                f"upload limit for {category} files."
            ),
        )

    except AttachmentContentTypeMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"{safe_filename} has extension '.{extension}' but its content "
                f"appears to be '{exc.detected_content_type}'. Please upload a file whose contents "
                "match its extension."
            ),
        ) from exc

    except PlaneError as exc:
        logger.warning(
            "Attachment materialization failed for user=%s code=%s",
            user_id,
            getattr(exc, "code", "plane_error"),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Attachment materialization could not be verified. "
                "Durable recovery remains pending; retry later."
            ),
        ) from exc

    logger.info(
        f"Uploaded attachment {attachment_id} ({attachment.size_bytes} bytes, {category}) "
        f"for user={user_id}"
    )

    # Feature 031: eager parser-coverage check. If no built-in or globally
    # promoted parser can read this type, kick off the safe auto-creation flow
    # in the background (off the request path) and report the status.
    response_body = _attachment_to_response(attachment)
    response_body["parser_status"] = "covered"
    try:
        from orchestrator import attachment_autoparse
        orch = _get_orchestrator(request)
        if orch is not None:
            cov = await asyncio.to_thread(
                attachment_autoparse.coverage_status, orch,
                extension=extension, category=category,
            )
            response_body["parser_status"] = cov["status"]
            if cov["status"] == "preparing":
                asyncio.create_task(
                    attachment_autoparse.start(orch, attachment, user_id=user_id))
    except Exception:
        logger.debug("parser coverage check failed (non-fatal)", exc_info=True)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=response_body,
    )


# ---------------------------------------------------------------------------
# GET /api/attachments
# ---------------------------------------------------------------------------


@attachments_router.get(
    "/api/attachments",
    summary="List the calling user's attachments",
)
async def list_attachments(
    request: Request,
    category: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(default=None),
    user_id: str = Depends(require_user_id),
):
    repo = _get_repository(request)
    items, next_cursor = await repo.alist_for_user(
        user_id, category=category, limit=limit, cursor=cursor,
    )
    return {
        "attachments": [_attachment_to_response(a) for a in items],
        "next_cursor": next_cursor,
    }


@attachments_router.get(
    "/api/attachments/{attachment_id}",
    summary="Get one attachment's metadata",
)
async def get_attachment(
    request: Request,
    attachment_id: str,
    user_id: str = Depends(require_user_id),
):
    repo = _get_repository(request)
    att = await repo.aget_by_id(attachment_id, user_id)
    if att is None:
        # Deliberately 404, not 403, so we don't confirm existence to non-owners.
        raise HTTPException(status_code=404, detail="Attachment not found")
    return _attachment_to_response(att)


@attachments_router.delete(
    "/api/attachments/{attachment_id}",
    summary="Durably schedule attachment deletion",
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_attachment(
    request: Request,
    attachment_id: str,
    user_id: str = Depends(require_user_id),
):
    coordinator = _get_purge_coordinator(request)
    try:
        acceptance = await coordinator.aschedule_attachment(
            owner_id=user_id,
            attachment_id=attachment_id,
        )
    except PlaneError as exc:
        if exc.code == "purge_object_not_found":
            raise HTTPException(status_code=404, detail="Attachment not found") from exc
        logger.warning(
            "Attachment durable purge failed for owner=%s attachment=%s code=%s",
            user_id,
            attachment_id,
            exc.code,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Attachment deletion could not be completed; please retry.",
        ) from exc
    except Exception as exc:
        logger.warning(
            "Attachment durable purge failed for owner=%s attachment=%s",
            user_id,
            attachment_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Attachment deletion could not be completed; please retry.",
        ) from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "cleanup_pending",
            "cleanup_id": acceptance.cleanup_id,
        },
    )


@attachments_router.post(
    "/api/account/retirement",
    tags=["Account"],
    summary="Begin authenticated account retirement",
    status_code=status.HTTP_202_ACCEPTED,
)
async def begin_account_retirement(
    request: Request,
    retirement: AccountRetirementRequest,
    user_id: str = Depends(require_user_id),
):
    """Fence new owner blobs and begin durable namespace cleanup.

    This event is intentionally distinct from logout.  It accepts only the
    immutable subject of the verified access token and never an owner supplied
    in the request body.
    """

    del retirement
    coordinator = _get_purge_coordinator(request)
    try:
        from orchestrator.attachments.account_lifecycle import (
            initiate_account_retirement,
        )

        acceptance = await initiate_account_retirement(coordinator, user_id)
    except AccountRetirementNeedsReconciliation as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            headers={"Cache-Control": "no-store"},
            content={
                "status": "reconciliation_required",
                "code": "account_retirement_reconciliation_required",
                "unresolved_action_count": exc.unresolved_action_count,
                "detail": "Ongoing agents were stopped. Review their unresolved actions before completing account retirement.",
            },
        )
    except Exception as exc:
        logger.warning(
            "Account retirement cleanup could not be accepted for owner=%s",
            user_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account retirement could not be accepted; please retry.",
        ) from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        headers={"Cache-Control": "no-store"},
        content={
            "status": "cleanup_pending",
            "cleanup_id": acceptance.cleanup_id,
        },
    )


@attachments_router.get(
    "/api/account/retirement/{cleanup_id}",
    tags=["Account"],
    summary="Read authenticated account-retirement cleanup status",
)
async def read_account_retirement(
    request: Request,
    cleanup_id: str,
    user_id: str = Depends(require_user_id),
):
    coordinator = _get_purge_coordinator(request)
    try:
        from orchestrator.attachments.account_lifecycle import (
            account_retirement_status,
        )

        cleanup = await account_retirement_status(coordinator, user_id, cleanup_id)
    except Exception as exc:
        logger.warning(
            "Account retirement status failed for owner=%s cleanup=%s",
            user_id,
            cleanup_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account retirement status is temporarily unavailable.",
        ) from exc
    if cleanup is None:
        raise HTTPException(status_code=404, detail="Retirement cleanup not found")
    content = {
        "status": cleanup.status,
        "cleanup_id": cleanup.cleanup_id,
        "attempt_count": cleanup.attempt_count,
        "requested_at": cleanup.requested_at.isoformat(),
        "verified_absent_at": (
            cleanup.verified_absent_at.isoformat()
            if cleanup.verified_absent_at is not None
            else None
        ),
        "operator_action_required": cleanup.status == "manual_review",
    }
    return JSONResponse(headers={"Cache-Control": "no-store"}, content=content)


__all__ = ["AccountRetirementRequest", "attachments_router", "MAX_UPLOAD_BYTES"]
