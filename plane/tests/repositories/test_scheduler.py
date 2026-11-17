"""Tests for astralplane.repositories.scheduler and scheduler_models: scheduled-job CRUD
and due ordering, occurrence claim/retry fencing, job-policy CAS, and
episode-admission validation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from _support import Result, ScriptedTransaction

from astralplane.errors import PlaneError
from astralplane.repositories.scheduler import (
    EffectState,
    OccurrenceState,
    ScheduledJob,
    SchedulerRepository,
    StagedChatLayout,
    StagedChatMessage,
    StagedChatPublication,
    _policy,
)
from astralplane.repositories.scheduler_models import (
    EpisodeAdmission,
    JobStopOutcome,
    ScheduledJobPolicy,
)

NOW = datetime(2026, 8, 13, 20, tzinfo=UTC)
OPERATION_ID = "11111111-1111-4111-8111-111111111111"
SUBMISSION_ID = "22222222-2222-5222-8222-222222222222"
JOB_ID = "33333333-3333-4333-8333-333333333333"
OCCURRENCE_ID = "44444444-4444-4444-8444-444444444444"
LEASE_TOKEN = "55555555-5555-4555-8555-555555555555"
OPERATION_LEASE_TOKEN = "66666666-6666-4666-8666-666666666666"
OTHER_OCCURRENCE_ID = "77777777-7777-4777-8777-777777777777"
OTHER_JOB_ID = "88888888-8888-4888-8888-888888888888"


def job_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": JOB_ID,
        "user_id": "owner-1",
        "name": "Morning review",
        "instruction": "Summarize the inbox",
        "schedule_kind": "cron",
        "schedule_expr": "0 9 * * *",
        "timezone": "UTC",
        "status": "active",
        "next_run_at": 10,
        "created_at": 1,
        "updated_at": 2,
    }
    row.update(overrides)
    return row


def job(**overrides: object) -> ScheduledJob:
    values = {
        "job_id": JOB_ID,
        "owner_id": "owner-1",
        "name": "Morning review",
        "instruction": "Summarize the inbox",
        "schedule_kind": "cron",
        "schedule_expression": "0 9 * * *",
        "timezone": "UTC",
        "status": "active",
        "next_run_at": 10,
        "created_at": 1,
        "updated_at": 2,
    }
    values.update(overrides)
    return ScheduledJob(**values)  # type: ignore[arg-type]


def occurrence_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "occurrence_id": OCCURRENCE_ID,
        "job_id": JOB_ID,
        "owner_user_id": "owner-1",
        "scheduled_for": NOW,
        "state": "pending",
        "claim_generation": 0,
        "lease_token": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "attempt_count": 0,
        "current_operation_id": None,
        "operation_execution_generation": None,
    }
    row.update(overrides)
    return row


def effect_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "occurrence_id": OCCURRENCE_ID,
        "effect_kind": "chat_publication",
        "effect_key": "message-1",
        "payload_digest": "b" * 64,
        "state": "reserved",
        "operation_id": OPERATION_ID,
        "operation_execution_generation": 2,
        "occurrence_claim_generation": 3,
        "downstream_receipt_digest": None,
        "published_at": None,
    }
    row.update(overrides)
    return row

def test_scheduled_job_crud_and_due_ordering() -> None:
    repository = SchedulerRepository()
    value = job()
    assert repository.put_job(ScriptedTransaction(one=[job_row()]), value) == value
    assert repository.put_job(ScriptedTransaction(one=[None, job_row()]), value) == value
    assert (
        repository.get_job(ScriptedTransaction(one=[job_row()]), owner_id="owner-1", job_id=JOB_ID)
        == value
    )
    assert (
        repository.get_job(ScriptedTransaction(one=[None]), owner_id="other", job_id=JOB_ID) is None
    )
    tx = ScriptedTransaction(all_rows=[(job_row(),)])
    assert repository.list_due_jobs(tx, owner_id="owner-1", due_at_ms=10, limit=1) == (value,)
    assert tx.calls[0][2][0] == "owner-1"  # type: ignore[index]
    with pytest.raises(ValueError):
        repository.list_due_jobs(ScriptedTransaction(), owner_id="owner-1", due_at_ms=1, limit=0)


@pytest.mark.parametrize(
    "changes",
    [
        {"job_id": SUBMISSION_ID},
        {"name": ""},
        {"schedule_kind": "solar"},
        {"status": "deleted"},
        {"created_at": 3, "updated_at": 2},
        {"agent_id": ""},
        {"consented_scopes": ("",)},
        {"consented_scopes": ("tools:read", "tools:read")},
        {"delivery": "email"},
        {"last_run_at": -1},
        {"offline_grant_id": "not-a-uuid"},
    ],
)
def test_job_validation(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        job(**changes)


def test_job_conflict_is_visible() -> None:
    with pytest.raises(PlaneError) as raised:
        SchedulerRepository().put_job(
            ScriptedTransaction(one=[None, job_row(name="Different")]), job()
        )
    assert raised.value.code == "scheduled_job_conflict"


def test_occurrence_creation_replay_claim_and_failures() -> None:
    repository = SchedulerRepository()
    created = repository.create_occurrence(
        ScriptedTransaction(one=[occurrence_row()]),
        occurrence_id=OCCURRENCE_ID,
        job_id=JOB_ID,
        owner_id="owner-1",
        scheduled_for=NOW,
    )
    assert created.state is OccurrenceState.PENDING
    replay = repository.create_occurrence(
        ScriptedTransaction(one=[None, occurrence_row()]),
        occurrence_id=OTHER_OCCURRENCE_ID,
        job_id=JOB_ID,
        owner_id="owner-1",
        scheduled_for=NOW,
    )
    assert replay.occurrence_id == OCCURRENCE_ID
    claimed_row = occurrence_row(
        state="claimed",
        claim_generation=1,
        lease_token=LEASE_TOKEN,
        lease_owner="worker",
        lease_expires_at=NOW + timedelta(minutes=1),
        attempt_count=1,
    )
    claimed_transaction = ScriptedTransaction(one=[claimed_row])
    claimed = repository.claim_occurrence(
        claimed_transaction,
        owner_id="owner-1",
        occurrence_id=OCCURRENCE_ID,
        worker_id="worker",
        lease_token=LEASE_TOKEN,
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    assert claimed.claim_generation == 1
    assert "state IN ('claimed', 'running')" in claimed_transaction.fetch_sql()
    assert "operation_record" not in claimed_transaction.fetch_sql()
    with pytest.raises(ValueError):
        repository.claim_occurrence(
            ScriptedTransaction(),
            owner_id="owner-1",
            occurrence_id=OCCURRENCE_ID,
            worker_id="worker",
            lease_token=LEASE_TOKEN,
            now=NOW,
            lease_expires_at=NOW,
        )
    with pytest.raises(PlaneError) as raised:
        repository.claim_occurrence(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            occurrence_id=OCCURRENCE_ID,
            worker_id="worker",
            lease_token=LEASE_TOKEN,
            now=NOW,
            lease_expires_at=NOW + timedelta(seconds=1),
        )
    assert raised.value.code == "stale_occurrence_fence"
    with pytest.raises(PlaneError):
        repository.create_occurrence(
            ScriptedTransaction(one=[None, None]),
            occurrence_id=OCCURRENCE_ID,
            job_id=OTHER_JOB_ID,
            owner_id="owner-1",
            scheduled_for=NOW,
        )


def running_occurrence_row(**overrides: object) -> dict[str, object]:
    return occurrence_row(
        state="running",
        claim_generation=3,
        lease_token=LEASE_TOKEN,
        lease_owner="worker",
        lease_expires_at=NOW + timedelta(minutes=1),
        attempt_count=3,
        current_operation_id=OPERATION_ID,
        operation_execution_generation=2,
        database_now=NOW,
        **overrides,
    )


def job_run_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": OTHER_OCCURRENCE_ID,
        "job_id": JOB_ID,
        "user_id": "owner-1",
        "started_at": 10,
        "ended_at": None,
        "outcome": "running",
        "auth_ref": None,
        "correlation_id": OPERATION_LEASE_TOKEN,
        "summary": None,
        "occurrence_id": OCCURRENCE_ID,
        "attempt_number": 3,
        "operation_id": OPERATION_ID,
        "operation_execution_generation": 2,
        "occurrence_claim_generation": 3,
    }
    row.update(overrides)
    return row


def test_complete_job_contracts_are_owner_scoped() -> None:
    repository = SchedulerRepository()
    value = job()
    assert repository.count_active_jobs(
        ScriptedTransaction(one=[{"n": 2}]), owner_id="owner-1"
    ) == 2
    assert repository.create_job_definition(
        ScriptedTransaction(one=[job_row()]), value
    ) == value
    assert repository.list_jobs(
        ScriptedTransaction(all_rows=[(job_row(),)]), owner_id="owner-1"
    ) == (value,)
    assert repository.set_job_status(
        ScriptedTransaction(),
        owner_id="owner-1",
        job_id=JOB_ID,
        status="paused",
        updated_at=12,
    )
    assert repository.set_job_offline_grant(
        ScriptedTransaction(),
        owner_id="owner-1",
        job_id=JOB_ID,
        grant_id=SUBMISSION_ID,
        updated_at=13,
    )
    with pytest.raises(ValueError, match="status"):
        repository.set_job_status(
            ScriptedTransaction(),
            owner_id="owner-1",
            job_id=JOB_ID,
            status="deleted",
            updated_at=12,
        )


def test_run_now_materialization_replays_exact_owner_submission() -> None:
    repository = SchedulerRepository()
    inserted = occurrence_row(scheduled_for=NOW, state="pending")
    transaction = ScriptedTransaction(one=[job_row(), None, {"now": NOW}, inserted])
    result = repository.materialize_run_now(
        transaction,
        owner_id="owner-1",
        job_id=JOB_ID,
        submission_id=LEASE_TOKEN,
    )
    assert result.created
    assert result.occurrence_id == OCCURRENCE_ID
    replay = repository.materialize_run_now(
        ScriptedTransaction(one=[job_row(), inserted]),
        owner_id="owner-1",
        job_id=JOB_ID,
        submission_id=LEASE_TOKEN,
    )
    assert not replay.created
    with pytest.raises(PlaneError) as raised:
        repository.materialize_run_now(
            ScriptedTransaction(one=[job_row(), occurrence_row(job_id=OTHER_JOB_ID)]),
            owner_id="owner-1",
            job_id=JOB_ID,
            submission_id=LEASE_TOKEN,
        )
    assert raised.value.code == "scheduled_run_now_idempotency_conflict"


def test_claim_assertion_attachment_start_and_retry_are_fenced() -> None:
    repository = SchedulerRepository()
    claimed = occurrence_row(
        state="claimed",
        claim_generation=3,
        lease_token=LEASE_TOKEN,
        lease_owner="worker",
        lease_expires_at=NOW + timedelta(minutes=1),
        attempt_count=3,
        current_operation_id=OPERATION_ID,
    )
    assert repository.attach_operation_to_claim(
        ScriptedTransaction(one=[claimed]),
        owner_id="owner-1",
        occurrence_id=OCCURRENCE_ID,
        claim_generation=3,
        lease_token=LEASE_TOKEN,
        operation_id=OPERATION_ID,
    ).operation_id == OPERATION_ID
    run = repository.start_claim_attempt(
        ScriptedTransaction(one=[{"occurrence_id": OCCURRENCE_ID}, job_run_row()]),
        owner_id="owner-1",
        job_id=JOB_ID,
        occurrence_id=OCCURRENCE_ID,
        attempt_number=3,
        claim_generation=3,
        lease_token=LEASE_TOKEN,
        operation_id=OPERATION_ID,
        operation_execution_generation=2,
        run_id=OTHER_OCCURRENCE_ID,
        correlation_id=OPERATION_LEASE_TOKEN,
        lease_seconds=15,
    )
    assert run.operation_id == OPERATION_ID
    retryable = occurrence_row(
        state="retryable",
        claim_generation=3,
        lease_token=None,
        lease_owner=None,
        lease_expires_at=None,
        attempt_count=3,
        current_operation_id=OPERATION_ID,
        operation_execution_generation=2,
    )
    retried = repository.mark_claim_retryable(
        ScriptedTransaction(one=[running_occurrence_row(), {"id": run.run_id}, retryable]),
        owner_id="owner-1",
        occurrence_id=OCCURRENCE_ID,
        attempt_number=3,
        claim_generation=3,
        lease_token=LEASE_TOKEN,
        lease_owner="worker",
        operation_id=OPERATION_ID,
        operation_execution_generation=2,
        error_code="claim_lost",
        retry_after_seconds=1,
    )
    assert retried.state is OccurrenceState.RETRYABLE


def test_effect_contracts_reconcile_publish_and_fail() -> None:
    repository = SchedulerRepository()
    common = {
        "owner_id": "owner-1",
        "occurrence_id": OCCURRENCE_ID,
        "claim_generation": 3,
        "lease_token": LEASE_TOKEN,
        "lease_owner": "worker",
        "operation_id": OPERATION_ID,
        "operation_execution_generation": 2,
        "effect_kind": "chat_publication",
        "effect_key": "message-1",
        "payload_digest": "b" * 64,
    }
    reserved = repository.reserve_effect_for_attempt(
        ScriptedTransaction(one=[running_occurrence_row(), effect_row()]), **common
    )
    assert reserved.state is EffectState.RESERVED
    assert not reserved.created and not reserved.ambiguous
    recovered = repository.reserve_effect_for_attempt(
        ScriptedTransaction(
            one=[
                running_occurrence_row(),
                effect_row(state="failed"),
                effect_row(),
            ]
        ),
        **common,
    )
    assert recovered.created
    published = repository.publish_reserved_effect(
        ScriptedTransaction(
            one=[
                running_occurrence_row(),
                effect_row(),
                effect_row(
                    state="published",
                    published_at=NOW,
                    downstream_receipt_digest="c" * 64,
                ),
            ]
        ),
        **common,
        downstream_receipt_digest="c" * 64,
    )
    assert published.state is EffectState.PUBLISHED
    failed = repository.fail_reserved_effect(
        ScriptedTransaction(
            one=[running_occurrence_row(), effect_row(state="failed")]
        ),
        **common,
        failure_code="downstream_failed",
    )
    assert failed.state is EffectState.FAILED
    with pytest.raises(PlaneError) as raised:
        repository.reserve_effect_for_attempt(
            ScriptedTransaction(
                one=[running_occurrence_row(), effect_row(payload_digest="d" * 64)]
            ),
            **common,
        )
    assert raised.value.code == "effect_idempotency_conflict"


def test_finish_claim_attempt_updates_run_and_occurrence_together() -> None:
    completed = occurrence_row(
        state="completed",
        claim_generation=3,
        lease_token=None,
        lease_owner=None,
        lease_expires_at=None,
        attempt_count=3,
        current_operation_id=OPERATION_ID,
        operation_execution_generation=2,
    )
    result = SchedulerRepository().finish_claim_attempt(
        ScriptedTransaction(one=[running_occurrence_row(), {"id": OTHER_OCCURRENCE_ID}, completed]),
        owner_id="owner-1",
        job_id=JOB_ID,
        occurrence_id=OCCURRENCE_ID,
        attempt_number=3,
        claim_generation=3,
        lease_token=LEASE_TOKEN,
        lease_owner="worker",
        operation_id=OPERATION_ID,
        operation_execution_generation=2,
        run_id=OTHER_OCCURRENCE_ID,
        job_outcome="success",
        occurrence_state=OccurrenceState.COMPLETED,
        safe_code="success",
        summary="done",
        auth_ref=None,
        retry_after_seconds=1,
    )
    assert result.state is OccurrenceState.COMPLETED


def test_legacy_run_cadence_and_reconciliation_contracts() -> None:
    repository = SchedulerRepository()
    run = repository.start_legacy_run(
        ScriptedTransaction(one=[job_run_row()]),
        run_id=OTHER_OCCURRENCE_ID,
        job_id=JOB_ID,
        owner_id="owner-1",
        correlation_id=OPERATION_LEASE_TOKEN,
        started_at=10,
    )
    assert run.run_id == OTHER_OCCURRENCE_ID
    assert repository.start_legacy_run(
        ScriptedTransaction(one=[None, job_run_row()]),
        run_id=OTHER_OCCURRENCE_ID,
        job_id=JOB_ID,
        owner_id="owner-1",
        correlation_id=OPERATION_LEASE_TOKEN,
        started_at=10,
    ) == run
    with pytest.raises(PlaneError, match="not found"):
        repository.start_legacy_run(
            ScriptedTransaction(one=[None, None]),
            run_id=OTHER_OCCURRENCE_ID,
            job_id=JOB_ID,
            owner_id="owner-1",
            correlation_id=OPERATION_LEASE_TOKEN,
            started_at=10,
        )

    assert repository.finish_run_for_administration(
        ScriptedTransaction(execute=[Result(rowcount=1)]),
        run_id=OTHER_OCCURRENCE_ID,
        outcome="success",
        summary="done",
        auth_ref="grant-1",
        ended_at=20,
    )
    assert repository.list_runs(
        ScriptedTransaction(all_rows=[(job_run_row(),)]),
        owner_id="owner-1",
        job_id=JOB_ID,
        limit=1,
    ) == (run,)
    assert repository.reconcile_interrupted_for_administration(
        ScriptedTransaction(execute=[Result(rowcount=3)]),
        ended_at=21,
    ) == 3
    assert repository.update_job_after_run_for_administration(
        ScriptedTransaction(execute=[Result(rowcount=1)]),
        job_id=JOB_ID,
        last_run_at=20,
        next_run_at=30,
        completed=False,
        updated_at=21,
    )
    assert repository.list_due_jobs_for_administration(
        ScriptedTransaction(all_rows=[(job_row(),)]),
        due_at_ms=10,
        limit=1,
    ) == (job(),)


def test_job_definition_replay_and_unstarted_cancellation_failures_are_visible() -> None:
    repository = SchedulerRepository()
    value = job()
    assert repository.count_active_jobs(
        ScriptedTransaction(one=[None]), owner_id="owner-1"
    ) == 0
    assert repository.create_job_definition(
        ScriptedTransaction(one=[None, job_row()]), value
    ) == value
    with pytest.raises(PlaneError) as conflict:
        repository.create_job_definition(
            ScriptedTransaction(one=[None, job_row(name="changed")]), value
        )
    assert conflict.value.code == "scheduled_job_conflict"

    terminal = ScriptedTransaction(
        one=[None, {"state": "completed", "current_operation_id": OPERATION_ID}]
    )
    assert not repository.cancel_unstarted_occurrence(
        terminal,
        owner_id="owner-1",
        occurrence_id=OCCURRENCE_ID,
        expected_operation_id=OPERATION_ID,
        terminal_code="cancelled_job_paused",
    )
    changed_operation = ScriptedTransaction(
        one=[None, {"state": "pending", "current_operation_id": OTHER_JOB_ID}]
    )
    with pytest.raises(PlaneError) as changed:
        repository.cancel_unstarted_occurrence(
            changed_operation,
            owner_id="owner-1",
            occurrence_id=OCCURRENCE_ID,
            expected_operation_id=OPERATION_ID,
            terminal_code="cancelled_job_paused",
        )
    assert changed.value.code == "scheduled_occurrence_operation_conflict"
    stale = ScriptedTransaction(
        one=[None, {"state": "pending", "current_operation_id": OPERATION_ID}]
    )
    with pytest.raises(PlaneError) as lost:
        repository.cancel_unstarted_occurrence(
            stale,
            owner_id="owner-1",
            occurrence_id=OCCURRENCE_ID,
            expected_operation_id=OPERATION_ID,
            terminal_code="cancelled_job_paused",
        )
    assert lost.value.code == "stale_occurrence_fence"


def test_staged_chat_validation_reaches_reserved_effect_authority() -> None:
    repository = SchedulerRepository()
    publication = StagedChatPublication(
        publication_id=OTHER_JOB_ID,
        conversation_id="chat-1",
        owner_id="owner-1",
        request_generation=LEASE_TOKEN,
        create_conversation_if_missing=False,
        agent_id="agent-1",
        requested_title="Scheduled result",
        base_render_revision=0,
        committed_render_revision=1,
        messages=(
            StagedChatMessage(
                role="assistant",
                content='[{"type":"text","content":"done"}]',
                timestamp_ms=10,
                title_source="done",
            ),
        ),
        layouts=(
            StagedChatLayout(
                layout_key="main",
                position=0,
                tree=({"type": "stack", "children": []},),
            ),
        ),
    )
    common = {
        "owner_id": "owner-1",
        "occurrence_id": OCCURRENCE_ID,
        "claim_generation": 3,
        "lease_token": LEASE_TOKEN,
        "lease_owner": "worker",
        "operation_id": OPERATION_ID,
        "operation_execution_generation": 2,
        "effect_key": "chat-1",
        "payload_digest": "b" * 64,
        "publication": publication,
    }
    with pytest.raises(PlaneError) as missing:
        repository.publish_staged_chat_effect(
            ScriptedTransaction(one=[running_occurrence_row(), None]),
            **common,
        )
    assert missing.value.code == "effect_reservation_not_found"

    published_effect = effect_row(
        effect_kind="chat_history",
        effect_key="chat-1",
        state="published",
        published_at=NOW,
    )
    replay = repository.publish_staged_chat_effect(
        ScriptedTransaction(one=[running_occurrence_row(), published_effect]),
        **common,
    )
    assert replay.state is EffectState.PUBLISHED
    assert not replay.created and not replay.ambiguous

    reserved_effect = effect_row(
        effect_kind="chat_history",
        effect_key="chat-1",
    )
    create_publication = replace(
        publication,
        create_conversation_if_missing=True,
    )
    with pytest.raises(PlaneError) as stale_publication:
        repository.publish_staged_chat_effect(
            ScriptedTransaction(
                one=[
                    running_occurrence_row(),
                    reserved_effect,
                    None,
                    {"user_id": "owner-1", "render_revision": 0},
                    None,
                ]
            ),
            **{**common, "publication": create_publication},
        )
    assert stale_publication.value.code == "scheduled_publication_conflict"


def test_scheduler_public_validation_and_impossible_results_fail_closed() -> None:
    repository = SchedulerRepository()
    with pytest.raises(ValueError, match="for_update"):
        repository.get_job(
            ScriptedTransaction(),
            owner_id="owner-1",
            job_id=JOB_ID,
            for_update=1,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="boolean"):
        repository.update_job_after_run_for_administration(
            ScriptedTransaction(),
            job_id=JOB_ID,
            last_run_at=1,
            next_run_at=None,
            completed="no",  # type: ignore[arg-type]
            updated_at=2,
        )
    for outcome, summary, auth_ref, message in (
        ("running", None, None, "outcome"),
        ("success", "x" * 2001, None, "summary"),
        ("success", None, "", "auth_ref"),
    ):
        with pytest.raises(ValueError, match=message):
            repository.finish_run_for_administration(
                ScriptedTransaction(),
                run_id=OTHER_OCCURRENCE_ID,
                outcome=outcome,
                summary=summary,
                auth_ref=auth_ref,
                ended_at=1,
            )
    with pytest.raises(PlaneError) as invalid_count:
        repository.reconcile_interrupted_for_administration(
            ScriptedTransaction(execute=[Result(rowcount=-1)]),
            ended_at=1,
        )
    assert invalid_count.value.code == "scheduler_rowcount_integrity_error"

    with pytest.raises(PlaneError) as missing_job:
        repository.materialize_run_now(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            job_id=JOB_ID,
            submission_id=LEASE_TOKEN,
        )
    assert missing_job.value.code == "scheduled_job_not_found"
    with pytest.raises(PlaneError) as inactive_job:
        repository.materialize_run_now(
            ScriptedTransaction(one=[job_row(status="paused"), None]),
            owner_id="owner-1",
            job_id=JOB_ID,
            submission_id=LEASE_TOKEN,
        )
    assert inactive_job.value.code == "scheduled_job_not_active"
    with pytest.raises(PlaneError) as missing_clock:
        repository.materialize_run_now(
            ScriptedTransaction(one=[job_row(), None, None]),
            owner_id="owner-1",
            job_id=JOB_ID,
            submission_id=LEASE_TOKEN,
        )
    assert missing_clock.value.code == "scheduler_clock_missing"

    settlement = {
        "owner_id": "owner-1",
        "job_id": JOB_ID,
        "occurrence_id": OCCURRENCE_ID,
        "attempt_number": 3,
        "claim_generation": 3,
        "lease_token": LEASE_TOKEN,
        "lease_owner": "worker",
        "operation_id": OPERATION_ID,
        "operation_execution_generation": 2,
        "run_id": OTHER_OCCURRENCE_ID,
        "job_outcome": "success",
        "occurrence_state": OccurrenceState.COMPLETED,
        "safe_code": "success",
        "summary": "done",
        "auth_ref": None,
        "retry_after_seconds": 0,
    }
    for changes, message in (
        ({"attempt_number": 0}, "generations"),
        ({"job_outcome": "running"}, "outcome"),
        ({"occurrence_state": OccurrenceState.CLAIMED}, "state"),
        ({"summary": "x" * 2001}, "summary"),
        ({"retry_after_seconds": -1}, "retry_after_seconds"),
    ):
        with pytest.raises(ValueError, match=message):
            repository.finish_claim_attempt(
                ScriptedTransaction(),
                **{**settlement, **changes},
            )


ASSIGNMENT_ID = "99999999-9999-4999-8999-999999999999"
OTHER_ASSIGNMENT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def policy_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "job_id": JOB_ID,
        "owner_id": "owner-1",
        "version": 1,
        "max_runs": 3,
        "admitted_runs": 0,
        "per_episode_limits": {"model_calls": 4},
        "max_outstanding_episodes": 1,
        "monitor_changes": True,
        "definition_revision": 1,
        "terminal_stop": False,
        "last_assignment_id": None,
        "updated_at": 5,
    }
    row.update(overrides)
    return row


def policy_value(**overrides: object) -> ScheduledJobPolicy:
    return _policy(policy_row(**overrides))


def claimed_row(**overrides: object) -> dict[str, object]:
    return occurrence_row(
        state="claimed",
        claim_generation=1,
        lease_token=LEASE_TOKEN,
        lease_owner="worker-1",
        lease_expires_at=NOW + timedelta(seconds=30),
        database_now=NOW,
        attempt_count=1,
        **overrides,
    )


def admit(tx: ScriptedTransaction, **changes: object) -> EpisodeAdmission:
    values: dict[str, object] = dict(
        owner_id="owner-1",
        job_id=JOB_ID,
        occurrence_id=OCCURRENCE_ID,
        claim_generation=1,
        lease_token=LEASE_TOKEN,
        lease_owner="worker-1",
        assignment_id=ASSIGNMENT_ID,
        admitted_at=7,
    )
    values.update(changes)
    return SchedulerRepository().admit_assignment_episode(tx, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "call",
    [
        lambda repo, tx: repo.get_job_policy(tx, owner_id="", job_id=JOB_ID),
        lambda repo, tx: repo.get_job_policy(tx, owner_id="owner-1", job_id="job"),
        lambda repo, tx: repo.put_job_policy(tx, policy=policy_row(), expected_version=0),
        lambda repo, tx: repo.put_job_policy(tx, policy=policy_value(), expected_version=-1),
        lambda repo, tx: repo.put_job_policy(tx, policy=policy_value(), expected_version=True),
        lambda repo, tx: repo.put_job_policy(tx, policy=policy_value(), expected_version=1),
        lambda repo, tx: repo.put_job_policy(
            tx, policy=policy_value(admitted_runs=1), expected_version=0
        ),
        lambda repo, tx: repo.put_job_policy(
            tx, policy=policy_value(terminal_stop=True), expected_version=0
        ),
        lambda repo, tx: repo.put_job_policy(
            tx, policy=policy_value(last_assignment_id=ASSIGNMENT_ID), expected_version=0
        ),
        lambda repo, tx: admit(tx, spend=-1),
        lambda repo, tx: admit(tx, spend=1_000_001),
        lambda repo, tx: admit(tx, spend=True),
        lambda repo, tx: admit(tx, admitted_at=-1),
        lambda repo, tx: admit(tx, assignment_id="assignment"),
        lambda repo, tx: admit(tx, job_id=SUBMISSION_ID),
        lambda repo, tx: admit(tx, claim_generation=0),
        lambda repo, tx: admit(tx, lease_owner="bad owner"),
        lambda repo, tx: repo.stop_assignment_job(
            tx, owner_id="owner-1", job_id=JOB_ID, expected_version=0, stopped_at=1
        ),
        lambda repo, tx: repo.stop_assignment_job(
            tx, owner_id="owner-1", job_id=JOB_ID, expected_version=1, stopped_at=-1
        ),
        lambda repo, tx: repo.stop_assignment_job(
            tx, owner_id=" ", job_id=JOB_ID, expected_version=1, stopped_at=1
        ),
        lambda repo, tx: repo.list_outstanding_episodes(tx, owner_id="owner-1", job_id="x"),
    ],
)
def test_policy_contract_validation_refuses_before_database(call) -> None:
    tx = ScriptedTransaction()
    with pytest.raises(ValueError):
        call(SchedulerRepository(), tx)
    assert tx.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"max_runs": 0},
        {"max_runs": 1_000_001},
        {"max_runs": -5},
        {"admitted_runs": -1},
        {"admitted_runs": 4},
        {"max_outstanding_episodes": 0},
        {"max_outstanding_episodes": 65},
        {"per_episode_limits": {"model_calls": -1}},
        {"per_episode_limits": {"model_calls": 2**31}},
        {"per_episode_limits": {"Model": 1}},
        {"per_episode_limits": {f"k{i}": 1 for i in range(33)}},
        {"per_episode_limits": [("model_calls", 1)]},
        {"per_episode_limits": (("model_calls", 1), ("model_calls", 2))},
        {"per_episode_limits": (("model_calls",),)},
        {"definition_revision": 0},
        {"version": 0},
        {"version": 2**53},
        {"monitor_changes": 1},
        {"terminal_stop": "no"},
        {"last_assignment_id": "not-a-uuid"},
        {"owner_id": ""},
        {"job_id": SUBMISSION_ID},
        {"updated_at": -1},
    ],
)
def test_policy_record_refuses_negative_or_oversized_limits(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(policy_value(), **changes)


def test_policy_record_canonicalizes_limits_and_projects_remaining_runs() -> None:
    value = policy_value(per_episode_limits={"tool_calls": 2, "model_calls": 4}, admitted_runs=2)
    assert value.per_episode_limits == (("model_calls", 4), ("tool_calls", 2))
    assert value.limits() == {"model_calls": 4, "tool_calls": 2}
    assert value.remaining_runs == 1
    assert policy_value(max_runs=None, admitted_runs=1_000_000).remaining_runs is None
    assert _policy(policy_row(per_episode_limits='{"model_calls": 4}')) == policy_value()
    assert _policy(policy_row(per_episode_limits=None)).per_episode_limits == ()
    for broken in (policy_row(per_episode_limits="[1]"), policy_row(version="x")):
        with pytest.raises(PlaneError) as invalid:
            _policy(broken)
        assert invalid.value.code == "scheduler_record_invalid"
    with pytest.raises(ValueError):
        EpisodeAdmission(
            JOB_ID, "owner-1", OCCURRENCE_ID, ASSIGNMENT_ID, True, True, "terminal_stop", 1, None
        )
    with pytest.raises(ValueError):
        EpisodeAdmission(
            JOB_ID, "owner-1", OCCURRENCE_ID, ASSIGNMENT_ID, True, True, "replayed", 1, None
        )
    with pytest.raises(ValueError):
        EpisodeAdmission(
            JOB_ID, "owner-1", OCCURRENCE_ID, ASSIGNMENT_ID, False, False, "nope", 1, None
        )
    with pytest.raises(ValueError):
        EpisodeAdmission(
            JOB_ID, "owner-1", OCCURRENCE_ID, ASSIGNMENT_ID, True, True, "admitted", 1, "policy"
        )
    with pytest.raises(ValueError):
        JobStopOutcome(JOB_ID, "owner-1", True, policy_value(), (), (), ())
    with pytest.raises(ValueError):
        JobStopOutcome(
            JOB_ID, "owner-1", True, policy_value(terminal_stop=True), (ASSIGNMENT_ID,) * 2, (), ()
        )
    with pytest.raises(ValueError):
        JobStopOutcome(JOB_ID, "owner-1", "yes", policy_value(terminal_stop=True), (), (), ())


def test_policy_put_and_get_are_owner_scoped_version_cas() -> None:
    repository = SchedulerRepository()
    assert (
        repository.get_job_policy(ScriptedTransaction(one=[None]), owner_id="other", job_id=JOB_ID)
        is None
    )
    tx = ScriptedTransaction(one=[policy_row()])
    assert repository.get_job_policy(tx, owner_id="owner-1", job_id=JOB_ID) == policy_value()
    assert tx.calls[0][2] == (JOB_ID, "owner-1")

    created = repository.put_job_policy(
        ScriptedTransaction(one=[{"id": JOB_ID}, None, policy_row()]),
        policy=policy_value(),
        expected_version=0,
    )
    assert created == policy_value()
    with pytest.raises(PlaneError) as missing_job:
        repository.put_job_policy(
            ScriptedTransaction(one=[None]), policy=policy_value(), expected_version=0
        )
    assert missing_job.value.code == "scheduled_job_missing"
    with pytest.raises(PlaneError) as exists:
        repository.put_job_policy(
            ScriptedTransaction(one=[{"id": JOB_ID}, policy_row()]),
            policy=policy_value(),
            expected_version=0,
        )
    assert exists.value.code == "scheduled_job_policy_version_conflict"
    assert dict(exists.value.metadata)["observed_version"] == "1"
    with pytest.raises(PlaneError) as lost_insert:
        repository.put_job_policy(
            ScriptedTransaction(one=[{"id": JOB_ID}, None, None]),
            policy=policy_value(),
            expected_version=0,
        )
    assert lost_insert.value.code == "scheduled_job_policy_version_conflict"
    assert dict(lost_insert.value.metadata)["observed_version"] == "<none>"

    tx = ScriptedTransaction(one=[{"id": JOB_ID}, policy_row(), policy_row(version=2, max_runs=5)])
    updated = repository.put_job_policy(
        tx, policy=policy_value(version=2, max_runs=5), expected_version=1
    )
    assert updated.max_runs == 5 and updated.version == 2
    assert tx.calls[-1][2][-1] == 1  # type: ignore[index]
    with pytest.raises(PlaneError) as no_policy:
        repository.put_job_policy(
            ScriptedTransaction(one=[{"id": JOB_ID}, None]),
            policy=policy_value(version=2),
            expected_version=1,
        )
    assert no_policy.value.code == "scheduled_job_policy_missing"
    with pytest.raises(PlaneError) as stale:
        repository.put_job_policy(
            ScriptedTransaction(one=[{"id": JOB_ID}, policy_row(version=3)]),
            policy=policy_value(version=2),
            expected_version=1,
        )
    assert stale.value.code == "scheduled_job_policy_version_conflict"
    with pytest.raises(PlaneError) as rewrite:
        repository.put_job_policy(
            ScriptedTransaction(one=[{"id": JOB_ID}, policy_row(admitted_runs=1)]),
            policy=policy_value(version=2, admitted_runs=0),
            expected_version=1,
        )
    assert rewrite.value.code == "scheduled_job_policy_charge_rewrite"
    with pytest.raises(PlaneError) as lost_update:
        repository.put_job_policy(
            ScriptedTransaction(one=[{"id": JOB_ID}, policy_row(), None]),
            policy=policy_value(version=2),
            expected_version=1,
        )
    assert lost_update.value.code == "scheduled_job_policy_version_conflict"


def test_admission_refusals_write_nothing_and_impossible_results_fail_closed() -> None:
    no_policy = ScriptedTransaction(one=[job_row(), None])
    refused = admit(no_policy)
    assert (refused.admitted, refused.reason, refused.policy) == (False, "policy_missing", None)
    assert [kind for kind, _, _ in no_policy.calls] == ["one", "one"]

    stopped = ScriptedTransaction(one=[job_row(), policy_row(terminal_stop=True), claimed_row()])
    assert admit(stopped).reason == "terminal_stop"
    assert len(stopped.calls) == 3

    with pytest.raises(PlaneError) as mismatch:
        admit(
            ScriptedTransaction(one=[job_row(), policy_row(), claimed_row(job_id=OTHER_JOB_ID)])
        )
    assert mismatch.value.code == "scheduled_occurrence_job_mismatch"
    with pytest.raises(PlaneError) as paused:
        admit(ScriptedTransaction(one=[job_row(status="paused")]))
    assert paused.value.code == "stale_occurrence_claim"
    with pytest.raises(PlaneError) as stale:
        admit(ScriptedTransaction(one=[job_row(), policy_row(), None]))
    assert stale.value.code == "stale_occurrence_claim"

    replay = admit(
        ScriptedTransaction(
            one=[
                job_row(),
                policy_row(admitted_runs=1),
                claimed_row(),
                {"assignment_id": ASSIGNMENT_ID, "spend": 1},
            ]
        )
    )
    assert (replay.admitted, replay.created, replay.reason, replay.spend) == (
        True,
        False,
        "replayed",
        1,
    )
    with pytest.raises(PlaneError) as bound_elsewhere:
        admit(
            ScriptedTransaction(
                one=[
                    job_row(),
                    policy_row(),
                    claimed_row(),
                    {"assignment_id": OTHER_ASSIGNMENT_ID, "spend": 1},
                ]
            )
        )
    assert bound_elsewhere.value.code == "scheduled_occurrence_binding_conflict"
    with pytest.raises(PlaneError) as missing:
        admit(ScriptedTransaction(one=[job_row(), policy_row(), claimed_row(), None, None]))
    assert missing.value.code == "scheduled_episode_assignment_missing"
    with pytest.raises(PlaneError) as resolved:
        admit(
            ScriptedTransaction(
                one=[job_row(), policy_row(), claimed_row(), None, {"lifecycle": "completed"}]
            )
        )
    assert resolved.value.code == "scheduled_episode_assignment_resolved"

    outstanding = ScriptedTransaction(
        one=[job_row(), policy_row(), claimed_row(), None, {"lifecycle": "active"}],
        all_rows=[({"assignment_id": OTHER_ASSIGNMENT_ID},)],
    )
    assert admit(outstanding).reason == "episode_outstanding"
    assert len(outstanding.calls) == 6
    exhausted = ScriptedTransaction(
        one=[
            job_row(),
            policy_row(max_runs=2, admitted_runs=2),
            claimed_row(),
            None,
            {"lifecycle": "active"},
        ],
        all_rows=[()],
    )
    assert admit(exhausted).reason == "allowance_exhausted"
    over_spend = ScriptedTransaction(
        one=[
            job_row(),
            policy_row(max_runs=2, admitted_runs=1),
            claimed_row(),
            None,
            {"lifecycle": "paused"},
        ],
        all_rows=[()],
    )
    assert admit(over_spend, spend=2).reason == "allowance_exhausted"

    admitted_tx = ScriptedTransaction(
        one=[
            job_row(),
            policy_row(max_runs=None),
            claimed_row(),
            None,
            {"lifecycle": "active"},
            {"occurrence_id": OCCURRENCE_ID},
            policy_row(
                max_runs=None, version=2, admitted_runs=1, last_assignment_id=ASSIGNMENT_ID
            ),
        ],
        all_rows=[()],
    )
    admitted = admit(admitted_tx)
    assert (admitted.admitted, admitted.created, admitted.reason) == (True, True, "admitted")
    assert admitted.policy == policy_value(
        max_runs=None, version=2, admitted_runs=1, last_assignment_id=ASSIGNMENT_ID
    )
    insert_parameters = admitted_tx.calls[-2][2]
    assert insert_parameters == (OCCURRENCE_ID, "owner-1", JOB_ID, ASSIGNMENT_ID, 1, 7)
    with pytest.raises(PlaneError) as lost_binding:
        admit(
            ScriptedTransaction(
                one=[job_row(), policy_row(), claimed_row(), None, {"lifecycle": "active"}, None],
                all_rows=[()],
            )
        )
    assert lost_binding.value.code == "scheduled_occurrence_binding_conflict"
    with pytest.raises(PlaneError) as lost_charge:
        admit(
            ScriptedTransaction(
                one=[
                    job_row(),
                    policy_row(),
                    claimed_row(),
                    None,
                    {"lifecycle": "active"},
                    {"occurrence_id": OCCURRENCE_ID},
                    None,
                ],
                all_rows=[()],
            )
        )
    assert lost_charge.value.code == "scheduled_job_policy_version_conflict"


def test_stop_is_terminal_idempotent_and_fails_closed_on_lost_fences() -> None:
    repository = SchedulerRepository()

    def stop(tx: ScriptedTransaction, expected_version: int = 1) -> JobStopOutcome:
        return repository.stop_assignment_job(
            tx, owner_id="owner-1", job_id=JOB_ID, expected_version=expected_version, stopped_at=9
        )

    with pytest.raises(PlaneError) as missing_job:
        stop(ScriptedTransaction(one=[None]))
    assert missing_job.value.code == "scheduled_job_missing"
    with pytest.raises(PlaneError) as missing_policy:
        stop(ScriptedTransaction(one=[{"id": JOB_ID}, None]))
    assert missing_policy.value.code == "scheduled_job_policy_missing"
    with pytest.raises(PlaneError) as stale:
        stop(ScriptedTransaction(one=[{"id": JOB_ID}, policy_row(version=2)]))
    assert stale.value.code == "scheduled_job_policy_version_conflict"

    repeat_tx = ScriptedTransaction(
        one=[{"id": JOB_ID}, policy_row(terminal_stop=True)],
        all_rows=[({"assignment_id": ASSIGNMENT_ID},)],
    )
    repeat = stop(repeat_tx)
    assert not repeat.stopped and repeat.outstanding_assignment_ids == (ASSIGNMENT_ID,)
    assert repeat.cancelled_occurrence_ids == () and repeat.policy.terminal_stop
    assert len(repeat_tx.calls) == 3

    stopped_policy = policy_row(version=2, terminal_stop=True, admitted_runs=1)
    other_occurrence = occurrence_row(
        occurrence_id=OTHER_OCCURRENCE_ID, current_operation_id=OPERATION_ID
    )
    tx = ScriptedTransaction(
        one=[
            {"id": JOB_ID},
            policy_row(admitted_runs=1),
            stopped_policy,
            occurrence_row(),
            other_occurrence,
        ],
        all_rows=[
            (occurrence_row(), other_occurrence),
            ({"assignment_id": ASSIGNMENT_ID}, {"assignment_id": ASSIGNMENT_ID}),
        ],
        execute=[Result(rowcount=1)],
    )
    outcome = stop(tx)
    assert outcome.stopped and outcome.policy == _policy(stopped_policy)
    assert outcome.cancelled_occurrence_ids == (OCCURRENCE_ID, OTHER_OCCURRENCE_ID)
    assert outcome.cancelled_operation_ids == (OPERATION_ID,)
    assert outcome.outstanding_assignment_ids == (ASSIGNMENT_ID,)
    statements = tx.fetch_sql()
    assert "SET status = 'completed', next_run_at = NULL" in statements
    assert statements.count("SET state = 'cancelled'") == 2
    assert tx.calls[-3][2][0] == "cancelled_job_stopped"  # type: ignore[index]

    with pytest.raises(PlaneError) as lost_policy:
        stop(ScriptedTransaction(one=[{"id": JOB_ID}, policy_row(), None]))
    assert lost_policy.value.code == "scheduled_job_policy_version_conflict"
    with pytest.raises(PlaneError) as lost_status:
        stop(
            ScriptedTransaction(
                one=[{"id": JOB_ID}, policy_row(), stopped_policy], execute=[Result(rowcount=0)]
            )
        )
    assert lost_status.value.code == "scheduled_job_status_conflict"
    with pytest.raises(PlaneError) as lost_cancel:
        stop(
            ScriptedTransaction(
                one=[
                    {"id": JOB_ID},
                    policy_row(),
                    stopped_policy,
                    None,
                    {"state": "completed", "current_operation_id": None},
                ],
                all_rows=[(occurrence_row(),)],
                execute=[Result(rowcount=1)],
            )
        )
    assert lost_cancel.value.code == "stale_occurrence_fence"
    outstanding_tx = ScriptedTransaction(all_rows=[({"assignment_id": ASSIGNMENT_ID},)])
    assert repository.list_outstanding_episodes(
        outstanding_tx, owner_id="owner-1", job_id=JOB_ID
    ) == (ASSIGNMENT_ID,)
    assert outstanding_tx.calls[0][2] == (JOB_ID, "owner-1")
