"""Owner-scoped persistence for qualification test runs, evidence, and a hash-chained
review audit trail spanning legacy v1 and current v2 entry formats. Used by
AstralDeep's qual_audit/database.py.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _canonical_json,
    _required_id,
    _row_value,
    _single_returned,
    _structured_json,
)

_RUN_STATUSES = frozenset({"running", "completed", "failed"})
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed"})
_OUTCOMES = frozenset({"passed", "failed", "error", "skipped"})
_VERIFICATION_STATUSES = frozenset({"pending", "verified", "disputed", "needs_rerun"})
_AUDIT_ACTIONS = frozenset({"verified", "disputed", "needs_rerun"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
QUALITY_AUDIT_GENESIS_HASH = hashlib.sha256(b"genesis").hexdigest()


@dataclass(frozen=True, slots=True)
class QualityTestRunRecord:
    owner_id: str
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    system_state: Mapping[str, Any]
    categories: tuple[str, ...]
    status: str


@dataclass(frozen=True, slots=True)
class QualityTestCaseRecord:
    owner_id: str
    case_id: str
    run_id: str
    suite: str
    test_name: str
    outcome: str
    duration_ms: float
    metrics: Mapping[str, Any]
    qualitative: str
    evidence_hash: str
    verification_status: str


@dataclass(frozen=True, slots=True)
class QualityEvidenceRecord:
    owner_id: str
    evidence_id: str
    case_id: str
    evidence_type: str
    data: Mapping[str, Any]
    sha256: str
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class QualityAuditEntryRecord:
    owner_id: str
    entry_id: str
    case_id: str
    action: str
    reviewer: str
    rationale: str
    timestamp: datetime
    previous_hash: str
    hash_version: int = 2


@dataclass(frozen=True, slots=True)
class QualityCaseReviewResult:
    audit_entry: QualityAuditEntryRecord
    test_case: QualityTestCaseRecord


@dataclass(frozen=True, slots=True)
class QualityLatexArtifactRecord:
    owner_id: str
    artifact_id: str
    run_id: str
    filename: str
    generated_from: tuple[str, ...]
    verification_complete: bool
    generated_at: datetime


def _optional_returned(result: object, operation: str) -> Any:
    if not getattr(result, "returned_records", ()):
        return None
    return _single_returned(result, operation)


def _aware_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryValidationError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _stored_time(value: object, field: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise RepositoryDataError(
                "persisted qualification timestamp is invalid", metadata={"field": field}
            ) from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryDataError(
            "persisted qualification timestamp is not timezone-aware",
            metadata={"field": field},
        )
    return value.astimezone(UTC)


def _iso_time(value: object, field: str) -> str:
    return _aware_time(value, field).isoformat()


def _stored_mapping(value: object, field: str) -> Mapping[str, Any]:
    decoded = _structured_json(value, field)
    if not isinstance(decoded, Mapping):
        raise RepositoryDataError(
            "persisted qualification JSON has an invalid shape", metadata={"field": field}
        )
    return decoded


def _stored_strings(value: object, field: str) -> tuple[str, ...]:
    decoded = _structured_json(value, field)
    if not isinstance(decoded, tuple) or not all(isinstance(item, str) for item in decoded):
        raise RepositoryDataError(
            "persisted qualification string list has an invalid shape",
            metadata={"field": field},
        )
    return decoded


def _bounded_json(value: object, field: str, *, maximum: int) -> str:
    encoded = _canonical_json(value, field)
    if len(encoded.encode("utf-8")) > maximum:
        raise RepositoryValidationError(
            f"{field} exceeds its maximum serialized size",
            metadata={"field": field, "maximum": maximum},
        )
    return encoded


def _bounded_strings(values: Sequence[str], field: str, *, maximum: int) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RepositoryValidationError(f"{field} must be a sequence of strings")
    if len(values) > maximum:
        raise RepositoryValidationError(f"{field} contains too many entries")
    return tuple(_bounded_text(value, field, maximum=1024) for value in values)


def _finite_non_negative(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RepositoryValidationError(f"{field} must be a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise RepositoryValidationError(f"{field} must be a finite non-negative number")
    return number


def _digest(value: object, field: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise RepositoryValidationError(f"{field} must be a SHA-256 string")
    if allow_empty and value == "":
        return value
    if _SHA256.fullmatch(value) is None:
        raise RepositoryValidationError(f"{field} must be lowercase SHA-256")
    return value


def quality_audit_chain_hash(record: QualityAuditEntryRecord) -> str:
    if not isinstance(record, QualityAuditEntryRecord):
        raise RepositoryValidationError("record must be a QualityAuditEntryRecord")
    entry_id = _required_id(record.entry_id, "entry_id")
    if record.action not in _AUDIT_ACTIONS:
        raise RepositoryValidationError("qualification audit action is unsupported")
    timestamp = _aware_time(record.timestamp, "timestamp").isoformat()
    if record.hash_version == 1:
        payload = f"{entry_id}{record.action}{timestamp}".encode()
    elif record.hash_version == 2:
        payload = (
            "astralplane.quality-audit.chain.v2\0"
            + _canonical_json(
                {
                    "action": record.action,
                    "caseId": _required_id(record.case_id, "case_id"),
                    "entryId": entry_id,
                    "hashVersion": 2,
                    "ownerId": _required_id(record.owner_id, "owner_id"),
                    "previousHash": _digest(record.previous_hash, "previous_hash"),
                    "rationale": _bounded_text(
                        record.rationale,
                        "rationale",
                        maximum=65536,
                        allow_empty=True,
                    ),
                    "reviewer": _bounded_text(record.reviewer, "reviewer", maximum=1024),
                    "timestamp": timestamp,
                },
                "quality_audit_chain_record",
            )
        ).encode()
    else:
        raise RepositoryValidationError("qualification audit hash version is unsupported")
    return hashlib.sha256(payload).hexdigest()


def verify_quality_audit_chain(
    records: Sequence[QualityAuditEntryRecord],
    *,
    require_genesis: bool = True,
) -> bool:
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise RepositoryValidationError("records must be an ordered sequence")
    entries = tuple(records)
    if not entries:
        return True
    if not all(isinstance(entry, QualityAuditEntryRecord) for entry in entries):
        raise RepositoryValidationError("records must contain QualityAuditEntryRecord values")
    owner_id = entries[0].owner_id
    if require_genesis and entries[0].previous_hash != QUALITY_AUDIT_GENESIS_HASH:
        return False
    for previous, current in pairwise(entries):
        if current.owner_id != owner_id or current.timestamp <= previous.timestamp:
            return False
        if current.previous_hash != quality_audit_chain_hash(previous):
            return False
    return True


def _filename(value: object) -> str:
    name = _bounded_text(value, "filename", maximum=4096)
    if "\\" in name or ":" in name:
        raise RepositoryValidationError("filename must be a portable relative path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RepositoryValidationError("filename must be a portable relative path")
    return name


def _run(row: Mapping[str, Any]) -> QualityTestRunRecord:
    finished = row.get("finished_at")
    return QualityTestRunRecord(
        owner_id=str(_row_value(row, "owner_id")),
        run_id=str(_row_value(row, "id")),
        started_at=_stored_time(_row_value(row, "started_at"), "started_at"),
        finished_at=None if finished is None else _stored_time(finished, "finished_at"),
        system_state=_stored_mapping(_row_value(row, "system_state"), "system_state"),
        categories=_stored_strings(_row_value(row, "categories"), "categories"),
        status=str(_row_value(row, "status")),
    )


def _case(row: Mapping[str, Any]) -> QualityTestCaseRecord:
    duration = float(row.get("duration_ms") or 0.0)
    if not math.isfinite(duration) or duration < 0:
        raise RepositoryDataError("persisted qualification duration is invalid")
    return QualityTestCaseRecord(
        owner_id=str(_row_value(row, "owner_id")),
        case_id=str(_row_value(row, "id")),
        run_id=str(_row_value(row, "run_id")),
        suite=str(_row_value(row, "suite")),
        test_name=str(_row_value(row, "test_name")),
        outcome=str(_row_value(row, "outcome")),
        duration_ms=duration,
        metrics=_stored_mapping(row.get("metrics") or "{}", "metrics"),
        qualitative=str(row.get("qualitative") or ""),
        evidence_hash=str(row.get("evidence_hash") or ""),
        verification_status=str(_row_value(row, "verification_status")),
    )


def _evidence(row: Mapping[str, Any]) -> QualityEvidenceRecord:
    return QualityEvidenceRecord(
        owner_id=str(_row_value(row, "owner_id")),
        evidence_id=str(_row_value(row, "id")),
        case_id=str(_row_value(row, "case_id")),
        evidence_type=str(_row_value(row, "evidence_type")),
        data=_stored_mapping(_row_value(row, "data"), "data"),
        sha256=str(_row_value(row, "sha256")),
        captured_at=_stored_time(_row_value(row, "captured_at"), "captured_at"),
    )


def _audit_entry(row: Mapping[str, Any]) -> QualityAuditEntryRecord:
    try:
        hash_version = int(_row_value(row, "hash_version"))
    except (TypeError, ValueError) as exc:
        raise RepositoryDataError("persisted qualification hash version is invalid") from exc
    if hash_version not in {1, 2}:
        raise RepositoryDataError("persisted qualification hash version is unsupported")
    previous_hash = str(_row_value(row, "previous_hash"))
    try:
        _digest(previous_hash, "previous_hash")
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted qualification chain link is invalid") from exc
    return QualityAuditEntryRecord(
        owner_id=str(_row_value(row, "owner_id")),
        entry_id=str(_row_value(row, "id")),
        case_id=str(_row_value(row, "case_id")),
        action=str(_row_value(row, "action")),
        reviewer=str(_row_value(row, "reviewer")),
        rationale=str(row.get("rationale") or ""),
        timestamp=_stored_time(_row_value(row, "timestamp"), "timestamp"),
        previous_hash=previous_hash,
        hash_version=hash_version,
    )


def _artifact(row: Mapping[str, Any]) -> QualityLatexArtifactRecord:
    return QualityLatexArtifactRecord(
        owner_id=str(_row_value(row, "owner_id")),
        artifact_id=str(_row_value(row, "id")),
        run_id=str(_row_value(row, "run_id")),
        filename=str(_row_value(row, "filename")),
        generated_from=_stored_strings(_row_value(row, "generated_from"), "generated_from"),
        verification_complete=bool(_row_value(row, "verification_complete")),
        generated_at=_stored_time(_row_value(row, "generated_at"), "generated_at"),
    )


class QualityAuditRepository:
    _RUN_FIELDS = (
        "owner_id, id, started_at, finished_at, system_state, categories, status"
    )
    _CASE_FIELDS = """
        owner_id, id, run_id, suite, test_name, outcome, duration_ms, metrics,
        qualitative, evidence_hash, verification_status
    """
    _EVIDENCE_FIELDS = "owner_id, id, case_id, evidence_type, data, sha256, captured_at"
    _AUDIT_FIELDS = (
        "owner_id, id, case_id, action, reviewer, rationale, timestamp, previous_hash, "
        "hash_version"
    )
    _ARTIFACT_FIELDS = (
        "owner_id, id, run_id, filename, generated_from, verification_complete, generated_at"
    )

    def create_run(
        self, transaction: Transaction, record: QualityTestRunRecord
    ) -> QualityTestRunRecord:
        owner_id = _required_id(record.owner_id, "owner_id")
        run_id = _required_id(record.run_id, "run_id")
        if record.status not in _RUN_STATUSES:
            raise RepositoryValidationError("qualification run status is unsupported")
        started_at = _aware_time(record.started_at, "started_at")
        finished_at = (
            None
            if record.finished_at is None
            else _aware_time(record.finished_at, "finished_at")
        )
        if finished_at is not None and finished_at < started_at:
            raise RepositoryValidationError("qualification run cannot finish before it starts")
        if (record.status == "running") != (finished_at is None):
            raise RepositoryValidationError("qualification run status and finish time disagree")
        categories = _bounded_strings(record.categories, "categories", maximum=100)
        system_state = _bounded_json(dict(record.system_state), "system_state", maximum=65536)
        categories_json = _bounded_json(categories, "categories", maximum=16384)
        result = transaction.execute(
            f"""
            INSERT INTO test_runs (
                owner_id, id, started_at, finished_at, system_state, categories, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._RUN_FIELDS}
            """,
            (
                owner_id,
                run_id,
                started_at.isoformat(),
                None if finished_at is None else finished_at.isoformat(),
                system_state,
                categories_json,
                record.status,
            ),
        )
        row = _optional_returned(result, "quality_audit.create_run")
        if row is not None:
            return _run(row)
        existing = self.get_run(transaction, owner_id=owner_id, run_id=run_id)
        if existing is None:
            raise RepositoryConflictError(
                "qualification run identity is owned by another namespace",
                metadata={"operation": "quality_audit.create_run"},
            )
        if existing != record:
            raise RepositoryConflictError(
                "qualification run idempotency identity was reused with different semantics",
                metadata={"operation": "quality_audit.create_run"},
            )
        return existing

    def get_run(
        self, query: QueryExecutor, *, owner_id: str, run_id: str
    ) -> QualityTestRunRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        run_id = _required_id(run_id, "run_id")
        row = query.fetch_one(
            f"SELECT {self._RUN_FIELDS} FROM test_runs WHERE owner_id = %s AND id = %s",
            (owner_id, run_id),
        )
        return None if row is None else _run(row)

    def get_latest_run(
        self, query: QueryExecutor, *, owner_id: str
    ) -> QualityTestRunRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            f"""
            SELECT {self._RUN_FIELDS}
            FROM test_runs
            WHERE owner_id = %s
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (owner_id,),
        )
        return None if row is None else _run(row)

    def finish_run(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        run_id: str,
        status: str,
        finished_at: datetime,
        expected_status: str = "running",
    ) -> QualityTestRunRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        run_id = _required_id(run_id, "run_id")
        if status not in _TERMINAL_RUN_STATUSES:
            raise RepositoryValidationError("qualification terminal status is unsupported")
        if expected_status not in _RUN_STATUSES:
            raise RepositoryValidationError("qualification expected status is unsupported")
        finished = _iso_time(finished_at, "finished_at")
        result = transaction.execute(
            f"""
            UPDATE test_runs
            SET finished_at = %s, status = %s
            WHERE owner_id = %s AND id = %s AND status = %s
            RETURNING {self._RUN_FIELDS}
            """,
            (finished, status, owner_id, run_id, expected_status),
        )
        row = _optional_returned(result, "quality_audit.finish_run")
        if row is not None:
            finished_run = _run(row)
            if (
                finished_run.finished_at is not None
                and finished_run.finished_at < finished_run.started_at
            ):
                raise RepositoryDataError("qualification run finished before it started")
            return finished_run
        existing = self.get_run(transaction, owner_id=owner_id, run_id=run_id)
        if existing is None:
            return None
        raise RepositoryConflictError(
            "qualification run changed since it was read",
            metadata={"operation": "quality_audit.finish_run"},
        )

    def create_case(
        self, transaction: Transaction, record: QualityTestCaseRecord
    ) -> QualityTestCaseRecord:
        owner_id = _required_id(record.owner_id, "owner_id")
        case_id = _required_id(record.case_id, "case_id")
        run_id = _required_id(record.run_id, "run_id")
        suite = _bounded_text(record.suite, "suite", maximum=1024)
        test_name = _bounded_text(record.test_name, "test_name", maximum=4096)
        if record.outcome not in _OUTCOMES:
            raise RepositoryValidationError("qualification outcome is unsupported")
        if record.verification_status not in _VERIFICATION_STATUSES:
            raise RepositoryValidationError("verification status is unsupported")
        duration_ms = _finite_non_negative(record.duration_ms, "duration_ms")
        metrics = _bounded_json(dict(record.metrics), "metrics", maximum=262144)
        qualitative = _bounded_text(
            record.qualitative, "qualitative", maximum=65536, allow_empty=True
        )
        evidence_hash = _digest(record.evidence_hash, "evidence_hash")
        result = transaction.execute(
            f"""
            INSERT INTO test_case_results (
                owner_id, id, run_id, suite, test_name, outcome, duration_ms,
                metrics, qualitative, evidence_hash, verification_status
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            FROM test_runs
            WHERE owner_id = %s AND id = %s
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._CASE_FIELDS}
            """,
            (
                owner_id,
                case_id,
                run_id,
                suite,
                test_name,
                record.outcome,
                duration_ms,
                metrics,
                qualitative,
                evidence_hash,
                record.verification_status,
                owner_id,
                run_id,
            ),
        )
        row = _optional_returned(result, "quality_audit.create_case")
        if row is not None:
            return _case(row)
        existing = self.get_case(transaction, owner_id=owner_id, case_id=case_id)
        if existing is None:
            raise RepositoryConflictError(
                "qualification case parent is unavailable or identity belongs to another namespace",
                metadata={"operation": "quality_audit.create_case"},
            )
        if existing != record:
            raise RepositoryConflictError(
                "qualification case idempotency identity was reused with different semantics",
                metadata={"operation": "quality_audit.create_case"},
            )
        return existing

    def get_case(
        self, query: QueryExecutor, *, owner_id: str, case_id: str
    ) -> QualityTestCaseRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        case_id = _required_id(case_id, "case_id")
        row = query.fetch_one(
            f"""
            SELECT {self._CASE_FIELDS}
            FROM test_case_results
            WHERE owner_id = %s AND id = %s
            """,
            (owner_id, case_id),
        )
        return None if row is None else _case(row)

    def list_cases_for_run(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        run_id: str,
        suite: str | None = None,
        limit: int = 1000,
    ) -> tuple[QualityTestCaseRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        run_id = _required_id(run_id, "run_id")
        limit = _bounded_limit(limit, maximum=5000)
        if suite is None:
            clause = ""
            parameters: tuple[object, ...] = (owner_id, run_id, limit)
            ordering = "suite, test_name, id"
        else:
            suite = _bounded_text(suite, "suite", maximum=1024)
            clause = " AND suite = %s"
            parameters = (owner_id, run_id, suite, limit)
            ordering = "test_name, id"
        rows = query.fetch_all(
            f"""
            SELECT {self._CASE_FIELDS}
            FROM test_case_results
            WHERE owner_id = %s AND run_id = %s{clause}
            ORDER BY {ordering}
            LIMIT %s
            """,
            parameters,
        )
        return tuple(_case(row) for row in rows)

    def transition_verification_status(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        case_id: str,
        status: str,
        expected_status: str,
    ) -> QualityTestCaseRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        case_id = _required_id(case_id, "case_id")
        if status not in _VERIFICATION_STATUSES or expected_status not in _VERIFICATION_STATUSES:
            raise RepositoryValidationError("verification status is unsupported")
        if status == expected_status:
            raise RepositoryValidationError("verification transition must change status")
        result = transaction.execute(
            f"""
            UPDATE test_case_results
            SET verification_status = %s
            WHERE owner_id = %s AND id = %s AND verification_status = %s
            RETURNING {self._CASE_FIELDS}
            """,
            (status, owner_id, case_id, expected_status),
        )
        row = _optional_returned(result, "quality_audit.transition_verification_status")
        if row is not None:
            return _case(row)
        existing = self.get_case(transaction, owner_id=owner_id, case_id=case_id)
        if existing is None:
            return None
        raise RepositoryConflictError(
            "qualification case changed since it was read",
            metadata={"operation": "quality_audit.transition_verification_status"},
        )

    def create_evidence(
        self, transaction: Transaction, record: QualityEvidenceRecord
    ) -> QualityEvidenceRecord:
        owner_id = _required_id(record.owner_id, "owner_id")
        evidence_id = _required_id(record.evidence_id, "evidence_id")
        case_id = _required_id(record.case_id, "case_id")
        evidence_type = _bounded_text(record.evidence_type, "evidence_type", maximum=1024)
        data = _bounded_json(dict(record.data), "data", maximum=1048576)
        digest = _digest(record.sha256, "sha256", allow_empty=False)
        captured_at = _iso_time(record.captured_at, "captured_at")
        result = transaction.execute(
            f"""
            INSERT INTO test_evidence (
                owner_id, id, case_id, evidence_type, data, sha256, captured_at
            )
            SELECT %s, %s, %s, %s, %s, %s, %s
            FROM test_case_results
            WHERE owner_id = %s AND id = %s
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._EVIDENCE_FIELDS}
            """,
            (
                owner_id,
                evidence_id,
                case_id,
                evidence_type,
                data,
                digest,
                captured_at,
                owner_id,
                case_id,
            ),
        )
        row = _optional_returned(result, "quality_audit.create_evidence")
        if row is not None:
            return _evidence(row)
        existing = self.get_evidence(
            transaction, owner_id=owner_id, evidence_id=evidence_id
        )
        if existing is None:
            raise RepositoryConflictError(
                "qualification evidence parent is unavailable or identity belongs "
                "to another namespace",
                metadata={"operation": "quality_audit.create_evidence"},
            )
        if existing != record:
            raise RepositoryConflictError(
                "qualification evidence identity was reused with different semantics",
                metadata={"operation": "quality_audit.create_evidence"},
            )
        return existing

    def get_evidence(
        self, query: QueryExecutor, *, owner_id: str, evidence_id: str
    ) -> QualityEvidenceRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        evidence_id = _required_id(evidence_id, "evidence_id")
        row = query.fetch_one(
            f"""
            SELECT {self._EVIDENCE_FIELDS}
            FROM test_evidence
            WHERE owner_id = %s AND id = %s
            """,
            (owner_id, evidence_id),
        )
        return None if row is None else _evidence(row)

    def list_evidence_for_case(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        case_id: str,
        limit: int = 1000,
    ) -> tuple[QualityEvidenceRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        case_id = _required_id(case_id, "case_id")
        limit = _bounded_limit(limit, maximum=5000)
        rows = query.fetch_all(
            f"""
            SELECT {self._EVIDENCE_FIELDS}
            FROM test_evidence
            WHERE owner_id = %s AND case_id = %s
            ORDER BY captured_at, id
            LIMIT %s
            """,
            (owner_id, case_id, limit),
        )
        return tuple(_evidence(row) for row in rows)

    # Locked so concurrent reviews can't fork the audit chain
    def append_review_and_transition(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        entry_id: str,
        case_id: str,
        action: str,
        reviewer: str,
        rationale: str,
        timestamp: datetime,
        expected_verification_status: str,
    ) -> QualityCaseReviewResult | None:
        owner_id = _required_id(owner_id, "owner_id")
        entry_id = _required_id(entry_id, "entry_id")
        case_id = _required_id(case_id, "case_id")
        if action not in _AUDIT_ACTIONS:
            raise RepositoryValidationError("qualification audit action is unsupported")
        if expected_verification_status not in _VERIFICATION_STATUSES:
            raise RepositoryValidationError("verification status is unsupported")
        if action == expected_verification_status:
            raise RepositoryValidationError("verification transition must change status")
        reviewer = _bounded_text(reviewer, "reviewer", maximum=1024)
        rationale = _bounded_text(rationale, "rationale", maximum=65536, allow_empty=True)
        observed_at = _aware_time(timestamp, "timestamp")

        transaction.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"astralplane:quality-audit:{owner_id}",),
        )
        case_row = transaction.fetch_one(
            f"""
            SELECT {self._CASE_FIELDS}
            FROM test_case_results
            WHERE owner_id = %s AND id = %s
            FOR UPDATE
            """,
            (owner_id, case_id),
        )
        if case_row is None:
            return None
        current_case = _case(case_row)

        existing_row = transaction.fetch_one(
            f"""
            SELECT {self._AUDIT_FIELDS}
            FROM audit_entries
            WHERE owner_id = %s AND id = %s
            """,
            (owner_id, entry_id),
        )
        if existing_row is not None:
            existing = _audit_entry(existing_row)
            requested_replay = QualityAuditEntryRecord(
                owner_id=owner_id,
                entry_id=entry_id,
                case_id=case_id,
                action=action,
                reviewer=reviewer,
                rationale=rationale,
                timestamp=observed_at,
                previous_hash=existing.previous_hash,
                hash_version=2,
            )
            if existing != requested_replay:
                raise RepositoryConflictError(
                    "qualification audit identity was reused with different semantics",
                    metadata={"operation": "quality_audit.append_review_and_transition"},
                )
            if current_case.verification_status != action:
                raise RepositoryConflictError(
                    "qualification audit replay disagrees with case state",
                    metadata={"operation": "quality_audit.append_review_and_transition"},
                )
            return QualityCaseReviewResult(audit_entry=existing, test_case=current_case)

        if current_case.verification_status != expected_verification_status:
            raise RepositoryConflictError(
                "qualification case changed since it was read",
                metadata={"operation": "quality_audit.append_review_and_transition"},
            )

        head_row = transaction.fetch_one(
            f"""
            SELECT {self._AUDIT_FIELDS}
            FROM audit_entries
            WHERE owner_id = %s
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            FOR UPDATE
            """,
            (owner_id,),
        )
        head = None if head_row is None else _audit_entry(head_row)
        if head is not None and observed_at <= head.timestamp:
            raise RepositoryConflictError(
                "qualification audit timestamp must advance the serialized owner chain",
                metadata={"operation": "quality_audit.append_review_and_transition"},
            )
        previous_hash = (
            QUALITY_AUDIT_GENESIS_HASH if head is None else quality_audit_chain_hash(head)
        )
        new_entry = QualityAuditEntryRecord(
            owner_id=owner_id,
            entry_id=entry_id,
            case_id=case_id,
            action=action,
            reviewer=reviewer,
            rationale=rationale,
            timestamp=observed_at,
            previous_hash=previous_hash,
            hash_version=2,
        )
        inserted = transaction.execute(
            f"""
            INSERT INTO audit_entries (
                owner_id, id, case_id, action, reviewer, rationale, timestamp,
                previous_hash, hash_version
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 2)
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._AUDIT_FIELDS}
            """,
            (
                owner_id,
                entry_id,
                case_id,
                action,
                reviewer,
                rationale,
                observed_at.isoformat(),
                previous_hash,
            ),
        )
        inserted_row = _optional_returned(
            inserted, "quality_audit.append_review_and_transition.insert"
        )
        if inserted_row is None:
            raise RepositoryConflictError(
                "qualification audit identity is unavailable",
                metadata={"operation": "quality_audit.append_review_and_transition"},
            )
        persisted_entry = _audit_entry(inserted_row)
        if persisted_entry != new_entry:
            raise RepositoryDataError("persisted qualification audit entry changed unexpectedly")

        transitioned = transaction.execute(
            f"""
            UPDATE test_case_results
            SET verification_status = %s
            WHERE owner_id = %s AND id = %s AND verification_status = %s
            RETURNING {self._CASE_FIELDS}
            """,
            (action, owner_id, case_id, expected_verification_status),
        )
        transitioned_row = _optional_returned(
            transitioned, "quality_audit.append_review_and_transition.update"
        )
        if transitioned_row is None:
            raise RepositoryConflictError(
                "qualification case changed during atomic review",
                metadata={"operation": "quality_audit.append_review_and_transition"},
            )
        return QualityCaseReviewResult(
            audit_entry=persisted_entry,
            test_case=_case(transitioned_row),
        )

    def create_audit_entry(
        self, transaction: Transaction, record: QualityAuditEntryRecord
    ) -> QualityAuditEntryRecord:
        owner_id = _required_id(record.owner_id, "owner_id")
        entry_id = _required_id(record.entry_id, "entry_id")
        case_id = _required_id(record.case_id, "case_id")
        if record.action not in _AUDIT_ACTIONS:
            raise RepositoryValidationError("qualification audit action is unsupported")
        reviewer = _bounded_text(record.reviewer, "reviewer", maximum=1024)
        rationale = _bounded_text(record.rationale, "rationale", maximum=65536, allow_empty=True)
        timestamp = _iso_time(record.timestamp, "timestamp")
        previous_hash = _digest(record.previous_hash, "previous_hash")
        if record.hash_version not in {1, 2}:
            raise RepositoryValidationError(
                "qualification audit hash version is unsupported"
            )
        result = transaction.execute(
            f"""
            INSERT INTO audit_entries (
                owner_id, id, case_id, action, reviewer, rationale, timestamp,
                previous_hash, hash_version
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s
            FROM test_case_results
            WHERE owner_id = %s AND id = %s
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._AUDIT_FIELDS}
            """,
            (
                owner_id,
                entry_id,
                case_id,
                record.action,
                reviewer,
                rationale,
                timestamp,
                previous_hash,
                record.hash_version,
                owner_id,
                case_id,
            ),
        )
        row = _optional_returned(result, "quality_audit.create_audit_entry")
        if row is not None:
            return _audit_entry(row)
        existing = self.get_audit_entry(
            transaction, owner_id=owner_id, entry_id=entry_id
        )
        if existing is None:
            raise RepositoryConflictError(
                "qualification audit parent is unavailable or identity belongs "
                "to another namespace",
                metadata={"operation": "quality_audit.create_audit_entry"},
            )
        if existing != record:
            raise RepositoryConflictError(
                "qualification audit identity was reused with different semantics",
                metadata={"operation": "quality_audit.create_audit_entry"},
            )
        return existing

    def get_audit_entry(
        self, query: QueryExecutor, *, owner_id: str, entry_id: str
    ) -> QualityAuditEntryRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        entry_id = _required_id(entry_id, "entry_id")
        row = query.fetch_one(
            f"""
            SELECT {self._AUDIT_FIELDS}
            FROM audit_entries
            WHERE owner_id = %s AND id = %s
            """,
            (owner_id, entry_id),
        )
        return None if row is None else _audit_entry(row)

    def list_audits_for_case(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        case_id: str,
        limit: int = 1000,
    ) -> tuple[QualityAuditEntryRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        case_id = _required_id(case_id, "case_id")
        limit = _bounded_limit(limit, maximum=5000)
        rows = query.fetch_all(
            f"""
            SELECT {self._AUDIT_FIELDS}
            FROM audit_entries
            WHERE owner_id = %s AND case_id = %s
            ORDER BY timestamp, id
            LIMIT %s
            """,
            (owner_id, case_id, limit),
        )
        return tuple(_audit_entry(row) for row in rows)

    def get_latest_audit(
        self, query: QueryExecutor, *, owner_id: str
    ) -> QualityAuditEntryRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            f"""
            SELECT {self._AUDIT_FIELDS}
            FROM audit_entries
            WHERE owner_id = %s
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            (owner_id,),
        )
        return None if row is None else _audit_entry(row)

    def list_audits_for_run(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        run_id: str,
        limit: int = 5000,
    ) -> tuple[QualityAuditEntryRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        run_id = _required_id(run_id, "run_id")
        limit = _bounded_limit(limit, maximum=10000)
        rows = query.fetch_all(
            f"""
            SELECT a.{self._AUDIT_FIELDS.replace(', ', ', a.')}
            FROM audit_entries AS a
            JOIN test_case_results AS c
              ON c.owner_id = a.owner_id AND c.id = a.case_id
            WHERE a.owner_id = %s AND c.run_id = %s
            ORDER BY a.timestamp, a.id
            LIMIT %s
            """,
            (owner_id, run_id, limit),
        )
        return tuple(_audit_entry(row) for row in rows)

    def create_artifact(
        self, transaction: Transaction, record: QualityLatexArtifactRecord
    ) -> QualityLatexArtifactRecord:
        owner_id = _required_id(record.owner_id, "owner_id")
        artifact_id = _required_id(record.artifact_id, "artifact_id")
        run_id = _required_id(record.run_id, "run_id")
        filename = _filename(record.filename)
        generated_from = _bounded_strings(
            record.generated_from, "generated_from", maximum=1000
        )
        generated_json = _bounded_json(
            generated_from, "generated_from", maximum=262144
        )
        generated_at = _iso_time(record.generated_at, "generated_at")
        result = transaction.execute(
            f"""
            INSERT INTO latex_artifacts (
                owner_id, id, run_id, filename, generated_from,
                verification_complete, generated_at
            )
            SELECT %s, %s, %s, %s, %s, %s, %s
            FROM test_runs
            WHERE owner_id = %s AND id = %s
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._ARTIFACT_FIELDS}
            """,
            (
                owner_id,
                artifact_id,
                run_id,
                filename,
                generated_json,
                bool(record.verification_complete),
                generated_at,
                owner_id,
                run_id,
            ),
        )
        row = _optional_returned(result, "quality_audit.create_artifact")
        if row is not None:
            return _artifact(row)
        existing = self.get_artifact(
            transaction, owner_id=owner_id, artifact_id=artifact_id
        )
        if existing is None:
            raise RepositoryConflictError(
                "qualification artifact parent is unavailable or identity belongs "
                "to another namespace",
                metadata={"operation": "quality_audit.create_artifact"},
            )
        if existing != record:
            raise RepositoryConflictError(
                "qualification artifact identity was reused with different semantics",
                metadata={"operation": "quality_audit.create_artifact"},
            )
        return existing

    def get_artifact(
        self, query: QueryExecutor, *, owner_id: str, artifact_id: str
    ) -> QualityLatexArtifactRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        artifact_id = _required_id(artifact_id, "artifact_id")
        row = query.fetch_one(
            f"""
            SELECT {self._ARTIFACT_FIELDS}
            FROM latex_artifacts
            WHERE owner_id = %s AND id = %s
            """,
            (owner_id, artifact_id),
        )
        return None if row is None else _artifact(row)

    def list_artifacts_for_run(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        run_id: str,
        limit: int = 1000,
    ) -> tuple[QualityLatexArtifactRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        run_id = _required_id(run_id, "run_id")
        limit = _bounded_limit(limit, maximum=5000)
        rows = query.fetch_all(
            f"""
            SELECT {self._ARTIFACT_FIELDS}
            FROM latex_artifacts
            WHERE owner_id = %s AND run_id = %s
            ORDER BY filename, id
            LIMIT %s
            """,
            (owner_id, run_id, limit),
        )
        return tuple(_artifact(row) for row in rows)


__all__ = (
    "QualityAuditEntryRecord",
    "QualityAuditRepository",
    "QualityEvidenceRecord",
    "QualityLatexArtifactRecord",
    "QualityTestCaseRecord",
    "QualityTestRunRecord",
)
