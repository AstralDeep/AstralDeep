"""Tests for astralplane.repositories.knowledge: interaction synthesis marking,
quality-sample CAS updates, quarantine hold/release, and proposal
supersede-under-advisory-lock semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.knowledge import (
    InteractionRepository,
    KnowledgeProposalRecord,
    KnowledgeProposalRepository,
    KnowledgeRepository,
    ProposalStatus,
    QualitySignalRecord,
    QualitySignalRepository,
    QuarantineRepository,
    QuarantineStatus,
)
from tests.repositories._support import Result, ScriptedTransaction

START = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)
END = START + timedelta(hours=1)
COMPUTED = END + timedelta(seconds=1)


def _interaction_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 7,
        "agent_id": "agent-1",
        "tool_name": "search",
        "success": True,
        "error_message": None,
        "response_time_ms": 25,
        "chat_id": "chat-1",
        "synthesized": False,
        "created_at": 100,
    }
    row.update(overrides)
    return row


def _quality_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "signal-1",
        "agent_id": "agent-1",
        "tool_name": "search",
        "window_start": START,
        "window_end": END,
        "dispatch_count": 10,
        "failure_count": 2,
        "negative_feedback_count": 1,
        "failure_rate": 0.2,
        "negative_feedback_rate": 0.1,
        "status": "healthy",
        "computed_at": COMPUTED,
    }
    row.update(overrides)
    return row


def _quality(**overrides: object) -> QualitySignalRecord:
    values: dict[str, object] = {
        "signal_id": "signal-1",
        "agent_id": "agent-1",
        "tool_name": "search",
        "window_start": START,
        "window_end": END,
        "dispatch_count": 10,
        "failure_count": 2,
        "negative_feedback_count": 1,
        "failure_rate": 0.2,
        "negative_feedback_rate": 0.1,
        "status": "healthy",
        "computed_at": COMPUTED,
    }
    values.update(overrides)
    return QualitySignalRecord(**values)  # type: ignore[arg-type]


def _quarantine_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "feedback_id": "feedback-1",
        "reason": "unsafe",
        "detector": "inline",
        "detected_at": START,
        "status": "held",
        "actor_user_id": None,
        "actioned_at": None,
    }
    row.update(overrides)
    return row


def _review_row(**overrides: object) -> dict[str, object]:
    row = _quarantine_row()
    row.update(
        {
            "user_id": "owner-1",
            "source_agent": "agent-1",
            "source_tool": "search",
            "comment_raw": "private comment",
        }
    )
    row.update(overrides)
    return row


def _proposal_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "proposal-1",
        "agent_id": "agent-1",
        "tool_name": "search",
        "artifact_path": "agents/agent-1.md",
        "diff_payload": "@@ improvement",
        "artifact_sha_at_gen": "sha-1",
        "evidence": {"audit_ids": ["audit-1"]},
        "status": "pending",
        "reviewer_user_id": None,
        "reviewed_at": None,
        "reviewer_rationale": None,
        "applied_at": None,
        "generated_at": START,
    }
    row.update(overrides)
    return row


def _proposal(**overrides: object) -> KnowledgeProposalRecord:
    values: dict[str, object] = {
        "proposal_id": "proposal-1",
        "agent_id": "agent-1",
        "tool_name": "search",
        "artifact_path": "agents/agent-1.md",
        "diff_payload": "@@ improvement",
        "artifact_sha_at_generation": "sha-1",
        "evidence": {"audit_ids": ["audit-1"]},
        "status": ProposalStatus.PENDING,
        "reviewer_user_id": None,
        "reviewed_at": None,
        "reviewer_rationale": None,
        "applied_at": None,
        "generated_at": START,
    }
    values.update(overrides)
    return KnowledgeProposalRecord(**values)  # type: ignore[arg-type]


def test_knowledge_facade_exposes_fresh_typed_stores() -> None:
    first = KnowledgeRepository()
    second = KnowledgeRepository()
    assert isinstance(first.interactions, InteractionRepository)
    assert isinstance(first.quality_signals, QualitySignalRepository)
    assert isinstance(first.quarantine, QuarantineRepository)
    assert isinstance(first.proposals, KnowledgeProposalRepository)
    assert first.interactions is not second.interactions


def test_interactions_bind_owner_conversation_or_explicit_administration() -> None:
    repository = InteractionRepository()
    owner_tx = ScriptedTransaction(
        execute=[Result(returned_records=(_interaction_row(),))]
    )
    owned = repository.record_for_owner(
        owner_tx,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="chat-1",
        agent_id="agent-1",
        tool_name="search",
        success=True,
        error_message=None,
        response_time_ms=25,
        created_at=100,
    )
    assert owned.interaction_id == 7
    assert "chat.user_id = %s" in owner_tx.calls[0][1]

    admin_tx = ScriptedTransaction(
        execute=[Result(returned_records=(_interaction_row(chat_id=None),))]
    )
    assert repository.record_for_administration(
        admin_tx,  # type: ignore[arg-type]
        agent_id="agent-1",
        tool_name="search",
        success=True,
        error_message=None,
        response_time_ms=25,
        created_at=100,
    ).conversation_id is None
    assert "VALUES (%s, %s, %s, %s, %s, NULL" in admin_tx.calls[0][1]


def test_owner_interaction_missing_conversation_and_input_validation_fail_closed() -> None:
    repository = InteractionRepository()
    with pytest.raises(RepositoryNotFoundError):
        repository.record_for_owner(
            ScriptedTransaction(execute=[Result(rowcount=0)]),  # type: ignore[arg-type]
            owner_id="owner-1",
            conversation_id="chat-1",
            agent_id="agent-1",
            tool_name="search",
            success=True,
            error_message=None,
            response_time_ms=1,
            created_at=1,
        )
    with pytest.raises(RepositoryValidationError, match="boolean"):
        repository.record_for_administration(
            ScriptedTransaction(),  # type: ignore[arg-type]
            agent_id="agent-1",
            tool_name="search",
            success=1,  # type: ignore[arg-type]
            error_message=None,
            response_time_ms=None,
            created_at=1,
        )


def test_interaction_listing_marking_and_stats_are_bounded_and_verified() -> None:
    repository = InteractionRepository()
    query = ScriptedTransaction(
        all_rows=[
            (_interaction_row(),),
            ({"id": 7, "synthesized": True},),
            (
                {
                    "agent_id": "agent-1",
                    "tool_name": "search",
                    "total_calls": 2,
                    "success_count": 1,
                    "avg_response_ms": 12.5,
                },
            ),
        ]
    )
    assert repository.list_unsynthesized_for_administration(query, limit=3)  # type: ignore[arg-type]
    assert repository.mark_synthesized_for_administration(
        query, interaction_ids=(7,)  # type: ignore[arg-type]
    ) == (7,)
    stats = repository.stats_for_administration(
        query, agent_id="agent-1", limit=4  # type: ignore[arg-type]
    )
    assert stats[0].average_response_ms == 12.5
    assert query.calls[-1][2] == ("agent-1", 4)


def test_interaction_exact_admin_batch_preserves_requested_order() -> None:
    repository = InteractionRepository()
    query = ScriptedTransaction(
        all_rows=[(_interaction_row(id=9), _interaction_row(id=7))]
    )
    records = repository.get_many_for_administration(
        query, interaction_ids=(9, 7)  # type: ignore[arg-type]
    )
    assert tuple(record.interaction_id for record in records) == (9, 7)
    assert "array_position" in query.calls[0][1]
    assert query.calls[0][2] == ([9, 7], [9, 7])


def test_interaction_exact_admin_batch_fails_closed_on_partial_or_invalid_set() -> None:
    repository = InteractionRepository()
    with pytest.raises(RepositoryNotFoundError, match="incomplete"):
        repository.get_many_for_administration(
            ScriptedTransaction(all_rows=[(_interaction_row(id=7),)]),
            interaction_ids=(7, 9),  # type: ignore[arg-type]
        )
    for identifiers in ((), (7, 7), tuple(range(1, 2002))):
        with pytest.raises(RepositoryValidationError, match="bounded"):
            repository.get_many_for_administration(
                ScriptedTransaction(), interaction_ids=identifiers  # type: ignore[arg-type]
            )


def test_mark_synthesized_requires_complete_exact_set() -> None:
    with pytest.raises(RepositoryNotFoundError):
        InteractionRepository().mark_synthesized_for_administration(
            ScriptedTransaction(all_rows=[()]), interaction_ids=(7,)  # type: ignore[arg-type]
        )
    with pytest.raises(RepositoryValidationError):
        InteractionRepository().mark_synthesized_for_administration(
            ScriptedTransaction(), interaction_ids=(7, 7)  # type: ignore[arg-type]
        )


def test_quality_insert_and_exact_replay_are_idempotent() -> None:
    repository = QualitySignalRepository()
    inserted = ScriptedTransaction(execute=[Result(returned_records=(_quality_row(),))])
    assert repository.put_for_administration(inserted, _quality()) == _quality()  # type: ignore[arg-type]
    replay = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_quality_row()])
    assert repository.put_for_administration(replay, _quality()) == _quality()  # type: ignore[arg-type]


def test_quality_changed_semantics_require_timestamp_cas() -> None:
    repository = QualitySignalRepository()
    changed = _quality(status="underperforming")
    without_fence = ScriptedTransaction(
        execute=[Result(rowcount=0)], one=[_quality_row()]
    )
    with pytest.raises(RepositoryConflictError, match="semantics"):
        repository.put_for_administration(without_fence, changed)  # type: ignore[arg-type]

    updated_row = _quality_row(status="underperforming")
    with_fence = ScriptedTransaction(
        execute=[Result(rowcount=0), Result(returned_records=(updated_row,))],
        one=[_quality_row()],
    )
    assert repository.put_for_administration(
        with_fence, changed, expected_computed_at=COMPUTED  # type: ignore[arg-type]
    ).status == "underperforming"

    stale = ScriptedTransaction(
        execute=[Result(rowcount=0), Result(rowcount=0)], one=[_quality_row()]
    )
    with pytest.raises(RepositoryConflictError, match="stale"):
        repository.put_for_administration(
            stale, changed, expected_computed_at=COMPUTED  # type: ignore[arg-type]
        )


def test_quality_reads_aggregates_evidence_and_clean_samples() -> None:
    repository = QualitySignalRepository()
    query = ScriptedTransaction(
        one=[_quality_row(), {"count": 2}],
        all_rows=[
            (_quality_row(status="underperforming"),),
            (
                {
                    "agent_id": "agent-1",
                    "tool_name": "search",
                    "dispatch_count": 10,
                    "failure_count": 2,
                    "negative_feedback_count": 1,
                },
            ),
            ({"category": "wrong-data", "count": 2},),
            ({"event_id": "audit-1"},),
            ({"id": "feedback-1"},),
            (
                {
                    "id": "feedback-1",
                    "category": "wrong-data",
                    "comment_raw": "incorrect result",
                    "created_at": START,
                },
            ),
        ],
    )
    assert repository.latest_for_administration(
        query, agent_id="agent-1", tool_name="search"  # type: ignore[arg-type]
    )
    assert repository.list_underperforming_for_administration(
        query,  # type: ignore[arg-type]
        limit=5,
        before_computed_at=COMPUTED + timedelta(seconds=1),
        before_signal_id="signal-2",
    )
    assert repository.aggregate_window_for_administration(
        query, window_start=START, window_end=END  # type: ignore[arg-type]
    )[0].failure_count == 2
    categories = repository.category_breakdown_for_administration(
        query,  # type: ignore[arg-type]
        agent_id="agent-1",
        tool_name="search",
        window_start=START,
        window_end=END,
    )
    assert isinstance(categories, MappingProxyType) and categories["wrong-data"] == 2
    evidence = repository.evidence_ids_for_administration(
        query,  # type: ignore[arg-type]
        agent_id="agent-1",
        tool_name="search",
        window_start=START,
        window_end=END,
    )
    assert evidence.audit_event_ids == ("audit-1",)
    assert repository.clean_comment_samples_for_administration(
        query,  # type: ignore[arg-type]
        agent_id="agent-1",
        tool_name="search",
        window_start=START,
        window_end=END,
    )[0].comment == "incorrect result"
    assert repository.underperforming_count_for_administration(query) == 2  # type: ignore[arg-type]


def test_quality_validation_and_corrupt_aggregates_fail_closed() -> None:
    repository = QualitySignalRepository()
    with pytest.raises(RepositoryValidationError):
        repository.put_for_administration(
            ScriptedTransaction(), _quality(failure_count=11)  # type: ignore[arg-type]
        )
    with pytest.raises(RepositoryValidationError, match="cursor"):
        repository.list_underperforming_for_administration(
            ScriptedTransaction(), before_computed_at=COMPUTED  # type: ignore[arg-type]
        )
    with pytest.raises(RepositoryValidationError, match="follow"):
        repository.aggregate_window_for_administration(
            ScriptedTransaction(), window_start=END, window_end=START  # type: ignore[arg-type]
        )
    with pytest.raises(RepositoryDataError, match="inconsistent"):
        repository.aggregate_window_for_administration(
            ScriptedTransaction(
                all_rows=[
                    (
                        {
                            "agent_id": "agent-1",
                            "tool_name": "search",
                            "dispatch_count": 1,
                            "failure_count": 2,
                            "negative_feedback_count": 0,
                        },
                    )
                ]
            ),  # type: ignore[arg-type]
            window_start=START,
            window_end=END,
        )


def test_quarantine_hold_is_owner_scoped_and_atomic() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(rowcount=1), Result(returned_records=(_quarantine_row(),))]
    )
    record = QuarantineRepository().hold_for_owner(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        feedback_id="feedback-1",
        reason="unsafe",
        detector="inline",
        detected_at=START,
    )
    assert record.status is QuarantineStatus.HELD
    assert "user_id = %s" in transaction.calls[0][1]
    with pytest.raises(RepositoryNotFoundError):
        QuarantineRepository().hold_for_owner(
            ScriptedTransaction(execute=[Result(rowcount=0)]),  # type: ignore[arg-type]
            owner_id="owner-1",
            feedback_id="missing",
            reason="unsafe",
            detector="inline",
            detected_at=START,
        )


def test_quarantine_get_list_release_and_dismiss() -> None:
    repository = QuarantineRepository()
    query = ScriptedTransaction(one=[_quarantine_row()], all_rows=[(_review_row(),)])
    assert repository.get_for_administration(query, feedback_id="feedback-1")  # type: ignore[arg-type]
    reviews = repository.list_for_administration(
        query,  # type: ignore[arg-type]
        status="held",
        before_detected_at=START + timedelta(seconds=1),
        before_feedback_id="feedback-2",
    )
    assert reviews[0].owner_id == "owner-1"
    assert "owner-1" not in repr(reviews[0])

    released = _quarantine_row(
        status="released",
        actor_user_id="admin-1",
        actioned_at=END,
    )
    release = ScriptedTransaction(
        execute=[Result(returned_records=(released,)), Result(rowcount=1)]
    )
    assert repository.action_for_administration(
        release,  # type: ignore[arg-type]
        feedback_id="feedback-1",
        expected_detected_at=START,
        status="released",
        actor_user_id="admin-1",
        actioned_at=END,
    ).status is QuarantineStatus.RELEASED

    dismissed = _quarantine_row(
        status="dismissed",
        actor_user_id="admin-1",
        actioned_at=END,
    )
    dismiss = ScriptedTransaction(execute=[Result(returned_records=(dismissed,))])
    assert repository.action_for_administration(
        dismiss,  # type: ignore[arg-type]
        feedback_id="feedback-1",
        expected_detected_at=START,
        status="dismissed",
        actor_user_id="admin-1",
        actioned_at=END,
    ).status is QuarantineStatus.DISMISSED


def test_quarantine_cas_misses_and_invalid_lifecycle_are_explicit() -> None:
    repository = QuarantineRepository()
    with pytest.raises(RepositoryValidationError, match="release or dismiss"):
        repository.action_for_administration(
            ScriptedTransaction(),  # type: ignore[arg-type]
            feedback_id="feedback-1",
            expected_detected_at=START,
            status="held",
            actor_user_id="admin-1",
            actioned_at=END,
        )
    stale = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_quarantine_row()])
    with pytest.raises(RepositoryConflictError):
        repository.action_for_administration(
            stale,  # type: ignore[arg-type]
            feedback_id="feedback-1",
            expected_detected_at=START,
            status="dismissed",
            actor_user_id="admin-1",
            actioned_at=END,
        )
    missing = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    with pytest.raises(RepositoryNotFoundError):
        repository.action_for_administration(
            missing,  # type: ignore[arg-type]
            feedback_id="feedback-1",
            expected_detected_at=START,
            status="dismissed",
            actor_user_id="admin-1",
            actioned_at=END,
        )


def test_proposal_create_supersedes_prior_pending_under_advisory_lock() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(), Result(), Result(returned_records=(_proposal_row(),))]
    )
    proposal = KnowledgeProposalRepository().create_for_administration(
        transaction, _proposal()  # type: ignore[arg-type]
    )
    assert proposal.status is ProposalStatus.PENDING
    assert "pg_advisory_xact_lock" in transaction.calls[0][1]
    assert "status = 'superseded'" in transaction.calls[1][1]
    assert "::jsonb" in transaction.calls[2][1]


def test_proposal_replay_and_conflict_are_distinguished() -> None:
    repository = KnowledgeProposalRepository()
    replay = ScriptedTransaction(
        execute=[Result(), Result(), Result(rowcount=0)], one=[_proposal_row()]
    )
    assert repository.create_for_administration(replay, _proposal()).proposal_id == "proposal-1"  # type: ignore[arg-type]
    changed = ScriptedTransaction(
        execute=[Result(), Result(), Result(rowcount=0)],
        one=[_proposal_row(diff_payload="other")],
    )
    with pytest.raises(RepositoryConflictError, match="replay"):
        repository.create_for_administration(changed, _proposal())  # type: ignore[arg-type]


def test_proposal_get_list_transition_and_counts() -> None:
    repository = KnowledgeProposalRepository()
    query = ScriptedTransaction(
        one=[_proposal_row(), {"count": 1}], all_rows=[(_proposal_row(),)]
    )
    assert repository.get_for_administration(query, proposal_id="proposal-1")  # type: ignore[arg-type]
    assert repository.list_for_administration(
        query,  # type: ignore[arg-type]
        status="pending",
        agent_id="agent-1",
        tool_name="search",
        before_generated_at=START + timedelta(seconds=1),
        before_proposal_id="proposal-2",
    )
    assert repository.pending_count_for_administration(query) == 1  # type: ignore[arg-type]

    accepted = _proposal_row(
        status="accepted",
        reviewer_user_id="admin-1",
        reviewed_at=END,
        reviewer_rationale="evidence supports change",
    )
    transition = ScriptedTransaction(execute=[Result(returned_records=(accepted,))])
    assert repository.transition_for_administration(
        transition,  # type: ignore[arg-type]
        proposal_id="proposal-1",
        expected_status="pending",
        status="accepted",
        reviewer_user_id="admin-1",
        reviewed_at=END,
        reviewer_rationale="evidence supports change",
    ).status is ProposalStatus.ACCEPTED


def test_proposal_lifecycle_cas_and_new_state_validation_fail_closed() -> None:
    repository = KnowledgeProposalRepository()
    with pytest.raises(RepositoryValidationError, match="begin pending"):
        repository.create_for_administration(
            ScriptedTransaction(),
            _proposal(
                status=ProposalStatus.ACCEPTED,
                reviewer_user_id="admin-1",
                reviewed_at=END,
            ),  # type: ignore[arg-type]
        )
    with pytest.raises(RepositoryValidationError, match="edge"):
        repository.transition_for_administration(
            ScriptedTransaction(),  # type: ignore[arg-type]
            proposal_id="proposal-1",
            expected_status="pending",
            status="applied",
            reviewer_user_id="admin-1",
            reviewed_at=END,
        )
    stale = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_proposal_row()])
    with pytest.raises(RepositoryConflictError):
        repository.transition_for_administration(
            stale,  # type: ignore[arg-type]
            proposal_id="proposal-1",
            expected_status="pending",
            status="rejected",
            reviewer_user_id="admin-1",
            reviewed_at=END,
        )
    missing = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    with pytest.raises(RepositoryNotFoundError):
        repository.transition_for_administration(
            missing,  # type: ignore[arg-type]
            proposal_id="proposal-1",
            expected_status="pending",
            status="rejected",
            reviewer_user_id="admin-1",
            reviewed_at=END,
        )


def test_corrupt_persisted_lifecycle_rows_fail_closed() -> None:
    with pytest.raises(RepositoryDataError, match="quarantine"):
        QuarantineRepository().get_for_administration(
            ScriptedTransaction(one=[_quarantine_row(status="released")]),  # type: ignore[arg-type]
            feedback_id="feedback-1",
        )
    with pytest.raises(RepositoryDataError, match="pending proposal"):
        KnowledgeProposalRepository().get_for_administration(
            ScriptedTransaction(
                one=[_proposal_row(reviewer_user_id="admin-1", reviewed_at=END)]
            ),  # type: ignore[arg-type]
            proposal_id="proposal-1",
        )
