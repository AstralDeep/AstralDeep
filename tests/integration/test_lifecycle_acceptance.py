"""Exercises owner isolation, generation fences, caller rollback, and share revocation
through real Plane repositories and PostgreSQL transactions. The focused fault gate
runs these same acceptance assertions against deliberately broken guards.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Barrier, Event
from time import monotonic

import pytest
from psycopg2.extensions import make_dsn

from astralplane.api import create_repository_catalog
from astralplane.database.pool import ConnectionPool
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.transaction import PlaneDatabase
from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
from astralplane.repositories.share_grants import ShareGrantRevocationState

OWNER = "acceptance-owner"
FOREIGN = "acceptance-foreign"
DRAFT = "10000000-0000-4000-8000-000000000001"
CLAIMS = (
    "10000000-0000-4000-8000-000000000002",
    "10000000-0000-4000-8000-000000000003",
)
DIGEST = "a" * 64
NOW = datetime(2026, 10, 12, tzinfo=UTC)


@contextmanager
def concurrent_database(clone):
    if clone.dsn.startswith("sqlite:"):
        from astralplane.database.sqlite import create_sqlite_driver_pool

        pool = ConnectionPool(create_sqlite_driver_pool(clone.dsn))
        try:
            yield PlaneDatabase(pool)
        finally:
            pool.close()
        return
    configured = make_dsn(clone.dsn, options=f"-csearch_path={clone.schema},pg_catalog")
    pool = ConnectionPool(
        create_postgres_driver_pool(configured, minimum_connections=1, maximum_connections=2)
    )
    try:
        yield PlaneDatabase(pool)
    finally:
        pool.close()


def draft(tx):
    return create_repository_catalog().draft_agents.create_draft(
        tx,
        draft_id=DRAFT,
        draft_uuid=DRAFT,
        target_agent_id="acceptance-agent",
        owner_id=OWNER,
        agent_name="Synthetic acceptance",
        agent_slug="synthetic-acceptance",
        description="original",
        observed_at=1,
    )


def grant(tx):
    return create_repository_catalog().share_grants.create_grant(
        tx,
        token_sha256=DIGEST,
        owner_id=OWNER,
        chat_id="acceptance-chat",
        scope="chat",
        component_id=None,
        snapshot_html="<p>Synthetic</p>",
        snapshot_json={"type": "text", "value": "Synthetic"},
        expires_at=None,
    )


def wait_for_blocked_statement(database, waiting_pid, blocking_pid):
    deadline = monotonic() + 30
    with database.transaction() as observer:
        while monotonic() < deadline:
            observer.fetch_one("SELECT pg_stat_clear_snapshot()")
            row = observer.fetch_one(
                "SELECT wait_event_type, pg_blocking_pids(pid) AS blockers "
                "FROM pg_stat_activity WHERE pid=%s",
                (waiting_pid,),
            )
            if (
                row is not None
                and row["wait_event_type"] == "Lock"
                and blocking_pid in row["blockers"]
            ):
                return
    pytest.fail("competing SQL did not block on the exact held row transaction")


def test_owner_predicate_blocks_foreign_read_and_write(migrated_clone):
    repo = create_repository_catalog().draft_agents
    with migrated_clone.database.transaction() as tx:
        original = draft(tx)
        assert repo.get_draft(tx, owner_id=FOREIGN, draft_id=DRAFT) is None
        with pytest.raises(RepositoryNotFoundError):
            repo.compare_and_set_draft(
                tx,
                owner_id=FOREIGN,
                draft_id=DRAFT,
                expected_revision=0,
                updates={"description": "foreign"},
                updated_at=2,
            )
        assert repo.get_draft(tx, owner_id=OWNER, draft_id=DRAFT) == original


def test_stale_version_cannot_overwrite_committed_state(migrated_clone):
    repo = create_repository_catalog().draft_agents
    with migrated_clone.database.transaction() as tx:
        draft(tx)
        changed = repo.compare_and_set_draft(
            tx,
            owner_id=OWNER,
            draft_id=DRAFT,
            expected_revision=0,
            updates={"description": "committed"},
            updated_at=2,
        )
    with migrated_clone.database.transaction() as tx:
        with pytest.raises(RepositoryConflictError):
            repo.compare_and_set_draft(
                tx,
                owner_id=OWNER,
                draft_id=DRAFT,
                expected_revision=0,
                updates={"description": "stale"},
                updated_at=3,
            )
        assert changed.description == "committed" and changed.state_revision == 1
        assert repo.get_draft(tx, owner_id=OWNER, draft_id=DRAFT) == changed


def test_two_claimers_commit_exactly_one_generation(migrated_clone):
    repo = create_repository_catalog().draft_agents
    with migrated_clone.database.transaction() as tx:
        draft(tx)
    barrier = Barrier(2)
    with concurrent_database(migrated_clone) as database:

        def claim(claim_id):
            try:
                with database.transaction() as tx:
                    barrier.wait(timeout=30)
                    return repo.claim_generation(
                        tx,
                        owner_id=OWNER,
                        draft_id=DRAFT,
                        expected_revision=0,
                        claim_id=claim_id,
                    )
            except RepositoryConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = tuple(workers.map(claim, CLAIMS))
    accepted = tuple(record for record in results if record is not None)
    assert len(accepted) == 1
    with migrated_clone.database.transaction() as tx:
        stored = repo.get_draft(tx, owner_id=OWNER, draft_id=DRAFT)
        assert stored == accepted[0]
        assert stored.state_revision == 1 and stored.generation_claim_id in CLAIMS
        with pytest.raises(RepositoryConflictError):
            repo.claim_generation(
                tx,
                owner_id=FOREIGN,
                draft_id=DRAFT,
                expected_revision=1,
                claim_id=CLAIMS[0],
            )


def test_repository_write_keeps_caller_rollback_and_connection_custody(migrated_clone):
    repo = create_repository_catalog().draft_agents
    with (
        pytest.raises(RuntimeError, match="caller abort"),
        migrated_clone.database.transaction() as tx,
    ):
        created = draft(tx)
        assert repo.get_draft(tx, owner_id=OWNER, draft_id=DRAFT) == created
        assert tx.active
        with pytest.raises(AttributeError):
            created.description = "mutated"
        raise RuntimeError("caller abort")
    with migrated_clone.database.transaction() as tx:
        assert repo.get_draft(tx, owner_id=OWNER, draft_id=DRAFT) is None
        assert tx.fetch_one("SELECT 1 AS healthy")["healthy"] == 1
    assert not tx.active


@pytest.mark.parametrize("first", ("revoke", "open"))
@pytest.mark.postgresql_lock_catalog
def test_share_open_rechecks_revocation_after_row_lock(migrated_clone, first):
    repo = create_repository_catalog().share_grants
    with migrated_clone.database.transaction() as tx:
        original = grant(tx)
    entered = Event()
    state = {}

    def competing(database):
        with database.transaction() as tx:
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            if first == "revoke":
                assert repo.resolve_active_by_digest(tx, token_sha256=DIGEST, as_of=NOW) == original
                entered.set()
                return repo.record_open(
                    tx,
                    share_id=original.share_id,
                    token_sha256=DIGEST,
                    as_of=NOW,
                )
            entered.set()
            return repo.revoke_grant(
                tx,
                owner_id=OWNER,
                share_id=original.share_id,
                revoked_at=NOW,
            )

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        with migrated_clone.database.transaction() as tx:
            blocking_pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            if first == "revoke":
                assert (
                    repo.revoke_grant(
                        tx,
                        owner_id=OWNER,
                        share_id=original.share_id,
                        revoked_at=NOW,
                    )
                    == ShareGrantRevocationState.REVOKED
                )
            else:
                assert (
                    repo.record_open(
                        tx,
                        share_id=original.share_id,
                        token_sha256=DIGEST,
                        as_of=NOW,
                    ).open_count
                    == 1
                )
            pending = workers.submit(competing, database)
            assert entered.wait(timeout=30)
            wait_for_blocked_statement(database, state["pid"], blocking_pid)
        result = pending.result(timeout=30)
    if first == "revoke":
        assert result is None
    else:
        assert result == ShareGrantRevocationState.REVOKED
    with migrated_clone.database.transaction() as tx:
        stored = tx.fetch_one(
            "SELECT open_count, revoked_at FROM share_grant WHERE id=%s",
            (original.share_id,),
        )
        assert stored["open_count"] == (0 if first == "revoke" else 1)
        assert stored["revoked_at"] == NOW
        assert repo.resolve_active_by_digest(tx, token_sha256=DIGEST, as_of=NOW) is None
        assert (
            repo.record_open(tx, share_id=original.share_id, token_sha256=DIGEST, as_of=NOW) is None
        )
        assert (
            repo.revoke_grant(
                tx,
                owner_id=FOREIGN,
                share_id=original.share_id,
                revoked_at=NOW,
            )
            == ShareGrantRevocationState.MISSING
        )
