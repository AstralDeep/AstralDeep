"""Exercises observation capture, paged recall, expiry, purge, races, corruption, and
caller rollback against real PostgreSQL. The focused repository contract matrix reuses
these scope, replay, concurrency, and failure cases as executable evidence.
"""

from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from astralplane.api import create_repository_catalog
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.observations import (
    MAX_OBSERVATION_OCTETS,
    MAX_PAGE_OCTETS,
    ObservationCaptureCommand,
    ObservationCursor,
)
from tests.fixtures.database_errors import check_violation
from tests.integration.test_lifecycle_acceptance import concurrent_database

OWNER = "observation-owner"
FOREIGN = "observation-foreign-owner"
CONVERSATION = "conversation-1"
OTHER_CONVERSATION = "conversation-2"
OBSERVATION_A = "70000000-0000-4000-8000-000000000001"
OBSERVATION_B = "70000000-0000-4000-8000-000000000002"
OBSERVATION_C = "70000000-0000-4000-8000-000000000003"


def capture_command(**changes):
    values = dict(
        owner_id=OWNER,
        conversation_id=CONVERSATION,
        observation_id=OBSERVATION_A,
        operation_id="operation-1",
        source_refs=("synthetic/source-one",),
        seal="synthetic-seal-v1",
        content=b"synthetic-opaque-ciphertext",
        created_at=1000,
        expires_at=2000,
    )
    values.update(changes)
    return ObservationCaptureCommand(**values)


def test_capture_recall_and_listing_round_trip_scopes_to_owner_and_conversation(migrated_clone):
    repo = create_repository_catalog().observations
    first = capture_command()
    second = capture_command(
        observation_id=OBSERVATION_B,
        operation_id="operation-2",
        conversation_id=OTHER_CONVERSATION,
        created_at=1001,
        expires_at=2001,
    )
    with migrated_clone.database.transaction() as tx:
        captured = repo.capture(tx, command=first)
        assert not captured.replayed
        assert captured.observation.capture_seq == 1
        assert captured.observation.page_count == 1
        assert captured.observation.observation_octets == len(first.content)
        assert captured.observation.content_sha256 == hashlib.sha256(first.content).hexdigest()
        other = repo.capture(tx, command=second)
        assert other.observation.capture_seq == 1
        assert repo.get_observation(
            tx, owner_id=OWNER, conversation_id=CONVERSATION, observation_id=OBSERVATION_A, now=1500
        ) == captured.observation
        assert (
            repo.get_observation(
                tx,
                owner_id=FOREIGN,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                now=1500,
            )
            is None
        )
        assert (
            repo.get_observation(
                tx, owner_id=OWNER, conversation_id=OTHER_CONVERSATION, observation_id=OBSERVATION_A
            )
            is None
        )
        listing = repo.list_observations(tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1500)
        assert [record.observation_id for record in listing.records] == [OBSERVATION_A]
        foreign = repo.list_observations(
            tx, owner_id=FOREIGN, conversation_id=CONVERSATION, now=1500
        )
        assert foreign.records == ()
        stored = tx.fetch_one(
            "SELECT content, content_sha256 FROM tool_observation_page"
            " WHERE owner_id=%s AND conversation_id=%s AND observation_id=%s AND page_no=1",
            (OWNER, CONVERSATION, OBSERVATION_A),
        )
        assert bytes(stored["content"]) == first.content
        assert stored["content_sha256"] == hashlib.sha256(first.content).hexdigest()
        head = tx.fetch_one(
            "SELECT seal, source_refs, operation_id FROM tool_observation"
            " WHERE owner_id=%s AND conversation_id=%s AND observation_id=%s",
            (OWNER, CONVERSATION, OBSERVATION_A),
        )
        assert head["seal"] == "synthetic-seal-v1"
        assert head["source_refs"] == ("synthetic/source-one",)
        assert head["operation_id"] == "operation-1"
        usage = repo.conversation_usage(tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1500)
        assert (usage.live_count, usage.retained_octets, usage.expired_count) == (1, 27, 0)


