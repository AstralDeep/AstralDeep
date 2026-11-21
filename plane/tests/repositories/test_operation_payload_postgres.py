"""Real-PostgreSQL tests for astralplane.repositories.assignments and work_admission:
transient input never persists private payload fields, and result/retirement
disposition share one owner lock and settle exactly once.
"""

import hashlib
import hmac
import traceback
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from test_assignments_postgres import (
    action,
    create,
    create_operation,
    expire_claim,
    parallel_transactions,
    reserve,
    session_observation,
    start,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import control, current, operation_claim
from test_operation_terminal_postgres import action_bytes, change_action, expire_authority

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.assignments import (
    AssignmentActionOutcome,
    AssignmentInputReference,
    AssignmentOperationBinding,
    AssignmentResourceAmount,
    AssignmentResultDisposition,
    AssignmentTransientInput,
    canonical,
    digest,
    plain,
)
from astralplane.repositories.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionRepository,
)

PRIVATE = "PRIVATE_NOTE_088_DO_NOT_RETAIN patient's confidential working text"
SOURCE = "DISCARDED_SOURCE_088 private full extraction text"


def keyed(text):
    return hmac.new(b"synthetic-test-only-key", text.encode(), hashlib.sha256).hexdigest()


def payload(**changes):
    values = dict(
        binding_key_id="synthetic-key-v1",
        payload_binding=keyed(PRIVATE),
        source_retention="none",
        references=(AssignmentInputReference("note", uid(), 1),),
    )
    values.update(changes)
    return AssignmentTransientInput(**values)


def admission(repo, tx, claim, *, owner="owner"):
    work = WorkAdmissionRepository()
    configs = (
        AdmissionClassConfig(AdmissionClass.GLOBAL, None, 100, 0, 0, "t025-tests"),
        AdmissionClassConfig(
            AdmissionClass.BACKGROUND, AdmissionClass.GLOBAL, 100, 0, 0, "t025-tests"
        ),
    )
    work.configure(tx, configs)
    work.bind_configs(configs)
    accepted = work.submit(
        tx,
        OperationRequest(
            operation_kind="assignment_episode",
            admission_class=AdmissionClass.BACKGROUND,
            owner=OperationOwner(OwnerScope.USER, owner, None),
            submission_id=uuid.uuid4(),
            idempotency_namespace=None,
            idempotency_key=None,
            normalized_input_digest=None,
            chat_id=None,
            parent_operation_id=None,
            connection_generation=None,
            request_generation=None,
        ),
        now=None,
        retention=timedelta(days=1),
        slot_lease=timedelta(minutes=1),
    )
    selected = work.claim_operation(
        tx,
        AdmissionClass.BACKGROUND,
        accepted.operation_id,
        now=None,
        retention=timedelta(days=1),
        slot_lease=timedelta(minutes=1),
    )
    assert selected is not None
    binding = AssignmentOperationBinding(
        str(selected.fence.operation_id),
        selected.fence.execution_generation,
        str(selected.fence.execution_lease_token),
    )
    repo.bind_operation(tx, fence=claim.fence, binding=binding)
    return work, selected, binding


def transient_action(repo, tx, claim, **changes):
    reference = payload()
    values = dict(
        request={"kind": "model", "max_output_tokens": 128},
        request_digest=reference.payload_binding,
        transient_input=reference,
        maximum=AssignmentResourceAmount(model_calls=1, tokens=1024, elapsed_ms=1000),
        boundary="unreplayable",
    )
    values.update(changes)
    return action(repo, tx, claim.fence, **values)


def issued(repo, tx, *, owner="owner", transient=True):
    record = create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    work, selected, binding = admission(repo, tx, claim, owner=owner)
    created = transient_action(repo, tx, claim) if transient else action(repo, tx, claim.fence)
    permit = start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, created), binding)
    return record, claim, work, selected, binding, created, permit


def settle_args(tx, record, claim, binding, permit, *, available=True, **changes):
    result = {"text": "safe result"} if available else {}
    disposition = AssignmentResultDisposition(
        available=available,
        reason=None if available else "retention_discarded",
        binding_key_id="synthetic-key-v1",
    )
    values = dict(
        owner_id="owner",
        assignment_id=record.assignment_id,
        action_id=permit.action_id,
        attempt_id=permit.attempt_id,
        dispatch_token=permit.dispatch_token,
        expected_request_digest=permit.request_digest,
        outcome=AssignmentActionOutcome(
            "succeeded", keyed("safe result"), result, result_disposition=disposition
        ),
        result_fence=claim.fence,
        result_binding=binding,
        result_authority=session_observation(tx),
    )
    values.update(changes)
    return values


