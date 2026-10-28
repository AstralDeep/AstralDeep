"""Verifies owner-attested effect refinement under the real current-human transaction guard.
Signed local IAM fixtures and Plane records exercise revocation, immutable outcomes and final-check rollback.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from orchestrator import remote_machines
from orchestrator.credential_manager import CredentialManager
from orchestrator.work_admission import OperationState
from orchestrator.work_reconciliation import EffectReconciliationService, AttemptOutcome, ReconciliationRefused
from persistent_agents.models import AssignmentError
from tests.test_human_request_authority_088 import human as human, selected
from tests.test_work_control_authority_088 import (
    bound as bound, fixture as fixture, runtime as runtime, service as service, signing_key as signing_key,
)
from tests.test_work_reconciliation_postgres import _node, _ledger, _submit_and_claim, _target_authority, _report

pytestmark = pytest.mark.asyncio


@pytest.fixture
def effect(human, fixture, runtime, request):
    owner = fixture[1]
    source = SimpleNamespace(plane_runtime=runtime, plane_repositories=runtime.repositories)
    credentials = CredentialManager(db=source)
    machine = remote_machines.create_machine(source, owner, "attestation", "ssh.test", 22, "runner", "linux", "plain")
    credentials.set_machine_credential(machine, owner, "password", "fixture")
    remote_machines.record_probe(source, owner, machine, "ok", {"type": "ssh-ed25519", "fingerprint": "SHA256:fixture", "blob_b64": "fixture"})
    target = remote_machines.build_effect_target(source, credentials, owner, machine)
    node = _node(runtime, lambda: datetime.now(timezone.utc))
    accepted, claim = _submit_and_claim(node, uuid.uuid4().hex, owner)
    with runtime.transaction() as tx:
        runtime.repositories.stop_epochs.bind_operation(tx, owner_id=owner, operation_id=accepted.operation_id)
    ledger = _ledger(runtime, node)
    options = getattr(request, "param", {})
    qualified = options.get("qualified", True)
    attempt = ledger.bind(claim.fence, target_kind="remote_machine", target_id=machine,
        argument_digest="a" * 64, dispatch_id=uuid.uuid4().hex if qualified else None,
        authority_digest=target.authority_digest if qualified else None)
    node.terminalize(claim.fence, state=options.get("state", OperationState.RETRYABLE), terminal_code="effect_uncertain",
                     safe_summary="Uncertain", retry_after_ms=None)
    reports = EffectReconciliationService(ledger=ledger, target_authority=_target_authority(runtime))
    return SimpleNamespace(ledger=ledger, reports=reports, attempt=attempt, owner=owner,
        source=source, credentials=credentials, machine=machine, orch=human[2], node=node, claim=claim)


async def test_current_owner_can_refine_uncertainty_and_repeat_immutable_result(effect, human, bound, fixture):
    caller = await selected(human, bound, fixture)
    for value in ("uncertain", "confirmed_applied", "confirmed_applied"):
        result = await effect.reports.report_current(caller, effect.attempt.operation_id,
            _report(effect.attempt, value), orchestrator=effect.orch)
        assert result["outcome"] == value
    with pytest.raises(AssignmentError, match="reconcile_outcome_conflict"):
        await effect.reports.report_current(caller, effect.attempt.operation_id,
            _report(effect.attempt, "confirmed_not_applied"), orchestrator=effect.orch)
    assert effect.node.repository.get_operation_for_administration(effect.attempt.operation_id).state is OperationState.RETRYABLE


async def test_owner_attestation_requires_current_human_even_for_replay(effect, human, bound, fixture):
    caller = await selected(human, bound, fixture)
    await effect.reports.report_current(caller, effect.attempt.operation_id,
        _report(effect.attempt, "confirmed_applied"), orchestrator=effect.orch)
    effect.credentials.delete_machine_credential(effect.machine, effect.owner)
    with pytest.raises(AssignmentError):
        await effect.reports.report_current(caller, effect.attempt.operation_id,
            _report(effect.attempt, "confirmed_applied"), orchestrator=effect.orch)
    with pytest.raises(ReconciliationRefused, match="effect_current_authority_unavailable"):
        effect.reports.report(effect.owner, effect.attempt.operation_id, _report(effect.attempt, "confirmed_applied"))


async def test_owner_session_loss_during_audit_rolls_back_attestation(effect, human, bound, fixture, runtime, monkeypatch):
    caller = await selected(human, bound, fixture)
    original = effect.ledger._audit.insert_in_transaction

    def revoke(event, *, transaction, plane_runtime):
        receipt = original(event, transaction=transaction, plane_runtime=plane_runtime)
        credential = caller.require_session().credential
        runtime.repositories.history.sessions.delete_and_return(transaction,
            owner_id=credential.owner_id, session_id=credential.session_id,
            expected_incarnation_id=credential.incarnation_id)
        return receipt

    monkeypatch.setattr(effect.ledger._audit, "insert_in_transaction", revoke)
    with pytest.raises(AssignmentError):
        await effect.reports.report_current(caller, effect.attempt.operation_id,
            _report(effect.attempt, "confirmed_applied"), orchestrator=effect.orch)
    record = effect.ledger.read(partition=effect.owner, operation_id=effect.attempt.operation_id,
                                nonce=effect.attempt.attempt_nonce)
    assert record.outcome is not AttemptOutcome.APPLIED
    await caller.transaction(lambda tx, _: None, expected_orchestrator=effect.orch)


@pytest.mark.parametrize("failure", ["missing_audit", "target_during_audit"])
async def test_owner_attestation_requires_final_target_and_audit(effect, human, bound, fixture, runtime, monkeypatch, failure):
    caller = await selected(human, bound, fixture)
    original = effect.ledger._audit.insert_in_transaction

    def revoke(event, *, transaction, plane_runtime):
        receipt = original(event, transaction=transaction, plane_runtime=plane_runtime)
        runtime.repositories.credentials.delete_machine_credential(transaction,
            owner_id=effect.owner, machine_id=effect.machine)
        return receipt

    if failure == "missing_audit":
        monkeypatch.setattr(effect.ledger, "_audit", None)
    else:
        monkeypatch.setattr(effect.ledger._audit, "insert_in_transaction", revoke)
    with pytest.raises((AssignmentError, ReconciliationRefused)):
        await effect.reports.report_current(caller, effect.attempt.operation_id,
            _report(effect.attempt, "confirmed_applied"), orchestrator=effect.orch)
    record = effect.ledger.read(partition=effect.owner, operation_id=effect.attempt.operation_id,
                                nonce=effect.attempt.attempt_nonce)
    assert record.outcome is not AttemptOutcome.APPLIED
    assert effect.credentials.get_machine_credential(effect.machine, effect.owner)


@pytest.mark.parametrize("effect", [{"state": OperationState.CANCELLED}], indirect=True)
async def test_cancelled_qualified_attempt_allows_report_only_refinement(effect, human, bound, fixture):
    caller = await selected(human, bound, fixture)
    for value in ("uncertain", "confirmed_applied", "confirmed_applied"):
        result = await effect.reports.report_current(caller, effect.attempt.operation_id,
            _report(effect.attempt, value), orchestrator=effect.orch)
        assert result["outcome"] == value and result["operation_state"] == "cancelled"
    assert effect.node.repository.get_operation_for_administration(effect.attempt.operation_id).state is OperationState.CANCELLED
    assert len(effect.ledger.list_for_operation(partition=effect.owner, operation_id=effect.attempt.operation_id)) == 1


@pytest.mark.parametrize("effect", [{"state": OperationState.CANCELLED, "qualified": False}], indirect=True)
async def test_cancelled_legacy_attempt_keeps_its_existing_report_denial(effect, human, bound, fixture):
    caller = await selected(human, bound, fixture)
    with pytest.raises(AssignmentError, match="reconcile_outcome_conflict"):
        await effect.reports.report_current(caller, effect.attempt.operation_id,
            _report(effect.attempt, "confirmed_applied"), orchestrator=effect.orch)


@pytest.mark.parametrize("effect", [{"state": OperationState.CANCELLED}], indirect=True)
@pytest.mark.parametrize("at_audit", [False, True])
async def test_cancelled_report_rechecks_stop_and_rolls_back(effect, human, bound, fixture, runtime, monkeypatch, at_audit):
    caller = await selected(human, bound, fixture)
    original = effect.ledger._audit.insert_in_transaction

    def engage(tx):
        repository = runtime.repositories.stop_epochs
        state = repository.get(tx, owner_id=effect.owner)
        repository.engage(tx, owner_id=effect.owner, expected_revision=0 if state is None else state.revision,
            reason="fixture", actor_id=effect.owner, at=datetime.now(timezone.utc))

    def auditing(event, *, transaction, plane_runtime):
        receipt = original(event, transaction=transaction, plane_runtime=plane_runtime)
        engage(transaction)
        return receipt

    if at_audit:
        monkeypatch.setattr(effect.ledger._audit, "insert_in_transaction", auditing)
    else:
        with runtime.transaction() as tx:
            engage(tx)
    with pytest.raises(AssignmentError, match="reconcile_execution_stopped"):
        await effect.reports.report_current(caller, effect.attempt.operation_id,
            _report(effect.attempt, "confirmed_applied"), orchestrator=effect.orch)
    assert not effect.ledger.read(partition=effect.owner, operation_id=effect.attempt.operation_id,
        nonce=effect.attempt.attempt_nonce).settled
