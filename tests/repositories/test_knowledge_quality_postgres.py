"""Runs knowledge quality selection against both durable backends with exact latest-row semantics.
Tied clocks, stale status, cursor boundaries, aggregate reads and caller rollback use real SQL.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from astralplane import create_repository_catalog
from astralplane.repositories.knowledge import QualitySignalRecord

START = datetime(2026, 11, 1, tzinfo=UTC)
END = START + timedelta(hours=1)


def signal(number, agent, status, *, computed=1):
    return QualitySignalRecord(
        signal_id=str(UUID(int=number)),
        agent_id=agent,
        tool_name="inspect",
        window_start=START,
        window_end=END + timedelta(minutes=number),
        dispatch_count=10,
        failure_count=2,
        negative_feedback_count=1,
        failure_rate=0.2,
        negative_feedback_rate=0.1,
        status=status,
        computed_at=END + timedelta(hours=computed),
    )


def test_latest_quality_status_is_chosen_before_filter_and_cursor(migrated_clone):
    repository = create_repository_catalog().knowledge.quality_signals
    values = (
        signal(1, "improved", "underperforming"),
        signal(2, "improved", "healthy", computed=2),
        signal(3, "regressed", "healthy"),
        signal(4, "regressed", "underperforming", computed=3),
        signal(5, "tied", "underperforming", computed=2),
        signal(6, "tied", "healthy", computed=2),
        signal(7, "later-page", "underperforming", computed=2),
    )
    with migrated_clone.database.transaction() as tx:
        for value in values:
            assert repository.put_for_administration(tx, value) == value
        assert repository.underperforming_count_for_administration(tx) == 2
        first = repository.list_underperforming_for_administration(tx, limit=1)
        assert first == (values[3],)
        assert repository.list_underperforming_for_administration(
            tx,
            limit=1,
            before_computed_at=first[0].computed_at,
            before_signal_id=first[0].signal_id,
        ) == (values[6],)
        assert (
            repository.latest_for_administration(
                tx,
                agent_id="tied",
                tool_name="inspect",
            )
            == values[5]
        )
        with pytest.raises(RuntimeError), tx.savepoint("quality_rollback"):
            repository.put_for_administration(
                tx,
                replace(values[5], status="underperforming", computed_at=END + timedelta(hours=4)),
                expected_computed_at=values[5].computed_at,
            )
            assert repository.underperforming_count_for_administration(tx) == 3
            raise RuntimeError("caller abort")
        assert repository.underperforming_count_for_administration(tx) == 2


def test_quality_aggregate_and_evidence_queries_execute_on_an_empty_window(migrated_clone):
    repository = create_repository_catalog().knowledge.quality_signals
    with migrated_clone.database.transaction() as tx:
        window = dict(window_start=START, window_end=END)
        scope = dict(agent_id="agent", tool_name="inspect", **window)
        assert repository.aggregate_window_for_administration(tx, **window) == ()
        assert dict(repository.category_breakdown_for_administration(tx, **scope)) == {}
        evidence = repository.evidence_ids_for_administration(tx, **scope)
        assert evidence.audit_event_ids == evidence.feedback_ids == ()
        assert repository.clean_comment_samples_for_administration(tx, **scope) == ()