def test_capture_replay_is_exact_and_conflicting_identity_is_refused(migrated_clone):
    repo = create_repository_catalog().observations
    command = capture_command()
    with migrated_clone.database.transaction() as tx:
        original = repo.capture(tx, command=command)
        replay = repo.capture(tx, command=command)
        assert replay.replayed and replay.observation == original.observation
        with pytest.raises(RepositoryConflictError, match="identity was reused"):
            repo.capture(tx, command=replace(command, content=b"changed-ciphertext"))
        with pytest.raises(RepositoryConflictError, match="identity was reused"):
            repo.capture(tx, command=replace(command, expires_at=9000))
        second = repo.capture(
            tx, command=replace(command, observation_id=OBSERVATION_B, operation_id="operation-2")
        )
        assert second.observation.capture_seq == original.observation.capture_seq + 1
        assert tx.fetch_one("SELECT count(*) AS count FROM tool_observation")["count"] == 2
    with migrated_clone.database.transaction() as tx:
        assert repo.capture(tx, command=command).observation == original.observation


def test_paged_recall_reassembles_exact_bytes_and_reports_end_state(migrated_clone):
    repo = create_repository_catalog().observations
    payload = bytes(
        value % 251 for value in range(MAX_PAGE_OCTETS * 2 + 1234)
    )
    command = capture_command(content=payload, expires_at=5000)
    with migrated_clone.database.transaction() as tx:
        captured = repo.capture(tx, command=command)
        assert captured.observation.page_count == 3
        assert captured.observation.observation_octets == len(payload)
        recalled = bytearray()
        page_no = 1
        while True:
            page = repo.read_page(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                page_no=page_no,
                now=1500,
            )
            assert page is not None
            assert page.content_sha256 == captured.observation.content_sha256
            if page_no < 3:
                assert page.continuation and len(page.content) == MAX_PAGE_OCTETS
            recalled.extend(page.content)
            if not page.continuation:
                break
            page_no += 1
        assert bytes(recalled) == payload
        end = repo.read_page(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            page_no=4,
            now=1500,
        )
        assert end is not None and end.content == b"" and not end.continuation
        with pytest.raises(RepositoryValidationError, match="beyond the observation extent"):
            repo.read_page(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                page_no=5,
                now=1500,
            )
        with pytest.raises(RepositoryValidationError):
            repo.read_page(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                page_no=0,
                now=1500,
            )
        verified = repo.verify_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            now=1500,
        )
        assert verified == captured.observation
        assert (
            repo.verify_observation(
                tx,
                owner_id=FOREIGN,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                now=1500,
            )
            is None
        )


def test_recall_denies_foreign_expired_and_purged_sources_without_disclosure(migrated_clone):
    repo = create_repository_catalog().observations
    with migrated_clone.database.transaction() as tx:
        repo.capture(tx, command=capture_command())
        repo.capture(
            tx,
            command=capture_command(
                observation_id=OBSERVATION_B, operation_id="operation-2", expires_at=1500
            ),
        )
        repo.purge_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_B,
            reason="revoked",
            at=1200,
        )
        for owner_id, observation_id, now in (
            (FOREIGN, OBSERVATION_A, 1500),
            (OWNER, OBSERVATION_B, 1500),
            (OWNER, OBSERVATION_A, 2000),
            (OWNER, OBSERVATION_A, 2001),
            (OWNER, "80000000-0000-4000-8000-000000000009", 1500),
        ):
            assert (
                repo.get_observation(
                    tx,
                    owner_id=owner_id,
                    conversation_id=CONVERSATION,
                    observation_id=observation_id,
                    now=now,
                )
                is None
            )
            assert (
                repo.read_page(
                    tx,
                    owner_id=owner_id,
                    conversation_id=CONVERSATION,
                    observation_id=observation_id,
                    page_no=1,
                    now=now,
                )
                is None
            )
            assert (
                repo.verify_observation(
                    tx,
                    owner_id=owner_id,
                    conversation_id=CONVERSATION,
                    observation_id=observation_id,
                    now=now,
                )
                is None
            )
        tombstone = repo.get_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_B,
            include_purged=True,
        )
        assert tombstone is not None and tombstone.purged
        assert tombstone.purged_reason == "revoked"
        assert tombstone.seal is None and tombstone.content_sha256 is None


