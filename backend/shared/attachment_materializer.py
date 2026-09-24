"""Turns inline/pasted text (e.g. chat-pasted CSV) into a real, user-owned attachment so
file_handle-only tool pipelines (classify/forecaster tools) can consume it as if
uploaded; user_id must come from the session, never the model.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid

logger = logging.getLogger("AttachmentMaterializer")

MAX_INLINE_BYTES = 1024 * 1024

_MATERIALIZATION_SERVICE = None


def register_materialization_service(service) -> bool:
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
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1:] if first_newline != -1 else ""
        cleaned = cleaned.strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()
    return cleaned


def _validate_csv(text: str) -> None:
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
