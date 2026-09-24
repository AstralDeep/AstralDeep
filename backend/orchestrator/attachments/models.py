"""Pydantic models for the attachment domain: Attachment, the lightweight AttachmentRef
embedded in chat messages, and the cursor-paginated AttachmentList response.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

AttachmentCategory = Literal[
    "document", "spreadsheet", "presentation", "text", "image", "medical",
    "data", "archive",
]


class Attachment(BaseModel):
    attachment_id: str = Field(..., description="UUIDv4")
    user_id: str = Field(..., description="Keycloak sub of the owning user")
    filename: str
    content_type: str
    category: AttachmentCategory
    extension: str
    size_bytes: int
    sha256: str
    storage_path: str = Field(..., description="Path relative to the upload root")
    created_at: datetime
    deleted_at: Optional[datetime] = None


class AttachmentRef(BaseModel):
    attachment_id: str
    filename: str
    category: AttachmentCategory


class AttachmentList(BaseModel):
    attachments: List[Attachment]
    next_cursor: Optional[str] = None


__all__ = ["Attachment", "AttachmentRef", "AttachmentList", "AttachmentCategory"]
