"""Tests for astralplane.repositories.quality_audit: run/case/evidence/artifact CRUD and
replay, hash-chained atomic review transitions, and chain-verifier support for
bounded subchains.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.quality_audit import (
    QUALITY_AUDIT_GENESIS_HASH,
    QualityAuditEntryRecord,
    QualityAuditRepository,
    QualityCaseReviewResult,
    QualityEvidenceRecord,
    QualityLatexArtifactRecord,
    QualityTestCaseRecord,
    QualityTestRunRecord,
    quality_audit_chain_hash,
    verify_quality_audit_chain,
)
from tests.repositories._support import Result, ScriptedTransaction

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
DIGEST = "a" * 64


def run_record(**changes: Any) -> QualityTestRunRecord:
    values: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "run_id": "run-1",
        "started_at": NOW,
        "finished_at": None,
        "system_state": {"revision": "abc", "nested": {"healthy": True}},
        "categories": ("unit", "security"),
        "status": "running",
    }
    values.update(changes)
    return QualityTestRunRecord(**values)


def run_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "id": "run-1",
        "started_at": NOW.isoformat(),
        "finished_at": None,
        "system_state": '{"nested":{"healthy":true},"revision":"abc"}',
        "categories": '["unit","security"]',
        "status": "running",
    }
    row.update(changes)
    return row


def case_record(**changes: Any) -> QualityTestCaseRecord:
    values: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "case_id": "case-1",
        "run_id": "run-1",
        "suite": "plane",
        "test_name": "test_owner_scope",
        "outcome": "passed",
        "duration_ms": 12.5,
        "metrics": {"assertions": 3},
        "qualitative": "clean",
        "evidence_hash": DIGEST,
        "verification_status": "pending",
    }
    values.update(changes)
    return QualityTestCaseRecord(**values)


def case_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "id": "case-1",
        "run_id": "run-1",
        "suite": "plane",
        "test_name": "test_owner_scope",
        "outcome": "passed",
        "duration_ms": 12.5,
        "metrics": '{"assertions":3}',
        "qualitative": "clean",
        "evidence_hash": DIGEST,
        "verification_status": "pending",
    }
    row.update(changes)
    return row


def evidence_record(**changes: Any) -> QualityEvidenceRecord:
    values: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "evidence_id": "evidence-1",
        "case_id": "case-1",
        "evidence_type": "pytest-report",
        "data": {"passed": True},
        "sha256": DIGEST,
        "captured_at": NOW,
    }
    values.update(changes)
    return QualityEvidenceRecord(**values)


def evidence_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "id": "evidence-1",
        "case_id": "case-1",
        "evidence_type": "pytest-report",
        "data": '{"passed":true}',
        "sha256": DIGEST,
        "captured_at": NOW.isoformat(),
    }
    row.update(changes)
    return row


def audit_record(**changes: Any) -> QualityAuditEntryRecord:
    values: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "entry_id": "audit-1",
        "case_id": "case-1",
        "action": "verified",
        "reviewer": "reviewer-1",
        "rationale": "evidence matches",
        "timestamp": NOW,
        "previous_hash": "",
        "hash_version": 2,
    }
    values.update(changes)
    return QualityAuditEntryRecord(**values)


def audit_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "id": "audit-1",
        "case_id": "case-1",
        "action": "verified",
        "reviewer": "reviewer-1",
        "rationale": "evidence matches",
        "timestamp": NOW.isoformat(),
        "previous_hash": "",
        "hash_version": 2,
    }
    row.update(changes)
    return row


def artifact_record(**changes: Any) -> QualityLatexArtifactRecord:
    values: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "artifact_id": "artifact-1",
        "run_id": "run-1",
        "filename": "reports/run-1.tex",
        "generated_from": ("case-1", "evidence-1"),
        "verification_complete": False,
        "generated_at": NOW,
    }
    values.update(changes)
    return QualityLatexArtifactRecord(**values)


def artifact_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "id": "artifact-1",
        "run_id": "run-1",
        "filename": "reports/run-1.tex",
        "generated_from": '["case-1","evidence-1"]',
        "verification_complete": False,
        "generated_at": NOW.isoformat(),
    }
    row.update(changes)
    return row


def test_run_create_get_latest_and_replay() -> None:
    repository = QualityAuditRepository()
    create = ScriptedTransaction(execute=[Result(returned_records=(run_row(),))])
    assert repository.create_run(create, run_record()) == run_record()
    assert create.calls[0][2][0:2] == ("system:quality-audit", "run-1")
    assert create.calls[0][2][4] == '{"nested":{"healthy":true},"revision":"abc"}'

    query = ScriptedTransaction(one=[run_row(), None, run_row()])
    assert repository.get_run(
        query, owner_id="system:quality-audit", run_id="run-1"
    ) == run_record()
    assert repository.get_run(
        query, owner_id="other", run_id="run-1"
    ) is None
    assert repository.get_latest_run(
        query, owner_id="system:quality-audit"
    ) == run_record()

    replay = ScriptedTransaction(execute=[Result(rowcount=0)], one=[run_row()])
    assert repository.create_run(replay, run_record()) == run_record()


def test_run_create_conflicts_are_distinct() -> None:
    repository = QualityAuditRepository()
    foreign = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    with pytest.raises(RepositoryConflictError, match="namespace"):
        repository.create_run(foreign, run_record())

    changed = ScriptedTransaction(
        execute=[Result(rowcount=0)], one=[run_row(system_state="{}")]
    )
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.create_run(changed, run_record())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": "unknown"}, "status"),
        ({"started_at": NOW.replace(tzinfo=None)}, "started_at"),
        ({"finished_at": NOW - timedelta(seconds=1), "status": "failed"}, "before"),
        ({"finished_at": NOW, "status": "running"}, "disagree"),
        ({"finished_at": None, "status": "completed"}, "disagree"),
        ({"categories": ("",)}, "categories"),
        ({"system_state": {"bad": float("nan")}}, "system_state"),
    ],
)
def test_run_rejects_invalid_records(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(RepositoryValidationError, match=message):
        QualityAuditRepository().create_run(ScriptedTransaction(), run_record(**changes))


def test_run_finish_success_missing_conflict_and_validation() -> None:
    repository = QualityAuditRepository()
    finished_at = NOW + timedelta(seconds=10)
    finished_row = run_row(
        finished_at=finished_at.isoformat(), status="completed"
    )
    success = ScriptedTransaction(execute=[Result(returned_records=(finished_row,))])
    finished = repository.finish_run(
        success,
        owner_id="system:quality-audit",
        run_id="run-1",
        status="completed",
        finished_at=finished_at,
    )
    assert finished is not None and finished.status == "completed"
    assert success.calls[0][2][-3:] == ("system:quality-audit", "run-1", "running")

    missing = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    assert repository.finish_run(
        missing,
        owner_id="other",
        run_id="run-1",
        status="failed",
        finished_at=finished_at,
    ) is None

    stale = ScriptedTransaction(execute=[Result(rowcount=0)], one=[finished_row])
    with pytest.raises(RepositoryConflictError, match="changed"):
        repository.finish_run(
            stale,
            owner_id="system:quality-audit",
            run_id="run-1",
            status="failed",
            finished_at=finished_at,
        )

    with pytest.raises(RepositoryValidationError, match="terminal"):
        repository.finish_run(
            ScriptedTransaction(),
            owner_id="system:quality-audit",
            run_id="run-1",
            status="running",
            finished_at=finished_at,
        )
    with pytest.raises(RepositoryValidationError, match="expected"):
        repository.finish_run(
            ScriptedTransaction(),
            owner_id="system:quality-audit",
            run_id="run-1",
            status="failed",
            expected_status="unknown",
            finished_at=finished_at,
        )

    corrupt = ScriptedTransaction(
        execute=[
            Result(
                returned_records=(
                    run_row(
                        started_at=finished_at.isoformat(),
                        finished_at=NOW.isoformat(),
                        status="failed",
                    ),
                )
            )
        ]
    )
    with pytest.raises(RepositoryDataError, match="before"):
        repository.finish_run(
            corrupt,
            owner_id="system:quality-audit",
            run_id="run-1",
            status="failed",
            finished_at=finished_at,
        )


def test_case_create_get_lists_and_replay() -> None:
    repository = QualityAuditRepository()
    create = ScriptedTransaction(execute=[Result(returned_records=(case_row(),))])
    assert repository.create_case(create, case_record()) == case_record()
    assert create.calls[0][2][-2:] == ("system:quality-audit", "run-1")

    replay = ScriptedTransaction(execute=[Result(rowcount=0)], one=[case_row()])
    assert repository.create_case(replay, case_record()) == case_record()

    query = ScriptedTransaction(
        one=[case_row(), None], all_rows=[(case_row(),), (case_row(),)]
    )
    assert repository.get_case(
        query, owner_id="system:quality-audit", case_id="case-1"
    ) == case_record()
    assert repository.get_case(query, owner_id="other", case_id="case-1") is None
    assert repository.list_cases_for_run(
        query, owner_id="system:quality-audit", run_id="run-1", limit=2
    ) == (case_record(),)
    assert query.calls[-1][2] == ("system:quality-audit", "run-1", 2)
    repository.list_cases_for_run(
        query,
        owner_id="system:quality-audit",
        run_id="run-1",
        suite="plane",
        limit=2,
    )
    assert query.calls[-1][2] == ("system:quality-audit", "run-1", "plane", 2)


def test_case_conflicts_validation_and_corrupt_duration() -> None:
    repository = QualityAuditRepository()
    unavailable = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    with pytest.raises(RepositoryConflictError, match="parent"):
        repository.create_case(unavailable, case_record())

    conflict = ScriptedTransaction(
        execute=[Result(rowcount=0)], one=[case_row(outcome="failed")]
    )
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.create_case(conflict, case_record())

    invalid_records = (
        (case_record(outcome="unknown"), "outcome"),
        (case_record(verification_status="unknown"), "verification"),
        (case_record(duration_ms=-1), "duration_ms"),
        (case_record(duration_ms=float("inf")), "duration_ms"),
        (case_record(evidence_hash="bad"), "evidence_hash"),
        (case_record(metrics={"bad": float("nan")}), "metrics"),
    )
    for record, message in invalid_records:
        with pytest.raises(RepositoryValidationError, match=message):
            repository.create_case(ScriptedTransaction(), record)

    corrupt = ScriptedTransaction(one=[case_row(duration_ms=-1)])
    with pytest.raises(RepositoryDataError, match="duration"):
        repository.get_case(
            corrupt, owner_id="system:quality-audit", case_id="case-1"
        )


def test_case_verification_transition_is_cas_fenced() -> None:
    repository = QualityAuditRepository()
    verified_row = case_row(verification_status="verified")
    success = ScriptedTransaction(execute=[Result(returned_records=(verified_row,))])
    verified = repository.transition_verification_status(
        success,
        owner_id="system:quality-audit",
        case_id="case-1",
        status="verified",
        expected_status="pending",
    )
    assert verified is not None and verified.verification_status == "verified"

    missing = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    assert repository.transition_verification_status(
        missing,
        owner_id="other",
        case_id="case-1",
        status="verified",
        expected_status="pending",
    ) is None

    stale = ScriptedTransaction(execute=[Result(rowcount=0)], one=[verified_row])
    with pytest.raises(RepositoryConflictError, match="changed"):
        repository.transition_verification_status(
            stale,
            owner_id="system:quality-audit",
            case_id="case-1",
            status="disputed",
            expected_status="pending",
        )

    with pytest.raises(RepositoryValidationError, match="unsupported"):
        repository.transition_verification_status(
            ScriptedTransaction(),
            owner_id="system:quality-audit",
            case_id="case-1",
            status="unknown",
            expected_status="pending",
        )
    with pytest.raises(RepositoryValidationError, match="change"):
        repository.transition_verification_status(
            ScriptedTransaction(),
            owner_id="system:quality-audit",
            case_id="case-1",
            status="pending",
            expected_status="pending",
        )


def test_evidence_create_get_list_replay_and_conflicts() -> None:
    repository = QualityAuditRepository()
    create = ScriptedTransaction(execute=[Result(returned_records=(evidence_row(),))])
    assert repository.create_evidence(create, evidence_record()) == evidence_record()
    assert create.calls[0][2][-2:] == ("system:quality-audit", "case-1")

    replay = ScriptedTransaction(execute=[Result(rowcount=0)], one=[evidence_row()])
    assert repository.create_evidence(replay, evidence_record()) == evidence_record()

    unavailable = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    with pytest.raises(RepositoryConflictError, match="parent"):
        repository.create_evidence(unavailable, evidence_record())

    conflict = ScriptedTransaction(
        execute=[Result(rowcount=0)], one=[evidence_row(data='{"passed":false}')]
    )
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.create_evidence(conflict, evidence_record())

    query = ScriptedTransaction(one=[evidence_row(), None], all_rows=[(evidence_row(),)])
    assert repository.get_evidence(
        query, owner_id="system:quality-audit", evidence_id="evidence-1"
    ) == evidence_record()
    assert repository.get_evidence(
        query, owner_id="other", evidence_id="evidence-1"
    ) is None
    assert repository.list_evidence_for_case(
        query, owner_id="system:quality-audit", case_id="case-1", limit=2
    ) == (evidence_record(),)

    with pytest.raises(RepositoryValidationError, match="sha256"):
        repository.create_evidence(
            ScriptedTransaction(), evidence_record(sha256="")
        )
    with pytest.raises(RepositoryValidationError, match="captured_at"):
        repository.create_evidence(
            ScriptedTransaction(), evidence_record(captured_at=NOW.replace(tzinfo=None))
        )


def test_audit_create_get_lists_latest_run_and_validation() -> None:
    repository = QualityAuditRepository()
    create = ScriptedTransaction(execute=[Result(returned_records=(audit_row(),))])
    assert repository.create_audit_entry(create, audit_record()) == audit_record()

    replay = ScriptedTransaction(execute=[Result(rowcount=0)], one=[audit_row()])
    assert repository.create_audit_entry(replay, audit_record()) == audit_record()

    unavailable = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    with pytest.raises(RepositoryConflictError, match="parent"):
        repository.create_audit_entry(unavailable, audit_record())

    conflict = ScriptedTransaction(
        execute=[Result(rowcount=0)], one=[audit_row(rationale="changed")]
    )
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.create_audit_entry(conflict, audit_record())

    query = ScriptedTransaction(
        one=[audit_row(), None, audit_row()],
        all_rows=[(audit_row(),), (audit_row(),)],
    )
    assert repository.get_audit_entry(
        query, owner_id="system:quality-audit", entry_id="audit-1"
    ) == audit_record()
    assert repository.get_audit_entry(query, owner_id="other", entry_id="audit-1") is None
    assert repository.list_audits_for_case(
        query, owner_id="system:quality-audit", case_id="case-1", limit=2
    ) == (audit_record(),)
    assert repository.get_latest_audit(
        query, owner_id="system:quality-audit"
    ) == audit_record()
    assert repository.list_audits_for_run(
        query, owner_id="system:quality-audit", run_id="run-1", limit=2
    ) == (audit_record(),)
    assert "JOIN test_case_results" in query.calls[-1][1]

    with pytest.raises(RepositoryValidationError, match="action"):
        repository.create_audit_entry(
            ScriptedTransaction(), audit_record(action="unknown")
        )
    with pytest.raises(RepositoryValidationError, match="previous_hash"):
        repository.create_audit_entry(
            ScriptedTransaction(), audit_record(previous_hash="bad")
        )


def test_atomic_review_serializes_genesis_and_transitions_case() -> None:
    repository = QualityAuditRepository()
    persisted_audit = audit_row(previous_hash=QUALITY_AUDIT_GENESIS_HASH)
    verified_case = case_row(verification_status="verified")
    transaction = ScriptedTransaction(
        one=[case_row(), None, None],
        execute=[
            Result(),
            Result(returned_records=(persisted_audit,)),
            Result(returned_records=(verified_case,)),
        ],
    )

    result = repository.append_review_and_transition(
        transaction,
        owner_id="system:quality-audit",
        entry_id="audit-1",
        case_id="case-1",
        action="verified",
        reviewer="reviewer-1",
        rationale="evidence matches",
        timestamp=NOW,
        expected_verification_status="pending",
    )

    assert result == QualityCaseReviewResult(
        audit_entry=audit_record(previous_hash=QUALITY_AUDIT_GENESIS_HASH),
        test_case=case_record(verification_status="verified"),
    )
    sql = transaction.fetch_sql()
    assert "pg_advisory_xact_lock" in sql
    assert "FOR UPDATE" in sql
    assert sql.index("INSERT INTO audit_entries") < sql.index("UPDATE test_case_results")


def test_atomic_review_derives_locked_head_hash_and_exact_replay() -> None:
    repository = QualityAuditRepository()
    head = audit_record(
        entry_id="audit-0",
        action="disputed",
        rationale="prior",
        timestamp=NOW - timedelta(seconds=1),
        previous_hash=QUALITY_AUDIT_GENESIS_HASH,
    )
    expected_hash = quality_audit_chain_hash(head)
    persisted = audit_row(previous_hash=expected_hash)
    transaction = ScriptedTransaction(
        one=[
            case_row(),
            None,
            audit_row(
                id="audit-0",
                action="disputed",
                rationale="prior",
                timestamp=(NOW - timedelta(seconds=1)).isoformat(),
                previous_hash=QUALITY_AUDIT_GENESIS_HASH,
            ),
        ],
        execute=[
            Result(),
            Result(returned_records=(persisted,)),
            Result(returned_records=(case_row(verification_status="verified"),)),
        ],
    )
    result = repository.append_review_and_transition(
        transaction,
        owner_id="system:quality-audit",
        entry_id="audit-1",
        case_id="case-1",
        action="verified",
        reviewer="reviewer-1",
        rationale="evidence matches",
        timestamp=NOW,
        expected_verification_status="pending",
    )
    assert result is not None
    assert result.audit_entry.previous_hash == expected_hash

    replay = ScriptedTransaction(
        one=[case_row(verification_status="verified"), persisted],
        execute=[Result()],
    )
    assert repository.append_review_and_transition(
        replay,
        owner_id="system:quality-audit",
        entry_id="audit-1",
        case_id="case-1",
        action="verified",
        reviewer="reviewer-1",
        rationale="evidence matches",
        timestamp=NOW,
        expected_verification_status="pending",
    ) == QualityCaseReviewResult(
        audit_entry=audit_record(previous_hash=expected_hash),
        test_case=case_record(verification_status="verified"),
    )
    assert "INSERT INTO audit_entries" not in replay.fetch_sql()


def test_atomic_review_rejects_stale_or_forking_writes() -> None:
    repository = QualityAuditRepository()
    stale = ScriptedTransaction(
        one=[case_row(verification_status="disputed"), None], execute=[Result()]
    )
    with pytest.raises(RepositoryConflictError, match="changed"):
        repository.append_review_and_transition(
            stale,
            owner_id="system:quality-audit",
            entry_id="audit-2",
            case_id="case-1",
            action="verified",
            reviewer="reviewer-1",
            rationale="",
            timestamp=NOW,
            expected_verification_status="pending",
        )

    head = audit_row(
        id="audit-0",
        timestamp=NOW.isoformat(),
        previous_hash=QUALITY_AUDIT_GENESIS_HASH,
    )
    non_monotonic = ScriptedTransaction(
        one=[case_row(), None, head], execute=[Result()]
    )
    with pytest.raises(RepositoryConflictError, match="timestamp"):
        repository.append_review_and_transition(
            non_monotonic,
            owner_id="system:quality-audit",
            entry_id="audit-2",
            case_id="case-1",
            action="verified",
            reviewer="reviewer-1",
            rationale="",
            timestamp=NOW,
            expected_verification_status="pending",
        )


def test_atomic_review_missing_replay_and_write_failures_are_distinct() -> None:
    repository = QualityAuditRepository()
    missing = ScriptedTransaction(one=[None], execute=[Result()])
    assert repository.append_review_and_transition(
        missing,
        owner_id="other",
        entry_id="audit-2",
        case_id="case-1",
        action="verified",
        reviewer="reviewer-1",
        rationale="",
        timestamp=NOW,
        expected_verification_status="pending",
    ) is None

    replay_mismatch = ScriptedTransaction(
        one=[case_row(verification_status="verified"), audit_row(reviewer="other")],
        execute=[Result()],
    )
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.append_review_and_transition(
            replay_mismatch,
            owner_id="system:quality-audit",
            entry_id="audit-1",
            case_id="case-1",
            action="verified",
            reviewer="reviewer-1",
            rationale="evidence matches",
            timestamp=NOW,
            expected_verification_status="pending",
        )

    insert_lost = ScriptedTransaction(
        one=[case_row(), None, None], execute=[Result(), Result(rowcount=0)]
    )
    with pytest.raises(RepositoryConflictError, match="identity is unavailable"):
        repository.append_review_and_transition(
            insert_lost,
            owner_id="system:quality-audit",
            entry_id="audit-1",
            case_id="case-1",
            action="verified",
            reviewer="reviewer-1",
            rationale="evidence matches",
            timestamp=NOW,
            expected_verification_status="pending",
        )

    update_lost = ScriptedTransaction(
        one=[case_row(), None, None],
        execute=[
            Result(),
            Result(
                returned_records=(
                    audit_row(previous_hash=QUALITY_AUDIT_GENESIS_HASH),
                )
            ),
            Result(rowcount=0),
        ],
    )
    with pytest.raises(RepositoryConflictError, match="during atomic review"):
        repository.append_review_and_transition(
            update_lost,
            owner_id="system:quality-audit",
            entry_id="audit-1",
            case_id="case-1",
            action="verified",
            reviewer="reviewer-1",
            rationale="evidence matches",
            timestamp=NOW,
            expected_verification_status="pending",
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"action": "unknown"}, "action"),
        ({"expected_verification_status": "unknown"}, "status"),
        ({"expected_verification_status": "verified"}, "change"),
        ({"timestamp": NOW.replace(tzinfo=None)}, "timestamp"),
    ],
)
def test_atomic_review_validates_command(changes: dict[str, Any], message: str) -> None:
    arguments: dict[str, Any] = {
        "owner_id": "system:quality-audit",
        "entry_id": "audit-1",
        "case_id": "case-1",
        "action": "verified",
        "reviewer": "reviewer-1",
        "rationale": "",
        "timestamp": NOW,
        "expected_verification_status": "pending",
    }
    arguments.update(changes)
    with pytest.raises(RepositoryValidationError, match=message):
        repository = QualityAuditRepository()
        repository.append_review_and_transition(ScriptedTransaction(), **arguments)


def test_versioned_chain_preserves_v1_and_authenticates_full_v2_record() -> None:
    legacy = audit_record(
        entry_id="legacy-1",
        timestamp=NOW - timedelta(seconds=2),
        previous_hash=QUALITY_AUDIT_GENESIS_HASH,
        hash_version=1,
    )
    assert quality_audit_chain_hash(legacy) == hashlib.sha256(
        f"{legacy.entry_id}{legacy.action}{legacy.timestamp.isoformat()}".encode()
    ).hexdigest()

    canonical = audit_record(
        entry_id="canonical-1",
        timestamp=NOW - timedelta(seconds=1),
        previous_hash=quality_audit_chain_hash(legacy),
        hash_version=2,
    )
    tail = audit_record(
        entry_id="canonical-2",
        timestamp=NOW,
        previous_hash=quality_audit_chain_hash(canonical),
        hash_version=2,
    )
    assert verify_quality_audit_chain((legacy, canonical, tail))

    material_changes = (
        {"owner_id": "other-owner"},
        {"case_id": "other-case"},
        {"reviewer": "other-reviewer"},
        {"rationale": "rewritten rationale"},
        {"previous_hash": "f" * 64},
    )
    for changes in material_changes:
        tampered = replace(canonical, **changes)
        assert quality_audit_chain_hash(tampered) != quality_audit_chain_hash(canonical)
        assert not verify_quality_audit_chain((legacy, tampered, tail))


def test_chain_verifier_supports_bounded_subchains_and_rejects_invalid_values() -> None:
    first = audit_record(
        entry_id="audit-a",
        timestamp=NOW - timedelta(seconds=1),
        previous_hash="f" * 64,
    )
    second = audit_record(
        entry_id="audit-b",
        timestamp=NOW,
        previous_hash=quality_audit_chain_hash(first),
    )
    assert not verify_quality_audit_chain((first, second))
    assert verify_quality_audit_chain((first, second), require_genesis=False)
    assert not verify_quality_audit_chain((second, first), require_genesis=False)
    with pytest.raises(RepositoryValidationError, match="ordered sequence"):
        verify_quality_audit_chain("not-records")  # type: ignore[arg-type]
    with pytest.raises(RepositoryValidationError, match="unsupported"):
        quality_audit_chain_hash(replace(first, hash_version=3))


def test_artifact_create_get_list_replay_conflicts_and_paths() -> None:
    repository = QualityAuditRepository()
    create = ScriptedTransaction(execute=[Result(returned_records=(artifact_row(),))])
    assert repository.create_artifact(create, artifact_record()) == artifact_record()

    replay = ScriptedTransaction(execute=[Result(rowcount=0)], one=[artifact_row()])
    assert repository.create_artifact(replay, artifact_record()) == artifact_record()

    unavailable = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    with pytest.raises(RepositoryConflictError, match="parent"):
        repository.create_artifact(unavailable, artifact_record())

    conflict = ScriptedTransaction(
        execute=[Result(rowcount=0)], one=[artifact_row(filename="changed.tex")]
    )
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.create_artifact(conflict, artifact_record())

    query = ScriptedTransaction(one=[artifact_row(), None], all_rows=[(artifact_row(),)])
    assert repository.get_artifact(
        query, owner_id="system:quality-audit", artifact_id="artifact-1"
    ) == artifact_record()
    assert repository.get_artifact(
        query, owner_id="other", artifact_id="artifact-1"
    ) is None
    assert repository.list_artifacts_for_run(
        query, owner_id="system:quality-audit", run_id="run-1", limit=2
    ) == (artifact_record(),)

    for filename in ("../escape.tex", "/absolute.tex", r"C:\escape.tex"):
        with pytest.raises(RepositoryValidationError, match="filename"):
            repository.create_artifact(
                ScriptedTransaction(), artifact_record(filename=filename)
            )
    with pytest.raises(RepositoryValidationError, match="generated_from"):
        repository.create_artifact(
            ScriptedTransaction(), artifact_record(generated_from=("",))
        )


@pytest.mark.parametrize(
    ("row", "method", "message"),
    [
        (run_row(started_at="not-time"), "run", "timestamp"),
        (run_row(started_at=1), "run", "timezone-aware"),
        (run_row(system_state="[]"), "run", "invalid shape"),
        (run_row(categories="{}"), "run", "invalid shape"),
        (case_row(metrics="[]"), "case", "invalid shape"),
        (evidence_row(data="[]"), "evidence", "invalid shape"),
        (artifact_row(generated_from="{}"), "artifact", "invalid shape"),
    ],
)
def test_corrupt_persisted_shapes_fail_closed(
    row: dict[str, Any], method: str, message: str
) -> None:
    repository = QualityAuditRepository()
    query = ScriptedTransaction(one=[row])
    with pytest.raises(RepositoryDataError, match=message):
        if method == "run":
            repository.get_run(
                query, owner_id="system:quality-audit", run_id="run-1"
            )
        elif method == "case":
            repository.get_case(
                query, owner_id="system:quality-audit", case_id="case-1"
            )
        elif method == "evidence":
            repository.get_evidence(
                query, owner_id="system:quality-audit", evidence_id="evidence-1"
            )
        else:
            repository.get_artifact(
                query, owner_id="system:quality-audit", artifact_id="artifact-1"
            )
