"""Lets an owner mint durable, independently-lived bearer tokens for their own external
tooling (SDK/MCP/A2A callers) without sharing their session; Plane persists only a
hash and display prefix, never the token.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Optional

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.framework_credentials import (
    FRAMEWORK_CREDENTIAL_SCOPES,
    FrameworkCredentialRecord,
)
from astralplane.repositories.history import (
    FrameworkCredentialFence,
    FrameworkCredentialObservation,
    SessionConsentObservation,
)

from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from
from persistent_agents.models import AssignmentError

_TOKEN_PREFIX = "afk_"
_OBSERVATION_WINDOW = timedelta(seconds=15)
_MIN_TTL_SECONDS = 300
_MAX_TTL_SECONDS = 90 * 86400
_NAME_MAX = 128
_UUID4_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_OWNER_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-]{1,700}")
_MAX_TOKEN_LENGTH = 2048


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FrameworkCaller:
    owner_id: str
    credential_id: str
    scopes: frozenset[str]
    _token_hash: str = field(repr=False)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def _encode_owner(owner_id: str) -> str:
    return base64.urlsafe_b64encode(owner_id.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_owner(segment: str) -> Optional[str]:
    if not isinstance(segment, str) or not _OWNER_SEGMENT_RE.fullmatch(segment):
        return None
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _validate_name(name: object) -> str:
    if not isinstance(name, str):
        raise AssignmentError("framework_credential_invalid", 422)
    trimmed = name.strip()
    if not trimmed or len(trimmed) > _NAME_MAX:
        raise AssignmentError("framework_credential_invalid", 422)
    return trimmed


def _validate_requested_scopes(scopes: object) -> tuple[str, ...]:
    if not isinstance(scopes, (list, tuple, set, frozenset)) or not scopes:
        raise AssignmentError("framework_credential_invalid", 422)
    ordered: list[str] = []
    for scope in scopes:
        if not isinstance(scope, str) or scope not in FRAMEWORK_CREDENTIAL_SCOPES:
            raise AssignmentError("framework_credential_invalid", 422)
        if scope not in ordered:
            ordered.append(scope)
    return tuple(sorted(ordered))


def _validate_ttl(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (
        _MIN_TTL_SECONDS <= value <= _MAX_TTL_SECONDS
    ):
        raise AssignmentError("framework_credential_invalid", 422)
    return value


def _validate_admissions(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
        raise AssignmentError("framework_credential_invalid", 422)
    return value


def _view_row(record: FrameworkCredentialRecord) -> dict:
    now = _now().timestamp()
    return {
        "credential_id": record.credential_id,
        "revision": record.consumed_admissions + 1,
        "name": record.name,
        "prefix": record.token_prefix,
        "scopes": sorted(record.scopes),
        "created_at": datetime.fromtimestamp(record.created_at, UTC).isoformat(),
        "expires_at": datetime.fromtimestamp(record.expires_at, UTC).isoformat(),
        "consumed_admissions": record.consumed_admissions,
        "max_admissions": record.max_admissions,
        "revoked": record.revoked_at is not None,
        "expired": record.expires_at <= now,
    }


class FrameworkCredentialService:
    def __init__(
        self,
        *,
        plane_runtime=None,
        plane_repositories=None,
        legacy_database=None,
        sessions=None,
        audit=None,
    ) -> None:
        from audit.repository import AuditRepository

        if not isinstance(audit, AuditRepository):
            raise ValueError("FrameworkCredentialService requires a real AuditRepository")
        repository, runtime = repository_from(
            "framework_credentials",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=legacy_database,
        )
        self._credentials = PlaneRepositoryContext(
            repository=repository, plane_runtime=runtime, legacy_database=legacy_database,
        )
        self._sessions = sessions if sessions is not None else runtime.repositories.history.sessions
        self._audit = audit

    @property
    def plane_runtime(self):
        return self._credentials.plane_runtime

    def issue(
        self,
        *,
        owner_id: str,
        caller: SessionConsentObservation,
        name: str,
        scopes,
        expires_in_seconds: int,
        max_admissions: int,
    ) -> tuple[dict, str]:
        if not isinstance(owner_id, str) or not owner_id:
            raise AssignmentError("framework_credential_authority_required", 401)
        if not isinstance(caller, SessionConsentObservation) or caller.credential.owner_id != owner_id:
            raise AssignmentError("framework_credential_authority_required", 401)
        display_name = _validate_name(name)
        scope_tuple = _validate_requested_scopes(scopes)
        ttl = _validate_ttl(expires_in_seconds)
        limit = _validate_admissions(max_admissions)

        credential_id = str(uuid.uuid4())
        secret = secrets.token_urlsafe(32)
        token = f"{_TOKEN_PREFIX}{_encode_owner(owner_id)}.{credential_id}.{secret}"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        token_prefix = f"{_TOKEN_PREFIX}{secret[:10]}"

        def _mint(transaction):
            self._sessions.assert_current_consent(transaction, observation=caller)
            record = self._credentials.repository.issue(
                transaction,
                owner_id=owner_id,
                credential_id=credential_id,
                name=display_name,
                scopes=scope_tuple,
                token_hash=token_hash,
                token_prefix=token_prefix,
                issuer_kind="session_incarnation",
                issuer_reference=caller.credential.incarnation_id,
                max_admissions=limit,
                ttl_seconds=ttl,
            )
            self._audit_event(
                transaction,
                owner_id=owner_id,
                action_type="auth.framework_issue",
                description="Issued a framework credential",
                outputs_meta={
                    "credential_id": record.credential_id,
                    "scopes": list(record.scopes),
                    "max_admissions": record.max_admissions,
                },
            )
            self._sessions.assert_current_consent(transaction, observation=caller)
            return record

        try:
            with self._credentials.transaction() as transaction:
                record = _mint(transaction)
        except RepositoryConflictError as exc:
            raise AssignmentError("framework_credential_authority_unavailable", 409) from exc
        except RepositoryValidationError as exc:
            raise AssignmentError("framework_credential_invalid", 422) from exc
        return _view_row(record), token

    def revoke(self, *, owner_id: str, credential_id: str) -> dict:
        if not isinstance(owner_id, str) or not owner_id:
            raise AssignmentError("framework_credential_authority_required", 401)
        if not isinstance(credential_id, str) or not credential_id:
            raise AssignmentError("framework_credential_not_found", 404)

        def _do(transaction):
            record = self._credentials.repository.revoke(
                transaction, owner_id=owner_id, credential_id=credential_id,
            )
            self._audit_event(
                transaction,
                owner_id=owner_id,
                action_type="auth.framework_revoke",
                description="Revoked a framework credential",
                outputs_meta={"credential_id": record.credential_id},
            )
            return record

        try:
            with self._credentials.transaction() as transaction:
                record = _do(transaction)
        except RepositoryNotFoundError as exc:
            raise AssignmentError("framework_credential_not_found", 404) from exc
        except RepositoryConflictError as exc:
            raise AssignmentError("framework_credential_authority_unavailable", 409) from exc
        except RepositoryValidationError as exc:
            raise AssignmentError("framework_credential_invalid", 422) from exc
        return _view_row(record)

    def list(self, *, owner_id: str) -> list[dict]:
        if not isinstance(owner_id, str) or not owner_id:
            raise AssignmentError("framework_credential_authority_required", 401)
        records = self._credentials.call(
            self._credentials.repository.list_for_owner, owner_id=owner_id,
        )
        return [_view_row(record) for record in records]

    def resolve_bearer(self, token: object) -> Optional[FrameworkCaller]:
        parsed = self._parse_token(token)
        if parsed is None:
            return None
        owner_id, credential_id, token_hash = parsed
        try:
            records = self._credentials.call(
                self._credentials.repository.list_for_owner, owner_id=owner_id,
            )
        except (RepositoryConflictError, RepositoryValidationError, RepositoryNotFoundError):
            return None
        record = next((r for r in records if r.credential_id == credential_id), None)
        if record is None:
            return None
        try:
            fence = FrameworkCredentialFence(
                owner_id=owner_id,
                credential_id=credential_id,
                token_hash=token_hash,
                scopes=tuple(sorted(record.scopes)),
                max_admissions=record.max_admissions,
                consumed_admissions=record.consumed_admissions,
                created_at=record.created_at,
                expires_at=record.expires_at,
                revoked_at=record.revoked_at,
            )
        except (ValueError, TypeError):
            return None
        now = _now()
        observation = FrameworkCredentialObservation(fence, now, now + _OBSERVATION_WINDOW)

        def _check(transaction):
            return self._credentials.repository.assert_current_execution(
                transaction, observation=observation,
            )

        try:
            with self._credentials.transaction() as transaction:
                _check(transaction)
        except (RepositoryConflictError, RepositoryNotFoundError, RepositoryValidationError,
                ValueError, TypeError):
            return None
        return FrameworkCaller(
            owner_id=owner_id, credential_id=credential_id, scopes=frozenset(record.scopes),
            _token_hash=token_hash,
        )

    def consume_admission(self, transaction, *, owner_id: str, credential_id: str):
        try:
            return self._credentials.repository.consume_admission(
                transaction, owner_id=owner_id, credential_id=credential_id,
            )
        except RepositoryConflictError as exc:
            raise AssignmentError("credential_allowance_exhausted", 409) from exc

    def assert_execution(self, transaction, observation: FrameworkCredentialObservation):
        try:
            return self._credentials.repository.assert_current_execution(
                transaction, observation=observation,
            )
        except (RepositoryConflictError, RepositoryNotFoundError, RepositoryValidationError) as exc:
            raise AssignmentError("framework_credential_authority_unavailable", 409) from exc

    def fresh_observation(self, caller: FrameworkCaller):
        if not isinstance(caller, FrameworkCaller):
            return None
        try:
            records = self._credentials.call(
                self._credentials.repository.list_for_owner, owner_id=caller.owner_id,
            )
        except (RepositoryConflictError, RepositoryValidationError, RepositoryNotFoundError):
            return None
        record = next((r for r in records if r.credential_id == caller.credential_id), None)
        if record is None:
            return None
        try:
            fence = FrameworkCredentialFence(
                owner_id=caller.owner_id,
                credential_id=caller.credential_id,
                token_hash=caller._token_hash,
                scopes=tuple(sorted(record.scopes)),
                max_admissions=record.max_admissions,
                consumed_admissions=record.consumed_admissions,
                created_at=record.created_at,
                expires_at=record.expires_at,
                revoked_at=record.revoked_at,
            )
        except (ValueError, TypeError):
            return None
        now = _now()
        return FrameworkCredentialObservation(fence, now, now + _OBSERVATION_WINDOW)

    def _parse_token(self, token: object) -> Optional[tuple[str, str, str]]:
        if (not isinstance(token, str) or not token.startswith(_TOKEN_PREFIX)
                or len(token) > _MAX_TOKEN_LENGTH):
            return None
        body = token[len(_TOKEN_PREFIX):]
        parts = body.split(".")
        if len(parts) != 3:
            return None
        owner_segment, credential_id, secret = parts
        if not secret or len(secret) < 16:
            return None
        owner_id = _decode_owner(owner_segment)
        if owner_id is None or _UUID4_RE.fullmatch(credential_id) is None:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return owner_id, credential_id, token_hash

    def _audit_event(self, transaction, *, owner_id, action_type, description, outputs_meta) -> None:
        from audit.schemas import AuditEventCreate

        now = _now()
        event = AuditEventCreate(
            actor_user_id=owner_id,
            auth_principal=owner_id,
            event_class="auth",
            action_type=action_type,
            description=description,
            correlation_id=outputs_meta.get("credential_id"),
            outcome="success",
            outputs_meta=outputs_meta,
            started_at=now,
            completed_at=now,
        )
        self._audit.insert_in_transaction(event, transaction=transaction, plane_runtime=self.plane_runtime)


__all__ = ["FrameworkCaller", "FrameworkCredentialService"]
