"""Resolve an owned ``file_handle`` through Plane's scoped blob capabilities.

Used by external-service agents (CLASSify, Forecaster, LLM-Factory) and by
``modify_data`` in the general agent — anywhere a tool needs to read a CSV
the user uploaded via the chat composer. The host must inject its one
application-scoped AstralPlane runtime before a tool call reaches this module.

Trust boundary: the resolver requires a ``user_id`` argument and uses the
``AttachmentRepository.get_by_id(attachment_id, user_id)`` query, which
already enforces per-user ownership at the database layer (returns
``None`` if the attachment exists but belongs to a different user).
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from collections.abc import Iterator

from astralplane import BlobReadStream
from astralplane.errors import PlaneError

from orchestrator.attachments.blob_access import (
    AttachmentBlobReferenceError,
    open_attachment_parser_lease as _open_parser_lease,
    open_attachment_reader as _open_reader,
)

logger = logging.getLogger("AttachmentResolver")

_PLANE_RUNTIME = None
_PLANE_REPOSITORIES = None
_PLANE_BLOBS = None


def register_plane_runtime(
    plane_runtime,
    plane_repositories=None,
    blob_store=None,
) -> bool:
    """Bind the host's initialized AstralPlane runtime exactly once.

    A networked agent process owns one explicitly composed runtime; an
    in-process agent receives the orchestrator's already initialized runtime.
    This module never constructs a driver, pool, or repository catalog.
    """
    if plane_runtime is None:
        raise ValueError("an initialized AstralPlane runtime is required")
    repositories = plane_repositories or getattr(plane_runtime, "repositories", None)
    if repositories is None:
        raise ValueError("the AstralPlane repository catalog is required")
    if blob_store is None:
        raise ValueError("the AstralPlane streaming blob store is required")

    global _PLANE_RUNTIME, _PLANE_REPOSITORIES, _PLANE_BLOBS
    already_bound = (
        _PLANE_RUNTIME is not None
        or _PLANE_REPOSITORIES is not None
        or _PLANE_BLOBS is not None
    )
    if _PLANE_RUNTIME is not None and _PLANE_RUNTIME is not plane_runtime:
        raise RuntimeError("attachment resolver Plane runtime is already bound")
    if _PLANE_REPOSITORIES is not None and _PLANE_REPOSITORIES is not repositories:
        raise RuntimeError("attachment resolver Plane catalog is already bound")
    if _PLANE_BLOBS is not None and _PLANE_BLOBS is not blob_store:
        raise RuntimeError("attachment resolver Plane blob store is already bound")
    _PLANE_RUNTIME = plane_runtime
    _PLANE_REPOSITORIES = repositories
    _PLANE_BLOBS = blob_store
    return not already_bound


def unregister_plane_runtime(
    plane_runtime,
    plane_repositories,
    blob_store,
) -> None:
    """Release only the exact application binding after consumers are joined."""

    global _PLANE_RUNTIME, _PLANE_REPOSITORIES, _PLANE_BLOBS
    if _PLANE_RUNTIME is _PLANE_REPOSITORIES is _PLANE_BLOBS is None:
        return
    if (
        _PLANE_RUNTIME is not plane_runtime
        or _PLANE_REPOSITORIES is not plane_repositories
        or _PLANE_BLOBS is not blob_store
    ):
        raise RuntimeError("attachment resolver unbind does not own the Plane binding")
    _PLANE_RUNTIME = None
    _PLANE_REPOSITORIES = None
    _PLANE_BLOBS = None


def _plane_dependencies():
    if _PLANE_RUNTIME is None or _PLANE_REPOSITORIES is None or _PLANE_BLOBS is None:
        raise RuntimeError(
            "attachment resolver has no AstralPlane runtime; "
            "the application must call register_plane_runtime() during startup"
        )
    return _PLANE_RUNTIME, _PLANE_REPOSITORIES, _PLANE_BLOBS


def _resolve_attachment(file_handle: str, user_id: str):
    """Resolve typed metadata after the owner-scoped repository lookup."""

    if not file_handle:
        raise ValueError("file_handle is required")

    try:
        from orchestrator.attachments.repository import AttachmentRepository
    except ImportError as e:
        raise ValueError(f"Attachments subsystem unavailable: {e}") from e

    try:
        plane_runtime, plane_repositories, blobs = _plane_dependencies()
        repo = AttachmentRepository(
            plane_runtime=plane_runtime,
            plane_repositories=plane_repositories,
        )
    except Exception as e:
        raise ValueError(f"Could not open attachment persistence: {e}") from e

    attachment = repo.get_by_id(file_handle, user_id)
    if attachment is None:
        raise ValueError(
            f"file_handle {file_handle!r} is not a valid attachment for this user. "
            "Upload the file first and use the returned attachment_id."
        )

    return attachment, blobs


@contextmanager
def open_attachment_parser_lease(
    file_handle: str,
    user_id: str,
) -> Iterator[os.PathLike[str]]:
    """Yield a revocable local path only for a trusted path-only parser."""

    attachment, blobs = _resolve_attachment(file_handle, user_id)
    try:
        with _open_parser_lease(blobs, attachment, user_id) as capability:
            logger.debug("Opened scoped parser lease for attachment %s", file_handle)
            yield capability
    except (AttachmentBlobReferenceError, PlaneError) as exc:
        raise ValueError(
            f"Attachment {file_handle!r} is unavailable; please re-upload."
        ) from exc


@contextmanager
def open_attachment_blob_reader(
    file_handle: str,
    user_id: str,
) -> Iterator[tuple[object, BlobReadStream]]:
    """Yield typed metadata plus a bounded, digest-fenced Plane reader."""

    attachment, blobs = _resolve_attachment(file_handle, user_id)
    try:
        with _open_reader(blobs, attachment, user_id) as reader:
            yield attachment, reader
    except (AttachmentBlobReferenceError, PlaneError) as exc:
        raise ValueError(
            f"Attachment {file_handle!r} is unavailable; please re-upload."
        ) from exc


__all__ = (
    "open_attachment_blob_reader",
    "open_attachment_parser_lease",
    "register_plane_runtime",
    "unregister_plane_runtime",
)
