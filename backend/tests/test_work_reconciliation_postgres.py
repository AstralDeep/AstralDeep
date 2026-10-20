"""Real-PostgreSQL reconciliation regressions: two coordinator nodes share one durable
store, a lost executor's ledgered effect stays preserved until an authenticated late
result reconciles it, and revoked targets or stale reports never duplicate effects.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterator

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from orchestrator import mesh_enrollment as me
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    WorkAdmissionCoordinator,
)
from orchestrator.work_reconciliation import (
    AttemptOutcome,
    EffectAttemptLedger,
    EffectReconciliationService,
    MeshTargetAuthority,
    ReconciliationRefused,
)
from orchestrator.work_recovery import (
    ExecutorRecoveryMode,
    ExecutorRecoveryService,
    RecoveryDisposition,
)
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


@dataclass
class _FakeClock:
    current: datetime = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _classes():
    return (
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=4,
            queue_limit=0,
            max_wait_ms=None,
            config_revision="test-reconcile-pg",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.INTERACTIVE,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=4,
            queue_limit=2,
            max_wait_ms=5_000,
            config_revision="test-reconcile-pg",
        ),
    )


def _node(runtime: PlaneTestRuntime, clock: _FakeClock) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=_classes(),
        repository=_plane_repository(runtime),
        clock=clock,
        operation_retention=timedelta(hours=24),
    )


def _plane_repository(runtime: PlaneTestRuntime):
    from orchestrator.work_admission import PlaneWorkAdmissionRepository

    return PlaneWorkAdmissionRepository(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


def _ledger(runtime: PlaneTestRuntime, coordinator) -> EffectAttemptLedger:
    from audit.repository import AuditRepository
    from orchestrator.plane_repository_context import PlaneRepositoryContext

    return EffectAttemptLedger(
        coordinator=coordinator,
        source=PlaneRepositoryContext(
            repository=runtime.repositories.credentials, plane_runtime=runtime
        ),
        audit_repository=AuditRepository(
            plane_runtime=runtime, plane_repositories=runtime.repositories
        ),
    )


def _target_authority(runtime: PlaneTestRuntime) -> MeshTargetAuthority:
    from audit.repository import AuditRepository

    return MeshTargetAuthority(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
        audit_repository=AuditRepository(
            plane_runtime=runtime, plane_repositories=runtime.repositories
        ),
    )


def _enrollment_store(runtime: PlaneTestRuntime) -> me.MeshEnrollmentStore:
    from audit.repository import AuditRepository

    return me.MeshEnrollmentStore(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
        audit_repository=AuditRepository(
            plane_runtime=runtime, plane_repositories=runtime.repositories
        ),
    )


def _enroll_member(store: me.MeshEnrollmentStore, owner_id: str, label: str) -> str:
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": me._b64url_encode(raw)}
    invitation = store.create_invitation(
        owner_id,
        label=label,
        device_key=jwk,
        scopes=["tools:read"],
        creator_kind="owner",
        creator_id=owner_id,
    )
    fields = me.parse_enrollment_payload(invitation["payload"])
    store.decide_invitation(
        owner_id, fields["invite_id"], decision="confirmed", decider_kind="owner",
        decider_id=owner_id,
    )
    challenge = fields["challenge"]
    signature = me._b64url_encode(
        private_key.sign(me._b64url_decode(challenge))
    )
    redeemed = store.redeem_invitation(
        owner_id=owner_id,
        invite_id=fields["invite_id"],
        token=fields["token"],
        challenge=challenge,
        signature=signature,
    )
    return redeemed["member"]["member_id"]


def _submit_and_claim(coordinator: WorkAdmissionCoordinator, label: str, owner: str):
    submission_id = uuid.uuid4()
    accepted = coordinator.submit(
        OperationRequest(
            operation_kind="connection_frame",
            admission_class=AdmissionClass.INTERACTIVE,
            owner=OperationOwner(OwnerScope.USER, owner, None),
            submission_id=submission_id,
            idempotency_namespace="reconcile_pg",
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


def _report(attempt, outcome: str, *, argument_digest: str | None = None,
            target_id: str | None = None, result_digest: str | None = None):
    from orchestrator.work_reconciliation import LateResultReport

    return LateResultReport(
        attempt_nonce=attempt.attempt_nonce,
        argument_digest=argument_digest or attempt.argument_digest,
        target_kind=attempt.target_kind,
        target_id=target_id or attempt.target_id,
        outcome=outcome,
        result_digest=result_digest
        or (_digest("result") if outcome == "confirmed_applied" else None),
    )


def _service(coordinator, ledger, *, mode=ExecutorRecoveryMode.PRESERVE_UNSUPPORTED):
    return ExecutorRecoveryService(
        coordinator,
        stop_gate=lambda record: False,
        uncertainty_probe=ledger.resolution_probe,
        mode=mode,
    )


def test_remote_machine_targets_are_verified_against_the_owner_inventory(
    clean_database: PlaneTestRuntime, encryption_key
) -> None:
    from astralplane.repositories.remote import RemoteMachine

    clock = _FakeClock()
    node = _node(clean_database, clock)
    runtime = clean_database
    accepted, claim = _submit_and_claim(node, "machine-target", "owner-a")
    ledger = _ledger(runtime, node)
    machine_id = "machine-" + uuid.uuid4().hex[:16]
    now_ms = int(clock.current.timestamp() * 1000)
    with runtime.transaction() as transaction:
        runtime.repositories.remote.create_machine(
            transaction,
            RemoteMachine(
                machine_id=machine_id,
                owner_id="owner-a",
                label="Lab box",
                address="lab.example.org",
                port=22,
                username="astral",
                os_family="linux",
                role="plain",
                host_key_type=None,
                host_key_fingerprint=None,
                host_key_blob=None,
                last_verdict=None,
                last_checked_at=None,
                created_at=now_ms,
                updated_at=now_ms,
            ),
        )
    attempt = ledger.bind(
        claim.fence, target_kind="remote_machine", target_id=machine_id,
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=30))
    node.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_target_authority(runtime)
    )
    result = service.report("owner-a", accepted.operation_id,
                            _report(attempt, "confirmed_not_applied"))
    assert result["outcome"] == "confirmed_not_applied"

    with runtime.transaction() as transaction:
        runtime.repositories.remote.delete_machine(
            transaction, owner_id="owner-a", machine_id=machine_id
        )
    removed = _submit_and_claim(node, "machine-target-2", "owner-a")
    removed_attempt = ledger.bind(
        removed[1].fence, target_kind="remote_machine", target_id=machine_id,
        argument_digest=_digest("arguments"),
    )
    node.expire_execution_leases()
    with pytest.raises(ReconciliationRefused, match="reconcile_target_unavailable"):
        service.report("owner-a", removed[0].operation_id,
                       _report(removed_attempt, "confirmed_not_applied"))

    unknown_kind = _submit_and_claim(node, "machine-target-3", "owner-a")
    unknown_attempt = ledger.bind(
        unknown_kind[1].fence, target_kind="orbital_platform", target_id="station-1",
        argument_digest=_digest("arguments"),
    )
    node.expire_execution_leases()
    with pytest.raises(ReconciliationRefused, match="reconcile_target_unavailable"):
        service.report("owner-a", unknown_kind[0].operation_id,
                       _report(unknown_attempt, "confirmed_not_applied"))


def test_unavailable_target_authority_fails_closed_without_settlement(
    clean_database: PlaneTestRuntime, encryption_key, monkeypatch
) -> None:
    clock = _FakeClock()
    node = _node(clean_database, clock)
    runtime = clean_database
    member_id = _enroll_member(_enrollment_store(runtime), "owner-a", "Flaky")
    accepted, claim = _submit_and_claim(node, "authority-failure", "owner-a")
    ledger = _ledger(runtime, node)
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id=member_id,
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=30))
    node.expire_execution_leases()
    authority = _target_authority(runtime)

    def broken_current_member(transaction, owner_id, member_id, **kwargs):
        raise RuntimeError("membership store unavailable")

    monkeypatch.setattr(
        authority._store, "_current_member", broken_current_member
    )
    service = EffectReconciliationService(ledger=ledger, target_authority=authority)
    with pytest.raises(ReconciliationRefused, match="reconcile_authority_unavailable"):
        service.report("owner-a", accepted.operation_id,
                       _report(attempt, "confirmed_not_applied"))
    assert ledger.read(
        partition="owner-a",
        operation_id=accepted.operation_id,
        nonce=attempt.attempt_nonce,
    ).state == "bound"


def test_audit_failures_roll_back_binding_and_settlement(
    clean_database: PlaneTestRuntime, encryption_key, monkeypatch
) -> None:
    clock = _FakeClock()
    node = _node(clean_database, clock)
    runtime = clean_database
    accepted, claim = _submit_and_claim(node, "audit-failure", "owner-a")
    ledger = _ledger(runtime, node)
    original = ledger._audit.insert_in_transaction

    def failing_insert(event, *, transaction, plane_runtime):
        raise RuntimeError("audit writer unavailable")

    monkeypatch.setattr(ledger._audit, "insert_in_transaction", failing_insert)
    with pytest.raises(ReconciliationRefused, match="reconcile_audit_unavailable"):
        ledger.bind(
            claim.fence, target_kind="mesh_member", target_id="member-1",
            argument_digest=_digest("arguments"),
        )
    assert ledger.list_for_operation(
        partition="owner-a", operation_id=accepted.operation_id
    ) == ()

    def none_insert(event, *, transaction, plane_runtime):
        return None

    monkeypatch.setattr(ledger._audit, "insert_in_transaction", none_insert)
    with pytest.raises(ReconciliationRefused, match="reconcile_audit_unavailable"):
        ledger.bind(
            claim.fence, target_kind="mesh_member", target_id="member-1",
            argument_digest=_digest("arguments"),
        )
    assert ledger.list_for_operation(
        partition="owner-a", operation_id=accepted.operation_id
    ) == ()
    monkeypatch.setattr(ledger._audit, "insert_in_transaction", original)
    ledger.bind(
        claim.fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    assert len(ledger.list_for_operation(
        partition="owner-a", operation_id=accepted.operation_id
    )) == 1


def test_unavailable_remote_machine_inventory_fails_closed(
    clean_database: PlaneTestRuntime, encryption_key, monkeypatch
) -> None:
    clock = _FakeClock()
    node = _node(clean_database, clock)
    runtime = clean_database
    accepted, claim = _submit_and_claim(node, "machine-failure", "owner-a")
    ledger = _ledger(runtime, node)
    attempt = ledger.bind(
        claim.fence, target_kind="remote_machine", target_id="machine-x",
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=30))
    node.expire_execution_leases()
    authority = _target_authority(runtime)

    def failing_get_machine(transaction, *, owner_id, machine_id):
        raise RuntimeError("machine inventory unavailable")

    monkeypatch.setattr(authority._remote, "get_machine", failing_get_machine)
    service = EffectReconciliationService(ledger=ledger, target_authority=authority)
    with pytest.raises(ReconciliationRefused, match="reconcile_authority_unavailable"):
        service.report("owner-a", accepted.operation_id,
                       _report(attempt, "confirmed_not_applied"))
    assert ledger.read(
        partition="owner-a",
        operation_id=accepted.operation_id,
        nonce=attempt.attempt_nonce,
    ).state == "bound"


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[PlaneTestRuntime]:
    with isolated_plane_runtime("work_reconciliation") as runtime:
        yield runtime


@pytest.fixture
def clean_database(postgres_database: PlaneTestRuntime) -> PlaneTestRuntime:
    postgres_database.execute("DELETE FROM operation_submission_result")
    postgres_database.execute(
        "UPDATE operation_admission_slot SET operation_id=NULL, lease_token=NULL, lease_expires_at=NULL"
    )
    postgres_database.execute("DELETE FROM operation_record")
    postgres_database.execute("DELETE FROM user_credentials")
    with postgres_database.transaction() as transaction:
        transaction.execute("SET LOCAL audit.allow_purge = 'true'")
        transaction.execute("DELETE FROM audit_events")
    postgres_database.execute("DELETE FROM mesh_enrollment_challenge")
    postgres_database.execute("DELETE FROM mesh_enrollment_invitation")
    postgres_database.execute("DELETE FROM mesh_member_revocation")
    postgres_database.execute("DELETE FROM mesh_public_identity")
    postgres_database.execute("DELETE FROM mesh_member")
    postgres_database.execute("DELETE FROM mesh_record")
    return postgres_database


@pytest.fixture
def encryption_key(monkeypatch):
    monkeypatch.setenv(
        "CREDENTIAL_ENCRYPTION_KEY", "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ="
    )


def test_lost_executor_node_effect_is_reconciled_by_a_late_applied_result(
    clean_database: PlaneTestRuntime, encryption_key
) -> None:
    clock = _FakeClock()
    node_a = _node(clean_database, clock)
    node_b = _node(clean_database, clock)
    runtime = clean_database
    member_id = _enroll_member(_enrollment_store(runtime), "owner-a", "Tablet")
    accepted, claim = _submit_and_claim(node_a, "lost-executor", "owner-a")
    ledger_a = _ledger(runtime, node_a)
    attempt = ledger_a.bind(
        claim.fence, target_kind="mesh_member", target_id=member_id,
        argument_digest=_digest("arguments"),
    )
    assert attempt.state == "bound"

    clock.advance(timedelta(seconds=30))
    preserved = _service(node_b, _ledger(runtime, node_b)).recover_expired_executors()
    assert preserved.count(RecoveryDisposition.PRESERVED_UNCERTAIN) == 1

    service = EffectReconciliationService(
        ledger=_ledger(runtime, node_b), target_authority=_target_authority(runtime)
    )
    result = service.report("owner-a", accepted.operation_id,
                            _report(attempt, "confirmed_applied"))
    assert result["applied"] is True
    assert result["operation_state"] == OperationState.RETRYABLE.value

    resolved = _service(
        node_b, _ledger(runtime, node_b)
    ).recover_expired_executors()
    assert resolved.count(RecoveryDisposition.RESOLVED_APPLIED) == 1
    assert node_b.claim_next(AdmissionClass.INTERACTIVE) is None

    rows = clean_database.fetch_all(
        "SELECT action_type FROM audit_events WHERE actor_user_id=%s "
        "AND action_type IN ('work.effect_attempt.bound','work.effect_attempt.settled') "
        "ORDER BY recorded_at, event_id",
        ("owner-a",),
    )
    assert [row["action_type"] for row in rows] == [
        "work.effect_attempt.bound",
        "work.effect_attempt.settled",
    ]


def test_confirmed_not_applied_lets_supported_recovery_reschedule_without_duplication(
    clean_database: PlaneTestRuntime, encryption_key
) -> None:
    clock = _FakeClock()
    node_a = _node(clean_database, clock)
    node_b = _node(clean_database, clock)
    runtime = clean_database
    member_id = _enroll_member(_enrollment_store(runtime), "owner-a", "Bench")
    accepted, claim = _submit_and_claim(node_a, "interrupted", "owner-a")
    ledger_a = _ledger(runtime, node_a)
    attempt = ledger_a.bind(
        claim.fence, target_kind="mesh_member", target_id=member_id,
        argument_digest=_digest("arguments"),
    )

    clock.advance(timedelta(seconds=30))
    ledger_b = _ledger(runtime, node_b)
    _service(node_b, ledger_b).recover_expired_executors()
    service = EffectReconciliationService(
        ledger=ledger_b, target_authority=_target_authority(runtime)
    )
    settled = service.report("owner-a", accepted.operation_id,
                             _report(attempt, "confirmed_not_applied"))
    assert settled["outcome"] == "confirmed_not_applied"

    rescheduled = _service(
        node_b, ledger_b, mode=ExecutorRecoveryMode.ADMISSION_METADATA_ONLY
    ).recover_expired_executors()
    assert rescheduled.count(RecoveryDisposition.RESCHEDULED) == 1
    replacement = node_b.claim_next(AdmissionClass.INTERACTIVE)
    assert replacement is not None
    child = node_b.repository.get_operation_for_administration(
        replacement.fence.operation_id
    )
    assert child.parent_operation_id == accepted.operation_id

    with pytest.raises(ReconciliationRefused, match="reconcile_outcome_conflict"):
        service.report("owner-a", accepted.operation_id,
                       _report(attempt, "confirmed_applied"))


def test_stale_and_revoked_member_reports_never_settle(
    clean_database: PlaneTestRuntime, encryption_key
) -> None:
    clock = _FakeClock()
    node_a = _node(clean_database, clock)
    runtime = clean_database
    store = _enrollment_store(runtime)
    member_id = _enroll_member(store, "owner-a", "Revocable")
    accepted, claim = _submit_and_claim(node_a, "revoked", "owner-a")
    ledger = _ledger(runtime, node_a)
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id=member_id,
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=30))
    node_a.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_target_authority(runtime)
    )

    with pytest.raises(ReconciliationRefused, match="reconcile_report_stale"):
        service.report(
            "owner-a", accepted.operation_id,
            _report(attempt, "confirmed_not_applied", argument_digest=_digest("wrong")),
        )

    store.revoke_member("owner-a", member_id)
    with pytest.raises(ReconciliationRefused, match="reconcile_target_unavailable"):
        service.report("owner-a", accepted.operation_id,
                       _report(attempt, "confirmed_not_applied"))
    current = ledger.read(
        partition="owner-a",
        operation_id=accepted.operation_id,
        nonce=attempt.attempt_nonce,
    )
    assert current.state == "bound"


def test_reports_from_another_owner_are_invisible(
    clean_database: PlaneTestRuntime, encryption_key
) -> None:
    clock = _FakeClock()
    node = _node(clean_database, clock)
    runtime = clean_database
    member_id = _enroll_member(_enrollment_store(runtime), "owner-a", "Private")
    accepted, claim = _submit_and_claim(node, "private", "owner-a")
    ledger = _ledger(runtime, node)
    attempt = ledger.bind(
        claim.fence, target_kind="mesh_member", target_id=member_id,
        argument_digest=_digest("arguments"),
    )
    clock.advance(timedelta(seconds=30))
    node.expire_execution_leases()
    service = EffectReconciliationService(
        ledger=ledger, target_authority=_target_authority(runtime)
    )

    with pytest.raises(ReconciliationRefused, match="work_not_found"):
        service.report("owner-b", accepted.operation_id,
                       _report(attempt, "confirmed_not_applied"))


def test_binding_is_impossible_without_a_current_fence(
    clean_database: PlaneTestRuntime, encryption_key
) -> None:
    clock = _FakeClock()
    node_a = _node(clean_database, clock)
    node_b = _node(clean_database, clock)
    accepted, claim = _submit_and_claim(node_a, "stale-fence", "owner-a")
    ledger_b = _ledger(clean_database, node_b)
    from orchestrator.work_admission import StaleExecutionFenceError

    clock.advance(timedelta(seconds=30))
    node_b.expire_execution_leases()
    with pytest.raises(StaleExecutionFenceError):
        ledger_b.bind(
            claim.fence, target_kind="mesh_member", target_id="member-x",
            argument_digest=_digest("arguments"),
        )
    assert node_b.repository.get_operation_for_administration(
        accepted.operation_id
    ).state is OperationState.RETRYABLE


def test_purged_operations_prune_their_ledger_rows_on_the_next_bind(
    clean_database: PlaneTestRuntime, encryption_key
) -> None:
    clock = _FakeClock()
    node = _node(clean_database, clock)
    runtime = clean_database
    accepted, claim = _submit_and_claim(node, "purged", "owner-a")
    ledger = _ledger(runtime, node)
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
    clock.advance(timedelta(seconds=30))
    node.expire_execution_leases()
    node.repository.purge_expired(
        now=clock.current + timedelta(hours=25), limit=100
    )
    second = _submit_and_claim(node, "fresh", "owner-a")
    ledger.bind(
        second[1].fence, target_kind="mesh_member", target_id="member-1",
        argument_digest=_digest("arguments"),
    )
    remaining = ledger.list_for_operation(
        partition="owner-a", operation_id=accepted.operation_id
    )
    assert remaining == ()
