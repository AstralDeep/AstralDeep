"""Owner-isolated conversations, revisioned messages, and durable encrypted web
sessions, including host-verified execution/consent observation fences. Widely
imported across AstralDeep's orchestrator session and work-authority modules.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.database.dialect import dialect_of
from astralplane.errors import PlaneError
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
)
from astralplane.repositories._issuing_identity import _issuing_pair


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    conversation_id: str
    owner_id: str
    title: str
    agent_id: str | None
    created_at: int
    updated_at: int
    render_revision: int
    publication_id: str | None
    has_saved_components: bool
    snapshot_committed_at: datetime | None = None
    fork_source_conversation_id: str | None = None
    fork_source_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class ConversationBranchHeadRecord:
    conversation_id: str
    owner_id: str
    head_message_id: int | None
    branch_snapshot_id: int | None
    version: int


@dataclass(frozen=True, slots=True)
class ConversationSummaryRecord:
    conversation_id: str
    owner_id: str
    title: str
    agent_id: str | None
    created_at: int
    updated_at: int
    render_revision: int
    publication_id: str | None
    has_saved_components: bool
    latest_message_content: Any | None
    snapshot_committed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    message_id: int
    conversation_id: str
    owner_id: str
    role: str
    content: Any
    timestamp: int
    publication_id: str | None
    commit_position: int | None
    committed_render_revision: int | None
    parent_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    owner_id: str
    access_token_ciphertext: str
    refresh_token_ciphertext: str
    interactive_anchor: int
    hard_expires_at: int
    last_refresh_at: int
    resumed: bool
    created_at: int
    incarnation_id: str | None = field(default=None, repr=False)
    issuing_issuer: str | None = None
    issuing_client_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionCredentialFence:
    owner_id: str = field(repr=False)
    session_id: str = field(repr=False)
    created_at: int
    interactive_anchor: int
    hard_expires_at: int
    refresh_generation: int
    encrypted_state_binding: str = field(repr=False)
    incarnation_id: str = field(repr=False)
    version: int = 2
    issuing_issuer: str | None = None
    issuing_client_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionExecutionState:
    credential: SessionCredentialFence = field(repr=False)
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SessionExecutionObservation:
    credential: SessionCredentialFence = field(repr=False)
    started_at: datetime
    valid_until: datetime
    version: int = 1


@dataclass(frozen=True, slots=True)
class SessionConsentObservation:
    credential: SessionCredentialFence = field(repr=False)
    started_at: datetime
    valid_until: datetime
    version: int = 1


@dataclass(frozen=True, slots=True)
class FrameworkCredentialFence:
    owner_id: str = field(repr=False)
    credential_id: str = field(repr=False)
    token_hash: str = field(repr=False)
    scopes: tuple[str, ...]
    max_admissions: int
    consumed_admissions: int
    created_at: int
    expires_at: int
    revoked_at: int | None = None


@dataclass(frozen=True, slots=True)
class FrameworkCredentialExecutionState:
    credential: FrameworkCredentialFence = field(repr=False)
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class FrameworkCredentialObservation:
    credential: FrameworkCredentialFence = field(repr=False)
    started_at: datetime
    valid_until: datetime
    version: int = 1


def _incarnation(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value
        )
        is None
    ):
        raise RepositoryValidationError("canonical session incarnation required")
    return value


def _session_fence(value: object) -> SessionCredentialFence:
    if not isinstance(value, SessionCredentialFence) or type(value.version) is not int:
        raise RepositoryValidationError("typed session credential fence required")
    if value.version != 2:
        raise RepositoryValidationError("unsupported session credential fence")
    _incarnation(value.incarnation_id)
    _issuing_pair(value.issuing_issuer, value.issuing_client_id)
    _required_id(value.owner_id, "owner_id")
    _required_id(value.session_id, "session_id")
    for name in ("created_at", "interactive_anchor", "hard_expires_at", "refresh_generation"):
        number = getattr(value, name)
        if type(number) is not int or not 0 <= number <= 2**53 - 1:
            raise RepositoryValidationError("invalid session credential generation")
    if not (
        value.created_at <= value.interactive_anchor < value.hard_expires_at
        and value.interactive_anchor <= value.refresh_generation
    ):
        raise RepositoryValidationError("invalid session lifetime")
    if not isinstance(value.encrypted_state_binding, str) or not re.fullmatch(
        "[0-9a-f]{64}", value.encrypted_state_binding
    ):
        raise RepositoryValidationError("invalid encrypted session binding")
    return value


def _session_observation(value: object) -> SessionExecutionObservation:
    if not isinstance(value, SessionExecutionObservation) or type(value.version) is not int:
        raise RepositoryValidationError("typed session execution observation required")
    if value.version != 1:
        raise RepositoryValidationError("unsupported session execution observation")
    _validate_observation_lifetime(value)
    return value


def _validate_observation_lifetime(
    value: SessionExecutionObservation | SessionConsentObservation,
) -> None:
    _session_fence(value.credential)
    for timestamp in (value.started_at, value.valid_until):
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise RepositoryValidationError("aware session observation timestamp required")
    if not timedelta(0) < value.valid_until - value.started_at <= timedelta(seconds=15):
        raise RepositoryValidationError("session observation exceeds freshness bound")


def _session_unavailable() -> None:
    raise RepositoryConflictError(
        "session authority unavailable", code="session_authority_unavailable"
    )


_FRAMEWORK_SCOPE_PATTERN = re.compile(r"^[0-9a-z][0-9a-z_.]{0,63}$")
_TOKEN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _framework_credential_fence(value: object) -> FrameworkCredentialFence:
    if not isinstance(value, FrameworkCredentialFence):
        raise RepositoryValidationError("typed framework credential fence required")
    _required_id(value.owner_id, "owner_id")
    _required_id(value.credential_id, "credential_id")
    if (
        not isinstance(value.token_hash, str)
        or _TOKEN_HASH_PATTERN.fullmatch(value.token_hash) is None
    ):
        raise RepositoryValidationError("invalid framework credential token hash")
    if not isinstance(value.scopes, tuple) or not value.scopes:
        raise RepositoryValidationError("framework credential scopes must be a non-empty tuple")
    for scope in value.scopes:
        if not isinstance(scope, str) or _FRAMEWORK_SCOPE_PATTERN.fullmatch(scope) is None:
            raise RepositoryValidationError("invalid framework credential scope")
    for name in ("max_admissions", "consumed_admissions", "created_at", "expires_at"):
        number = getattr(value, name)
        if type(number) is not int or not 0 <= number <= 2**53 - 1:
            raise RepositoryValidationError("invalid framework credential generation")
    if (
        not 1 <= value.max_admissions <= 10000
        or not 0 <= value.consumed_admissions <= value.max_admissions
    ):
        raise RepositoryValidationError("invalid framework credential allowance")
    if value.expires_at <= value.created_at:
        raise RepositoryValidationError("invalid framework credential lifetime")
    if value.revoked_at is not None and (
        type(value.revoked_at) is not int or not 0 <= value.revoked_at <= 2**53 - 1
    ):
        raise RepositoryValidationError("invalid framework credential revocation")
    return value


def _framework_credential_observation(value: object) -> FrameworkCredentialObservation:
    if not isinstance(value, FrameworkCredentialObservation) or type(value.version) is not int:
        raise RepositoryValidationError("typed framework credential observation required")
    if value.version != 1:
        raise RepositoryValidationError("unsupported framework credential observation")
    _framework_credential_fence(value.credential)
    for timestamp in (value.started_at, value.valid_until):
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise RepositoryValidationError("aware framework observation timestamp required")
    if not timedelta(0) < value.valid_until - value.started_at <= timedelta(seconds=15):
        raise RepositoryValidationError("framework observation exceeds freshness bound")
    return value


def _framework_credential_unavailable() -> None:
    raise RepositoryConflictError(
        "credential authority unavailable", code="credential_authority_unavailable"
    )


def _optional_returned(result: object, operation: str) -> Any:
    rows = getattr(result, "returned_records", ())
    if not rows:
        return None
    return _single_returned(result, operation)


def _conversation(row: Any) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=str(_row_value(row, "id")),
        owner_id=str(_row_value(row, "user_id")),
        title=str(_row_value(row, "title")),
        agent_id=None if row.get("agent_id") is None else str(row["agent_id"]),
        created_at=int(_row_value(row, "created_at")),
        updated_at=int(_row_value(row, "updated_at")),
        render_revision=int(row.get("render_revision") or 0),
        publication_id=(
            None
            if row.get("conversation_commit_id") is None
            else str(row["conversation_commit_id"])
        ),
        has_saved_components=bool(row.get("has_saved_components")),
        snapshot_committed_at=(
            None if row.get("snapshot_committed_at") is None else row["snapshot_committed_at"]
        ),
        fork_source_conversation_id=(
            None
            if row.get("fork_source_chat_id") is None
            else str(row["fork_source_chat_id"])
        ),
        fork_source_message_id=(
            None
            if row.get("fork_source_message_id") is None
            else int(row["fork_source_message_id"])
        ),
    )


def _conversation_summary(row: Any) -> ConversationSummaryRecord:
    conversation = _conversation(row)
    content = row.get("latest_message_content")
    return ConversationSummaryRecord(
        conversation_id=conversation.conversation_id,
        owner_id=conversation.owner_id,
        title=conversation.title,
        agent_id=conversation.agent_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        render_revision=conversation.render_revision,
        publication_id=conversation.publication_id,
        has_saved_components=conversation.has_saved_components,
        latest_message_content=(None if content is None else _content_value(content)),
        snapshot_committed_at=conversation.snapshot_committed_at,
    )


def _conversation_branch_head(row: Any) -> ConversationBranchHeadRecord:
    return ConversationBranchHeadRecord(
        conversation_id=str(_row_value(row, "chat_id")),
        owner_id=str(_row_value(row, "user_id")),
        head_message_id=(
            None if row.get("head_message_id") is None else int(row["head_message_id"])
        ),
        branch_snapshot_id=(
            None if row.get("branch_snapshot_id") is None else int(row["branch_snapshot_id"])
        ),
        version=int(_row_value(row, "version")),
    )


def _message(row: Any) -> MessageRecord:
    return MessageRecord(
        message_id=int(_row_value(row, "id")),
        conversation_id=str(_row_value(row, "chat_id")),
        owner_id=str(_row_value(row, "user_id")),
        role=str(_row_value(row, "role")),
        content=_content_value(_row_value(row, "content")),
        timestamp=int(_row_value(row, "timestamp")),
        publication_id=(
            None
            if row.get("conversation_commit_id") is None
            else str(row["conversation_commit_id"])
        ),
        commit_position=(
            None if row.get("commit_position") is None else int(row["commit_position"])
        ),
        committed_render_revision=(
            None
            if row.get("committed_render_revision") is None
            else int(row["committed_render_revision"])
        ),
        parent_message_id=(
            None if row.get("parent_message_id") is None else int(row["parent_message_id"])
        ),
    )


def _session(row: Any) -> SessionRecord:
    try:
        incarnation = _incarnation(str(row["incarnation_id"]))
        issuer, client_id = _issuing_pair(row["issuing_issuer"], row["issuing_client_id"])
    except (KeyError, RepositoryValidationError):
        raise RepositoryDataError("stored session identity is invalid") from None
    return SessionRecord(
        session_id=str(_row_value(row, "sid")),
        owner_id=str(_row_value(row, "user_id")),
        access_token_ciphertext=str(_row_value(row, "access_token_enc")),
        refresh_token_ciphertext=str(_row_value(row, "refresh_token_enc")),
        interactive_anchor=int(_row_value(row, "interactive_anchor")),
        hard_expires_at=int(_row_value(row, "hard_expires_at")),
        last_refresh_at=int(_row_value(row, "last_refresh_at")),
        resumed=bool(row.get("resumed")),
        created_at=int(_row_value(row, "created_at")),
        incarnation_id=incarnation,
        issuing_issuer=issuer,
        issuing_client_id=client_id,
    )


class ConversationRepository:
    _SELECT = """
        SELECT id, user_id, title, agent_id, created_at, updated_at,
               COALESCE(render_revision, 0) AS render_revision,
               conversation_commit_id, COALESCE(has_saved_components, FALSE)
                   AS has_saved_components, snapshot_committed_at,
               fork_source_chat_id, fork_source_message_id
        FROM chats
    """

    _HEAD_SELECT = """
        SELECT head.chat_id, head.user_id, head.head_message_id,
               head.branch_snapshot_id, head.version
        FROM chat_branch_head AS head
    """

    def create(
        self,
        transaction: Transaction,
        *,
        conversation_id: str,
        owner_id: str,
        title: str,
        agent_id: str | None,
        created_at: int,
    ) -> ConversationRecord:
        conversation_id = _required_id(conversation_id, "conversation_id")
        owner_id = _required_id(owner_id, "owner_id")
        title = _bounded_text(title, "title", maximum=512)
        if agent_id is not None:
            agent_id = _required_id(agent_id, "agent_id")
        created_at = _non_negative_int(created_at, "created_at")
        result = transaction.execute(
            """
            INSERT INTO chats (
                id, user_id, title, agent_id, created_at, updated_at,
                has_saved_components, render_revision
            ) VALUES (%s, %s, %s, %s, %s, %s, FALSE, 0)
            ON CONFLICT (id) DO NOTHING
            RETURNING id, user_id, title, agent_id, created_at, updated_at,
                      render_revision, conversation_commit_id, has_saved_components
            """,
            (conversation_id, owner_id, title, agent_id, created_at, created_at),
        )
        row = _optional_returned(result, "conversation.create")
        if row is not None:
            transaction.execute(
                """
                INSERT INTO chat_branch_head (chat_id, user_id, head_message_id,
                                              branch_snapshot_id, version)
                SELECT id, user_id, NULL, NULL, 0
                FROM chats
                WHERE id = %s AND user_id = %s
                ON CONFLICT (chat_id) DO NOTHING
                """,
                (conversation_id, owner_id),
            )
            return _conversation(row)
        existing = self.get(transaction, owner_id=owner_id, conversation_id=conversation_id)
        if existing is None:
            raise RepositoryConflictError(
                "conversation identity is already owned by another namespace",
                metadata={"operation": "conversation.create"},
            )
        expected = (title, agent_id, created_at, None, None)
        observed = (
            existing.title,
            existing.agent_id,
            existing.created_at,
            existing.fork_source_conversation_id,
            existing.fork_source_message_id,
        )
        if observed != expected:
            raise RepositoryConflictError(
                "conversation idempotency identity was reused with different semantics",
                metadata={"operation": "conversation.create"},
            )
        return existing

    def fork(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        source_conversation_id: str,
        source_message_id: int,
        title: str,
        agent_id: str | None,
        created_at: int,
    ) -> ConversationRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        source_conversation_id = _required_id(source_conversation_id, "source_conversation_id")
        source_message_id = _positive_int(source_message_id, "source_message_id")
        title = _bounded_text(title, "title", maximum=512)
        if agent_id is not None:
            agent_id = _required_id(agent_id, "agent_id")
        created_at = _non_negative_int(created_at, "created_at")
        if conversation_id == source_conversation_id:
            raise RepositoryValidationError("fork conversation must differ from its source")
        source = transaction.fetch_one(
            f"""
            SELECT {MessageRepository._FIELDS}
            FROM messages AS message
            LEFT JOIN conversation_commit AS publication
              ON publication.commit_id = message.conversation_commit_id
             AND publication.chat_id = message.chat_id
             AND publication.owner_user_id = message.user_id
            WHERE message.id = %s AND message.chat_id = %s AND message.user_id = %s
              AND (
                message.conversation_commit_id IS NULL
                OR (
                    publication.state = 'committed'
                    AND publication.committed_render_revision =
                        message.committed_render_revision
                )
              )
            """,
            (source_message_id, source_conversation_id, owner_id),
        )
        if source is None:
            raise RepositoryNotFoundError(
                "fork source message was not found",
                metadata={"operation": "conversation.fork"},
            )
        result = transaction.execute(
            """
            INSERT INTO chats (
                id, user_id, title, agent_id, created_at, updated_at,
                has_saved_components, render_revision,
                fork_source_chat_id, fork_source_message_id
            )
            SELECT %s, %s, %s, %s, %s, %s, FALSE, 0, %s, %s
            FROM messages AS message
            WHERE message.id = %s AND message.chat_id = %s AND message.user_id = %s
            ON CONFLICT (id) DO NOTHING
            RETURNING id, user_id, title, agent_id, created_at, updated_at,
                      render_revision, conversation_commit_id, has_saved_components,
                      fork_source_chat_id, fork_source_message_id
            """,
            (
                conversation_id,
                owner_id,
                title,
                agent_id,
                created_at,
                created_at,
                source_conversation_id,
                source_message_id,
                source_message_id,
                source_conversation_id,
                owner_id,
            ),
        )
        row = _optional_returned(result, "conversation.fork")
        if row is not None:
            transaction.execute(
                """
                INSERT INTO chat_branch_head (chat_id, user_id, head_message_id,
                                              branch_snapshot_id, version)
                SELECT id, user_id, NULL, NULL, 0
                FROM chats
                WHERE id = %s AND user_id = %s
                ON CONFLICT (chat_id) DO NOTHING
                """,
                (conversation_id, owner_id),
            )
            return _conversation(row)
        existing = self.get(transaction, owner_id=owner_id, conversation_id=conversation_id)
        if existing is None:
            raise RepositoryConflictError(
                "conversation identity is already owned by another namespace",
                metadata={"operation": "conversation.fork"},
            )
        expected = (title, agent_id, created_at, source_conversation_id, source_message_id)
        observed = (
            existing.title,
            existing.agent_id,
            existing.created_at,
            existing.fork_source_conversation_id,
            existing.fork_source_message_id,
        )
        if observed != expected:
            raise RepositoryConflictError(
                "conversation idempotency identity was reused with different semantics",
                metadata={"operation": "conversation.fork"},
            )
        return existing

    def branch_head(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> ConversationBranchHeadRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        row = query.fetch_one(
            self._HEAD_SELECT + " WHERE head.chat_id = %s AND head.user_id = %s",
            (conversation_id, owner_id),
        )
        return None if row is None else _conversation_branch_head(row)

    def move_branch_head(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        head_message_id: int | None,
        expected_version: int,
        branch_snapshot_id: int | None = None,
    ) -> ConversationBranchHeadRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        expected_version = _non_negative_int(expected_version, "expected_version")
        if branch_snapshot_id is not None:
            branch_snapshot_id = _positive_int(branch_snapshot_id, "branch_snapshot_id")
        if head_message_id is not None:
            head_message_id = _positive_int(head_message_id, "head_message_id")
            visible = transaction.fetch_one(
                f"""
                SELECT {MessageRepository._FIELDS}
                FROM messages AS message
                LEFT JOIN conversation_commit AS publication
                  ON publication.commit_id = message.conversation_commit_id
                 AND publication.chat_id = message.chat_id
                 AND publication.owner_user_id = message.user_id
                WHERE message.id = %s AND message.chat_id = %s AND message.user_id = %s
                  AND (
                    message.conversation_commit_id IS NULL
                    OR (
                        publication.state = 'committed'
                        AND publication.committed_render_revision =
                            message.committed_render_revision
                    )
                  )
                """,
                (head_message_id, conversation_id, owner_id),
            )
            if visible is None:
                raise RepositoryNotFoundError(
                    "branch head message was not found",
                    metadata={"operation": "conversation.move_branch_head"},
                )
        if branch_snapshot_id is not None:
            snapshot = transaction.fetch_one(
                """
                SELECT snapshot.id
                FROM workspace_snapshot AS snapshot
                WHERE snapshot.id = %s AND snapshot.chat_id = %s AND snapshot.user_id = %s
                """,
                (branch_snapshot_id, conversation_id, owner_id),
            )
            if snapshot is None:
                raise RepositoryNotFoundError(
                    "branch head snapshot was not found",
                    metadata={"operation": "conversation.move_branch_head"},
                )
        result = transaction.execute(
            """
            UPDATE chat_branch_head AS head
            SET head_message_id = %s, branch_snapshot_id = %s, version = head.version + 1
            WHERE head.chat_id = %s AND head.user_id = %s AND head.version = %s
            RETURNING chat_id, user_id, head_message_id, branch_snapshot_id, version
            """,
            (head_message_id, branch_snapshot_id, conversation_id, owner_id, expected_version),
        )
        row = _optional_returned(result, "conversation.move_branch_head")
        if row is not None:
            return _conversation_branch_head(row)
        if self.get(transaction, owner_id=owner_id, conversation_id=conversation_id) is None:
            raise RepositoryNotFoundError(
                "conversation was not found",
                metadata={"operation": "conversation.move_branch_head"},
            )
        raise RepositoryConflictError(
            "branch head changed since it was read",
            metadata={
                "operation": "conversation.move_branch_head",
                "expected_version": expected_version,
            },
        )

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        for_update: bool = False,
    ) -> ConversationRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        lock = " FOR UPDATE" if for_update else ""
        row = query.fetch_one(
            self._SELECT + " WHERE id = %s AND user_id = %s" + lock,
            (conversation_id, owner_id),
        )
        return None if row is None else _conversation(row)

    def get_for_administration(
        self,
        query: QueryExecutor,
        *,
        conversation_id: str,
        for_update: bool = False,
    ) -> ConversationRecord | None:
        conversation_id = _required_id(conversation_id, "conversation_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        lock = " FOR UPDATE" if for_update else ""
        row = query.fetch_one(
            self._SELECT + " WHERE id = %s" + lock,
            (conversation_id,),
        )
        return None if row is None else _conversation(row)

    def list_recent(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        limit: int = 20,
    ) -> tuple[ConversationRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        limit = _bounded_limit(limit)
        rows = query.fetch_all(
            self._SELECT + " WHERE user_id = %s ORDER BY updated_at DESC, id ASC LIMIT %s",
            (owner_id, limit),
        )
        return tuple(_conversation(row) for row in rows)

    def list_recent_nonempty(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        limit: int = 20,
    ) -> tuple[ConversationSummaryRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        limit = _bounded_limit(limit, maximum=200)
        rows = query.fetch_all(
            """
            SELECT chat.id, chat.user_id, chat.title, chat.agent_id,
                   chat.created_at, chat.updated_at,
                   COALESCE(chat.render_revision, 0) AS render_revision,
                   chat.conversation_commit_id,
                   COALESCE(chat.has_saved_components, FALSE)
                       AS has_saved_components,
                   chat.snapshot_committed_at,
                   (
                       SELECT message.content
                       FROM messages AS message
                       WHERE message.chat_id = chat.id
                         AND message.user_id = chat.user_id
                         AND (
                           message.conversation_commit_id IS NULL
                           OR EXISTS (
                               SELECT 1
                               FROM conversation_commit AS publication
                               WHERE publication.commit_id =
                                         message.conversation_commit_id
                                 AND publication.chat_id = message.chat_id
                                 AND publication.owner_user_id = message.user_id
                                 AND publication.state = 'committed'
                                 AND publication.committed_render_revision =
                                         message.committed_render_revision
                           )
                         )
                       ORDER BY
                           COALESCE(message.committed_render_revision, 0) DESC,
                           CASE
                               WHEN message.conversation_commit_id IS NULL
                                   THEN message.timestamp
                               ELSE message.commit_position::BIGINT
                           END DESC,
                           message.id DESC
                       LIMIT 1
                   ) AS latest_message_content
            FROM chats AS chat
            WHERE chat.user_id = %s
              AND chat.id NOT LIKE 'draft-test-%%'
              AND EXISTS (
                  SELECT 1
                  FROM messages AS visible
                  WHERE visible.chat_id = chat.id
                    AND visible.user_id = chat.user_id
                    AND (
                      visible.conversation_commit_id IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM conversation_commit AS publication
                          WHERE publication.commit_id =
                                    visible.conversation_commit_id
                            AND publication.chat_id = visible.chat_id
                            AND publication.owner_user_id = visible.user_id
                            AND publication.state = 'committed'
                            AND publication.committed_render_revision =
                                    visible.committed_render_revision
                      )
                    )
              )
            ORDER BY chat.updated_at DESC, chat.id DESC
            LIMIT %s
            """,
            (owner_id, limit),
        )
        return tuple(_conversation_summary(row) for row in rows)

    def rename(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        title: str,
        expected_updated_at: int,
        updated_at: int,
    ) -> ConversationRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        title = _bounded_text(title, "title", maximum=512)
        expected_updated_at = _non_negative_int(expected_updated_at, "expected_updated_at")
        updated_at = _non_negative_int(updated_at, "updated_at")
        if updated_at <= expected_updated_at:
            raise RepositoryValidationError("updated_at must advance the compare-and-set fence")
        result = transaction.execute(
            """
            UPDATE chats
            SET title = %s, updated_at = %s
            WHERE id = %s AND user_id = %s AND updated_at = %s
            RETURNING id, user_id, title, agent_id, created_at, updated_at,
                      render_revision, conversation_commit_id, has_saved_components
            """,
            (title, updated_at, conversation_id, owner_id, expected_updated_at),
        )
        row = _optional_returned(result, "conversation.rename")
        if row is not None:
            return _conversation(row)
        existing = self.get(transaction, owner_id=owner_id, conversation_id=conversation_id)
        if existing is None:
            raise RepositoryNotFoundError(
                "conversation was not found", metadata={"operation": "conversation.rename"}
            )
        raise RepositoryConflictError(
            "conversation changed since it was read",
            metadata={"operation": "conversation.rename"},
        )

    def delete(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> bool:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        result = transaction.execute(
            "DELETE FROM chats WHERE id = %s AND user_id = %s",
            (conversation_id, owner_id),
        )
        return result.rowcount == 1


class MessageRepository:
    _FIELDS = (
        "message.id, message.chat_id, message.user_id, message.role, message.content, "
        "message.timestamp, message.conversation_commit_id, message.commit_position, "
        "message.committed_render_revision, message.parent_message_id"
    )

    @staticmethod
    def _stored_content(content: object) -> str:
        # Encode as JSON so prose like '[]' can't be misread as data
        return _canonical_json(content, "content")

    @staticmethod
    def _visible_message_filter() -> str:
        return """
              AND (
                message.conversation_commit_id IS NULL
                OR (
                    publication.state = 'committed'
                    AND publication.committed_render_revision =
                        message.committed_render_revision
                )
              )"""

    def _visible_parent_id(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        parent_message_id: int,
        publication_id: str | None = None,
    ) -> int:
        same_publication = (
            """
              OR message.conversation_commit_id = %s"""
            if publication_id is not None
            else ""
        )
        parameters: list[object] = [parent_message_id, conversation_id, owner_id]
        if publication_id is not None:
            parameters.append(publication_id)
        row = query.fetch_one(
            f"""
            SELECT message.id
            FROM messages AS message
            LEFT JOIN conversation_commit AS publication
              ON publication.commit_id = message.conversation_commit_id
             AND publication.chat_id = message.chat_id
             AND publication.owner_user_id = message.user_id
            WHERE message.id = %s AND message.chat_id = %s AND message.user_id = %s
              AND (
                message.conversation_commit_id IS NULL
                OR (
                    publication.state = 'committed'
                    AND publication.committed_render_revision =
                        message.committed_render_revision
                )
                {same_publication}
              )
            """,
            tuple(parameters),
        )
        if row is None:
            raise RepositoryNotFoundError(
                "branch parent message was not found",
                metadata={"operation": "message.append"},
            )
        return int(_row_value(row, "id"))

    def append(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        role: str,
        content: object,
        timestamp: int,
        publication_id: str | None = None,
        commit_position: int | None = None,
        committed_render_revision: int | None = None,
        parent_message_id: int | None = None,
    ) -> MessageRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        role = _bounded_text(role, "role", maximum=64)
        timestamp = _non_negative_int(timestamp, "timestamp")
        if parent_message_id is not None:
            parent_message_id = self._visible_parent_id(
                transaction,
                owner_id=owner_id,
                conversation_id=conversation_id,
                parent_message_id=parent_message_id,
                publication_id=publication_id,
            )
        stored_content = self._stored_content(content)
        revisioned = publication_id is not None
        if revisioned:
            publication_id = _required_id(publication_id, "publication_id")
            commit_position = _non_negative_int(commit_position, "commit_position")
            committed_render_revision = _non_negative_int(
                committed_render_revision, "committed_render_revision"
            )
            if committed_render_revision == 0:
                raise RepositoryValidationError(
                    "revisioned messages require a positive render revision"
                )
            result = transaction.execute(
                """
                INSERT INTO messages (
                    chat_id, user_id, role, content, timestamp,
                    conversation_commit_id, commit_position,
                    committed_render_revision, parent_message_id
                )
                SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s
                FROM conversation_commit AS publication
                WHERE publication.commit_id = %s
                  AND publication.chat_id = %s
                  AND publication.owner_user_id = %s
                  AND publication.state = 'staged'
                  AND publication.base_render_revision + 1 = %s
                ON CONFLICT (conversation_commit_id, commit_position)
                    WHERE conversation_commit_id IS NOT NULL
                    DO NOTHING
                RETURNING id, chat_id, user_id, role, content, timestamp,
                          conversation_commit_id, commit_position,
                          committed_render_revision, parent_message_id
                """,
                (
                    conversation_id,
                    owner_id,
                    role,
                    stored_content,
                    timestamp,
                    publication_id,
                    commit_position,
                    committed_render_revision,
                    parent_message_id,
                    publication_id,
                    conversation_id,
                    owner_id,
                    committed_render_revision,
                ),
            )
        else:
            if commit_position is not None or committed_render_revision is not None:
                raise RepositoryValidationError("legacy messages cannot carry publication metadata")
            result = transaction.execute(
                """
                INSERT INTO messages (chat_id, user_id, role, content, timestamp,
                                      parent_message_id)
                SELECT %s, %s, %s, %s, %s, %s
                FROM chats
                WHERE id = %s AND user_id = %s
                  AND COALESCE(render_revision, 0) = 0
                RETURNING id, chat_id, user_id, role, content, timestamp,
                          conversation_commit_id, commit_position,
                          committed_render_revision, parent_message_id
                """,
                (
                    conversation_id,
                    owner_id,
                    role,
                    stored_content,
                    timestamp,
                    parent_message_id,
                    conversation_id,
                    owner_id,
                ),
            )
        row = _optional_returned(result, "message.append")
        if row is not None:
            return _message(row)
        if not revisioned:
            raise RepositoryNotFoundError(
                "conversation is unavailable for legacy message publication",
                metadata={"operation": "message.append"},
            )
        existing = self.get_by_publication_position(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=publication_id,
            commit_position=commit_position,
        )
        if existing is None:
            raise RepositoryNotFoundError(
                "staged publication is unavailable",
                metadata={"operation": "message.append"},
            )
        expected = (
            role,
            _content_value(stored_content),
            timestamp,
            committed_render_revision,
            parent_message_id,
        )
        observed = (
            existing.role,
            existing.content,
            existing.timestamp,
            existing.committed_render_revision,
            existing.parent_message_id,
        )
        if observed != expected:
            raise RepositoryConflictError(
                "message idempotency position was reused with different semantics",
                metadata={"operation": "message.append"},
            )
        return existing

    def append_next_to_staged_publication(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        role: str,
        content: object,
        timestamp: int | None = None,
    ) -> MessageRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id = _required_id(publication_id, "publication_id")
        role = _bounded_text(role, "role", maximum=64)
        if timestamp is not None:
            timestamp = _non_negative_int(timestamp, "timestamp")
        staged = transaction.fetch_one(
            """
            SELECT publication.base_render_revision, publication.state,
                   publication.publication_role,
                   COALESCE(chat.render_revision, 0) AS chat_render_revision
            FROM conversation_commit AS publication
            JOIN chats AS chat
              ON chat.id = publication.chat_id
             AND chat.user_id = publication.owner_user_id
            WHERE publication.commit_id = %s
              AND publication.chat_id = %s
              AND publication.owner_user_id = %s
            FOR UPDATE OF publication, chat
            """,
            (publication_id, conversation_id, owner_id),
        )
        if staged is None:
            raise RepositoryNotFoundError(
                "staged publication was not found",
                metadata={"operation": "message.append_next_to_staged_publication"},
            )
        if str(_row_value(staged, "state")) != "staged":
            raise RepositoryConflictError(
                "publication is terminal",
                metadata={"operation": "message.append_next_to_staged_publication"},
            )
        base_revision = int(_row_value(staged, "base_render_revision"))
        publication_role = str(staged.get("publication_role") or "atomic")
        if (
            publication_role != "assistant_result"
            and int(_row_value(staged, "chat_render_revision")) != base_revision
        ):
            raise RepositoryConflictError(
                "conversation base revision changed before staged append",
                metadata={"operation": "message.append_next_to_staged_publication"},
            )
        position_row = transaction.fetch_one(
            """
            SELECT COALESCE(MAX(commit_position), -1) + 1 AS next_position
            FROM messages
            WHERE conversation_commit_id = %s AND chat_id = %s AND user_id = %s
            """,
            (publication_id, conversation_id, owner_id),
        )
        if position_row is None:  # pragma: no cover
            raise RepositoryDataError("publication position query returned no row")
        position = int(_row_value(position_row, "next_position"))
        if position < 0:  # pragma: no cover
            raise RepositoryDataError("publication position query returned a negative value")
        if timestamp is None:
            timestamp_row = transaction.fetch_one(
                """
                SELECT (extract(epoch from clock_timestamp()) * 1000)::bigint
                       AS observed_at
                """
            )
            if timestamp_row is None:  # pragma: no cover
                raise RepositoryDataError("database timestamp query returned no row")
            timestamp = int(_row_value(timestamp_row, "observed_at")) + position
        if position == 0:
            head_row = transaction.fetch_one(
                """
                SELECT head.head_message_id
                FROM chat_branch_head AS head
                WHERE head.chat_id = %s AND head.user_id = %s
                """,
                (conversation_id, owner_id),
            )
            parent_message_id = (
                None
                if head_row is None or head_row.get("head_message_id") is None
                else int(head_row["head_message_id"])
            )
        else:
            previous_row = transaction.fetch_one(
                """
                SELECT message.id
                FROM messages AS message
                WHERE message.chat_id = %s AND message.user_id = %s
                  AND message.conversation_commit_id = %s
                  AND message.commit_position = %s
                """,
                (conversation_id, owner_id, publication_id, position - 1),
            )
            if previous_row is None:  # pragma: no cover
                raise RepositoryDataError("staged publication chain is broken")
            parent_message_id = int(_row_value(previous_row, "id"))
        return self.append(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            timestamp=timestamp,
            publication_id=publication_id,
            commit_position=position,
            committed_render_revision=base_revision + 1,
            parent_message_id=parent_message_id,
        )

    def get_by_publication_position(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        commit_position: int,
    ) -> MessageRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id = _required_id(publication_id, "publication_id")
        commit_position = _non_negative_int(commit_position, "commit_position")
        row = query.fetch_one(
            f"""
            SELECT {self._FIELDS}
            FROM messages AS message
            WHERE message.chat_id = %s AND message.user_id = %s
              AND message.conversation_commit_id = %s
              AND message.commit_position = %s
            """,
            (conversation_id, owner_id, publication_id, commit_position),
        )
        return None if row is None else _message(row)

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        message_id: int,
    ) -> MessageRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        message_id = _positive_int(message_id, "message_id")
        row = query.fetch_one(
            f"""
            SELECT {self._FIELDS}
            FROM messages AS message
            LEFT JOIN conversation_commit AS publication
              ON publication.commit_id = message.conversation_commit_id
             AND publication.chat_id = message.chat_id
             AND publication.owner_user_id = message.user_id
            WHERE message.id = %s AND message.chat_id = %s AND message.user_id = %s
              AND (
                message.conversation_commit_id IS NULL
                OR (
                    publication.state = 'committed'
                    AND publication.committed_render_revision =
                        message.committed_render_revision
                )
              )
            """,
            (message_id, conversation_id, owner_id),
        )
        return None if row is None else _message(row)

    def list_for_publication(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        publication_id: str,
        limit: int = 1000,
    ) -> tuple[MessageRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        publication_id = _required_id(publication_id, "publication_id")
        limit = _bounded_limit(limit, maximum=2000)
        rows = query.fetch_all(
            f"""
            SELECT {self._FIELDS}
            FROM messages AS message
            JOIN conversation_commit AS publication
              ON publication.commit_id = message.conversation_commit_id
             AND publication.chat_id = message.chat_id
             AND publication.owner_user_id = message.user_id
            WHERE message.chat_id = %s AND message.user_id = %s
              AND message.conversation_commit_id = %s
            ORDER BY message.commit_position, message.id
            LIMIT %s
            """,
            (conversation_id, owner_id, publication_id, limit),
        )
        return tuple(_message(row) for row in rows)

    def list_visible(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        through_render_revision: int | None = None,
        limit: int = 200,
    ) -> tuple[MessageRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        limit = _bounded_limit(limit, maximum=1000)
        if through_render_revision is not None:
            through_render_revision = _non_negative_int(
                through_render_revision, "through_render_revision"
            )
        rows = query.fetch_all(
            f"""
            SELECT {self._FIELDS}
            FROM messages AS message
            LEFT JOIN conversation_commit AS publication
              ON publication.commit_id = message.conversation_commit_id
             AND publication.chat_id = message.chat_id
             AND publication.owner_user_id = message.user_id
            WHERE message.chat_id = %s AND message.user_id = %s
              AND (
                message.conversation_commit_id IS NULL
                OR (
                    publication.state = 'committed'
                    AND publication.committed_render_revision =
                        message.committed_render_revision
                    AND (%s IS NULL OR message.committed_render_revision <= %s)
                )
              )
            ORDER BY
                COALESCE(message.committed_render_revision, 0) ASC,
                CASE
                    WHEN message.conversation_commit_id IS NULL
                        THEN message.timestamp
                    ELSE message.commit_position::BIGINT
                END ASC,
                message.id ASC
            LIMIT %s
            """,
            (
                conversation_id,
                owner_id,
                through_render_revision,
                through_render_revision,
                limit,
            ),
        )
        return tuple(_message(row) for row in rows)

    def latest_visible_id(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> int | None:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        row = query.fetch_one(
            """
            SELECT message.id
            FROM messages AS message
            LEFT JOIN conversation_commit AS publication
              ON publication.commit_id = message.conversation_commit_id
             AND publication.chat_id = message.chat_id
             AND publication.owner_user_id = message.user_id
            WHERE message.chat_id = %s AND message.user_id = %s
              AND (
                message.conversation_commit_id IS NULL
                OR (
                    publication.state = 'committed'
                    AND publication.committed_render_revision =
                        message.committed_render_revision
                )
              )
            ORDER BY
                COALESCE(message.committed_render_revision, 0) DESC,
                CASE
                    WHEN message.conversation_commit_id IS NULL
                        THEN message.timestamp
                    ELSE message.commit_position::BIGINT
                END DESC,
                message.id DESC
            LIMIT 1
            """,
            (conversation_id, owner_id),
        )
        return None if row is None else int(_row_value(row, "id"))

    def _require_conversation(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> None:
        row = query.fetch_one(
            "SELECT id FROM chats WHERE id = %s AND user_id = %s",
            (conversation_id, owner_id),
        )
        if row is None:
            raise RepositoryNotFoundError(
                "conversation was not found",
                metadata={"operation": "message.branch_read"},
            )

    def list_branch_path(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        limit: int = 200,
    ) -> tuple[MessageRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        limit = _bounded_limit(limit, maximum=1000)
        self._require_conversation(query, owner_id=owner_id, conversation_id=conversation_id)
        rows = query.fetch_all(
            f"""
            WITH RECURSIVE branch_path AS (
                SELECT head.head_message_id AS message_id, 1 AS depth
                FROM chat_branch_head AS head
                WHERE head.chat_id = %s AND head.user_id = %s
                  AND head.head_message_id IS NOT NULL
                UNION ALL
                SELECT message.parent_message_id, branch_path.depth + 1
                FROM branch_path
                JOIN messages AS message ON message.id = branch_path.message_id
                WHERE message.parent_message_id IS NOT NULL
                  AND branch_path.depth < 4096
            )
            SELECT {self._FIELDS}
            FROM branch_path AS branch
            JOIN messages AS message ON message.id = branch.message_id
            LEFT JOIN conversation_commit AS publication
              ON publication.commit_id = message.conversation_commit_id
             AND publication.chat_id = message.chat_id
             AND publication.owner_user_id = message.user_id
            WHERE message.chat_id = %s AND message.user_id = %s
            {self._visible_message_filter()}
            ORDER BY branch.depth DESC
            LIMIT %s
            """,
            (conversation_id, owner_id, conversation_id, owner_id, limit),
        )
        return tuple(_message(row) for row in rows)

    def count_message_siblings(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        message_id: int,
    ) -> int:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        message_id = _positive_int(message_id, "message_id")
        self._require_conversation(query, owner_id=owner_id, conversation_id=conversation_id)
        message = self.get(
            query,
            owner_id=owner_id,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        if message is None:
            raise RepositoryNotFoundError(
                "message was not found",
                metadata={"operation": "message.count_message_siblings"},
            )
        row = query.fetch_one(
            f"""
            SELECT count(*) AS siblings
            FROM messages AS message
            LEFT JOIN conversation_commit AS publication
              ON publication.commit_id = message.conversation_commit_id
             AND publication.chat_id = message.chat_id
             AND publication.owner_user_id = message.user_id
            WHERE message.chat_id = %s AND message.user_id = %s
              AND message.parent_message_id IS NOT DISTINCT FROM %s
            {self._visible_message_filter()}
            """,
            (conversation_id, owner_id, message.parent_message_id),
        )
        if row is None:  # pragma: no cover
            raise RepositoryDataError("sibling count query returned no row")
        return int(_row_value(row, "siblings"))

    def list_branch_heads(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        limit: int = 200,
    ) -> tuple[MessageRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        limit = _bounded_limit(limit, maximum=1000)
        self._require_conversation(query, owner_id=owner_id, conversation_id=conversation_id)
        rows = query.fetch_all(
            f"""
            SELECT {self._FIELDS}
            FROM messages AS message
            LEFT JOIN conversation_commit AS publication
              ON publication.commit_id = message.conversation_commit_id
             AND publication.chat_id = message.chat_id
             AND publication.owner_user_id = message.user_id
            WHERE message.chat_id = %s AND message.user_id = %s
            {self._visible_message_filter()}
              AND NOT EXISTS (
                  SELECT 1
                  FROM messages AS child
                  WHERE child.parent_message_id = message.id
              )
            ORDER BY
                COALESCE(message.committed_render_revision, 0) ASC,
                CASE
                    WHEN message.conversation_commit_id IS NULL
                        THEN message.timestamp
                    ELSE message.commit_position::BIGINT
                END ASC,
                message.id ASC
            LIMIT %s
            """,
            (conversation_id, owner_id, limit),
        )
        return tuple(_message(row) for row in rows)


class SessionRepository:
    _SELECT = """
        SELECT sid, user_id, access_token_enc, refresh_token_enc,
               interactive_anchor, hard_expires_at, last_refresh_at,
               resumed, created_at, incarnation_id, issuing_issuer, issuing_client_id
        FROM web_session
    """

    @staticmethod
    def bound_request_execution_waits(transaction: Transaction) -> None:
        if dialect_of(transaction).is_sqlite:
            for name, bound in (("lock_timeout", 100), ("statement_timeout", 1000)):
                transaction.fetch_one(
                    "SELECT set_config(%s,astral_timeout_cap(current_setting(%s),%s),true)",
                    (name, name, bound),
                )
            return
        transaction.execute(
            """
            SELECT set_config(
                settings.name,
                LEAST(NULLIF(settings.setting::bigint, 0), limits.milliseconds)::text,
                true
            )
            FROM pg_settings AS settings
            JOIN (VALUES ('lock_timeout', 100), ('statement_timeout', 1000))
                AS limits(name, milliseconds) ON settings.name = limits.name
            """
        )

    @staticmethod
    def execution_fence(record: SessionRecord) -> SessionCredentialFence:
        if not isinstance(record, SessionRecord):
            raise RepositoryValidationError("typed session record required")
        encrypted = []
        for value in (record.access_token_ciphertext, record.refresh_token_ciphertext):
            encrypted.append(_bounded_text(value, "encrypted credential", maximum=131072))
        issuer, client_id = _issuing_pair(record.issuing_issuer, record.issuing_client_id)
        bound_state = (
            encrypted
            if issuer is None
            else [*encrypted, {"issuing_issuer": issuer, "issuing_client_id": client_id}]
        )
        binding = hashlib.sha256(
            _canonical_json(bound_state, "encrypted state").encode("utf-8")
        ).hexdigest()
        return _session_fence(
            SessionCredentialFence(
                owner_id=record.owner_id,
                session_id=record.session_id,
                created_at=record.created_at,
                interactive_anchor=record.interactive_anchor,
                hard_expires_at=record.hard_expires_at,
                refresh_generation=record.last_refresh_at,
                encrypted_state_binding=binding,
                incarnation_id=record.incarnation_id,
                issuing_issuer=issuer,
                issuing_client_id=client_id,
            )
        )

    def get_execution_state(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        session_id: str,
    ) -> SessionExecutionState | None:
        record = self.get(query, owner_id=owner_id, session_id=session_id)
        if record is None:
            return None
        now = query.fetch_one("SELECT clock_timestamp() AS now")["now"]
        return SessionExecutionState(self.execution_fence(record), now)

    @staticmethod
    def _lock_execution_owner(transaction: Transaction, owner_id: str) -> None:
        transaction.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner_id,))
        retired = transaction.fetch_one(
            "SELECT state FROM astralplane_blob_owner_state WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        if retired is not None and retired["state"] != "active":
            _session_unavailable()

    def _assert_credential(
        self,
        transaction: Transaction,
        credential: SessionCredentialFence,
    ) -> SessionExecutionState:
        credential = _session_fence(credential)
        self._lock_execution_owner(transaction, credential.owner_id)
        row = transaction.fetch_one(
            self._SELECT + " WHERE sid = %s AND user_id = %s FOR UPDATE",
            (credential.session_id, credential.owner_id),
        )
        # clock_timestamp() here since the row lock above can wait
        now = transaction.fetch_one("SELECT clock_timestamp() AS now")["now"]
        if row is None:
            _session_unavailable()
        current = self.execution_fence(_session(row))
        if (
            current != credential
            or now.timestamp() < current.interactive_anchor
            or now.timestamp() >= current.hard_expires_at
        ):
            _session_unavailable()
        return SessionExecutionState(current, now)

    def assert_current_execution(
        self,
        transaction: Transaction,
        *,
        observation: SessionExecutionObservation,
    ) -> SessionExecutionState:
        observation = _session_observation(observation)
        current = self._assert_credential(transaction, observation.credential)
        if not observation.started_at <= current.observed_at < observation.valid_until:
            _session_unavailable()
        return current

    def assert_current_consent(
        self,
        transaction: Transaction,
        *,
        observation: SessionConsentObservation,
    ) -> SessionExecutionState:
        if (
            type(observation) is not SessionConsentObservation
            or type(observation.version) is not int
        ):
            raise RepositoryValidationError("typed session consent observation required")
        if observation.version != 1:
            raise RepositoryValidationError("unsupported session consent observation")
        _validate_observation_lifetime(observation)
        current = self._assert_credential(transaction, observation.credential)
        if not observation.started_at <= current.observed_at < observation.valid_until:
            _session_unavailable()
        return current

    def put(self, transaction: Transaction, record: SessionRecord) -> SessionRecord:
        issuer, client_id = _issuing_pair(record.issuing_issuer, record.issuing_client_id)
        session_id = _required_id(record.session_id, "session_id")
        owner_id = _required_id(record.owner_id, "owner_id")
        access = _bounded_text(
            record.access_token_ciphertext,
            "access_token_ciphertext",
            maximum=131072,
        )
        refresh = _bounded_text(
            record.refresh_token_ciphertext,
            "refresh_token_ciphertext",
            maximum=131072,
        )
        interactive_anchor = _non_negative_int(record.interactive_anchor, "interactive_anchor")
        hard_expires_at = _non_negative_int(record.hard_expires_at, "hard_expires_at")
        last_refresh_at = _non_negative_int(record.last_refresh_at, "last_refresh_at")
        created_at = _non_negative_int(record.created_at, "created_at")
        if record.incarnation_id is not None:
            _incarnation(record.incarnation_id)
        self._lock_execution_owner(transaction, owner_id)
        if record.incarnation_id is not None:
            row = transaction.fetch_one(
                self._SELECT + " WHERE sid = %s AND user_id = %s FOR UPDATE",
                (session_id, owner_id),
            )
            if row is not None and _session(row) == record:
                return _session(row)
            raise RepositoryConflictError("session incarnation cannot be issued by a caller")
        result = transaction.execute(
            """
            INSERT INTO web_session (
                sid, user_id, access_token_enc, refresh_token_enc,
                interactive_anchor, hard_expires_at, last_refresh_at,
                resumed, created_at, issuing_issuer, issuing_client_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sid) DO NOTHING
            RETURNING sid, user_id, access_token_enc, refresh_token_enc,
                      interactive_anchor, hard_expires_at, last_refresh_at,
                      resumed, created_at, incarnation_id, issuing_issuer, issuing_client_id
            """,
            (
                session_id,
                owner_id,
                access,
                refresh,
                interactive_anchor,
                hard_expires_at,
                last_refresh_at,
                bool(record.resumed),
                created_at,
                issuer,
                client_id,
            ),
        )
        row = _optional_returned(result, "session.put")
        if row is None:
            existing = self.get(
                transaction,
                owner_id=owner_id,
                session_id=session_id,
            )
            if existing is None or existing != replace(
                record, incarnation_id=existing.incarnation_id
            ):
                raise RepositoryConflictError(
                    "session identity replay changed durable state",
                    metadata={"operation": "session.put"},
                )
            return existing
        return _session(row)

    def compare_and_set_refresh(
        self,
        transaction: Transaction,
        record: SessionRecord,
        *,
        expected_last_refresh_at: int,
        expected_credential: SessionCredentialFence | None = None,
    ) -> SessionRecord:
        issuer, client_id = _issuing_pair(record.issuing_issuer, record.issuing_client_id)
        session_id = _required_id(record.session_id, "session_id")
        owner_id = _required_id(record.owner_id, "owner_id")
        access = _bounded_text(
            record.access_token_ciphertext,
            "access_token_ciphertext",
            maximum=131072,
        )
        refresh = _bounded_text(
            record.refresh_token_ciphertext,
            "refresh_token_ciphertext",
            maximum=131072,
        )
        interactive_anchor = _non_negative_int(record.interactive_anchor, "interactive_anchor")
        hard_expires_at = _non_negative_int(record.hard_expires_at, "hard_expires_at")
        last_refresh_at = _non_negative_int(record.last_refresh_at, "last_refresh_at")
        created_at = _non_negative_int(record.created_at, "created_at")
        expected = _non_negative_int(expected_last_refresh_at, "expected_last_refresh_at")
        incarnation = _incarnation(record.incarnation_id)
        if last_refresh_at <= expected:
            raise RepositoryValidationError(
                "last_refresh_at must advance beyond expected_last_refresh_at"
            )
        if expected_credential is not None:
            _session_fence(expected_credential)
            if (
                expected_credential.owner_id != owner_id
                or expected_credential.session_id != session_id
                or expected_credential.created_at != created_at
                or expected_credential.interactive_anchor != interactive_anchor
                or expected_credential.hard_expires_at != hard_expires_at
                or expected_credential.refresh_generation != expected
                or expected_credential.incarnation_id != incarnation
                or expected_credential.issuing_issuer != issuer
                or expected_credential.issuing_client_id != client_id
            ):
                raise RepositoryValidationError("refresh changed the bound session identity")
            self._assert_credential(transaction, expected_credential)
        result = transaction.execute(
            """
            UPDATE web_session SET
                access_token_enc = %s,
                refresh_token_enc = %s,
                last_refresh_at = %s,
                resumed = %s
            WHERE sid = %s AND user_id = %s AND created_at = %s
              AND interactive_anchor = %s AND hard_expires_at = %s
              AND last_refresh_at = %s AND incarnation_id = %s::uuid
              AND issuing_issuer IS NOT DISTINCT FROM %s
              AND issuing_client_id IS NOT DISTINCT FROM %s
            RETURNING sid, user_id, access_token_enc, refresh_token_enc,
                      interactive_anchor, hard_expires_at, last_refresh_at,
                      resumed, created_at, incarnation_id, issuing_issuer, issuing_client_id
            """,
            (
                access,
                refresh,
                last_refresh_at,
                bool(record.resumed),
                session_id,
                owner_id,
                created_at,
                interactive_anchor,
                hard_expires_at,
                expected,
                incarnation,
                issuer,
                client_id,
            ),
        )
        row = _optional_returned(result, "session.compare_and_set_refresh")
        if row is not None:
            return _session(row)
        existing = self.get(
            transaction,
            owner_id=owner_id,
            session_id=session_id,
        )
        if existing is None:
            raise RepositoryNotFoundError("owner-scoped web session was not found")
        raise RepositoryConflictError(
            "session refresh generation is stale",
            metadata={"operation": "session.compare_and_set_refresh"},
        )

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        session_id: str,
    ) -> SessionRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        session_id = _required_id(session_id, "session_id")
        row = query.fetch_one(
            self._SELECT + " WHERE sid = %s AND user_id = %s",
            (session_id, owner_id),
        )
        return None if row is None else _session(row)

    def get_by_incarnation(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        incarnation_id: str,
    ) -> SessionRecord | None:
        owner = _required_id(owner_id, "owner_id")
        incarnation = _incarnation(incarnation_id)
        row = query.fetch_one(
            self._SELECT + " WHERE user_id = %s AND incarnation_id = %s::uuid",
            (owner, incarnation),
        )
        return None if row is None else _session(row)

    def get_by_session_id_for_administration(
        self,
        query: QueryExecutor,
        *,
        session_id: str,
    ) -> SessionRecord | None:
        session_id = _required_id(session_id, "session_id")
        row = query.fetch_one(
            self._SELECT + " WHERE sid = %s",
            (session_id,),
        )
        return None if row is None else _session(row)

    def get_latest_live_for_owner(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        observed_at: int,
    ) -> SessionRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        observed_at = _non_negative_int(observed_at, "observed_at")
        row = query.fetch_one(
            self._SELECT
            + """
              WHERE user_id = %s AND hard_expires_at > %s
              ORDER BY last_refresh_at DESC, created_at DESC, sid DESC
              LIMIT 1
            """,
            (owner_id, observed_at),
        )
        return None if row is None else _session(row)

    def mark_resumed(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        expected_resumed: bool,
        resumed: bool,
        expected_incarnation_id: str,
    ) -> SessionRecord:
        owner_id = _required_id(owner_id, "owner_id")
        session_id = _required_id(session_id, "session_id")
        incarnation = _incarnation(expected_incarnation_id)
        if not isinstance(expected_resumed, bool) or not isinstance(resumed, bool):
            raise RepositoryValidationError("session resumed states must be booleans")
        if resumed == expected_resumed:
            raise RepositoryValidationError("resumed must differ from expected_resumed")
        result = transaction.execute(
            """
            UPDATE web_session
            SET resumed = %s
            WHERE sid = %s AND user_id = %s AND resumed = %s AND incarnation_id = %s::uuid
            RETURNING sid, user_id, access_token_enc, refresh_token_enc,
                      interactive_anchor, hard_expires_at, last_refresh_at,
                      resumed, created_at, incarnation_id, issuing_issuer, issuing_client_id
            """,
            (resumed, session_id, owner_id, expected_resumed, incarnation),
        )
        row = _optional_returned(result, "session.mark_resumed")
        if row is not None:
            return _session(row)
        existing = self.get(
            transaction,
            owner_id=owner_id,
            session_id=session_id,
        )
        if existing is None:
            raise RepositoryNotFoundError("owner-scoped web session was not found")
        if existing.incarnation_id == incarnation and existing.resumed == resumed:
            return existing
        raise RepositoryConflictError(
            "session resumed state compare-and-set fence is stale",
            metadata={"operation": "session.mark_resumed"},
        )

    def delete(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        expected_incarnation_id: str,
    ) -> bool:
        owner_id = _required_id(owner_id, "owner_id")
        session_id = _required_id(session_id, "session_id")
        incarnation = _incarnation(expected_incarnation_id)
        result = transaction.execute(
            "DELETE FROM web_session WHERE sid = %s AND user_id = %s AND incarnation_id = %s::uuid",
            (session_id, owner_id, incarnation),
        )
        return result.rowcount == 1

    def delete_and_return(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        expected_incarnation_id: str,
    ) -> SessionRecord | None:
        owner = _required_id(owner_id, "owner_id")
        session = _required_id(session_id, "session_id")
        incarnation = _incarnation(expected_incarnation_id)
        row = transaction.fetch_one(
            """DELETE FROM web_session WHERE sid = %s AND user_id = %s
               AND incarnation_id = %s::uuid
               RETURNING sid, user_id, access_token_enc, refresh_token_enc,
                         interactive_anchor, hard_expires_at, last_refresh_at,
                         resumed, created_at, incarnation_id, issuing_issuer, issuing_client_id""",
            (session, owner, incarnation),
        )
        return None if row is None else _session(row)

    def delete_owner(self, transaction: Transaction, *, owner_id: str) -> int:
        owner_id = _required_id(owner_id, "owner_id")
        result = transaction.execute(
            "DELETE FROM web_session WHERE user_id = %s",
            (owner_id,),
        )
        if result.rowcount < 0:
            raise PlaneError(
                "session owner deletion returned an invalid row count",
                code="session_owner_delete_invalid",
                metadata={"owner_id": owner_id},
            )
        return result.rowcount

    def retire_all_for_recovery(self, transaction: Transaction) -> int:
        self.bound_request_execution_waits(transaction)
        if not dialect_of(transaction).is_sqlite:
            transaction.execute("LOCK TABLE web_session IN ACCESS EXCLUSIVE MODE")
        result = transaction.execute("DELETE FROM web_session")
        if type(result.rowcount) is not int or result.rowcount < 0:
            raise PlaneError(
                "restored session retirement returned an invalid count",
                code="session_retirement_invalid",
            )
        sqlite = dialect_of(transaction).is_sqlite
        remaining = transaction.fetch_one(
            "SELECT count(*) AS remaining FROM web_session"
            if sqlite
            else "SELECT EXISTS(SELECT 1 FROM web_session) AS remaining"
        )
        complete = remaining is not None and (
            type(remaining["remaining"]) is int and remaining["remaining"] == 0
            if sqlite
            else remaining["remaining"] is False
        )
        if not complete:
            raise PlaneError(
                "restored session retirement was incomplete", code="session_retirement_incomplete"
            )
        return result.rowcount

    def delete_expired_for_administration(
        self,
        transaction: Transaction,
        *,
        observed_at: int,
    ) -> int:
        observed_at = _non_negative_int(observed_at, "observed_at")
        result = transaction.execute(
            "DELETE FROM web_session WHERE hard_expires_at <= %s",
            (observed_at,),
        )
        if result.rowcount < 0:
            raise PlaneError(
                "expired session deletion returned an invalid row count",
                code="session_expired_delete_invalid",
            )
        return result.rowcount


class HistoryRepository:
    def __init__(self) -> None:
        self.conversations = ConversationRepository()
        self.messages = MessageRepository()
        self.sessions = SessionRepository()


__all__ = (
    "ConversationBranchHeadRecord",
    "ConversationRecord",
    "ConversationRepository",
    "ConversationSummaryRecord",
    "FrameworkCredentialExecutionState",
    "FrameworkCredentialFence",
    "FrameworkCredentialObservation",
    "HistoryRepository",
    "MessageRecord",
    "MessageRepository",
    "SessionConsentObservation",
    "SessionCredentialFence",
    "SessionExecutionObservation",
    "SessionExecutionState",
    "SessionRecord",
    "SessionRepository",
)
