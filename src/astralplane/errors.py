"""Typed PlaneError hierarchy (pool, transaction, schema, migration, reconciliation,
domain, and repository-conflict errors) carrying only bounded, non-sensitive
diagnostic metadata, raised across astralplane and its many AstralDeep callers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from heapq import nsmallest
from typing import Final

from psycopg2.errors import LockNotAvailable

MAX_METADATA_ENTRIES: Final = 16
MAX_METADATA_KEY_LENGTH: Final = 64
MAX_METADATA_VALUE_LENGTH: Final = 512
MAX_CODE_LENGTH: Final = 64
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*", re.ASCII)
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_UNSUPPORTED = "<unsupported>"


def _diagnostic_value(value: object) -> str:
    if type(value) is str:
        text = value[:MAX_METADATA_VALUE_LENGTH]
    elif value is None or type(value) in (bool, int, float):
        if type(value) is int and value.bit_length() > 1700:
            return _UNSUPPORTED
        text = str(value)
    else:
        return _UNSUPPORTED
    return _CONTROLS.sub(lambda match: f"\\x{ord(match.group()):02x}", text)[
        :MAX_METADATA_VALUE_LENGTH
    ]


def _bounded_metadata(metadata: Mapping[str, object] | None) -> tuple[tuple[str, str], ...]:
    if metadata is None:
        return ()
    if not isinstance(metadata, Mapping):
        raise TypeError("PlaneError metadata must be a Mapping or None")
    for key in metadata:
        if type(key) is not str:
            raise TypeError("PlaneError metadata keys must be strings")
        if len(key) > MAX_METADATA_KEY_LENGTH:
            raise ValueError("PlaneError metadata key exceeds its maximum length")
        if _IDENTIFIER.fullmatch(key) is None:
            raise ValueError("PlaneError metadata key contains unsupported characters")
    return tuple((key, _diagnostic_value(metadata[key]))
                 for key in nsmallest(MAX_METADATA_ENTRIES, metadata))


def _resolve_code(code: str | None, default: str) -> str:
    if code is not None and type(code) is not str:
        raise TypeError("PlaneError code must be a string")
    selected = code or default
    if type(selected) is not str:
        raise TypeError("PlaneError code must be a string")
    if len(selected) > MAX_CODE_LENGTH:
        raise ValueError("PlaneError code exceeds its maximum length")
    if _IDENTIFIER.fullmatch(selected) is None:
        raise ValueError("PlaneError code contains unsupported characters")
    return selected


class PlaneError(RuntimeError):
    default_code: Final = "plane_error"
    discard_connection: Final = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = _resolve_code(code, self.default_code)
        self.metadata = _bounded_metadata(metadata)


class PoolClosedError(PlaneError):
    default_code = "pool_closed"


class PoolCheckoutError(PlaneError):
    default_code = "pool_checkout_failed"


class PoolInUseError(PlaneError):
    default_code = "pool_in_use"


class ConnectionResetError(PlaneError):
    default_code = "connection_reset_failed"


class PoolReleaseError(PlaneError):
    default_code = "pool_release_failed"


class PoolShutdownError(PlaneError):
    default_code = "pool_shutdown_failed"


class TransactionStateError(PlaneError):
    default_code = "transaction_state"


class TransactionStatementError(TransactionStateError):
    default_code = "transaction_statement_failed"
    discard_connection = True


class TransactionCleanupError(TransactionStateError):
    default_code = "transaction_cleanup_failed"
    discard_connection = True


class TransactionCommitError(PlaneError):
    default_code = "transaction_commit_failed"


class TransactionCommitOutcomeUnknownError(TransactionCommitError):
    default_code = "transaction_commit_outcome_unknown"
    discard_connection = True


class TransactionRollbackError(TransactionCleanupError):
    default_code = "transaction_rollback_failed"


class SQLContractError(PlaneError, ValueError):
    default_code = "sql_contract"


class SchemaRevisionError(PlaneError):
    default_code = "schema_revision_incompatible"


class MigrationDefinitionError(PlaneError, ValueError):
    default_code = "migration_definition"


class InitializationError(PlaneError):
    default_code = "initialization_failed"


class ReconciliationError(PlaneError):
    default_code = "reconciliation_failed"


class DomainValidationError(PlaneError, ValueError):
    default_code = "domain_validation"


class RepositoryError(PlaneError):
    default_code = "repository_error"


class RepositoryConflictError(RepositoryError):
    default_code = "repository_conflict"


class LockContentionError(RepositoryConflictError):
    default_code = "repository_busy"


def is_lock_contention(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (LockContentionError, LockNotAvailable)):
            return True
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return False
