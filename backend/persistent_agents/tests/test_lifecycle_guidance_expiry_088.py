"""Real DB-time expiry after lifecycle writes, with synthetic external IAM only.

The note is bound through public Plane APIs before claim. These no-output
lifecycle witnesses do not claim selected Work ingress or model execution.
"""

import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest
from astralplane.repositories.guidance_models import ExplicitNoteRecord, GuidanceReference
from personalization.explicit_notes import ExplicitNoteCipher, ExplicitNoteMetadata
from persistent_agents.models import AssignmentError
from persistent_agents.runner import OneShotEpisodeResult, _episode_lease
from persistent_agents.tests import test_one_shot_lifecycle_postgres_088 as original
from persistent_agents.tests.test_one_shot_lifecycle_postgres_088 import (
    admission,
    authority,
    completion,
    current,
    fixture as fixture,
    lifecycle as lifecycle,
    runtime as runtime,
    signing_key as signing_key,
)
from orchestrator.work_admission import OperationState


@pytest.fixture
def selected_note(monkeypatch):
    create = original.create_operation
    selected = SimpleNamespace()

    def create_selected(fixture, runtime):
        record = create(fixture, runtime)
        notes = runtime.repositories.preferences.personalization
        with runtime.transaction() as tx:
            prepared = notes.prepare_explicit_note(
                tx, owner_id=record.owner_id, note_id=str(uuid4()), expected_revision=0
            )
            metadata = ExplicitNoteMetadata(
                owner_id=record.owner_id, note_id=prepared.note_id, revision=1,
                format_version=1, category="context", enabled=True,
                created_at=prepared.observed_at_ms, updated_at=prepared.observed_at_ms,
                expires_at=prepared.observed_at_ms + 3000,
            )
            encrypted = ExplicitNoteCipher(Fernet(Fernet.generate_key())).seal(
                metadata, "Synthetic private guidance."
            )
            selected.note = notes.put_explicit_note(
                tx, preparation=prepared,
                record=ExplicitNoteRecord(**asdict(metadata), ciphertext=encrypted.ciphertext),
            )
            return runtime.repositories.assignments.bind_guidance_references(
                tx, owner_id=record.owner_id, assignment_id=record.assignment_id,
                expected_instruction_revision=record.instruction_revision,
                expected_control_epoch=record.control_epoch,
                expected_state_version=record.state_version,
                references=(GuidanceReference("note", metadata.note_id, 1),),
            )

    monkeypatch.setattr(original, "create_operation", create_selected)
    return selected


@pytest.fixture
async def guided(selected_note, lifecycle):
    lifecycle.note = selected_note.note
    return lifecycle


def cross_expiry(note):
    # The real database expiry is unchanged. This bounded host wait runs after
    # the real write; it is not a fabricated SQL clock or lock-contention claim.
    remaining = note.expires_at / 1000 - time.time()
    assert 0 < remaining < 3
    time.sleep(remaining + 0.03)


@pytest.mark.parametrize("kind", ["renew", "finish"])
@pytest.mark.parametrize("wait_at", ["write", "final_session"])
async def test_expiry_after_write_rolls_back_claim_and_admission(
    guided, monkeypatch, kind, wait_at
):
    op = guided
    before = await current(op)
    admission_before = await admission(op)
    observed = await authority(op)
    wrote = []
    if wait_at == "final_session":
        sessions = op.runtime.repositories.history.sessions
        check = sessions.assert_current_execution

        def delayed_check(*args, **kwargs):
            result = check(*args, **kwargs)
            if wrote:
                cross_expiry(op.note)
            return result

        monkeypatch.setattr(sessions, "assert_current_execution", delayed_check)

    def after_write(result):
        wrote.append(result)
        if wait_at == "write":
            cross_expiry(op.note)
        return result

    if kind == "renew":
        def renew(tx, repo, _current):
            repo.renew_claim(tx, fence=op.executor.claim.fence, lease_seconds=30)
            return after_write(op.coordinator.renew_execution_lease(
                op.executor.operation_fence, transaction=tx
            ))

        async def execute():
            return await op.store.operation_lifecycle_transaction(
                authority=observed, fence=op.executor.claim.fence,
                binding=op.executor.binding, callback=renew,
            )
    else:
        terminalize = op.coordinator.terminalize
        monkeypatch.setattr(
            op.coordinator, "terminalize",
            lambda *a, **kw: after_write(terminalize(*a, **kw)),
        )

        async def execute():
            return await op.runner._finish_operation(
                op.executor, OneShotEpisodeResult(
                    before, completion(before, completed=True, next_wake_at=None)
                ),
            )

    with pytest.raises(AssignmentError, match="assignment_guidance_changed"):
        await execute()
    assert len(wrote) == 1
    assert await current(op) == before
    assert await admission(op) == admission_before
    assert admission_before.state == OperationState.RUNNING
    assert not _episode_lease(op.executor).terminal
    # Expiry is logical: no maintenance worker has erased this ciphertext yet.
    with op.runtime.transaction() as tx:
        row = tx.fetch_one(
            "SELECT ciphertext FROM explicit_note_current WHERE owner_id=%s AND note_id=%s",
            (op.note.owner_id, op.note.note_id),
        )
        assert row is not None and bytes(row["ciphertext"]) == op.note.ciphertext


