"""Tests for astralplane.repositories.personalization_graph: bidirectional links stay
owner-bound on both endpoints, and signal/sweep records are idempotent, owner-scoped,
and bounded.
"""

from __future__ import annotations

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.personalization_graph import (
    ConsolidationSweepRecord,
    PersonalizationGraphRepository,
    ShortTermSignalRecord,
)
from tests.repositories._support import Result, ScriptedTransaction


def _link(memory: str, linked: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "user_id": "owner-1",
        "memory_id": memory,
        "linked_id": linked,
        "created_at": 100,
    }
    row.update(overrides)
    return row


def _signal_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "signal-1",
        "user_id": "owner-1",
        "category": "preference",
        "value": "prefers concise output",
        "recall_count": 2,
        "last_seen_at": 110,
        "created_at": 100,
    }
    row.update(overrides)
    return row


def _signal(**overrides: object) -> ShortTermSignalRecord:
    values: dict[str, object] = {
        "signal_id": "signal-1",
        "owner_id": "owner-1",
        "category": "preference",
        "value": "prefers concise output",
        "recall_count": 2,
        "last_seen_at": 110,
        "created_at": 100,
    }
    values.update(overrides)
    return ShortTermSignalRecord(**values)  # type: ignore[arg-type]


def _sweep_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "sweep-1",
        "user_id": "owner-1",
        "ran_at": 120,
        "candidates_considered": 4,
        "promoted_count": 2,
        "summary": "promoted stable preferences",
        "trigger": "scheduled",
    }
    row.update(overrides)
    return row


def _sweep(**overrides: object) -> ConsolidationSweepRecord:
    values: dict[str, object] = {
        "sweep_id": "sweep-1",
        "owner_id": "owner-1",
        "ran_at": 120,
        "candidates_considered": 4,
        "promoted_count": 2,
        "summary": "promoted stable preferences",
        "trigger": "scheduled",
    }
    values.update(overrides)
    return ConsolidationSweepRecord(**values)  # type: ignore[arg-type]


def test_links_are_bidirectional_and_both_endpoints_are_owner_bound() -> None:
    transaction = ScriptedTransaction(
        all_rows=[(_link("memory-1", "memory-2"), _link("memory-2", "memory-1"))]
    )
    pair = PersonalizationGraphRepository().add_link(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        memory_id="memory-1",
        linked_id="memory-2",
        created_at=100,
    )
    assert len(pair) == 2
    assert "source.user_id = %s" in transaction.calls[0][1]
    assert "target.user_id = %s" in transaction.calls[0][1]
    assert "UNION ALL" in transaction.calls[0][1]


def test_missing_endpoint_or_partial_pair_fails_closed() -> None:
    transaction = ScriptedTransaction(all_rows=[(_link("memory-1", "memory-2"),)])
    with pytest.raises(RepositoryNotFoundError, match="endpoints"):
        PersonalizationGraphRepository().add_link(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            memory_id="memory-1",
            linked_id="memory-2",
            created_at=100,
        )


def test_remove_link_requires_zero_or_two_rows() -> None:
    repository = PersonalizationGraphRepository()
    assert repository.remove_link(
        ScriptedTransaction(execute=[Result(rowcount=2)]),  # type: ignore[arg-type]
        owner_id="owner-1",
        memory_id="memory-1",
        linked_id="memory-2",
    )
    assert not repository.remove_link(
        ScriptedTransaction(execute=[Result(rowcount=0)]),  # type: ignore[arg-type]
        owner_id="owner-1",
        memory_id="memory-1",
        linked_id="memory-2",
    )
    with pytest.raises(RepositoryDataError, match="partial"):
        repository.remove_link(
            ScriptedTransaction(execute=[Result(rowcount=1)]),  # type: ignore[arg-type]
            owner_id="owner-1",
            memory_id="memory-1",
            linked_id="memory-2",
        )


def test_link_queries_are_owner_scoped_and_bounded() -> None:
    transaction = ScriptedTransaction(
        all_rows=[({"linked_id": "memory-2"},), (_link("memory-1", "memory-2"),)]
    )
    repository = PersonalizationGraphRepository()
    assert repository.linked_ids(
        transaction, owner_id="owner-1", memory_id="memory-1", limit=7  # type: ignore[arg-type]
    ) == ("memory-2",)
    assert len(repository.list_links(transaction, owner_id="owner-1", limit=8)) == 1  # type: ignore[arg-type]
    assert transaction.calls[0][2] == ("owner-1", "memory-1", 7)
    assert transaction.calls[1][2] == ("owner-1", 8)


def test_signal_create_and_replay_are_idempotent() -> None:
    inserted = ScriptedTransaction(execute=[Result(returned_records=(_signal_row(),))])
    assert PersonalizationGraphRepository().create_signal(
        inserted, _signal()  # type: ignore[arg-type]
    ) == _signal()
    replay = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_signal_row()])
    assert PersonalizationGraphRepository().create_signal(
        replay, _signal()  # type: ignore[arg-type]
    ) == _signal()


