"""Exercises the mesh event log against real PostgreSQL: chain checks on append, idempotent
replay, fork evidence that freezes a feed, reads by feed and by scope in clock order, bodies
addressed by hash, and owner isolation on every read and write.
"""

import pytest

from astralplane import create_event_log_repository
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.mesh_events import APPENDED, DUPLICATE, FORKED, EventCursor
from tests.repositories._mesh_support import (
    FEED,
    FEED_TWO,
    OTHER,
    OWNER,
    append_chain,
    event_kwargs,
    genesis,
    ref,
)

repo = create_event_log_repository()


def test_chain_appends_advance_the_feed_head(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        results = append_chain(tx, mesh, 3)
        assert [result.outcome for result in results] == [APPENDED] * 3
        head = repo.get_feed(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED)
        assert (head.head_seq, head.head_hash, head.state) == (
            3,
            results[2].event.event_id,
            "accepted",
        )
        assert results[0].event.prev_hash is None
        assert results[1].event.prev_hash == results[0].event.event_id
        assert results[1].event.labels["trust"] == "owner"
        assert (
            repo.get_event(tx, owner_id=OWNER, mesh_id=mesh, event_id=results[1].event.event_id)
            == results[1].event
        )


def test_replay_of_the_same_event_is_idempotent_even_when_resealed(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        first = append_chain(tx, mesh, 2)
        again = event_kwargs(mesh, 2, first[0].event.event_id)
        again["header_sealed"] = b"a different nonce"
        replay = repo.append_event(tx, **again)
        assert replay.outcome == DUPLICATE
        assert replay.event == first[1].event
        assert replay.feed.head_seq == 2
        assert repo.get_feed(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED).head_seq == 2


def test_same_event_id_with_different_derived_fields_is_a_conflict(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        first = append_chain(tx, mesh, 1)
        changed = event_kwargs(mesh, 1, None)
        changed["scope"] = "another-scope"
        with pytest.raises(RepositoryConflictError, match="different semantics"):
            repo.append_event(tx, **changed)
        assert first[0].event.scope == "chat-1"


def test_gaps_wrong_previous_hashes_and_bad_event_ids_are_refused(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        results = append_chain(tx, mesh, 2)
        head = results[1].event.event_id
        with pytest.raises(RepositoryConflictError) as gap:
            repo.append_event(tx, **event_kwargs(mesh, 4, head))
        assert dict(gap.value.metadata)["reason"] == "sequence_gap"
        with pytest.raises(RepositoryConflictError) as mismatch:
            repo.append_event(tx, **event_kwargs(mesh, 3, ref(b"not the head")))
        assert dict(mismatch.value.metadata)["reason"] == "previous_hash_mismatch"
        with pytest.raises(RepositoryConflictError) as shape:
            repo.append_event(tx, **event_kwargs(mesh, 3, None))
        assert dict(shape.value.metadata)["reason"] == "previous_hash_shape"
        forged = event_kwargs(mesh, 3, head)
        forged["event_id"] = ref(b"somebody else")
        with pytest.raises(RepositoryConflictError) as identity:
            repo.append_event(tx, **forged)
        assert dict(identity.value.metadata)["reason"] == "event_id_mismatch"
        assert repo.get_feed(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED).head_seq == 2


def test_an_event_id_cannot_be_recorded_at_two_positions(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        first = append_chain(tx, mesh, 1)
        other = event_kwargs(mesh, 1, None, feed=FEED_TWO)
        other["header_bytes"] = event_kwargs(mesh, 1, None)["header_bytes"]
        other["event_id"] = first[0].event.event_id
        with pytest.raises(RepositoryConflictError) as reused:
            repo.append_event(tx, **other)
        assert dict(reused.value.metadata)["reason"] == "event_id_reused"


def test_two_different_events_at_one_position_keep_both_and_freeze_the_feed(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        results = append_chain(tx, mesh, 2)
        rival = event_kwargs(mesh, 2, results[0].event.event_id, tag="rival")
        outcome = repo.append_event(tx, **rival)
        assert outcome.outcome == FORKED and outcome.event is None
        assert outcome.feed.state == "frozen" and outcome.feed.frozen_at is not None
        evidence = repo.list_fork_evidence(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED)
        assert {item.event_id for item in evidence} == {
            results[1].event.event_id,
            rival["event_id"],
        }
        assert {item.seq for item in evidence} == {2}
        third = repo.append_event(tx, **event_kwargs(mesh, 2, results[0].event.event_id, tag="x"))
        assert third.outcome == FORKED
        assert len(repo.list_fork_evidence(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED)) == 3
        with pytest.raises(RepositoryConflictError) as frozen:
            repo.append_event(tx, **event_kwargs(mesh, 3, results[1].event.event_id))
        assert dict(frozen.value.metadata)["reason"] == "feed_frozen"
        replay = repo.append_event(tx, **event_kwargs(mesh, 1, None))
        assert replay.outcome == DUPLICATE
        assert repo.get_feed(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED).state == "frozen"


def test_a_fork_does_not_freeze_other_feeds(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        results = append_chain(tx, mesh, 1)
        append_chain(tx, mesh, 2, feed=FEED_TWO)
        repo.append_event(tx, **event_kwargs(mesh, 1, None, tag="rival"))
        heads = {
            head.feed_id: head for head in repo.list_feed_heads(tx, owner_id=OWNER, mesh_id=mesh)
        }
        assert heads[FEED].state == "frozen"
        assert heads[FEED_TWO].state == "accepted" and heads[FEED_TWO].head_seq == 2
        assert results[0].event.seq == 1


def test_reads_by_feed_range_and_scope_in_clock_order(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        append_chain(tx, mesh, 3, clock=(3000, 0))
        later = append_chain(tx, mesh, 2, feed=FEED_TWO, clock=(1000, 5))
        append_chain(tx, mesh, 1, feed="feed-third", clock=(1000, 5), scope="chat-2")
        after = repo.list_events_after(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED, after_seq=1)
        assert [event.seq for event in after] == [2, 3]
        assert (
            repo.list_events_after(
                tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED, after_seq=1, limit=1
            )[0].seq
            == 2
        )
        ordered = repo.list_events_by_scope(tx, owner_id=OWNER, mesh_id=mesh, scope="chat-1")
        assert [(event.feed_id, event.seq) for event in ordered] == [
            (FEED_TWO, 1),
            (FEED_TWO, 2),
            (FEED, 1),
            (FEED, 2),
            (FEED, 3),
        ]
        cursor = EventCursor(1000, 5, FEED_TWO, 1)
        resumed = repo.list_events_by_scope(
            tx, owner_id=OWNER, mesh_id=mesh, scope="chat-1", after=cursor, limit=2
        )
        assert [(event.feed_id, event.seq) for event in resumed] == [(FEED_TWO, 2), (FEED, 1)]
        assert later[0].event.clock_n == 5
        other_scope = repo.list_events_by_scope(tx, owner_id=OWNER, mesh_id=mesh, scope="chat-2")
        assert [event.feed_id for event in other_scope] == ["feed-third"]


def test_bodies_are_addressed_by_hash_and_may_be_absent(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        results = append_chain(tx, mesh, 2)
        missing = repo.list_events_missing_bodies(tx, owner_id=OWNER, mesh_id=mesh)
        assert [event.seq for event in missing] == [1, 2]
        sealed = b"body|feed-phone|1|a"
        stored = repo.put_body(
            tx,
            owner_id=OWNER,
            mesh_id=mesh,
            body_hash=results[0].event.body_hash,
            sealed=sealed,
            recorded_at=9,
        )
        assert stored.sealed == sealed
        assert (
            repo.put_body(
                tx,
                owner_id=OWNER,
                mesh_id=mesh,
                body_hash=ref(sealed),
                sealed=sealed,
                recorded_at=99,
            )
            == stored
        )
        assert repo.get_body(tx, owner_id=OWNER, mesh_id=mesh, body_hash=ref(sealed)) == stored
        assert [
            e.seq for e in repo.list_events_missing_bodies(tx, owner_id=OWNER, mesh_id=mesh)
        ] == [2]
        assert (
            repo.list_events_missing_bodies(tx, owner_id=OWNER, mesh_id=mesh, scope="no-such-scope")
            == ()
        )
        with pytest.raises(RepositoryConflictError) as wrong:
            repo.put_body(
                tx,
                owner_id=OWNER,
                mesh_id=mesh,
                body_hash=ref(b"other"),
                sealed=b"not other",
                recorded_at=1,
            )
        assert dict(wrong.value.metadata)["reason"] == "body_hash_mismatch"


def test_owners_cannot_see_or_write_each_others_meshes(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        other_mesh = genesis(tx, owner=OTHER)
        results = append_chain(tx, mesh, 2)
        append_chain(tx, other_mesh, 1, owner=OTHER)
        event_id = results[0].event.event_id
        assert repo.get_mesh(tx, owner_id=OTHER, mesh_id=mesh) is None
        assert repo.get_event(tx, owner_id=OTHER, mesh_id=mesh, event_id=event_id) is None
        assert repo.get_feed(tx, owner_id=OTHER, mesh_id=mesh, feed_id=FEED) is None
        assert repo.list_feed_heads(tx, owner_id=OTHER, mesh_id=mesh) == ()
        assert repo.list_events_after(tx, owner_id=OTHER, mesh_id=mesh, feed_id=FEED) == ()
        assert repo.list_events_by_scope(tx, owner_id=OTHER, mesh_id=mesh, scope="chat-1") == ()
        assert repo.list_fork_evidence(tx, owner_id=OTHER, mesh_id=mesh, feed_id=FEED) == ()
        assert (
            repo.get_body(tx, owner_id=OTHER, mesh_id=mesh, body_hash=results[0].event.body_hash)
            is None
        )
        assert [m.mesh_id for m in repo.list_meshes(tx, owner_id=OTHER)] == [other_mesh]
        with pytest.raises(RepositoryNotFoundError):
            repo.append_event(tx, **event_kwargs(mesh, 1, None, owner=OTHER, tag="x"))
        with pytest.raises(RepositoryNotFoundError):
            repo.put_body(
                tx, owner_id=OTHER, mesh_id=mesh, body_hash=ref(b"x"), sealed=b"x", recorded_at=1
            )
        assert repo.get_feed(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED).head_seq == 2


def test_inputs_are_validated(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        base = event_kwargs(mesh, 1, None)
        for field, value in (
            ("seq", 0),
            ("seq", True),
            ("clock_ms", -1),
            ("clock_n", 2**53),
            ("scope", ""),
            ("event_type", "x" * 129),
            ("key_id", ""),
            ("body_hash", "sha256:xyz"),
            ("header_bytes", b""),
            ("header_sealed", "text"),
            ("recorded_at", 1.5),
            ("labels", {"bad": float("nan")}),
        ):
            changed = dict(base, **{field: value})
            with pytest.raises((RepositoryValidationError, RepositoryConflictError)):
                repo.append_event(tx, **changed)
        with pytest.raises(RepositoryValidationError):
            repo.get_mesh(tx, owner_id=OWNER, mesh_id="not-a-hash")
        with pytest.raises(RepositoryValidationError):
            repo.list_events_after(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED, limit=5000)
        with pytest.raises(RepositoryValidationError):
            repo.list_events_by_scope(
                tx, owner_id=OWNER, mesh_id=mesh, scope="s", after=EventCursor(1, 0, FEED, 0)
            )


def test_caller_rollback_removes_appended_events(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
    with (
        pytest.raises(RuntimeError, match="rollback"),
        migrated_clone.database.transaction() as tx,
    ):
        append_chain(tx, mesh, 2)
        raise RuntimeError("rollback")
    with migrated_clone.database.transaction() as tx:
        assert repo.get_feed(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED) is None
        assert append_chain(tx, mesh, 1)[0].outcome == APPENDED


def test_public_event_log_contract_behaviors():
    from tests.fixtures.migrated_template import bound_clone, current_lineages

    for check in (
        test_chain_appends_advance_the_feed_head,
        test_replay_of_the_same_event_is_idempotent_even_when_resealed,
        test_two_different_events_at_one_position_keep_both_and_freeze_the_feed,
        test_owners_cannot_see_or_write_each_others_meshes,
    ):
        with bound_clone(current_lineages().current()) as clone:
            check(clone)


def test_concurrent_competing_events_record_one_winner_and_one_fork(migrated_clone):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from tests.repositories.test_chat_read_receipts_postgres import concurrent_database

    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
    start = Barrier(2, timeout=30)

    def competitor(args):
        database, tag = args
        start.wait()
        with database.transaction() as tx:
            return repo.append_event(tx, **event_kwargs(mesh, 1, None, tag=tag)).outcome

    with (
        concurrent_database(migrated_clone) as first,
        concurrent_database(migrated_clone) as second,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        outcomes = sorted(workers.map(competitor, ((first, "left"), (second, "right"))))
    assert outcomes == [APPENDED, FORKED]
    with migrated_clone.database.transaction() as tx:
        assert repo.get_feed(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED).state == "frozen"
        assert len(repo.list_fork_evidence(tx, owner_id=OWNER, mesh_id=mesh, feed_id=FEED)) == 2
