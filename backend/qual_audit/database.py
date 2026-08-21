"""AstralPlane-backed storage facade for the qualification audit trail."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from astralplane.repositories.quality_audit import (
    QualityAuditEntryRecord,
    QualityEvidenceRecord,
    QualityLatexArtifactRecord,
    QualityTestCaseRecord,
    QualityTestRunRecord,
    verify_quality_audit_chain,
)
from orchestrator.plane_repository_context import PlaneRepositoryContext
from qual_audit.models import (
    AuditAction,
    AuditEntry,
    LatexArtifact,
    Outcome,
    RunStatus,
    TestCaseResult,
    TestEvidence,
    TestRun,
    VerificationStatus,
)

_DEFAULT_OWNER_ID = "system:quality-audit"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditDatabase:
    """Compatibility-shaped qualification API over one initialized Plane runtime.

    Every caller injects its already initialized application-scoped runtime.
    Runtime construction and reconciliation stay at the application boundary,
    so this facade can never create a second pool or bypass Deep's product
    reconciliation hook.
    """

    def __init__(
        self,
        *,
        plane_runtime,
        plane_repositories=None,
        quality_audit_repository=None,
        owner_id: str | None = None,
    ) -> None:
        self.owner_id = (owner_id or _DEFAULT_OWNER_ID).strip()
        if not self.owner_id:
            raise ValueError("qualification audit owner id must be non-empty")

        catalog = plane_repositories or plane_runtime.repositories
        repository = quality_audit_repository or catalog.quality_audit
        self._context = PlaneRepositoryContext(
            repository=repository,
            plane_runtime=plane_runtime,
        )

    # -- TestRun -----------------------------------------------------------

    def insert_run(self, run: TestRun) -> None:
        self._context.call(
            self._context.repository.create_run,
            record=QualityTestRunRecord(
                owner_id=self.owner_id,
                run_id=run.id,
                started_at=_aware(run.started_at),
                finished_at=(
                    None if run.finished_at is None else _aware(run.finished_at)
                ),
                system_state=dict(run.system_state),
                categories=tuple(run.categories),
                status=run.status.value,
            ),
        )

    def finish_run(self, run_id: str, status: RunStatus) -> None:
        with self._context.transaction() as transaction:
            existing = self._context.repository.get_run(
                transaction,
                owner_id=self.owner_id,
                run_id=run_id,
            )
            if existing is None or existing.status == status.value:
                return
            self._context.repository.finish_run(
                transaction,
                owner_id=self.owner_id,
                run_id=run_id,
                status=status.value,
                finished_at=_utcnow(),
                expected_status=existing.status,
            )

    def get_run(self, run_id: str) -> Optional[TestRun]:
        record = self._context.call(
            self._context.repository.get_run,
            owner_id=self.owner_id,
            run_id=run_id,
        )
        return None if record is None else _run_from_record(record)

    def get_latest_run(self) -> Optional[TestRun]:
        record = self._context.call(
            self._context.repository.get_latest_run,
            owner_id=self.owner_id,
        )
        return None if record is None else _run_from_record(record)

    # -- TestCaseResult ----------------------------------------------------

    def insert_case(self, case: TestCaseResult) -> None:
        self._context.call(
            self._context.repository.create_case,
            record=QualityTestCaseRecord(
                owner_id=self.owner_id,
                case_id=case.id,
                run_id=case.run_id,
                suite=case.suite,
                test_name=case.test_name,
                outcome=case.outcome.value,
                duration_ms=case.duration_ms,
                metrics=dict(case.metrics),
                qualitative=case.qualitative,
                evidence_hash=case.evidence_hash,
                verification_status=case.verification_status.value,
            ),
        )

    def get_cases_for_run(
        self,
        run_id: str,
        suite: Optional[str] = None,
    ) -> List[TestCaseResult]:
        records = self._context.call(
            self._context.repository.list_cases_for_run,
            owner_id=self.owner_id,
            run_id=run_id,
            suite=suite,
            limit=1000,
        )
        return [_case_from_record(record) for record in records]

    def get_case(self, case_id: str) -> Optional[TestCaseResult]:
        record = self._context.call(
            self._context.repository.get_case,
            owner_id=self.owner_id,
            case_id=case_id,
        )
        return None if record is None else _case_from_record(record)

    def update_verification_status(
        self,
        case_id: str,
        status: VerificationStatus,
    ) -> None:
        with self._context.transaction() as transaction:
            existing = self._context.repository.get_case(
                transaction,
                owner_id=self.owner_id,
                case_id=case_id,
            )
            if existing is None or existing.verification_status == status.value:
                return
            self._context.repository.transition_verification_status(
                transaction,
                owner_id=self.owner_id,
                case_id=case_id,
                status=status.value,
                expected_status=existing.verification_status,
            )

    def review_case(self, entry: AuditEntry) -> Optional[AuditEntry]:
        """Append one owner-chain review and transition its case atomically."""

        with self._context.transaction() as transaction:
            existing = self._context.repository.get_case(
                transaction,
                owner_id=self.owner_id,
                case_id=entry.case_id,
            )
            if existing is None:
                return None
            result = self._context.repository.append_review_and_transition(
                transaction,
                owner_id=self.owner_id,
                entry_id=entry.id,
                case_id=entry.case_id,
                action=entry.action.value,
                reviewer=entry.reviewer,
                rationale=entry.rationale,
                timestamp=_aware(entry.timestamp),
                expected_verification_status=existing.verification_status,
            )
        return (
            None
            if result is None
            else _audit_from_record(result.audit_entry)
        )

    # -- TestEvidence ------------------------------------------------------

    def insert_evidence(self, evidence: TestEvidence) -> None:
        self._context.call(
            self._context.repository.create_evidence,
            record=QualityEvidenceRecord(
                owner_id=self.owner_id,
                evidence_id=evidence.id,
                case_id=evidence.case_id,
                evidence_type=evidence.evidence_type,
                data=dict(evidence.data),
                sha256=evidence.sha256,
                captured_at=_aware(evidence.captured_at),
            ),
        )

    def get_evidence_for_case(self, case_id: str) -> List[TestEvidence]:
        records = self._context.call(
            self._context.repository.list_evidence_for_case,
            owner_id=self.owner_id,
            case_id=case_id,
            limit=1000,
        )
        return [
            TestEvidence(
                id=record.evidence_id,
                case_id=record.case_id,
                evidence_type=record.evidence_type,
                data=dict(record.data),
                sha256=record.sha256,
                captured_at=record.captured_at,
            )
            for record in records
        ]

    # -- AuditEntry -------------------------------------------------------

    def insert_audit(self, entry: AuditEntry) -> None:
        self._context.call(
            self._context.repository.create_audit_entry,
            record=QualityAuditEntryRecord(
                owner_id=self.owner_id,
                entry_id=entry.id,
                case_id=entry.case_id,
                action=entry.action.value,
                reviewer=entry.reviewer,
                rationale=entry.rationale,
                timestamp=_aware(entry.timestamp),
                previous_hash=entry.previous_hash,
            ),
        )

    def get_audits_for_case(self, case_id: str) -> List[AuditEntry]:
        records = self._context.call(
            self._context.repository.list_audits_for_case,
            owner_id=self.owner_id,
            case_id=case_id,
            limit=1000,
        )
        return [_audit_from_record(record) for record in records]

    def get_latest_audit(self) -> Optional[AuditEntry]:
        record = self._context.call(
            self._context.repository.get_latest_audit,
            owner_id=self.owner_id,
        )
        return None if record is None else _audit_from_record(record)

    def get_all_audits_for_run(self, run_id: str) -> List[AuditEntry]:
        records = self._context.call(
            self._context.repository.list_audits_for_run,
            owner_id=self.owner_id,
            run_id=run_id,
            limit=5000,
        )
        return [_audit_from_record(record) for record in records]

    def verify_audit_chain_for_run(
        self,
        run_id: str,
        *,
        require_genesis: bool = False,
    ) -> bool:
        """Verify the versioned Plane records without dropping authenticated fields."""

        records = self._context.call(
            self._context.repository.list_audits_for_run,
            owner_id=self.owner_id,
            run_id=run_id,
            limit=5000,
        )
        return verify_quality_audit_chain(
            records,
            require_genesis=require_genesis,
        )

    # -- LatexArtifact ----------------------------------------------------

    def insert_artifact(self, artifact: LatexArtifact) -> None:
        self._context.call(
            self._context.repository.create_artifact,
            record=QualityLatexArtifactRecord(
                owner_id=self.owner_id,
                artifact_id=artifact.id,
                run_id=artifact.run_id,
                filename=artifact.filename,
                generated_from=tuple(artifact.generated_from),
                verification_complete=artifact.verification_complete,
                generated_at=_aware(artifact.generated_at),
            ),
        )

    def get_artifacts_for_run(self, run_id: str) -> List[LatexArtifact]:
        records = self._context.call(
            self._context.repository.list_artifacts_for_run,
            owner_id=self.owner_id,
            run_id=run_id,
            limit=1000,
        )
        return [
            LatexArtifact(
                id=record.artifact_id,
                run_id=record.run_id,
                filename=record.filename,
                generated_from=list(record.generated_from),
                verification_complete=record.verification_complete,
                generated_at=record.generated_at,
            )
            for record in records
        ]


def _run_from_record(record: QualityTestRunRecord) -> TestRun:
    return TestRun(
        id=record.run_id,
        started_at=record.started_at,
        finished_at=record.finished_at,
        system_state=dict(record.system_state),
        categories=list(record.categories),
        status=RunStatus(record.status),
    )


def _case_from_record(record: QualityTestCaseRecord) -> TestCaseResult:
    return TestCaseResult(
        id=record.case_id,
        run_id=record.run_id,
        suite=record.suite,
        test_name=record.test_name,
        outcome=Outcome(record.outcome),
        duration_ms=record.duration_ms,
        metrics=dict(record.metrics),
        qualitative=record.qualitative,
        evidence_hash=record.evidence_hash,
        verification_status=VerificationStatus(record.verification_status),
    )


def _audit_from_record(record: QualityAuditEntryRecord) -> AuditEntry:
    return AuditEntry(
        id=record.entry_id,
        case_id=record.case_id,
        action=AuditAction(record.action),
        reviewer=record.reviewer,
        rationale=record.rationale,
        timestamp=record.timestamp,
        previous_hash=record.previous_hash,
    )


__all__ = ("AuditDatabase",)