def test_expiry_is_absolute_and_never_renewed_by_recall(migrated_clone):
    repo = create_repository_catalog().observations
    command = capture_command(expires_at=1500)
    with migrated_clone.database.transaction() as tx:
        repo.capture(tx, command=command)
        assert (
            repo.read_page(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                page_no=1,
                now=1499,
            )
            is not None
        )
        assert (
            repo.read_page(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                page_no=1,
                now=1500,
            )
            is None
        )
    with migrated_clone.database.transaction() as tx:
        assert (
            repo.read_page(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                page_no=1,
                now=1499,
            )
            is not None
        )
        stored = tx.fetch_one(
            "SELECT expires_at FROM tool_observation WHERE owner_id=%s"
            " AND conversation_id=%s AND observation_id=%s",
            (OWNER, CONVERSATION, OBSERVATION_A),
        )
        assert stored["expires_at"] == 1500


def test_purge_erases_pages_and_the_tombstone_is_terminal(migrated_clone):
    repo = create_repository_catalog().observations
    with migrated_clone.database.transaction() as tx:
        repo.capture(tx, command=capture_command(content=b"x" * (MAX_PAGE_OCTETS + 5)))
        purged = repo.purge_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            reason="deleted",
            at=1500,
        )
        assert not purged.replayed and purged.observation.purged
        assert purged.observation.purged_at == 1500
        assert purged.observation.page_count == 0
        assert tx.fetch_one("SELECT count(*) AS count FROM tool_observation_page")["count"] == 0
        replay = repo.purge_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            reason="deleted",
            at=1500,
        )
        assert replay.replayed and replay.observation == purged.observation
    with (
        migrated_clone.database.transaction() as tx,
        pytest.raises(RepositoryConflictError, match="purge reason changed"),
    ):
        repo.purge_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            reason="revoked",
            at=1500,
        )
    with (
        migrated_clone.database.transaction() as tx,
        pytest.raises(RepositoryConflictError, match="identity was reused"),
    ):
        repo.capture(tx, command=capture_command())
    with (
        check_violation(),
        migrated_clone.database.transaction() as tx,
    ):
        tx.execute(
            "UPDATE tool_observation SET purged_at=NULL,purged_reason=NULL,page_count=1,"
            "observation_octets=5,content_sha256=%s,seal=%s"
            " WHERE owner_id=%s AND conversation_id=%s AND observation_id=%s",
            (
                hashlib.sha256(b"restored").hexdigest(),
                "synthetic-seal-v1",
                OWNER,
                CONVERSATION,
                OBSERVATION_A,
            ),
        )
    with (
        check_violation(),
        migrated_clone.database.transaction() as tx,
    ):
        tx.execute(
            "DELETE FROM tool_observation WHERE owner_id=%s AND conversation_id=%s"
            " AND observation_id=%s",
            (OWNER, CONVERSATION, OBSERVATION_A),
        )


def test_page_mutations_and_unpurged_deletion_are_refused_by_postgresql(migrated_clone):
    repo = create_repository_catalog().observations
    with migrated_clone.database.transaction() as tx:
        repo.capture(tx, command=capture_command())
    with (
        check_violation(),
        migrated_clone.database.transaction() as tx,
    ):
        tx.execute(
            "UPDATE tool_observation_page SET content=%s WHERE owner_id=%s"
            " AND conversation_id=%s AND observation_id=%s AND page_no=1",
            (b"tampered", OWNER, CONVERSATION, OBSERVATION_A),
        )
    with (
        check_violation(),
        migrated_clone.database.transaction() as tx,
    ):
        tx.execute(
            "DELETE FROM tool_observation_page WHERE owner_id=%s AND conversation_id=%s"
            " AND observation_id=%s AND page_no=1",
            (OWNER, CONVERSATION, OBSERVATION_A),
        )
    with migrated_clone.database.transaction() as tx:
        assert repo.read_page(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            page_no=1,
            now=1500,
        ).content == b"synthetic-opaque-ciphertext"


