"""Exercises run lease and goal histories against real PostgreSQL: contiguous epochs and
revisions, one executor per run, handoff and takeover rules, finished runs and goals,
idempotent replay by event id, and owner isolation on every read and write.
"""

import pytest

from astralplane import create_run_repository
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from tests.repositories._mesh_support import OTHER, OWNER, genesis, ref

repo = create_run_repository()
RUN = "run-1"


def lease(tx, mesh, tag, kind="take", executor="phone", state="running", run=RUN, owner=OWNER):
    return repo.record_lease(
        tx,
        owner_id=owner,
        mesh_id=mesh,
        run_id=run,
        kind=kind,
        executor_feed_id=executor,
        state=state,
        event_id=ref(tag.encode()),
        recorded_at=10,
    )


def goal(tx, mesh, tag, state="active", rounds=0, stalls=0, text="ship it", run=RUN, owner=OWNER):
    return repo.record_goal_transition(
        tx,
        owner_id=owner,
        mesh_id=mesh,
        run_id=run,
        goal=text,
        state=state,
        rounds=rounds,
        stalls=stalls,
        event_id=ref(tag.encode()),
        recorded_at=10,
    )


def reason(raised):
    return dict(raised.value.metadata)["reason"]


def test_lease_history_records_take_transfer_takeover_and_state(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        first = lease(tx, mesh, "e1")
        assert (first.epoch, first.kind, first.executor_feed_id) == (1, "take", "phone")
        paused = lease(tx, mesh, "e2", kind="state", state="paused")
        moved = lease(tx, mesh, "e3", kind="transfer", executor="mac")
        taken = lease(tx, mesh, "e4", kind="takeover", executor="phone")
        assert [e.epoch for e in (paused, moved, taken)] == [2, 3, 4]
        assert repo.get_lease(tx, owner_id=OWNER, mesh_id=mesh, run_id=RUN) == taken
        history = repo.list_lease_history(tx, owner_id=OWNER, mesh_id=mesh, run_id=RUN)
        assert [(e.kind, e.executor_feed_id) for e in history] == [
            ("take", "phone"),
            ("state", "phone"),
            ("transfer", "mac"),
            ("takeover", "phone"),
        ]
        lease(tx, mesh, "x1", executor="mac", run="run-2")
        assert [
            r.run_id
            for r in repo.list_executor_runs(
                tx, owner_id=OWNER, mesh_id=mesh, executor_feed_id="phone"
            )
        ] == [RUN]
        assert [
            r.run_id
            for r in repo.list_executor_runs(
                tx, owner_id=OWNER, mesh_id=mesh, executor_feed_id="mac"
            )
        ] == ["run-2"]
        lease(tx, mesh, "x2", kind="state", executor="mac", state="completed", run="run-2")
        assert (
            repo.list_executor_runs(tx, owner_id=OWNER, mesh_id=mesh, executor_feed_id="mac") == ()
        )


def test_lease_replay_is_idempotent_and_changed_replays_conflict(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        first = lease(tx, mesh, "e1")
        assert lease(tx, mesh, "e1") == first
        assert len(repo.list_lease_history(tx, owner_id=OWNER, mesh_id=mesh, run_id=RUN)) == 1
        with pytest.raises(RepositoryConflictError, match="different semantics"):
            lease(tx, mesh, "e1", executor="mac")


def test_lease_rules_refuse_second_executors_foreign_state_changes_and_finished_runs(
    migrated_clone,
):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        with pytest.raises(RepositoryConflictError) as untaken:
            lease(tx, mesh, "a", kind="transfer", executor="mac")
        assert reason(untaken) == "lease_not_taken"
        lease(tx, mesh, "e1")
        for tag, kwargs, expected in (
            ("b", {"kind": "take", "executor": "mac"}, "lease_held"),
            ("c", {"kind": "state", "executor": "mac", "state": "paused"}, "not_the_executor"),
            ("d", {"kind": "transfer", "executor": "phone"}, "same_executor"),
            ("e", {"kind": "takeover", "executor": "phone"}, "same_executor"),
        ):
            with pytest.raises(RepositoryConflictError) as refused:
                lease(tx, mesh, tag, **kwargs)
            assert reason(refused) == expected
        lease(tx, mesh, "f", kind="state", state="failed")
        with pytest.raises(RepositoryConflictError) as finished:
            lease(tx, mesh, "g", kind="transfer", executor="mac")
        assert reason(finished) == "run_finished"
        assert repo.get_lease(tx, owner_id=OWNER, mesh_id=mesh, run_id=RUN).epoch == 2
        with pytest.raises(RepositoryValidationError):
            lease(tx, mesh, "h", kind="steal")
        with pytest.raises(RepositoryValidationError):
            lease(tx, mesh, "h", state="sleeping")
        with pytest.raises(RepositoryValidationError):
            lease(tx, mesh, "h", run="")


def test_goal_history_is_contiguous_monotonic_and_stops_when_finished(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        with pytest.raises(RepositoryConflictError) as unstarted:
            goal(tx, mesh, "a", state="stalled")
        assert reason(unstarted) == "goal_not_started"
        first = goal(tx, mesh, "g1")
        assert goal(tx, mesh, "g1") == first
        second = goal(tx, mesh, "g2", rounds=2, stalls=1, state="stalled")
        assert (second.revision, second.rounds, second.stalls) == (2, 2, 1)
        with pytest.raises(RepositoryConflictError) as backwards:
            goal(tx, mesh, "g3", rounds=1)
        assert reason(backwards) == "rounds_decreased"
        with pytest.raises(RepositoryConflictError) as edited:
            goal(tx, mesh, "g4", rounds=3, text="something else")
        assert reason(edited) == "goal_changed"
        with pytest.raises(RepositoryConflictError, match="different semantics"):
            goal(tx, mesh, "g2", rounds=9)
        done = goal(tx, mesh, "g5", state="achieved", rounds=3)
        assert repo.get_goal(tx, owner_id=OWNER, mesh_id=mesh, run_id=RUN) == done
        with pytest.raises(RepositoryConflictError) as finished:
            goal(tx, mesh, "g6", rounds=4)
        assert reason(finished) == "goal_finished"
        history = repo.list_goal_history(tx, owner_id=OWNER, mesh_id=mesh, run_id=RUN)
        assert [h.revision for h in history] == [1, 2, 3]
        with pytest.raises(RepositoryValidationError):
            goal(tx, mesh, "g7", run="run-2", rounds=-1)
        with pytest.raises(RepositoryValidationError):
            goal(tx, mesh, "g7", run="run-2", state="winning")
        with pytest.raises(RepositoryValidationError):
            goal(tx, mesh, "g7", run="run-2", text="")


def test_owners_cannot_read_or_write_each_others_runs(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        genesis(tx, owner=OTHER)
        lease(tx, mesh, "e1")
        goal(tx, mesh, "g1")
        assert repo.get_lease(tx, owner_id=OTHER, mesh_id=mesh, run_id=RUN) is None
        assert repo.list_lease_history(tx, owner_id=OTHER, mesh_id=mesh, run_id=RUN) == ()
        assert (
            repo.list_executor_runs(tx, owner_id=OTHER, mesh_id=mesh, executor_feed_id="phone")
            == ()
        )
        assert repo.get_goal(tx, owner_id=OTHER, mesh_id=mesh, run_id=RUN) is None
        assert repo.list_goal_history(tx, owner_id=OTHER, mesh_id=mesh, run_id=RUN) == ()
        with pytest.raises(RepositoryNotFoundError):
            lease(tx, mesh, "z1", owner=OTHER)
        with pytest.raises(RepositoryNotFoundError):
            goal(tx, mesh, "z2", owner=OTHER)
        assert repo.get_lease(tx, owner_id=OWNER, mesh_id=mesh, run_id=RUN).epoch == 1


def test_public_run_contract_behaviors():
    from tests.fixtures.migrated_template import bound_clone, current_lineages

    for check in (
        test_lease_history_records_take_transfer_takeover_and_state,
        test_lease_replay_is_idempotent_and_changed_replays_conflict,
        test_goal_history_is_contiguous_monotonic_and_stops_when_finished,
        test_owners_cannot_read_or_write_each_others_runs,
    ):
        with bound_clone(current_lineages().current()) as clone:
            check(clone)
