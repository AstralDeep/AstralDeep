"""Exercises the mesh roster and checkpoint store against real PostgreSQL: the mesh id as the
hash of the first entry, entries that must follow held entries, concurrent tips, checkpoints
with countersignatures, and the reads that compare checkpoints with the held feed heads.
"""

import pytest

from astralplane import create_event_log_repository, create_event_roster_repository
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from tests.repositories._mesh_support import (
    FEED,
    FEED_TWO,
    OTHER,
    OWNER,
    append_chain,
    genesis,
    ref,
)

roster = create_event_roster_repository()
log = create_event_log_repository()


def entry(tx, mesh, kind, follows, tag, owner=OWNER):
    return roster.append_entry(
        tx,
        owner_id=owner,
        mesh_id=mesh,
        kind=kind,
        follows=follows,
        signed_bytes=f"{kind}|{tag}".encode(),
        recorded_at=10,
    )


def test_the_mesh_id_is_the_hash_of_the_first_entry_and_creation_is_idempotent(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        first = roster.create_mesh(
            tx, owner_id=OWNER, kind="genesis", signed_bytes=b"first", recorded_at=1
        )
        assert first.mesh_id == first.entry_id == ref(b"first")
        assert first.follows == ()
        again = roster.create_mesh(
            tx, owner_id=OWNER, kind="genesis", signed_bytes=b"first", recorded_at=77
        )
        assert again == first
        assert log.get_mesh(tx, owner_id=OWNER, mesh_id=first.mesh_id).recorded_at == 1
        with pytest.raises(RepositoryConflictError, match="another namespace"):
            roster.create_mesh(
                tx, owner_id=OTHER, kind="genesis", signed_bytes=b"first", recorded_at=1
            )
        with pytest.raises(RepositoryConflictError, match="different semantics"):
            roster.create_mesh(
                tx, owner_id=OWNER, kind="other-kind", signed_bytes=b"first", recorded_at=1
            )


def test_entries_follow_held_entries_and_tips_track_concurrent_branches(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        add_a = entry(tx, mesh, "add", [mesh], "a")
        add_b = entry(tx, mesh, "add", [mesh], "b")
        assert {tip.entry_id for tip in roster.list_tips(tx, owner_id=OWNER, mesh_id=mesh)} == {
            add_a.entry_id,
            add_b.entry_id,
        }
        merged = entry(tx, mesh, "remove", [add_b.entry_id, add_a.entry_id], "m")
        assert merged.follows == tuple(sorted([add_a.entry_id, add_b.entry_id]))
        assert [tip.entry_id for tip in roster.list_tips(tx, owner_id=OWNER, mesh_id=mesh)] == [
            merged.entry_id
        ]
        assert entry(tx, mesh, "remove", [add_a.entry_id, add_b.entry_id], "m") == merged
        listed = roster.list_entries(tx, owner_id=OWNER, mesh_id=mesh)
        assert len(listed) == 4 and listed[0].entry_id == mesh
        assert (
            roster.get_entry(tx, owner_id=OWNER, mesh_id=mesh, entry_id=merged.entry_id) == merged
        )


def test_entries_must_follow_recorded_entries_and_stay_inside_the_owner(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        other_mesh = genesis(tx, owner=OTHER)
        with pytest.raises(RepositoryNotFoundError, match="followed"):
            entry(tx, mesh, "add", [ref(b"unknown")], "x")
        with pytest.raises(RepositoryNotFoundError, match="followed"):
            entry(tx, mesh, "add", [other_mesh], "x")
        with pytest.raises(RepositoryNotFoundError):
            entry(tx, other_mesh, "add", [other_mesh], "x", owner=OWNER)
        with pytest.raises(RepositoryValidationError):
            entry(tx, mesh, "add", [], "x")
        with pytest.raises(RepositoryValidationError):
            entry(tx, mesh, "add", [mesh, mesh], "x")
        with pytest.raises(RepositoryValidationError):
            entry(tx, mesh, "add", "not-a-list", "x")
        with pytest.raises(RepositoryValidationError):
            entry(tx, mesh, "add", ["nope"], "x")
        assert roster.get_entry(tx, owner_id=OTHER, mesh_id=mesh, entry_id=mesh) is None
        assert roster.list_entries(tx, owner_id=OTHER, mesh_id=mesh) == ()
        assert roster.list_tips(tx, owner_id=OTHER, mesh_id=mesh) == ()
        roster.append_entry(
            tx,
            owner_id=OWNER,
            mesh_id=mesh,
            kind="add",
            follows=[mesh],
            signed_bytes=b"same",
            recorded_at=1,
        )
        with pytest.raises(RepositoryConflictError, match="different semantics"):
            roster.append_entry(
                tx,
                owner_id=OWNER,
                mesh_id=mesh,
                kind="remove",
                follows=[mesh],
                signed_bytes=b"same",
                recorded_at=1,
            )


def test_checkpoints_collect_countersignatures_idempotently(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        results = append_chain(tx, mesh, 2)
        head = results[1].event.event_id
        checkpoint = roster.put_checkpoint(
            tx,
            owner_id=OWNER,
            mesh_id=mesh,
            feed_id=FEED,
            seq=2,
            head_hash=head,
            signed_bytes=b"checkpoint-1",
            recorded_at=3,
        )
        assert checkpoint.checkpoint_id == ref(b"checkpoint-1")
        assert checkpoint.countersignatures == ()
        again = roster.put_checkpoint(
            tx,
            owner_id=OWNER,
            mesh_id=mesh,
            feed_id=FEED,
            seq=2,
            head_hash=head,
            signed_bytes=b"checkpoint-1",
            recorded_at=99,
        )
        assert again == checkpoint
        signed = roster.add_countersignature(
            tx,
            owner_id=OWNER,
            mesh_id=mesh,
            checkpoint_id=checkpoint.checkpoint_id,
            signer_id=FEED_TWO,
            signature=b"sig-1",
            recorded_at=4,
        )
        assert [(s.signer_id, s.signature) for s in signed.countersignatures] == [
            (FEED_TWO, b"sig-1")
        ]
        replay = roster.add_countersignature(
            tx,
            owner_id=OWNER,
            mesh_id=mesh,
            checkpoint_id=checkpoint.checkpoint_id,
            signer_id=FEED_TWO,
            signature=b"different-sig",
            recorded_at=5,
        )
        assert replay == signed
        roster.add_countersignature(
            tx,
            owner_id=OWNER,
            mesh_id=mesh,
            checkpoint_id=checkpoint.checkpoint_id,
            signer_id="feed-third",
            signature=b"sig-2",
            recorded_at=6,
        )
        listed = roster.list_checkpoints(tx, owner_id=OWNER, mesh_id=mesh)
        assert [len(item.countersignatures) for item in listed] == [2]
        assert roster.list_checkpoints(tx, owner_id=OWNER, mesh_id=mesh, feed_id="none") == ()
        assert roster.list_checkpoints(tx, owner_id=OTHER, mesh_id=mesh) == ()
        assert (
            roster.get_checkpoint(
                tx, owner_id=OTHER, mesh_id=mesh, checkpoint_id=checkpoint.checkpoint_id
            )
            is None
        )
        with pytest.raises(RepositoryConflictError, match="different semantics"):
            roster.put_checkpoint(
                tx,
                owner_id=OWNER,
                mesh_id=mesh,
                feed_id=FEED,
                seq=1,
                head_hash=head,
                signed_bytes=b"checkpoint-1",
                recorded_at=3,
            )
        with pytest.raises(RepositoryNotFoundError):
            roster.add_countersignature(
                tx,
                owner_id=OWNER,
                mesh_id=mesh,
                checkpoint_id=ref(b"unknown"),
                signer_id=FEED,
                signature=b"s",
                recorded_at=1,
            )
        with pytest.raises(RepositoryNotFoundError):
            roster.put_checkpoint(
                tx,
                owner_id=OTHER,
                mesh_id=mesh,
                feed_id=FEED,
                seq=1,
                head_hash=head,
                signed_bytes=b"checkpoint-foreign",
                recorded_at=3,
            )


def test_countersigned_checkpoints_reveal_feeds_that_are_shorter_or_contradicted(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx)
        results = append_chain(tx, mesh, 2)

        def checkpoint(feed, seq, head, tag, signed=True):
            record = roster.put_checkpoint(
                tx,
                owner_id=OWNER,
                mesh_id=mesh,
                feed_id=feed,
                seq=seq,
                head_hash=head,
                signed_bytes=tag.encode(),
                recorded_at=1,
            )
            if signed:
                roster.add_countersignature(
                    tx,
                    owner_id=OWNER,
                    mesh_id=mesh,
                    checkpoint_id=record.checkpoint_id,
                    signer_id="peer",
                    signature=b"s",
                    recorded_at=1,
                )
            return record

        checkpoint(FEED, 2, results[1].event.event_id, "matches")
        assert roster.list_feeds_behind_checkpoints(tx, owner_id=OWNER, mesh_id=mesh) == ()
        assert roster.list_checkpoint_contradictions(tx, owner_id=OWNER, mesh_id=mesh) == ()
        ahead = checkpoint(FEED, 5, ref(b"later head"), "ahead")
        checkpoint(FEED, 6, ref(b"unsigned later head"), "unsigned", signed=False)
        unseen = checkpoint("feed-unseen", 3, ref(b"unseen head"), "unseen")
        behind = roster.list_feeds_behind_checkpoints(tx, owner_id=OWNER, mesh_id=mesh)
        assert [(item.feed_id, item.checkpoint_seq, item.held_seq) for item in behind] == [
            (FEED, 5, 2),
            ("feed-unseen", 3, 0),
        ]
        assert {item.checkpoint_id for item in behind} == {
            ahead.checkpoint_id,
            unseen.checkpoint_id,
        }
        wrong = checkpoint(FEED, 1, ref(b"not the first event"), "contradicts")
        contradictions = roster.list_checkpoint_contradictions(tx, owner_id=OWNER, mesh_id=mesh)
        assert [(c.checkpoint_id, c.held_event_id) for c in contradictions] == [
            (wrong.checkpoint_id, results[0].event.event_id)
        ]
        assert roster.list_feeds_behind_checkpoints(tx, owner_id=OTHER, mesh_id=mesh) == ()
        assert roster.list_checkpoint_contradictions(tx, owner_id=OTHER, mesh_id=mesh) == ()


def test_public_event_roster_contract_behaviors():
    from tests.fixtures.migrated_template import bound_clone, current_lineages

    for check in (
        test_the_mesh_id_is_the_hash_of_the_first_entry_and_creation_is_idempotent,
        test_entries_follow_held_entries_and_tips_track_concurrent_branches,
        test_checkpoints_collect_countersignatures_idempotently,
        test_entries_must_follow_recorded_entries_and_stay_inside_the_owner,
    ):
        with bound_clone(current_lineages().current()) as clone:
            check(clone)