def test_purge_fences_clock_and_missing_rows(migrated_clone):
    repo = create_repository_catalog().observations
    with migrated_clone.database.transaction() as tx:
        repo.capture(tx, command=capture_command())
        with pytest.raises(RepositoryConflictError, match="clock regressed"):
            repo.purge_observation(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                reason="deleted",
                at=999,
            )
        with pytest.raises(RepositoryNotFoundError):
            repo.purge_observation(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_B,
                reason="deleted",
                at=1500,
            )
        with pytest.raises(RepositoryNotFoundError):
            repo.purge_observation(
                tx,
                owner_id=FOREIGN,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                reason="deleted",
                at=1500,
            )
        assert repo.conversation_usage(
            tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1500
        ).live_count == 1


def test_purge_expired_sweeps_only_expired_observations_in_order(migrated_clone):
    repo = create_repository_catalog().observations
    with migrated_clone.database.transaction() as tx:
        repo.capture(tx, command=capture_command(expires_at=1100))
        repo.capture(
            tx,
            command=capture_command(
                observation_id=OBSERVATION_B, operation_id="operation-2", expires_at=1200
            ),
        )
        repo.capture(
            tx,
            command=capture_command(
                observation_id=OBSERVATION_C, operation_id="operation-3", expires_at=2000
            ),
        )
        swept = repo.purge_expired(
            tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1250, limit=1
        )
        assert [purge.observation.observation_id for purge in swept] == [OBSERVATION_A]
        assert all(purge.observation.purged_reason == "expired" for purge in swept)
        swept = repo.purge_expired(
            tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1250, limit=10
        )
        assert [purge.observation.observation_id for purge in swept] == [OBSERVATION_B]
        replay = repo.purge_expired(
            tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1250, limit=10
        )
        assert replay == ()
        usage = repo.conversation_usage(tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1250)
        assert usage.live_count == 1 and usage.expired_count == 0
        assert usage.retained_octets == len(b"synthetic-opaque-ciphertext")
    with migrated_clone.database.transaction() as tx:
        assert repo.purge_expired(
            tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1250
        ) == ()
        remaining = repo.list_observations(
            tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1250
        )
        assert [record.observation_id for record in remaining.records] == [OBSERVATION_C]


def test_listing_is_bounded_ordered_and_cursor_scoped(migrated_clone):
    repo = create_repository_catalog().observations
    with migrated_clone.database.transaction() as tx:
        for index, observation_id in enumerate(
            (OBSERVATION_A, OBSERVATION_B, OBSERVATION_C), start=1
        ):
            repo.capture(
                tx,
                command=capture_command(
                    observation_id=observation_id,
                    operation_id=f"operation-{index}",
                    created_at=1000 + index,
                    expires_at=2000 + index,
                ),
            )
        repo.capture(
            tx,
            command=capture_command(
                owner_id=FOREIGN,
                observation_id=OBSERVATION_A,
                operation_id="foreign-operation",
            ),
        )
        repo.capture(
            tx,
            command=capture_command(
                observation_id="70000000-0000-4000-8000-000000000004",
                operation_id="operation-4",
                conversation_id=OTHER_CONVERSATION,
            ),
        )
        first = repo.list_observations(
            tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1500, limit=2
        )
        assert [record.capture_seq for record in first.records] == [1, 2]
        assert first.next_cursor is not None
        second = repo.list_observations(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            now=1500,
            cursor=first.next_cursor,
            limit=2,
        )
        assert [record.capture_seq for record in second.records] == [3]
        assert second.next_cursor is None
        with pytest.raises(RepositoryValidationError):
            repo.list_observations(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                now=1500,
                cursor=ObservationCursor(FOREIGN, CONVERSATION, 1),
            )
        with pytest.raises(RepositoryValidationError):
            repo.list_observations(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                now=1500,
                cursor=ObservationCursor(OWNER, OTHER_CONVERSATION, 1),
            )
        repo.purge_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_B,
            reason="deleted",
            at=1600,
        )
        remaining = repo.list_observations(
            tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1700
        )
        assert [record.observation_id for record in remaining.records] == [
            OBSERVATION_A,
            OBSERVATION_C,
        ]


