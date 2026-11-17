"""PostgreSQL-backed coordination for reconciliation.py: holds a session advisory lock
on one dedicated connection while marker transitions commit independently, since a
transaction-scoped lock would release on each commit.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from typing import Any, Final

from astralplane.contracts import (
    ReconciliationHookIdentity,
    ReconciliationMarker,
    ReconciliationMarkerState,
)
from astralplane.database.pool import ConnectionPool
from astralplane.database.revision import validate_revision
from astralplane.database.sql import execute_native
from astralplane.errors import ReconciliationError
from astralplane.reconciliation import RECONCILIATION_ADVISORY_LOCK

_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_LOCK_SQL: Final = "SELECT pg_advisory_lock(%s, %s)"
_UNLOCK_SQL: Final = "SELECT pg_advisory_unlock(%s, %s) AS unlocked"
_MARKER_COLUMNS: Final = (
    "schema_revision",
    "plan_digest",
    "hook_name",
    "hook_version",
    "state",
    "attempt",
    "result_digest",
    "error_type",
)
_MARKER_PROJECTION: Final = ", ".join(_MARKER_COLUMNS)
_GET_MARKER_SQL: Final = f"""
SELECT {_MARKER_PROJECTION}
FROM astralplane_reconciliation_marker
WHERE schema_revision = %s
  AND plan_digest = %s
  AND hook_name = %s
  AND hook_version = %s
""".strip()
_MARK_STARTED_SQL: Final = f"""
INSERT INTO astralplane_reconciliation_marker (
    schema_revision,
    plan_digest,
    hook_name,
    hook_version,
    state,
    attempt,
    result_digest,
    error_type,
    updated_at
)
VALUES (%s, %s, %s, %s, 'started', 1, NULL, NULL, clock_timestamp())
ON CONFLICT (schema_revision, plan_digest, hook_name, hook_version)
DO UPDATE SET
    state = 'started',
    attempt = astralplane_reconciliation_marker.attempt + 1,
    result_digest = NULL,
    error_type = NULL,
    updated_at = clock_timestamp()
WHERE astralplane_reconciliation_marker.result_digest IS NULL
  AND (
      (
          astralplane_reconciliation_marker.state = 'started'
          AND astralplane_reconciliation_marker.error_type IS NULL
      )
      OR (
          astralplane_reconciliation_marker.state = 'failed'
          AND astralplane_reconciliation_marker.error_type
              ~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$'
      )
  )
RETURNING {_MARKER_PROJECTION}
""".strip()
_MARK_COMPLETED_SQL: Final = f"""
UPDATE astralplane_reconciliation_marker
SET state = 'completed',
    result_digest = %s,
    error_type = NULL,
    updated_at = clock_timestamp()
WHERE schema_revision = %s
  AND plan_digest = %s
  AND hook_name = %s
  AND hook_version = %s
  AND state = 'started'
  AND result_digest IS NULL
  AND error_type IS NULL
RETURNING {_MARKER_PROJECTION}
""".strip()
_MARK_FAILED_SQL: Final = f"""
UPDATE astralplane_reconciliation_marker
SET state = 'failed',
    result_digest = NULL,
    error_type = %s,
    updated_at = clock_timestamp()
WHERE schema_revision = %s
  AND plan_digest = %s
  AND hook_name = %s
  AND hook_version = %s
  AND state = 'started'
  AND result_digest IS NULL
  AND error_type IS NULL