def assert_no_private_storage(tx):
    for table in (
        "persistent_assignment",
        "persistent_assignment_action",
        "persistent_assignment_activity",
        "persistent_assignment_event",
    ):
        for row in tx.fetch_all("SELECT data FROM " + table):
            stored = canonical(row["data"])
            for text in (PRIVATE, SOURCE):
                assert text not in stored
                assert hashlib.sha256(text.encode()).hexdigest() not in stored


def test_transient_input_contains_only_keyed_reconstruction_metadata(tx, repo):
    record, claim, _, _, binding, created, permit = issued(repo, tx)
    stored = action_bytes(tx, created.action_id)
    assert '"messages":' not in stored
    assert created.intent.request_digest == keyed(PRIVATE)
    assert created.intent.transient_input.reconstruction_kind == "model_messages"
    assert "assignment_fence" not in repr(created.attempts)
    result = repo.record_action_outcome(tx, **settle_args(tx, record, claim, binding, permit))
    assert result.result["result_available"] is True
    assert result.result["result"] == {"text": "safe result"}
    assert current(repo, tx, record).usage["spent"]["model_calls"] == 1
    assert_no_private_storage(tx)


@pytest.mark.parametrize("private_key", ["messages", "arguments", "text", "prompt", "notes"])
def test_transient_input_rejects_payload_fields_without_echoing_them(tx, repo, private_key):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    with pytest.raises(RepositoryValidationError) as exc:
        transient_action(
            repo,
            tx,
            claim,
            request={"kind": "model", "max_output_tokens": 128, private_key: PRIVATE},
        )
    assert PRIVATE not in str(exc.value)
    assert tx.fetch_one("SELECT count(*) AS n FROM persistent_assignment_action")["n"] == 0
    assert_no_private_storage(tx)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"version": 0},
        {"binding_key_id": ""},
        {"payload_binding": "not-a-binding"},
        {"source_retention": "forever"},
        {"source_retention": {}},
        {"reconstruction_kind": "raw_messages"},
        {"references": (AssignmentInputReference("note", "invalid private text", 1),)},
        {"references": (AssignmentInputReference("note", "not-a-note-uuid", 1),)},
        {"references": (AssignmentInputReference("secret", "id", 1),)},
        {"references": (AssignmentInputReference("source", "id", True),)},
    ],
)
def test_transient_metadata_denials(tx, repo, changes):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    value = payload(**changes)
    with pytest.raises((RepositoryValidationError, RepositoryConflictError)):
        transient_action(
            repo, tx, claim, transient_input=value, request_digest=value.payload_binding
        )
    assert_no_private_storage(tx)


def test_successful_discarded_read_is_not_failed_or_reused_as_evidence(tx, repo):
    record, claim, _, _, binding, created, permit = issued(repo, tx, transient=False)
    args = settle_args(tx, record, claim, binding, permit, available=False)
    result = repo.record_action_outcome(tx, **args)
    assert result.state == "succeeded"
    assert result.result["result_available"] is False
    assert result.result["reacquisition_reason"] == "retention_discarded"
    after = current(repo, tx, record)
    assert repo.record_action_outcome(tx, **args) == result
    assert current(repo, tx, record) == after
    with pytest.raises(RepositoryConflictError):
        reserve(repo, tx, claim.fence, created)
    next_read = action(repo, tx, claim.fence)
    next_permit = start(repo, tx, claim.fence, reserve(repo, tx, claim.fence, next_read), binding)
    repo.record_action_outcome(
        tx, **settle_args(tx, record, claim, binding, next_permit, available=False)
    )
    assert current(repo, tx, record).usage["spent"]["tool_calls"] == 2
    assert next_read.action_id != created.action_id
    assert_no_private_storage(tx)