def test_conversation_capacity_bounds_retained_bytes_and_purge_frees_them(migrated_clone):
    repo = create_repository_catalog().observations
    maxed = b"x" * MAX_OBSERVATION_OCTETS
    with migrated_clone.database.transaction() as tx:
        for index in range(8):
            captured = repo.capture(
                tx,
                command=capture_command(
                    observation_id=f"70000000-0000-4000-8000-00000000000{index + 1}",
                    operation_id=f"operation-{index}",
                    content=maxed,
                    created_at=1000 + index,
                    expires_at=2000 + index,
                ),
            )
            assert captured.observation.page_count == 512
        with pytest.raises(RepositoryConflictError, match="capacity is exhausted"):
            repo.capture(
                tx,
                command=capture_command(
                    observation_id="70000000-0000-4000-8000-000000000009",
                    operation_id="operation-overflow",
                    content=b"one byte over",
                ),
            )
        usage = repo.conversation_usage(tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1500)
        assert usage.retained_octets == 8 * MAX_OBSERVATION_OCTETS
        repo.purge_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            reason="deleted",
            at=1500,
        )
        freed = repo.capture(
            tx,
            command=capture_command(
                observation_id="70000000-0000-4000-8000-000000000009",
                operation_id="operation-overflow",
                content=b"fits again",
            ),
        )
        assert freed.observation.observation_octets == len(b"fits again")


@pytest.mark.postgresql_schema_catalog
def test_corrupt_pages_and_extent_fail_closed(migrated_clone):
    repo = create_repository_catalog().observations
    with migrated_clone.database.transaction() as tx:
        repo.capture(tx, command=capture_command())
        tx.execute(
            "ALTER TABLE tool_observation_page DISABLE TRIGGER tool_observation_page_immutable"
        )
        tx.execute(
            "UPDATE tool_observation_page SET content=%s WHERE page_no=1",
            (b"tampered-ciphertext",),
        )
    with migrated_clone.database.transaction() as tx:
        with pytest.raises(RepositoryDataError, match="failed its integrity check"):
            repo.read_page(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                page_no=1,
                now=1500,
            )
        with pytest.raises(RepositoryDataError, match="failed its integrity check"):
            repo.verify_observation(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                now=1500,
            )
    with migrated_clone.database.transaction() as tx:
        tx.execute(
            "ALTER TABLE tool_observation_page DISABLE TRIGGER tool_observation_page_immutable"
        )
        tx.execute(
            "UPDATE tool_observation_page SET content=%s, content_sha256=%s WHERE page_no=1",
            (b"tampered-ciphertext", hashlib.sha256(b"tampered-ciphertext").hexdigest()),
        )
    with migrated_clone.database.transaction() as tx:
        page = repo.read_page(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            page_no=1,
            now=1500,
        )
        assert page.content == b"tampered-ciphertext"
        with pytest.raises(RepositoryDataError, match="extent is inconsistent"):
            repo.verify_observation(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                now=1500,
            )
    with migrated_clone.database.transaction() as tx:
        tx.execute("ALTER TABLE tool_observation_page DISABLE TRIGGER tool_observation_page_purge")
        tx.execute(
            "DELETE FROM tool_observation_page WHERE owner_id=%s AND conversation_id=%s"
            " AND observation_id=%s AND page_no=1",
            (OWNER, CONVERSATION, OBSERVATION_A),
        )
    with migrated_clone.database.transaction() as tx:
        with pytest.raises(RepositoryDataError, match="page is missing"):
            repo.read_page(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                page_no=1,
                now=1500,
            )
        with pytest.raises(RepositoryDataError, match="page count is inconsistent"):
            repo.verify_observation(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                now=1500,
            )


