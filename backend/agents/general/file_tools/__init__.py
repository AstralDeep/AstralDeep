"""File-handling tools for the AstralDeep general agent.

Each public reader function (``read_document``, ``read_spreadsheet``,
``read_presentation``, ``read_text``, ``read_image``, ``list_attachments``)
is registered in :data:`backend.agents.general.mcp_tools.TOOL_REGISTRY`.

All readers route through :func:`resolve_attachment` which:

  * Verifies the calling user owns the attachment (FR-009).
  * Re-sniffs content type via libmagic (FR-008).
  * Uses a bounded Plane stream when the parser accepts bytes.
  * Returns a scoped path capability only for path-only parser libraries.

The orchestrator injects ``user_id`` into tool-call ``arguments`` before
dispatch (see ``orchestrator.py:2325``), so each reader receives it as a
kwarg. Calls without a ``user_id`` are refused.
"""

from __future__ import annotations

import logging
import os
from contextlib import ExitStack
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from astralplane.errors import PlaneError

from orchestrator.attachments import content_type as ct
from orchestrator.attachments.blob_access import (
    AttachmentBlobReferenceError,
    open_attachment_parser_lease,
    open_attachment_reader,
)
from orchestrator.attachments.repository import AttachmentRepository
from orchestrator.attachments.models import Attachment

logger = logging.getLogger("FileTools")


def _error(code: str, message: str) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message}}


_ReaderResult = TypeVar("_ReaderResult")
_ACTIVE_LEASE_STACK: ContextVar[ExitStack | None] = ContextVar(
    "general_file_tool_attachment_leases",
    default=None,
)
_PRODUCTION_DEPENDENCIES: tuple[object, object, object] | None = None
_TEST_DEPENDENCIES: tuple[object, object, object] | None = None


def _get_plane_dependencies() -> tuple[object, object, object]:
    dependencies = _TEST_DEPENDENCIES or _PRODUCTION_DEPENDENCIES
    if dependencies is not None:
        return dependencies
    raise RuntimeError(
        "file_tools: the application Plane runtime, catalog, and blob store are not wired"
    )


def _get_repository() -> AttachmentRepository:
    runtime, repositories, _blobs = _get_plane_dependencies()
    return AttachmentRepository(
        plane_runtime=runtime,
        plane_repositories=repositories,
    )


def set_plane_dependencies_for_testing(
    plane_runtime=None,
    plane_repositories=None,
    blob_store=None,
) -> None:
    """Inject or clear typed Plane dependencies for focused unit tests."""

    global _TEST_DEPENDENCIES
    if plane_runtime is plane_repositories is blob_store is None:
        _TEST_DEPENDENCIES = None
        return
    if plane_runtime is None or plane_repositories is None or blob_store is None:
        raise ValueError("all Plane test dependencies are required")
    _TEST_DEPENDENCIES = (plane_runtime, plane_repositories, blob_store)


def register_plane_dependencies(plane_runtime, plane_repositories, blob_store) -> bool:
    """Bind the process's single application Plane composition."""

    if plane_runtime is None or plane_repositories is None or blob_store is None:
        raise ValueError("all Plane file-tool dependencies are required")
    global _PRODUCTION_DEPENDENCIES
    incoming = (plane_runtime, plane_repositories, blob_store)
    if _PRODUCTION_DEPENDENCIES is not None:
        if any(
            existing is not observed
            for existing, observed in zip(_PRODUCTION_DEPENDENCIES, incoming, strict=True)
        ):
            raise RuntimeError("file_tools Plane dependencies are already bound")
        return False
    _PRODUCTION_DEPENDENCIES = incoming
    return True


def unregister_plane_dependencies(plane_runtime, plane_repositories, blob_store) -> None:
    """Release the exact application binding after all file-tool work is joined."""

    global _PRODUCTION_DEPENDENCIES
    if _PRODUCTION_DEPENDENCIES is None:
        return
    expected = (plane_runtime, plane_repositories, blob_store)
    if any(
        existing is not observed
        for existing, observed in zip(_PRODUCTION_DEPENDENCIES, expected, strict=True)
    ):
        raise RuntimeError("file_tools Plane dependency unbind does not own the binding")
    _PRODUCTION_DEPENDENCIES = None


