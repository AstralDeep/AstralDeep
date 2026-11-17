"""Checks fixed harness cleanup against real backends for canonical OIDC synthetic owners.
Issuer/run neighbors and legacy owners survive scoped cleanup, including caller and SQL failures.
"""

import pytest

from astralplane import create_repository_catalog
from astralplane.repositories.harness_cleanup import HarnessCleanupProfile

ISSUER = "0123456789abcdef"


def seed(database, profile):
    namespace = "__verif__" if profile is HarnessCleanupProfile.VERIFICATION else "__bench__"
    suffix = "_" if profile is HarnessCleanupProfile.VERIFICATION else "__"
    target = f"{namespace}run_1{suffix}owner"
    owners = (
        f"oidc.{ISSUER}.{target}",
        f"oidc.ffffffffffffffff.{target}",
        target,
        f"oidc.{ISSUER}.{namespace}runA1{suffix}owner",
        f"oidc.{ISSUER}.{namespace}run_1x{suffix}owner",
        f"oidc.{ISSUER}.everyday-owner",
    )
    with database.transaction() as tx:
        for index, owner in enumerate(owners):
            create_repository_catalog().draft_agents.create_draft(
                tx,
                draft_id=f"cleanup-{index}",
                owner_id=owner,
                agent_name="Fixture",
                agent_slug=f"fixture-{index}",
                description="synthetic cleanup fixture",
                observed_at=10,
            )
    return owners


def stored(database):
    with database.transaction() as tx:
        return tuple(
            row["user_id"] for row in tx.fetch_all("SELECT user_id FROM draft_agents ORDER BY id")
        )


@pytest.mark.parametrize("profile", tuple(HarnessCleanupProfile))
def test_canonical_cleanup_scopes_issuer_run_and_literal_separator_with_rollback(
    migrated_clone, profile
):
    database = migrated_clone.database
    owners = seed(database, profile)
    repository = create_repository_catalog().harness_cleanup
    with pytest.raises(RuntimeError), database.transaction() as tx:
        report = repository.purge_run(tx, profile=profile, run_id="run_1", owner_issuer_hash=ISSUER)
        assert report.total_deleted == 1
        assert tx.fetch_one("SELECT id FROM draft_agents WHERE user_id=%s", (owners[0],)) is None
        raise RuntimeError("caller rollback")
    assert stored(database) == owners
    with database.transaction() as tx:
        assert (
            repository.purge_run(
                tx,
                profile=profile,
                run_id="run_1",
                owner_issuer_hash=ISSUER,
            ).total_deleted
            == 1
        )
        assert repository.purge_run(tx, profile=profile, run_id="run_1").total_deleted == 1
    assert stored(database) == tuple(
        owner for index, owner in enumerate(owners) if index not in (0, 2)
    )


@pytest.mark.parametrize("profile", tuple(HarnessCleanupProfile))
def test_canonical_cleanup_sql_failure_rolls_back_earlier_deletions(migrated_clone, profile):
    database = migrated_clone.database
    owners = seed(database, profile)

    class FailedDelete:
        def __init__(self, tx):
            self.tx = tx

        def execute(self, statement, parameters):
            result = self.tx.execute(statement, parameters)
            if statement.startswith("DELETE FROM draft_agents "):
                raise RuntimeError("cleanup failed after deletion")
            return result

    with pytest.raises(RuntimeError, match="cleanup failed"), database.transaction() as tx:
        create_repository_catalog().harness_cleanup.purge_run(
            FailedDelete(tx),
            profile=profile,
            run_id="run_1",
            owner_issuer_hash=ISSUER,
        )
    assert stored(database) == owners