def test_caller_rollback_removes_capture_and_purge_writes(migrated_clone):
    repo = create_repository_catalog().observations
    with (
        pytest.raises(RuntimeError, match="caller abort capture"),
        migrated_clone.database.transaction() as tx,
    ):
        repo.capture(tx, command=capture_command())
        raise RuntimeError("caller abort capture")
    with migrated_clone.database.transaction() as tx:
        assert (
            repo.get_observation(
                tx, owner_id=OWNER, conversation_id=CONVERSATION, observation_id=OBSERVATION_A
            )
            is None
        )
        assert tx.fetch_one("SELECT count(*) AS count FROM tool_observation")["count"] == 0
    with migrated_clone.database.transaction() as tx:
        repo.capture(tx, command=capture_command())
    with (
        pytest.raises(RuntimeError, match="caller abort purge"),
        migrated_clone.database.transaction() as tx,
    ):
        repo.purge_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            reason="deleted",
            at=1500,
        )
        raise RuntimeError("caller abort purge")
    with migrated_clone.database.transaction() as tx:
        assert (
            repo.read_page(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                page_no=1,
                now=1500,
            ).content
            == b"synthetic-opaque-ciphertext"
        )


def test_concurrent_capture_admits_exactly_one_writer_per_identity(migrated_clone):
    repo = create_repository_catalog().observations
    barrier = Barrier(2)
    commands = (
        capture_command(),
        capture_command(operation_id="operation-race"),
    )

    def capture(database, command):
        try:
            with database.transaction() as tx:
                barrier.wait(timeout=30)
                return repo.capture(tx, command=command)
        except RepositoryConflictError:
            return None

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = tuple(workers.map(lambda command: capture(database, command), commands))
    accepted = tuple(result for result in results if result is not None)
    assert len(accepted) == 1
    with migrated_clone.database.transaction() as tx:
        assert repo.get_observation(
            tx, owner_id=OWNER, conversation_id=CONVERSATION, observation_id=OBSERVATION_A
        ) == accepted[0].observation
        assert tx.fetch_one("SELECT count(*) AS count FROM tool_observation")["count"] == 1
        assert tx.fetch_one("SELECT count(*) AS count FROM tool_observation_page")["count"] == 1


def test_distinct_concurrent_captures_assign_distinct_capture_order(migrated_clone):
    repo = create_repository_catalog().observations
    barrier = Barrier(2)
    commands = (
        capture_command(),
        capture_command(observation_id=OBSERVATION_B, operation_id="operation-2"),
    )

    def capture(database, command):
        with database.transaction() as tx:
            barrier.wait(timeout=30)
            return repo.capture(tx, command=command)

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = tuple(workers.map(lambda command: capture(database, command), commands))
    sequences = sorted(result.observation.capture_seq for result in results)
    assert sequences == [1, 2]
    with migrated_clone.database.transaction() as tx:
        listing = repo.list_observations(tx, owner_id=OWNER, conversation_id=CONVERSATION, now=1500)
        assert len(listing.records) == 2


