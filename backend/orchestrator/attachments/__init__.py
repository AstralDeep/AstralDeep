"""Attachment package: storage, persistence, and content-type sniffing for chat-message
uploads; re-exports attachments/models.py.
"""

from orchestrator.attachments.models import (
    Attachment,
    AttachmentRef,
    AttachmentCategory,
)

__all__ = ["Attachment", "AttachmentRef", "AttachmentCategory"]
