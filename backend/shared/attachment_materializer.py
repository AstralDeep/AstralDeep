"""Materialize inline/pasted text into a real, user-owned attachment.

Closes the gap where data pasted directly into chat (e.g. 16 weekly
enrollment counts) had no path into the file_handle-only pipelines
(``forecaster_submit_dataset`` / ``classify_submit_dataset``) — the chat
LLM would fabricate a handle to satisfy the schema and the tool would
fail. Tools that accept an ``inline_data`` string call
:func:`materialize_text_attachment` to turn the pasted text into a
first-class attachment row + blob, then proceed exactly as if the user
had uploaded the file.

Trust boundary: ``user_id`` must come from the tool kwargs the
orchestrator injects from the authenticated session
(``execute_single_tool`` overwrites ``args["user_id"]`` server-side) —
the same channel ``shared.attachment_resolver`` already trusts. It is
never model-suppliable; the resulting attachment is owned by, and only
resolvable by, that user (``AttachmentRepository`` enforces per-user
ownership at the database layer).

Each consuming application injects its one initialized attachment
materialization service; this module never receives a raw Plane runtime, blob
store, or repository mutation surface. Stdlib only: ``csv``, ``io``, ``uuid``.
"""
from __future__ import annotations

import csv
import io
import logging
import uuid

logger = logging.getLogger("AttachmentMaterializer")

#: Hard cap for inline/pasted payloads (bytes, UTF-8 encoded).
MAX_INLINE_BYTES = 1024 * 1024  # 1 MB

_MATERIALIZATION_SERVICE = None


def register_materialization_service(service) -> bool:
    """Bind the host's application-scoped durable publisher exactly once.

    In-process consumers reuse the orchestrator runtime; a networked agent
    process explicitly composes one application runtime before registering it.
    Missing or conflicting bindings fail closed.
    """
    if service is None or not callable(getattr(service, "materialize_bytes", None)):
        raise ValueError("the durable attachment materialization service is required")

    global _MATERIALIZATION_SERVICE
    if _MATERIALIZATION_SERVICE is not None:
        if _MATERIALIZATION_SERVICE is not service:
            raise RuntimeError("attachment materialization service is already bound")
        return False
    _MATERIALIZATION_SERVICE = service
    return True


def unregister_materialization_service(service) -> None:
    """Release the exact application publisher after every tool call is joined."""

    global _MATERIALIZATION_SERVICE
    if _MATERIALIZATION_SERVICE is None:
        return
    if _MATERIALIZATION_SERVICE is not service:
        raise RuntimeError("attachment materializer unbind does not own the service")
    _MATERIALIZATION_SERVICE = None


def _materialization_service():
    if _MATERIALIZATION_SERVICE is None:
        raise RuntimeError(
            "attachment materializer has no durable publisher; "
            "the application must call register_materialization_service() during startup"
        )
    return _MATERIALIZATION_SERVICE


def strip_code_fences(text: str) -> str:
    """Strip a wrapping markdown code fence from pasted text.

    The chat LLM frequently forwards pasted data wrapped in a fenced
    block (\\`\\`\\`csv … \\`\\`\\`). Mirrors the fence-stripping idiom in
    ``agents/medical/mcp_tools.analyze_generic_data`` but tolerates any
    info string (\\`\\`\\`csv, \\`\\`\\`text, bare \\`\\`\\`) by dropping the entire
    opening fence line.

    Args:
        text: Raw pasted text, possibly fence-wrapped.

    Returns:
        The inner text with surrounding fences and whitespace removed.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1:] if first_newline != -1 else ""
        cleaned = cleaned.strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()
    return cleaned


def _validate_csv(text: str) -> None:
    """Validate that *text* parses as CSV with a header and ≥1 data row.

    Mirrors the stdlib-csv validation idiom in
    ``agents/medical/mcp_tools.analyze_generic_data`` (DictReader →
    require non-empty fieldnames and rows).

    Args:
        text: Fence-stripped candidate CSV text.

    Raises:
        ValueError: When the text cannot be parsed as CSV, has no header
            row, or contains a header but no data rows.
    """
    try:
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    except csv.Error as e:
        raise ValueError(f"inline_data is not valid CSV: {e}") from e
    if not fieldnames:
        raise ValueError("inline_data is not valid CSV: no header row detected.")
    if not rows:
        raise ValueError(
            "inline_data contains a header but no data rows. "
            "Include the data rows below the header line."
        )


def materialize_text_attachment(text: str, user_id: str, *, extension: str = "csv") -> str:
    """Persist pasted text as a real attachment owned by *user_id*.

    Writes the blob through the application Plane store (same
    on-disk layout as ``POST /api/upload``: ``{root}/{user}/{id}/{name}``)
    and inserts the metadata row via ``AttachmentRepository``, so the
    returned id resolves through ``shared.attachment_resolver`` and shows
    up in the user's attachment list like any uploaded file.

    Args:
        text: The pasted text. Markdown code fences are stripped; CSV
            payloads (``extension="csv"``) are validated via stdlib csv.
        user_id: Authenticated owner. MUST come from the
            orchestrator-injected ``user_id`` tool kwarg — never from
            model-supplied tool arguments.
        extension: Target file extension (default ``"csv"``). Must map to
            a supported attachment category.

    Returns:
        The new ``attachment_id`` (UUIDv4), usable anywhere a
        ``file_handle`` is accepted.

    Raises:
        ValueError: On missing ``user_id``, empty text, invalid CSV,
            payloads over 1 MB, unsupported extensions, or persistence
            failures (blob is rolled back if the DB insert fails).
    """
    if not user_id:
        raise ValueError("user_id is required to materialize inline data.")

    cleaned = strip_code_fences(text)
    if not cleaned:
        raise ValueError("inline_data is empty; paste the raw data rows.")

    data = cleaned.encode("utf-8")
    if len(data) > MAX_INLINE_BYTES:
        raise ValueError(
            f"inline_data is {len(data)} bytes, over the "
            f"{MAX_INLINE_BYTES // (1024 * 1024)} MB inline limit. "
            "Upload the data as a file instead."
        )

    ext = (extension or "csv").lstrip(".").lower()
    if ext == "csv":
        _validate_csv(cleaned)

    try:
        from orchestrator.attachments import content_type as ct
    except ImportError as e:
        raise ValueError(f"Attachments subsystem unavailable: {e}") from e

    category = ct.category_for_extension(ext)
    if category is None:
        raise ValueError(f"Unsupported inline_data extension '.{ext}'.")

    try:
        materializations = _materialization_service()
    except Exception as e:
        raise ValueError(f"Could not open attachment persistence: {e}") from e

    attachment_id = str(uuid.uuid4())
    filename = f"inline-data-{attachment_id[:8]}.{ext}"
    try:
        attachment = materializations.materialize_bytes(
            owner_id=user_id,
            attachment_id=attachment_id,
            filename=filename,
            category=category,
            extension=ext,
            chunks=(data,),
            max_bytes=MAX_INLINE_BYTES,
            resolve_content_type=(
                lambda _prefix: "text/csv" if ext == "csv" else "text/plain"
            ),
        )
    except Exception as e:
        raise ValueError(f"Could not record inline attachment: {e}") from e

    logger.info(
        "Materialized inline attachment %s (%d bytes, .%s, sha256=%s…) for user=%s",
        attachment_id,
        attachment.size_bytes,
        ext,
        attachment.sha256[:12],
        user_id,
    )
    return attachment_id


__all__ = [
    "MAX_INLINE_BYTES",
    "materialize_text_attachment",
    "register_materialization_service",
    "unregister_materialization_service",
    "strip_code_fences",
]