RETURNING {_MARKER_PROJECTION}
""".strip()


def _validate_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReconciliationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _validate_hook(hook: object) -> ReconciliationHookIdentity:
    if not isinstance(hook, ReconciliationHookIdentity):
        raise ReconciliationError("reconciliation hook identity is invalid")
    if (
        not isinstance(hook.name, str)
        or _SAFE_IDENTITY.fullmatch(hook.name) is None
        or not isinstance(hook.version, str)
        or _SAFE_IDENTITY.fullmatch(hook.version) is None
    ):
        raise ReconciliationError(
            "reconciliation hook name and version must be bounded identifiers"
        )
    return hook


def _column_names(description: object) -> tuple[str, ...]:
    if description is None:
        return ()
    names: list[str] = []
    for column in description:  # type: ignore[union-attr]
        name = getattr(column, "name", None)
        if name is None:
            name = column[0]
        names.append(str(name))
    return tuple(names)


def _row_mapping(row: object, description: object) -> dict[str, Any]:
    if isinstance(row, Mapping):
        values = dict(row)
    else:
        names = _column_names(description)
        try:
            items = tuple(row)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ReconciliationError("reconciliation store returned a non-record row") from exc
        if len(names) != len(items):
            raise ReconciliationError("reconciliation marker row width is invalid")
        values = dict(zip(names, items, strict=True))
    if set(values) != set(_MARKER_COLUMNS):
        raise ReconciliationError("reconciliation marker row columns are invalid")
    return values


def _marker_from_row(row: object, description: object) -> ReconciliationMarker:
    values = _row_mapping(row, description)
    schema_revision = values["schema_revision"]
    plan_digest = values["plan_digest"]
    hook_name = values["hook_name"]
    hook_version = values["hook_version"]
    state_value = values["state"]
    attempt = values["attempt"]
    result_digest = values["result_digest"]
    error_type = values["error_type"]

    if not isinstance(schema_revision, str):
        raise ReconciliationError("reconciliation marker schema revision is invalid")
    validate_revision(schema_revision, field="reconciliation marker schema revision")
    _validate_digest(plan_digest, field="reconciliation marker plan digest")
    hook = _validate_hook(
        ReconciliationHookIdentity(name=hook_name, version=hook_version)
        if isinstance(hook_name, str) and isinstance(hook_version, str)
        else None
    )
    try:
        state = ReconciliationMarkerState(state_value)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError("reconciliation marker state is invalid") from exc
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ReconciliationError("reconciliation marker attempt must be positive")
    if result_digest is not None and not isinstance(result_digest, str):
        raise ReconciliationError("reconciliation marker result digest is invalid")
    if error_type is not None and not isinstance(error_type, str):
        raise ReconciliationError("reconciliation marker error type is invalid")

    marker = ReconciliationMarker(
        schema_revision=schema_revision,
        plan_digest=plan_digest,
        hook=hook,
        state=state,
        attempt=attempt,
        result_digest=result_digest,
        error_type=error_type,
    )
    if state is ReconciliationMarkerState.STARTED:
        if result_digest is not None or error_type is not None:
            raise ReconciliationError("started reconciliation marker is malformed")
    elif state is ReconciliationMarkerState.COMPLETED:
        if result_digest is None or _SHA256.fullmatch(result_digest) is None:
            raise ReconciliationError("completed reconciliation marker is malformed")
        if error_type is not None:
            raise ReconciliationError("completed reconciliation marker carries a failure")
    elif (
        result_digest is not None
        or error_type is None
        or _SAFE_IDENTITY.fullmatch(error_type) is None
    ):
        raise ReconciliationError("failed reconciliation marker is malformed")
    return marker


class PostgresReconciliationSession:
    def __init__(self, connection: Any, *, schema_revision: str, plan_digest: str) -> None:
        self._connection = connection
        self._schema_revision = validate_revision(
            schema_revision, field="reconciliation schema revision"
        )
        self._plan_digest = _validate_digest(plan_digest, field="reconciliation plan digest")
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise ReconciliationError("reconciliation session is no longer active")

    def _deactivate(self) -> None:
        self._active = False

    def _discard(self) -> None:
        self._deactivate()
        with suppress(BaseException):
            self._connection.close()

    def _parameters(self, hook: ReconciliationHookIdentity) -> tuple[str, str, str, str]:
        exact = _validate_hook(hook)
        return self._schema_revision, self._plan_digest, exact.name, exact.version

    def _rollback_after_failure(self) -> None:
        try:
            self._connection.rollback()
        except BaseException:
            self._discard()

    def _read_row(
        self,
        statement: str,
        parameters: tuple[object, ...],
        *,
        action: str,
    ) -> tuple[object | None, object]:
        self._require_active()
        cursor: Any | None = None
        try:
            cursor = self._connection.cursor()
            execute_native(cursor, statement, parameters)
            row = cursor.fetchone()
            description = cursor.description
            cursor.close()
            cursor = None
            return row, description
        except BaseException as exc:
            if cursor is not None:
                with suppress(BaseException):
                    cursor.close()
            self._rollback_after_failure()
            if isinstance(exc, (KeyboardInterrupt, SystemExit, ReconciliationError)):
                raise
            raise ReconciliationError(f"reconciliation marker {action} failed") from exc

    def _commit_marker(
        self,
        statement: str,
        parameters: tuple[object, ...],
        *,
        hook: ReconciliationHookIdentity,
        expected_state: ReconciliationMarkerState,
        action: str,
    ) -> ReconciliationMarker:
        row, description = self._read_row(statement, parameters, action=action)
        if row is None:
            self._rollback_after_failure()
            raise ReconciliationError(
                f"reconciliation marker cannot transition to {expected_state.value}",
                metadata={"hook": hook.name, "version": hook.version},
            )
        try:
            marker = _marker_from_row(row, description)
            if (
                marker.schema_revision != self._schema_revision
                or marker.plan_digest != self._plan_digest
                or marker.hook != hook
                or marker.state is not expected_state
            ):
                raise ReconciliationError(
                    "reconciliation marker transition did not preserve its exact identity"
                )
        except BaseException as exc:
            self._rollback_after_failure()
            if isinstance(exc, (KeyboardInterrupt, SystemExit, ReconciliationError)):
                raise
            raise ReconciliationError(
                f"reconciliation marker {action} returned invalid data"
            ) from exc
        try:
            self._connection.commit()
        except BaseException as exc:
            # Commit outcome is uncertain; close session to fail closed
            self._rollback_after_failure()
            self._discard()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ReconciliationError(
                f"reconciliation marker {action} could not be committed durably"
            ) from exc
        return marker

    def get_marker(self, hook: ReconciliationHookIdentity) -> ReconciliationMarker | None:
        parameters = self._parameters(hook)
        row, description = self._read_row(_GET_MARKER_SQL, parameters, action="read")
        try:
            marker = None if row is None else _marker_from_row(row, description)
            if marker is not None and (
                marker.schema_revision != self._schema_revision
                or marker.plan_digest != self._plan_digest
                or marker.hook != hook
            ):
                raise ReconciliationError(
                    "reconciliation marker read did not preserve its exact identity"
                )
        except BaseException:
            self._rollback_after_failure()
            raise
        try:
            self._connection.rollback()
        except BaseException as exc:
            self._discard()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ReconciliationError("reconciliation marker read could not be closed") from exc
        return marker

    def mark_started(self, hook: ReconciliationHookIdentity) -> ReconciliationMarker:
        parameters = self._parameters(hook)
        return self._commit_marker(
            _MARK_STARTED_SQL,
            parameters,
            hook=hook,
            expected_state=ReconciliationMarkerState.STARTED,
            action="start",
        )

    def mark_completed(
        self,
        hook: ReconciliationHookIdentity,
        *,
        result_digest: str,
    ) -> ReconciliationMarker:
        exact_digest = _validate_digest(result_digest, field="reconciliation result digest")
        parameters = (exact_digest, *self._parameters(hook))
        return self._commit_marker(
            _MARK_COMPLETED_SQL,
            parameters,
            hook=hook,
            expected_state=ReconciliationMarkerState.COMPLETED,
            action="completion",
        )

    def mark_failed(
        self,
        hook: ReconciliationHookIdentity,
        *,
        error_type: str,
    ) -> ReconciliationMarker:
        if not isinstance(error_type, str) or _SAFE_IDENTITY.fullmatch(error_type) is None:
            raise ReconciliationError("reconciliation error type must be a bounded identifier")
        parameters = (error_type, *self._parameters(hook))
        return self._commit_marker(
            _MARK_FAILED_SQL,
            parameters,
            hook=hook,
            expected_state=ReconciliationMarkerState.FAILED,
            action="failure",
        )


class PostgresReconciliationCoordinator:
    def __init__(self, pool: ConnectionPool) -> None:
        if not isinstance(pool, ConnectionPool):
            raise TypeError("pool must be an AstralPlane ConnectionPool")
        self._pool = pool

    @staticmethod
    def _discard(connection: Any) -> None:
        with suppress(BaseException):
            connection.close()

    @staticmethod
    def _acquire(connection: Any, advisory_lock: tuple[int, int]) -> None:
        cursor: Any | None = None
        try:
            cursor = connection.cursor()
            execute_native(cursor, _LOCK_SQL, advisory_lock)
            if cursor.fetchone() is None:
                raise ReconciliationError("PostgreSQL did not confirm advisory lock acquisition")
            cursor.close()
            cursor = None
            connection.commit()
        except BaseException as exc:
            if cursor is not None:
                with suppress(BaseException):
                    cursor.close()
            with suppress(BaseException):
                connection.rollback()
            PostgresReconciliationCoordinator._discard(connection)
            if isinstance(exc, (KeyboardInterrupt, SystemExit, ReconciliationError)):
                raise
            raise ReconciliationError("reconciliation advisory lock acquisition failed") from exc

    @staticmethod
    def _release(connection: Any, advisory_lock: tuple[int, int]) -> None:
        cursor: Any | None = None
        try:
            cursor = connection.cursor()
            execute_native(cursor, _UNLOCK_SQL, advisory_lock)
            row = cursor.fetchone()
            description = cursor.description
            cursor.close()
            cursor = None
            if isinstance(row, Mapping):
                unlocked = row.get("unlocked")
            else:
                names = _column_names(description)
                values = () if row is None else tuple(row)
                unlocked = values[0] if names == ("unlocked",) and len(values) == 1 else None
            if unlocked is not True:
                raise ReconciliationError("PostgreSQL did not release the advisory lock")
            connection.commit()
        except BaseException as exc:
            if cursor is not None:
                with suppress(BaseException):
                    cursor.close()
            with suppress(BaseException):
                connection.rollback()
            PostgresReconciliationCoordinator._discard(connection)
            if isinstance(exc, (KeyboardInterrupt, SystemExit, ReconciliationError)):
                raise
            raise ReconciliationError("reconciliation advisory lock release failed") from exc

    @contextmanager
    def coordinate(
        self,
        *,
        advisory_lock: tuple[int, int],
        schema_revision: str,
        plan_digest: str,
    ) -> Iterator[PostgresReconciliationSession]:
        if advisory_lock != RECONCILIATION_ADVISORY_LOCK:
            raise ReconciliationError(
                "reconciliation coordinator requires the canonical advisory lock"
            )
        revision = validate_revision(schema_revision, field="reconciliation schema revision")
        digest = _validate_digest(plan_digest, field="reconciliation plan digest")

        with self._pool.connection() as connection:
            self._acquire(connection, advisory_lock)
            session: PostgresReconciliationSession | None = None
            body_failed = False
            try:
                session = PostgresReconciliationSession(
                    connection,
                    schema_revision=revision,
                    plan_digest=digest,
                )
                yield session
            except BaseException:
                body_failed = True
                raise
            finally:
                if session is not None:
                    session._deactivate()
                try:
                    self._release(connection, advisory_lock)
                except BaseException:
                    if not body_failed:
                        raise


__all__ = (
    "PostgresReconciliationCoordinator",
    "PostgresReconciliationSession",
)
