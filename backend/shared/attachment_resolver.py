"""Resolve a user's ``file_handle`` (attachment_id) to a real on-disk path.

Used by external-service agents (CLASSify, Forecaster, LLM-Factory) and by
``modify_data`` in the general agent — anywhere a tool needs to read a CSV
the user uploaded via the chat composer. Each consuming agent runs in its
own process, so the resolver opens its own DB connection rather than
depending on the orchestrator's in-process file_tools wiring.

Trust boundary: the resolver requires a ``user_id`` argument and uses the
``AttachmentRepository.get_by_id(attachment_id, user_id)`` query, which
already enforces per-user ownership at the database layer (returns
``None`` if the attachment exists but belongs to a different user).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("AttachmentResolver")

_RESOLVED_DB = None


def _open_db():
    """Open (and cache) a Database connection for this process.

    Uses the same env-var configuration the orchestrator uses (DB_HOST,
    DB_PORT, DB_NAME, DB_USER, DB_PASSWORD) so a sidecar agent process
    can resolve attachments without any explicit wiring.
    """
    global _RESOLVED_DB
    if _RESOLVED_DB is not None:
        return _RESOLVED_DB
    from shared.database import Database
    _RESOLVED_DB = Database()
    return _RESOLVED_DB


def resolve_attachment_path(file_handle: str, user_id: str) -> str:
    """Return the on-disk path for ``file_handle`` owned by ``user_id``.

    Raises ``ValueError`` if the handle is unknown, not owned by the user,
    or refers to a file that no longer exists on disk.

    ``file_handle`` is always treated as an attachment id. An on-disk path
    is never honored: every caller takes this value straight from a tool
    argument, so accepting a path would let a model read any file the agent
    process can open, bypassing the ownership query below.
    """
    if not file_handle:
        raise ValueError("file_handle is required")

    try:
        from orchestrator.attachments.repository import AttachmentRepository
        from orchestrator.attachments import store
    except ImportError as e:
        raise ValueError(f"Attachments subsystem unavailable: {e}") from e

    try:
        repo = AttachmentRepository(_open_db())
    except Exception as e:
        raise ValueError(f"Could not open attachments database: {e}") from e

    attachment = repo.get_by_id(file_handle, user_id)
    if attachment is None:
        raise ValueError(
            f"file_handle {file_handle!r} is not a valid attachment for this user. "
            "Upload the file first and use the returned attachment_id."
        )

    storage_path: Optional[str] = getattr(attachment, "storage_path", None)
    if not storage_path:
        raise ValueError(
            f"Attachment {file_handle!r} has no storage_path; database row is corrupt."
        )

    # storage_path is stored relative to the upload root (e.g.
    # ``test_user/<uuid>/dataset.csv``); join with the configured root to
    # get the real on-disk location. Honors ATTACHMENT_UPLOAD_ROOT.
    abs_path = str(store.get_upload_root() / storage_path)
    if not os.path.exists(abs_path):
        raise ValueError(
            f"Attachment {file_handle!r} no longer exists on disk; please re-upload."
        )

    logger.debug("Resolved attachment %s for user %s -> %s", file_handle, user_id, abs_path)
    return abs_path
