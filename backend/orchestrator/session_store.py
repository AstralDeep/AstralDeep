"""Read-only access to the Plane session rows that earlier operations were bound to.
It reads fences and incarnations only, holds no key, mints nothing and refreshes nothing, so no stored credential is ever returned.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID

from astralplane.repositories.history import (
    SessionExecutionObservation,
    SessionExecutionState,
    SessionRepository,
)
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)


class SessionStoreError(Exception):
    pass


class SessionRefreshUnavailable(SessionStoreError):
    pass


def _valid_incarnation(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class WebSessionReference:
    state: SessionExecutionState = field(repr=False)


class WebSessionStore:
    def __init__(self, db=None, *, plane_runtime=None, plane_repositories=None,
                 session_context: PlaneRepositoryContext | None = None):
        if session_context is None:
            history, runtime = repository_from(
                "history", plane_runtime=plane_runtime, repositories=plane_repositories,
                legacy_database=db)
            if runtime is None:
                raise ValueError("an initialized Plane runtime is required")
            session_context = PlaneRepositoryContext(
                repository=history.sessions, plane_runtime=runtime)
        self._sessions = session_context

    def is_current_incarnation(self, owner_id: str, *, session_id: str, incarnation_id: str) -> bool:
        if not _valid_incarnation(incarnation_id):
            return False
        with self._request_execution_transaction() as transaction:
            record = self._sessions.repository.get_by_incarnation(
                transaction, owner_id=owner_id, incarnation_id=incarnation_id)
        return bool(record is not None and record.session_id == session_id
                    and record.hard_expires_at > int(time.time()))

    @contextmanager
    def _request_execution_transaction(self):
        try:
            with self._sessions.transaction() as transaction:
                self._sessions.repository.bound_request_execution_waits(transaction)
                yield transaction
        except Exception:
            raise SessionRefreshUnavailable("session execution database unavailable") from None

    def capture_execution_reference(self, *, owner_id: str, session_id: str) -> WebSessionReference:
        with self._request_execution_transaction() as transaction:
            state = self._sessions.repository.get_execution_state(
                transaction, owner_id=owner_id, session_id=session_id)
        if state is None:
            raise SessionRefreshUnavailable("session unavailable")
        return WebSessionReference(state)

    def capture_incarnation_execution_reference(
        self, *, owner_id: str, incarnation_id: str,
    ) -> WebSessionReference:
        if not _valid_incarnation(incarnation_id):
            raise SessionRefreshUnavailable("issued session required")
        with self._request_execution_transaction() as transaction:
            record = self._sessions.repository.get_by_incarnation(
                transaction, owner_id=owner_id, incarnation_id=incarnation_id)
            if record is None or record.incarnation_id != incarnation_id:
                raise SessionRefreshUnavailable("issued session unavailable")
            state = self._sessions.repository.get_execution_state(
                transaction, owner_id=owner_id, session_id=record.session_id)
            if (state is None or state.credential.incarnation_id != incarnation_id
                    or state.credential != SessionRepository.execution_fence(record)):
                raise SessionRefreshUnavailable("issued session changed during capture")
        return WebSessionReference(state)

    def assert_execution_observation(self, observation: SessionExecutionObservation) -> None:
        with self._request_execution_transaction() as transaction:
            self._sessions.repository.assert_current_execution(
                transaction, observation=observation)
