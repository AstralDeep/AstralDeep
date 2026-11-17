"""Real PostgreSQL tests bind durable stop epochs, owner isolation, immutable
receipts and admission locks to caller-owned transactions. Synthetic records cover
restart, stale state, concurrent first stops and rollback failures."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import (
    independent_database,
    parallel_transactions,
    standalone_database,
)

import astralplane
from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.stop_epochs import (
    OwnerStoppedError,
    StopEpochConflictError,
    StopEpochRepository,
)
from astralplane.repositories.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionRepository,
)

AT = datetime(2026, 10, 10, tzinfo=UTC)


@pytest.fixture(autouse=True)
def empty_stop_inventory(database):
    with database.transaction() as tx:
        tx.execute("DELETE FROM peer_stop_acknowledgment")
        tx.execute("DELETE FROM owner_stop_operation_epoch")
        tx.execute("DELETE FROM owner_stop_epoch")


@contextmanager
def isolated():
    generator = standalone_database()
    db = next(generator)
    try:
        yield db
    finally:
        generator.close()


def engage(tx, repo=None, **changes):
    values = dict(
        owner_id="stop-owner",
        expected_revision=0,
        reason="operator stop",
        actor_id="owner-device",
        at=AT,
    )
    return (repo or StopEpochRepository()).engage(tx, **(values | changes))


def acknowledge(tx, repo, **changes):
    values = dict(
        owner_id="stop-owner",
        mesh_id="mesh-one",
        peer_id="peer-one",
        epoch=1,
        expected_revision=1,
        receipt_digest="a" * 64,
        at=AT,
    )
    return repo.acknowledge(tx, **(values | changes))


def test_stop_owner_isolation():
    repo = StopEpochRepository()
    with isolated() as db, db.transaction() as tx:
        assert repo.get(tx, owner_id="stop-owner") is None
        assert repo.get(tx, owner_id="stop-owner", for_update=True) is None
        state = engage(tx, repo)
        assert repo.get(tx, owner_id="foreign-owner") is None
        assert repo.assert_running(tx, owner_id="foreign-owner", expected_epoch=0) is None
        assert repo.list_acknowledgments(tx, owner_id="foreign-owner", epoch=1) == ()
        with pytest.raises(OwnerStoppedError):
            repo.assert_running(tx, owner_id=state.owner_id)
        with pytest.raises(RepositoryConflictError):
            repo.resume(tx, owner_id="foreign-owner", expected_revision=0, expected_epoch=1, at=AT)
        assert repo.get(tx, owner_id=state.owner_id) == state


def test_stop_exact_replay():
    with isolated() as db:
        repo = astralplane.create_stop_epoch_repository()
        with db.transaction() as tx:
            state = engage(tx, repo)
            assert engage(tx, repo, expected_revision=1) == state
            receipt = acknowledge(tx, repo)
            assert acknowledge(tx, repo, expected_revision=2) == receipt
            assert repo.get(tx, owner_id="stop-owner").revision == 2
            assert repo.list_acknowledgments(tx, owner_id="stop-owner", epoch=1) == (receipt,)
            with pytest.raises(RepositoryConflictError):
                acknowledge(tx, repo, expected_revision=2, receipt_digest="b" * 64)
            with pytest.raises(RepositoryConflictError):
                engage(tx, repo, expected_revision=2, reason="changed replay")


def test_stop_first_engagement_race():
    with isolated() as db:
        repo = StopEpochRepository()
        results = parallel_transactions(
            db, (lambda tx: engage(tx, repo), lambda tx: engage(tx, repo))
        )
        assert sum(isinstance(value, RepositoryConflictError) for value in results) == 1
        with db.transaction() as tx:
            state = repo.get(tx, owner_id="stop-owner")
            assert (state.epoch, state.revision, state.engaged) == (1, 1, True)


def test_stop_caller_failure_rolls_back():
    with isolated() as db:
        with pytest.raises(RuntimeError, match="audit failure"), db.transaction() as tx:
            engage(tx)
            raise RuntimeError("audit failure")
        with db.transaction() as tx:
            assert StopEpochRepository().get(tx, owner_id="stop-owner") is None


def test_stop_resume_restart_and_new_engagement(database):
    repo = StopEpochRepository()
    with database.transaction() as tx:
        state = engage(tx, repo)
        assert state.engaged_at == AT and state.engaged_by == "owner-device"
    with database.transaction() as tx:
        assert StopEpochRepository().get(tx, owner_id="stop-owner") == state
        resumed = repo.resume(
            tx,
            owner_id="stop-owner",
            expected_revision=1,
            expected_epoch=1,
            at=AT + timedelta(seconds=1),
        )
        assert (resumed.epoch, resumed.revision, resumed.engaged) == (1, 2, False)
        assert repo.assert_running(tx, owner_id="stop-owner", expected_epoch=1) == resumed
    with database.transaction() as tx:
        new = engage(tx, repo, expected_revision=2, at=AT + timedelta(seconds=2))
        assert (new.epoch, new.revision) == (2, 3)
        with pytest.raises(RepositoryConflictError):
            repo.resume(
                tx,
                owner_id="stop-owner",
                expected_revision=1,
                expected_epoch=1,
                at=AT + timedelta(seconds=3),
            )
        with pytest.raises(RepositoryConflictError):
            repo.resume(
                tx,
                owner_id="stop-owner",
                expected_revision=3,
                expected_epoch=1,
                at=AT + timedelta(seconds=3),
            )
        with pytest.raises(RepositoryConflictError):
            acknowledge(tx, repo, expected_revision=3, epoch=1, at=AT + timedelta(seconds=3))
        assert repo.get(tx, owner_id="stop-owner") == new


def test_optional_empty_reason_and_strict_update_flag(database):
    with database.transaction() as tx:
        state = engage(tx, reason="")
        assert state.reason == ""
        with pytest.raises(RepositoryValidationError):
            StopEpochRepository().get(tx, owner_id="stop-owner", for_update=1)


def test_absent_admission_holds_exact_owner_lock_to_caller_commit(database):
    from tests.fixtures.database_errors import LOCK_ERRORS

    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
        assert StopEpochRepository().assert_running(tx, owner_id="stop-owner") is None
        with independent_database(schema) as second:
            with pytest.raises(LOCK_ERRORS), second.transaction() as other:
                other.execute("SET LOCAL lock_timeout = '50ms'")
                engage(other)
            if tx.dialect.is_sqlite:
                with pytest.raises(LOCK_ERRORS), second.transaction() as other:
                    other.execute("SET LOCAL lock_timeout = '50ms'")
                    StopEpochRepository().assert_running(other, owner_id="foreign-owner")
            else:
                with second.transaction() as other:
                    assert StopEpochRepository().assert_running(
                        other, owner_id="foreign-owner"
                    ) is None
    with database.transaction() as tx:
        assert engage(tx).engaged
        assert StopEpochRepository().assert_running(tx, owner_id="foreign-owner") is None


@pytest.mark.parametrize("operation", ["engage", "resume", "acknowledge", "assert_running"])
@pytest.mark.parametrize("bad", [True, -1, 2**63, 1.5, "1", None])
def test_stop_integer_fences_are_strict(database, operation, bad):
    repo = StopEpochRepository()
    with database.transaction() as tx:
        engage(tx, repo)
        with pytest.raises(RepositoryValidationError):
            if operation == "engage":
                engage(tx, repo, expected_revision=bad)
            elif operation == "resume":
                repo.resume(
                    tx, owner_id="stop-owner", expected_revision=bad, expected_epoch=1, at=AT
                )
            elif operation == "acknowledge":
                acknowledge(tx, repo, epoch=bad)
            elif bad is None:
                repo.assert_running(tx, owner_id="stop-owner", expected_epoch=True)
            else:
                repo.assert_running(tx, owner_id="stop-owner", expected_epoch=bad)


@pytest.mark.parametrize(
    "bad", [datetime(2026, 10, 10), AT.astimezone(timezone(timedelta(hours=1))), 123, None]
)
def test_stop_times_require_aware_utc(database, bad):
    with database.transaction() as tx:
        with pytest.raises(RepositoryValidationError):
            engage(tx, at=bad)
        assert StopEpochRepository().get(tx, owner_id="stop-owner") is None


@pytest.mark.parametrize(
    "field,bad",
    [
        ("owner_id", " "),
        ("owner_id", "a" * 513),
        ("actor_id", "a" * 513),
        ("reason", None),
        ("reason", "a" * 281),
    ],
)
def test_stop_text_bounds(database, field, bad):
    with database.transaction() as tx, pytest.raises(RepositoryValidationError):
        engage(tx, **{field: bad})


@pytest.mark.parametrize(
    "field,bad",
    [
        ("mesh_id", "a" * 65),
        ("peer_id", "a" * 65),
        ("receipt_digest", "A" * 64),
        ("receipt_digest", "a" * 63),
    ],
)
def test_acknowledgment_identity_bounds(database, field, bad):
    repo = StopEpochRepository()
    with database.transaction() as tx:
        engage(tx, repo)
        with pytest.raises(RepositoryValidationError):
            acknowledge(tx, repo, **{field: bad})
        assert repo.list_acknowledgments(tx, owner_id="stop-owner", epoch=1) == ()


def test_receipt_bound_ordering_stale_revision_and_resume(database):
    repo = StopEpochRepository()
    with database.transaction() as tx:
        engage(tx, repo)
        for index in reversed(range(64)):
            acknowledge(tx, repo, peer_id=f"peer-{index:02}", expected_revision=64 - index)
        receipts = repo.list_acknowledgments(tx, owner_id="stop-owner", epoch=1)
        assert len(receipts) == 64 and receipts[0].peer_id == "peer-00"
        with pytest.raises(RepositoryConflictError):
            acknowledge(tx, repo, peer_id="overflow", expected_revision=65)
        with pytest.raises(RepositoryConflictError):
            acknowledge(tx, repo, expected_revision=1)
        repo.resume(tx, owner_id="stop-owner", expected_revision=65, expected_epoch=1, at=AT)
        with pytest.raises(RepositoryConflictError):
            acknowledge(tx, repo, expected_revision=66)
        assert repo.list_acknowledgments(tx, owner_id="stop-owner", epoch=1) == receipts


def test_regressive_time_and_wrong_epoch_never_change_state(database):
    repo = StopEpochRepository()
    with database.transaction() as tx:
        original = engage(tx, repo)
        with pytest.raises(RepositoryConflictError):
            repo.resume(
                tx,
                owner_id="stop-owner",
                expected_revision=1,
                expected_epoch=1,
                at=AT - timedelta(seconds=1),
            )
        with pytest.raises(RepositoryConflictError):
            acknowledge(tx, repo, at=AT - timedelta(seconds=1))
        assert repo.get(tx, owner_id="stop-owner") == original
        resumed = repo.resume(
            tx, owner_id="stop-owner", expected_revision=1, expected_epoch=1, at=AT
        )
        with pytest.raises(RepositoryConflictError):
            repo.assert_running(tx, owner_id="stop-owner", expected_epoch=0)
        with pytest.raises(RepositoryConflictError):
            repo.resume(tx, owner_id="stop-owner", expected_revision=2, expected_epoch=1, at=AT)
        assert repo.get(tx, owner_id="stop-owner") == resumed


@pytest.mark.parametrize("counter", ["epoch", "revision"])
def test_counter_exhaustion_is_typed_and_keeps_state(database, counter):
    repo = StopEpochRepository()
    with database.transaction() as tx:
        engage(tx, repo)
        repo.resume(tx, owner_id="stop-owner", expected_revision=1, expected_epoch=1, at=AT)
        tx.execute(
            "UPDATE owner_stop_epoch SET epoch = %s, revision = %s WHERE owner_id = %s",
            ((2**63 - 1 if counter == "epoch" else 1), 2**63 - 1, "stop-owner"),
        )
        original = repo.get(tx, owner_id="stop-owner")
        with pytest.raises(RepositoryConflictError):
            engage(tx, repo, expected_revision=2**63 - 1)
        assert repo.get(tx, owner_id="stop-owner") == original


def test_operation_binding_survives_restart_without_reviving_old_work(database):
    repo = StopEpochRepository()
    operation = uuid4()
    with database.transaction() as tx:
        assert repo.bind_operation(tx, owner_id="stop-owner", operation_id=operation) == 0
        assert repo.bind_operation(tx, owner_id="stop-owner", operation_id=operation) == 0
        repo.assert_operation(tx, owner_id="stop-owner", operation_id=operation)
        engage(tx, repo)
        for method in (repo.bind_operation, repo.assert_operation):
            with pytest.raises(OwnerStoppedError):
                method(tx, owner_id="stop-owner", operation_id=operation)
        repo.resume(tx, owner_id="stop-owner", expected_revision=1, expected_epoch=1, at=AT)
    with database.transaction() as tx:
        fresh = StopEpochRepository()
        for method in (fresh.bind_operation, fresh.assert_operation):
            with pytest.raises(StopEpochConflictError):
                method(tx, owner_id="stop-owner", operation_id=operation)
        with pytest.raises(StopEpochConflictError):
            fresh.assert_operation(tx, owner_id="stop-owner", operation_id=uuid4())
        new = uuid4()
        assert fresh.bind_operation(tx, owner_id="stop-owner", operation_id=new) == 1
        fresh.assert_operation(tx, owner_id="stop-owner", operation_id=new)
        assert (
            tx.fetch_one(
                "SELECT epoch FROM owner_stop_operation_epoch "
                "WHERE owner_id = %s AND operation_id = %s",
                ("stop-owner", str(operation)),
            )["epoch"]
            == 0
        )


def test_legacy_operation_allowed_only_before_first_stop_and_owner_isolated(database):
    repo = StopEpochRepository()
    operation = uuid4()
    with database.transaction() as tx:
        repo.assert_operation(tx, owner_id="stop-owner", operation_id=operation)
        assert tx.fetch_all("SELECT * FROM owner_stop_operation_epoch") == ()
        repo.bind_operation(tx, owner_id="stop-owner", operation_id=operation)
        engage(tx, repo)
        repo.resume(tx, owner_id="stop-owner", expected_revision=1, expected_epoch=1, at=AT)
        assert repo.bind_operation(tx, owner_id="foreign-owner", operation_id=operation) == 0
        repo.assert_operation(tx, owner_id="foreign-owner", operation_id=operation)
        with pytest.raises(StopEpochConflictError):
            repo.assert_operation(tx, owner_id="stop-owner", operation_id=operation)


def test_operation_binding_caller_rollback_and_parallel_replay(database):
    repo = StopEpochRepository()
    operation = uuid4()
    with pytest.raises(RuntimeError, match="admission audit"), database.transaction() as tx:
        repo.bind_operation(tx, owner_id="stop-owner", operation_id=operation)
        raise RuntimeError("admission audit")
    with database.transaction() as tx:
        assert tx.fetch_all("SELECT * FROM owner_stop_operation_epoch") == ()
        assert repo.get(tx, owner_id="stop-owner") is None
    results = parallel_transactions(
        database,
        (
            lambda tx: repo.bind_operation(tx, owner_id="stop-owner", operation_id=operation),
            lambda tx: repo.bind_operation(tx, owner_id="stop-owner", operation_id=operation),
        ),
    )
    assert results == (0, 0)


def test_operation_assertion_holds_stop_lock_through_execution(database):
    from tests.fixtures.database_errors import LOCK_ERRORS

    repo = StopEpochRepository()
    operation = UUID("a0101010-0000-4000-8000-000000000001")
    with database.transaction() as tx:
        repo.bind_operation(tx, owner_id="stop-owner", operation_id=operation)
    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
        repo.assert_operation(tx, owner_id="stop-owner", operation_id=operation)
        with (
            independent_database(schema) as second,
            pytest.raises(LOCK_ERRORS),
            second.transaction() as other,
        ):
            other.execute("SET LOCAL lock_timeout = '50ms'")
            engage(other)
    with database.transaction() as tx:
        engage(tx, repo)
        with pytest.raises(OwnerStoppedError):
            repo.assert_operation(tx, owner_id="stop-owner", operation_id=operation)


@pytest.mark.parametrize("method", ["bind_operation", "assert_operation"])
@pytest.mark.parametrize("bad", ["a0101010-0000-4000-8000-000000000001", None, 1, True])
def test_operation_binding_rejects_untyped_ids(database, method, bad):
    with database.transaction() as tx, pytest.raises(RepositoryValidationError):
        getattr(StopEpochRepository(), method)(tx, owner_id="stop-owner", operation_id=bad)


def test_work_created_discriminator_and_peek_preselected_then_queued():
    with isolated() as db, db.transaction() as tx:
        work = WorkAdmissionRepository()
        configs = (
            AdmissionClassConfig(AdmissionClass.GLOBAL, None, 1, 3, 1000, "stop-work-test"),
            AdmissionClassConfig(
                AdmissionClass.INTERACTIVE, AdmissionClass.GLOBAL, 1, 3, 1000, "stop-work-test"
            ),
        )
        work.configure(tx, configs)
        work.bind_configs(configs)
        request = OperationRequest(
            operation_kind="stop_test",
            admission_class=AdmissionClass.INTERACTIVE,
            owner=OperationOwner(OwnerScope.USER, "stop-owner", None),
            submission_id=uuid4(),
            idempotency_namespace="stop-test",
            idempotency_key="first",
            normalized_input_digest="a" * 64,
            chat_id=None,
            parent_operation_id=None,
            connection_generation=None,
            request_generation=None,
        )
        options = dict(now=AT, retention=timedelta(days=1), slot_lease=timedelta(minutes=5))
        first = work.submit(tx, request, **options)
        assert first.created is True
        assert work.submit(tx, request, **options).created is False
        idempotent = work.submit(tx, replace(request, submission_id=uuid4()), **options)
        assert idempotent.created is False and idempotent.operation_id == first.operation_id
        queued = work.submit(
            tx, replace(request, submission_id=uuid4(), idempotency_key="second"), **options
        )
        assert queued.created is True
        before = tx.fetch_all("SELECT * FROM operation_record ORDER BY operation_id")
        candidate = work.peek_next(tx, AdmissionClass.INTERACTIVE)
        assert candidate.operation_id == first.operation_id
        assert candidate.owner_user_id == "stop-owner"
        assert tx.fetch_all("SELECT * FROM operation_record ORDER BY operation_id") == before
        claim = work.claim_operation(tx, AdmissionClass.INTERACTIVE, first.operation_id, **options)
        assert claim is not None
        assert work.peek_next(tx, AdmissionClass.INTERACTIVE).operation_id == queued.operation_id
