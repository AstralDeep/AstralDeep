"""Exercises Atlas page ownership, replay, tombstones, keyset pagination, races, and caller
rollback against real PostgreSQL. The focused repository contract matrix reuses these scope,
replay, concurrency, and failure cases as executable evidence.
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
from astralplane.repositories.atlas_pages import (
    AtlasPageCreateCommand,
    AtlasPageCursor,
    AtlasPageDeleteCommand,
)
from tests.fixtures.database_errors import check_violation
from tests.integration.test_lifecycle_acceptance import concurrent_database

OWNER = "atlas-owner"
FOREIGN = "atlas-foreign-owner"
PROJECT = "atlas-project"
OTHER_PROJECT = "atlas-other-project"
PAGE_A = "10000000-0000-4000-8000-000000000001"
PAGE_B = "10000000-0000-4000-8000-000000000002"
PAGE_C = "10000000-0000-4000-8000-000000000003"
PAGE_D = "10000000-0000-4000-8000-000000000004"
COMMAND_A = "20000000-0000-4000-8000-000000000001"
COMMAND_B = "20000000-0000-4000-8000-000000000002"
COMMAND_C = "20000000-0000-4000-8000-000000000003"


def create_command(**changes):
    values = dict(
        owner_id=OWNER,
        command_id=str(uuid.uuid4()),
        page_id=PAGE_A,
        project_id=PROJECT,
        slug="kickoff",
        sources=("https://example.invalid/source",),
        content=b"synthetic-version-one-ciphertext",
        created_at=10,
    )
    values.update(changes)
    return AtlasPageCreateCommand(**values)


def delete_command(**changes):
    values = dict(
        owner_id=OWNER,
        command_id=str(uuid.uuid4()),
        page_id=PAGE_A,
        expected_revision=1,
        deleted_at=20,
    )
    values.update(changes)
    return AtlasPageDeleteCommand(**values)


def test_create_read_and_list_round_trip_owner_project_and_slug(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    project = create_command()
    global_page = create_command(
        page_id=PAGE_B, project_id=None, slug="global", command_id=COMMAND_B, created_at=11
    )
    with migrated_clone.database.transaction() as tx:
        created = repo.create_page(tx, command=project)
        assert not created.replayed and created.page.revision == 1
        assert created.page.content == project.content
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) == created.page
        assert (
            repo.get_page_by_slug(tx, owner_id=OWNER, project_id=PROJECT, slug="kickoff")
            == created.page
        )
        assert repo.get_page(tx, owner_id=FOREIGN, page_id=PAGE_A) is None
        assert (
            repo.get_page_by_slug(tx, owner_id=FOREIGN, project_id=PROJECT, slug="kickoff") is None
        )
        assert (
            repo.get_page_by_slug(tx, owner_id=OWNER, project_id=OTHER_PROJECT, slug="kickoff")
            is None
        )
        global_created = repo.create_page(tx, command=global_page)
        assert (
            repo.get_page_by_slug(tx, owner_id=OWNER, project_id=None, slug="global")
            == global_created.page
        )
        assert repo.list_pages(tx, owner_id=OWNER, project_id=PROJECT).records == (created.page,)
        assert repo.list_pages(tx, owner_id=OWNER, project_id=None).records == (
            global_created.page,
        )
        assert repo.list_pages(tx, owner_id=FOREIGN, project_id=PROJECT).records == ()
        stored = tx.fetch_one(
            "SELECT content, content_sha256, sources FROM atlas_page"
            " WHERE owner_id=%s AND page_id=%s",
            (OWNER, PAGE_A),
        )
        assert bytes(stored["content"]) == project.content
        assert stored["content_sha256"] == hashlib.sha256(project.content).hexdigest()
        assert stored["sources"] == ("https://example.invalid/source",)


def test_create_replay_is_exact_and_conflicting_identity_is_refused(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    command = create_command(command_id=COMMAND_A)
    with migrated_clone.database.transaction() as tx:
        original = repo.create_page(tx, command=command)
        replay = repo.create_page(tx, command=command)
        assert replay.replayed and replay.page == original.page
        assert replay.receipt == original.receipt
        with pytest.raises(RepositoryConflictError, match="identity was reused"):
            repo.create_page(tx, command=replace(command, content=b"changed-ciphertext"))
        with pytest.raises(RepositoryConflictError, match="identity already exists"):
            repo.create_page(tx, command=replace(command, command_id=COMMAND_B))
        with pytest.raises(RepositoryConflictError, match="slug is already used"):
            repo.create_page(
                tx,
                command=create_command(page_id=PAGE_B, command_id=COMMAND_C),
            )
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page")["count"] == 1
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_command")["count"] == 1
    with migrated_clone.database.transaction() as tx:
        assert repo.create_page(tx, command=command).page == original.page
    with migrated_clone.database.transaction() as tx:
        foreign = repo.create_page(
            tx,
            command=create_command(
                owner_id=FOREIGN, command_id=COMMAND_C, page_id=PAGE_A, slug="kickoff"
            ),
        )
        assert foreign.page.owner_id == FOREIGN
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A).owner_id == OWNER


def test_delete_erases_content_and_replay_cannot_restore_it(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    created_command = create_command(command_id=COMMAND_A)
    removal = delete_command(command_id=COMMAND_B)
    with migrated_clone.database.transaction() as tx:
        live = repo.create_page(tx, command=created_command)
        deleted = repo.delete_page(tx, command=removal)
        assert not deleted.replayed and deleted.page.deleted
        assert deleted.page.revision == live.page.revision + 1
        assert deleted.page.content is None and deleted.page.content_sha256 is None
        assert deleted.page.sources == ()
        assert deleted.page.deleted_at == deleted.page.updated_at == 20
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) is None
        assert repo.list_pages(tx, owner_id=OWNER, project_id=PROJECT).records == ()
        assert (
            repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A, include_deleted=True) == deleted.page
        )
        replay = repo.delete_page(tx, command=removal)
        assert replay.replayed and replay.page == deleted.page
        create_replay = repo.create_page(tx, command=created_command)
        assert create_replay.replayed and create_replay.page == deleted.page
        assert create_replay.page.content is None
        with pytest.raises(RepositoryConflictError, match="already deleted"):
            repo.delete_page(
                tx,
                command=delete_command(
                    command_id=COMMAND_C, expected_revision=deleted.page.revision
                ),
            )
        with pytest.raises(RepositoryConflictError, match="identity already exists"):
            repo.create_page(
                tx,
                command=create_command(command_id=COMMAND_C, page_id=PAGE_A, slug="replacement"),
            )
        stored = tx.fetch_one(
            "SELECT content, content_sha256, sources, revision, deleted_at"
            " FROM atlas_page WHERE owner_id=%s AND page_id=%s",
            (OWNER, PAGE_A),
        )
        assert stored == {
            "content": None,
            "content_sha256": None,
            "sources": (),
            "revision": 2,
            "deleted_at": 20,
        }
    with migrated_clone.database.transaction() as tx:
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) is None
        assert (
            repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A, include_deleted=True) == deleted.page
        )


def test_delete_fences_revision_time_and_missing_owner(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    created = create_command(command_id=COMMAND_A)
    with migrated_clone.database.transaction() as tx:
        repo.create_page(tx, command=created)
        with pytest.raises(RepositoryConflictError, match="revision changed"):
            repo.delete_page(
                tx,
                command=delete_command(command_id=COMMAND_B, expected_revision=2),
            )
        with pytest.raises(RepositoryConflictError, match="clock regressed"):
            repo.delete_page(
                tx,
                command=delete_command(command_id=COMMAND_B, expected_revision=1, deleted_at=9),
            )
        with pytest.raises(RepositoryNotFoundError):
            repo.delete_page(
                tx,
                command=delete_command(command_id=COMMAND_B, page_id=PAGE_B, expected_revision=1),
            )
        with pytest.raises(RepositoryNotFoundError):
            repo.delete_page(
                tx,
                command=delete_command(owner_id=FOREIGN, command_id=COMMAND_B, expected_revision=1),
            )
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A).revision == 1


def test_list_pages_is_bounded_ordered_and_scope_bound(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    pages = (
        create_command(page_id=PAGE_A, command_id=COMMAND_A, slug="alpha", created_at=10),
        create_command(page_id=PAGE_B, command_id=COMMAND_B, slug="beta", created_at=10),
        create_command(page_id=PAGE_C, command_id=COMMAND_C, slug="gamma", created_at=11),
    )
    with migrated_clone.database.transaction() as tx:
        created = tuple(repo.create_page(tx, command=page).page for page in pages)
        repo.create_page(
            tx,
            command=create_command(
                page_id=PAGE_D,
                command_id=str(uuid.uuid4()),
                project_id=OTHER_PROJECT,
                slug="other",
            ),
        )
        foreign_page = repo.create_page(
            tx,
            command=create_command(
                owner_id=FOREIGN,
                page_id=PAGE_D,
                command_id=str(uuid.uuid4()),
                slug="foreign",
            ),
        ).page
        first = repo.list_pages(tx, owner_id=OWNER, project_id=PROJECT, limit=2)
        assert [record.created_at for record in first.records] == [11, 10]
        assert first.records[0] == created[2]
        assert first.next_cursor is not None
        second = repo.list_pages(
            tx,
            owner_id=OWNER,
            project_id=PROJECT,
            cursor=first.next_cursor,
            limit=2,
        )
        assert second.next_cursor is None
        combined = first.records + second.records
        assert {record.page_id for record in combined} == {page.page_id for page in created}
        assert len(combined) == 3
        exact = repo.list_pages(tx, owner_id=OWNER, project_id=PROJECT, limit=3)
        assert len(exact.records) == 3 and exact.next_cursor is None
        assert foreign_page in repo.list_pages(tx, owner_id=FOREIGN, project_id=PROJECT).records
        for cursor in (
            AtlasPageCursor("foreign", PROJECT, first.next_cursor.created_at, PAGE_A),
            AtlasPageCursor(OWNER, OTHER_PROJECT, first.next_cursor.created_at, PAGE_A),
        ):
            with pytest.raises(RepositoryValidationError):
                repo.list_pages(tx, owner_id=OWNER, project_id=PROJECT, cursor=cursor)
    with migrated_clone.database.transaction() as tx:
        repo.delete_page(
            tx,
            command=delete_command(
                command_id=str(uuid.uuid4()), page_id=PAGE_C, expected_revision=1
            ),
        )
        remaining = repo.list_pages(tx, owner_id=OWNER, project_id=PROJECT, limit=3)
        assert {record.page_id for record in remaining.records} == {PAGE_A, PAGE_B}


def test_tombstone_and_receipt_mutations_are_refused_by_postgresql(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    with migrated_clone.database.transaction() as tx:
        repo.create_page(tx, command=create_command(command_id=COMMAND_A))
        repo.delete_page(tx, command=delete_command(command_id=COMMAND_B))
    with (
        check_violation(),
        migrated_clone.database.transaction() as tx,
    ):
        tx.execute(
            "UPDATE atlas_page SET content=%s, content_sha256=%s, sources='[]'::jsonb"
            " WHERE owner_id=%s AND page_id=%s",
            (b"restored", hashlib.sha256(b"restored").hexdigest(), OWNER, PAGE_A),
        )
    with (
        check_violation(),
        migrated_clone.database.transaction() as tx,
    ):
        tx.execute(
            "DELETE FROM atlas_page_command WHERE owner_id=%s AND command_id=%s",
            (OWNER, COMMAND_A),
        )
    with migrated_clone.database.transaction() as tx:
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) is None
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_command")["count"] == 2


@pytest.mark.postgresql_schema_catalog
def test_corrupt_content_digest_fails_closed_on_read(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    with migrated_clone.database.transaction() as tx:
        tx.execute("ALTER TABLE atlas_page DISABLE TRIGGER atlas_page_lifecycle")
        tx.execute(
            "INSERT INTO atlas_page(owner_id,page_id,project_id,slug,revision,sources,"
            "content,content_sha256,created_at,updated_at,deleted_at)"
            " VALUES(%s,%s,%s,%s,1,'[]'::jsonb,%s,%s,10,10,NULL)",
            (OWNER, PAGE_B, PROJECT, "corrupt", b"tampered", "0" * 64),
        )
        tx.execute(
            "INSERT INTO atlas_page_revision(owner_id,page_id,revision,sources,content,"
            "content_sha256,created_at) VALUES(%s,%s,1,'[]'::jsonb,%s,%s,10)",
            (OWNER, PAGE_B, b"tampered", "0" * 64),
        )
    with migrated_clone.database.transaction() as tx:
        with pytest.raises(RepositoryDataError, match="invalid stored atlas page"):
            repo.get_page(tx, owner_id=OWNER, page_id=PAGE_B)
        with pytest.raises(RepositoryDataError, match="invalid stored atlas page"):
            repo.list_pages(tx, owner_id=OWNER, project_id=PROJECT)


def test_caller_rollback_removes_create_and_delete_writes(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    with (
        pytest.raises(RuntimeError, match="caller abort create"),
        migrated_clone.database.transaction() as tx,
    ):
        repo.create_page(tx, command=create_command(command_id=COMMAND_A))
        raise RuntimeError("caller abort create")
    with migrated_clone.database.transaction() as tx:
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) is None
    with migrated_clone.database.transaction() as tx:
        repo.create_page(tx, command=create_command(command_id=COMMAND_A))
    with (
        pytest.raises(RuntimeError, match="caller abort delete"),
        migrated_clone.database.transaction() as tx,
    ):
        repo.delete_page(tx, command=delete_command(command_id=COMMAND_B))
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) is None
        raise RuntimeError("caller abort delete")
    with migrated_clone.database.transaction() as tx:
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A).content is not None
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_command")["count"] == 1


def test_concurrent_create_admits_exactly_one_writer(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    barrier = Barrier(2)
    commands = (
        create_command(command_id=COMMAND_A),
        create_command(command_id=COMMAND_B),
    )

    def create(database, command):
        try:
            with database.transaction() as tx:
                barrier.wait(timeout=30)
                return repo.create_page(tx, command=command)
        except RepositoryConflictError:
            return None

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = tuple(workers.map(lambda command: create(database, command), commands))
    accepted = tuple(result for result in results if result is not None)
    assert len(accepted) == 1
    with migrated_clone.database.transaction() as tx:
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) == accepted[0].page
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page")["count"] == 1
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_command")["count"] == 1


def test_public_atlas_page_contract_behaviors():
    from test_assignments_postgres import parallel_transactions, standalone_database

    fixture = standalone_database()
    database = next(fixture)
    repo = create_repository_catalog().atlas_pages
    first_command = create_command(command_id=COMMAND_A)
    second_command = create_command(command_id=COMMAND_B)
    try:
        with database.transaction() as tx:
            created = repo.create_page(tx, command=first_command)
            assert repo.get_page(tx, owner_id=FOREIGN, page_id=PAGE_A) is None
            assert repo.list_pages(tx, owner_id=FOREIGN, project_id=PROJECT).records == ()
            replay = repo.create_page(tx, command=first_command)
            assert replay.replayed and replay.page == created.page
            with pytest.raises(RepositoryConflictError):
                repo.create_page(
                    tx, command=replace(first_command, content=b"conflicting-ciphertext")
                )
            with pytest.raises(RepositoryConflictError):
                repo.delete_page(
                    tx,
                    command=delete_command(command_id=COMMAND_C, expected_revision=2),
                )
        results = parallel_transactions(
            database,
            (
                lambda tx: repo.create_page(tx, command=first_command),
                lambda tx: repo.create_page(tx, command=second_command),
            ),
        )
        assert sum(not isinstance(result, RepositoryConflictError) for result in results) == 1
        with database.transaction() as tx:
            deleted = repo.delete_page(tx, command=delete_command(command_id=COMMAND_C))
            assert deleted.page.deleted and deleted.page.content is None
            assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) is None
            with pytest.raises(RepositoryNotFoundError):
                repo.delete_page(
                    tx,
                    command=delete_command(command_id=str(uuid.uuid4()), page_id=PAGE_B),
                )
    finally:
        fixture.close()


@pytest.mark.parametrize("character", ["x", "é", "😀", '"', "\\", "\n", "\t"])
def test_advertised_source_maxima_are_insertable_and_rollback(migrated_clone, character):
    repo = create_repository_catalog().atlas_pages
    sources = tuple("x" + character * 510 + chr(0x1F600 + index) for index in range(8))
    command = create_command(sources=sources)
    with (
        pytest.raises(RuntimeError, match="caller rollback"),
        migrated_clone.database.transaction() as tx,
    ):
        created = repo.create_page(tx, command=command)
        assert created.page.sources == sources
        assert repo.create_page(tx, command=command).replayed
        stored_size = tx.fetch_one("SELECT octet_length(sources::text) AS size FROM atlas_page")[
            "size"
        ]
        assert stored_size <= 16416
        assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1
        raise RuntimeError("caller rollback")
    with migrated_clone.database.transaction() as tx:
        assert repo.get_page(tx, owner_id=OWNER, page_id=PAGE_A) is None
        assert tx.fetch_all("SELECT * FROM atlas_page_command") == ()


def test_four_byte_source_maximum_matches_database_bound(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    sources = tuple(chr(0x1F600 + index) * 512 for index in range(8))
    with migrated_clone.database.transaction() as tx:
        assert repo.create_page(tx, command=create_command(sources=sources)).page.sources == sources
        assert (
            tx.fetch_one("SELECT octet_length(sources::text) AS size FROM atlas_page")["size"]
            == (16409 if tx.dialect.is_sqlite else 16416)
        )


def test_safe_integer_timestamp_maximum_create_delete_and_replay(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    command = create_command(created_at=2**53 - 1)
    removal = delete_command(deleted_at=2**53 - 1)
    with migrated_clone.database.transaction() as tx:
        created = repo.create_page(tx, command=command)
        assert created.receipt.created_at == 2**53 - 1
        deleted = repo.delete_page(tx, command=removal)
        assert deleted.page.deleted_at == 2**53 - 1
        assert repo.delete_page(tx, command=removal).replayed
        assert repo.create_page(tx, command=command).replayed


@pytest.mark.parametrize(
    "changes",
    [
        {"created_at": 2**53},
        {"sources": tuple(str(i) for i in range(9))},
        {"sources": ("x" * 513,)},
    ],
)
def test_out_of_contract_create_is_typed_and_transaction_remains_usable(migrated_clone, changes):
    with migrated_clone.database.transaction() as tx:
        with pytest.raises(RepositoryValidationError):
            create_command(**changes)
        assert tx.fetch_one("SELECT 1 AS usable")["usable"] == 1
        assert tx.fetch_all("SELECT * FROM atlas_page") == ()
