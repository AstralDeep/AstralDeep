"""Qualifies knowledge proposal creation, replay, supersession, races, and caller
rollback through PostgreSQL. Valid ASCII and Unicode proposals must reach persistence
without an invalid advisory lock key.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier

import pytest

from astralplane.api import create_repository_catalog
from astralplane.repositories.knowledge import KnowledgeProposalRecord, ProposalStatus
from tests.integration.test_lifecycle_acceptance import concurrent_database

NOW = datetime(2026, 10, 12, tzinfo=UTC)
IDS = tuple(f"20000000-0000-4000-8000-00000000000{index}" for index in (1, 2, 3))


def proposal(identity=IDS[0], *, agent="ci-lifecycle-1", tool="inspect"):
    return KnowledgeProposalRecord(
        proposal_id=identity,
        agent_id=agent,
        tool_name=tool,
        artifact_path="agents/synthetic.md",
        diff_payload="@@ Synthetic improvement 👩🏾‍🚀 é",
        artifact_sha_at_generation="a" * 64,
        evidence={"audit_ids": ("synthetic-audit",)},
        status=ProposalStatus.PENDING,
        reviewer_user_id=None,
        reviewed_at=None,
        reviewer_rationale=None,
        applied_at=None,
        generated_at=NOW,
    )


@pytest.mark.parametrize(
    ("agent", "tool"),
    (
        ("ci-lifecycle-1", "inspect"),
        ("synthetic-測試", "檢查"),
        ('synthetic-"comma,slash\\', 'inspect:"quoted"'),
    ),
    ids=("ascii", "unicode", "delimiters"),
)
def test_valid_proposal_persists_and_replays_exact_bytes(migrated_clone, agent, tool):
    repo = create_repository_catalog().knowledge.proposals
    original = proposal(agent=agent, tool=tool)
    with migrated_clone.database.transaction() as tx:
        try:
            created = repo.create_for_administration(tx, original)
        except ValueError as error:
            if "NUL (0x00)" in str(error):
                pytest.fail("valid proposal rejected by a NUL advisory lock key")
            raise
        assert created == original
        assert repo.create_for_administration(tx, original) == original
    with migrated_clone.database.transaction() as tx:
        assert repo.get_for_administration(tx, proposal_id=IDS[0]) == original
        assert repo.pending_count_for_administration(tx) == 1


def test_new_proposal_supersedes_only_pending_exact_pair(migrated_clone):
    repo = create_repository_catalog().knowledge.proposals
    with migrated_clone.database.transaction() as tx:
        repo.create_for_administration(tx, proposal())
        unrelated = repo.create_for_administration(tx, proposal(IDS[2], tool="other"))
        newest = repo.create_for_administration(tx, proposal(IDS[1]))
        assert (
            repo.get_for_administration(tx, proposal_id=IDS[0]).status == ProposalStatus.SUPERSEDED
        )
        assert repo.get_for_administration(tx, proposal_id=IDS[2]) == unrelated
        assert newest.status == ProposalStatus.PENDING
        assert repo.pending_count_for_administration(tx) == 2


def test_barrier_raced_proposals_leave_one_pending_exact_pair(migrated_clone):
    repo = create_repository_catalog().knowledge.proposals
    barrier = Barrier(2)
    with concurrent_database(migrated_clone) as database:

        def create(identity):
            with database.transaction() as tx:
                barrier.wait(timeout=30)
                return repo.create_for_administration(tx, proposal(identity))

        with ThreadPoolExecutor(max_workers=2) as workers:
            created = tuple(workers.map(create, IDS[:2]))
    assert {row.proposal_id for row in created} == set(IDS[:2])
    with migrated_clone.database.transaction() as tx:
        records = repo.list_for_administration(tx, agent_id="ci-lifecycle-1", tool_name="inspect")
        assert {row.proposal_id for row in records} == set(IDS[:2])
        assert sorted(row.status.value for row in records) == ["pending", "superseded"]


def test_caller_rollback_restores_prior_pending_proposal(migrated_clone):
    repo = create_repository_catalog().knowledge.proposals
    with migrated_clone.database.transaction() as tx:
        original = repo.create_for_administration(tx, proposal())
    with (
        pytest.raises(RuntimeError, match="caller abort"),
        migrated_clone.database.transaction() as tx,
    ):
        repo.create_for_administration(tx, proposal(IDS[1]))
        assert (
            repo.get_for_administration(tx, proposal_id=IDS[0]).status == ProposalStatus.SUPERSEDED
        )
        raise RuntimeError("caller abort")
    with migrated_clone.database.transaction() as tx:
        assert repo.get_for_administration(tx, proposal_id=IDS[0]) == original
        assert repo.get_for_administration(tx, proposal_id=IDS[1]) is None


def test_delimiter_like_identifiers_keep_independent_supersession_pairs(migrated_clone):
    repo = create_repository_catalog().knowledge.proposals
    first = proposal(agent="a: b", tool="c")
    other = proposal(IDS[1], agent="a", tool="b: c")
    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=1) as workers,
    ):

        def create_other():
            with database.transaction() as tx:
                return repo.create_for_administration(tx, other)

        with migrated_clone.database.transaction() as tx:
            repo.create_for_administration(tx, first)
            if tx.dialect.is_sqlite:
                from astralplane.database.sqlite import SQLiteLockTimeoutError

                with pytest.raises(SQLiteLockTimeoutError):
                    workers.submit(create_other).result(timeout=30)
            else:
                assert workers.submit(create_other).result(timeout=30) == other
            repo.create_for_administration(tx, replace(first, proposal_id=IDS[2]))
        with migrated_clone.database.transaction() as tx:
            if tx.dialect.is_sqlite:
                assert repo.create_for_administration(tx, other) == other
            assert repo.get_for_administration(tx, proposal_id=IDS[1]) == other
            assert repo.pending_count_for_administration(tx) == 2
