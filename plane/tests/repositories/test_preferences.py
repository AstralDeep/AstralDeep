"""Tests for astralplane.repositories.preferences: feedback submit/amend/supersede CAS,
onboarding state transitions, profile reset fencing, and memory-record owner scoping,
using a fake transaction.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.preferences import (
    FeedbackCursor,
    FeedbackRecord,
    FeedbackRepository,
    MemoryRecord,
    OnboardingRepository,
    OnboardingStateRecord,
    PersonalizationProfileRecord,
    PersonalizationRepository,
    PreferencesRepository,
    ThemePreferenceRepository,
)

NOW = datetime(2026, 8, 13, 23, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Result:
    rowcount: int = 1
    status_message: str | None = None
    returned_records: tuple[dict[str, Any], ...] = ()


class FakeTransaction:
    def __init__(self) -> None:
        self.execute_results: deque[Result] = deque()
        self.fetch_one_results: deque[dict[str, Any] | None] = deque()
        self.fetch_all_results: deque[tuple[dict[str, Any], ...]] = deque()
        self.calls: list[tuple[str, str, object]] = []

    def execute(self, statement: str, parameters: object = ()) -> Result:
        self.calls.append(("execute", statement, parameters))
        return self.execute_results.popleft() if self.execute_results else Result()

    def fetch_one(self, statement: str, parameters: object = ()) -> dict[str, Any] | None:
        self.calls.append(("fetch_one", statement, parameters))
        return self.fetch_one_results.popleft() if self.fetch_one_results else None

    def fetch_all(self, statement: str, parameters: object = ()) -> tuple[dict[str, Any], ...]:
        self.calls.append(("fetch_all", statement, parameters))
        return self.fetch_all_results.popleft() if self.fetch_all_results else ()


def returned(row: dict[str, Any], *, rowcount: int = 1) -> Result:
    return Result(rowcount=rowcount, returned_records=(row,))


def feedback_record(**changes: Any) -> FeedbackRecord:
    values: dict[str, Any] = {
        "feedback_id": "feedback-1",
        "owner_id": "owner-1",
        "conversation_id": "chat-1",
        "correlation_id": "correlation-1",
        "source_agent": "agent-1",
        "source_tool": "tool-1",
        "component_id": "component-1",
        "sentiment": "positive",
        "category": "other",
        "comment": "useful",
        "comment_safety": "clean",
        "comment_safety_reason": "checked",
        "lifecycle": "active",
        "superseded_by": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return FeedbackRecord(**values)


def feedback_row(**changes: Any) -> dict[str, Any]:
    record = feedback_record()
    row = {
        "id": record.feedback_id,
        "user_id": record.owner_id,
        "conversation_id": record.conversation_id,
        "correlation_id": record.correlation_id,
        "source_agent": record.source_agent,
        "source_tool": record.source_tool,
        "component_id": record.component_id,
        "sentiment": record.sentiment,
        "category": record.category,
        "comment_raw": record.comment,
        "comment_safety": record.comment_safety,
        "comment_safety_reason": record.comment_safety_reason,
        "lifecycle": record.lifecycle,
        "superseded_by": record.superseded_by,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    row.update(changes)
    return row


def onboarding_record(**changes: Any) -> OnboardingStateRecord:
    values: dict[str, Any] = {
        "owner_id": "owner-1",
        "status": "in_progress",
        "last_step_id": 2,
        "started_at": NOW,
        "updated_at": NOW,
        "completed_at": None,
        "skipped_at": None,
        "dismissed_at": None,
        "dismiss_count": 0,
    }
    values.update(changes)
    return OnboardingStateRecord(**values)


def onboarding_row(**changes: Any) -> dict[str, Any]:
    record = onboarding_record()
    row = {
        "user_id": record.owner_id,
        "status": record.status,
        "last_step_id": record.last_step_id,
        "started_at": record.started_at,
        "updated_at": record.updated_at,
        "completed_at": record.completed_at,
        "skipped_at": record.skipped_at,
        "dismissed_at": record.dismissed_at,
        "dismiss_count": record.dismiss_count,
    }
    row.update(changes)
    return row


def profile_record(**changes: Any) -> PersonalizationProfileRecord:
    values: dict[str, Any] = {
        "owner_id": "owner-1",
        "profession": "engineer",
        "goals": ("ship", {"topic": "safety"}),
        "personality": {"tone": "direct"},
        "dreaming_enabled": True,
        "created_at": 10,
        "updated_at": 10,
    }
    values.update(changes)
    return PersonalizationProfileRecord(**values)


def profile_row(**changes: Any) -> dict[str, Any]:
    row = {
        "user_id": "owner-1",
        "profession": "engineer",
        "goals": '["ship",{"topic":"safety"}]',
        "personality": '{"tone":"direct"}',
        "dreaming_enabled": True,
        "created_at": 10,
        "updated_at": 10,
    }
    row.update(changes)
    return row


def memory_record(**changes: Any) -> MemoryRecord:
    values: dict[str, Any] = {
        "memory_id": "memory-1",
        "owner_id": "owner-1",
        "category": "goal",
        "value": "ship safely",
        "source": "explicit",
        "salience": 0.75,
        "created_at": 10,
        "updated_at": 10,
        "superseded_by": None,
        "superseded_at": None,
        "keywords": "ship,safety",
        "signature": "signature-1",
        "valid_from": 9,
        "valid_to": 99,
        "ingested_at": 11,
        "recall_count": 2,
        "last_recalled_at": 12,
        "project_id": "project-1",
    }
    values.update(changes)
    return MemoryRecord(**values)


def memory_row(**changes: Any) -> dict[str, Any]:
    record = memory_record()
    row = {
        "id": record.memory_id,
        "user_id": record.owner_id,
        "category": record.category,
        "value": record.value,
        "source": record.source,
        "salience": record.salience,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "superseded_by": record.superseded_by,
        "superseded_at": record.superseded_at,
        "keywords": record.keywords,
        "signature": record.signature,
        "valid_from": record.valid_from,
        "valid_to": record.valid_to,
        "ingested_at": record.ingested_at,
        "recall_count": record.recall_count,
        "last_recalled_at": record.last_recalled_at,
        "project_id": record.project_id,
    }
    row.update(changes)
    return row


def test_feedback_submit_replay_and_owner_scope() -> None:
    repository = FeedbackRepository()
    transaction = FakeTransaction()
    transaction.execute_results.append(returned(feedback_row()))
    created = repository.submit(transaction, feedback_record())
    assert created == feedback_record()
    assert transaction.calls[0][2][0:2] == ("feedback-1", "owner-1")

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(feedback_row())
    assert repository.submit(replay, feedback_record()) == created
    assert replay.calls[-1][2] == ("feedback-1", "owner-1")


def test_feedback_submit_handles_foreign_identity_and_semantic_conflict() -> None:
    repository = FeedbackRepository()
    foreign = FakeTransaction()
    foreign.execute_results.append(Result(rowcount=0))
    foreign.fetch_one_results.append(None)
    with pytest.raises(RepositoryConflictError, match="namespace"):
        repository.submit(foreign, feedback_record())

    changed = FakeTransaction()
    changed.execute_results.append(Result(rowcount=0))
    changed.fetch_one_results.append(feedback_row(updated_at=NOW + timedelta(seconds=1)))
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.submit(changed, feedback_record())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sentiment": "neutral"}, "sentiment"),
        ({"category": "mystery"}, "category"),
        ({"comment_safety": "unknown"}, "safety"),
        ({"lifecycle": "retracted"}, "active"),
        ({"superseded_by": "feedback-2"}, "active"),
        ({"comment": "x" * 8193}, "comment"),
        ({"comment_safety_reason": "x" * 1025}, "comment_safety_reason"),
        ({"created_at": NOW.replace(tzinfo=None)}, "created_at"),
        ({"updated_at": NOW - timedelta(seconds=1)}, "precede"),
        ({"conversation_id": ""}, "conversation_id"),
    ],
)
def test_feedback_submit_rejects_invalid_records(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(RepositoryValidationError, match=message):
        FeedbackRepository().submit(FakeTransaction(), feedback_record(**changes))


def test_feedback_optional_attribution_and_persisted_nulls_are_detached() -> None:
    transaction = FakeTransaction()
    null_row = feedback_row(
        conversation_id=None,
        correlation_id=None,
        source_agent=None,
        source_tool=None,
        component_id=None,
        comment_raw=None,
        comment_safety_reason=None,
        superseded_by=None,
    )
    transaction.execute_results.append(returned(null_row))
    record = feedback_record(
        conversation_id=None,
        correlation_id=None,
        source_agent=None,
        source_tool=None,
        component_id=None,
        comment=None,
        comment_safety_reason=None,
    )
    assert FeedbackRepository().submit(transaction, record) == record


def test_feedback_get_list_and_validation() -> None:
    repository = FeedbackRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((None, feedback_row()))
    assert repository.get(query, owner_id="owner-1", feedback_id="missing") is None
    assert repository.get(query, owner_id="owner-1", feedback_id="feedback-1") is not None

    query.fetch_all_results.append((feedback_row(), feedback_row(id="feedback-2")))
    records = repository.list_for_owner(query, owner_id="owner-1", lifecycle="superseded", limit=2)
    assert tuple(record.feedback_id for record in records) == ("feedback-1", "feedback-2")
    assert query.calls[-1][2] == ("owner-1", "superseded", 3)
    with pytest.raises(RepositoryValidationError, match="lifecycle"):
        repository.list_for_owner(query, owner_id="owner-1", lifecycle="missing")


def test_feedback_dedup_lookup_is_exact_and_owner_scoped() -> None:
    query = FakeTransaction()
    query.fetch_one_results.extend((feedback_row(), None))
    repository = FeedbackRepository()
    cutoff = NOW - timedelta(seconds=30)

    assert repository.find_in_dedup_window(
        query,
        owner_id="owner-1",
        correlation_id="correlation-1",
        component_id=None,
        cutoff=cutoff,
    ) == feedback_record()
    assert query.calls[0][2] == ("owner-1", "correlation-1", None, cutoff)
    assert "IS NOT DISTINCT FROM" in query.calls[0][1]
    assert (
        repository.find_in_dedup_window(
            query,
            owner_id="owner-2",
            correlation_id=None,
            component_id=None,
            cutoff=cutoff,
        )
        is None
    )

    with pytest.raises(RepositoryValidationError, match="cutoff"):
        repository.find_in_dedup_window(
            query,
            owner_id="owner-1",
            correlation_id=None,
            component_id=None,
            cutoff=cutoff.replace(tzinfo=None),
        )


def test_feedback_amend_active_uses_owner_lifecycle_and_timestamp_cas() -> None:
    repository = FeedbackRepository()
    updated_at = NOW + timedelta(seconds=1)
    success = FakeTransaction()
    success.execute_results.append(
        returned(
            feedback_row(
                sentiment="negative",
                category="wrong-data",
                comment_raw="incorrect",
                comment_safety_reason=None,
                updated_at=updated_at,
            )
        )
    )
    amended = repository.amend_active(
        success,
        owner_id="owner-1",
        feedback_id="feedback-1",
        expected_updated_at=NOW,
        sentiment="negative",
        category="wrong-data",
        comment="incorrect",
        comment_safety="clean",
        comment_safety_reason=None,
        updated_at=updated_at,
    )
    assert amended is not None and amended.sentiment == "negative"
    assert "lifecycle = 'active'" in success.calls[0][1]
    assert success.calls[0][2][-3:] == ("feedback-1", "owner-1", NOW)

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    assert (
        repository.amend_active(
            missing,
            owner_id="owner-2",
            feedback_id="feedback-1",
            expected_updated_at=NOW,
            sentiment="positive",
            category="other",
            comment=None,
            comment_safety="clean",
            comment_safety_reason=None,
            updated_at=updated_at,
        )
        is None
    )

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    stale.fetch_one_results.append(feedback_row(updated_at=updated_at))
    with pytest.raises(RepositoryConflictError, match="expected version"):
        repository.amend_active(
            stale,
            owner_id="owner-1",
            feedback_id="feedback-1",
            expected_updated_at=NOW,
            sentiment="positive",
            category="other",
            comment=None,
            comment_safety="clean",
            comment_safety_reason=None,
            updated_at=updated_at,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sentiment": "neutral"}, "sentiment"),
        ({"category": "unknown"}, "category"),
        ({"comment_safety": "unknown"}, "safety"),
        ({"comment": "x" * 8193}, "comment"),
        ({"comment_safety_reason": "x" * 1025}, "comment_safety_reason"),
        ({"updated_at": NOW}, "advance"),
    ],
)
def test_feedback_amend_rejects_invalid_values(changes: dict[str, Any], message: str) -> None:
    values: dict[str, Any] = {
        "owner_id": "owner-1",
        "feedback_id": "feedback-1",
        "expected_updated_at": NOW,
        "sentiment": "positive",
        "category": "other",
        "comment": None,
        "comment_safety": "clean",
        "comment_safety_reason": None,
        "updated_at": NOW + timedelta(seconds=1),
    }
    values.update(changes)
    with pytest.raises(RepositoryValidationError, match=message):
        FeedbackRepository().amend_active(FakeTransaction(), **values)


def test_feedback_filtered_keyset_page_and_cursor_validation() -> None:
    repository = FeedbackRepository()
    query = FakeTransaction()
    query.fetch_all_results.append(
        (
            feedback_row(id="feedback-3", created_at=NOW + timedelta(seconds=2)),
            feedback_row(id="feedback-2", created_at=NOW + timedelta(seconds=1)),
            feedback_row(id="feedback-1"),
        )
    )
    from_time = NOW - timedelta(days=1)
    to_time = NOW + timedelta(days=1)
    cursor = FeedbackCursor(created_at=NOW + timedelta(days=2), feedback_id="feedback-9")
    page = repository.list_page(
        query,
        owner_id="owner-1",
        source_tool="tool-1",
        source_agent="agent-1",
        from_time=from_time,
        to_time=to_time,
        cursor=cursor,
        limit=2,
    )
    assert tuple(record.feedback_id for record in page.records) == ("feedback-3", "feedback-2")
    assert page.next_cursor == FeedbackCursor(
        created_at=NOW + timedelta(seconds=1), feedback_id="feedback-2"
    )
    assert query.calls[0][2] == (
        "owner-1",
        "active",
        "tool-1",
        "agent-1",
        from_time,
        to_time,
        cursor.created_at,
        cursor.feedback_id,
        3,
    )
    assert "(created_at, id::text) <" in query.calls[0][1]

    with pytest.raises(RepositoryValidationError, match="from_time"):
        repository.list_page(
            query,
            owner_id="owner-1",
            from_time=to_time,
            to_time=from_time,
        )
    with pytest.raises(RepositoryValidationError, match="cursor"):
        repository.list_page(query, owner_id="owner-1", cursor="opaque")  # type: ignore[arg-type]


def test_feedback_admin_clean_comment_workload_is_bounded_and_typed() -> None:
    query = FakeTransaction()
    query.fetch_all_results.append(
        (
            {"id": "feedback-1", "user_id": "owner-1", "comment_raw": "safe sample"},
            {"id": "feedback-2", "user_id": "owner-2", "comment_raw": "second sample"},
        )
    )
    since = NOW - timedelta(days=14)
    candidates = FeedbackRepository().list_clean_comment_candidates_for_administration(
        query, since=since, limit=500
    )
    assert tuple(item.owner_id for item in candidates) == ("owner-1", "owner-2")
    assert query.calls[0][2] == (since, 500)
    assert "comment_safety = 'clean'" in query.calls[0][1]

    corrupt = FakeTransaction()
    corrupt.fetch_all_results.append(
        ({"id": "feedback-1", "user_id": "owner-1", "comment_raw": ""},)
    )
    with pytest.raises(RepositoryDataError, match="safe bounds"):
        FeedbackRepository().list_clean_comment_candidates_for_administration(
            corrupt, since=since
        )
    with pytest.raises(RepositoryValidationError, match="since"):
        FeedbackRepository().list_clean_comment_candidates_for_administration(
            query, since=since.replace(tzinfo=None)
        )


def test_feedback_retract_returns_updated_or_none() -> None:
    repository = FeedbackRepository()
    transaction = FakeTransaction()
    transaction.execute_results.extend(
        (
            returned(feedback_row(lifecycle="retracted", updated_at=NOW + timedelta(seconds=1))),
            Result(),
        )
    )
    updated = repository.retract(
        transaction,
        owner_id="owner-1",
        feedback_id="feedback-1",
        updated_at=NOW + timedelta(seconds=1),
    )
    assert updated is not None and updated.lifecycle == "retracted"
    assert (
        repository.retract(
            transaction,
            owner_id="owner-1",
            feedback_id="feedback-1",
            updated_at=NOW + timedelta(seconds=2),
        )
        is None
    )


def test_feedback_supersede_success_replay_missing_and_conflict() -> None:
    repository = FeedbackRepository()
    replacement = feedback_record(feedback_id="feedback-2")

    success = FakeTransaction()
    success.execute_results.extend((returned(feedback_row(id="feedback-2")), Result(rowcount=1)))
    assert (
        repository.supersede(
            success,
            owner_id="owner-1",
            old_feedback_id="feedback-1",
            replacement=replacement,
            updated_at=NOW + timedelta(seconds=1),
        ).feedback_id
        == "feedback-2"
    )

    replay = FakeTransaction()
    replay.execute_results.extend((returned(feedback_row(id="feedback-2")), Result(rowcount=0)))
    replay.fetch_one_results.append(
        feedback_row(lifecycle="superseded", superseded_by="feedback-2")
    )
    assert (
        repository.supersede(
            replay,
            owner_id="owner-1",
            old_feedback_id="feedback-1",
            replacement=replacement,
            updated_at=NOW + timedelta(seconds=1),
        ).feedback_id
        == "feedback-2"
    )

    missing = FakeTransaction()
    missing.execute_results.extend((returned(feedback_row(id="feedback-2")), Result(rowcount=0)))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.supersede(
            missing,
            owner_id="owner-1",
            old_feedback_id="feedback-1",
            replacement=replacement,
            updated_at=NOW,
        )

    conflict = FakeTransaction()
    conflict.execute_results.extend((returned(feedback_row(id="feedback-2")), Result(rowcount=0)))
    conflict.fetch_one_results.append(feedback_row(lifecycle="retracted"))
    with pytest.raises(RepositoryConflictError, match="no longer active"):
        repository.supersede(
            conflict,
            owner_id="owner-1",
            old_feedback_id="feedback-1",
            replacement=replacement,
            updated_at=NOW,
        )


def test_feedback_supersede_rejects_owner_change() -> None:
    with pytest.raises(RepositoryValidationError, match="owner"):
        FeedbackRepository().supersede(
            FakeTransaction(),
            owner_id="owner-1",
            old_feedback_id="feedback-1",
            replacement=feedback_record(owner_id="owner-2"),
            updated_at=NOW,
        )

    transaction = FakeTransaction()
    with pytest.raises(RepositoryValidationError, match="updated_at"):
        FeedbackRepository().supersede(
            transaction,
            owner_id="owner-1",
            old_feedback_id="feedback-1",
            replacement=feedback_record(feedback_id="feedback-2"),
            updated_at=NOW.replace(tzinfo=None),
        )
    assert transaction.calls == []


def test_feedback_rejects_corrupt_persisted_time() -> None:
    query = FakeTransaction()
    query.fetch_one_results.append(feedback_row(created_at=10))
    with pytest.raises(RepositoryDataError, match="timestamp"):
        FeedbackRepository().get(query, owner_id="owner-1", feedback_id="feedback-1")


def test_onboarding_get_create_replay_and_owner_scope() -> None:
    repository = OnboardingRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((None, onboarding_row()))
    assert repository.get_state(query, owner_id="owner-1") is None
    assert repository.get_state(query, owner_id="owner-1") == onboarding_record()
    assert query.calls[-1][2] == ("owner-1",)

    create = FakeTransaction()
    create.execute_results.append(returned(onboarding_row()))
    assert repository.put_state(create, onboarding_record()) == onboarding_record()

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(onboarding_row())
    assert repository.put_state(replay, onboarding_record()) == onboarding_record()


def test_onboarding_create_missing_and_conflict_are_visible() -> None:
    repository = OnboardingRepository()
    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.put_state(missing, onboarding_record())

    changed = FakeTransaction()
    changed.execute_results.append(Result(rowcount=0))
    changed.fetch_one_results.append(onboarding_row(status="completed"))
    with pytest.raises(RepositoryConflictError):
        repository.put_state(changed, onboarding_record())


def test_onboarding_compare_and_set_update_and_failures() -> None:
    repository = OnboardingRepository()
    update = onboarding_record(updated_at=NOW + timedelta(seconds=1), status="completed")
    success = FakeTransaction()
    success.execute_results.append(
        returned(onboarding_row(updated_at=update.updated_at, status="completed"))
    )
    assert repository.put_state(success, update, expected_updated_at=NOW) == update
    assert "updated_at = %s" in success.calls[0][1]

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.put_state(missing, update, expected_updated_at=NOW)

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    stale.fetch_one_results.append(onboarding_row())
    with pytest.raises(RepositoryConflictError):
        repository.put_state(stale, update, expected_updated_at=NOW)

    with pytest.raises(RepositoryValidationError, match="advance"):
        repository.put_state(FakeTransaction(), onboarding_record(), expected_updated_at=NOW)
    with pytest.raises(RepositoryValidationError, match="expected_updated_at"):
        repository.put_state(
            FakeTransaction(), update, expected_updated_at=NOW.replace(tzinfo=None)
        )


def test_onboarding_optional_times_and_dismissal() -> None:
    completed = NOW + timedelta(seconds=1)
    record = onboarding_record(
        status="completed",
        updated_at=completed,
        completed_at=completed,
        skipped_at=completed,
        dismissed_at=completed,
        dismiss_count=2,
    )
    row = onboarding_row(
        status="completed",
        updated_at=completed,
        completed_at=completed,
        skipped_at=completed,
        dismissed_at=completed,
        dismiss_count=2,
    )
    transaction = FakeTransaction()
    transaction.execute_results.append(returned(row))
    assert OnboardingRepository().put_state(transaction, record) == record

    dismissal = FakeTransaction()
    dismissal.execute_results.extend((returned(row), Result(rowcount=0)))
    assert (
        OnboardingRepository().record_dismissal(
            dismissal, owner_id="owner-1", dismissed_at=completed
        )
        == record
    )
    assert (
        OnboardingRepository().record_dismissal(
            dismissal, owner_id="owner-1", dismissed_at=completed
        )
        is None
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": "unknown"}, "status"),
        ({"last_step_id": -1}, "last_step_id"),
        ({"started_at": NOW.replace(tzinfo=None)}, "started_at"),
        ({"updated_at": NOW.replace(tzinfo=None)}, "updated_at"),
        ({"completed_at": NOW.replace(tzinfo=None)}, "completed_at"),
        ({"skipped_at": NOW.replace(tzinfo=None)}, "skipped_at"),
        ({"dismissed_at": NOW.replace(tzinfo=None)}, "dismissed_at"),
        ({"dismiss_count": -1}, "dismiss_count"),
    ],
)
def test_onboarding_rejects_invalid_state(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(RepositoryValidationError, match=message):
        OnboardingRepository().put_state(FakeTransaction(), onboarding_record(**changes))


def test_onboarding_rejects_corrupt_optional_time() -> None:
    query = FakeTransaction()
    query.fetch_one_results.append(onboarding_row(dismissed_at=1))
    with pytest.raises(RepositoryDataError, match="timestamp"):
        OnboardingRepository().get_state(query, owner_id="owner-1")


def test_profile_get_create_replay_and_detached_json() -> None:
    repository = PersonalizationRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((None, profile_row()))
    assert repository.get_profile(query, owner_id="owner-1") is None
    profile = repository.get_profile(query, owner_id="owner-1")
    assert profile is not None
    assert profile.goals[1]["topic"] == "safety"
    with pytest.raises(TypeError):
        profile.personality["tone"] = "changed"  # type: ignore[index]

    create = FakeTransaction()
    create.execute_results.append(returned(profile_row()))
    assert repository.put_profile(create, profile_record()) == profile_record()
    parameters = create.calls[0][2]
    assert parameters[2] == '["ship",{"topic":"safety"}]'

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(profile_row())
    assert repository.put_profile(replay, profile_record()) == profile_record()


def test_profile_create_missing_conflict_and_compare_and_set() -> None:
    repository = PersonalizationRepository()
    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.put_profile(missing, profile_record())

    changed = FakeTransaction()
    changed.execute_results.append(Result(rowcount=0))
    changed.fetch_one_results.append(profile_row(profession="doctor"))
    with pytest.raises(RepositoryConflictError):
        repository.put_profile(changed, profile_record())

    update_record = profile_record(updated_at=11, profession=None)
    update = FakeTransaction()
    update.execute_results.append(returned(profile_row(updated_at=11, profession=None)))
    assert repository.put_profile(update, update_record, expected_updated_at=10) == update_record

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    stale.fetch_one_results.append(profile_row(updated_at=12))
    with pytest.raises(RepositoryConflictError):
        repository.put_profile(stale, update_record, expected_updated_at=10)

    with pytest.raises(RepositoryValidationError, match="advance"):
        repository.put_profile(FakeTransaction(), profile_record(), expected_updated_at=10)


def test_profile_reset_is_owner_scoped_and_cas_fenced() -> None:
    repository = PersonalizationRepository()
    reset_row = profile_row(profession=None, goals="[]", personality="{}", updated_at=11)
    success = FakeTransaction()
    success.execute_results.append(returned(reset_row))
    reset = repository.reset_profile(
        success,
        owner_id="owner-1",
        updated_at=11,
        expected_updated_at=10,
    )
    assert reset.profession is None and reset.goals == () and dict(reset.personality) == {}
    assert success.calls[0][2] == (11, "owner-1", 10)

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.reset_profile(
            missing,
            owner_id="owner-1",
            updated_at=11,
            expected_updated_at=10,
        )

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    stale.fetch_one_results.append(profile_row(updated_at=12))
    with pytest.raises(RepositoryConflictError, match="changed"):
        repository.reset_profile(
            stale,
            owner_id="owner-1",
            updated_at=11,
            expected_updated_at=10,
        )

    with pytest.raises(RepositoryValidationError, match="advance"):
        repository.reset_profile(
            FakeTransaction(), owner_id="owner-1", updated_at=10, expected_updated_at=10
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"profession": "x" * 1025}, "profession"),
        ({"created_at": -1}, "created_at"),
        ({"updated_at": -1}, "updated_at"),
        ({"goals": (float("nan"),)}, "goals"),
        ({"personality": {"score": float("nan")}}, "personality"),
    ],
)
def test_profile_rejects_invalid_values(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(RepositoryValidationError, match=message):
        PersonalizationRepository().put_profile(FakeTransaction(), profile_record(**changes))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"goals": "{}"}, "invalid shapes"),
        ({"personality": "[]"}, "invalid shapes"),
        ({"goals": "not-json"}, "valid JSON"),
    ],
)
def test_profile_rejects_corrupt_persisted_json(changes: dict[str, Any], message: str) -> None:
    query = FakeTransaction()
    query.fetch_one_results.append(profile_row(**changes))
    with pytest.raises(RepositoryDataError, match=message):
        PersonalizationRepository().get_profile(query, owner_id="owner-1")


def test_memory_create_replay_and_owner_scope() -> None:
    repository = PersonalizationRepository()
    transaction = FakeTransaction()
    transaction.execute_results.append(returned(memory_row()))
    created = repository.create_memory(transaction, memory_record())
    assert created == memory_record()
    assert transaction.calls[0][2][0:2] == ("memory-1", "owner-1")

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(memory_row())
    assert repository.create_memory(replay, memory_record()) == created


def test_memory_create_foreign_identity_and_semantic_conflict() -> None:
    repository = PersonalizationRepository()
    foreign = FakeTransaction()
    foreign.execute_results.append(Result(rowcount=0))
    foreign.fetch_one_results.append(None)
    with pytest.raises(RepositoryConflictError, match="namespace"):
        repository.create_memory(foreign, memory_record())

    changed = FakeTransaction()
    changed.execute_results.append(Result(rowcount=0))
    changed.fetch_one_results.append(memory_row(value="changed"))
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.create_memory(changed, memory_record())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"category": "unknown"}, "category"),
        ({"source": "inferred"}, "source"),
        ({"value": ""}, "value"),
        ({"value": "x" * 16385}, "value"),
        ({"created_at": -1}, "created_at"),
        ({"updated_at": -1}, "updated_at"),
        ({"superseded_by": "memory-2"}, "begin live"),
        ({"superseded_at": 12}, "begin live"),
        ({"recall_count": -1}, "recall_count"),
    ],
)
def test_memory_create_rejects_invalid_record(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(RepositoryValidationError, match=message):
        PersonalizationRepository().create_memory(FakeTransaction(), memory_record(**changes))


def test_memory_get_list_project_scope_and_optional_nulls() -> None:
    repository = PersonalizationRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((None, memory_row()))
    assert repository.get_memory(query, owner_id="owner-1", memory_id="missing") is None
    assert repository.get_memory(query, owner_id="owner-1", memory_id="memory-1") is not None

    null_row = memory_row(
        superseded_by=None,
        superseded_at=None,
        keywords=None,
        signature=None,
        valid_from=None,
        valid_to=None,
        ingested_at=None,
        recall_count=None,
        last_recalled_at=None,
        project_id=None,
    )
    query.fetch_all_results.extend(
        ((null_row,), (memory_row(),), (memory_row(),), (null_row,))
    )
    assert repository.list_memory(query, owner_id="owner-1", limit=1)[0].recall_count == 0
    repository.list_memory(
        query, owner_id="owner-1", project_id="project-1", include_global=True, limit=2
    )
    assert "OR project_id IS NULL" in query.calls[-1][1]
    repository.list_memory(
        query, owner_id="owner-1", project_id="project-1", include_global=False, limit=2
    )
    assert "OR project_id IS NULL" not in query.calls[-1][1]
    assert query.calls[-1][2] == ("owner-1", "project-1", 2)
    only_global = repository.list_memory(
        query, owner_id="owner-1", global_only=True, limit=2
    )
    assert only_global[0].project_id is None
    assert "project_id IS NULL" in query.calls[-1][1]
    assert query.calls[-1][2] == ("owner-1", 2)

    with pytest.raises(RepositoryValidationError, match="combined"):
        repository.list_memory(
            query,
            owner_id="owner-1",
            project_id="project-1",
            global_only=True,
        )
    with pytest.raises(RepositoryValidationError, match="boolean"):
        repository.list_memory(
            query, owner_id="owner-1", global_only="yes"  # type: ignore[arg-type]
        )


def test_memory_supersede_success_replay_and_mismatch() -> None:
    repository = PersonalizationRepository()
    success = FakeTransaction()
    success.execute_results.append(Result(rowcount=1))
    assert repository.supersede_memory(
        success,
        owner_id="owner-1",
        memory_id="memory-1",
        superseded_at=20,
        replacement_id="memory-2",
    )

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(
        memory_row(superseded_at=20, superseded_by="memory-2", updated_at=20)
    )
    assert repository.supersede_memory(
        replay,
        owner_id="owner-1",
        memory_id="memory-1",
        superseded_at=20,
        replacement_id="memory-2",
    )

    mismatch = FakeTransaction()
    mismatch.execute_results.extend((Result(rowcount=0), Result(rowcount=0)))
    mismatch.fetch_one_results.extend((None, memory_row(superseded_at=21)))
    assert not repository.supersede_memory(
        mismatch, owner_id="owner-1", memory_id="memory-1", superseded_at=20
    )
    assert not repository.supersede_memory(
        mismatch, owner_id="owner-1", memory_id="memory-1", superseded_at=20
    )


def test_memory_record_recall_updated_and_missing() -> None:
    repository = PersonalizationRepository()
    transaction = FakeTransaction()
    transaction.execute_results.extend(
        (returned(memory_row(recall_count=3, last_recalled_at=20)), Result(rowcount=0))
    )
    recalled = repository.record_recall(
        transaction, owner_id="owner-1", memory_id="memory-1", recalled_at=20
    )
    assert recalled is not None and recalled.recall_count == 3
    assert (
        repository.record_recall(
            transaction, owner_id="owner-1", memory_id="memory-1", recalled_at=21
        )
        is None
    )


def test_memory_validity_update_uses_owner_and_version_fences() -> None:
    repository = PersonalizationRepository()
    success = FakeTransaction()
    success.execute_results.append(
        returned(memory_row(valid_from=20, valid_to=30, ingested_at=21, updated_at=22))
    )
    updated = repository.set_validity(
        success,
        owner_id="owner-1",
        memory_id="memory-1",
        valid_from=20,
        valid_to=30,
        ingested_at=21,
        updated_at=22,
        expected_updated_at=10,
    )
    assert updated is not None and updated.valid_to == 30
    assert success.calls[0][2][-3:] == ("memory-1", "owner-1", 10)

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    assert (
        repository.set_validity(
            missing,
            owner_id="owner-2",
            memory_id="memory-1",
            valid_from=None,
            valid_to=None,
            ingested_at=None,
            updated_at=11,
            expected_updated_at=10,
        )
        is None
    )

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    stale.fetch_one_results.append(memory_row(updated_at=12))
    with pytest.raises(RepositoryConflictError, match="changed"):
        repository.set_validity(
            stale,
            owner_id="owner-1",
            memory_id="memory-1",
            valid_from=None,
            valid_to=None,
            ingested_at=None,
            updated_at=11,
            expected_updated_at=10,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"valid_from": -1}, "valid_from"),
        ({"valid_from": 31, "valid_to": 30}, "valid_from"),
        ({"ingested_at": -1}, "ingested_at"),
        ({"updated_at": 10}, "advance"),
    ],
)
def test_memory_validity_rejects_invalid_values(changes: dict[str, Any], message: str) -> None:
    values = {
        "owner_id": "owner-1",
        "memory_id": "memory-1",
        "valid_from": 20,
        "valid_to": 30,
        "ingested_at": 20,
        "updated_at": 11,
        "expected_updated_at": 10,
    }
    values.update(changes)
    with pytest.raises(RepositoryValidationError, match=message):
        PersonalizationRepository().set_validity(FakeTransaction(), **values)


def test_memory_value_update_rotates_signature_and_detects_conflict() -> None:
    repository = PersonalizationRepository()
    success = FakeTransaction()
    success.execute_results.append(
        returned(memory_row(value="new value", signature="new-signature", updated_at=11))
    )
    updated = repository.update_memory_value(
        success,
        owner_id="owner-1",
        memory_id="memory-1",
        value="new value",
        signature="new-signature",
        updated_at=11,
        expected_updated_at=10,
    )
    assert updated is not None and updated.signature == "new-signature"
    assert success.calls[0][2] == (
        "new value",
        "new-signature",
        11,
        "memory-1",
        "owner-1",
        10,
    )

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    assert (
        repository.update_memory_value(
            missing,
            owner_id="owner-2",
            memory_id="memory-1",
            value="new value",
            signature=None,
            updated_at=11,
            expected_updated_at=10,
        )
        is None
    )

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    stale.fetch_one_results.append(memory_row(updated_at=12))
    with pytest.raises(RepositoryConflictError, match="changed"):
        repository.update_memory_value(
            stale,
            owner_id="owner-1",
            memory_id="memory-1",
            value="new value",
            signature=None,
            updated_at=11,
            expected_updated_at=10,
        )

    with pytest.raises(RepositoryValidationError, match="value"):
        repository.update_memory_value(
            FakeTransaction(),
            owner_id="owner-1",
            memory_id="memory-1",
            value="",
            signature=None,
            updated_at=11,
            expected_updated_at=10,
        )


def test_memory_delete_is_owner_scoped_and_version_fenced() -> None:
    repository = PersonalizationRepository()
    success = FakeTransaction()
    success.execute_results.append(Result(rowcount=1))
    assert repository.delete_memory(
        success, owner_id="owner-1", memory_id="memory-1", expected_updated_at=10
    )
    assert success.calls[0][2] == ("memory-1", "owner-1", 10)

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    assert not repository.delete_memory(
        missing, owner_id="owner-2", memory_id="memory-1", expected_updated_at=10
    )

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    stale.fetch_one_results.append(memory_row(updated_at=11))
    with pytest.raises(RepositoryConflictError, match="changed"):
        repository.delete_memory(
            stale, owner_id="owner-1", memory_id="memory-1", expected_updated_at=10
        )


def test_persona_put_and_stale_conflict() -> None:
    repository = PersonalizationRepository()
    transaction = FakeTransaction()
    transaction.execute_results.append(
        returned({"user_id": "owner-1", "persona": "concise", "score": 0.8, "updated_at": 20})
    )
    persona = repository.put_persona(
        transaction, owner_id="owner-1", persona="concise", score=0.8, updated_at=20
    )
    assert persona.persona == "concise"

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    with pytest.raises(RepositoryConflictError, match="older"):
        repository.put_persona(stale, owner_id="owner-1", persona="old", score=0.1, updated_at=19)


def test_persona_get_is_owner_scoped_and_detached() -> None:
    repository = PersonalizationRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend(
        (
            {"user_id": "owner-1", "persona": "concise", "score": 0.8, "updated_at": 20},
            None,
        )
    )
    persona = repository.get_persona(query, owner_id="owner-1")
    assert persona is not None and persona.persona == "concise"
    assert query.calls[0][2] == ("owner-1",)
    assert repository.get_persona(query, owner_id="owner-2") is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"owner_id": ""}, "owner_id"),
        ({"persona": "x" * 16385}, "persona"),
        ({"updated_at": -1}, "updated_at"),
    ],
)
def test_persona_rejects_invalid_input(kwargs: dict[str, Any], message: str) -> None:
    values: dict[str, Any] = {
        "owner_id": "owner-1",
        "persona": "concise",
        "score": 0.8,
        "updated_at": 20,
    }
    values.update(kwargs)
    with pytest.raises(RepositoryValidationError, match=message):
        PersonalizationRepository().put_persona(FakeTransaction(), **values)


def test_theme_preferences_read_and_replace_only_the_theme_key() -> None:
    repository = ThemePreferenceRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend(
        (
            {
                "user_id": "owner-1",
                "preferences": '{"disabled_agents":["agent-1"],'
                '"theme":{"preset":"ocean"}}',
                "updated_at": 10,
            },
            None,
        )
    )
    record = repository.get(query, owner_id="owner-1")
    assert record is not None and record.theme["preset"] == "ocean"
    assert repository.get(query, owner_id="owner-2") is None
    assert query.calls[0][2] == ("owner-1",)

    update = FakeTransaction()
    update.fetch_one_results.append(
        {
            "user_id": "owner-1",
            "preferences": '{"disabled_agents":["agent-1"],'
            '"theme":{"preset":"ocean"}}',
            "updated_at": 10,
        }
    )
    update.execute_results.append(
        returned(
            {
                "user_id": "owner-1",
                "preferences": '{"disabled_agents":["agent-1"],'
                '"theme":{"preset":"forest"}}',
                "updated_at": 11,
            }
        )
    )
    replaced = repository.put(
        update,
        owner_id="owner-1",
        theme={"preset": "forest"},
    )
    assert replaced.theme["preset"] == "forest"
    assert "FOR UPDATE" in update.calls[0][1]
    assert '"disabled_agents":["agent-1"]' in update.calls[1][2][0]
    assert '"theme":{"preset":"forest"}' in update.calls[1][2][0]


def test_theme_preferences_create_and_concurrent_insert_retry_are_typed() -> None:
    repository = ThemePreferenceRepository()
    create = FakeTransaction()
    create.fetch_one_results.append(None)
    create.execute_results.append(
        returned(
            {
                "user_id": "owner-1",
                "preferences": '{"theme":{"preset":"ocean"}}',
                "updated_at": 10,
            }
        )
    )
    assert repository.put(
        create, owner_id="owner-1", theme={"preset": "ocean"}
    ).theme["preset"] == "ocean"
    assert "ON CONFLICT (user_id) DO NOTHING" in create.calls[1][1]

    race = FakeTransaction()
    race.fetch_one_results.extend(
        (
            None,
            {
                "user_id": "owner-1",
                "preferences": '{"onboarding":{"status":"started"}}',
                "updated_at": 20,
            },
        )
    )
    race.execute_results.extend(
        (
            Result(rowcount=0),
            returned(
                {
                    "user_id": "owner-1",
                    "preferences": '{"onboarding":{"status":"started"},'
                    '"theme":{"preset":"daylight"}}',
                    "updated_at": 21,
                }
            ),
        )
    )
    retried = repository.put(
        race, owner_id="owner-1", theme={"preset": "daylight"}
    )
    assert retried.updated_at == 21
    assert race.calls[2][0] == "fetch_one"
    assert '"onboarding":{"status":"started"}' in race.calls[3][2][0]


@pytest.mark.parametrize(
    "theme",
    ("dark", {"payload": "x" * 65_537}, {"value": float("nan")}),
)
def test_theme_preferences_reject_non_object_unbounded_or_noncanonical_theme(
    theme: object,
) -> None:
    with pytest.raises(RepositoryValidationError):
        ThemePreferenceRepository().put(
            FakeTransaction(),
            owner_id="owner-1",
            theme=theme,  # type: ignore[arg-type]
        )

    corrupt = FakeTransaction()
    corrupt.fetch_one_results.append(
        {
            "user_id": "owner-1",
            "preferences": '{"theme":"dark"}',
            "updated_at": 10,
        }
    )
    with pytest.raises(RepositoryDataError, match="theme preference"):
        ThemePreferenceRepository().get(corrupt, owner_id="owner-1")


def test_preferences_facade_exposes_neutral_stores() -> None:
    facade = PreferencesRepository()
    assert isinstance(facade.feedback, FeedbackRepository)
    assert isinstance(facade.onboarding, OnboardingRepository)
    assert isinstance(facade.personalization, PersonalizationRepository)
    assert isinstance(facade.theme, ThemePreferenceRepository)


@pytest.mark.parametrize("for_update", (False, True))
def test_memory_read_lock_preserves_owner_and_record(for_update):
    repository, query = PersonalizationRepository(), FakeTransaction()
    query.fetch_one_results.append(memory_row())
    result = repository.get_memory(
        query, owner_id="owner-1", memory_id="memory-1", for_update=for_update
    )
    assert result == memory_record()
    assert query.calls[0][2] == ("memory-1", "owner-1")
    assert ("FOR UPDATE" in query.calls[0][1]) is for_update


@pytest.mark.parametrize("for_update", (None, 0, 1, "true", [], {}))
def test_memory_read_lock_refuses_non_boolean_before_query(for_update):
    query = FakeTransaction()
    with pytest.raises(RepositoryValidationError, match="for_update must be a boolean"):
        PersonalizationRepository().get_memory(
            query, owner_id="owner-1", memory_id="memory-1", for_update=for_update
        )
    assert query.calls == []
