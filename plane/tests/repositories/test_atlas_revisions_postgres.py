"""Exercises Atlas edit fences, immutable revision history, replay after deletion, bounded
pagination, concurrency, and caller rollback against real PostgreSQL. The repository contract
matrix reuses these scope, replay, concurrency, and failure cases as executable evidence.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import psycopg2
import pytest

from astralplane.api import create_repository_catalog
from astralplane.errors import TransactionCommitOutcomeUnknownError
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.atlas_pages import (
    AtlasPageCreateCommand,
    AtlasPageDeleteCommand,
    AtlasPageEditCommand,
    AtlasPageRevisionCursor,
)
from tests.fixtures.database_errors import check_violation
from tests.integration.test_lifecycle_acceptance import concurrent_database

OWNER = "atlas-revision-owner"
FOREIGN = "atlas-revision-foreign-owner"
PROJECT = "atlas-revision-project"
PAGE_A = "10000000-0000-4000-8000-000000000011"
PAGE_B = "10000000-0000-4000-8000-000000000012"
COMMAND_A = "20000000-0000-4000-8000-000000000011"
COMMAND_B = "20000000-0000-4000-8000-000000000012"
COMMAND_C = "20000000-0000-4000-8000-000000000013"
COMMAND_D = "20000000-0000-4000-8000-000000000014"
COMMAND_E = "20000000-0000-4000-8000-000000000015"


def create_command(**changes):
    values = dict(
        owner_id=OWNER,
        command_id=COMMAND_A,
        page_id=PAGE_A,
        project_id=PROJECT,
        slug="revision-page",
        sources=("synthetic/revision-one",),
        content=b"synthetic-revision-one-ciphertext",
        created_at=10,
    )
    values.update(changes)
    return AtlasPageCreateCommand(**values)


def edit_command(**changes):
    values = dict(
        owner_id=OWNER,
        command_id=COMMAND_B,
        page_id=PAGE_A,
        expected_revision=1,
        sources=("synthetic/revision-two",),
        content=b"synthetic-revision-two-ciphertext",
        updated_at=20,
    )
    values.update(changes)
    return AtlasPageEditCommand(**values)


def delete_command(**changes):
    values = dict(
        owner_id=OWNER,
        command_id=COMMAND_D,
        page_id=PAGE_A,
        expected_revision=2,
        deleted_at=30,
    )
    values.update(changes)
    return AtlasPageDeleteCommand(**values)


def test_create_and_edits_preserve_exact_history_and_advance_one_head(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    first = create_command()
    second = edit_command()
    third = edit_command(
        command_id=COMMAND_C,
        expected_revision=2,
        sources=(),
        content=b"synthetic-revision-three-ciphertext",
        updated_at=21,
    )
    with migrated_clone.database.transaction() as tx:
        created = repo.create_page(tx, command=first)
        edited = repo.edit_page(tx, command=second)
        appended = repo.append_revision(tx, command=third)
        assert created.page.revision == 1 and created.receipt.expected_revision is None
        assert edited.page.revision == 2 and edited.page.content == second.content
        assert edited.revision.revision == 2 and edited.revision.content == second.content
        assert edited.receipt.expected_revision == 1
        assert appended.page.revision == 3 and appended.page.content == third.content
        assert appended.revision.sources == ()
        assert repo.get_revision(tx, owner_id=OWNER, page_id=PAGE_A, revision=1).content == (
            first.content
        )
        assert repo.get_revision(tx, owner_id=OWNER, page_id=PAGE_A, revision=1).sources == (
            first.sources
        )
        revisions = repo.list_revisions(tx, owner_id=OWNER, page_id=PAGE_A, limit=2)
        assert [record.revision for record in revisions.records] == [3, 2]
        assert revisions.next_cursor == AtlasPageRevisionCursor(OWNER, PAGE_A, 2)
        remainder = repo.list_revisions(
            tx,
            owner_id=OWNER,
            page_id=PAGE_A,
            cursor=revisions.next_cursor,
            limit=2,
        )
        assert [record.revision for record in remainder.records] == [1]
        assert remainder.next_cursor is None
        assert tx.fetch_one(
            "SELECT count(*) AS count FROM atlas_page_revision"
            " WHERE owner_id=%s AND page_id=%s",
            (OWNER, PAGE_A),
        )["count"] == 3
        assert repo.get_revision(tx, owner_id=FOREIGN, page_id=PAGE_A, revision=1) is None
        assert repo.list_revisions(tx, owner_id=FOREIGN, page_id=PAGE_A).records == ()
        assert repo.list_revisions(tx, owner_id=OWNER, page_id=PAGE_B).records == ()


def test_edit_replay_is_exact_while_later_edits_keep_the_original_revision(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    first_edit = edit_command()
    second_edit = edit_command(
        command_id=COMMAND_C,
        expected_revision=2,
        content=b"synthetic-revision-three-ciphertext",
        updated_at=21,
    )
    with migrated_clone.database.transaction() as tx:
        repo.create_page(tx, command=create_command())
        original = repo.edit_page(tx, command=first_edit)
        repo.edit_page(tx, command=second_edit)
        replay = repo.edit_page(tx, command=first_edit)
        assert replay.replayed and replay.page.revision == 3
        assert replay.revision == original.revision
        assert replay.receipt == original.receipt
        with pytest.raises(RepositoryConflictError, match="identity was reused"):
            repo.edit_page(tx, command=replace(first_edit, content=b"changed-ciphertext"))
        with pytest.raises(RepositoryConflictError, match="identity was reused"):
            repo.edit_page(
                tx,
                command=edit_command(
                    command_id=COMMAND_A,
                    expected_revision=3,
                    updated_at=22,
                ),
            )
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_revision")["count"] == 3
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_command")["count"] == 3


def test_edit_rejects_stale_clock_wrong_owner_and_keeps_transaction_usable(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    with migrated_clone.database.transaction() as tx:
        repo.create_page(tx, command=create_command())
        with pytest.raises(RepositoryConflictError, match="revision changed"):
            repo.edit_page(
                tx,
                command=edit_command(command_id=COMMAND_C, expected_revision=2),
            )
        with pytest.raises(RepositoryConflictError, match="clock regressed"):
            repo.edit_page(
                tx,
                command=edit_command(command_id=COMMAND_C, updated_at=9),
            )
        with pytest.raises(RepositoryNotFoundError):
            repo.edit_page(
                tx,
                command=edit_command(owner_id=FOREIGN, command_id=COMMAND_C),
            )
        with pytest.raises(RepositoryNotFoundError):
            repo.edit_page(
                tx,
                command=edit_command(command_id=COMMAND_C, page_id=PAGE_B),
            )
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A).revision == 1
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_revision")["count"] == 1
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_command")["count"] == 1
        assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1


def test_deleted_page_history_requires_explicit_include_deleted_and_cannot_resurrect(
    migrated_clone,
):
    repo = create_repository_catalog().atlas_pages
    first_edit = edit_command()
    with migrated_clone.database.transaction() as tx:
        repo.create_page(tx, command=create_command())
        edit_result = repo.edit_page(tx, command=first_edit)
        deletion = repo.delete_page(tx, command=delete_command())
        assert deletion.page.deleted and deletion.receipt.expected_revision == 2
        assert repo.get_revision(tx, owner_id=OWNER, page_id=PAGE_A, revision=2) is None
        assert repo.list_revisions(tx, owner_id=OWNER, page_id=PAGE_A).records == ()
        hidden = repo.get_revision(
            tx,
            owner_id=OWNER,
            page_id=PAGE_A,
            revision=2,
            include_deleted=True,
        )
        assert hidden == edit_result.revision
        history = repo.list_revisions(
            tx,
            owner_id=OWNER,
            page_id=PAGE_A,
            include_deleted=True,
        )
        assert [record.revision for record in history.records] == [2, 1]
        with pytest.raises(RepositoryConflictError, match="deleted"):
            repo.edit_page(tx, command=first_edit)
        with pytest.raises(RepositoryConflictError, match="deleted"):
            repo.edit_page(
                tx,
                command=edit_command(
                    command_id=COMMAND_C,
                    expected_revision=2,
                    updated_at=30,
                ),
            )
        with pytest.raises(RepositoryConflictError, match="already deleted"):
            repo.delete_page(
                tx,
                command=delete_command(command_id=COMMAND_E, expected_revision=3, deleted_at=31),
            )
        with pytest.raises(RepositoryConflictError, match="identity already exists"):
            repo.create_page(
                tx,
                command=create_command(
                    command_id=COMMAND_C,
                    slug="resurrected",
                    created_at=31,
                ),
            )
        assert repo.delete_page(tx, command=delete_command()).replayed
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_revision")["count"] == 2


def test_concurrent_edits_from_the_same_fence_admit_exactly_one_writer(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    with migrated_clone.database.transaction() as tx:
        repo.create_page(tx, command=create_command())
    barrier = Barrier(2)
    commands = (
        edit_command(command_id=COMMAND_B),
        edit_command(
            command_id=COMMAND_C,
            content=b"synthetic-competing-revision-ciphertext",
        ),
    )

    def edit(database, command):
        try:
            with database.transaction() as tx:
                barrier.wait(timeout=30)
                return repo.edit_page(tx, command=command)
        except RepositoryConflictError:
            return None

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = tuple(workers.map(lambda command: edit(database, command), commands))
    accepted = tuple(result for result in results if result is not None)
    assert len(accepted) == 1
    with migrated_clone.database.transaction() as tx:
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) == accepted[0].page
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_revision")["count"] == 2
        assert tx.fetch_one(
            "SELECT count(*) AS count FROM atlas_page_command WHERE command='edit'"
        )["count"] == 1


def test_caller_rollback_removes_edit_head_revision_and_receipt_then_retry_succeeds(
    migrated_clone,
):
    repo = create_repository_catalog().atlas_pages
    command = edit_command()
    with migrated_clone.database.transaction() as tx:
        repo.create_page(tx, command=create_command())
    with (
        pytest.raises(RuntimeError, match="caller abort edit"),
        migrated_clone.database.transaction() as tx,
    ):
        repo.edit_page(tx, command=command)
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A).revision == 2
        raise RuntimeError("caller abort edit")
    with migrated_clone.database.transaction() as tx:
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A).revision == 1
        assert repo.list_revisions(tx, owner_id=OWNER, page_id=PAGE_A).records[0].revision == 1
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_command")["count"] == 1
    with migrated_clone.database.transaction() as tx:
        assert repo.edit_page(tx, command=command).page.revision == 2
    with migrated_clone.database.transaction() as tx:
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A).revision == 2


def test_postgresql_refuses_revision_mutation_and_unbacked_live_heads(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    with migrated_clone.database.transaction() as tx:
        repo.create_page(tx, command=create_command())
    with (
        check_violation(),
        migrated_clone.database.transaction() as tx,
    ):
        tx.execute(
            "UPDATE atlas_page_revision SET content=%s WHERE owner_id=%s AND page_id=%s",
            (b"mutated", OWNER, PAGE_A),
        )
    with (
        check_violation(),
        migrated_clone.database.transaction() as tx,
    ):
        tx.execute(
            "DELETE FROM atlas_page_revision WHERE owner_id=%s AND page_id=%s",
            (OWNER, PAGE_A),
        )
    with (
        check_violation(),
        migrated_clone.database.transaction() as tx,
    ):
        tx.execute(
            "UPDATE atlas_page SET revision=2,sources='[]'::jsonb,content=%s,"
            "content_sha256=%s,updated_at=20 WHERE owner_id=%s AND page_id=%s",
            (b"unbacked", hashlib.sha256(b"unbacked").hexdigest(), OWNER, PAGE_A),
        )
    with (
        pytest.raises(TransactionCommitOutcomeUnknownError) as error,
        migrated_clone.database.transaction() as tx,
    ):
        tx.execute(
            "INSERT INTO atlas_page(owner_id,page_id,project_id,slug,revision,sources,"
            "content,content_sha256,created_at,updated_at,deleted_at)"
            " VALUES(%s,%s,%s,%s,1,'[]'::jsonb,%s,%s,10,10,NULL)",
            (
                OWNER,
                PAGE_B,
                PROJECT,
                "unbacked-page",
                b"unbacked",
                hashlib.sha256(b"unbacked").hexdigest(),
            ),
        )
    if migrated_clone.dsn.startswith("sqlite:"):
        import sqlite3

        assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
        assert error.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY
        with migrated_clone.database.transaction() as tx:
            assert (
                tx.fetch_one(
                    "SELECT revision FROM atlas_page WHERE owner_id=%s AND page_id=%s",
                    (OWNER, PAGE_A),
                )["revision"]
                == 1
            )
            assert (
                tx.fetch_one(
                    "SELECT count(*) AS n FROM atlas_page WHERE owner_id=%s AND page_id=%s",
                    (OWNER, PAGE_B),
                )["n"]
                == 0
            )
        return
    assert isinstance(error.value.__cause__, psycopg2.errors.CheckViolation)
    with psycopg2.connect(migrated_clone.dsn) as connection, connection.cursor() as cursor:
        cursor.execute(f'SET search_path TO "{migrated_clone.schema}", pg_catalog')
        cursor.execute(
            "SELECT revision FROM atlas_page WHERE owner_id=%s AND page_id=%s",
            (OWNER, PAGE_A),
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "SELECT count(*) FROM atlas_page WHERE owner_id=%s AND page_id=%s",
            (OWNER, PAGE_B),
        )
        assert cursor.fetchone() == (0,)


@pytest.mark.postgresql_schema_catalog
def test_corrupt_revision_digest_fails_closed_on_revision_reads(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    with migrated_clone.database.transaction() as tx:
        repo.create_page(tx, command=create_command())
    with migrated_clone.database.transaction() as tx:
        tx.execute("ALTER TABLE atlas_page_revision DISABLE TRIGGER atlas_page_revision_immutable")
        tx.execute("ALTER TABLE atlas_page DISABLE TRIGGER atlas_page_head_revision")
        tx.execute(
            "UPDATE atlas_page_revision SET content_sha256=%s"
            " WHERE owner_id=%s AND page_id=%s AND revision=1",
            ("0" * 64, OWNER, PAGE_A),
        )
    with migrated_clone.database.transaction() as tx:
        with pytest.raises(RepositoryDataError, match="invalid stored atlas page revision"):
            repo.get_revision(tx, owner_id=OWNER, page_id=PAGE_A, revision=1)
        with pytest.raises(RepositoryDataError, match="invalid stored atlas page revision"):
            repo.list_revisions(tx, owner_id=OWNER, page_id=PAGE_A)


def test_revision_bounds_and_validation_remain_typed_inside_usable_transactions(
    migrated_clone,
):
    repo = create_repository_catalog().atlas_pages
    maximum_time = 2**53 - 1
    with migrated_clone.database.transaction() as tx:
        created = repo.create_page(tx, command=create_command(created_at=maximum_time))
        edited = repo.edit_page(
            tx,
            command=edit_command(updated_at=maximum_time),
        )
        assert created.page.created_at == maximum_time
        assert edited.page.updated_at == maximum_time
        with pytest.raises(RepositoryValidationError):
            edit_command(expected_revision=2**53 - 1)
        with pytest.raises(RepositoryValidationError):
            repo.list_revisions(tx, owner_id=OWNER, page_id=PAGE_A, limit=101)
        assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1

def test_public_atlas_revision_contract_behaviors():
    from test_assignments_postgres import parallel_transactions, standalone_database

    fixture = standalone_database()
    database = next(fixture)
    repo = create_repository_catalog().atlas_pages
    create = create_command(command_id=COMMAND_A)
    edits = (
        edit_command(command_id=COMMAND_B),
        edit_command(
            command_id=COMMAND_C,
            content=b"synthetic-public-competing-revision-ciphertext",
        ),
    )
    try:
        with database.transaction() as tx:
            repo.create_page(tx, command=create)
            assert repo.get_revision(tx, owner_id=FOREIGN, page_id=PAGE_A, revision=1) is None
            assert repo.list_revisions(tx, owner_id=FOREIGN, page_id=PAGE_A).records == ()
        results = parallel_transactions(
            database,
            tuple(
                lambda tx, command=command: repo.edit_page(tx, command=command)
                for command in edits
            ),
        )
        accepted = tuple(result for result in results if not isinstance(result, Exception))
        assert len(accepted) == 1
        with database.transaction() as tx:
            replay = repo.edit_page(
                tx,
                command=edits[0] if accepted[0].receipt.command_id == COMMAND_B else edits[1],
            )
            assert replay.replayed and replay.revision == accepted[0].revision
            assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) == accepted[0].page
            assert repo.get_revision(tx, owner_id=OWNER, page_id=PAGE_A, revision=1).content == (
                create.content
            )
            with pytest.raises(RepositoryConflictError, match="revision changed"):
                repo.edit_page(
                    tx,
                    command=edit_command(
                        command_id=COMMAND_D,
                        expected_revision=1,
                        updated_at=22,
                    ),
                )
            assert repo.list_revisions(
                tx, owner_id=OWNER, page_id=PAGE_A, limit=2
            ).next_cursor is None
    finally:
        fixture.close()
