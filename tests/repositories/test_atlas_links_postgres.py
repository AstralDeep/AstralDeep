"""Exercises Atlas link replay, revision fences, same-owner endpoints, bounded backlinks,
tombstone cleanup, races, and caller rollback against real PostgreSQL. The focused repository
contract matrix reuses the public behavior case as executable evidence.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from astralplane.api import create_repository_catalog
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.atlas_links import MAX_LINK_TARGETS, AtlasLinkSetCommand
from astralplane.repositories.atlas_pages import (
    AtlasPageCreateCommand,
    AtlasPageDeleteCommand,
)
from tests.fixtures.database_errors import check_violation
from tests.integration.test_lifecycle_acceptance import concurrent_database

OWNER = "atlas-link-owner"
FOREIGN = "atlas-link-foreign"
PROJECT = "atlas-link-project"
OTHER_PROJECT = "atlas-link-other-project"
PAGE_A = "10000000-0000-4000-8000-000000000101"
PAGE_B = "10000000-0000-4000-8000-000000000102"
PAGE_C = "10000000-0000-4000-8000-000000000103"
PAGE_D = "10000000-0000-4000-8000-000000000104"
PAGE_E = "10000000-0000-4000-8000-000000000105"


def page_command(page_id, slug, **changes):
    values = dict(
        owner_id=OWNER,
        command_id=str(uuid.uuid4()),
        page_id=page_id,
        project_id=PROJECT,
        slug=slug,
        sources=(),
        content=b"synthetic-atlas-link-ciphertext",
        created_at=10,
    )
    values.update(changes)
    return AtlasPageCreateCommand(**values)


def link_command(**changes):
    values = dict(
        owner_id=OWNER,
        command_id=str(uuid.uuid4()),
        page_id=PAGE_A,
        expected_page_revision=1,
        expected_link_revision=0,
        targets=(PAGE_B,),
        updated_at=20,
    )
    values.update(changes)
    return AtlasLinkSetCommand(**values)


def delete_command(page_id=PAGE_B, **changes):
    values = dict(
        owner_id=OWNER,
        command_id=str(uuid.uuid4()),
        page_id=page_id,
        expected_revision=1,
        deleted_at=30,
    )
    values.update(changes)
    return AtlasPageDeleteCommand(**values)


def create_page(transaction, pages, page_id, slug, **changes):
    return pages.create_page(
        transaction, command=page_command(page_id, slug, **changes)
    ).page


def test_set_and_read_links_round_trip_same_owner_across_projects(migrated_clone):
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    with migrated_clone.database.transaction() as tx:
        source = create_page(tx, pages, PAGE_A, "source")
        target = create_page(tx, pages, PAGE_B, "target")
        other_project = create_page(
            tx, pages, PAGE_C, "other-project", project_id=OTHER_PROJECT
        )
        changed = links.set_links(
            tx, command=link_command(targets=(PAGE_B, PAGE_C))
        )
        assert not changed.replayed
        assert changed.receipt.result_link_revision == 1
        assert [link.target_page_id for link in changed.links] == [PAGE_C, PAGE_B]
        assert {link.source_revision for link in changed.links} == {source.revision}
        assert {link.target_revision for link in changed.links} == {
            target.revision,
            other_project.revision,
        }
        outbound = links.list_links(tx, owner_id=OWNER, page_id=PAGE_A)
        assert outbound.records == changed.links
        assert outbound.next_cursor is None
        backlinks = links.list_backlinks(tx, owner_id=OWNER, page_id=PAGE_B)
        assert tuple(link.source_page_id for link in backlinks.records) == (PAGE_A,)
        assert links.list_links(tx, owner_id=OWNER, page_id=PAGE_B).records == ()
        foreign_source = create_page(
            tx,
            pages,
            PAGE_D,
            "foreign-source",
            owner_id=FOREIGN,
            project_id=PROJECT,
        )
        foreign_target = create_page(
            tx,
            pages,
            PAGE_B,
            "foreign-target",
            owner_id=FOREIGN,
            project_id=PROJECT,
        )
        links.set_links(
            tx,
            command=link_command(
                owner_id=FOREIGN,
                page_id=foreign_source.page_id,
                targets=(foreign_target.page_id,),
            ),
        )
        owner_backlinks = links.list_backlinks(tx, owner_id=OWNER, page_id=PAGE_B)
        assert tuple(link.source_page_id for link in owner_backlinks.records) == (PAGE_A,)
        with pytest.raises(RepositoryNotFoundError):
            links.list_backlinks(tx, owner_id=OWNER, page_id=PAGE_E)


def test_link_replacement_is_replay_safe_and_revision_fenced(migrated_clone):
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    with migrated_clone.database.transaction() as tx:
        for page_id, slug in ((PAGE_A, "a"), (PAGE_B, "b"), (PAGE_C, "c"), (PAGE_D, "d")):
            create_page(tx, pages, page_id, slug)
        first_command = link_command(targets=(PAGE_B,), updated_at=20)
        first = links.set_links(tx, command=first_command)
        replay = links.set_links(tx, command=first_command)
        assert replay.replayed and replay.receipt == first.receipt
        assert replay.links == first.links
        with pytest.raises(RepositoryConflictError, match="identity was reused"):
            links.set_links(tx, command=replace(first_command, targets=(PAGE_C,)))
        with pytest.raises(RepositoryConflictError, match="page revision changed"):
            links.set_links(
                tx,
                command=link_command(
                    expected_page_revision=2,
                    expected_link_revision=1,
                    targets=(PAGE_C,),
                    updated_at=21,
                ),
            )
        with pytest.raises(RepositoryConflictError, match="link revision changed"):
            links.set_links(
                tx,
                command=link_command(
                    expected_link_revision=2,
                    targets=(PAGE_C,),
                    updated_at=21,
                ),
            )
        with pytest.raises(RepositoryConflictError, match="clock regressed"):
            links.set_links(
                tx,
                command=link_command(
                    expected_link_revision=1,
                    targets=(PAGE_C,),
                    updated_at=19,
                ),
            )
        replaced = links.set_links(
            tx,
            command=link_command(
                expected_link_revision=1,
                targets=(PAGE_C, PAGE_D),
                updated_at=25,
            ),
        )
        assert replaced.receipt.result_link_revision == 2
        assert [link.target_page_id for link in replaced.links] == [PAGE_D, PAGE_C]
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link")["count"] == 2
        cleared = links.set_links(
            tx,
            command=link_command(
                expected_link_revision=2,
                targets=(),
                updated_at=30,
            ),
        )
        assert cleared.links == ()
        assert links.list_links(tx, owner_id=OWNER, page_id=PAGE_A).records == ()
        assert (
            tx.fetch_one("SELECT link_revision FROM atlas_page_link_head")["link_revision"]
            == 3
        )


def test_cross_owner_stale_and_nonexistent_endpoints_are_refused(migrated_clone):
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    with migrated_clone.database.transaction() as tx:
        create_page(tx, pages, PAGE_A, "a")
        create_page(tx, pages, PAGE_B, "b")
        create_page(tx, pages, PAGE_E, "foreign", owner_id=FOREIGN)
        for command in (
            link_command(targets=(PAGE_E,)),
            link_command(targets=("10000000-0000-4000-8000-000000000199",)),
        ):
            with pytest.raises(RepositoryNotFoundError, match="endpoint"):
                links.set_links(tx, command=command)
        with pytest.raises(RepositoryValidationError):
            link_command(targets=(PAGE_A,))
        with pytest.raises(RepositoryValidationError):
            link_command(targets=(PAGE_B, PAGE_B))
        with pytest.raises(RepositoryValidationError):
            link_command(
                targets=tuple(
                    f"10000000-0000-4000-8000-{index:012d}"
                    for index in range(MAX_LINK_TARGETS + 1)
                )
            )
        with pytest.raises(RepositoryNotFoundError):
            links.set_links(
                tx, command=link_command(page_id="10000000-0000-4000-8000-000000000198")
            )
        with pytest.raises(RepositoryNotFoundError):
            links.list_links(tx, owner_id=OWNER, page_id=PAGE_D)
        with pytest.raises(RepositoryNotFoundError):
            links.list_backlinks(tx, owner_id=OWNER, page_id=PAGE_D)
        changed = links.set_links(tx, command=link_command())
        assert changed.receipt.result_link_revision == 1
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link")["count"] == 1


def test_backlinks_are_bounded_ordered_and_cursor_scoped(migrated_clone):
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    sources = tuple(f"20000000-0000-4000-8000-{index:012d}" for index in range(1, 6))
    with migrated_clone.database.transaction() as tx:
        create_page(tx, pages, PAGE_A, "target")
        for index, source in enumerate(sources, start=1):
            create_page(tx, pages, source, f"source-{index}")
            links.set_links(
                tx,
                command=link_command(
                    page_id=source,
                    targets=(PAGE_A,),
                    updated_at=20 + index,
                ),
            )
        first = links.list_backlinks(tx, owner_id=OWNER, page_id=PAGE_A, limit=2)
        assert len(first.records) == 2 and first.next_cursor is not None
        second = links.list_backlinks(
            tx,
            owner_id=OWNER,
            page_id=PAGE_A,
            cursor=first.next_cursor,
            limit=2,
        )
        assert len(second.records) == 2 and second.next_cursor is not None
        third = links.list_backlinks(
            tx,
            owner_id=OWNER,
            page_id=PAGE_A,
            cursor=second.next_cursor,
            limit=2,
        )
        assert len(third.records) == 1 and third.next_cursor is None
        combined = first.records + second.records + third.records
        assert [record.linked_at for record in combined] == [25, 24, 23, 22, 21]
        assert {record.source_page_id for record in combined} == set(sources)
        exact = links.list_backlinks(tx, owner_id=OWNER, page_id=PAGE_A, limit=5)
        assert len(exact.records) == 5 and exact.next_cursor is None
        cursor = first.next_cursor
        with pytest.raises(RepositoryValidationError):
            links.list_links(
                tx, owner_id=OWNER, page_id=PAGE_A, cursor=cursor
            )
        with pytest.raises(RepositoryValidationError):
            links.list_backlinks(
                tx, owner_id=OWNER, page_id=sources[0], cursor=cursor
            )
        with pytest.raises(RepositoryValidationError):
            links.list_backlinks(
                tx, owner_id=FOREIGN, page_id=PAGE_A, cursor=cursor
            )
        with pytest.raises(RepositoryValidationError):
            links.list_backlinks(tx, owner_id=OWNER, page_id=PAGE_A, limit=101)


def test_page_tombstone_removes_links_and_invalidates_stale_link_revisions(migrated_clone):
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    with migrated_clone.database.transaction() as tx:
        create_page(tx, pages, PAGE_A, "a")
        create_page(tx, pages, PAGE_B, "b")
        create_page(tx, pages, PAGE_C, "c")
        links.set_links(tx, command=link_command(targets=(PAGE_B,), updated_at=20))
        b_command = link_command(
            page_id=PAGE_B,
            expected_link_revision=0,
            targets=(PAGE_C,),
            updated_at=21,
        )
        links.set_links(tx, command=b_command)
        deleted = pages.delete_page(tx, command=delete_command(page_id=PAGE_B))
        assert deleted.page.deleted
        assert links.list_links(tx, owner_id=OWNER, page_id=PAGE_A).records == ()
        assert (
            tx.fetch_one(
                "SELECT link_revision FROM atlas_page_link_head"
                " WHERE owner_id=%s AND page_id=%s",
                (OWNER, PAGE_A),
            )["link_revision"]
            == 2
        )
        assert (
            tx.fetch_one(
                "SELECT count(*) AS count FROM atlas_page_link_head"
                " WHERE owner_id=%s AND page_id=%s",
                (OWNER, PAGE_B),
            )["count"]
            == 0
        )
        with pytest.raises(RepositoryConflictError, match="link revision changed"):
            links.set_links(
                tx,
                command=link_command(
                    expected_link_revision=1,
                    targets=(PAGE_C,),
                    updated_at=40,
                ),
            )
        relinked = links.set_links(
            tx,
            command=link_command(
                expected_link_revision=2,
                targets=(PAGE_C,),
                updated_at=41,
            ),
        )
        assert relinked.receipt.result_link_revision == 3
        with pytest.raises(RepositoryConflictError, match="deleted"):
            links.set_links(
                tx,
                command=link_command(
                    page_id=PAGE_B,
                    expected_link_revision=0,
                    targets=(PAGE_C,),
                    updated_at=42,
                ),
            )
        replay_of_deleted_source = links.set_links(tx, command=b_command)
        assert replay_of_deleted_source.replayed
        assert replay_of_deleted_source.links == ()
        pages.delete_page(tx, command=delete_command(page_id=PAGE_A, deleted_at=50))
        with pytest.raises(RepositoryNotFoundError):
            links.list_links(tx, owner_id=OWNER, page_id=PAGE_A)
        with pytest.raises(RepositoryNotFoundError):
            links.list_backlinks(tx, owner_id=OWNER, page_id=PAGE_A)
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link")["count"] == 0
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link_head")["count"] == 0
        assert (
            tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link_command")["count"] == 3
        )


def test_sixteen_targets_fit_and_seventeen_are_rejected(migrated_clone):
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    targets = tuple(f"30000000-0000-4000-8000-{index:012d}" for index in range(1, 18))
    with migrated_clone.database.transaction() as tx:
        create_page(tx, pages, PAGE_A, "source")
        for index, target in enumerate(targets, start=1):
            create_page(tx, pages, target, f"target-{index}")
        bounded = links.set_links(
            tx,
            command=link_command(
                targets=targets[:MAX_LINK_TARGETS],
                updated_at=2**53 - 1,
            ),
        )
        assert len(bounded.links) == MAX_LINK_TARGETS
        assert len(links.list_links(tx, owner_id=OWNER, page_id=PAGE_A, limit=100).records) == (
            MAX_LINK_TARGETS
        )
        with pytest.raises(RepositoryValidationError):
            link_command(
                expected_link_revision=1,
                targets=targets,
                updated_at=2**53 - 1,
            )
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link")["count"] == (
            MAX_LINK_TARGETS
        )


def test_database_triggers_refuse_link_mutation_and_command_deletion(migrated_clone):
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    with migrated_clone.database.transaction() as tx:
        create_page(tx, pages, PAGE_A, "a")
        create_page(tx, pages, PAGE_B, "b")
        command = link_command()
        links.set_links(tx, command=command)
    with (
        check_violation(),
        migrated_clone.database.transaction() as failing,
    ):
        failing.execute(
            "UPDATE atlas_page_link SET link_revision=link_revision+1"
            " WHERE owner_id=%s AND source_page_id=%s",
            (OWNER, PAGE_A),
        )
    with (
        check_violation(),
        migrated_clone.database.transaction() as failing,
    ):
        failing.execute(
            "UPDATE atlas_page_link_head SET link_revision=link_revision+2"
            " WHERE owner_id=%s AND page_id=%s",
            (OWNER, PAGE_A),
        )
    with (
        check_violation(),
        migrated_clone.database.transaction() as failing,
    ):
        failing.execute(
            "DELETE FROM atlas_page_link_command WHERE owner_id=%s AND command_id=%s",
            (OWNER, command.command_id),
        )
    with migrated_clone.database.transaction() as tx:
        assert len(links.list_links(tx, owner_id=OWNER, page_id=PAGE_A).records) == 1


def test_concurrent_link_sets_admit_exactly_one_fenced_writer(migrated_clone):
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    with migrated_clone.database.transaction() as tx:
        create_page(tx, pages, PAGE_A, "a")
        create_page(tx, pages, PAGE_B, "b")
        create_page(tx, pages, PAGE_C, "c")
    barrier = Barrier(2)
    commands = (
        link_command(targets=(PAGE_B,)),
        link_command(targets=(PAGE_C,)),
    )

    def apply(database, command):
        try:
            with database.transaction() as tx:
                barrier.wait(timeout=30)
                return links.set_links(tx, command=command)
        except RepositoryConflictError:
            return None

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = tuple(workers.map(lambda command: apply(database, command), commands))
    accepted = tuple(result for result in results if result is not None)
    assert len(accepted) == 1
    with migrated_clone.database.transaction() as tx:
        stored = links.list_links(tx, owner_id=OWNER, page_id=PAGE_A)
        assert len(stored.records) == 1
        assert stored.records == accepted[0].links
        assert (
            tx.fetch_one("SELECT link_revision FROM atlas_page_link_head")["link_revision"]
            == 1
        )


def test_concurrent_delete_and_link_set_leave_no_dangling_links(migrated_clone):
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    with migrated_clone.database.transaction() as tx:
        create_page(tx, pages, PAGE_A, "a")
        create_page(tx, pages, PAGE_B, "b")
    barrier = Barrier(2)

    def link_or_conflict(database):
        try:
            with database.transaction() as tx:
                barrier.wait(timeout=30)
                return links.set_links(tx, command=link_command(targets=(PAGE_B,)))
        except RepositoryConflictError:
            return None

    def delete_or_conflict(database):
        try:
            with database.transaction() as tx:
                barrier.wait(timeout=30)
                return pages.delete_page(tx, command=delete_command(page_id=PAGE_A))
        except RepositoryConflictError:
            return None

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        outcomes = tuple(
            workers.map(
                lambda operation: operation(database),
                (link_or_conflict, delete_or_conflict),
            )
        )
    assert any(outcome is not None for outcome in outcomes)
    with migrated_clone.database.transaction() as tx:
        assert pages.get_page(tx, owner_id=OWNER, page_id=PAGE_A) is None
        assert links.list_backlinks(tx, owner_id=OWNER, page_id=PAGE_B).records == ()
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link")["count"] == 0
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link_head")["count"] == 0


def test_caller_rollback_removes_link_writes_and_restores_page_deletion(migrated_clone):
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    with migrated_clone.database.transaction() as tx:
        create_page(tx, pages, PAGE_A, "a")
        create_page(tx, pages, PAGE_B, "b")
    with (
        pytest.raises(RuntimeError, match="caller abort links"),
        migrated_clone.database.transaction() as tx,
    ):
        links.set_links(tx, command=link_command())
        raise RuntimeError("caller abort links")
    with migrated_clone.database.transaction() as tx:
        assert links.list_links(tx, owner_id=OWNER, page_id=PAGE_A).records == ()
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link")["count"] == 0
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link_head")["count"] == 0
        assert (
            tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link_command")["count"] == 0
        )
        links.set_links(tx, command=link_command())
    with (
        pytest.raises(RuntimeError, match="caller abort delete"),
        migrated_clone.database.transaction() as tx,
    ):
        pages.delete_page(tx, command=delete_command(page_id=PAGE_A, deleted_at=30))
        assert pages.get_page(
            tx, owner_id=OWNER, page_id=PAGE_A, include_deleted=True
        ).deleted
        raise RuntimeError("caller abort delete")
    with migrated_clone.database.transaction() as tx:
        assert pages.get_page(tx, owner_id=OWNER, page_id=PAGE_A) is not None
        assert len(links.list_links(tx, owner_id=OWNER, page_id=PAGE_A).records) == 1
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link_head")["count"] == 1


def test_public_atlas_link_contract_behaviors():
    from test_assignments_postgres import parallel_transactions, standalone_database

    fixture = standalone_database()
    database = next(fixture)
    catalog = create_repository_catalog()
    links = catalog.atlas_links
    pages = catalog.atlas_pages
    first_command = link_command()
    try:
        with database.transaction() as tx:
            create_page(tx, pages, PAGE_A, "a")
            create_page(tx, pages, PAGE_B, "b")
            create_page(tx, pages, PAGE_C, "c")
            changed = links.set_links(tx, command=first_command)
            with pytest.raises(RepositoryNotFoundError):
                links.list_links(tx, owner_id=FOREIGN, page_id=PAGE_A)
            assert links.set_links(tx, command=first_command).replayed
            with pytest.raises(RepositoryConflictError):
                links.set_links(tx, command=replace(first_command, targets=(PAGE_C,)))
            with pytest.raises(RepositoryConflictError):
                links.set_links(
                    tx,
                    command=link_command(
                        expected_link_revision=2,
                        targets=(PAGE_C,),
                        updated_at=21,
                    ),
                )
            assert changed.links == links.list_links(
                tx, owner_id=OWNER, page_id=PAGE_A
            ).records
        results = parallel_transactions(
            database,
            (
                lambda tx: links.set_links(
                    tx,
                    command=link_command(
                        expected_link_revision=1,
                        targets=(PAGE_B,),
                        updated_at=25,
                    ),
                ),
                lambda tx: links.set_links(
                    tx,
                    command=link_command(
                        expected_link_revision=1,
                        targets=(PAGE_C,),
                        updated_at=25,
                    ),
                ),
            ),
        )
        assert sum(not isinstance(result, RepositoryConflictError) for result in results) == 1
        with database.transaction() as tx:
            pages.delete_page(tx, command=delete_command(page_id=PAGE_A, deleted_at=30))
            with pytest.raises(RepositoryNotFoundError):
                links.list_links(tx, owner_id=OWNER, page_id=PAGE_A)
            with pytest.raises(RepositoryNotFoundError):
                links.list_backlinks(tx, owner_id=OWNER, page_id=PAGE_A)
            assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link")["count"] == 0
    finally:
        fixture.close()