def test_signal_identity_conflicts_are_explicit() -> None:
    repository = PersonalizationGraphRepository()
    foreign = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    with pytest.raises(RepositoryConflictError, match="another namespace"):
        repository.create_signal(foreign, _signal())  # type: ignore[arg-type]
    changed = ScriptedTransaction(
        execute=[Result(rowcount=0)], one=[_signal_row(value="different")]
    )
    with pytest.raises(RepositoryConflictError, match="semantics"):
        repository.create_signal(changed, _signal())  # type: ignore[arg-type]


def test_signal_get_list_delete_are_owner_scoped() -> None:
    repository = PersonalizationGraphRepository()
    transaction = ScriptedTransaction(one=[_signal_row()], all_rows=[(_signal_row(),)])
    assert repository.get_signal(
        transaction, owner_id="owner-1", signal_id="signal-1"  # type: ignore[arg-type]
    )
    assert len(repository.list_signals(transaction, owner_id="owner-1", limit=9)) == 1  # type: ignore[arg-type]
    assert repository.delete_signal(
        ScriptedTransaction(execute=[Result(rowcount=1)]),  # type: ignore[arg-type]
        owner_id="owner-1",
        signal_id="signal-1",
    )
    assert not repository.delete_signal(
        ScriptedTransaction(execute=[Result(rowcount=0)]),  # type: ignore[arg-type]
        owner_id="owner-1",
        signal_id="signal-1",
    )
    with pytest.raises(RepositoryDataError):
        repository.delete_signal(
            ScriptedTransaction(execute=[Result(rowcount=2)]),  # type: ignore[arg-type]
            owner_id="owner-1",
            signal_id="signal-1",
        )


def test_signal_owner_mismatch_and_persisted_enum_fail_closed() -> None:
    repository = PersonalizationGraphRepository()
    with pytest.raises(RepositoryDataError, match="another owner's"):
        repository.get_signal(
            ScriptedTransaction(one=[_signal_row(user_id="owner-2")]),  # type: ignore[arg-type]
            owner_id="owner-1",
            signal_id="signal-1",
        )
    with pytest.raises(RepositoryDataError, match="category"):
        repository.get_signal(
            ScriptedTransaction(one=[_signal_row(category="unknown")]),  # type: ignore[arg-type]
            owner_id="owner-1",
            signal_id="signal-1",
        )


def test_sweep_create_replay_get_and_list() -> None:
    repository = PersonalizationGraphRepository()
    inserted = ScriptedTransaction(execute=[Result(returned_records=(_sweep_row(),))])
    assert repository.record_sweep(inserted, _sweep()) == _sweep()  # type: ignore[arg-type]
    replay = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_sweep_row()])
    assert repository.record_sweep(replay, _sweep()) == _sweep()  # type: ignore[arg-type]
    query = ScriptedTransaction(one=[_sweep_row()], all_rows=[(_sweep_row(),)])
    assert repository.get_sweep(
        query, owner_id="owner-1", sweep_id="sweep-1"  # type: ignore[arg-type]
    )
    assert len(repository.list_sweeps(query, owner_id="owner-1", limit=3)) == 1  # type: ignore[arg-type]


def test_sweep_identity_conflict_and_owner_mismatch_fail_closed() -> None:
    repository = PersonalizationGraphRepository()
    with pytest.raises(RepositoryConflictError):
        repository.record_sweep(
            ScriptedTransaction(execute=[Result(rowcount=0)], one=[None]),  # type: ignore[arg-type]
            _sweep(),
        )
    with pytest.raises(RepositoryConflictError):
        repository.record_sweep(
            ScriptedTransaction(
                execute=[Result(rowcount=0)], one=[_sweep_row(summary="other")]
            ),  # type: ignore[arg-type]
            _sweep(),
        )
    with pytest.raises(RepositoryDataError, match="another owner's"):
        repository.get_sweep(
            ScriptedTransaction(one=[_sweep_row(user_id="owner-2")]),  # type: ignore[arg-type]
            owner_id="owner-1",
            sweep_id="sweep-1",
        )


@pytest.mark.parametrize(
    "action",
    [
        lambda repo: repo.add_link(
            ScriptedTransaction(),
            owner_id="owner-1",
            memory_id="same",
            linked_id="same",
            created_at=1,
        ),
        lambda repo: repo.create_signal(ScriptedTransaction(), _signal(category="unknown")),
        lambda repo: repo.create_signal(ScriptedTransaction(), _signal(recall_count=-1)),
        lambda repo: repo.record_sweep(
            ScriptedTransaction(), _sweep(promoted_count=5)
        ),
        lambda repo: repo.record_sweep(ScriptedTransaction(), _sweep(trigger="unknown")),
        lambda repo: repo.list_signals(
            ScriptedTransaction(), owner_id="owner-1", limit=1001
        ),
    ],
)
def test_validation_is_bounded(action: object) -> None:
    with pytest.raises(RepositoryValidationError):
        action(PersonalizationGraphRepository())  # type: ignore[operator]
