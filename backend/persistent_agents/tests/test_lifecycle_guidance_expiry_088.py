"""Real DB-time expiry after lifecycle writes, with synthetic external IAM only.

The note is bound through public Plane APIs before claim. These no-output
lifecycle witnesses do not claim selected Work ingress or model execution.
"""

import time
from dataclasses import asdict
from types import SimpleNamespace
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
