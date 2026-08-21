"""AstralDeep attachment identities mapped onto Plane-owned blob streams.

Plane owns the root, filesystem validation, streaming, atomic publication, and
deletion mechanics.  Deep owns only the attachment metadata convention: legacy
``storage_path`` values remain root-relative (``owner/attachment/file``), while
Plane receives the owner and the owner-relative key separately.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Any

from astralplane import BlobReadStream, StreamingBlobStore

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class AttachmentBlobReferenceError(ValueError):
    """Attachment metadata cannot be mapped to an owner-scoped Plane key."""


def attachment_storage_key(attachment_id: str, filename: str) -> str:
    """Return the owner-relative key used for one newly uploaded attachment."""

    if not isinstance(attachment_id, str) or not attachment_id:
        raise AttachmentBlobReferenceError("attachment_id is required")
    if not isinstance(filename, str) or not filename:
        raise AttachmentBlobReferenceError("attachment filename is required")
    return f"{attachment_id}/{filename}"


def metadata_storage_path(owner_id: str, storage_key: str) -> str:
    """Preserve the existing root-relative metadata representation."""

    if not isinstance(owner_id, str) or not owner_id:
        raise AttachmentBlobReferenceError("attachment owner_id is required")
    normalized = _safe_relative_parts(storage_key, field="storage_key")
    return "/".join((owner_id, *normalized))


def blob_key_from_storage_path(owner_id: str, storage_path: str) -> str:
    """Validate legacy metadata and detach its already-authorized owner prefix."""

    if not isinstance(owner_id, str) or not owner_id:
        raise AttachmentBlobReferenceError("attachment owner_id is required")
    parts = _safe_relative_parts(storage_path, field="storage_path")
    if len(parts) < 3 or parts[0] != owner_id:
        raise AttachmentBlobReferenceError(
            "attachment storage_path is outside the authorized owner namespace"
        )
    return "/".join(parts[1:])


def blob_key_for_attachment(attachment: Any, owner_id: str) -> str:
    """Derive the Plane key from typed identity and fence legacy locator drift."""

    attachment_id = getattr(attachment, "attachment_id", None)
    filename = getattr(attachment, "filename", None)
    key = attachment_storage_key(attachment_id, filename)
    storage_path = getattr(attachment, "storage_path", None)
    if storage_path is not None:
        observed = blob_key_from_storage_path(owner_id, storage_path)
        if observed != key:
            raise AttachmentBlobReferenceError(
                "attachment storage_path disagrees with its typed identity"
            )
    return key


def open_attachment_reader(
    blobs: StreamingBlobStore,
    attachment: Any,
    owner_id: str,
) -> BlobReadStream:
    """Open a bounded, integrity-fenced reader for one authorized attachment."""

    size = _attachment_size(attachment)
    digest = getattr(attachment, "sha256", None)
    if not isinstance(digest, str) or not digest:
        raise AttachmentBlobReferenceError("attachment metadata has no SHA-256 digest")
    return blobs.open_reader(
        owner_id=owner_id,
        key=blob_key_for_attachment(attachment, owner_id),
        max_bytes=max(size, 1),
        expected_size_bytes=size,
        expected_sha256=digest,
    )


@contextmanager
def open_attachment_parser_lease(
    blobs: StreamingBlobStore,
    attachment: Any,
    owner_id: str,
) -> Iterator[os.PathLike[str]]:
    """Yield a digest-verified scoped path to a trusted path-only parser.

    Plane validates the exact descriptor's identity, size, and SHA-256 before
    exposing the capability, then keeps the owner exclusion and descriptor
    alive for the complete parser call.
    """

    size = _attachment_size(attachment)
    digest = getattr(attachment, "sha256", None)
    if not isinstance(digest, str) or not digest:
        raise AttachmentBlobReferenceError("attachment metadata has no SHA-256 digest")
    with blobs.open_parser_lease(
        owner_id=owner_id,
        key=blob_key_for_attachment(attachment, owner_id),
        max_bytes=max(size, 1),
        expected_size_bytes=size,
        expected_sha256=digest,
    ) as capability:
        yield capability


def blob_store_from_orchestrator(orchestrator: Any) -> StreamingBlobStore:
    """Resolve the one application-scoped store, with explicit test injection."""

    injected = getattr(orchestrator, "attachment_blob_store", None)
    if injected is not None:
        return injected
    composition = getattr(orchestrator, "runtime_composition", None)
    plane = getattr(composition, "plane", None)
    blobs = getattr(plane, "blobs", None)
    if blobs is None:
        raise RuntimeError("the application Plane blob store is not initialized")
    return blobs


def _attachment_size(attachment: Any) -> int:
    value = getattr(attachment, "size_bytes", None)
    if isinstance(value, bool):
        raise AttachmentBlobReferenceError("attachment size_bytes is invalid")
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise AttachmentBlobReferenceError("attachment size_bytes is invalid") from None
    if size < 0:
        raise AttachmentBlobReferenceError("attachment size_bytes is invalid")
    return size


def _safe_relative_parts(value: str, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AttachmentBlobReferenceError(f"attachment {field} is invalid")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or _WINDOWS_DRIVE.match(normalized):
        raise AttachmentBlobReferenceError(f"attachment {field} must be relative")
    path = PurePosixPath(normalized)
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise AttachmentBlobReferenceError(f"attachment {field} is unsafe")
    if "/".join(parts) != normalized:
        raise AttachmentBlobReferenceError(f"attachment {field} is not canonical")
    return parts


__all__ = (
    "AttachmentBlobReferenceError",
    "attachment_storage_key",
    "blob_key_for_attachment",
    "blob_key_from_storage_path",
    "blob_store_from_orchestrator",
    "metadata_storage_path",
    "open_attachment_parser_lease",
    "open_attachment_reader",
)