@pytest.mark.parametrize(
    "loss",
    [
        "stop",
        "pause",
        "authority",
        "deadline",
        "retired",
        "revoked",
        "lease",
        "admission",
        "missing_context",
        "wrong_owner",
    ],
)
def test_late_authentic_outcome_settles_without_continuation_or_result(tx, repo, loss):
    record, claim, work, selected, binding, created, permit = issued(
        repo, tx, owner="other" if loss == "wrong_owner" else "owner"
    )
    args = settle_args(tx, record, claim, binding, permit)
    if loss in {"stop", "pause"}:
        control(repo, tx, current(repo, tx, record), loss)
    elif loss in {"authority", "deadline", "retired", "revoked"}:
        expire_authority(tx, record, loss)
    elif loss == "lease":
        expire_claim(tx, record)
    elif loss == "admission":
        work.reselect_execution(tx, selected.fence, now=None, slot_lease=timedelta(minutes=1))
    elif loss == "missing_context":
        args.pop("result_fence")
        args.pop("result_binding")
    before = current(repo, tx, record)
    result = repo.record_action_outcome(tx, **args)
    after = current(repo, tx, record)
    assert result.state == "succeeded"
    assert result.result["result_available"] is False
    assert result.result["result"] == {}
    assert result.result["reacquisition_reason"] == "stale_execution"
    assert after.usage["spent"]["model_calls"] == 1
    assert after.usage["outstanding"]["model_calls"] == 0
    assert (after.lifecycle, after.phase, after.wake_generation, after.checkpoint) == (
        before.lifecycle,
        before.phase,
        before.wake_generation,
        before.checkpoint,
    )
    assert repo.record_action_outcome(tx, **args) == result
    assert current(repo, tx, record) == after
    assert "safe result" not in action_bytes(tx, created.action_id)


def test_replay_after_control_does_not_return_previously_available_result(tx, repo):
    record, claim, _, _, binding, _, permit = issued(repo, tx)
    args = settle_args(tx, record, claim, binding, permit)
    first = repo.record_action_outcome(tx, **args)
    assert first.result["result_available"]
    stopped = control(repo, tx, current(repo, tx, record), "stop").assignment
    replay = repo.record_action_outcome(tx, **args)
    assert replay.result["result_available"] is False
    assert replay.result["result"] == {}
    assert current(repo, tx, record) == stopped


@pytest.mark.parametrize(
    "bad", ["plaintext", "missing_reason", "available_reason", "future", "unkeyed"]
)
def test_result_disposition_rejects_private_or_malformed_receipts(tx, repo, bad):
    record, claim, _, _, binding, _, permit = issued(repo, tx)
    args = settle_args(tx, record, claim, binding, permit, available=False)
    outcome = args["outcome"]
    if bad == "plaintext":
        outcome = replace(outcome, result={"text": SOURCE})
    elif bad == "missing_reason":
        outcome = replace(
            outcome, result_disposition=replace(outcome.result_disposition, reason=None)
        )
    elif bad == "available_reason":
        outcome = replace(
            outcome, result_disposition=replace(outcome.result_disposition, available=True)
        )
    elif bad == "future":
        outcome = replace(
            outcome, result_disposition=replace(outcome.result_disposition, version=2)
        )
    else:
        outcome = replace(outcome, result_disposition=None)
    with pytest.raises((RepositoryValidationError, RepositoryConflictError)) as exc:
        repo.record_action_outcome(tx, **dict(args, outcome=outcome))
    assert SOURCE not in str(exc.value)
    assert_no_private_storage(tx)


@pytest.mark.parametrize("stale", [False, True])
def test_transient_evidence_reference_is_opaque_before_current_or_stale_settlement(tx, repo, stale):
    record, claim, _, _, binding, created, permit = issued(repo, tx)
    args = settle_args(tx, record, claim, binding, permit)
    if stale:
        control(repo, tx, current(repo, tx, record), "stop")
    before = action_bytes(tx, created.action_id)
    with pytest.raises(RepositoryValidationError) as caught:
        repo.record_action_outcome(
            tx, **dict(args, outcome=replace(args["outcome"], evidence_reference=SOURCE))
        )
    assert SOURCE not in "".join(traceback.format_exception(caught.value))
    assert action_bytes(tx, created.action_id) == before
    assert_no_private_storage(tx)
    reference = "receipt:" + uid()
    accepted = repo.record_action_outcome(
        tx, **dict(args, outcome=replace(args["outcome"], evidence_reference=reference))
    )
    assert accepted.result["evidence_reference"] == reference
    assert accepted.result["result_available"] is not stale


