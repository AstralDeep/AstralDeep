"""Tests for orchestrator/work_reconciliation.py's fence-bound attempt ledger and
closed late-result vocabulary: binding requires the live fence, reports bind to the
recorded attempt identity, and uncertain outcomes are preserved without re-execution.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("USE_MOCK_AUTH", "true")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import mesh_enrollment as me  # noqa: E402
from orchestrator.work_admission import (  # noqa: E402
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
from orchestrator.work_reconciliation import (  # noqa: E402
    AttemptOutcome,
    EffectAttemptLedger,
    EffectReconciliationService,
    EFFECT_DISPATCHED_PHASE,
    LateResultReport,
    ReconciliationRefused,
)
from orchestrator.work_recovery import (  # noqa: E402
    ExecutorRecoveryMode,
    ExecutorRecoveryService,
    RecoveryDisposition,
)


@dataclass
class _FakeClock:
    current: datetime = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class _Row:
    def __init__(self, credential_key: str, encrypted_value: str, updated_at: int):
        self.credential_key = credential_key
        self.encrypted_value = encrypted_value
        self.updated_at = updated_at


class _FakeCredentialRepository:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], _Row] = {}
        self.fail_next_compare = False
        self.corrupt_next_write = False

    @staticmethod
    def _identity(owner_id, agent_id, credential_key):
        return (str(owner_id), str(agent_id), str(credential_key))

    def upsert_credential(self, transaction, *, owner_id, agent_id, credential_key,
                          encrypted_value, updated_at):
        key = self._identity(owner_id, agent_id, credential_key)
        if self.corrupt_next_write:
            encrypted_value = "corrupted-ciphertext"
            self.corrupt_next_write = False
        row = _Row(credential_key, encrypted_value, int(updated_at))
        self._rows[key] = row
        return row

    def compare_and_set_ciphertext(self, transaction, *, owner_id, agent_id,
                                   credential_key, expected_updated_at,
                                   encrypted_value, updated_at):
        key = self._identity(owner_id, agent_id, credential_key)
        row = self._rows.get(key)
        if row is None:
            from astralplane.repositories import RepositoryConflictError

            raise RepositoryConflictError(
                "owner-scoped credential does not exist for compare-and-set"
            )
        if self.fail_next_compare or row.updated_at != int(expected_updated_at):
            from astralplane.repositories import RepositoryConflictError

            self.fail_next_compare = False
            raise RepositoryConflictError("credential revision fence is stale")
        if self.corrupt_next_write:
            encrypted_value = "corrupted-ciphertext"
            self.corrupt_next_write = False
        replacement = _Row(credential_key, encrypted_value, int(updated_at))
        self._rows[key] = replacement
        return replacement

    def get_credential(self, transaction, *, owner_id, agent_id, credential_key):
        return self._rows.get(self._identity(owner_id, agent_id, credential_key))

    def list_credentials(self, transaction, *, owner_id, agent_id, limit=200):
        keys = sorted(
            key for key in self._rows
            if key[0] == str(owner_id) and key[1] == str(agent_id)
        )
        return [self._rows[key] for key in keys[: int(limit)]]

    def delete_credential(self, transaction, *, owner_id, agent_id, credential_key):
        return (
            self._rows.pop(self._identity(owner_id, agent_id, credential_key), None)
            is not None
        )


class _FakeRuntime:
    def __init__(self) -> None:
        self.repositories = SimpleNamespace(remote=object(), credentials=object(),
                                            mesh_enrollment=object())

    @contextmanager
    def transaction(self):
        yield object()


class _FakeAuditRepository:
    def __init__(self, *, fail: bool = False, return_none: bool = False):
        self.fail = fail
        self.return_none = return_none
        self.events = []

    def insert_in_transaction(self, event, *, transaction, plane_runtime):
        if self.fail:
            raise RuntimeError("audit unavailable")
        if self.return_none:
            return None
        self.events.append(event)
        return event


class _RecordingTargetAuthority:
    def __init__(self, *, unavailable: bool = False):
        self.calls: list[tuple[str, str, str]] = []
        self.unavailable = unavailable

    def assert_reportable(self, *, owner_id, target_kind, target_id):
        self.calls.append((owner_id, target_kind, target_id))
        if self.unavailable:
            raise ReconciliationRefused("reconcile_target_unavailable")


@pytest.fixture
def encryption_key(monkeypatch):
    monkeypatch.setenv(
        "CREDENTIAL_ENCRYPTION_KEY", "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ="
    )


def _coordinator(
    clock: _FakeClock,
    *,
    active_limit: int = 2,
    queue_limit: int = 1,
) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=active_limit,
                queue_limit=queue_limit,
                max_wait_ms=5_000,
                config_revision="test-reconcile",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
        operation_retention=timedelta(hours=24),
    )


def _submit_and_claim(coordinator: WorkAdmissionCoordinator, label: str):
    submission_id = uuid.uuid4()
    accepted = coordinator.submit(
        OperationRequest(
            operation_kind="connection_frame",
            admission_class=AdmissionClass.INTERACTIVE,
            owner=OperationOwner(OwnerScope.USER, "owner-a", None),
            submission_id=submission_id,
            idempotency_namespace="reconcile_stage",
            idempotency_key=label,
            normalized_input_digest=_digest(label),
            chat_id=f"chat-{label}",
            parent_operation_id=None,
            connection_generation=uuid.uuid4(),
            request_generation=uuid.uuid4(),
        )
    )
    claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, accepted.operation_id
    )
    assert claim is not None
    return accepted, claim


def _ledger(clock: _FakeClock, coordinator, repository, *, audit=None) -> EffectAttemptLedger:
    from orchestrator.plane_repository_context import PlaneRepositoryContext

    return EffectAttemptLedger(
        coordinator=coordinator,
        source=PlaneRepositoryContext(
            repository=repository, plane_runtime=_FakeRuntime()
        ),
        audit_repository=audit,
    )


def _audit() -> _FakeAuditRepository:
    return _FakeAuditRepository()


def _report(attempt, outcome: str, *, argument_digest: str | None = None,
            target_id: str = "member-1", result_digest: str | None = None):
    return LateResultReport(
        attempt_nonce=attempt.attempt_nonce,
        argument_digest=argument_digest or attempt.argument_digest,
        target_kind=attempt.target_kind,
        target_id=target_id,
        outcome=outcome,
        result_digest=result_digest
        or (_digest("result") if outcome == "confirmed_applied" else None),
    )


def _record(coordinator, operation_id):
    record = coordinator.repository.get_operation_for_administration(operation_id)
    assert record is not None
    return record


def test_bind_requires_the_live_fence_and_records_the_dispatch_phase(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "bind")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())

    attempt = ledger.bind(
        claim.fence,
        target_kind="mesh_member",
        target_id="member-1",
        argument_digest=_digest("arguments"),
    )

    assert attempt.operation_id == accepted.operation_id
    assert attempt.execution_generation == claim.fence.execution_generation
    assert attempt.state == "bound"
    assert attempt.outcome is None
    stored = ledger.read(
        partition="owner-a",
        operation_id=accepted.operation_id,
        nonce=attempt.attempt_nonce,
    )
    assert stored == attempt
    assert _record(coordinator, accepted.operation_id).phase_code == EFFECT_DISPATCHED_PHASE


def test_bind_replays_identically_and_refuses_identity_conflicts(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "replay")
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)

    first = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    second = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    assert first == second

    with pytest.raises(ReconciliationRefused, match="attempt_nonce_conflict"):
        ledger.bind(
            claim.fence, target_kind="mesh_member", target_id="member-1",
            argument_digest=_digest("other"),
        )
    distinct = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-2",
        argument_digest=_digest("arguments"),
    )
    assert distinct.attempt_nonce != first.attempt_nonce

    row = repository._rows[("owner-a", "work.effect-attempts.v1", first.attempt_nonce)]
    tampered = me._decode_record(row.encrypted_value)
    tampered["target_id"] = "member-9"
    row.encrypted_value = me._encode_record(tampered)
    with pytest.raises(ReconciliationRefused, match="attempt_nonce_conflict"):
        ledger.bind(
            claim.fence, target_kind="mesh_member", target_id="member-1",
            argument_digest=_digest("arguments"),
        )


def test_bind_refuses_invalid_identity_material(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _accepted, claim = _submit_and_claim(coordinator, "invalid")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())

    with pytest.raises(ReconciliationRefused, match="reconcile_report_invalid"):
        ledger.bind(
            claim.fence, target_kind="mesh_member", target_id="member-1",
            argument_digest="not-a-digest",
        )
    with pytest.raises(ReconciliationRefused, match="reconcile_report_invalid"):
        ledger.bind(
            claim.fence, target_kind="Not A Kind", target_id="member-1",
            argument_digest=_digest("arguments"),
        )
    with pytest.raises(ReconciliationRefused, match="reconcile_report_invalid"):
        ledger.bind(
            claim.fence, target_kind="mesh_member", target_id="bad id spaces",
            argument_digest=_digest("arguments"),
        )


def test_bind_after_lease_expiry_is_refused_and_writes_nothing(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "stale")
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)

    clock.advance(timedelta(seconds=31))
    expired = coordinator.expire_execution_leases()
    assert len(expired) == 1
    with pytest.raises(StaleExecutionFenceError):
        ledger.bind(
            claim.fence, target_kind="mesh_member", target_id="member-1",
            argument_digest=_digest("arguments"),
        )
    assert repository._rows == {}
    assert _record(coordinator, accepted.operation_id).phase_code is None


def test_unresolved_attempt_capacity_is_bounded(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _accepted, claim = _submit_and_claim(coordinator, "capacity")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())

    attempts = []
    for index in range(8):
        attempts.append(
            ledger.bind(
                claim.fence, target_kind="remote_machine", target_id=f"machine-{index}",
                argument_digest=_digest(f"arguments-{index}"),
            )
        )
    assert len({item.attempt_nonce for item in attempts}) == 8
    with pytest.raises(ReconciliationRefused, match="attempt_capacity_exhausted"):
        ledger.bind(
            claim.fence, target_kind="remote_machine", target_id="machine-9",
            argument_digest=_digest("arguments-9"),
        )
    ledger.settle(
        partition="owner-a",
        operation_id=attempts[0].operation_id,
        nonce=attempts[0].attempt_nonce,
        outcome=AttemptOutcome.NOT_APPLIED,
        outcome_digest=None,
    )
    ninth = ledger.bind(
        claim.fence, target_kind="remote_machine", target_id="machine-9",
        argument_digest=_digest("arguments-9"),
    )
    assert ninth.attempt_nonce != attempts[0].attempt_nonce


def test_settled_attempt_cannot_rebind_under_the_same_fence(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _accepted, claim = _submit_and_claim(coordinator, "settled-bind")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    ledger.settle(
        partition="owner-a",
        operation_id=attempt.operation_id,
        nonce=attempt.attempt_nonce,
        outcome=AttemptOutcome.NOT_APPLIED,
        outcome_digest=None,
    )
    with pytest.raises(ReconciliationRefused, match="attempt_already_settled"):
        ledger.bind(
            claim.fence, target_kind="mesh_member", target_id="member-1",
            argument_digest=_digest("arguments"),
        )


def test_ledger_and_service_construction_fails_closed(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    repository = _FakeCredentialRepository()
    with pytest.raises(ValueError, match="WorkAdmissionCoordinator"):
        _ledger(clock, object(), repository)
    with pytest.raises(ValueError, match="Plane repository context"):
        EffectAttemptLedger(coordinator=coordinator, source=object())
    with pytest.raises(ValueError, match="audit repository"):
        _ledger(clock, coordinator, repository, audit=object())
    real_ledger = _ledger(clock, coordinator, repository)
    with pytest.raises(ValueError, match="EffectAttemptLedger"):
        EffectReconciliationService(ledger=object(), target_authority=_RecordingTargetAuthority())
    with pytest.raises(ValueError, match="TargetReportAuthority"):
        EffectReconciliationService(ledger=real_ledger, target_authority=object())


def test_bind_refuses_objects_that_are_not_execution_fences(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _accepted, claim = _submit_and_claim(coordinator, "fence-type")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    with pytest.raises(ReconciliationRefused, match="reconcile_fence_invalid"):
        ledger.bind(
            "not-a-fence", target_kind="mesh_member", target_id="member-1",
            argument_digest=_digest("arguments"),
        )


def test_bound_attempts_report_no_outcome_class(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _accepted, claim = _submit_and_claim(coordinator, "outcome-class")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    assert attempt.report_outcome is None
    ledger.settle(
        partition="owner-a",
        operation_id=attempt.operation_id,
        nonce=attempt.attempt_nonce,
        outcome=AttemptOutcome.APPLIED,
        outcome_digest=_digest("result"),
    )
    settled = ledger.read(
        partition="owner-a",
        operation_id=attempt.operation_id,
        nonce=attempt.attempt_nonce,
    )
    assert settled.report_outcome == "confirmed_applied"


def test_ledger_partitions_follow_the_operation_owner_scope(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)
    connection_id = uuid.uuid4()
    for scope, owner, partition in (
        (OwnerScope.CONNECTION, None, "connection:" + str(connection_id)),
        (OwnerScope.SYSTEM, None, "system"),
    ):
        accepted = coordinator.submit(
            OperationRequest(
                operation_kind="connection_frame",
                admission_class=AdmissionClass.INTERACTIVE,
                owner=OperationOwner(scope, owner, connection_id if scope is OwnerScope.CONNECTION else None),
                submission_id=uuid.uuid4(),
                idempotency_namespace="reconcile_stage",
                idempotency_key=str(uuid.uuid4()),
                normalized_input_digest=_digest("scope"),
                chat_id="chat-scope",
                parent_operation_id=None,
                connection_generation=uuid.uuid4(),
                request_generation=uuid.uuid4(),
            )
        )
        claim = coordinator.claim_operation(AdmissionClass.INTERACTIVE, accepted.operation_id)
        attempt = ledger.bind(
            claim.fence, target_kind="mesh_member", target_id="member-1",
            argument_digest=_digest("arguments"),
        )
        keys = [
            key for key in repository._rows
            if key[0] == partition and key[2] == attempt.attempt_nonce
        ]
        assert keys, partition


def test_probe_of_a_user_record_without_partition_stays_none(encryption_key):
    from orchestrator.work_admission import OperationRecord

    clock = _FakeClock()
    coordinator = _coordinator(clock)
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    record = OperationRecord(
        operation_id=uuid.uuid4(),
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner_scope=OwnerScope.USER,
        owner_user_id="",
        connection_scope_id=None,
        idempotency_namespace=None,
        idempotency_key=None,
        normalized_input_digest=None,
        chat_id=None,
        parent_operation_id=None,
        connection_generation=None,
        request_generation=None,
        state=OperationState.RETRYABLE,
        phase_code=EFFECT_DISPATCHED_PHASE,
        terminal_code="execution_lease_expired",
        safe_summary=None,
        retry_after_ms=None,
        execution_generation=1,
        execution_lease_token=None,
        state_revision=2,
        accepted_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        queue_deadline_at=None,
        started_at=datetime.now(UTC),
        terminal_at=datetime.now(UTC),
        cancel_requested_at=None,
        purge_after=None,
    )
    assert ledger.resolution_probe(record) is None


def test_reads_of_malformed_or_foreign_rows_fail_closed(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)
    _accepted, claim = _submit_and_claim(coordinator, "malformed")
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    partition = "owner-a"

    assert ledger.read(partition=partition, operation_id=attempt.operation_id, nonce="zz") is None

    namespace = "work.effect-attempts.v1"
    row = repository._rows[(partition, namespace, attempt.attempt_nonce)]
    original = row.encrypted_value
    row.encrypted_value = "not-a-fernet-token"
    assert ledger.read(partition=partition, operation_id=attempt.operation_id,
                       nonce=attempt.attempt_nonce) is None
    row.encrypted_value = me._encode_record({"version": 99})
    assert ledger.read(partition=partition, operation_id=attempt.operation_id,
                       nonce=attempt.attempt_nonce) is None
    broken = me._decode_record(original)
    broken.pop("argument_digest")
    row.encrypted_value = me._encode_record(broken)
    assert ledger.read(partition=partition, operation_id=attempt.operation_id,
                       nonce=attempt.attempt_nonce) is None
    broken = me._decode_record(original)
    broken["state"] = "mysterious"
    row.encrypted_value = me._encode_record(broken)
    assert ledger.read(partition=partition, operation_id=attempt.operation_id,
                       nonce=attempt.attempt_nonce) is None


def test_settlement_rejects_unknown_or_mismatched_rows_and_bad_outcomes(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _accepted, claim = _submit_and_claim(coordinator, "settle-guards")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    with pytest.raises(ReconciliationRefused, match="reconcile_outcome_invalid"):
        ledger.settle(
            partition="owner-a",
            operation_id=attempt.operation_id,
            nonce=attempt.attempt_nonce,
            outcome="applied",
            outcome_digest=None,
        )
    with pytest.raises(ReconciliationRefused, match="reconcile_attempt_unknown"):
        ledger.settle(
            partition="owner-a",
            operation_id=attempt.operation_id,
            nonce="f" * 32,
            outcome=AttemptOutcome.UNCERTAIN,
            outcome_digest=None,
        )
    with pytest.raises(ReconciliationRefused, match="reconcile_attempt_unknown"):
        ledger.settle(
            partition="owner-a",
            operation_id=uuid.uuid4(),
            nonce=attempt.attempt_nonce,
            outcome=AttemptOutcome.UNCERTAIN,
            outcome_digest=None,
        )


def test_write_corruption_fails_closed_as_record_invalid(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)
    _accepted, claim = _submit_and_claim(coordinator, "corrupt-write")
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    repository.corrupt_next_write = True
    with pytest.raises(ReconciliationRefused, match="attempt_record_invalid"):
        ledger.settle(
            partition="owner-a",
            operation_id=attempt.operation_id,
            nonce=attempt.attempt_nonce,
            outcome=AttemptOutcome.NOT_APPLIED,
            outcome_digest=None,
        )


def test_inventory_overflow_refuses_bind_and_probe_failures_stay_preserved(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)
    _accepted, claim = _submit_and_claim(coordinator, "flood")
    for index in range(130):
        repository.upsert_credential(
            None,
            owner_id="owner-a",
            agent_id="work.effect-attempts.v1",
            credential_key=f"{index:032x}",
            encrypted_value="junk",
            updated_at=index + 1,
        )
    with pytest.raises(ReconciliationRefused, match="attempt_inventory_exhausted"):
        ledger.bind(
            claim.fence, target_kind="mesh_member", target_id="member-1",
            argument_digest=_digest("arguments"),
        )
    assert ledger.resolution_probe(_record(coordinator, _accepted.operation_id)) is None


def test_unreadable_rows_refuse_binding_and_preserve_inventory(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)
    _accepted, claim = _submit_and_claim(coordinator, "junk-skip")
    for index in range(100):
        repository.upsert_credential(
            None,
            owner_id="owner-a",
            agent_id="work.effect-attempts.v1",
            credential_key=f"{index:032x}",
            encrypted_value="junk",
            updated_at=index + 1,
        )
    with pytest.raises(ReconciliationRefused, match="attempt_inventory_invalid"):
        ledger.bind(claim.fence, target_kind="mesh_member", target_id="member-1",
                    argument_digest=_digest("arguments"))
    assert len(repository._rows) == 100
    probe_record = _record(coordinator, _accepted.operation_id)
    assert ledger.resolution_probe(probe_record) is None


def test_probe_without_attempts_reports_no_resolution(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    _accepted, _claim = _submit_and_claim(coordinator, "probe-empty")
    assert ledger.resolution_probe(_record(coordinator, _accepted.operation_id)) is None


def test_report_accepts_valid_dicts_and_refuses_invalid_ones(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "dict-body")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )
    result = service.report("owner-a", str(accepted.operation_id), {
        "attempt_nonce": attempt.attempt_nonce,
        "argument_digest": attempt.argument_digest,
        "target_kind": "mesh_member",
        "target_id": "member-1",
        "outcome": "confirmed_not_applied",
    })
    assert result["outcome"] == "confirmed_not_applied"
    with pytest.raises(ReconciliationRefused, match="reconcile_report_invalid"):
        service.report("owner-a", accepted.operation_id, {
            "attempt_nonce": "bad",
            "argument_digest": "bad",
            "target_kind": "bad",
            "target_id": "bad",
            "outcome": "bad",
        })



def test_prune_removes_every_row_of_a_purged_operation(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=100, queue_limit=0)
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)
    accepted, claim = _submit_and_claim(coordinator, "purged-repeat")
    ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-a",
        argument_digest=_digest("arguments-a"),
    )
    ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-b",
        argument_digest=_digest("arguments-b"),
    )
    fresh = _submit_and_claim(coordinator, "fresh-live")
    ledger.bind(
        fresh[1].fence, target_kind="mesh_member", target_id="member-x",
        argument_digest=_digest("arguments-x"),
    )
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    coordinator.repository.purge_expired(now=clock.current + timedelta(hours=25), limit=200)
    second = _submit_and_claim(coordinator, "fresh-after-purge")
    ledger.bind(
        second[1].fence, target_kind="mesh_member", target_id="member-y",
        argument_digest=_digest("arguments-y"),
    )
    third = _submit_and_claim(coordinator, "fresh-live-two")
    ledger.bind(
        third[1].fence, target_kind="mesh_member", target_id="member-z",
        argument_digest=_digest("arguments-z"),
    )
    assert ledger.list_for_operation(partition="owner-a", operation_id=accepted.operation_id) == ()
    assert len(ledger.list_for_operation(partition="owner-a", operation_id=second[0].operation_id)) == 1
    assert len(ledger.list_for_operation(partition="owner-a", operation_id=third[0].operation_id)) == 1


def test_prune_scan_limit_leaves_the_rest_for_the_next_bind(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=100, queue_limit=0)
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)
    purged = []
    for index in range(65):
        accepted, claim = _submit_and_claim(coordinator, f"purge-{index}")
        ledger.bind(
            claim.fence, target_kind="mesh_member", target_id=f"member-{index}",
            argument_digest=_digest(f"arguments-{index}"),
        )
        purged.append(accepted.operation_id)
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    coordinator.repository.purge_expired(now=clock.current + timedelta(hours=25), limit=200)
    first = _submit_and_claim(coordinator, "fresh-one")
    ledger.bind(
        first[1].fence, target_kind="mesh_member", target_id="member-x",
        argument_digest=_digest("arguments-x"),
    )
    remaining = [
        operation_id for operation_id in purged
        if ledger.list_for_operation(partition="owner-a", operation_id=operation_id)
    ]
    assert len(remaining) == 1
    second = _submit_and_claim(coordinator, "fresh-two")
    ledger.bind(
        second[1].fence, target_kind="mesh_member", target_id="member-z",
        argument_digest=_digest("arguments-z"),
    )
    assert ledger.list_for_operation(partition="owner-a", operation_id=remaining[0]) == ()


def test_late_confirmed_not_applied_reconciles_and_replays_idempotently(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "not-applied")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )

    first = service.report("owner-a", accepted.operation_id,
                           _report(attempt, "confirmed_not_applied"))
    assert first["outcome"] == "confirmed_not_applied"
    assert first["applied"] is False
    assert first["operation_state"] == OperationState.RETRYABLE.value
    replay = service.report("owner-a", accepted.operation_id,
                            _report(attempt, "confirmed_not_applied"))
    assert replay["outcome"] == first["outcome"]
    with pytest.raises(ReconciliationRefused, match="reconcile_outcome_conflict"):
        service.report("owner-a", accepted.operation_id,
                       _report(attempt, "uncertain"))


def test_late_confirmed_applied_binds_the_result_digest(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "applied")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )

    result = service.report("owner-a", accepted.operation_id,
                            _report(attempt, "confirmed_applied"))
    assert result["applied"] is True
    assert result["outcome"] == "confirmed_applied"
    settled = ledger.read(
        partition="owner-a",
        operation_id=accepted.operation_id,
        nonce=attempt.attempt_nonce,
    )
    assert settled.outcome is AttemptOutcome.APPLIED
    assert settled.outcome_digest == _digest("result")
    assert ledger.resolution_probe(_record(coordinator, accepted.operation_id)) == "applied"


def test_uncertain_outcome_stays_uncertain_for_the_probe(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "uncertain")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )

    result = service.report("owner-a", accepted.operation_id, _report(attempt, "uncertain"))
    assert result["outcome"] == "uncertain"
    assert result["applied"] is False
    assert ledger.resolution_probe(_record(coordinator, accepted.operation_id)) is None


def test_probe_clears_only_when_every_attempt_is_not_applied(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _accepted, claim = _submit_and_claim(coordinator, "probe")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    first = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments-1"),
    )
    second = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-2",
        argument_digest=_digest("arguments-2"),
    )
    record = _record(coordinator, _accepted.operation_id)
    assert ledger.resolution_probe(record) is None
    ledger.settle(
        partition="owner-a", operation_id=first.operation_id,
        nonce=first.attempt_nonce, outcome=AttemptOutcome.NOT_APPLIED,
        outcome_digest=None,
    )
    assert ledger.resolution_probe(record) is None
    ledger.settle(
        partition="owner-a", operation_id=second.operation_id,
        nonce=second.attempt_nonce, outcome=AttemptOutcome.NOT_APPLIED,
        outcome_digest=None,
    )
    assert ledger.resolution_probe(record) == "cleared"


def test_stale_reports_are_rejected_without_settling(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "stale-report")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )

    with pytest.raises(ReconciliationRefused, match="reconcile_report_stale"):
        service.report(
            "owner-a", accepted.operation_id,
            _report(attempt, "confirmed_not_applied", argument_digest=_digest("other")),
        )
    with pytest.raises(ReconciliationRefused, match="reconcile_report_stale"):
        service.report(
            "owner-a", accepted.operation_id,
            _report(attempt, "confirmed_not_applied", target_id="member-2"),
        )
    assert ledger.read(
        partition="owner-a",
        operation_id=accepted.operation_id,
        nonce=attempt.attempt_nonce,
    ).state == "bound"


def test_unknown_attempt_operation_and_owner_reports_are_not_found(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "unknown")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )

    stranger = LateResultReport(
        attempt_nonce="f" * 32,
        argument_digest=_digest("arguments"),
        target_kind="mesh_member",
        target_id="member-1",
        outcome="confirmed_not_applied",
    )
    with pytest.raises(ReconciliationRefused, match="reconcile_attempt_unknown"):
        service.report("owner-a", accepted.operation_id, stranger)
    with pytest.raises(ReconciliationRefused, match="work_not_found"):
        service.report("owner-a", uuid.uuid4(), _report(attempt, "confirmed_not_applied"))
    with pytest.raises(ReconciliationRefused, match="work_not_found"):
        service.report("owner-b", accepted.operation_id,
                       _report(attempt, "confirmed_not_applied"))
    with pytest.raises(ReconciliationRefused, match="reconcile_operation_invalid"):
        service.report("owner-a", "not-a-uuid", _report(attempt, "confirmed_not_applied"))
    with pytest.raises(ReconciliationRefused, match="work_authentication_required"):
        service.report(None, accepted.operation_id,
                       _report(attempt, "confirmed_not_applied"))


def test_revoked_or_unavailable_targets_are_refused_before_settlement(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "revoked")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger,
        target_authority=_RecordingTargetAuthority(unavailable=True),
    )

    with pytest.raises(ReconciliationRefused, match="reconcile_target_unavailable"):
        service.report("owner-a", accepted.operation_id,
                       _report(attempt, "confirmed_not_applied"))
    assert ledger.read(
        partition="owner-a",
        operation_id=accepted.operation_id,
        nonce=attempt.attempt_nonce,
    ).state == "bound"
    assert service._targets.calls == [("owner-a", "mesh_member", "member-1")]


def test_reports_are_refused_while_the_attempt_generation_still_holds_the_lease(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "current")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )

    with pytest.raises(ReconciliationRefused, match="reconcile_execution_current"):
        service.report("owner-a", accepted.operation_id,
                       _report(attempt, "confirmed_not_applied"))
    with pytest.raises(ReconciliationRefused, match="reconcile_execution_current"):
        service.report("owner-a", accepted.operation_id,
                       _report(attempt, "confirmed_applied"))


def test_reports_refuse_stale_generation_attempts_of_a_reselected_operation(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "reselected")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    coordinator.renew_execution_lease(claim.fence)
    next_fence = coordinator.reselect_execution(claim.fence)
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )

    with pytest.raises(ReconciliationRefused, match="reconcile_report_stale"):
        service.report("owner-a", accepted.operation_id, _report(attempt, "confirmed_not_applied"))
    assert next_fence.execution_generation > attempt.execution_generation


def test_terminal_operations_refuse_new_reports(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "terminal")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    coordinator.terminalize(
        claim.fence,
        state=OperationState.FAILED,
        terminal_code="executor_failed",
        safe_summary=None,
        retry_after_ms=None,
    )
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )

    with pytest.raises(ReconciliationRefused, match="reconcile_outcome_conflict"):
        service.report("owner-a", accepted.operation_id,
                       _report(attempt, "confirmed_not_applied"))


def test_settlement_conflict_surfaces_as_typed_unavailable(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "contended")
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    repository.fail_next_compare = True
    with pytest.raises(ReconciliationRefused, match="attempt_settlement_contended"):
        ledger.settle(
            partition="owner-a",
            operation_id=attempt.operation_id,
            nonce=attempt.attempt_nonce,
            outcome=AttemptOutcome.NOT_APPLIED,
            outcome_digest=None,
        )


def test_applied_outcome_requires_a_result_digest_and_others_forbid_one(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "digests")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )

    with pytest.raises(ReconciliationRefused, match="reconcile_report_invalid"):
        ledger.settle(
            partition="owner-a",
            operation_id=attempt.operation_id,
            nonce=attempt.attempt_nonce,
            outcome=AttemptOutcome.APPLIED,
            outcome_digest=None,
        )
    with pytest.raises(ReconciliationRefused, match="reconcile_report_invalid"):
        ledger.settle(
            partition="owner-a",
            operation_id=attempt.operation_id,
            nonce=attempt.attempt_nonce,
            outcome=AttemptOutcome.NOT_APPLIED,
            outcome_digest=_digest("result"),
        )


def test_report_models_reject_open_vocabulary(encryption_key):
    base = {
        "attempt_nonce": "a" * 32,
        "argument_digest": "a" * 64,
        "target_kind": "mesh_member",
        "target_id": "member-1",
        "outcome": "confirmed_applied",
        "result_digest": "b" * 64,
    }
    assert LateResultReport.model_validate(base).outcome == "confirmed_applied"
    for field_name, value in (
        ("attempt_nonce", "a" * 31),
        ("attempt_nonce", "A" * 32),
        ("argument_digest", "A" * 64),
        ("target_kind", "mesh member"),
        ("target_id", ""),
        ("target_id", "bad id"),
        ("outcome", "retry"),
        ("outcome", "confirmed_applied; drop"),
        ("result_digest", "b" * 63),
    ):
        fields = dict(base)
        fields[field_name] = value
        with pytest.raises(ValueError):
            LateResultReport.model_validate(fields)
    without_digest = dict(base)
    without_digest.pop("result_digest")
    with pytest.raises(ValueError):
        LateResultReport.model_validate(without_digest)
    not_applied = dict(base)
    not_applied["outcome"] = "confirmed_not_applied"
    with pytest.raises(ValueError):
        LateResultReport.model_validate(not_applied)


def _recovery_service(coordinator, ledger, *, probe=True, mode=ExecutorRecoveryMode.PRESERVE_UNSUPPORTED):
    return ExecutorRecoveryService(
        coordinator,
        stop_gate=lambda record: False,
        uncertainty_probe=ledger.resolution_probe if probe else None,
        mode=mode,
    )


def test_recovery_preserves_ledgered_effects_until_reconciliation(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "preserve")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))

    report = _recovery_service(coordinator, ledger, probe=False).recover_expired_executors()

    assert report.expired == 1
    assert report.count(RecoveryDisposition.PRESERVED_UNCERTAIN) == 1
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None


def test_recovery_reschedules_only_proven_not_applied_effects(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "cleared")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )
    service.report("owner-a", accepted.operation_id,
                   _report(attempt, "confirmed_not_applied"))
    recovery = _recovery_service(
        coordinator, ledger, mode=ExecutorRecoveryMode.ADMISSION_METADATA_ONLY
    )

    report = recovery.recover_expired_executors()

    assert report.count(RecoveryDisposition.RESCHEDULED) == 1
    rescheduled = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert rescheduled is not None
    child = _record(coordinator, rescheduled.fence.operation_id)
    assert child.parent_operation_id == accepted.operation_id


def test_recovery_never_reschedules_proven_applied_effects(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted, claim = _submit_and_claim(coordinator, "resolved")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_RecordingTargetAuthority()
    )
    service.report("owner-a", accepted.operation_id,
                   _report(attempt, "confirmed_applied"))
    recovery = _recovery_service(
        coordinator, ledger, mode=ExecutorRecoveryMode.ADMISSION_METADATA_ONLY
    )

    report = recovery.recover_expired_executors()

    assert report.count(RecoveryDisposition.RESOLVED_APPLIED) == 1
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None


def test_probe_failures_fail_closed_to_preservation(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _accepted, claim = _submit_and_claim(coordinator, "failclosed")
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=31))

    def broken_probe(record):
        raise RuntimeError("ledger unavailable")

    recovery = ExecutorRecoveryService(
        coordinator,
        stop_gate=lambda record: False,
        uncertainty_probe=broken_probe,
        mode=ExecutorRecoveryMode.ADMISSION_METADATA_ONLY,
    )
    report = recovery.recover_expired_executors()
    assert report.count(RecoveryDisposition.PRESERVED_UNCERTAIN) == 1


def test_corrupt_unresolved_row_never_clears_other_nonapplication(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)
    accepted, claim = _submit_and_claim(coordinator, "incomplete")
    first = ledger.bind(claim.fence, target_kind="mesh_member", target_id="member-1",
                        argument_digest=_digest("first"))
    second = ledger.bind(claim.fence, target_kind="mesh_member", target_id="member-2",
                         argument_digest=_digest("second"))
    ledger.settle(partition="owner-a", operation_id=accepted.operation_id,
                  nonce=first.attempt_nonce, outcome=AttemptOutcome.NOT_APPLIED,
                  outcome_digest=None)
    repository._rows[("owner-a", "work.effect-attempts.v1", second.attempt_nonce)].encrypted_value = "broken"
    assert ledger.resolution_probe(_record(coordinator, accepted.operation_id)) != "cleared"


def test_uncertainty_refines_to_conclusive_owner_attestation(encryption_key, monkeypatch):
    monkeypatch.setattr("orchestrator.work_reconciliation._now_ms", lambda: 1000)
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    accepted, claim = _submit_and_claim(coordinator, "refine")
    attempt = ledger.bind(claim.fence, target_kind="mesh_member", target_id="member-1",
                          argument_digest=_digest("first"))
    clock.advance(timedelta(seconds=31))
    coordinator.expire_execution_leases()
    service = EffectReconciliationService(ledger=ledger, target_authority=_RecordingTargetAuthority())
    service.report("owner-a", accepted.operation_id, _report(attempt, "uncertain"))
    result = service.report("owner-a", accepted.operation_id, _report(attempt, "confirmed_applied"))
    assert result["applied"] is True
    settled = ledger.read(partition="owner-a", operation_id=accepted.operation_id, nonce=attempt.attempt_nonce)
    assert settled.revision > attempt.revision


@pytest.mark.parametrize("field,value", [
    ("version", 3), ("version", True), ("execution_generation", 0),
    ("execution_generation", "1"), ("argument_digest", "bad"),
    ("attempt_nonce", "a" * 32), ("target_kind", "unbounded!"),
    ("target_id", "bad target"), ("target_id", 42),
    ("argument_digest", int("1" * 64)), ("state", "applied"),
    ("outcome", "applied"), ("outcome_digest", "a" * 64),
    ("settled_at", None), ("revision", 0), ("recorded_at", -1),
])
def test_semantically_invalid_inventory_cannot_resolve_or_bind(encryption_key, field, value):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    repository = _FakeCredentialRepository()
    ledger = _ledger(clock, coordinator, repository)
    accepted, claim = _submit_and_claim(coordinator, "invalid-record")
    attempt = ledger.bind(claim.fence, target_kind="mesh_member", target_id="member-1",
                          argument_digest=_digest("arguments"))
    ledger.settle(partition="owner-a", operation_id=accepted.operation_id, nonce=attempt.attempt_nonce,
                  outcome=AttemptOutcome.NOT_APPLIED, outcome_digest=None)
    row = repository._rows[("owner-a", "work.effect-attempts.v1", attempt.attempt_nonce)]
    value_record = me._decode_record(row.encrypted_value)
    value_record[field] = value
    row.encrypted_value = me._encode_record(value_record)
    assert ledger.resolution_probe(_record(coordinator, accepted.operation_id)) is None
    assert ledger.preservation_probe(_record(coordinator, accepted.operation_id)) == "uncertain"
    with pytest.raises(ReconciliationRefused, match="attempt_inventory_invalid"):
        ledger.bind(claim.fence, target_kind="mesh_member", target_id="member-2",
                    argument_digest=_digest("next"))
    assert len(repository._rows) == 1


def test_mixed_applied_and_unresolved_attempts_never_imply_complete_resolution(encryption_key):
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    ledger = _ledger(clock, coordinator, _FakeCredentialRepository())
    accepted, claim = _submit_and_claim(coordinator, "mixed")
    attempt = ledger.bind(claim.fence, target_kind="mesh_member", target_id="member-1",
                          argument_digest=_digest("arguments"))
    ledger.bind(claim.fence, target_kind="mesh_member", target_id="member-2",
                argument_digest=_digest("arguments"))
    ledger.settle(partition="owner-a", operation_id=accepted.operation_id, nonce=attempt.attempt_nonce,
                  outcome=AttemptOutcome.APPLIED, outcome_digest=_digest("result"))
    assert ledger.resolution_probe(_record(coordinator, accepted.operation_id)) is None
    assert ledger.preservation_probe(_record(coordinator, accepted.operation_id)) == "uncertain"
    coordinator.update_phase(claim.fence, "running")
    clock.advance(timedelta(seconds=31))
    recovery = ExecutorRecoveryService(coordinator, stop_gate=lambda record: False,
        uncertainty_probe=ledger.preservation_probe, mode=ExecutorRecoveryMode.ADMISSION_METADATA_ONLY)
    report = recovery.recover_expired_executors()
    assert report.recovered[0].disposition is RecoveryDisposition.PRESERVED_UNCERTAIN
