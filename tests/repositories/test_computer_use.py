"""Unit tests for the computer-use repository contract: owner-scoped SQL shapes,
record validation, replay identity, CAS fence misses, and one-way intent state
transitions against scripted transactions.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

import astralplane
from astralplane.api import create_repository_catalog
from astralplane.database import migrations as canonical
from astralplane.database.computer_use_schema import COMPUTER_USE_SCHEMA_STATEMENTS
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.computer_use import (
    ApprovalFence,
    ComputerSession,
    ComputerTaskGrant,
    ComputerUseRepository,
    HostEffectIntent,
    HostEffectIntentState,
)
from tests.repositories._support import ScriptedTransaction

OWNER = "computer-owner"
OTHER = "computer-other"
DIGEST = hashlib.sha256(b"synthetic-argument").hexdigest()
BINDING = hashlib.sha256(b"synthetic-binding").hexdigest()
SESSION_ID = str(uuid.UUID(int=1))
GRANT_ID = str(uuid.UUID(int=2))
INTENT_ID = str(uuid.UUID(int=3))
FENCE_ID = str(uuid.UUID(int=4))


def session_row(**changes):
    row = {
        "session_id": SESSION_ID,
        "owner_id": OWNER,
        "host_id": "mac-studio-1",
        "host_incarnation": "incarnation-7",
        "state": "active",
        "pause_reason": None,
        "revision": 1,
        "created_at": 10,
        "updated_at": 10,
        "ended_at": None,
    }
    row.update(changes)
    return row


def grant_row(**changes):
    row = {
        "grant_id": GRANT_ID,
        "owner_id": OWNER,
        "binding_digest": BINDING,
        "expires_at": 1000,
        "hard_cap": 3,
        "use_count": 0,
        "revision": 1,
        "created_at": 10,
        "revoked_at": None,
    }
    row.update(changes)
    return row


def intent_row(**changes):
    row = {
        "operation_id": INTENT_ID,
        "owner_id": OWNER,
        "verb": "screenshot",
        "argument_digest": DIGEST,
        "state": "pending",
        "created_at": 10,
        "updated_at": 10,
        "resolved_at": None,
        "acknowledged_at": None,
    }
    row.update(changes)
    return row


def fence_row(**changes):
    row = {
        "fence_id": FENCE_ID,
        "owner_id": OWNER,
        "continuation_digest": DIGEST,
        "state": "open",
        "created_at": 10,
        "updated_at": 10,
        "closed_at": None,
    }
    row.update(changes)
    return row


def session_record(**changes):
    values = dict(
        session_id=SESSION_ID,
        owner_id=OWNER,
        host_id="mac-studio-1",
        host_incarnation="incarnation-7",
        state="active",
        revision=1,
        created_at=10,
        updated_at=10,
    )
    values.update(changes)
    return ComputerSession(**values)


def grant_record(**changes):
    values = dict(
        grant_id=GRANT_ID,
        owner_id=OWNER,
        binding_digest=BINDING,
        expires_at=1000,
        hard_cap=3,
        use_count=0,
        revision=1,
        created_at=10,
    )
    values.update(changes)
    return ComputerTaskGrant(**values)


def intent_record(**changes):
    values = dict(
        operation_id=INTENT_ID,
        owner_id=OWNER,
        verb="screenshot",
        argument_digest=DIGEST,
        state=HostEffectIntentState.PENDING,
        created_at=10,
        updated_at=10,
    )
    values.update(changes)
    return HostEffectIntent(**values)


def fence_record(**changes):
    values = dict(
        fence_id=FENCE_ID,
        owner_id=OWNER,
        continuation_digest=DIGEST,
        state="open",
        created_at=10,
        updated_at=10,
    )
    values.update(changes)
    return ApprovalFence(**values)


def test_facade_exposes_computer_use_repository() -> None:
    catalog = create_repository_catalog()
    assert isinstance(catalog.computer_use, ComputerUseRepository)
    assert isinstance(catalog.as_mapping()["computer_use"], ComputerUseRepository)
    assert astralplane.ComputerUseRepository is ComputerUseRepository
    assert astralplane.create_computer_use_repository is not None
    assert astralplane.HostEffectIntentState.PENDING is HostEffectIntentState.PENDING


def test_089_018_computer_use_edge_is_pinned_to_the_exact_089_017_registry() -> None:
    edge = canonical.PLANE_SCHEMA_089_018_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-5] is edge
    assert edge.name == "astralplane-089-computer-use"
    assert edge.source_revisions == ("089.017",)
    assert edge.target_revision == "089.018"
    assert edge.checksum == canonical._statements_checksum(COMPUTER_USE_SCHEMA_STATEMENTS)

    ddl = "\n".join(COMPUTER_USE_SCHEMA_STATEMENTS)
    for table in (
        "computer_session",
        "computer_task_grant",
        "computer_host_effect_intent",
        "computer_approval_fence",
    ):
        assert f"CREATE TABLE {table}" in ddl
        assert f"('{table}')" in canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "IF NOT EXISTS" not in ddl
    assert "ALTER TABLE" not in ddl

    assert canonical.PLANE_SCHEMA_089_017_REGISTRY_DIGEST == (
        "99862c91fe45769d9b037987e80dbd3b685be5f7afbf0bafb2c895d15592c0f5"
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.017") == (
        canonical.PLANE_SCHEMA_089_017_REGISTRY_DIGEST
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.017"] == (
        "86af1f46f4909106c0ab736203acfdbca86f3ed105559a8972db4899faa8c03f",
    )
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )
    assert canonical.CURRENT_SCHEMA_VERIFIER_CHECKSUM == (
        "2aba6eb28dcfcea86eb53f704719332ff5dc3a8dcee37f26dae267f32e761d9c"
    )
    assert canonical.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "836964bd86b0329361aaa84d33712cdb07ee2277f62c143bca5ddd3fa31f627e"
    )
    assert canonical.MIGRATION_DIGEST == (
        "dde302967743cf8f6f5d55e82f8355ef405823dfee638909c3a2f11b85f5cb27"
    )


def test_session_record_validation_rejects_invalid_lifecycle() -> None:
    with pytest.raises(RepositoryValidationError):
        session_record(state="running")
    with pytest.raises(RepositoryValidationError):
        session_record(state="paused")
    with pytest.raises(RepositoryValidationError):
        session_record(pause_reason="user_requested")
    with pytest.raises(RepositoryValidationError):
        session_record(state="paused", pause_reason="user_requested", ended_at=20)
    with pytest.raises(RepositoryValidationError):
        session_record(ended_at=20)
    with pytest.raises(RepositoryValidationError):
        session_record(state="ended", ended_at=5)
    with pytest.raises(RepositoryValidationError):
        session_record(updated_at=5)
    with pytest.raises(RepositoryValidationError):
        session_record(revision=0)
    with pytest.raises(RepositoryValidationError):
        session_record(host_id="")
    with pytest.raises(RepositoryValidationError):
        session_record(session_id="not-a-uuid")
    with pytest.raises(RepositoryValidationError):
        session_record(state=1)


def test_session_open_replays_identity_and_refuses_conflicts() -> None:
    repository = ComputerUseRepository()
    created = repository.open_session(ScriptedTransaction(one=[session_row()]), session_record())
    assert created.state == "active"
    replayed = repository.open_session(
        ScriptedTransaction(one=[None, session_row()]),
        session_record(),
    )
    assert replayed == session_record()
    with pytest.raises(RepositoryConflictError, match="conflicting semantics"):
        repository.open_session(
            ScriptedTransaction(one=[None, session_row(revision=2)]),
            session_record(),
        )
    with pytest.raises(RepositoryConflictError, match="another owner"):
        repository.open_session(
            ScriptedTransaction(one=[None, None]),
            session_record(),
        )


def test_session_validation_rejects_non_string_and_nil_identifiers() -> None:
    with pytest.raises(RepositoryValidationError):
        session_record(session_id=None)
    with pytest.raises(RepositoryValidationError):
        session_record(session_id="00000000-0000-0000-0000-000000000000")
    with pytest.raises(RepositoryValidationError):
        session_record(session_id="00000000-0000-4000-8000-0000000000010")
    with pytest.raises(RepositoryValidationError):
        session_record(session_id="{00000000-0000-4000-8000-000000000001}")


def test_intent_and_fence_record_validation_completes_the_lifecycle_matrix() -> None:
    with pytest.raises(RepositoryValidationError):
        intent_record(updated_at=5)
    with pytest.raises(RepositoryValidationError):
        intent_record(state="pending")
    with pytest.raises(RepositoryValidationError):
        fence_record(state="open", closed_at=20)
    with pytest.raises(RepositoryValidationError):
        fence_record(state="consumed", closed_at=5)
    with pytest.raises(RepositoryValidationError):
        fence_record(state="open", updated_at=5)


def test_intent_and_fence_corrupt_rows_and_write_misses_fail_closed() -> None:
    repository = ComputerUseRepository()
    for corrupt in (
        session_row(created_at="late"),
        session_row(state="detached"),
        session_row(revision=None),
    ):
        with pytest.raises(RepositoryDataError):
            repository.get_session(
                ScriptedTransaction(one=[corrupt]),
                owner_id=OWNER,
                session_id=SESSION_ID,
            )
    for corrupt in (grant_row(use_count="nine"), grant_row(binding_digest=None)):
        with pytest.raises(RepositoryDataError):
            repository.get_task_grant(
                ScriptedTransaction(one=[corrupt]),
                owner_id=OWNER,
                grant_id=GRANT_ID,
            )
    for corrupt in (
        intent_row(created_at="late"),
        intent_row(state="deferred"),
        intent_row(verb=None),
    ):
        with pytest.raises(RepositoryDataError):
            repository.get_host_effect_intent(
                ScriptedTransaction(one=[corrupt]),
                owner_id=OWNER,
                operation_id=INTENT_ID,
            )
    for corrupt in (fence_row(created_at="late"), fence_row(state="void")):
        with pytest.raises(RepositoryDataError):
            repository.get_approval_fence(
                ScriptedTransaction(one=[corrupt]),
                owner_id=OWNER,
                fence_id=FENCE_ID,
            )
    with pytest.raises(RepositoryNotFoundError):
        repository.mark_host_effect_intent_unknown(
            ScriptedTransaction(one=[None, None]),
            owner_id=OWNER,
            operation_id=INTENT_ID,
            resolved_at=20,
        )
    with pytest.raises(RepositoryConflictError, match="another owner"):
        repository.create_approval_fence(
            ScriptedTransaction(one=[None, None]),
            fence_record(),
        )
    with pytest.raises(RepositoryNotFoundError, match="not found"):
        repository.end_session(
            ScriptedTransaction(one=[None, None]),
            owner_id=OTHER,
            session_id=SESSION_ID,
            expected_revision=1,
            ended_at=20,
        )
    with pytest.raises(RepositoryConflictError, match="revision fence"):
        repository.end_session(
            ScriptedTransaction(one=[None, session_row()]),
            owner_id=OWNER,
            session_id=SESSION_ID,
            expected_revision=9007199254740991,
            ended_at=20,
        )
    with pytest.raises(RepositoryNotFoundError, match="not found"):
        repository.revoke_task_grant(
            ScriptedTransaction(one=[None, None]),
            owner_id=OTHER,
            grant_id=GRANT_ID,
            expected_revision=1,
            revoked_at=20,
        )
    with pytest.raises(RepositoryConflictError, match="revision fence"):
        repository.revoke_task_grant(
            ScriptedTransaction(one=[None, grant_row()]),
            owner_id=OWNER,
            grant_id=GRANT_ID,
            expected_revision=9007199254740991,
            revoked_at=20,
        )


def test_session_reads_are_owner_scoped_and_validated() -> None:
    repository = ComputerUseRepository()
    transaction = ScriptedTransaction(one=[session_row()])
    found = repository.get_session(transaction, owner_id=OWNER, session_id=SESSION_ID)
    assert found == session_record()
    assert "owner_id = %s" in transaction.calls[0][1]
    assert transaction.calls[0][2] == (SESSION_ID, OWNER)
    assert repository.get_session(ScriptedTransaction(one=[None]), owner_id=OWNER,
                                  session_id=SESSION_ID) is None
    with pytest.raises(RepositoryValidationError):
        repository.get_session(ScriptedTransaction(), owner_id=OWNER, session_id="not-a-uuid")
    with pytest.raises(RepositoryValidationError):
        repository.get_session(ScriptedTransaction(), owner_id="", session_id=SESSION_ID)
    listed = ScriptedTransaction(all_rows=((session_row(),),))
    assert repository.list_open_sessions(listed, owner_id=OWNER) == (session_record(),)
    assert "state <> 'ended'" in listed.calls[0][1]
    with pytest.raises(RepositoryValidationError):
        repository.list_open_sessions(ScriptedTransaction(), owner_id=OWNER, limit=0)
    with pytest.raises(RepositoryValidationError):
        repository.list_open_sessions(ScriptedTransaction(), owner_id=OWNER, limit=1001)


def test_session_transitions_are_revision_fenced_and_typed_on_miss() -> None:
    repository = ComputerUseRepository()
    paused = repository.pause_session(
        ScriptedTransaction(one=[session_row(state="paused", pause_reason="user_requested",
                                            revision=2, updated_at=11)]),
        owner_id=OWNER,
        session_id=SESSION_ID,
        expected_revision=1,
        pause_reason="user_requested",
        updated_at=11,
    )
    assert paused.state == "paused"
    miss_missing = ScriptedTransaction(one=[None, None])
    with pytest.raises(RepositoryNotFoundError):
        repository.pause_session(
            miss_missing,
            owner_id=OWNER,
            session_id=SESSION_ID,
            expected_revision=1,
            pause_reason="user_requested",
            updated_at=11,
        )
    miss_stale = ScriptedTransaction(one=[None, session_row()])
    with pytest.raises(RepositoryConflictError, match="revision fence"):
        repository.pause_session(
            miss_stale,
            owner_id=OWNER,
            session_id=SESSION_ID,
            expected_revision=9007199254740991,
            pause_reason="user_requested",
            updated_at=11,
        )
    with pytest.raises(RepositoryValidationError):
        repository.pause_session(
            ScriptedTransaction(),
            owner_id=OWNER,
            session_id=SESSION_ID,
            expected_revision=1,
            pause_reason="x" * 65,
            updated_at=11,
        )
    resumed = repository.resume_session(
        ScriptedTransaction(one=[session_row(revision=3, updated_at=12)]),
        owner_id=OWNER,
        session_id=SESSION_ID,
        expected_revision=2,
        updated_at=12,
    )
    assert resumed.state == "active"
    ended = repository.end_session(
        ScriptedTransaction(one=[session_row(state="ended", ended_at=13, revision=4,
                                             updated_at=13)]),
        owner_id=OWNER,
        session_id=SESSION_ID,
        expected_revision=3,
        ended_at=13,
    )
    assert ended.state == "ended"
    with pytest.raises(RepositoryValidationError):
        repository.end_session(
            ScriptedTransaction(),
            owner_id=OWNER,
            session_id=SESSION_ID,
            expected_revision=3,
            ended_at=-1,
        )


def test_ended_session_cleanup_is_owner_scoped_and_bounded() -> None:
    repository = ComputerUseRepository()
    transaction = ScriptedTransaction(all_rows=(({"session_id": SESSION_ID},),))
    deleted = repository.delete_ended_sessions_before(
        transaction, owner_id=OWNER, ended_before=100, limit=5
    )
    assert deleted == (SESSION_ID,)
    statement = transaction.calls[0][1]
    assert "state = 'ended'" in statement
    assert "doomed.ended_at <= %s" in statement
    assert "LIMIT %s" in statement
    assert transaction.calls[0][2] == (OWNER, 100, 5)


def test_task_grant_validation_and_exhaustion_property() -> None:
    with pytest.raises(RepositoryValidationError):
        grant_record(binding_digest="Z" * 64)
    with pytest.raises(RepositoryValidationError):
        grant_record(hard_cap=0)
    with pytest.raises(RepositoryValidationError):
        grant_record(use_count=4)
    with pytest.raises(RepositoryValidationError):
        grant_record(expires_at=5)
    with pytest.raises(RepositoryValidationError):
        grant_record(revoked_at=5)
    assert grant_record(use_count=3, hard_cap=3).exhausted
    assert not grant_record().exhausted
    assert grant_record(revoked_at=20).revoked_at == 20


def test_task_grant_replay_conflicts_and_owner_scoped_reads() -> None:
    repository = ComputerUseRepository()
    created = repository.create_task_grant(ScriptedTransaction(one=[grant_row()]), grant_record())
    assert created == grant_record()
    with pytest.raises(RepositoryConflictError, match="conflicting semantics"):
        repository.create_task_grant(
            ScriptedTransaction(one=[None, grant_row(hard_cap=9)]),
            grant_record(),
        )
    with pytest.raises(RepositoryConflictError, match="another owner"):
        repository.create_task_grant(
            ScriptedTransaction(one=[None, None]),
            grant_record(),
        )
    assert repository.get_task_grant(
        ScriptedTransaction(one=[None]), owner_id=OTHER, grant_id=GRANT_ID
    ) is None
    listed = ScriptedTransaction(all_rows=((grant_row(),),))
    assert repository.list_active_task_grants(listed, owner_id=OWNER, now=20) == (grant_record(),)
    assert "revoked_at IS NULL" in listed.calls[0][1]
    assert "expires_at > %s" in listed.calls[0][1]
    assert listed.calls[0][2] == (OWNER, 20, 200)


def test_task_grant_consume_and_revoke_fences_fail_closed() -> None:
    repository = ComputerUseRepository()
    consume_tx = ScriptedTransaction(one=[grant_row(use_count=1, revision=2)])
    consumed = repository.consume_task_grant(
        consume_tx,
        owner_id=OWNER,
        grant_id=GRANT_ID,
        expected_revision=1,
        now=20,
    )
    assert consumed.use_count == 1
    assert "use_count < hard_cap" in consume_tx.calls[0][1]
    assert "owner_id = %s" in consume_tx.calls[0][1]
    assert consume_tx.calls[0][2] == (GRANT_ID, OWNER, 1, 20)
    with pytest.raises(RepositoryNotFoundError):
        repository.consume_task_grant(
            ScriptedTransaction(one=[None, None]),
            owner_id=OWNER,
            grant_id=GRANT_ID,
            expected_revision=1,
            now=20,
        )
    with pytest.raises(RepositoryConflictError, match="revision fence"):
        repository.consume_task_grant(
            ScriptedTransaction(one=[None, grant_row()]),
            owner_id=OWNER,
            grant_id=GRANT_ID,
            expected_revision=9007199254740991,
            now=20,
        )
    revoked = repository.revoke_task_grant(
        ScriptedTransaction(one=[grant_row(revoked_at=30, revision=2)]),
        owner_id=OWNER,
        grant_id=GRANT_ID,
        expected_revision=1,
        revoked_at=30,
    )
    assert revoked.revoked_at == 30
    with pytest.raises(RepositoryValidationError):
        repository.revoke_task_grant(
            ScriptedTransaction(),
            owner_id=OWNER,
            grant_id=GRANT_ID,
            expected_revision=1,
            revoked_at=-5,
        )
    transaction = ScriptedTransaction(all_rows=(({"grant_id": GRANT_ID},),))
    deleted = repository.delete_expired_task_grants_before(
        transaction, owner_id=OWNER, expired_before=2000, limit=3
    )
    assert deleted == (GRANT_ID,)
    assert "doomed.expires_at <= %s" in transaction.calls[0][1]
    assert "doomed.revoked_at IS NOT NULL" in transaction.calls[0][1]


def test_intent_record_validation_and_digest_only_storage() -> None:
    with pytest.raises(RepositoryValidationError):
        intent_record(verb="Screenshot")
    with pytest.raises(RepositoryValidationError):
        intent_record(verb="x" * 65)
    with pytest.raises(RepositoryValidationError):
        intent_record(argument_digest="short")
    with pytest.raises(RepositoryValidationError):
        intent_record(resolved_at=20)
    with pytest.raises(RepositoryValidationError):
        intent_record(
            state=HostEffectIntentState.COMMITTED,
            resolved_at=5,
            updated_at=20,
        )
    with pytest.raises(RepositoryValidationError):
        intent_record(
            state=HostEffectIntentState.ACKNOWLEDGED,
            resolved_at=20,
            acknowledged_at=19,
            updated_at=30,
        )
    with pytest.raises(RepositoryValidationError):
        intent_record(
            state=HostEffectIntentState.COMMITTED,
            resolved_at=20,
            acknowledged_at=21,
            updated_at=30,
        )
    committed = intent_record(
        state=HostEffectIntentState.COMMITTED, resolved_at=20, updated_at=20
    )
    assert committed.state is HostEffectIntentState.COMMITTED


def test_intent_replay_identity_and_cross_owner_conflict() -> None:
    repository = ComputerUseRepository()
    recorded = repository.record_host_effect_intent(
        ScriptedTransaction(one=[intent_row()]), intent_record()
    )
    assert recorded.state is HostEffectIntentState.PENDING
    insert_tx = ScriptedTransaction(one=[intent_row()])
    repository.record_host_effect_intent(insert_tx, intent_record())
    assert "argument_digest" in insert_tx.calls[0][1]
    with pytest.raises(RepositoryConflictError, match="conflicting semantics"):
        repository.record_host_effect_intent(
            ScriptedTransaction(one=[None, intent_row(argument_digest="0" * 64)]),
            intent_record(),
        )
    with pytest.raises(RepositoryConflictError, match="another owner"):
        repository.record_host_effect_intent(
            ScriptedTransaction(one=[None, None]),
            intent_record(),
        )
    assert repository.get_host_effect_intent(
        ScriptedTransaction(one=[None]), owner_id=OTHER, operation_id=INTENT_ID
    ) is None
    open_intents = ScriptedTransaction(all_rows=((intent_row(),),))
    listed = repository.list_open_host_effect_intents(open_intents, owner_id=OWNER)
    assert listed == (intent_record(),)
    assert "state = 'pending'" in open_intents.calls[0][1]
    with pytest.raises(RepositoryValidationError):
        repository.list_open_host_effect_intents(open_intents, owner_id=OWNER, limit="many")


def test_intent_state_transitions_are_one_way_and_typed() -> None:
    repository = ComputerUseRepository()
    committed = repository.commit_host_effect_intent(
        ScriptedTransaction(one=[intent_row(state="committed", resolved_at=20, updated_at=20)]),
        owner_id=OWNER,
        operation_id=INTENT_ID,
        resolved_at=20,
    )
    assert committed.state is HostEffectIntentState.COMMITTED
    replay = repository.commit_host_effect_intent(
        ScriptedTransaction(one=[None, intent_row(state="committed", resolved_at=20,
                                                  updated_at=20)]),
        owner_id=OWNER,
        operation_id=INTENT_ID,
        resolved_at=21,
    )
    assert replay.state is HostEffectIntentState.COMMITTED
    with pytest.raises(RepositoryConflictError, match="cannot commit"):
        repository.commit_host_effect_intent(
            ScriptedTransaction(one=[None, intent_row(state="unknown", resolved_at=20,
                                                      updated_at=20)]),
            owner_id=OWNER,
            operation_id=INTENT_ID,
            resolved_at=21,
        )
    with pytest.raises(RepositoryNotFoundError):
        repository.commit_host_effect_intent(
            ScriptedTransaction(one=[None, None]),
            owner_id=OWNER,
            operation_id=INTENT_ID,
            resolved_at=21,
        )
    unknown = repository.mark_host_effect_intent_unknown(
        ScriptedTransaction(one=[intent_row(state="unknown", resolved_at=20, updated_at=20)]),
        owner_id=OWNER,
        operation_id=INTENT_ID,
        resolved_at=20,
    )
    assert unknown.state is HostEffectIntentState.UNKNOWN
    unknown_replay = repository.mark_host_effect_intent_unknown(
        ScriptedTransaction(one=[None, intent_row(state="unknown", resolved_at=20,
                                                  updated_at=20)]),
        owner_id=OWNER,
        operation_id=INTENT_ID,
        resolved_at=21,
    )
    assert unknown_replay.state is HostEffectIntentState.UNKNOWN
    with pytest.raises(RepositoryConflictError, match="cannot become unknown"):
        repository.mark_host_effect_intent_unknown(
            ScriptedTransaction(one=[None, intent_row(state="committed", resolved_at=20,
                                                      updated_at=20)]),
            owner_id=OWNER,
            operation_id=INTENT_ID,
            resolved_at=21,
        )
    acknowledged = repository.acknowledge_host_effect_intent(
        ScriptedTransaction(one=[intent_row(state="acknowledged", resolved_at=20,
                                            acknowledged_at=30, updated_at=30)]),
        owner_id=OWNER,
        operation_id=INTENT_ID,
        acknowledged_at=30,
    )
    assert acknowledged.state is HostEffectIntentState.ACKNOWLEDGED
    with pytest.raises(RepositoryConflictError, match="must resolve before acknowledgment"):
        repository.acknowledge_host_effect_intent(
            ScriptedTransaction(one=[None, intent_row()]),
            owner_id=OWNER,
            operation_id=INTENT_ID,
            acknowledged_at=30,
        )
    acknowledged_replay = repository.acknowledge_host_effect_intent(
        ScriptedTransaction(one=[None, intent_row(state="acknowledged", resolved_at=20,
                                                  acknowledged_at=30, updated_at=30)]),
        owner_id=OWNER,
        operation_id=INTENT_ID,
        acknowledged_at=31,
    )
    assert acknowledged_replay.state is HostEffectIntentState.ACKNOWLEDGED
    with pytest.raises(RepositoryNotFoundError):
        repository.acknowledge_host_effect_intent(
            ScriptedTransaction(one=[None, None]),
            owner_id=OWNER,
            operation_id=INTENT_ID,
            acknowledged_at=31,
        )


def test_bulk_unknown_transition_is_owner_scoped_and_bounded() -> None:
    repository = ComputerUseRepository()
    transaction = ScriptedTransaction(
        all_rows=((intent_row(state="unknown", resolved_at=50, updated_at=50),),)
    )
    marked = repository.mark_host_effect_intents_unknown(
        transaction, owner_id=OWNER, resolved_at=50, limit=25
    )
    assert marked[0].state is HostEffectIntentState.UNKNOWN
    statement = transaction.calls[0][1]
    assert "intent.state = 'pending'" in statement
    assert "LIMIT %s" in statement
    assert transaction.calls[0][2] == (50, 50, OWNER, OWNER, 25)
    with pytest.raises(RepositoryValidationError):
        repository.mark_host_effect_intents_unknown(
            ScriptedTransaction(), owner_id=OWNER, resolved_at=-1
        )


def test_approval_fence_validation_and_close_fences() -> None:
    repository = ComputerUseRepository()
    with pytest.raises(RepositoryValidationError):
        fence_record(state="closed")
    with pytest.raises(RepositoryValidationError):
        fence_record(closed_at=20)
    with pytest.raises(RepositoryValidationError):
        fence_record(state="consumed")
    with pytest.raises(RepositoryValidationError):
        fence_record(continuation_digest="nope")
    created = repository.create_approval_fence(ScriptedTransaction(one=[fence_row()]),
                                               fence_record())
    assert created.state == "open"
    with pytest.raises(RepositoryConflictError, match="conflicting semantics"):
        repository.create_approval_fence(
            ScriptedTransaction(one=[None, fence_row(continuation_digest="0" * 64)]),
            fence_record(),
        )
    consumed = repository.consume_approval_fence(
        ScriptedTransaction(one=[fence_row(state="consumed", closed_at=20, updated_at=20)]),
        owner_id=OWNER,
        fence_id=FENCE_ID,
        closed_at=20,
    )
    assert consumed.state == "consumed"
    released = repository.release_approval_fence(
        ScriptedTransaction(one=[fence_row(state="released", closed_at=21, updated_at=21)]),
        owner_id=OWNER,
        fence_id=FENCE_ID,
        closed_at=21,
    )
    assert released.state == "released"
    with pytest.raises(RepositoryConflictError, match="already closed"):
        repository.consume_approval_fence(
            ScriptedTransaction(one=[None, fence_row(state="consumed", closed_at=20,
                                                     updated_at=20)]),
            owner_id=OWNER,
            fence_id=FENCE_ID,
            closed_at=22,
        )
    with pytest.raises(RepositoryNotFoundError):
        repository.release_approval_fence(
            ScriptedTransaction(one=[None, None]),
            owner_id=OWNER,
            fence_id=FENCE_ID,
            closed_at=22,
        )
    assert repository.get_approval_fence(
        ScriptedTransaction(one=[None]), owner_id=OTHER, fence_id=FENCE_ID
    ) is None
    open_fences = ScriptedTransaction(all_rows=((fence_row(),),))
    assert repository.list_open_approval_fences(open_fences, owner_id=OWNER) == (fence_record(),)
    assert "state = 'open'" in open_fences.calls[0][1]