def attachment_parser_scope(
    function: Callable[..., _ReaderResult],
) -> Callable[..., _ReaderResult]:
    """Keep every path capability inside one complete trusted parser call."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with ExitStack() as leases:
            token = _ACTIVE_LEASE_STACK.set(leases)
            try:
                return function(*args, **kwargs)
            finally:
                _ACTIVE_LEASE_STACK.reset(token)

    return wrapped


def resolve_attachment(
    attachment_id: str,
    user_id: Optional[str],
) -> Tuple[Optional[Attachment], Optional[os.PathLike[str]], Optional[Dict[str, Any]]]:
    """Resolve an attachment_id to ``(Attachment, blob_path, error)``.

    Returns ``(attachment, path, None)`` on success, or
    ``(None, None, error_dict)`` on any failure (foreign owner, deleted,
    missing on disk, content-type mismatch).
    """
    if not user_id:
        return None, None, _error(
            "not_found",
            "Tool was called without a user context; refusing to read.",
        )
    if not attachment_id:
        return None, None, _error("not_found", "attachment_id is required.")

    try:
        repo = _get_repository()
    except RuntimeError as exc:
        return None, None, _error("not_found", str(exc))

    att = repo.get_by_id(attachment_id, user_id)
    if att is None:
        return None, None, _error("not_found", f"Attachment {attachment_id} not found.")

    _runtime, _repositories, blobs = _get_plane_dependencies()
    leases = _ACTIVE_LEASE_STACK.get()
    if leases is None:
        return None, None, _error(
            "unreadable_file",
            "Attachment parser was called outside its scoped Plane lease.",
        )
    try:
        capability = leases.enter_context(open_attachment_parser_lease(blobs, att, user_id))
        with open(capability, "rb") as source:
            sniffed = ct.sniff_content_type(source.read(8192))
    except (AttachmentBlobReferenceError, PlaneError, OSError):
        return None, None, _error(
            "not_found",
            f"Attachment {attachment_id} has no on-disk blob.",
        )
    if sniffed and not ct.is_consistent(att.extension, sniffed):
        return None, None, _error(
            "unreadable_file",
            f"File contents (sniffed: {sniffed}) do not match extension '.{att.extension}'.",
        )

    return att, capability, None


def read_attachment_bytes(
    attachment_id: str,
    user_id: Optional[str],
) -> Tuple[Optional[Attachment], Optional[bytes], Optional[Dict[str, Any]]]:
    """Read an owned attachment through Plane without exposing a local path.

    This is the default seam for byte-oriented parsers.  Iterating to EOF is
    deliberate: Plane verifies the expected digest at the complete read
    boundary, while the metadata size and reader limit bound memory usage.
    """

    if not user_id:
        return None, None, _error(
            "not_found",
            "Tool was called without a user context; refusing to read.",
        )
    if not attachment_id:
        return None, None, _error("not_found", "attachment_id is required.")

    try:
        repo = _get_repository()
    except RuntimeError as exc:
        return None, None, _error("not_found", str(exc))
    att = repo.get_by_id(attachment_id, user_id)
    if att is None:
        return None, None, _error("not_found", f"Attachment {attachment_id} not found.")

    _runtime, _repositories, blobs = _get_plane_dependencies()
    try:
        with open_attachment_reader(blobs, att, user_id) as reader:
            payload = b"".join(reader.iter_chunks())
    except (AttachmentBlobReferenceError, PlaneError):
        return None, None, _error(
            "not_found",
            f"Attachment {attachment_id} has no on-disk blob.",
        )

    sniffed = ct.sniff_content_type(payload[:8192])
    if sniffed and not ct.is_consistent(att.extension, sniffed):
        return None, None, _error(
            "unreadable_file",
            f"File contents (sniffed: {sniffed}) do not match extension '.{att.extension}'.",
        )
    return att, payload, None


__all__ = [
    "resolve_attachment",
    "attachment_parser_scope",
    "read_attachment_bytes",
    "register_plane_dependencies",
    "unregister_plane_dependencies",
    "set_plane_dependencies_for_testing",
]