def test_public_observation_contract_behaviors():
    from test_assignments_postgres import parallel_transactions, standalone_database

    fixture = standalone_database()
    database = next(fixture)
    repo = create_repository_catalog().observations
    first = capture_command()
    try:
        with database.transaction() as tx:
            captured = repo.capture(tx, command=first)
            assert (
                repo.get_observation(
                    tx,
                    owner_id=FOREIGN,
                    conversation_id=CONVERSATION,
                    observation_id=OBSERVATION_A,
                )
                is None
            )
            assert repo.list_observations(
                tx, owner_id=FOREIGN, conversation_id=CONVERSATION, now=1500
            ).records == ()
            replay = repo.capture(tx, command=first)
            assert replay.replayed and replay.observation == captured.observation
            with pytest.raises(RepositoryConflictError):
                repo.capture(tx, command=replace(first, content=b"conflicting-ciphertext"))
            with pytest.raises(RepositoryNotFoundError):
                repo.purge_observation(
                    tx,
                    owner_id=OWNER,
                    conversation_id=CONVERSATION,
                    observation_id=OBSERVATION_B,
                    reason="deleted",
                    at=1500,
                )
        results = parallel_transactions(
            database,
            (
                lambda tx: repo.purge_observation(
                    tx,
                    owner_id=OWNER,
                    conversation_id=CONVERSATION,
                    observation_id=OBSERVATION_A,
                    reason="deleted",
                    at=1500,
                ),
                lambda tx: repo.purge_observation(
                    tx,
                    owner_id=OWNER,
                    conversation_id=CONVERSATION,
                    observation_id=OBSERVATION_A,
                    reason="revoked",
                    at=1500,
                ),
            ),
        )
        succeeded = tuple(result for result in results if not isinstance(result, Exception))
        assert len(succeeded) == 1
        with database.transaction() as tx:
            assert repo.read_page(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                page_no=1,
                now=1500,
            ) is None
            assert repo.get_observation(
                tx,
                owner_id=OWNER,
                conversation_id=CONVERSATION,
                observation_id=OBSERVATION_A,
                include_purged=True,
            ).purged
            with pytest.raises(RepositoryNotFoundError):
                repo.purge_observation(
                    tx,
                    owner_id=OWNER,
                    conversation_id=CONVERSATION,
                    observation_id=str(uuid.uuid4()),
                    reason="deleted",
                    at=1500,
                )
    finally:
        fixture.close()


def test_maximum_bounds_capture_recall_and_purge(migrated_clone):
    repo = create_repository_catalog().observations
    command = capture_command(
        content=b"\xfe" * MAX_OBSERVATION_OCTETS,
        operation_id="x" * 512,
        seal="s" * 512,
        source_refs=tuple(f"source-{index}" for index in range(8)),
        created_at=2**53 - 90000,
        expires_at=2**53 - 90000 + 86400,
    )
    with migrated_clone.database.transaction() as tx:
        captured = repo.capture(tx, command=command)
        assert captured.observation.page_count == 512
        assert captured.observation.observation_octets == MAX_OBSERVATION_OCTETS
        tail = repo.read_page(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            page_no=512,
            now=captured.observation.created_at + 1,
        )
        assert tail.content == b"\xfe" * (MAX_OBSERVATION_OCTETS - 511 * MAX_PAGE_OCTETS)
        assert not tail.continuation
        purged = repo.purge_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            reason="expired",
            at=2**53 - 90000 + 86400,
        )
        assert purged.observation.purged_at == 2**53 - 90000 + 86400
    with migrated_clone.database.transaction() as tx:
        assert repo.purge_observation(
            tx,
            owner_id=OWNER,
            conversation_id=CONVERSATION,
            observation_id=OBSERVATION_A,
            reason="expired",
            at=2**53 - 90000 + 86400,
        ).replayed


@pytest.mark.parametrize(
    "changes",
    [
        {"created_at": 2**53},
        {"expires_at": 2**53},
        {"content": b""},
        {"seal": "x" * 513},
        {"source_refs": tuple(str(index) for index in range(9))},
    ],
)
def test_out_of_contract_capture_is_typed_and_transaction_remains_usable(migrated_clone, changes):
    with migrated_clone.database.transaction() as tx:
        with pytest.raises(RepositoryValidationError):
            capture_command(**changes)
        assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1
        assert tx.fetch_all("SELECT * FROM tool_observation") == ()
