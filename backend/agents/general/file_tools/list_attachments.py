"""``list_attachments`` tool: enumerate the calling user's attachments."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agents.general.file_tools import _get_repository

logger = logging.getLogger("FileTools.list_attachments")


def list_attachments(
    category: Optional[str] = None,
    limit: int = 50,
    user_id: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Return the calling user's live attachments, newest first."""
    if not user_id:
        return {"error": {"code": "not_found", "message": "user context required"}}
    try:
        repo = _get_repository()
    except RuntimeError as exc:
        return {"error": {"code": "not_found", "message": str(exc)}}
    items, next_cursor = repo.list_for_user(user_id, category=category, limit=limit)
    return {
        "attachments": [
            {
                "attachment_id": a.attachment_id,
                "filename": a.filename,
                "category": a.category,
                "extension": a.extension,
                "size_bytes": a.size_bytes,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in items
        ],
        "next_cursor": next_cursor,
    }


__all__ = ["list_attachments"]
