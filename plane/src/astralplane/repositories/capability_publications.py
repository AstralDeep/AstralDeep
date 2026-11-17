"""Owner-scoped capability-publication records select which approved capability revision
is exposed to one audience for one lifetime. Transitions use caller transactions,
state-revision fences, and expiry-aware reads; discovery, IAM, invocation, and transport
remain host decisions, and Plane stores only opaque ciphertext plus its host digest."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from astralplane.contracts import Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _required_id,
)

_MAX_REVISION: Final = 2**63 - 1
_MAX_METADATA_CIPHERTEXT: Final = 65_536
_MAX_LIST: Final = 200
_FIELDS: Final = (
    "owner_id, capability_id, audience_id, capability_revision, "
    "metadata_ciphertext, metadata_digest, publication_state, state_revision, "
    "published_at, expires_at, ended_at, end_reason"
)


class CapabilityPublicationState(StrEnum):
    PUBLISHED = "published"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


class CapabilityPublicationConflictError(RepositoryConflictError):
    default_code = "capability_publication_conflict"


class CapabilityPublicationNotExpiredError(CapabilityPublicationConflictError):
    default_code = "capability_publication_not_expired"


@dataclass(frozen=True, slots=True)
class CapabilityPublicationRecord:
    owner_id: str
    capability_id: str
    audience_id: str
    capability_revision: str
    metadata_ciphertext: str
    metadata_digest: str
    state: CapabilityPublicationState
    state_revision: int
    published_at: datetime
    expires_at: datetime
    ended_at: datetime | None
    end_reason: str | None


@dataclass(frozen=True, slots=True)
class _PublicationIntent:
    capability_revision: str
    metadata_ciphertext: str
    metadata_digest: str
    published_at: datetime
    expires_at: datetime


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise RepositoryValidationError(f"{field} must be a timezone-aware UTC datetime")
    return value.astimezone(UTC)


def _row_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryDataError(f"stored {field} is not a timezone-aware timestamp")
    return value.astimezone(UTC)


def _revision(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_REVISION:
        raise RepositoryValidationError(f"{field} must be a bounded non-negative integer")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise RepositoryValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _reason(value: object) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, "reason", maximum=280, allow_empty=True)


def _row_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RepositoryDataError(f"stored {field} is not non-empty text")
    return value


def _record(row) -> CapabilityPublicationRecord:
    try:
        state = CapabilityPublicationState(row["publication_state"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDataError("stored capability publication state is invalid") from exc
    try:
        state_revision = row["state_revision"]
        ended_at = row["ended_at"]
        end_reason = row["end_reason"]
        record = CapabilityPublicationRecord(
            owner_id=_row_text(row["owner_id"], "owner_id"),
            capability_id=_row_text(row["capability_id"], "capability_id"),
            audience_id=_row_text(row["audience_id"], "audience_id"),
            capability_revision=_row_text(row["capability_revision"], "capability_revision"),
            metadata_ciphertext=_row_text(
                row["metadata_ciphertext"],
                "metadata_ciphertext",
            ),
            metadata_digest=_row_text(row["metadata_digest"], "metadata_digest"),
            state=state,
            state_revision=state_revision,
            published_at=_row_time(row["published_at"], "published_at"),
            expires_at=_row_time(row["expires_at"], "expires_at"),
            ended_at=None if ended_at is None else _row_time(ended_at, "ended_at"),
            end_reason=end_reason,
        )
    except KeyError as exc:
        raise RepositoryDataError("stored capability publication row is incomplete") from exc
    if type(record.state_revision) is not int or not 1 <= record.state_revision <= _MAX_REVISION:
        raise RepositoryDataError("stored capability publication revision is invalid")
    if record.end_reason is not None and not isinstance(record.end_reason, str):
        raise RepositoryDataError("stored capability publication reason is invalid")
    return record


def _same_intent(current: CapabilityPublicationRecord, intent: _PublicationIntent) -> bool:
    return (
        current.capability_revision,
        current.metadata_ciphertext,
        current.metadata_digest,
        current.published_at,
        current.expires_at,
    ) == (
        intent.capability_revision,
        intent.metadata_ciphertext,
        intent.metadata_digest,
        intent.published_at,
        intent.expires_at,
    )


def _same_withdrawal(
    current: CapabilityPublicationRecord,
    at: datetime,
    reason: str | None,
) -> bool:
    return current.ended_at == at and current.end_reason == reason


def _replay_revision(current_revision: int, expected_revision: int) -> bool:
    return expected_revision in (current_revision, current_revision - 1)


class CapabilityPublicationRepository:
    @staticmethod
    def _locked(
        transaction: Transaction,
        *,
        owner_id: str,
        capability_id: str,
        audience_id: str,
    ):
        return transaction.fetch_one(
            f"SELECT {_FIELDS} FROM capability_publication "
            "WHERE owner_id = %s AND capability_id = %s AND audience_id = %s "
            "FOR UPDATE",
            (owner_id, capability_id, audience_id),
        )

    def get(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        capability_id: str,
        audience_id: str,
        for_update: bool = False,
    ) -> CapabilityPublicationRecord | None:
        owner = _required_id(owner_id, "owner_id")
        capability = _required_id(capability_id, "capability_id")
        audience = _required_id(audience_id, "audience_id")
        if type(for_update) is not bool:
            raise RepositoryValidationError("for_update must be a boolean")
        if for_update:
            row = self._locked(
                transaction,
                owner_id=owner,
                capability_id=capability,
                audience_id=audience,
            )
        else:
            row = transaction.fetch_one(
                f"SELECT {_FIELDS} FROM capability_publication "
                "WHERE owner_id = %s AND capability_id = %s AND audience_id = %s",
                (owner, capability, audience),
            )
        return None if row is None else _record(row)

    def get_active(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        capability_id: str,
        audience_id: str,
        at: datetime,
        for_update: bool = False,
    ) -> CapabilityPublicationRecord | None:
        owner = _required_id(owner_id, "owner_id")
        capability = _required_id(capability_id, "capability_id")
        audience = _required_id(audience_id, "audience_id")
        observed_at = _time(at, "at")
        if type(for_update) is not bool:
            raise RepositoryValidationError("for_update must be a boolean")
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            f"SELECT {_FIELDS} FROM capability_publication "
            "WHERE owner_id = %s AND capability_id = %s AND audience_id = %s "
            "AND publication_state = 'published' "
            f"AND published_at <= %s AND expires_at > %s{lock}",
            (owner, capability, audience, observed_at, observed_at),
        )
        return None if row is None else _record(row)

    def list_active(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        at: datetime,
        limit: int,
    ) -> tuple[CapabilityPublicationRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        observed_at = _time(at, "at")
        bound = _bounded_limit(limit, maximum=_MAX_LIST)
        rows = transaction.fetch_all(
            f"SELECT {_FIELDS} FROM capability_publication "
            "WHERE owner_id = %s AND publication_state = 'published' "
            "AND published_at <= %s AND expires_at > %s "
            "ORDER BY capability_id, audience_id LIMIT %s",
            (owner, observed_at, observed_at, bound),
        )
        return tuple(_record(row) for row in rows)

    def publish(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        capability_id: str,
        audience_id: str,
        capability_revision: str,
        metadata_ciphertext: str,
        metadata_digest: str,
        published_at: datetime,
        expires_at: datetime,
        expected_revision: int,
    ) -> CapabilityPublicationRecord:
        owner = _required_id(owner_id, "owner_id")
        capability = _required_id(capability_id, "capability_id")
        audience = _required_id(audience_id, "audience_id")
        intent = _PublicationIntent(
            capability_revision=_required_id(capability_revision, "capability_revision"),
            metadata_ciphertext=_bounded_text(
                metadata_ciphertext,
                "metadata_ciphertext",
                maximum=_MAX_METADATA_CIPHERTEXT,
            ),
            metadata_digest=_digest(metadata_digest, "metadata_digest"),
            published_at=_time(published_at, "published_at"),
            expires_at=_time(expires_at, "expires_at"),
        )
        if intent.expires_at <= intent.published_at:
            raise RepositoryValidationError("expires_at must be later than published_at")
        revision = _revision(expected_revision, "expected_revision")
        row = self._locked(
            transaction,
            owner_id=owner,
            capability_id=capability,
            audience_id=audience,
        )
        if row is None:
            if revision != 0:
                raise CapabilityPublicationConflictError(
                    "capability publication is absent at the requested revision"
                )
            inserted = transaction.fetch_one(
                "INSERT INTO capability_publication "
                "(owner_id, capability_id, audience_id, capability_revision, "
                "metadata_ciphertext, metadata_digest, publication_state, state_revision, "
                "published_at, expires_at, ended_at, end_reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'published', 1, %s, %s, NULL, NULL) "
                "ON CONFLICT (owner_id, capability_id, audience_id) DO NOTHING "
                f"RETURNING {_FIELDS}",
                (
                    owner,
                    capability,
                    audience,
                    intent.capability_revision,
                    intent.metadata_ciphertext,
                    intent.metadata_digest,
                    intent.published_at,
                    intent.expires_at,
                ),
            )
            if inserted is not None:
                return _record(inserted)
            row = self._locked(
                transaction,
                owner_id=owner,
                capability_id=capability,
                audience_id=audience,
            )
            if row is None:
                raise RepositoryDataError("capability publication insert lost its row")
        current = _record(row)
        if (
            current.state is CapabilityPublicationState.PUBLISHED
            and _same_intent(current, intent)
            and _replay_revision(current.state_revision, revision)
        ):
            return current
        if current.state_revision != revision:
            raise CapabilityPublicationConflictError(
                "capability publication revision is stale"
            )
        if current.state is CapabilityPublicationState.PUBLISHED:
            raise CapabilityPublicationConflictError(
                "capability publication is already published"
            )
        if current.state_revision >= _MAX_REVISION:
            raise CapabilityPublicationConflictError(
                "capability publication revision is exhausted"
            )
        changed = transaction.fetch_one(
            "UPDATE capability_publication SET "
            "capability_revision = %s, metadata_ciphertext = %s, metadata_digest = %s, "
            "publication_state = 'published', state_revision = state_revision + 1, "
            "published_at = %s, expires_at = %s, ended_at = NULL, end_reason = NULL "
            "WHERE owner_id = %s AND capability_id = %s AND audience_id = %s "
            "AND state_revision = %s "
            "AND publication_state IN ('withdrawn', 'expired') "
            f"RETURNING {_FIELDS}",
            (
                intent.capability_revision,
                intent.metadata_ciphertext,
                intent.metadata_digest,
                intent.published_at,
                intent.expires_at,
                owner,
                capability,
                audience,
                revision,
            ),
        )
        if changed is None:
            raise CapabilityPublicationConflictError(
                "capability publication publish fence was rejected"
            )
        return _record(changed)

    def withdraw(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        capability_id: str,
        audience_id: str,
        expected_revision: int,
        at: datetime,
        reason: str | None = None,
    ) -> CapabilityPublicationRecord:
        owner = _required_id(owner_id, "owner_id")
        capability = _required_id(capability_id, "capability_id")
        audience = _required_id(audience_id, "audience_id")
        revision = _revision(expected_revision, "expected_revision")
        withdrawn_at = _time(at, "at")
        withdrawn_reason = _reason(reason)
        row = self._locked(
            transaction,
            owner_id=owner,
            capability_id=capability,
            audience_id=audience,
        )
        if row is None:
            raise RepositoryNotFoundError("owner-scoped capability publication was not found")
        current = _record(row)
        if (
            current.state is CapabilityPublicationState.WITHDRAWN
            and _same_withdrawal(current, withdrawn_at, withdrawn_reason)
            and _replay_revision(current.state_revision, revision)
        ):
            return current
        if current.state_revision != revision:
            raise CapabilityPublicationConflictError(
                "capability publication revision is stale"
            )
        if current.state is not CapabilityPublicationState.PUBLISHED:
            raise CapabilityPublicationConflictError(
                "only a published capability can be withdrawn"
            )
        if withdrawn_at < current.published_at:
            raise CapabilityPublicationConflictError(
                "withdrawal predates the capability publication"
            )
        if current.state_revision >= _MAX_REVISION:
            raise CapabilityPublicationConflictError(
                "capability publication revision is exhausted"
            )
        changed = transaction.fetch_one(
            "UPDATE capability_publication SET publication_state = 'withdrawn', "
            "state_revision = state_revision + 1, ended_at = %s, end_reason = %s "
            "WHERE owner_id = %s AND capability_id = %s AND audience_id = %s "
            "AND state_revision = %s AND publication_state = 'published' "
            f"RETURNING {_FIELDS}",
            (withdrawn_at, withdrawn_reason, owner, capability, audience, revision),
        )
        if changed is None:
            raise CapabilityPublicationConflictError(
                "capability publication withdraw fence was rejected"
            )
        return _record(changed)

    def expire(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        capability_id: str,
        audience_id: str,
        expected_revision: int,
        at: datetime,
    ) -> CapabilityPublicationRecord:
        owner = _required_id(owner_id, "owner_id")
        capability = _required_id(capability_id, "capability_id")
        audience = _required_id(audience_id, "audience_id")
        revision = _revision(expected_revision, "expected_revision")
        expired_at = _time(at, "at")
        row = self._locked(
            transaction,
            owner_id=owner,
            capability_id=capability,
            audience_id=audience,
        )
        if row is None:
            raise RepositoryNotFoundError("owner-scoped capability publication was not found")
        current = _record(row)
        if (
            current.state is CapabilityPublicationState.EXPIRED
            and current.ended_at == expired_at
            and _replay_revision(current.state_revision, revision)
        ):
            return current
        if current.state_revision != revision:
            raise CapabilityPublicationConflictError(
                "capability publication revision is stale"
            )
        if current.state is not CapabilityPublicationState.PUBLISHED:
            raise CapabilityPublicationConflictError(
                "only a published capability can expire"
            )
        if expired_at < current.expires_at:
            raise CapabilityPublicationNotExpiredError(
                "capability publication lifetime has not elapsed"
            )
        if current.state_revision >= _MAX_REVISION:
            raise CapabilityPublicationConflictError(
                "capability publication revision is exhausted"
            )
        changed = transaction.fetch_one(
            "UPDATE capability_publication SET publication_state = 'expired', "
            "state_revision = state_revision + 1, ended_at = %s, end_reason = NULL "
            "WHERE owner_id = %s AND capability_id = %s AND audience_id = %s "
            "AND state_revision = %s AND publication_state = 'published' "
            f"RETURNING {_FIELDS}",
            (expired_at, owner, capability, audience, revision),
        )
        if changed is None:
            raise CapabilityPublicationConflictError(
                "capability publication expiry fence was rejected"
            )
        return _record(changed)


__all__ = (
    "CapabilityPublicationConflictError",
    "CapabilityPublicationNotExpiredError",
    "CapabilityPublicationRecord",
    "CapabilityPublicationRepository",
    "CapabilityPublicationState",
)