@pytest.mark.parametrize("malformed", ["evidence", "key"])
def test_transient_opaque_decoder_holds_malformed_settlement_metadata(tx, repo, malformed):
    record, claim, _, _, binding, created, permit = issued(repo, tx)
    repo.record_action_outcome(tx, **settle_args(tx, record, claim, binding, permit))

    def change(data):
        outcome = data["attempts"][0]["outcome"]
        if malformed == "evidence":
            outcome["evidence_reference"] = SOURCE
        else:
            outcome["result_disposition"]["binding_key_id"] = None

    change_action(tx, created.action_id, change)
    before = action_bytes(tx, created.action_id)
    result = repo.retire_operations_for_owner(tx, owner_id="owner")
    assert result.retained_assignment_ids == (record.assignment_id,)
    assert result.unresolved_action_ids == (created.action_id,)
    assert action_bytes(tx, created.action_id) == before


def test_future_input_disposition_is_inspectable_and_cancellable_without_interpretation(tx, repo):
    record, claim, _, _, _, created, _ = issued(repo, tx)

    def change(data):
        data["intent"]["transient_input"] = {"version": 2, "future": {"opaque": True}}
        data["intent_digest"] = digest(data["intent"])

    change_action(tx, created.action_id, change)
    before = action_bytes(tx, created.action_id)
    inspected = repo.get_action(
        tx, owner_id="owner", assignment_id=record.assignment_id, action_id=created.action_id
    )
    assert inspected.intent.transient_input["version"] == 2
    with pytest.raises(RepositoryConflictError, match="assignment_payload_version_unsupported"):
        reserve(repo, tx, claim.fence, created)
    stopped = control(repo, tx, current(repo, tx, record), "stop")
    assert stopped.begun_action_ids == (created.action_id,)
    assert repo.retire_operations_for_owner(tx, owner_id="owner").unresolved_action_ids == (
        created.action_id,
    )
    assert action_bytes(tx, created.action_id) == before


@pytest.mark.parametrize("field", ["input", "outcome"])
def test_future_payload_recovery_holds_opaque_action_and_recovers_supported_neighbor(
    tx, repo, field
):
    record, claim, _, _, binding, created, permit = issued(repo, tx)
    if field == "outcome":
        repo.record_action_outcome(tx, **settle_args(tx, record, claim, binding, permit))

    def change(data):
        if field == "input":
            data["intent"]["transient_input"] = {"version": 2, "opaque": [1]}
            data["intent_digest"] = digest(data["intent"])
        else:
            data["attempts"][0]["outcome"]["result_disposition"] = {"version": 2, "opaque": [1]}

    change_action(tx, created.action_id, change)
    before = action_bytes(tx, created.action_id)
    next_record = create_operation(repo, tx, caller_key="supported-neighbor")
    operation_claim(repo, tx)
    expire_claim(tx, record)
    expire_claim(tx, next_record)
    recovery = repo.recover_expired_operations_for_administration(tx)
    assert set(recovery.reclaimed_assignment_ids) == {
        record.assignment_id,
        next_record.assignment_id,
    }
    assert recovery.uncertain_action_ids == (created.action_id,)
    assert current(repo, tx, record).phase == "reconciliation"
    assert current(repo, tx, record).next_wake_at is None
    assert current(repo, tx, next_record).next_wake_at is not None
    assert action_bytes(tx, created.action_id) == before


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "note_limit",
        "skill_limit",
        "source_limit",
        "total_limit",
        "byte_limit",
        "mapping",
        "missing_fields",
    ],
)
def test_reconstruction_envelope_bounds_and_shape(tx, repo, case):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    if case == "mapping":
        value = "malformed"
    elif case == "missing_fields":
        value = {"version": 1}
    else:
        counts = {
            "duplicate": ("note", 2),
            "note_limit": ("note", 9),
            "skill_limit": ("skill", 21),
            "source_limit": ("source", 33),
            "total_limit": ("source", 61),
            "byte_limit": ("source", 32),
        }
        kind, count = counts[case]
        identity = uid()
        refs = tuple(
            AssignmentInputReference(
                kind,
                identity
                if case == "duplicate"
                else (str(i) + "x" * 250 if case == "byte_limit" else uid()),
                1,
            )
            for i in range(count)
        )
        value = payload(references=refs)
    with pytest.raises(RepositoryValidationError):
        transient_action(repo, tx, claim, transient_input=value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", PRIVATE),
        ("provider", PRIVATE),
        ("max_output_tokens", True),
        ("reasoning_effort", "arbitrary"),
        ("reasoning_effort", []),
        ("response_format", {"type": "json_object", "schema": PRIVATE}),
    ],
)
def test_routing_metadata_cannot_hide_private_text(tx, repo, field, value):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    with pytest.raises(RepositoryValidationError):
        transient_action(
            repo, tx, claim, request={"kind": "model", "max_output_tokens": 128, field: value}
        )
    assert_no_private_storage(tx)


