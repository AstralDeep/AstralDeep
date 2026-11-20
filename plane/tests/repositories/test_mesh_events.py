"""Unit-proofs the mesh event, roster, derived chat, run and ledger stores' fail-closed
guards with scripted driver doubles: rows from another owner inside batched reads, writes that
return nothing or several records, and persisted rows with malformed shapes. PostgreSQL tests
cover the statement-level chain, fork and isolation behavior.
"""

import pytest

from astralplane import (
    create_derived_chat_repository,
    create_event_log_repository,
    create_event_roster_repository,
    create_learning_ledger_repository,
    create_run_repository,
)
from astralplane.repositories import RepositoryDataError
from tests.repositories._mesh_support import event_kwargs, ref
from tests.repositories._support import Result, ScriptedTransaction

MESH = ref(b"mesh")
OWNER = "mesh-owner"


def feed_row(**changes):
    row = {
        "owner_id": OWNER,
        "mesh_id": MESH,
        "feed_id": "feed",
        "state": "accepted",
        "head_seq": 0,
        "head_hash": None,
        "recorded_at": 1,
        "frozen_at": None,
    }
    row.update(changes)
    return row


def entry_row(**changes):
    kwargs = event_kwargs(MESH, 1, None)
    row = {key: kwargs[key] for key in kwargs if key not in {"seq"}} | {"seq": 1}
    row["labels"] = {"trust": "owner"}
    row.update(changes)
    return row


def test_batched_event_reads_fail_closed_on_a_foreign_owner_row():
    repo = create_event_log_repository()
    foreign = entry_row(owner_id="someone-else")
    for call in (
        lambda tx: repo.list_events_after(tx, owner_id=OWNER, mesh_id=MESH, feed_id="feed"),
        lambda tx: repo.list_events_by_scope(tx, owner_id=OWNER, mesh_id=MESH, scope="chat-1"),
        lambda tx: repo.list_events_missing_bodies(tx, owner_id=OWNER, mesh_id=MESH),
    ):
        with pytest.raises(RepositoryDataError, match="owner boundary"):
            call(ScriptedTransaction(all_rows=[(foreign,)]))
    with pytest.raises(RepositoryDataError, match="owner boundary"):
        repo.list_feed_heads(
            ScriptedTransaction(all_rows=[(feed_row(owner_id="someone-else"),)]),
            owner_id=OWNER,
            mesh_id=MESH,
        )
    with pytest.raises(RepositoryDataError, match="owner boundary"):
        repo.list_meshes(
            ScriptedTransaction(all_rows=[({"owner_id": "x", "mesh_id": MESH, "recorded_at": 1},)]),
            owner_id=OWNER,
        )
    with pytest.raises(RepositoryDataError, match="owner boundary"):
        repo.list_fork_evidence(
            ScriptedTransaction(all_rows=[(entry_row(owner_id="x"),)]),
            owner_id=OWNER,
            mesh_id=MESH,
            feed_id="feed",
        )


def test_append_refuses_a_write_that_returns_nothing():
    kwargs = event_kwargs(MESH, 1, None)
    transaction = ScriptedTransaction(
        one=[feed_row(), None, None],
        execute=[Result(), Result(), Result(), Result()],
    )
    with pytest.raises(RepositoryDataError, match="did not return"):
        create_event_log_repository().append_event(transaction, **kwargs)


def test_append_refuses_a_write_that_returns_several_records():
    kwargs = event_kwargs(MESH, 1, None)
    transaction = ScriptedTransaction(
        one=[feed_row(), None, None],
        execute=[Result(), Result(returned_records=(entry_row(), entry_row()))],
    )
    with pytest.raises(RepositoryDataError, match="multiple detached"):
        create_event_log_repository().append_event(transaction, **kwargs)


def test_persisted_rows_with_malformed_shapes_are_data_errors():
    roster = create_event_roster_repository()
    bad_follows = {
        "owner_id": OWNER,
        "mesh_id": MESH,
        "entry_id": MESH,
        "kind": "add",
        "follows": [1],
        "signed_bytes": b"x",
        "recorded_at": 1,
    }
    with pytest.raises(RepositoryDataError, match="follows"):
        roster.get_entry(
            ScriptedTransaction(one=[bad_follows]), owner_id=OWNER, mesh_id=MESH, entry_id=MESH
        )
    with pytest.raises(RepositoryDataError, match="bytes"):
        roster.get_entry(
            ScriptedTransaction(one=[dict(bad_follows, follows=[], signed_bytes="text")]),
            owner_id=OWNER,
            mesh_id=MESH,
            entry_id=MESH,
        )
    with pytest.raises(RepositoryDataError, match="integer"):
        roster.get_entry(
            ScriptedTransaction(one=[dict(bad_follows, follows=[], recorded_at="1")]),
            owner_id=OWNER,
            mesh_id=MESH,
            entry_id=MESH,
        )
    ledger = create_learning_ledger_repository()
    with pytest.raises(RepositoryDataError, match="evidence"):
        ledger.get_entry(
            ScriptedTransaction(
                one=[
                    {
                        "owner_id": OWNER,
                        "mesh_id": MESH,
                        "event_id": ref(b"e"),
                        "kind": "proposal",
                        "proposal_id": ref(b"e"),
                        "item_kind": "skill",
                        "content_hash": ref(b"c"),
                        "scope": "s",
                        "review": None,
                        "evidence": {"not": "a list"},
                        "recorded_at": 1,
                    }
                ]
            ),
            owner_id=OWNER,
            mesh_id=MESH,
            event_id=ref(b"e"),
        )


def test_roster_reads_fail_closed_on_a_foreign_owner_row():
    roster = create_event_roster_repository()
    foreign = {
        "owner_id": "x",
        "mesh_id": MESH,
        "entry_id": MESH,
        "kind": "add",
        "follows": [],
        "signed_bytes": b"x",
        "recorded_at": 1,
    }
    for call in (
        lambda tx: roster.list_entries(tx, owner_id=OWNER, mesh_id=MESH),
        lambda tx: roster.list_tips(tx, owner_id=OWNER, mesh_id=MESH),
        lambda tx: roster.list_checkpoints(tx, owner_id=OWNER, mesh_id=MESH),
    ):
        with pytest.raises(RepositoryDataError, match="owner boundary"):
            call(ScriptedTransaction(all_rows=[(foreign,)]))


def test_derived_chat_and_run_writes_refuse_missing_records():
    derived = create_derived_chat_repository()
    event = ref(b"chat")
    with pytest.raises(Exception, match="derived chat was not stored"):
        derived.upsert_chat(
            ScriptedTransaction(one=[None, None, None], execute=[Result()]),
            owner_id=OWNER,
            event_id=event,
            chat_id=event,
            title="t",
            agent_id="agent",
            created_at=1,
        )
    with pytest.raises(RepositoryDataError, match="multiple detached"):
        derived.upsert_chat(
            ScriptedTransaction(
                one=[None, None, None],
                execute=[Result(returned_records=({}, {}))],
            ),
            owner_id=OWNER,
            event_id=event,
            chat_id=event,
            title="t",
            agent_id=None,
            created_at=1,
        )
    runs = create_run_repository()
    with pytest.raises(Exception, match="mesh was not found"):
        runs.record_lease(
            ScriptedTransaction(one=[None, None, None], execute=[Result()]),
            owner_id=OWNER,
            mesh_id=MESH,
            run_id="run",
            kind="take",
            executor_feed_id="phone",
            state="running",
            event_id=ref(b"lease"),
            recorded_at=1,
        )
