"""PostgreSQL persistence for workspace canvases, layouts, snapshots, and atomic
assistant-result publication rebases. Used by orchestrator/workspace.py,
orchestrator/work_publication.py, and orchestrator/history.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _canonical_json,
    _content_value,
    _non_negative_int,
    _positive_int,
    _required_id,
    _row_value,
    _single_returned,
    _structured_json,
)


@dataclass(frozen=True, slots=True)
class CanvasComponentRecord:
    row_id: str
    conversation_id: str
    owner_id: str
    component_id: str | None
    payload: Any
    component_type: str
    title: str | None
    position: int | None
    created_at: int
    updated_at: int | None
    publication_id: str | None = None
    committed_render_revision: int | None = None


@dataclass(frozen=True, slots=True)
class LayoutRecord:
    layout_id: int
    conversation_id: str
    owner_id: str
    layout_key: str
    position: int
    tree: Any
    created_at: int
    updated_at: int
    publication_id: str | None = None
    committed_render_revision: int | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotRecord:
    snapshot_id: int
    conversation_id: str
    owner_id: str
    turn_message_id: int | None
    cause: str
    components: tuple[Any, ...]
    layouts: tuple[Any, ...]
    created_at: int


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    publication_id: str
    conversation_id: str
    owner_id: str
    request_generation: str
    operation_id: str | None
    operation_execution_generation: int | None
    base_render_revision: int
    committed_render_revision: int | None
    state: str
    publication_role: str
    parent_publication_id: str | None
    execution_base_publication_id: str | None
    execution_base_render_revision: int | None
    execution_base_components_sha256: str | None
    execution_base_layouts_sha256: str | None
    publication_rebase_count: int
    started_at: datetime
    committed_at: datetime | None
    aborted_at: datetime | None


@dataclass(frozen=True, slots=True)
class PublicationStageMessageFence:
    position: int
    committed_render_revision: int


@dataclass(frozen=True, slots=True)
class PublicationStageEntityFence:
    identity: str
    position: int
    committed_render_revision: int


@dataclass(frozen=True, slots=True)
class PublicationStageSummary:
    publication: PublicationRecord
    messages: tuple[PublicationStageMessageFence, ...]
    components: tuple[PublicationStageEntityFence, ...]
    layouts: tuple[PublicationStageEntityFence, ...]


@dataclass(frozen=True, slots=True)
class PublicationRebaseComponent:
    row_id: str
    component_id: str
    payload: Any
    component_type: str
    title: str | None
    position: int


@dataclass(frozen=True, slots=True)
class PublicationRebaseLayout:
    layout_key: str
    position: int
    tree: Any


@dataclass(frozen=True, slots=True)
class PublicationAssistantContentRecord:
    message_id: int
    conversation_id: str
    owner_id: str
    publication_id: str
    position: int
    committed_render_revision: int
    content: Any


def _optional_returned(result: object, operation: str) -> Any:
    if not getattr(result, "returned_records", ()):
        return None
    return _single_returned(result, operation)


def _aware_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryValidationError(f"{field} must be a timezone-aware datetime")
    return value


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RepositoryValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _persisted_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryDataError(
            "persisted timestamp is not timezone-aware", metadata={"field": field}
        )
    return value


def _scope(
    publication_id: str | None,
    committed_render_revision: int | None,
) -> tuple[str | None, int | None]:
    if publication_id is None and committed_render_revision is None:
        return None, None
    if publication_id is None or committed_render_revision is None:
        raise RepositoryValidationError(
            "publication_id and committed_render_revision must be supplied together"
        )
    return (
        _required_id(publication_id, "publication_id"),
        _positive_int(committed_render_revision, "committed_render_revision"),
    )


def _optional_publication_state(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in {"staged", "committed", "aborted"}:
        raise RepositoryValidationError("require_state is invalid")
    return value


def _publication_conflict_notice() -> dict[str, str]:
    return {
        "type": "alert",
        "variant": "warning",
        "message": (
            "Some canvas changes from this result conflicted with newer work. "
            "The newer canvas version was preserved."
        ),
    }


def _contains_publication_conflict_notice(content: object) -> bool:
    if not isinstance(content, Sequence) or isinstance(
        content, (str, bytes, bytearray, memoryview)
    ):
        return False
    if not content:
        return False
    return _canonical_json(content[-1], "content") == _canonical_json(
        _publication_conflict_notice(), "content"
    )


def _with_publication_conflict_notice(content: object) -> list[object]:
    notice: object = _publication_conflict_notice()
    if isinstance(content, Sequence) and not isinstance(
        content, (str, bytes, bytearray, memoryview)
    ):
        return [*content, notice]
    if isinstance(content, Mapping):
        return [dict(content), notice]
    if isinstance(content, str) and content:
        return [{"type": "text", "content": content}, notice]
    return [notice]


def _component(row: Mapping[str, Any]) -> CanvasComponentRecord:
    return CanvasComponentRecord(
        row_id=str(_row_value(row, "id")),
        conversation_id=str(_row_value(row, "chat_id")),
        owner_id=str(_row_value(row, "user_id")),
        component_id=(
            None if row.get("component_id") is None else str(row["component_id"])
        ),
        payload=_structured_json(_row_value(row, "component_data"), "component_data"),
        component_type=str(_row_value(row, "component_type")),
        title=None if row.get("title") is None else str(row["title"]),
        position=None if row.get("position") is None else int(row["position"]),
        created_at=int(_row_value(row, "created_at")),
        updated_at=(
            None if row.get("updated_at") is None else int(row["updated_at"])
        ),
        publication_id=(
            None
            if row.get("conversation_commit_id") is None
            else str(row["conversation_commit_id"])
        ),
        committed_render_revision=(
            None
            if row.get("committed_render_revision") is None
            else int(row["committed_render_revision"])
        ),
    )


def _layout(row: Mapping[str, Any]) -> LayoutRecord:
    return LayoutRecord(
        layout_id=int(_row_value(row, "id")),
        conversation_id=str(_row_value(row, "chat_id")),
        owner_id=str(_row_value(row, "user_id")),
        layout_key=str(_row_value(row, "layout_key")),
        position=int(_row_value(row, "position")),
        tree=_structured_json(_row_value(row, "layout"), "layout"),
        created_at=int(_row_value(row, "created_at")),
        updated_at=int(_row_value(row, "updated_at")),
        publication_id=(
            None
            if row.get("conversation_commit_id") is None
            else str(row["conversation_commit_id"])
        ),
        committed_render_revision=(
            None
            if row.get("committed_render_revision") is None
            else int(row["committed_render_revision"])
        ),
    )


def _snapshot(row: Mapping[str, Any]) -> WorkspaceSnapshotRecord:
    components = _structured_json(_row_value(row, "components"), "components")
    layouts = _structured_json(row.get("layouts"), "layouts", nullable=True)
    if not isinstance(components, tuple) or (
        layouts is not None and not isinstance(layouts, tuple)
    ):
        raise RepositoryDataError("workspace snapshot payloads must be JSON arrays")
    return WorkspaceSnapshotRecord(
        snapshot_id=int(_row_value(row, "id")),
        conversation_id=str(_row_value(row, "chat_id")),
        owner_id=str(_row_value(row, "user_id")),
        turn_message_id=(
            None if row.get("turn_message_id") is None else int(row["turn_message_id"])
        ),
        cause=str(_row_value(row, "cause")),
        components=components,
        layouts=() if layouts is None else layouts,
        created_at=int(_row_value(row, "created_at")),
    )


def _publication(row: Mapping[str, Any]) -> PublicationRecord:
    return PublicationRecord(
        publication_id=str(_row_value(row, "commit_id")),
        conversation_id=str(_row_value(row, "chat_id")),
        owner_id=str(_row_value(row, "owner_user_id")),
        request_generation=str(_row_value(row, "request_generation")),
        operation_id=(None if row.get("operation_id") is None else str(row["operation_id"])),
        operation_execution_generation=(
            None
            if row.get("operation_execution_generation") is None
            else int(row["operation_execution_generation"])
        ),
        base_render_revision=int(_row_value(row, "base_render_revision")),
        committed_render_revision=(
            None
            if row.get("committed_render_revision") is None
            else int(row["committed_render_revision"])
        ),
        state=str(_row_value(row, "state")),
        publication_role=str(_row_value(row, "publication_role")),
        parent_publication_id=(
            None if row.get("parent_commit_id") is None else str(row["parent_commit_id"])
        ),
        execution_base_publication_id=(
            None
            if row.get("execution_base_commit_id") is None
            else str(row["execution_base_commit_id"])
        ),
        execution_base_render_revision=(
            None
            if row.get("execution_base_render_revision") is None
            else int(row["execution_base_render_revision"])
        ),
        execution_base_components_sha256=(
            None
            if row.get("execution_base_components_sha256") is None
            else str(row["execution_base_components_sha256"])
        ),
        execution_base_layouts_sha256=(
            None
            if row.get("execution_base_layouts_sha256") is None
            else str(row["execution_base_layouts_sha256"])
        ),
        publication_rebase_count=int(_row_value(row, "publication_rebase_count")),
        started_at=_persisted_time(_row_value(row, "started_at"), "started_at"),
        committed_at=(
            None
            if row.get("committed_at") is None
            else _persisted_time(row["committed_at"], "committed_at")
        ),
        aborted_at=(
            None
            if row.get("aborted_at") is None
            else _persisted_time(row["aborted_at"], "aborted_at")
        ),
    )


class CanvasRepository:
    _FIELDS = """
        id, chat_id, user_id, component_id, component_data, component_type,
        title, position, created_at, updated_at, conversation_commit_id,
        committed_render_revision
    """
    _COMPONENT_FIELDS = """
        component.id, component.chat_id, component.user_id,
        component.component_id, component.component_data,
        component.component_type, component.title, component.position,
        component.created_at, component.updated_at,
        component.conversation_commit_id,
        component.committed_render_revision
    """

    def create(
        self,
        transaction: Transaction,
        record: CanvasComponentRecord,
    ) -> CanvasComponentRecord:
        row_id = _required_id(record.row_id, "row_id")
        conversation_id = _required_id(record.conversation_id, "conversation_id")
        owner_id = _required_id(record.owner_id, "owner_id")
        component_id = _required_id(record.component_id, "component_id")
        component_type = _bounded_text(record.component_type, "component_type", maximum=128)
        if record.title is not None:
            _bounded_text(record.title, "title", maximum=512, allow_empty=True)
        position = _non_negative_int(record.position, "position")
        created_at = _non_negative_int(record.created_at, "created_at")
        updated_at = _non_negative_int(record.updated_at, "updated_at")
        if updated_at < created_at:
            raise RepositoryValidationError("updated_at cannot precede created_at")
        publication_id, revision = _scope(record.publication_id, record.committed_render_revision)
        payload = _canonical_json(record.payload, "payload")
        result = transaction.execute(
            f"""
            INSERT INTO saved_components (
                id, chat_id, user_id, component_id, component_data,
                component_type, title, position, created_at, updated_at,
                conversation_commit_id, committed_render_revision
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            FROM chats AS chat
            WHERE chat.id = %s AND chat.user_id = %s
              AND (
                (%s IS NULL AND COALESCE(chat.render_revision, 0) = 0)
                OR EXISTS (
                    SELECT 1 FROM conversation_commit AS publication
                    WHERE publication.commit_id = %s
                      AND publication.chat_id = chat.id
                      AND publication.owner_user_id = chat.user_id
                      AND publication.state = 'staged'
                      AND publication.base_render_revision + 1 = %s
                )
              )
            ON CONFLICT DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (
                row_id,
                conversation_id,
                owner_id,
                component_id,
                payload,
                component_type,
                record.title,
                position,
                created_at,
                updated_at,
                publication_id,
                revision,
                conversation_id,
                owner_id,
                publication_id,
                publication_id,
                revision,
            ),
        )
        returned = _optional_returned(result, "canvas.create")
        if returned is not None:
            return _component(returned)
        existing = self.get_scoped(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            component_id=component_id,
            publication_id=publication_id,
            committed_render_revision=revision,
        )
        if existing is None:
            chat = transaction.fetch_one(
                "SELECT id FROM chats WHERE id = %s AND user_id = %s",
                (conversation_id, owner_id),
            )
            if chat is None:
                raise RepositoryNotFoundError(
                    "workspace conversation was not found",
                    metadata={"operation": "canvas.create"},
                )
            raise RepositoryConflictError(
                "canvas identity or publication scope is unavailable",
                metadata={"operation": "canvas.create"},
            )
        expected = (
            row_id,
            payload,
            component_type,
            record.title,
            position,
            created_at,
            updated_at,
        )
        observed = (
            existing.row_id,
            _canonical_json(existing.payload, "payload"),
            existing.component_type,
            existing.title,
            existing.position,
            existing.created_at,
            existing.updated_at,
        )
        if observed != expected:
            raise RepositoryConflictError(
                "canvas idempotency identity was reused with different semantics",
                metadata={"operation": "canvas.create"},
            )
        return existing

    def get_scoped(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        component_id: str,
        publication_id: str | None = None,
        committed_render_revision: int | None = None,
    ) -> CanvasComponentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        component_id = _required_id(component_id, "component_id")
        publication_id, revision = _scope(publication_id, committed_render_revision)
        row = query.fetch_one(
            f"""
            SELECT {self._FIELDS}
            FROM saved_components
            WHERE chat_id = %s AND user_id = %s AND component_id = %s
              AND conversation_commit_id IS NOT DISTINCT FROM %s
              AND committed_render_revision IS NOT DISTINCT FROM %s
            """,
            (conversation_id, owner_id, component_id, publication_id, revision),
        )
        return None if row is None else _component(row)

    def get_current_by_row_id(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        row_id: str,
        for_update: bool = False,
    ) -> CanvasComponentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        row_id = _required_id(row_id, "row_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        lock = " FOR UPDATE OF component" if for_update else ""
        row = query.fetch_one(
            f"""
            SELECT {self._COMPONENT_FIELDS}
            FROM saved_components AS component
            JOIN chats AS chat
              ON chat.id = component.chat_id AND chat.user_id = component.user_id
            WHERE component.id = %s AND component.user_id = %s
              AND (
                (COALESCE(chat.render_revision, 0) = 0
                 AND component.conversation_commit_id IS NULL
                 AND component.committed_render_revision IS NULL)
                OR (
                    chat.render_revision > 0
                    AND component.conversation_commit_id = chat.conversation_commit_id
                    AND component.committed_render_revision = chat.render_revision
                )
              )
            """
            + lock,
            (row_id, owner_id),
        )
        return None if row is None else _component(row)

    def list_scoped(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        committed_render_revision: int,
        expected_base_render_revision: int,
    ) -> tuple[CanvasComponentRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id, revision = _scope(
            publication_id,
            committed_render_revision,
        )
        base_revision = _non_negative_int(
            expected_base_render_revision,
            "expected_base_render_revision",
        )
        if revision != base_revision + 1:
            raise RepositoryValidationError(
                "committed_render_revision must follow expected_base_render_revision"
            )
        rows = query.fetch_all(
            f"""
            SELECT {self._COMPONENT_FIELDS}
            FROM saved_components AS component
            JOIN conversation_commit AS publication
              ON publication.commit_id = component.conversation_commit_id
             AND publication.chat_id = component.chat_id
             AND publication.owner_user_id = component.user_id
            WHERE component.chat_id = %s AND component.user_id = %s
              AND component.conversation_commit_id = %s
              AND component.committed_render_revision = %s
              AND publication.state = 'staged'
              AND publication.base_render_revision = %s
            ORDER BY COALESCE(component.position, 2147483647),
                     component.created_at, component.id
            """,
            (
                conversation_id,
                owner_id,
                publication_id,
                revision,
                base_revision,
            ),
        )
        return tuple(_component(row) for row in rows)

    def list_for_publication(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        committed_render_revision: int,
        require_state: str | None = None,
    ) -> tuple[CanvasComponentRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id, revision = _scope(
            publication_id,
            committed_render_revision,
        )
        required_state = _optional_publication_state(require_state)
        rows = query.fetch_all(
            f"""
            SELECT {self._COMPONENT_FIELDS}
            FROM saved_components AS component
            JOIN conversation_commit AS publication
              ON publication.commit_id = component.conversation_commit_id
             AND publication.chat_id = component.chat_id
             AND publication.owner_user_id = component.user_id
            WHERE component.chat_id = %s AND component.user_id = %s
              AND component.conversation_commit_id = %s
              AND component.committed_render_revision = %s
              AND (%s IS NULL OR publication.state = %s)
            ORDER BY COALESCE(component.position, 2147483647),
                     component.created_at, component.id
            """,
            (
                conversation_id,
                owner_id,
                publication_id,
                revision,
                required_state,
                required_state,
            ),
        )
        return tuple(_component(row) for row in rows)

    def list_current(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> tuple[CanvasComponentRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        rows = query.fetch_all(
            """
            SELECT component.id, component.chat_id, component.user_id,
                   component.component_id, component.component_data,
                   component.component_type, component.title, component.position,
                   component.created_at, component.updated_at,
                   component.conversation_commit_id,
                   component.committed_render_revision
            FROM saved_components AS component
            JOIN chats AS chat
              ON chat.id = component.chat_id AND chat.user_id = component.user_id
            WHERE component.chat_id = %s AND component.user_id = %s
              AND (
                (COALESCE(chat.render_revision, 0) = 0
                 AND component.conversation_commit_id IS NULL
                 AND component.committed_render_revision IS NULL)
                OR (
                    chat.render_revision > 0
                    AND component.conversation_commit_id = chat.conversation_commit_id
                    AND component.committed_render_revision = chat.render_revision
                )
              )
            ORDER BY COALESCE(component.position, 2147483647), component.created_at,
                     component.id
            """,
            (conversation_id, owner_id),
        )
        return tuple(_component(row) for row in rows)

    def list_current_for_owner(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str | None = None,
        limit: int = 1000,
    ) -> tuple[CanvasComponentRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        if conversation_id is not None:
            conversation_id = _required_id(conversation_id, "conversation_id")
        limit = _bounded_limit(limit, maximum=1000)
        rows = query.fetch_all(
            f"""
            SELECT {self._COMPONENT_FIELDS}
            FROM saved_components AS component
            JOIN chats AS chat
              ON chat.id = component.chat_id AND chat.user_id = component.user_id
            WHERE component.user_id = %s
              AND (%s IS NULL OR component.chat_id = %s)
              AND (
                (COALESCE(chat.render_revision, 0) = 0
                 AND component.conversation_commit_id IS NULL
                 AND component.committed_render_revision IS NULL)
                OR (
                    chat.render_revision > 0
                    AND component.conversation_commit_id = chat.conversation_commit_id
                    AND component.committed_render_revision = chat.render_revision
                )
              )
            ORDER BY component.created_at DESC, component.id DESC
            LIMIT %s
            """,
            (owner_id, conversation_id, conversation_id, limit),
        )
        return tuple(_component(row) for row in rows)

    def remove_current_by_row_id(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        row_id: str,
    ) -> CanvasComponentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        row_id = _required_id(row_id, "row_id")
        result = transaction.execute(
            f"""
            DELETE FROM saved_components AS component
            USING chats AS chat
            WHERE component.id = %s AND component.user_id = %s
              AND chat.id = component.chat_id AND chat.user_id = component.user_id
              AND COALESCE(chat.render_revision, 0) = 0
              AND component.conversation_commit_id IS NULL
              AND component.committed_render_revision IS NULL
            RETURNING {self._COMPONENT_FIELDS}
            """,
            (row_id, owner_id),
        )
        row = _optional_returned(result, "canvas.remove_current_by_row_id")
        return None if row is None else _component(row)

    def replace(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        component_id: str,
        payload: object,
        component_type: str,
        title: str | None,
        expected_updated_at: int,
        updated_at: int,
        publication_id: str | None = None,
        committed_render_revision: int | None = None,
    ) -> CanvasComponentRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        component_id = _required_id(component_id, "component_id")
        component_type = _bounded_text(component_type, "component_type", maximum=128)
        if title is not None:
            _bounded_text(title, "title", maximum=512, allow_empty=True)
        expected_updated_at = _non_negative_int(expected_updated_at, "expected_updated_at")
        updated_at = _non_negative_int(updated_at, "updated_at")
        if updated_at <= expected_updated_at:
            raise RepositoryValidationError("updated_at must advance the compare-and-set fence")
        publication_id, revision = _scope(publication_id, committed_render_revision)
        result = transaction.execute(
            f"""
            UPDATE saved_components
            SET component_data = %s, component_type = %s, title = %s, updated_at = %s
            WHERE chat_id = %s AND user_id = %s AND component_id = %s
              AND conversation_commit_id IS NOT DISTINCT FROM %s
              AND committed_render_revision IS NOT DISTINCT FROM %s
              AND updated_at = %s
            RETURNING {self._FIELDS}
            """,
            (
                _canonical_json(payload, "payload"),
                component_type,
                title,
                updated_at,
                conversation_id,
                owner_id,
                component_id,
                publication_id,
                revision,
                expected_updated_at,
            ),
        )
        row = _optional_returned(result, "canvas.replace")
        if row is not None:
            return _component(row)
        existing = self.get_scoped(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            component_id=component_id,
            publication_id=publication_id,
            committed_render_revision=revision,
        )
        if existing is None:
            raise RepositoryNotFoundError(
                "canvas component was not found",
                metadata={"operation": "canvas.replace"},
            )
        raise RepositoryConflictError(
            "canvas component changed since it was read",
            metadata={"operation": "canvas.replace"},
        )

    def remove(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        component_id: str,
        publication_id: str | None = None,
        committed_render_revision: int | None = None,
    ) -> bool:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        component_id = _required_id(component_id, "component_id")
        publication_id, revision = _scope(publication_id, committed_render_revision)
        result = transaction.execute(
            """
            DELETE FROM saved_components
            WHERE chat_id = %s AND user_id = %s AND component_id = %s
              AND conversation_commit_id IS NOT DISTINCT FROM %s
              AND committed_render_revision IS NOT DISTINCT FROM %s
            """,
            (conversation_id, owner_id, component_id, publication_id, revision),
        )
        return result.rowcount == 1

    def sync_legacy_presence(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> bool:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        result = transaction.execute(
            """
            UPDATE chats AS chat
            SET has_saved_components = EXISTS (
                SELECT 1
                FROM saved_components AS component
                WHERE component.chat_id = chat.id
                  AND component.user_id = chat.user_id
                  AND component.conversation_commit_id IS NULL
                  AND component.committed_render_revision IS NULL
            )
            WHERE chat.id = %s AND chat.user_id = %s
              AND COALESCE(chat.render_revision, 0) = 0
            RETURNING COALESCE(has_saved_components, FALSE) AS has_saved_components
            """,
            (conversation_id, owner_id),
        )
        row = _optional_returned(result, "canvas.sync_legacy_presence")
        if row is not None:
            return bool(_row_value(row, "has_saved_components"))
        conversation = transaction.fetch_one(
            "SELECT render_revision FROM chats WHERE id = %s AND user_id = %s",
            (conversation_id, owner_id),
        )
        if conversation is None:
            raise RepositoryNotFoundError(
                "workspace conversation was not found",
                metadata={"operation": "canvas.sync_legacy_presence"},
            )
        raise RepositoryConflictError(
            "revisioned workspace presence requires atomic publication",
            metadata={"operation": "canvas.sync_legacy_presence"},
        )


class LayoutRepository:
    _FIELDS = """
        id, chat_id, user_id, layout_key, position, layout, created_at,
        updated_at, conversation_commit_id, committed_render_revision
    """
    _LAYOUT_FIELDS = """
        layout.id, layout.chat_id, layout.user_id, layout.layout_key,
        layout.position, layout.layout, layout.created_at, layout.updated_at,
        layout.conversation_commit_id, layout.committed_render_revision
    """

    def create(self, transaction: Transaction, record: LayoutRecord) -> LayoutRecord:
        conversation_id = _required_id(record.conversation_id, "conversation_id")
        owner_id = _required_id(record.owner_id, "owner_id")
        layout_key = _required_id(record.layout_key, "layout_key")
        position = _non_negative_int(record.position, "position")
        created_at = _non_negative_int(record.created_at, "created_at")
        updated_at = _non_negative_int(record.updated_at, "updated_at")
        if updated_at < created_at:
            raise RepositoryValidationError("updated_at cannot precede created_at")
        publication_id, revision = _scope(record.publication_id, record.committed_render_revision)
        tree_payload = _canonical_json(record.tree, "tree")
        result = transaction.execute(
            f"""
            INSERT INTO workspace_layout (
                chat_id, user_id, layout_key, position, layout, created_at,
                updated_at, conversation_commit_id, committed_render_revision
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s
            FROM chats AS chat
            WHERE chat.id = %s AND chat.user_id = %s
              AND (
                (%s IS NULL AND COALESCE(chat.render_revision, 0) = 0)
                OR EXISTS (
                    SELECT 1 FROM conversation_commit AS publication
                    WHERE publication.commit_id = %s
                      AND publication.chat_id = chat.id
                      AND publication.owner_user_id = chat.user_id
                      AND publication.state = 'staged'
                      AND publication.base_render_revision + 1 = %s
                )
              )
            ON CONFLICT DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (
                conversation_id,
                owner_id,
                layout_key,
                position,
                tree_payload,
                created_at,
                updated_at,
                publication_id,
                revision,
                conversation_id,
                owner_id,
                publication_id,
                publication_id,
                revision,
            ),
        )
        row = _optional_returned(result, "layout.create")
        if row is not None:
            return _layout(row)
        existing = self.get_scoped(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            layout_key=layout_key,
            publication_id=publication_id,
            committed_render_revision=revision,
        )
        if existing is None:
            raise RepositoryConflictError(
                "layout identity or publication scope is unavailable",
                metadata={"operation": "layout.create"},
            )
        expected = (tree_payload, position, created_at, updated_at)
        observed = (
            _canonical_json(existing.tree, "tree"),
            existing.position,
            existing.created_at,
            existing.updated_at,
        )
        if observed != expected:
            raise RepositoryConflictError(
                "layout idempotency identity was reused with different semantics",
                metadata={"operation": "layout.create"},
            )
        return existing

    def get_scoped(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        layout_key: str,
        publication_id: str | None = None,
        committed_render_revision: int | None = None,
    ) -> LayoutRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        layout_key = _required_id(layout_key, "layout_key")
        publication_id, revision = _scope(publication_id, committed_render_revision)
        row = query.fetch_one(
            f"""
            SELECT {self._FIELDS}
            FROM workspace_layout
            WHERE chat_id = %s AND user_id = %s AND layout_key = %s
              AND conversation_commit_id IS NOT DISTINCT FROM %s
              AND committed_render_revision IS NOT DISTINCT FROM %s
            """,
            (conversation_id, owner_id, layout_key, publication_id, revision),
        )
        return None if row is None else _layout(row)

    def list_scoped(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        committed_render_revision: int,
        expected_base_render_revision: int,
    ) -> tuple[LayoutRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id, revision = _scope(
            publication_id,
            committed_render_revision,
        )
        base_revision = _non_negative_int(
            expected_base_render_revision,
            "expected_base_render_revision",
        )
        if revision != base_revision + 1:
            raise RepositoryValidationError(
                "committed_render_revision must follow expected_base_render_revision"
            )
        rows = query.fetch_all(
            f"""
            SELECT {self._LAYOUT_FIELDS}
            FROM workspace_layout AS layout
            JOIN conversation_commit AS publication
              ON publication.commit_id = layout.conversation_commit_id
             AND publication.chat_id = layout.chat_id
             AND publication.owner_user_id = layout.user_id
            WHERE layout.chat_id = %s AND layout.user_id = %s
              AND layout.conversation_commit_id = %s
              AND layout.committed_render_revision = %s
              AND publication.state = 'staged'
              AND publication.base_render_revision = %s
            ORDER BY layout.position, layout.id
            """,
            (
                conversation_id,
                owner_id,
                publication_id,
                revision,
                base_revision,
            ),
        )
        return tuple(_layout(row) for row in rows)

    def list_for_publication(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        committed_render_revision: int,
        require_state: str | None = None,
    ) -> tuple[LayoutRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id, revision = _scope(
            publication_id,
            committed_render_revision,
        )
        required_state = _optional_publication_state(require_state)
        rows = query.fetch_all(
            f"""
            SELECT {self._LAYOUT_FIELDS}
            FROM workspace_layout AS layout
            JOIN conversation_commit AS publication
              ON publication.commit_id = layout.conversation_commit_id
             AND publication.chat_id = layout.chat_id
             AND publication.owner_user_id = layout.user_id
            WHERE layout.chat_id = %s AND layout.user_id = %s
              AND layout.conversation_commit_id = %s
              AND layout.committed_render_revision = %s
              AND (%s IS NULL OR publication.state = %s)
            ORDER BY layout.position, layout.id
            """,
            (
                conversation_id,
                owner_id,
                publication_id,
                revision,
                required_state,
                required_state,
            ),
        )
        return tuple(_layout(row) for row in rows)

    def list_current(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> tuple[LayoutRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        rows = query.fetch_all(
            """
            SELECT layout.id, layout.chat_id, layout.user_id, layout.layout_key,
                   layout.position, layout.layout, layout.created_at,
                   layout.updated_at, layout.conversation_commit_id,
                   layout.committed_render_revision
            FROM workspace_layout AS layout
            JOIN chats AS chat
              ON chat.id = layout.chat_id AND chat.user_id = layout.user_id
            WHERE layout.chat_id = %s AND layout.user_id = %s
              AND (
                (COALESCE(chat.render_revision, 0) = 0
                 AND layout.conversation_commit_id IS NULL
                 AND layout.committed_render_revision IS NULL)
                OR (
                    chat.render_revision > 0
                    AND layout.conversation_commit_id = chat.conversation_commit_id
                    AND layout.committed_render_revision = chat.render_revision
                )
              )
            ORDER BY layout.position, layout.id
            """,
            (conversation_id, owner_id),
        )
        return tuple(_layout(row) for row in rows)

    def replace(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        layout_key: str,
        tree: object,
        expected_updated_at: int,
        updated_at: int,
        publication_id: str | None = None,
        committed_render_revision: int | None = None,
    ) -> LayoutRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        layout_key = _required_id(layout_key, "layout_key")
        expected_updated_at = _non_negative_int(expected_updated_at, "expected_updated_at")
        updated_at = _non_negative_int(updated_at, "updated_at")
        if updated_at <= expected_updated_at:
            raise RepositoryValidationError("updated_at must advance the compare-and-set fence")
        publication_id, revision = _scope(publication_id, committed_render_revision)
        result = transaction.execute(
            f"""
            UPDATE workspace_layout
            SET layout = %s, updated_at = %s
            WHERE chat_id = %s AND user_id = %s AND layout_key = %s
              AND conversation_commit_id IS NOT DISTINCT FROM %s
              AND committed_render_revision IS NOT DISTINCT FROM %s
              AND updated_at = %s
            RETURNING {self._FIELDS}
            """,
            (
                _canonical_json(tree, "tree"),
                updated_at,
                conversation_id,
                owner_id,
                layout_key,
                publication_id,
                revision,
                expected_updated_at,
            ),
        )
        row = _optional_returned(result, "layout.replace")
        if row is not None:
            return _layout(row)
        existing = self.get_scoped(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            layout_key=layout_key,
            publication_id=publication_id,
            committed_render_revision=revision,
        )
        if existing is None:
            raise RepositoryNotFoundError(
                "workspace layout was not found",
                metadata={"operation": "layout.replace"},
            )
        raise RepositoryConflictError(
            "workspace layout changed since it was read",
            metadata={"operation": "layout.replace"},
        )

    def remove(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        layout_key: str,
        publication_id: str | None = None,
        committed_render_revision: int | None = None,
    ) -> bool:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        layout_key = _required_id(layout_key, "layout_key")
        publication_id, revision = _scope(publication_id, committed_render_revision)
        result = transaction.execute(
            """
            DELETE FROM workspace_layout
            WHERE chat_id = %s AND user_id = %s AND layout_key = %s
              AND conversation_commit_id IS NOT DISTINCT FROM %s
              AND committed_render_revision IS NOT DISTINCT FROM %s
            """,
            (conversation_id, owner_id, layout_key, publication_id, revision),
        )
        return result.rowcount == 1


class WorkspaceSnapshotRepository:
    def capture(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        cause: str,
        components: Sequence[object],
        layouts: Sequence[object] = (),
        created_at: int,
        turn_message_id: int | None = None,
    ) -> WorkspaceSnapshotRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        cause = _bounded_text(cause, "cause", maximum=128)
        created_at = _non_negative_int(created_at, "created_at")
        if turn_message_id is not None:
            turn_message_id = _positive_int(turn_message_id, "turn_message_id")
        result = transaction.execute(
            """
            INSERT INTO workspace_snapshot (
                chat_id, user_id, turn_message_id, cause, components, layouts, created_at
            )
            SELECT %s, %s, %s, %s, %s, %s, %s
            FROM chats
            WHERE id = %s AND user_id = %s
            RETURNING id, chat_id, user_id, turn_message_id, cause,
                      components, layouts, created_at
            """,
            (
                conversation_id,
                owner_id,
                turn_message_id,
                cause,
                _canonical_json(tuple(components), "components"),
                None if not layouts else _canonical_json(tuple(layouts), "layouts"),
                created_at,
                conversation_id,
                owner_id,
            ),
        )
        row = _optional_returned(result, "workspace_snapshot.capture")
        if row is None:
            raise RepositoryNotFoundError(
                "workspace conversation was not found",
                metadata={"operation": "workspace_snapshot.capture"},
            )
        return _snapshot(row)

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        snapshot_id: int,
    ) -> WorkspaceSnapshotRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        snapshot_id = _positive_int(snapshot_id, "snapshot_id")
        row = query.fetch_one(
            """
            SELECT id, chat_id, user_id, turn_message_id, cause,
                   components, layouts, created_at
            FROM workspace_snapshot
            WHERE id = %s AND user_id = %s
            """,
            (snapshot_id, owner_id),
        )
        return None if row is None else _snapshot(row)

    def list_for_conversation(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[WorkspaceSnapshotRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        limit = _bounded_limit(limit)
        offset = _non_negative_int(offset, "offset")
        rows = query.fetch_all(
            """
            SELECT id, chat_id, user_id, turn_message_id, cause,
                   components, layouts, created_at
            FROM workspace_snapshot
            WHERE chat_id = %s AND user_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (conversation_id, owner_id, limit, offset),
        )
        return tuple(_snapshot(row) for row in rows)

    def count_for_conversation(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> int:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        row = query.fetch_one(
            """
            SELECT COUNT(*) AS snapshot_count
            FROM workspace_snapshot
            WHERE chat_id = %s AND user_id = %s
            """,
            (conversation_id, owner_id),
        )
        if row is None:  # pragma: no cover
            raise RepositoryDataError("workspace snapshot count returned no row")
        count = int(_row_value(row, "snapshot_count"))
        if count < 0:  # pragma: no cover
            raise RepositoryDataError("workspace snapshot count is negative")
        return count


class PublicationRepository:
    _FIELDS = """
        commit_id, chat_id, owner_user_id, request_generation, operation_id,
        operation_execution_generation, base_render_revision,
        committed_render_revision, state, publication_role, parent_commit_id,
        execution_base_commit_id, execution_base_render_revision,
        execution_base_components_sha256, execution_base_layouts_sha256,
        publication_rebase_count, started_at, committed_at, aborted_at
    """

    def stage(
        self,
        transaction: Transaction,
        *,
        publication_id: str,
        owner_id: str,
        conversation_id: str,
        request_generation: str,
        base_render_revision: int,
        started_at: datetime,
        operation_id: str | None = None,
        operation_execution_generation: int | None = None,
        publication_role: str = "atomic",
        parent_publication_id: str | None = None,
        execution_base_publication_id: str | None = None,
        execution_base_render_revision: int | None = None,
        execution_base_components_sha256: str | None = None,
        execution_base_layouts_sha256: str | None = None,
    ) -> PublicationRecord:
        publication_id = _required_id(publication_id, "publication_id")
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        request_generation = _required_id(request_generation, "request_generation")
        base_render_revision = _non_negative_int(base_render_revision, "base_render_revision")
        started_at = _aware_time(started_at, "started_at")
        if operation_id is not None:
            operation_id = _required_id(operation_id, "operation_id")
        if operation_execution_generation is not None:
            operation_execution_generation = _positive_int(
                operation_execution_generation, "operation_execution_generation"
            )
        if (operation_id is None) != (operation_execution_generation is None):
            raise RepositoryValidationError(
                "operation_id and operation_execution_generation must be supplied together"
            )
        if not isinstance(publication_role, str) or publication_role not in {
            "atomic",
            "user_acceptance",
            "assistant_result",
        }:
            raise RepositoryValidationError("publication_role is invalid")
        if publication_role == "atomic":
            if any(
                value is not None
                for value in (
                    parent_publication_id,
                    execution_base_publication_id,
                    execution_base_render_revision,
                    execution_base_components_sha256,
                    execution_base_layouts_sha256,
                )
            ):
                raise RepositoryValidationError(
                    "atomic publications cannot carry voice execution metadata"
                )
        else:
            if publication_role == "user_acceptance":
                if parent_publication_id is not None:
                    raise RepositoryValidationError(
                        "user acceptance publications cannot have a parent"
                    )
                execution_base_render_revision = _non_negative_int(
                    execution_base_render_revision,
                    "execution_base_render_revision",
                )
            else:
                parent_publication_id = _required_id(
                    parent_publication_id, "parent_publication_id"
                )
                execution_base_render_revision = _positive_int(
                    execution_base_render_revision,
                    "execution_base_render_revision",
                )
            if execution_base_render_revision == 0:
                if execution_base_publication_id is not None:
                    raise RepositoryValidationError(
                        "revision-zero execution bases cannot name a publication"
                    )
            else:
                execution_base_publication_id = _required_id(
                    execution_base_publication_id,
                    "execution_base_publication_id",
                )
            execution_base_components_sha256 = _sha256(
                execution_base_components_sha256,
                "execution_base_components_sha256",
            )
            execution_base_layouts_sha256 = _sha256(
                execution_base_layouts_sha256,
                "execution_base_layouts_sha256",
            )
        if parent_publication_id == publication_id:
            raise RepositoryValidationError("a publication cannot be its own parent")
        if execution_base_publication_id == publication_id:
            raise RepositoryValidationError(
                "a publication cannot be its own execution base"
            )
        result = transaction.execute(
            f"""
            INSERT INTO conversation_commit (
                commit_id, chat_id, owner_user_id, request_generation,
                operation_id, operation_execution_generation,
                base_render_revision, committed_render_revision, state,
                publication_role, parent_commit_id, execution_base_commit_id,
                execution_base_render_revision,
                execution_base_components_sha256,
                execution_base_layouts_sha256, publication_rebase_count,
                started_at, committed_at, aborted_at
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, NULL, 'staged',
                   %s, %s, %s, %s, %s, %s, 0, %s, NULL, NULL
            FROM chats
            WHERE id = %s AND user_id = %s
              AND COALESCE(render_revision, 0) = %s
            ON CONFLICT (chat_id, request_generation) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (
                publication_id,
                conversation_id,
                owner_id,
                request_generation,
                operation_id,
                operation_execution_generation,
                base_render_revision,
                publication_role,
                parent_publication_id,
                execution_base_publication_id,
                execution_base_render_revision,
                execution_base_components_sha256,
                execution_base_layouts_sha256,
                started_at,
                conversation_id,
                owner_id,
                base_render_revision,
            ),
        )
        row = _optional_returned(result, "publication.stage")
        if row is not None:
            return _publication(row)
        existing = self.get_by_request(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            request_generation=request_generation,
        )
        if existing is None:
            chat = transaction.fetch_one(
                "SELECT id FROM chats WHERE id = %s AND user_id = %s",
                (conversation_id, owner_id),
            )
            if chat is None:
                raise RepositoryNotFoundError(
                    "publication conversation was not found",
                    metadata={"operation": "publication.stage"},
                )
            raise RepositoryConflictError(
                "conversation render revision changed before publication staging",
                metadata={"operation": "publication.stage"},
            )
        expected = (
            publication_id,
            operation_id,
            operation_execution_generation,
            base_render_revision,
            publication_role,
            parent_publication_id,
            execution_base_publication_id,
            execution_base_render_revision,
            execution_base_components_sha256,
            execution_base_layouts_sha256,
        )
        observed = (
            existing.publication_id,
            existing.operation_id,
            existing.operation_execution_generation,
            existing.base_render_revision,
            existing.publication_role,
            existing.parent_publication_id,
            existing.execution_base_publication_id,
            existing.execution_base_render_revision,
            existing.execution_base_components_sha256,
            existing.execution_base_layouts_sha256,
        )
        if expected != observed:
            raise RepositoryConflictError(
                "publication request generation was reused with different semantics",
                metadata={"operation": "publication.stage"},
            )
        return existing

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        for_update: bool = False,
    ) -> PublicationRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id = _required_id(publication_id, "publication_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be a boolean")
        lock_clause = " FOR UPDATE" if for_update else ""
        row = query.fetch_one(
            f"""
            SELECT {self._FIELDS}
            FROM conversation_commit
            WHERE commit_id = %s AND chat_id = %s AND owner_user_id = %s
            {lock_clause}
            """,
            (publication_id, conversation_id, owner_id),
        )
        return None if row is None else _publication(row)

    def get_for_owner(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        publication_id: str,
        for_update: bool = False,
    ) -> PublicationRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        publication_id = _required_id(publication_id, "publication_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be a boolean")
        lock_clause = " FOR UPDATE" if for_update else ""
        row = query.fetch_one(
            f"""
            SELECT {self._FIELDS}
            FROM conversation_commit
            WHERE commit_id = %s AND owner_user_id = %s
            {lock_clause}
            """,
            (publication_id, owner_id),
        )
        return None if row is None else _publication(row)

    def get_latest_committed_assistant_content(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        publication_id: str,
    ) -> PublicationAssistantContentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        publication_id = _required_id(publication_id, "publication_id")
        row = query.fetch_one(
            """
            SELECT message.id AS message_id, message.chat_id,
                   message.user_id, message.conversation_commit_id,
                   message.commit_position, message.committed_render_revision,
                   message.content
            FROM conversation_commit AS publication
            JOIN messages AS message
              ON message.conversation_commit_id = publication.commit_id
             AND message.chat_id = publication.chat_id
             AND message.user_id = publication.owner_user_id
             AND message.committed_render_revision =
                 publication.committed_render_revision
            WHERE publication.commit_id = %s
              AND publication.owner_user_id = %s
              AND publication.state = 'committed'
              AND message.role = 'assistant'
            ORDER BY message.commit_position DESC, message.id DESC
            LIMIT 1
            """,
            (publication_id, owner_id),
        )
        if row is None:
            return None
        return PublicationAssistantContentRecord(
            message_id=_positive_int(_row_value(row, "message_id"), "message_id"),
            conversation_id=str(_row_value(row, "chat_id")),
            owner_id=str(_row_value(row, "user_id")),
            publication_id=str(_row_value(row, "conversation_commit_id")),
            position=_non_negative_int(
                _row_value(row, "commit_position"), "commit_position"
            ),
            committed_render_revision=_positive_int(
                _row_value(row, "committed_render_revision"),
                "committed_render_revision",
            ),
            content=_content_value(_row_value(row, "content")),
        )

    def get_by_request(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        request_generation: str,
        for_update: bool = False,
    ) -> PublicationRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        request_generation = _required_id(request_generation, "request_generation")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be a boolean")
        lock_clause = " FOR UPDATE" if for_update else ""
        row = query.fetch_one(
            f"""
            SELECT {self._FIELDS}
            FROM conversation_commit
            WHERE chat_id = %s AND owner_user_id = %s AND request_generation = %s
            {lock_clause}
            """,
            (conversation_id, owner_id, request_generation),
        )
        return None if row is None else _publication(row)

    def validate_stage(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
    ) -> PublicationStageSummary:
        publication = self.get(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=publication_id,
            for_update=True,
        )
        if publication is None:
            raise RepositoryNotFoundError(
                "publication was not found",
                metadata={"operation": "publication.validate_stage"},
            )
        if publication.state != "staged":
            raise RepositoryConflictError(
                "publication is not staged",
                metadata={"operation": "publication.validate_stage"},
            )
        maximum = 10_000
        message_rows = transaction.fetch_all(
            """
            SELECT commit_position, committed_render_revision
            FROM messages
            WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s
            ORDER BY commit_position, id
            LIMIT %s
            """,
            (publication_id, conversation_id, owner_id, maximum + 1),
        )
        component_rows = transaction.fetch_all(
            """
            SELECT component_id, position, committed_render_revision
            FROM saved_components
            WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s
            ORDER BY COALESCE(position, 2147483647), created_at, id
            LIMIT %s
            """,
            (publication_id, conversation_id, owner_id, maximum + 1),
        )
        layout_rows = transaction.fetch_all(
            """
            SELECT layout_key, position, committed_render_revision
            FROM workspace_layout
            WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s
            ORDER BY position, id
            LIMIT %s
            """,
            (publication_id, conversation_id, owner_id, maximum + 1),
        )
        if any(
            len(rows) > maximum
            for rows in (message_rows, component_rows, layout_rows)
        ):
            raise RepositoryDataError(
                "staged publication exceeds the integrity-summary bound",
                metadata={"operation": "publication.validate_stage"},
            )

        def persisted_integer(row: Mapping[str, Any], field: str) -> int:
            value = _row_value(row, field)
            if isinstance(value, bool):
                raise RepositoryDataError(
                    "staged publication contains an invalid integer",
                    metadata={"field": field},
                )
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise RepositoryDataError(
                    "staged publication contains an invalid integer",
                    metadata={"field": field},
                ) from exc
            if parsed < 0:
                raise RepositoryDataError(
                    "staged publication contains a negative integer",
                    metadata={"field": field},
                )
            return parsed

        messages = tuple(
            PublicationStageMessageFence(
                position=persisted_integer(row, "commit_position"),
                committed_render_revision=persisted_integer(
                    row, "committed_render_revision"
                ),
            )
            for row in message_rows
        )
        components = tuple(
            PublicationStageEntityFence(
                identity=str(_row_value(row, "component_id")),
                position=persisted_integer(row, "position"),
                committed_render_revision=persisted_integer(
                    row, "committed_render_revision"
                ),
            )
            for row in component_rows
        )
        layouts = tuple(
            PublicationStageEntityFence(
                identity=str(_row_value(row, "layout_key")),
                position=persisted_integer(row, "position"),
                committed_render_revision=persisted_integer(
                    row, "committed_render_revision"
                ),
            )
            for row in layout_rows
        )
        if any(not fence.identity for fence in (*components, *layouts)):
            raise RepositoryDataError(
                "staged publication contains an empty identity",
                metadata={"operation": "publication.validate_stage"},
            )
        return PublicationStageSummary(
            publication=publication,
            messages=messages,
            components=components,
            layouts=layouts,
        )

    def rebase_assistant_stage(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        expected_staged_base_render_revision: int,
        expected_head_render_revision: int,
        expected_head_publication_id: str | None,
        components: Sequence[PublicationRebaseComponent],
        layouts: Sequence[PublicationRebaseLayout],
        append_conflict_notice: bool,
    ) -> PublicationStageSummary:
        operation = "publication.rebase_assistant_stage"
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id = _required_id(publication_id, "publication_id")
        staged_base = _non_negative_int(
            expected_staged_base_render_revision,
            "expected_staged_base_render_revision",
        )
        head_revision = _non_negative_int(
            expected_head_render_revision,
            "expected_head_render_revision",
        )
        if head_revision < staged_base:
            raise RepositoryValidationError(
                "expected_head_render_revision cannot precede the staged base"
            )
        if head_revision == 0:
            if expected_head_publication_id is not None:
                raise RepositoryValidationError(
                    "revision-zero heads cannot name a publication"
                )
        else:
            expected_head_publication_id = _required_id(
                expected_head_publication_id,
                "expected_head_publication_id",
            )
        if not isinstance(append_conflict_notice, bool):
            raise RepositoryValidationError("append_conflict_notice must be boolean")
        if not isinstance(components, Sequence) or isinstance(
            components, (str, bytes, bytearray, memoryview)
        ):
            raise RepositoryValidationError("components must be a bounded sequence")
        if not isinstance(layouts, Sequence) or isinstance(
            layouts, (str, bytes, bytearray, memoryview)
        ):
            raise RepositoryValidationError("layouts must be a bounded sequence")
        maximum = 10_000
        if len(components) > maximum or len(layouts) > maximum:
            raise RepositoryValidationError(
                "publication rebase exceeds the supported row bound",
                metadata={"maximum": maximum},
            )

        normalized_components: list[
            tuple[str, str, str, str, str | None, int]
        ] = []
        for item in components:
            if not isinstance(item, PublicationRebaseComponent):
                raise RepositoryValidationError(
                    "components must contain PublicationRebaseComponent records"
                )
            title = item.title
            if title is not None:
                title = _bounded_text(
                    title,
                    "component.title",
                    maximum=512,
                    allow_empty=True,
                )
            normalized_components.append(
                (
                    _required_id(item.row_id, "component.row_id"),
                    _required_id(item.component_id, "component.component_id"),
                    _canonical_json(item.payload, "component.payload"),
                    _bounded_text(
                        item.component_type,
                        "component.component_type",
                        maximum=128,
                    ),
                    title,
                    _non_negative_int(item.position, "component.position"),
                )
            )
        if [item[5] for item in normalized_components] != list(
            range(len(normalized_components))
        ):
            raise RepositoryValidationError(
                "component positions must be contiguous and input ordered"
            )
        if len({item[0] for item in normalized_components}) != len(
            normalized_components
        ) or len({item[1] for item in normalized_components}) != len(
            normalized_components
        ):
            raise RepositoryValidationError(
                "component row and semantic identities must be unique"
            )

        normalized_layouts: list[tuple[str, int, str]] = []
        for item in layouts:
            if not isinstance(item, PublicationRebaseLayout):
                raise RepositoryValidationError(
                    "layouts must contain PublicationRebaseLayout records"
                )
            normalized_layouts.append(
                (
                    _required_id(item.layout_key, "layout.layout_key"),
                    _non_negative_int(item.position, "layout.position"),
                    _canonical_json(item.tree, "layout.tree"),
                )
            )
        if len({item[0] for item in normalized_layouts}) != len(normalized_layouts):
            raise RepositoryValidationError("layout identities must be unique")
        normalized_layouts.sort(key=lambda item: item[1])

        publication = self.get(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=publication_id,
            for_update=True,
        )
        if publication is None:
            raise RepositoryNotFoundError(
                "publication was not found", metadata={"operation": operation}
            )
        if (
            publication.state != "staged"
            or publication.publication_role != "assistant_result"
            or publication.base_render_revision != staged_base
        ):
            raise RepositoryConflictError(
                "publication is not the expected assistant-result stage",
                metadata={"operation": operation},
            )
        chat = transaction.fetch_one(
            """
            SELECT render_revision, conversation_commit_id
            FROM chats
            WHERE id = %s AND user_id = %s
            FOR UPDATE
            """,
            (conversation_id, owner_id),
        )
        if chat is None:
            raise RepositoryNotFoundError(
                "publication conversation was not found",
                metadata={"operation": operation},
            )
        observed_head = (
            int(_row_value(chat, "render_revision")),
            None
            if chat.get("conversation_commit_id") is None
            else str(chat["conversation_commit_id"]),
        )
        if observed_head != (head_revision, expected_head_publication_id):
            raise RepositoryConflictError(
                "conversation head changed before stage rebase",
                metadata={"operation": operation},
            )

        message_rows = transaction.fetch_all(
            """
            SELECT id, role, content, commit_position,
                   committed_render_revision
            FROM messages
            WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s
            ORDER BY commit_position, id
            LIMIT %s
            FOR UPDATE
            """,
            (publication_id, conversation_id, owner_id, maximum + 1),
        )
        component_rows = transaction.fetch_all(
            """
            SELECT id, component_id, component_data, component_type, title,
                   position, committed_render_revision
            FROM saved_components
            WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s
            ORDER BY position, id
            LIMIT %s
            FOR UPDATE
            """,
            (publication_id, conversation_id, owner_id, maximum + 1),
        )
        layout_rows = transaction.fetch_all(
            """
            SELECT id, layout_key, position, layout, committed_render_revision
            FROM workspace_layout
            WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s
            ORDER BY position, id
            LIMIT %s
            FOR UPDATE
            """,
            (publication_id, conversation_id, owner_id, maximum + 1),
        )
        if any(
            len(rows) > maximum
            for rows in (message_rows, component_rows, layout_rows)
        ):
            raise RepositoryDataError(
                "staged publication exceeds the rebase integrity bound",
                metadata={"operation": operation},
            )

        def persisted_non_negative(row: Mapping[str, Any], field: str) -> int:
            value = _row_value(row, field)
            if isinstance(value, bool):
                raise RepositoryDataError(
                    "staged publication contains an invalid integer",
                    metadata={"field": field},
                )
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise RepositoryDataError(
                    "staged publication contains an invalid integer",
                    metadata={"field": field},
                ) from exc
            if parsed < 0:
                raise RepositoryDataError(
                    "staged publication contains a negative integer",
                    metadata={"field": field},
                )
            return parsed

        message_positions = [
            persisted_non_negative(row, "commit_position") for row in message_rows
        ]
        if message_positions != list(range(len(message_positions))):
            raise RepositoryDataError(
                "assistant-result messages are incomplete",
                metadata={"operation": operation},
            )
        next_revision = head_revision + 1
        current_components = tuple(
            (
                str(_row_value(row, "id")),
                str(_row_value(row, "component_id")),
                _canonical_json(
                    _structured_json(
                        _row_value(row, "component_data"), "component_data"
                    ),
                    "component_data",
                ),
                str(_row_value(row, "component_type")),
                None if row.get("title") is None else str(row["title"]),
                persisted_non_negative(row, "position"),
            )
            for row in component_rows
        )
        current_layouts = tuple(
            (
                str(_row_value(row, "layout_key")),
                persisted_non_negative(row, "position"),
                _canonical_json(
                    _structured_json(_row_value(row, "layout"), "layout"),
                    "layout",
                ),
            )
            for row in layout_rows
        )
        message_revisions = [
            persisted_non_negative(row, "committed_render_revision")
            for row in message_rows
        ]
        component_revisions = [
            persisted_non_negative(row, "committed_render_revision")
            for row in component_rows
        ]
        layout_revisions = [
            persisted_non_negative(row, "committed_render_revision")
            for row in layout_rows
        ]
        assistant_row = next(
            (
                row
                for row in reversed(message_rows)
                if str(_row_value(row, "role")) == "assistant"
            ),
            None,
        )
        # UUIDs vary per retry; compare semantic fields only
        current_component_semantics = tuple(
            component[1:] for component in current_components
        )
        normalized_component_semantics = tuple(
            component[1:] for component in normalized_components
        )
        rows_match = (
            current_component_semantics == normalized_component_semantics
            and current_layouts == tuple(normalized_layouts)
            and all(revision == next_revision for revision in message_revisions)
            and all(revision == next_revision for revision in component_revisions)
            and all(revision == next_revision for revision in layout_revisions)
        )
        if rows_match:
            if append_conflict_notice:
                if assistant_row is None:
                    raise RepositoryDataError(
                        "conflicted assistant-result stage has no assistant message",
                        metadata={"operation": operation},
                    )
                content = _content_value(_row_value(assistant_row, "content"))
                if not _contains_publication_conflict_notice(content):
                    self._append_conflict_notice(
                        transaction,
                        owner_id=owner_id,
                        conversation_id=conversation_id,
                        publication_id=publication_id,
                        next_revision=next_revision,
                        assistant_row=assistant_row,
                    )
            return self._rebase_summary(
                publication,
                message_positions,
                normalized_components,
                normalized_layouts,
                next_revision,
            )
        # Revision alone can't tell first apply from changed replay
        if publication.publication_rebase_count > 0:
            raise RepositoryConflictError(
                "assistant-result rebase generation was reused with different semantics",
                metadata={"operation": operation},
            )

        time_row = transaction.fetch_one(
            "SELECT clock_timestamp() AS observed_at"
        )
        if time_row is None:
            raise RepositoryDataError(
                "database time was unavailable", metadata={"operation": operation}
            )
        observed_at = _persisted_time(_row_value(time_row, "observed_at"), "observed_at")
        observed_at_ms = int(observed_at.timestamp() * 1000)
        if observed_at_ms < 0:
            raise RepositoryDataError(
                "database time precedes the supported epoch",
                metadata={"operation": operation},
            )
        marked = transaction.execute(
            f"""
            UPDATE conversation_commit
            SET publication_rebase_count = publication_rebase_count + 1
            WHERE commit_id = %s AND chat_id = %s AND owner_user_id = %s
              AND state = 'staged' AND publication_role = 'assistant_result'
              AND base_render_revision = %s
              AND publication_rebase_count = %s
            RETURNING {self._FIELDS}
            """,
            (
                publication_id,
                conversation_id,
                owner_id,
                staged_base,
                publication.publication_rebase_count,
            ),
        )
        marked_row = _optional_returned(marked, operation)
        if marked_row is None:
            raise RepositoryConflictError(
                "assistant-result rebase generation marker changed",
                metadata={"operation": operation},
            )
        publication = _publication(marked_row)
        component_delete = transaction.execute(
            """
            DELETE FROM saved_components
            WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s
            """,
            (publication_id, conversation_id, owner_id),
        )
        layout_delete = transaction.execute(
            """
            DELETE FROM workspace_layout
            WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s
            """,
            (publication_id, conversation_id, owner_id),
        )
        if component_delete.rowcount != len(component_rows) or layout_delete.rowcount != len(
            layout_rows
        ):
            raise RepositoryConflictError(
                "assistant-result stage changed while it was being replaced",
                metadata={"operation": operation},
            )
        for row_id, component_id, payload, component_type, title, position in (
            normalized_components
        ):
            inserted = transaction.execute(
                """
                INSERT INTO saved_components (
                    id, chat_id, user_id, component_id, component_data,
                    component_type, title, position, created_at, updated_at,
                    conversation_commit_id, committed_render_revision
                )
                SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                FROM conversation_commit AS publication
                WHERE publication.commit_id = %s
                  AND publication.chat_id = %s
                  AND publication.owner_user_id = %s
                  AND publication.state = 'staged'
                  AND publication.publication_role = 'assistant_result'
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    row_id,
                    conversation_id,
                    owner_id,
                    component_id,
                    payload,
                    component_type,
                    title,
                    position,
                    observed_at_ms,
                    observed_at_ms,
                    publication_id,
                    next_revision,
                    publication_id,
                    conversation_id,
                    owner_id,
                ),
            )
            if len(inserted.returned_records) != 1:
                raise RepositoryConflictError(
                    "assistant-result component identity is unavailable",
                    metadata={"operation": operation},
                )
        for layout_key, position, tree in normalized_layouts:
            inserted = transaction.execute(
                """
                INSERT INTO workspace_layout (
                    chat_id, user_id, layout_key, position, layout, created_at,
                    updated_at, conversation_commit_id, committed_render_revision
                )
                SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s
                FROM conversation_commit AS publication
                WHERE publication.commit_id = %s
                  AND publication.chat_id = %s
                  AND publication.owner_user_id = %s
                  AND publication.state = 'staged'
                  AND publication.publication_role = 'assistant_result'
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    conversation_id,
                    owner_id,
                    layout_key,
                    position,
                    tree,
                    observed_at_ms,
                    observed_at_ms,
                    publication_id,
                    next_revision,
                    publication_id,
                    conversation_id,
                    owner_id,
                ),
            )
            if len(inserted.returned_records) != 1:
                raise RepositoryConflictError(
                    "assistant-result layout identity is unavailable",
                    metadata={"operation": operation},
                )
        message_update = transaction.execute(
            """
            UPDATE messages
            SET committed_render_revision = %s
            WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s
            """,
            (next_revision, publication_id, conversation_id, owner_id),
        )
        if message_update.rowcount != len(message_rows):
            raise RepositoryConflictError(
                "assistant-result messages changed during stage rebase",
                metadata={"operation": operation},
            )
        if append_conflict_notice:
            if assistant_row is None:
                raise RepositoryDataError(
                    "conflicted assistant-result stage has no assistant message",
                    metadata={"operation": operation},
                )
            self._append_conflict_notice(
                transaction,
                owner_id=owner_id,
                conversation_id=conversation_id,
                publication_id=publication_id,
                next_revision=next_revision,
                assistant_row=assistant_row,
            )
        return self._rebase_summary(
            publication,
            message_positions,
            normalized_components,
            normalized_layouts,
            next_revision,
        )

    @staticmethod
    def _append_conflict_notice(
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        next_revision: int,
        assistant_row: Mapping[str, Any],
    ) -> None:
        content = _content_value(_row_value(assistant_row, "content"))
        if _contains_publication_conflict_notice(content):
            return
        result = transaction.execute(
            """
            UPDATE messages
            SET content = %s, committed_render_revision = %s
            WHERE id = %s AND conversation_commit_id = %s
              AND chat_id = %s AND user_id = %s AND role = 'assistant'
              AND content IS NOT DISTINCT FROM %s
            """,
            (
                _canonical_json(
                    _with_publication_conflict_notice(content),
                    "assistant_content",
                ),
                next_revision,
                _row_value(assistant_row, "id"),
                publication_id,
                conversation_id,
                owner_id,
                _row_value(assistant_row, "content"),
            ),
        )
        if result.rowcount != 1:
            raise RepositoryConflictError(
                "assistant-result conflict notice update was lost",
                metadata={"operation": "publication.rebase_assistant_stage"},
            )

    @staticmethod
    def _rebase_summary(
        publication: PublicationRecord,
        message_positions: Sequence[int],
        components: Sequence[tuple[str, str, str, str, str | None, int]],
        layouts: Sequence[tuple[str, int, str]],
        next_revision: int,
    ) -> PublicationStageSummary:
        return PublicationStageSummary(
            publication=publication,
            messages=tuple(
                PublicationStageMessageFence(
                    position=position,
                    committed_render_revision=next_revision,
                )
                for position in message_positions
            ),
            components=tuple(
                PublicationStageEntityFence(
                    identity=component_id,
                    position=position,
                    committed_render_revision=next_revision,
                )
                for _, component_id, _, _, _, position in components
            ),
            layouts=tuple(
                PublicationStageEntityFence(
                    identity=layout_key,
                    position=position,
                    committed_render_revision=next_revision,
                )
                for layout_key, position, _ in layouts
            ),
        )

    def commit_at_head(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        expected_staged_base_render_revision: int,
        expected_head_render_revision: int,
        expected_head_publication_id: str | None,
        committed_at: datetime,
        updated_at: int,
    ) -> PublicationRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id = _required_id(publication_id, "publication_id")
        staged_base = _non_negative_int(
            expected_staged_base_render_revision,
            "expected_staged_base_render_revision",
        )
        head_revision = _non_negative_int(
            expected_head_render_revision, "expected_head_render_revision"
        )
        if head_revision == 0:
            if expected_head_publication_id is not None:
                raise RepositoryValidationError(
                    "revision-zero heads cannot name a publication"
                )
        else:
            expected_head_publication_id = _required_id(
                expected_head_publication_id, "expected_head_publication_id"
            )
        committed_at = _aware_time(committed_at, "committed_at")
        updated_at = _non_negative_int(updated_at, "updated_at")
        publication = self.get(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=publication_id,
            for_update=True,
        )
        if publication is None:
            raise RepositoryNotFoundError(
                "publication was not found",
                metadata={"operation": "publication.commit_at_head"},
            )
        next_revision = head_revision + 1
        if publication.state == "committed":
            original_base = (
                publication.base_render_revision
                if publication.publication_role == "atomic"
                else publication.execution_base_render_revision
            )
            if (
                original_base == staged_base
                and publication.base_render_revision == head_revision
                and publication.committed_render_revision == next_revision
                and self._chat_authority_matches(transaction, publication)
            ):
                return publication
            raise RepositoryConflictError(
                "publication replay does not match the committed authority",
                metadata={"operation": "publication.commit_at_head"},
            )
        if publication.state != "staged" or publication.base_render_revision != staged_base:
            raise RepositoryConflictError(
                "publication is not in the expected staged generation",
                metadata={"operation": "publication.commit_at_head"},
            )
        if publication.publication_role != "assistant_result" and staged_base != head_revision:
            raise RepositoryConflictError(
                "only assistant-result publications may rebase at commit",
                metadata={"operation": "publication.commit_at_head"},
            )
        chat = transaction.fetch_one(
            """
            SELECT render_revision, conversation_commit_id
            FROM chats
            WHERE id = %s AND user_id = %s
            FOR UPDATE
            """,
            (conversation_id, owner_id),
        )
        if chat is None:
            raise RepositoryNotFoundError(
                "publication conversation was not found",
                metadata={"operation": "publication.commit_at_head"},
            )
        observed_head = (
            int(_row_value(chat, "render_revision")),
            None
            if chat.get("conversation_commit_id") is None
            else str(chat["conversation_commit_id"]),
        )
        if observed_head != (head_revision, expected_head_publication_id):
            raise RepositoryConflictError(
                "conversation head changed before publication",
                metadata={"operation": "publication.commit_at_head"},
            )
        result = transaction.execute(
            f"""
            UPDATE conversation_commit
            SET state = 'committed', base_render_revision = %s,
                committed_render_revision = %s, committed_at = %s,
                execution_base_commit_id = NULL,
                publication_rebase_count = publication_rebase_count + CASE
                    WHEN execution_base_render_revision IS NOT NULL
                     AND publication_rebase_count = 0
                     AND execution_base_render_revision <> %s THEN 1
                    ELSE 0
                END
            WHERE commit_id = %s AND chat_id = %s AND owner_user_id = %s
              AND state = 'staged' AND base_render_revision = %s
            RETURNING {self._FIELDS}
            """,
            (
                head_revision,
                next_revision,
                committed_at,
                head_revision,
                publication_id,
                conversation_id,
                owner_id,
                staged_base,
            ),
        )
        row = _optional_returned(result, "publication.commit_at_head")
        if row is None:
            raise RepositoryConflictError(
                "publication lost its staged compare-and-set fence",
                metadata={"operation": "publication.commit_at_head"},
            )
        committed = _publication(row)
        chat_result = transaction.execute(
            """
            UPDATE chats
            SET render_revision = %s, conversation_commit_id = %s,
                snapshot_committed_at = %s, updated_at = %s,
                has_saved_components = EXISTS (
                    SELECT 1 FROM saved_components
                    WHERE chat_id = %s AND user_id = %s
                      AND conversation_commit_id = %s
                      AND committed_render_revision = %s
                )
            WHERE id = %s AND user_id = %s AND render_revision = %s
              AND conversation_commit_id IS NOT DISTINCT FROM %s
            """,
            (
                next_revision,
                publication_id,
                committed_at,
                updated_at,
                conversation_id,
                owner_id,
                publication_id,
                next_revision,
                conversation_id,
                owner_id,
                head_revision,
                expected_head_publication_id,
            ),
        )
        if chat_result.rowcount != 1:
            raise RepositoryConflictError(
                "conversation authority changed during atomic publication",
                metadata={"operation": "publication.commit_at_head"},
            )
        return committed

    def _chat_authority_matches(
        self,
        query: QueryExecutor,
        publication: PublicationRecord,
    ) -> bool:
        row = query.fetch_one(
            """
            SELECT 1 AS matched
            FROM chats
            WHERE id = %s AND user_id = %s
              AND render_revision = %s AND conversation_commit_id = %s
            """,
            (
                publication.conversation_id,
                publication.owner_id,
                publication.committed_render_revision,
                publication.publication_id,
            ),
        )
        return row is not None

    def abort(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        aborted_at: datetime,
    ) -> PublicationRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id = _required_id(publication_id, "publication_id")
        aborted_at = _aware_time(aborted_at, "aborted_at")
        result = transaction.execute(
            f"""
            UPDATE conversation_commit
            SET state = 'aborted', aborted_at = %s,
                execution_base_commit_id = NULL
            WHERE commit_id = %s AND chat_id = %s AND owner_user_id = %s
              AND state = 'staged'
            RETURNING {self._FIELDS}
            """,
            (aborted_at, publication_id, conversation_id, owner_id),
        )
        row = _optional_returned(result, "publication.abort")
        if row is None:
            existing = self.get(
                transaction,
                owner_id=owner_id,
                conversation_id=conversation_id,
                publication_id=publication_id,
            )
            if existing is None:
                raise RepositoryNotFoundError(
                    "publication was not found",
                    metadata={"operation": "publication.abort"},
                )
            if existing.state == "aborted":
                return existing
            raise RepositoryConflictError(
                "a committed publication cannot be aborted",
                metadata={"operation": "publication.abort"},
            )
        for table in ("saved_components", "workspace_layout", "messages"):
            transaction.execute(
                f"DELETE FROM {table} "
                "WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s",
                (publication_id, conversation_id, owner_id),
            )
        return _publication(row)


class WorkspaceRepository:
    def __init__(self) -> None:
        self.canvas = CanvasRepository()
        self.layouts = LayoutRepository()
        self.snapshots = WorkspaceSnapshotRepository()
        self.publications = PublicationRepository()


__all__ = (
    "CanvasComponentRecord",
    "CanvasRepository",
    "LayoutRecord",
    "LayoutRepository",
    "PublicationAssistantContentRecord",
    "PublicationRebaseComponent",
    "PublicationRebaseLayout",
    "PublicationRecord",
    "PublicationRepository",
    "PublicationStageEntityFence",
    "PublicationStageMessageFence",
    "PublicationStageSummary",
    "WorkspaceRepository",
    "WorkspaceSnapshotRecord",
    "WorkspaceSnapshotRepository",
)
