"""Product adapter for Plane-owned per-turn attachment links.

Feature 031-attachment-upload-parsing. Records which attachments a user
included on a sent chat turn so the orchestrator can (a) deliver structured
references to the handling agent and (b) re-hydrate them on ``load_chat``.

All reads are user-scoped; a caller only ever sees its own links.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import List, Optional

from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)


def _to_dict(record) -> dict:
    return {
        "id": record.link_id,
        "chat_id": record.conversation_id,
        "message_id": record.message_id,
        "attachment_id": record.attachment_id,
        "user_id": record.owner_id,
        "created_at": record.created_at,
    }


class MessageAttachmentRepository:
    """Product-shape adapter over Plane's message-attachment repository."""

    def __init__(
        self,
        db=None,
        *,
        plane_runtime=None,
        plane_repositories=None,
        plane_repository=None,
    ) -> None:
        self.db = db
        repository, runtime = repository_from(
            "artifacts",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._artifacts = PlaneRepositoryContext(
            repository=plane_repository or repository,
            plane_runtime=runtime,
            legacy_database=db,
        )
        self._last_created_at = 0

    @classmethod
    def from_plane_source(cls, source) -> "MessageAttachmentRepository":
        """Bind to an application-scoped Plane runtime/catalog source."""

        runtime = getattr(source, "plane_runtime", None) or getattr(source, "runtime", None)
        repositories = getattr(source, "plane_repositories", None) or getattr(
            source, "repositories", None
        )
        if runtime is None or repositories is None:
            raise RuntimeError("attachment-link persistence requires the application Plane runtime")
        return cls(plane_runtime=runtime, plane_repositories=repositories)

    def insert(
        self,
        *,
        chat_id: str,
        attachment_id: str,
        user_id: str,
        message_id: Optional[str] = None,
    ) -> str:
        """Insert one turn→attachment link and return its row id."""
        row_id = str(uuid.uuid4())
        now_ms = max(int(time.time() * 1000), self._last_created_at + 1)
        self._last_created_at = now_ms
        self._artifacts.call(
            self._artifacts.repository.message_attachments.link,
            link_id=row_id,
            owner_id=user_id,
            conversation_id=chat_id,
            attachment_id=attachment_id,
            created_at=now_ms,
            message_id=message_id,
        )
        return row_id

    def list_for_chat(self, chat_id: str, user_id: str) -> List[dict]:
        """All attachment links for *chat_id* owned by *user_id*, oldest first."""
        records = self._artifacts.call(
            self._artifacts.repository.message_attachments.list_for_conversation,
            owner_id=user_id,
            conversation_id=chat_id,
        )
        return [_to_dict(record) for record in records]

    def list_for_message(self, message_id: str, user_id: str) -> List[dict]:
        """All attachment links for a specific persisted user message."""
        records = self._artifacts.call(
            self._artifacts.repository.message_attachments.list_for_message,
            owner_id=user_id,
            message_id=message_id,
        )
        return [_to_dict(record) for record in records]

    # ── async facade (event-loop-safe twins of the sync methods above) ────
    async def ainsert(
        self,
        *,
        chat_id: str,
        attachment_id: str,
        user_id: str,
        message_id: Optional[str] = None,
    ) -> str:
        """Async twin of :meth:`insert`, run off the event loop."""
        return await asyncio.to_thread(
            self.insert, chat_id=chat_id, attachment_id=attachment_id,
            user_id=user_id, message_id=message_id,
        )

    async def alist_for_chat(self, chat_id: str, user_id: str) -> List[dict]:
        """Async twin of :meth:`list_for_chat`, run off the event loop."""
        return await asyncio.to_thread(self.list_for_chat, chat_id, user_id)

    async def alist_for_message(self, message_id: str, user_id: str) -> List[dict]:
        """Async twin of :meth:`list_for_message`, run off the event loop."""
        return await asyncio.to_thread(self.list_for_message, message_id, user_id)


__all__ = ["MessageAttachmentRepository"]
