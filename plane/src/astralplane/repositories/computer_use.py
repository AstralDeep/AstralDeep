"""Owner-scoped Mac computer-use durable bookkeeping: session records, task grants,
one-way host effect intents, and approval-continuation fences for Deep recovery.
All writes carry owner predicates and revision fences; only digests and bounded
identifiers are stored.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

from astralplane.contracts import Record, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_VERB = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_INTEGER = 9_007_199_254_740_991

SESSION_STATES = ("active", "paused", "ended")
FENCE_STATES = ("open", "consumed", "released")


class HostEffectIntentState(StrEnum):
    PENDING = "pending"
    COMMITTED = "committed"
    UNKNOWN = "unknown"
    ACKNOWLEDGED = "acknowledged"


def _owner(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise RepositoryValidationError("owner_id must be a non-empty string of at most 512")
    return value


def _uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RepositoryValidationError(f"{field} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise RepositoryValidationError(f"{field} must be a UUID string") from exc
    if str(parsed) != value or parsed.int == 0:
        raise RepositoryValidationError(f"{field} must be a canonical non-nil UUID")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise RepositoryValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _bounded(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RepositoryValidationError(f"{field} must be a non-empty string of at most {maximum}")
    return value


def _millis(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_INTEGER:
        raise RepositoryValidationError(f"{field} must be a non-negative integer timestamp")
    return value


def _revision(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_INTEGER:
        raise RepositoryValidationError(f"{field} must be a positive integer fence")
    return value


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise RepositoryValidationError("limit must be an integer between 1 and 1000")
    return value


@dataclass(frozen=True, slots=True)
class ComputerSession:
    session_id: str
    owner_id: str
    host_id: str
    host_incarnation: str
    state: str
    revision: int
    created_at: int
    updated_at: int
    pause_reason: str | None = None
    ended_at: int | None = None

    def __post_init__(self) -> None:
        _uuid(self.session_id, "session_id")
        _owner(self.owner_id)
        _bounded(self.host_id, "host_id", 256)
        _bounded(self.host_incarnation, "host_incarnation", 128)
        if self.state not in SESSION_STATES:
            raise RepositoryValidationError("computer session state is not supported")
        _revision(self.revision, "revision")
        _millis(self.created_at, "created_at")
        _millis(self.updated_at, "updated_at")
        if self.created_at > self.updated_at:
            raise RepositoryValidationError("updated_at precedes created_at")
        if self.state == "paused":
            _bounded(self.pause_reason, "pause_reason", 64)
        elif self.pause_reason is not None:
            raise RepositoryValidationError("only paused sessions carry a pause reason")
        if self.state == "ended":
            _millis(self.ended_at, "ended_at")
            if self.ended_at < self.created_at:
                raise RepositoryValidationError("ended_at precedes created_at")
        elif self.ended_at is not None:
            raise RepositoryValidationError("only ended sessions carry ended_at")


@dataclass(frozen=True, slots=True)
class ComputerTaskGrant:
    grant_id: str
    owner_id: str
    binding_digest: str
    expires_at: int
    hard_cap: int
    use_count: int
    revision: int
    created_at: int
    revoked_at: int | None = None

    def __post_init__(self) -> None:
        _uuid(self.grant_id, "grant_id")
        _owner(self.owner_id)
        _digest(self.binding_digest, "binding_digest")
        _millis(self.expires_at, "expires_at")
        if (
            isinstance(self.hard_cap, bool)
            or not isinstance(self.hard_cap, int)
            or not 1 <= self.hard_cap <= _MAX_INTEGER
        ):
            raise RepositoryValidationError("hard_cap must be a positive integer")
        if (
            isinstance(self.use_count, bool)
            or not isinstance(self.use_count, int)
            or not 0 <= self.use_count <= self.hard_cap
        ):
            raise RepositoryValidationError("use_count must stay within hard_cap")
        _revision(self.revision, "revision")
        _millis(self.created_at, "created_at")
        if self.created_at > self.expires_at:
            raise RepositoryValidationError("expires_at precedes created_at")
        if self.revoked_at is not None:
            _millis(self.revoked_at, "revoked_at")
            if self.revoked_at < self.created_at:
                raise RepositoryValidationError("revoked_at precedes created_at")

    @property
    def exhausted(self) -> bool:
        return self.use_count >= self.hard_cap


@dataclass(frozen=True, slots=True)
class HostEffectIntent:
    operation_id: str
    owner_id: str
    verb: str
    argument_digest: str
    state: HostEffectIntentState
    created_at: int
    updated_at: int
    resolved_at: int | None = None
    acknowledged_at: int | None = None

    def __post_init__(self) -> None:
        _uuid(self.operation_id, "operation_id")
        _owner(self.owner_id)
        _bounded(self.verb, "verb", 64)
        if _VERB.fullmatch(self.verb) is None:
            raise RepositoryValidationError("verb must be bounded snake_case")
        _digest(self.argument_digest, "argument_digest")
        if not isinstance(self.state, HostEffectIntentState):
            raise RepositoryValidationError("host effect intent state is not supported")
        _millis(self.created_at, "created_at")
        _millis(self.updated_at, "updated_at")
        if self.created_at > self.updated_at:
            raise RepositoryValidationError("updated_at precedes created_at")
        if self.state is HostEffectIntentState.PENDING:
            if self.resolved_at is not None or self.acknowledged_at is not None:
                raise RepositoryValidationError("pending intents carry no resolution times")
            return
        _millis(self.resolved_at, "resolved_at")
        if self.resolved_at < self.created_at:
            raise RepositoryValidationError("resolved_at precedes created_at")
        if self.state is HostEffectIntentState.ACKNOWLEDGED:
            _millis(self.acknowledged_at, "acknowledged_at")
            if self.acknowledged_at < (self.resolved_at or 0):
                raise RepositoryValidationError("acknowledged_at precedes resolved_at")
        elif self.acknowledged_at is not None:
            raise RepositoryValidationError("only acknowledged intents carry acknowledged_at")


@dataclass(frozen=True, slots=True)
class ApprovalFence:
    fence_id: str
    owner_id: str
    continuation_digest: str
    state: str
    created_at: int
    updated_at: int
    closed_at: int | None = None

    def __post_init__(self) -> None:
        _uuid(self.fence_id, "fence_id")
        _owner(self.owner_id)
        _digest(self.continuation_digest, "continuation_digest")
        if self.state not in FENCE_STATES:
            raise RepositoryValidationError("approval fence state is not supported")
        _millis(self.created_at, "created_at")
        _millis(self.updated_at, "updated_at")
        if self.created_at > self.updated_at:
            raise RepositoryValidationError("updated_at precedes created_at")
        if self.state == "open":
            if self.closed_at is not None:
                raise RepositoryValidationError("open fences carry no closed_at")
            return
        _millis(self.closed_at, "closed_at")
        if self.closed_at < self.created_at:
            raise RepositoryValidationError("closed_at precedes created_at")


class ComputerUseRepository:
    def open_session(self, transaction: Transaction, session: ComputerSession) -> ComputerSession:
        row = transaction.fetch_one(
            """
            INSERT INTO computer_session (
                session_id, owner_id, host_id, host_incarnation, state,
                pause_reason, revision, created_at, updated_at, ended_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
            RETURNING *
            """,
            (
                session.session_id,
                session.owner_id,
                session.host_id,
                session.host_incarnation,
                session.state,
                session.pause_reason,
                session.revision,
                session.created_at,
                session.updated_at,
                session.ended_at,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM computer_session WHERE session_id = %s AND owner_id = %s",
                (session.session_id, session.owner_id),
            )
        if row is None:
            raise RepositoryConflictError(
                "computer session identity already exists for another owner",
                metadata={"operation": "open_session"},
            )
        existing = _session(row)
        if existing != session:
            raise RepositoryConflictError(
                "computer session identity has conflicting semantics",
                metadata={"operation": "open_session"},
            )
        return session

    def get_session(
        self, transaction: Transaction, *, owner_id: str, session_id: str
    ) -> ComputerSession | None:
        owner = _owner(owner_id)
        identifier = _uuid(session_id, "session_id")
        row = transaction.fetch_one(
            "SELECT * FROM computer_session WHERE session_id = %s AND owner_id = %s",
            (identifier, owner),
        )
        return None if row is None else _session(row)

    def list_open_sessions(
        self, transaction: Transaction, *, owner_id: str, limit: int = 200
    ) -> tuple[ComputerSession, ...]:
        owner = _owner(owner_id)
        bound = _limit(limit)
        rows = transaction.fetch_all(
            """
            SELECT * FROM computer_session
            WHERE owner_id = %s AND state <> 'ended'
            ORDER BY created_at, session_id
            LIMIT %s
            """,
            (owner, bound),
        )
        return tuple(_session(row) for row in rows)

    def pause_session(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        expected_revision: int,
        pause_reason: str,
        updated_at: int,
    ) -> ComputerSession:
        owner = _owner(owner_id)
        identifier = _uuid(session_id, "session_id")
        reason = _bounded(pause_reason, "pause_reason", 64)
        at = _millis(updated_at, "updated_at")
        _revision(expected_revision, "expected_revision")
        row = transaction.fetch_one(
            """
            UPDATE computer_session
            SET state = 'paused', pause_reason = %s, revision = revision + 1,
                updated_at = %s
            WHERE session_id = %s AND owner_id = %s AND revision = %s
              AND state = 'active'
            RETURNING *
            """,
            (reason, at, identifier, owner, expected_revision),
        )
        return _required_session(transaction, row, "pause_session", owner, identifier)

    def resume_session(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        expected_revision: int,
        updated_at: int,
    ) -> ComputerSession:
        owner = _owner(owner_id)
        identifier = _uuid(session_id, "session_id")
        at = _millis(updated_at, "updated_at")
        _revision(expected_revision, "expected_revision")
        row = transaction.fetch_one(
            """
            UPDATE computer_session
            SET state = 'active', pause_reason = NULL, revision = revision + 1,
                updated_at = %s
            WHERE session_id = %s AND owner_id = %s AND revision = %s
              AND state = 'paused'
            RETURNING *
            """,
            (at, identifier, owner, expected_revision),
        )
        return _required_session(transaction, row, "resume_session", owner, identifier)

    def end_session(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        expected_revision: int,
        ended_at: int,
    ) -> ComputerSession:
        owner = _owner(owner_id)
        identifier = _uuid(session_id, "session_id")
        at = _millis(ended_at, "ended_at")
        _revision(expected_revision, "expected_revision")
        row = transaction.fetch_one(
            """
            UPDATE computer_session
            SET state = 'ended', pause_reason = NULL, ended_at = %s,
                revision = revision + 1, updated_at = %s
            WHERE session_id = %s AND owner_id = %s AND revision = %s
              AND state IN ('active', 'paused')
            RETURNING *
            """,
            (at, at, identifier, owner, expected_revision),
        )
        return _required_session(transaction, row, "end_session", owner, identifier)

    def delete_ended_sessions_before(
        self, transaction: Transaction, *, owner_id: str, ended_before: int, limit: int = 200
    ) -> tuple[str, ...]:
        owner = _owner(owner_id)
        before = _millis(ended_before, "ended_before")
        bound = _limit(limit)
        rows = transaction.fetch_all(
            """
            DELETE FROM computer_session
            WHERE session_id IN (
                SELECT doomed.session_id FROM computer_session AS doomed
                WHERE doomed.owner_id = %s AND doomed.state = 'ended'
                  AND doomed.ended_at <= %s
                ORDER BY doomed.ended_at, doomed.session_id
                LIMIT %s
            )
            RETURNING session_id
            """,
            (owner, before, bound),
        )
        return tuple(str(row["session_id"]) for row in rows)

    def create_task_grant(
        self, transaction: Transaction, grant: ComputerTaskGrant
    ) -> ComputerTaskGrant:
        row = transaction.fetch_one(
            """
            INSERT INTO computer_task_grant (
                grant_id, owner_id, binding_digest, expires_at, hard_cap,
                use_count, revision, created_at, revoked_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (grant_id) DO NOTHING
            RETURNING *
            """,
            (
                grant.grant_id,
                grant.owner_id,
                grant.binding_digest,
                grant.expires_at,
                grant.hard_cap,
                grant.use_count,
                grant.revision,
                grant.created_at,
                grant.revoked_at,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM computer_task_grant WHERE grant_id = %s AND owner_id = %s",
                (grant.grant_id, grant.owner_id),
            )
        if row is None:
            raise RepositoryConflictError(
                "computer task grant identity already exists for another owner",
                metadata={"operation": "create_task_grant"},
            )
        existing = _grant(row)
        if existing != grant:
            raise RepositoryConflictError(
                "computer task grant identity has conflicting semantics",
                metadata={"operation": "create_task_grant"},
            )
        return grant

    def get_task_grant(
        self, transaction: Transaction, *, owner_id: str, grant_id: str
    ) -> ComputerTaskGrant | None:
        owner = _owner(owner_id)
        identifier = _uuid(grant_id, "grant_id")
        row = transaction.fetch_one(
            "SELECT * FROM computer_task_grant WHERE grant_id = %s AND owner_id = %s",
            (identifier, owner),
        )
        return None if row is None else _grant(row)

    def list_active_task_grants(
        self, transaction: Transaction, *, owner_id: str, now: int, limit: int = 200
    ) -> tuple[ComputerTaskGrant, ...]:
        owner = _owner(owner_id)
        at = _millis(now, "now")
        bound = _limit(limit)
        rows = transaction.fetch_all(
            """
            SELECT * FROM computer_task_grant
            WHERE owner_id = %s AND revoked_at IS NULL AND expires_at > %s
            ORDER BY created_at, grant_id
            LIMIT %s
            """,
            (owner, at, bound),
        )
        return tuple(_grant(row) for row in rows)

    def consume_task_grant(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        grant_id: str,
        expected_revision: int,
        now: int,
    ) -> ComputerTaskGrant:
        owner = _owner(owner_id)
        identifier = _uuid(grant_id, "grant_id")
        _revision(expected_revision, "expected_revision")
        at = _millis(now, "now")
        row = transaction.fetch_one(
            """
            UPDATE computer_task_grant
            SET use_count = use_count + 1, revision = revision + 1
            WHERE grant_id = %s AND owner_id = %s AND revision = %s
              AND revoked_at IS NULL AND expires_at > %s AND use_count < hard_cap
            RETURNING *
            """,
            (identifier, owner, expected_revision, at),
        )
        return _required_grant(transaction, row, "consume_task_grant", owner, identifier)

    def revoke_task_grant(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        grant_id: str,
        expected_revision: int,
        revoked_at: int,
    ) -> ComputerTaskGrant:
        owner = _owner(owner_id)
        identifier = _uuid(grant_id, "grant_id")
        _revision(expected_revision, "expected_revision")
        at = _millis(revoked_at, "revoked_at")
        row = transaction.fetch_one(
            """
            UPDATE computer_task_grant
            SET revoked_at = %s, revision = revision + 1
            WHERE grant_id = %s AND owner_id = %s AND revision = %s
              AND revoked_at IS NULL
            RETURNING *
            """,
            (at, identifier, owner, expected_revision),
        )
        return _required_grant(transaction, row, "revoke_task_grant", owner, identifier)

    def delete_expired_task_grants_before(
        self, transaction: Transaction, *, owner_id: str, expired_before: int, limit: int = 200
    ) -> tuple[str, ...]:
        owner = _owner(owner_id)
        before = _millis(expired_before, "expired_before")
        bound = _limit(limit)
        rows = transaction.fetch_all(
            """
            DELETE FROM computer_task_grant
            WHERE grant_id IN (
                SELECT doomed.grant_id FROM computer_task_grant AS doomed
                WHERE doomed.owner_id = %s
                  AND (doomed.revoked_at IS NOT NULL OR doomed.expires_at <= %s)
                ORDER BY doomed.expires_at, doomed.grant_id
                LIMIT %s
            )
            RETURNING grant_id
            """,
            (owner, before, bound),
        )
        return tuple(str(row["grant_id"]) for row in rows)

    def record_host_effect_intent(
        self, transaction: Transaction, intent: HostEffectIntent
    ) -> HostEffectIntent:
        row = transaction.fetch_one(
            """
            INSERT INTO computer_host_effect_intent (
                operation_id, owner_id, verb, argument_digest, state,
                created_at, updated_at, resolved_at, acknowledged_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (operation_id) DO NOTHING
            RETURNING *
            """,
            (
                intent.operation_id,
                intent.owner_id,
                intent.verb,
                intent.argument_digest,
                intent.state.value,
                intent.created_at,
                intent.updated_at,
                intent.resolved_at,
                intent.acknowledged_at,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                """
                SELECT * FROM computer_host_effect_intent
                WHERE operation_id = %s AND owner_id = %s
                """,
                (intent.operation_id, intent.owner_id),
            )
        if row is None:
            raise RepositoryConflictError(
                "host effect intent identity already exists for another owner",
                metadata={"operation": "record_host_effect_intent"},
            )
        existing = _intent(row)
        if existing != intent:
            raise RepositoryConflictError(
                "host effect intent identity has conflicting semantics",
                metadata={"operation": "record_host_effect_intent"},
            )
        return intent

    def get_host_effect_intent(
        self, transaction: Transaction, *, owner_id: str, operation_id: str
    ) -> HostEffectIntent | None:
        owner = _owner(owner_id)
        identifier = _uuid(operation_id, "operation_id")
        row = transaction.fetch_one(
            """
            SELECT * FROM computer_host_effect_intent
            WHERE operation_id = %s AND owner_id = %s
            """,
            (identifier, owner),
        )
        return None if row is None else _intent(row)

    def list_open_host_effect_intents(
        self, transaction: Transaction, *, owner_id: str, limit: int = 1000
    ) -> tuple[HostEffectIntent, ...]:
        owner = _owner(owner_id)
        bound = _limit(limit)
        rows = transaction.fetch_all(
            """
            SELECT * FROM computer_host_effect_intent
            WHERE owner_id = %s AND state = 'pending'
            ORDER BY created_at, operation_id
            LIMIT %s
            """,
            (owner, bound),
        )
        return tuple(_intent(row) for row in rows)

    def commit_host_effect_intent(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        operation_id: str,
        resolved_at: int,
    ) -> HostEffectIntent:
        owner = _owner(owner_id)
        identifier = _uuid(operation_id, "operation_id")
        at = _millis(resolved_at, "resolved_at")
        row = transaction.fetch_one(
            """
            UPDATE computer_host_effect_intent
            SET state = 'committed', resolved_at = %s, updated_at = %s
            WHERE operation_id = %s AND owner_id = %s AND state = 'pending'
            RETURNING *
            """,
            (at, at, identifier, owner),
        )
        if row is not None:
            return _intent(row)
        current = self.get_host_effect_intent(
            transaction, owner_id=owner, operation_id=identifier
        )
        if current is None:
            raise RepositoryNotFoundError(
                "owner-scoped host effect intent was not found",
                metadata={"operation": "commit_host_effect_intent"},
            )
        if current.state is HostEffectIntentState.COMMITTED:
            return current
        raise RepositoryConflictError(
            "host effect intent already left pending and cannot commit",
            metadata={"operation": "commit_host_effect_intent"},
        )

    def mark_host_effect_intent_unknown(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        operation_id: str,
        resolved_at: int,
    ) -> HostEffectIntent:
        owner = _owner(owner_id)
        identifier = _uuid(operation_id, "operation_id")
        at = _millis(resolved_at, "resolved_at")
        row = transaction.fetch_one(
            """
            UPDATE computer_host_effect_intent
            SET state = 'unknown', resolved_at = %s, updated_at = %s
            WHERE operation_id = %s AND owner_id = %s AND state = 'pending'
            RETURNING *
            """,
            (at, at, identifier, owner),
        )
        if row is not None:
            return _intent(row)
        current = self.get_host_effect_intent(
            transaction, owner_id=owner, operation_id=identifier
        )
        if current is None:
            raise RepositoryNotFoundError(
                "owner-scoped host effect intent was not found",
                metadata={"operation": "mark_host_effect_intent_unknown"},
            )
        if current.state is HostEffectIntentState.UNKNOWN:
            return current
        raise RepositoryConflictError(
            "host effect intent already left pending and cannot become unknown",
            metadata={"operation": "mark_host_effect_intent_unknown"},
        )

    def mark_host_effect_intents_unknown(
        self, transaction: Transaction, *, owner_id: str, resolved_at: int, limit: int = 1000
    ) -> tuple[HostEffectIntent, ...]:
        owner = _owner(owner_id)
        at = _millis(resolved_at, "resolved_at")
        bound = _limit(limit)
        rows = transaction.fetch_all(
            """
            UPDATE computer_host_effect_intent AS intent
            SET state = 'unknown', resolved_at = %s, updated_at = %s
            WHERE intent.owner_id = %s AND intent.state = 'pending'
              AND intent.operation_id IN (
                  SELECT pending.operation_id
                  FROM computer_host_effect_intent AS pending
                  WHERE pending.owner_id = %s AND pending.state = 'pending'
                  ORDER BY pending.created_at, pending.operation_id
                  LIMIT %s
              )
            RETURNING intent.*
            """,
            (at, at, owner, owner, bound),
        )
        return tuple(_intent(row) for row in rows)

    def acknowledge_host_effect_intent(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        operation_id: str,
        acknowledged_at: int,
    ) -> HostEffectIntent:
        owner = _owner(owner_id)
        identifier = _uuid(operation_id, "operation_id")
        at = _millis(acknowledged_at, "acknowledged_at")
        row = transaction.fetch_one(
            """
            UPDATE computer_host_effect_intent
            SET state = 'acknowledged', acknowledged_at = %s, updated_at = %s
            WHERE operation_id = %s AND owner_id = %s
              AND state IN ('committed', 'unknown')
            RETURNING *
            """,
            (at, at, identifier, owner),
        )
        if row is not None:
            return _intent(row)
        current = self.get_host_effect_intent(
            transaction, owner_id=owner, operation_id=identifier
        )
        if current is None:
            raise RepositoryNotFoundError(
                "owner-scoped host effect intent was not found",
                metadata={"operation": "acknowledge_host_effect_intent"},
            )
        if current.state is HostEffectIntentState.ACKNOWLEDGED:
            return current
        raise RepositoryConflictError(
            "pending host effect intents must resolve before acknowledgment",
            metadata={"operation": "acknowledge_host_effect_intent"},
        )

    def create_approval_fence(
        self, transaction: Transaction, fence: ApprovalFence
    ) -> ApprovalFence:
        row = transaction.fetch_one(
            """
            INSERT INTO computer_approval_fence (
                fence_id, owner_id, continuation_digest, state,
                created_at, updated_at, closed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fence_id) DO NOTHING
            RETURNING *
            """,
            (
                fence.fence_id,
                fence.owner_id,
                fence.continuation_digest,
                fence.state,
                fence.created_at,
                fence.updated_at,
                fence.closed_at,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM computer_approval_fence WHERE fence_id = %s AND owner_id = %s",
                (fence.fence_id, fence.owner_id),
            )
        if row is None:
            raise RepositoryConflictError(
                "approval fence identity already exists for another owner",
                metadata={"operation": "create_approval_fence"},
            )
        existing = _fence(row)
        if existing != fence:
            raise RepositoryConflictError(
                "approval fence identity has conflicting semantics",
                metadata={"operation": "create_approval_fence"},
            )
        return fence

    def get_approval_fence(
        self, transaction: Transaction, *, owner_id: str, fence_id: str
    ) -> ApprovalFence | None:
        owner = _owner(owner_id)
        identifier = _uuid(fence_id, "fence_id")
        row = transaction.fetch_one(
            "SELECT * FROM computer_approval_fence WHERE fence_id = %s AND owner_id = %s",
            (identifier, owner),
        )
        return None if row is None else _fence(row)

    def list_open_approval_fences(
        self, transaction: Transaction, *, owner_id: str, limit: int = 200
    ) -> tuple[ApprovalFence, ...]:
        owner = _owner(owner_id)
        bound = _limit(limit)
        rows = transaction.fetch_all(
            """
            SELECT * FROM computer_approval_fence
            WHERE owner_id = %s AND state = 'open'
            ORDER BY created_at, fence_id
            LIMIT %s
            """,
            (owner, bound),
        )
        return tuple(_fence(row) for row in rows)

    def consume_approval_fence(
        self, transaction: Transaction, *, owner_id: str, fence_id: str, closed_at: int
    ) -> ApprovalFence:
        return self._close_fence(
            transaction,
            owner_id=owner_id,
            fence_id=fence_id,
            closed_at=closed_at,
            target="consumed",
            operation="consume_approval_fence",
        )

    def release_approval_fence(
        self, transaction: Transaction, *, owner_id: str, fence_id: str, closed_at: int
    ) -> ApprovalFence:
        return self._close_fence(
            transaction,
            owner_id=owner_id,
            fence_id=fence_id,
            closed_at=closed_at,
            target="released",
            operation="release_approval_fence",
        )

    def _close_fence(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        fence_id: str,
        closed_at: int,
        target: str,
        operation: str,
    ) -> ApprovalFence:
        owner = _owner(owner_id)
        identifier = _uuid(fence_id, "fence_id")
        at = _millis(closed_at, "closed_at")
        row = transaction.fetch_one(
            """
            UPDATE computer_approval_fence
            SET state = %s, closed_at = %s, updated_at = %s
            WHERE fence_id = %s AND owner_id = %s AND state = 'open'
            RETURNING *
            """,
            (target, at, at, identifier, owner),
        )
        if row is not None:
            return _fence(row)
        current = self.get_approval_fence(transaction, owner_id=owner, fence_id=identifier)
        if current is None:
            raise RepositoryNotFoundError(
                "owner-scoped approval fence was not found",
                metadata={"operation": operation},
            )
        raise RepositoryConflictError(
            "approval fence already closed",
            metadata={"operation": operation},
        )


def _session(row: Record) -> ComputerSession:
    try:
        return ComputerSession(
            session_id=str(row["session_id"]),
            owner_id=str(row["owner_id"]),
            host_id=str(row["host_id"]),
            host_incarnation=str(row["host_incarnation"]),
            state=str(row["state"]),
            pause_reason=None if row.get("pause_reason") is None else str(row["pause_reason"]),
            revision=int(row["revision"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            ended_at=None if row.get("ended_at") is None else int(row["ended_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDataError("persisted computer session record is invalid") from exc


def _required_session(
    transaction: Transaction,
    row: Record | None,
    operation: str,
    owner_id: str,
    identifier: str,
) -> ComputerSession:
    if row is None:
        _raise_write_miss(transaction, operation, owner_id, identifier, "session_id")
    return _session(row)


def _grant(row: Record) -> ComputerTaskGrant:
    try:
        return ComputerTaskGrant(
            grant_id=str(row["grant_id"]),
            owner_id=str(row["owner_id"]),
            binding_digest=str(row["binding_digest"]),
            expires_at=int(row["expires_at"]),
            hard_cap=int(row["hard_cap"]),
            use_count=int(row["use_count"]),
            revision=int(row["revision"]),
            created_at=int(row["created_at"]),
            revoked_at=None if row.get("revoked_at") is None else int(row["revoked_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDataError("persisted computer task grant record is invalid") from exc


def _required_grant(
    transaction: Transaction,
    row: Record | None,
    operation: str,
    owner_id: str,
    identifier: str,
) -> ComputerTaskGrant:
    if row is None:
        _raise_write_miss(transaction, operation, owner_id, identifier, "grant_id")
    return _grant(row)


def _intent(row: Record) -> HostEffectIntent:
    try:
        return HostEffectIntent(
            operation_id=str(row["operation_id"]),
            owner_id=str(row["owner_id"]),
            verb=str(row["verb"]),
            argument_digest=str(row["argument_digest"]),
            state=HostEffectIntentState(str(row["state"])),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            resolved_at=None if row.get("resolved_at") is None else int(row["resolved_at"]),
            acknowledged_at=(
                None if row.get("acknowledged_at") is None else int(row["acknowledged_at"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDataError("persisted host effect intent record is invalid") from exc


def _fence(row: Record) -> ApprovalFence:
    try:
        return ApprovalFence(
            fence_id=str(row["fence_id"]),
            owner_id=str(row["owner_id"]),
            continuation_digest=str(row["continuation_digest"]),
            state=str(row["state"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            closed_at=None if row.get("closed_at") is None else int(row["closed_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDataError("persisted approval fence record is invalid") from exc


def _raise_write_miss(
    transaction: Transaction,
    operation: str,
    owner_id: str,
    identifier: str,
    field: str,
) -> None:
    table = "computer_session" if field == "session_id" else "computer_task_grant"
    existing = transaction.fetch_one(
        f"SELECT 1 AS present FROM {table} WHERE {field} = %s AND owner_id = %s",
        (identifier, owner_id),
    )
    if existing is None:
        raise RepositoryNotFoundError(
            "owner-scoped record was not found",
            metadata={"operation": operation},
        )
    raise RepositoryConflictError(
        "owner-scoped write lost its revision fence",
        metadata={"operation": operation},
    )