def test_closed_model_routing_and_keyed_binding_are_preserved(tx, repo):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    routing = {
        "kind": "model",
        "max_output_tokens": 128,
        "provider": "factory:primary",
        "model": "approved/model-v1@revision",
        "reasoning_effort": "medium",
        "response_format": {"type": "json_object"},
    }
    created = transient_action(repo, tx, claim, request=routing)
    assert plain(created.intent.request) == routing
    with pytest.raises(RepositoryValidationError):
        transient_action(repo, tx, claim, request_digest=digest(PRIVATE))
    assert_no_private_storage(tx)


@pytest.mark.parametrize("nested", [False, True])
def test_private_field_names_are_absent_from_validation_exception_chains(tx, repo, nested):
    create_operation(repo, tx)
    claim = operation_claim(repo, tx)
    value = plain(payload())
    target = value["references"][0] if nested else value
    target[PRIVATE] = SOURCE
    with pytest.raises(RepositoryValidationError) as caught:
        transient_action(repo, tx, claim, transient_input=value)
    rendered = "".join(traceback.format_exception(caught.value))
    assert PRIVATE not in rendered and SOURCE not in rendered
    assert_no_private_storage(tx)


@pytest.mark.parametrize("profile", ["persistent", "wrong_retention", "consequential"])
def test_transient_mode_does_not_change_legacy_or_reviewed_effect_contract(tx, repo, profile):
    if profile == "persistent":
        create(repo, tx)
        claim = repo.claim_due_for_administration(tx, worker_id="legacy")[0]
        changes = {}
    else:
        create_operation(repo, tx)
        claim = operation_claim(repo, tx)
        value = payload(source_retention="operation" if profile == "wrong_retention" else "none")
        changes = {"transient_input": value, "request_digest": value.payload_binding}
        if profile == "consequential":
            changes["sensitivity"] = "sensitive"
    with pytest.raises(RepositoryValidationError):
        transient_action(repo, tx, claim, **changes)


def test_result_fence_requires_typed_context_without_exposing_authority_tokens(tx, repo):
    record, claim, _, _, binding, created, permit = issued(repo, tx)
    args = settle_args(tx, record, claim, binding, permit)
    with pytest.raises(RepositoryValidationError):
        repo.record_action_outcome(tx, **dict(args, result_fence={"claim_token": PRIVATE}))
    inspected = repo.get_action(
        tx, owner_id="owner", assignment_id=record.assignment_id, action_id=created.action_id
    )
    public = canonical(inspected)
    assert claim.fence.claim_token not in public
    assert permit.dispatch_token not in public
    assert binding.execution_lease_token not in public
    assert_no_private_storage(tx)


def test_retirement_and_result_share_owner_lock_and_settle_once(database, repo):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        record, claim, _, _, binding, _, permit = issued(repo, tx)
    results = parallel_transactions(
        database,
        (
            lambda tx: repo.record_action_outcome(
                tx, **settle_args(tx, record, claim, binding, permit)
            ),
            lambda tx: repo.retire_operations_for_owner(tx, owner_id="owner"),
        ),
    )
    assert not any(isinstance(result, Exception) for result in results)
    with database.transaction() as tx:
        record_after = repo.get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
        if record_after is not None:
            assert record_after.lifecycle == "stopped"
            assert record_after.usage["spent"]["model_calls"] == 1
            assert record_after.usage["outstanding"]["model_calls"] == 0
            assert not repo.retire_operations_for_owner(
                tx, owner_id="owner"
            ).retained_assignment_ids
        assert (
            tx.fetch_one("SELECT state FROM astralplane_blob_owner_state WHERE owner_id='owner'")[
                "state"
            ]
            == "retired"
        )