async def test_current_note_allows_completion_after_old_execution_fence_retires(guided):
    op = guided
    before = await current(op)
    result = await op.runner._finish_operation(
        op.executor,
        OneShotEpisodeResult(before, completion(before, completed=True, next_wake_at=None)),
    )
    assert result.lifecycle == "completed"
    assert (await admission(op)).state == OperationState.COMPLETED
    assert _episode_lease(op.executor).terminal
    with pytest.raises(AssignmentError):
        await op.store.call("assert_current_claim", fence=op.executor.claim.fence)


async def cutoff_during_final_guidance_read(op, monkeypatch, kind):
    before = await current(op)
    admission_before = await admission(op)
    observed = await authority(op)
    observed = replace(observed, observation=replace(
        observed.observation, valid_until=datetime.now(UTC) + timedelta(milliseconds=400)
    ))
    monkeypatch.setattr(op.runner, "_operation_authority", AsyncMock(return_value=observed))
    check = op.store.repository.assert_guidance_current
    final_reads = []

    def delayed_guidance(*args, **kwargs):
        # Delay only the new final public guard, after its preceding session
        # observation. Its own final DB clock must enforce that same cutoff.
        final_reads.append(True)
        remaining = (observed.observation.valid_until - datetime.now(UTC)).total_seconds()
        assert 0 < remaining < 0.4
        time.sleep(remaining + 0.03)
        return check(*args, **kwargs)

    monkeypatch.setattr(op.store.repository, "assert_guidance_current", delayed_guidance)
    with pytest.raises(AssignmentError, match="assignment_guidance_changed"):
        if kind == "renew":
            def renew(tx, repo, _current):
                repo.renew_claim(tx, fence=op.executor.claim.fence, lease_seconds=30)
                return op.coordinator.renew_execution_lease(
                    op.executor.operation_fence, transaction=tx
                )

            await op.store.operation_lifecycle_transaction(
                authority=observed, fence=op.executor.claim.fence,
                binding=op.executor.binding, callback=renew,
            )
        else:
            await op.runner._finish_operation(
                op.executor, OneShotEpisodeResult(
                    before, completion(before, completed=True, next_wake_at=None)
                ),
            )
    assert final_reads == [True]
    assert await current(op) == before
    assert await admission(op) == admission_before
    assert not _episode_lease(op.executor).terminal


@pytest.mark.parametrize("kind", ["renew", "finish"])
async def test_original_cutoff_crossed_by_final_selected_read(guided, monkeypatch, kind):
    await cutoff_during_final_guidance_read(guided, monkeypatch, kind)


@pytest.mark.parametrize("kind", ["renew", "finish"])
async def test_original_cutoff_crossed_by_final_absent_selection_read(
    lifecycle, monkeypatch, kind
):
    await cutoff_during_final_guidance_read(lifecycle, monkeypatch, kind)


@pytest.mark.parametrize("retired", [False, True])
async def test_final_local_identity_check_follows_last_db_read(
    lifecycle, monkeypatch, retired
):
    op = lifecycle
    before = await current(op)
    admission_before = await admission(op)
    observed = await authority(op)
    check = op.store.repository.assert_guidance_current
    events = []
    local = {"current": True}

    def guidance(*args, **kwargs):
        result = check(*args, **kwargs)
        events.append("database")
        if retired:
            local["current"] = False
        return result

    monkeypatch.setattr(op.store.repository, "assert_guidance_current", guidance)

    def renew(tx, repo, _current):
        events.append("write")
        repo.renew_claim(tx, fence=op.executor.claim.fence, lease_seconds=30)
        return op.coordinator.renew_execution_lease(
            op.executor.operation_fence, transaction=tx
        )

    def final_check():
        events.append("local")
        if not local["current"]:
            raise AssignmentError("work_selection_unavailable", 503)

    async def execute():
        return await op.store.operation_lifecycle_transaction(
            authority=observed, fence=op.executor.claim.fence,
            binding=op.executor.binding, callback=renew, final_check=final_check,
        )

    if retired:
        with pytest.raises(AssignmentError, match="work_selection_unavailable"):
            await execute()
        assert await current(op) == before
        assert await admission(op) == admission_before
    else:
        assert await execute() is not None
        assert (await current(op)).state_version == before.state_version + 1
        assert (await admission(op)).state == OperationState.RUNNING
    assert events == ["write", "database", "local"]


@pytest.mark.parametrize("invalid", ["noncallable", "awaitable", "false", "true"])
async def test_final_local_check_contract_failure_cannot_commit(lifecycle, invalid):
    op = lifecycle
    before = await current(op)
    admission_before = await admission(op)
    observed = await authority(op)
    wrote = []
    coroutines = []

    async def asynchronous():
        pytest.fail("a final local check must never run asynchronously")

    def check():
        if invalid == "awaitable":
            value = asynchronous()
            coroutines.append(value)
            return value
        return invalid == "true"

    def renew(tx, repo, _current):
        wrote.append(True)
        repo.renew_claim(tx, fence=op.executor.claim.fence, lease_seconds=30)
        return op.coordinator.renew_execution_lease(
            op.executor.operation_fence, transaction=tx
        )

    with pytest.raises(AssignmentError, match="assignment_transaction_callback_invalid"):
        await op.store.operation_lifecycle_transaction(
            authority=observed, fence=op.executor.claim.fence,
            binding=op.executor.binding, callback=renew,
            final_check=object() if invalid == "noncallable" else check,
        )
    assert wrote == ([] if invalid == "noncallable" else [True])
    assert await current(op) == before
    assert await admission(op) == admission_before
    assert all(value.cr_frame is None for value in coroutines)
