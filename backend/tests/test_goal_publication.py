"""Real PostgreSQL coverage for goal-aware conversation publication and work state."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Iterator

import pytest

from orchestrator.history import (
    ConversationCommitRepository,
    ConversationNotFound,
)
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    WorkAdmissionCoordinator,
)
from tests.helpers.voice_plane_runtime import (
    PlaneTestRuntime,
    isolated_plane_runtime,
    plane_work_admission_repository,
)


OWNER = "goal-publication-owner"


@pytest.fixture(scope="module")
def database() -> Iterator[PlaneTestRuntime]:
    with isolated_plane_runtime("goal_publication") as runtime:
        yield runtime


def _coordinator(database: PlaneTestRuntime) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=4,
                queue_limit=0,
                max_wait_ms=None,
                config_revision="goal-publication",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=4,
                queue_limit=8,
                max_wait_ms=5_000,
                config_revision="goal-publication",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.VOICE_INTERACTIVE,
                parent_class_name=AdmissionClass.INTERACTIVE,
                active_limit=2,
                queue_limit=0,
                max_wait_ms=0,
                config_revision="goal-publication",
            ),
        ),
        repository=plane_work_admission_repository(database),
        operation_retention=timedelta(hours=24),
    )


def _claim(
    coordinator: WorkAdmissionCoordinator,
    *,
    chat_id: str,
    admission_class: AdmissionClass = AdmissionClass.INTERACTIVE,
    request_generation: uuid.UUID | None = None,
):
    owner = OperationOwner(OwnerScope.USER, OWNER, None)
    submission_id = uuid.uuid4()
    connection_generation = uuid.uuid4()
    request_generation = request_generation or uuid.uuid4()
    accepted = coordinator.submit(
        OperationRequest(
            operation_kind="connection_frame",
            admission_class=admission_class,
            owner=owner,
            submission_id=submission_id,
            idempotency_namespace="goal_publication",
            idempotency_key=str(submission_id),
            normalized_input_digest="ab" * 32,
            chat_id=chat_id,
            parent_operation_id=None,
            connection_generation=connection_generation,
            request_generation=request_generation,
        )
    )
    assert accepted.accepted
    claim = coordinator.claim_operation(admission_class, accepted.operation_id)
    assert claim is not None
    return owner, connection_generation, claim


def _repository(
    database: PlaneTestRuntime, coordinator: WorkAdmissionCoordinator
) -> ConversationCommitRepository:
    return ConversationCommitRepository(
        plane_runtime=database,
        plane_repositories=database.repositories,
        operation_coordinator=coordinator,
    )


def _create_chat(database: PlaneTestRuntime, chat_id: str) -> None:
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'Goal publication', 1, 1)",
        (chat_id, OWNER),
    )


def _operation_state(
    coordinator: WorkAdmissionCoordinator, owner: OperationOwner, claim
):
    return coordinator.query_operation(
        owner=owner, operation_id=claim.operation.operation_id
    )


def _voice_stage(
    database: PlaneTestRuntime,
    repository: ConversationCommitRepository,
    coordinator: WorkAdmissionCoordinator,
):
    chat_id = str(uuid.uuid4())
    _create_chat(database, chat_id)
    owner, connection_generation, claim = _claim(
        coordinator,
        chat_id=chat_id,
        admission_class=AdmissionClass.VOICE_INTERACTIVE,
    )
    result = repository.accept_voice_turn(
        chat_id=chat_id,
        owner_user_id=OWNER,
        request_generation=claim.operation.request_generation,
        result_request_generation=uuid.uuid4(),
        connection_generation=connection_generation,
        user_content="question",
        operation_fence=claim.fence,
        operation_owner=owner,
        accept_turn=lambda **_: None,
    )
    repository.append_staged_message(
        commit_id=result["result"]["commit_id"],
        owner_user_id=OWNER,
        role="assistant",
        content="Useful partial answer",
        operation_fence=claim.fence,
    )
    return chat_id, owner, claim, result["result"]


def test_failed_publication_keeps_partial_conversation_and_terminalizes_operation(
    database: PlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    _create_chat(database, chat_id)
    coordinator = _coordinator(database)
    owner, _, claim = _claim(coordinator, chat_id=chat_id)
    repository = _repository(database, coordinator)
    staged = repository.stage_commit(
        chat_id=chat_id,
        owner_user_id=OWNER,
        request_generation=claim.operation.request_generation,
        operation_fence=claim.fence,
        operation_owner=owner,
        connection_generation=claim.operation.connection_generation,
    )

    committed = repository.publish_commit(
        commit_id=staged["commit_id"],
        owner_user_id=OWNER,
        messages=[{"role": "assistant", "content": "Useful partial answer"}],
        canvas_components=[],
        operation_fence=claim.fence,
        completion_state="failed",
    )

    assert committed["state"] == "committed"
    assert _operation_state(coordinator, owner, claim).state is OperationState.FAILED
    snapshot = repository.build_snapshot(
        chat_id=chat_id,
        owner_user_id=OWNER,
        connection_generation=uuid.uuid4(),
        request_generation=claim.operation.request_generation,
        snapshot_purpose="commit",
    )
    assert snapshot["transcript"][0]["parts"] == [
        {"type": "text", "text": "Useful partial answer"}
    ]
    operation = _operation_state(coordinator, owner, claim)
    assert operation.terminal_code == "goal_incomplete"
    assert operation.safe_summary == "Request incomplete; conversation committed"


def test_voice_result_publication_can_commit_partial_answer_as_failed(
    database: PlaneTestRuntime,
) -> None:
    coordinator = _coordinator(database)
    repository = _repository(database, coordinator)
    chat_id, owner, claim, staged = _voice_stage(database, repository, coordinator)

    committed = repository.publish_voice_result(
        commit_id=staged["commit_id"],
        owner_user_id=OWNER,
        canvas_components=[],
        canvas_layouts=[],
        operation_fence=claim.fence,
        completion_state="failed",
    )

    assert committed["state"] == "committed"
    operation = _operation_state(coordinator, owner, claim)
    assert operation.state is OperationState.FAILED
    assert operation.terminal_code == "goal_incomplete"
    snapshot = repository.build_snapshot(
        chat_id=chat_id,
        owner_user_id=OWNER,
        connection_generation=uuid.uuid4(),
        request_generation=staged["request_generation"],
        snapshot_purpose="commit",
    )
    assert [part["text"] for message in snapshot["transcript"] for part in message["parts"]] == [
        "question",
        "Useful partial answer",
    ]


def test_atomic_publication_can_commit_partial_answer_as_cancelled(
    database: PlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    _create_chat(database, chat_id)
    coordinator = _coordinator(database)
    owner, _, claim = _claim(coordinator, chat_id=chat_id)
    repository = _repository(database, coordinator)
    staged = repository.stage_commit(
        chat_id=chat_id,
        owner_user_id=OWNER,
        request_generation=claim.operation.request_generation,
        operation_fence=claim.fence,
        operation_owner=owner,
        connection_generation=claim.operation.connection_generation,
    )

    committed = repository.publish_commit(
        commit_id=staged["commit_id"],
        owner_user_id=OWNER,
        messages=[{"role": "assistant", "content": "Cancelled after progress"}],
        canvas_components=[],
        operation_fence=claim.fence,
        completion_state="cancelled",
    )

    assert committed["state"] == "committed"
    operation = _operation_state(coordinator, owner, claim)
    assert operation.state is OperationState.CANCELLED
    assert operation.terminal_code == "goal_cancelled"
    assert operation.safe_summary == "Request cancelled; conversation committed"
    snapshot = repository.build_snapshot(
        chat_id=chat_id,
        owner_user_id=OWNER,
        connection_generation=uuid.uuid4(),
        request_generation=staged["request_generation"],
        snapshot_purpose="commit",
    )
    assert snapshot["transcript"][0]["parts"] == [
        {"type": "text", "text": "Cancelled after progress"}
    ]


def test_voice_result_publication_can_commit_partial_answer_as_cancelled(
    database: PlaneTestRuntime,
) -> None:
    coordinator = _coordinator(database)
    repository = _repository(database, coordinator)
    chat_id, owner, claim, staged = _voice_stage(database, repository, coordinator)

    committed = repository.publish_voice_result(
        commit_id=staged["commit_id"],
        owner_user_id=OWNER,
        canvas_components=[],
        canvas_layouts=[],
        operation_fence=claim.fence,
        completion_state="cancelled",
    )

    assert committed["state"] == "committed"
    operation = _operation_state(coordinator, owner, claim)
    assert operation.state is OperationState.CANCELLED
    assert operation.terminal_code == "goal_cancelled"
    assert operation.safe_summary == "Request cancelled; conversation committed"
    snapshot = repository.build_snapshot(
        chat_id=chat_id,
        owner_user_id=OWNER,
        connection_generation=uuid.uuid4(),
        request_generation=staged["request_generation"],
        snapshot_purpose="commit",
    )
    assert [
        part["text"]
        for message in snapshot["transcript"]
        for part in message["parts"]
    ] == ["question", "Useful partial answer"]


def test_voice_result_invalid_completion_state_has_no_database_effects(
    database: PlaneTestRuntime,
) -> None:
    coordinator = _coordinator(database)
    repository = _repository(database, coordinator)
    _, owner, claim, staged = _voice_stage(database, repository, coordinator)

    with pytest.raises(ValueError, match="completion state"):
        repository.publish_voice_result(
            commit_id=staged["commit_id"],
            owner_user_id=OWNER,
            canvas_components=[],
            canvas_layouts=[],
            operation_fence=claim.fence,
            completion_state="retryable",
        )

    current = database.fetch_one(
        "SELECT state FROM conversation_commit WHERE commit_id = ?",
        (staged["commit_id"],),
    )
    assert current["state"] == "staged"
    assert _operation_state(coordinator, owner, claim).state is OperationState.RUNNING


def test_invalid_publication_completion_state_has_no_database_effects(
    database: PlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    _create_chat(database, chat_id)
    coordinator = _coordinator(database)
    owner, _, claim = _claim(coordinator, chat_id=chat_id)
    repository = _repository(database, coordinator)
    staged = repository.stage_commit(
        chat_id=chat_id,
        owner_user_id=OWNER,
        request_generation=claim.operation.request_generation,
        operation_fence=claim.fence,
        operation_owner=owner,
        connection_generation=claim.operation.connection_generation,
    )
    before_revision = database.fetch_one(
        "SELECT render_revision FROM chats WHERE id = ?", (chat_id,)
    )["render_revision"]

    with pytest.raises(ValueError, match="completion state"):
        repository.publish_commit(
            commit_id=staged["commit_id"],
            owner_user_id=OWNER,
            messages=[{"role": "assistant", "content": "must not publish"}],
            canvas_components=[],
            operation_fence=claim.fence,
            completion_state="retryable",
        )

    current = database.fetch_one(
        "SELECT state FROM conversation_commit WHERE commit_id = ?",
        (staged["commit_id"],),
    )
    after_revision = database.fetch_one(
        "SELECT render_revision FROM chats WHERE id = ?", (chat_id,)
    )["render_revision"]
    assert current["state"] == "staged"
    assert after_revision == before_revision
    assert _operation_state(coordinator, owner, claim).state is OperationState.RUNNING


def test_wrong_owner_cannot_observe_or_change_staged_publication(
    database: PlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    _create_chat(database, chat_id)
    coordinator = _coordinator(database)
    owner, _, claim = _claim(coordinator, chat_id=chat_id)
    repository = _repository(database, coordinator)
    staged = repository.stage_commit(
        chat_id=chat_id,
        owner_user_id=OWNER,
        request_generation=claim.operation.request_generation,
        operation_fence=claim.fence,
        operation_owner=owner,
        connection_generation=claim.operation.connection_generation,
    )

    with pytest.raises(ConversationNotFound):
        repository.publish_commit(
            commit_id=staged["commit_id"],
            owner_user_id="different-owner",
            messages=[{"role": "assistant", "content": "must stay private"}],
            canvas_components=[],
            operation_fence=claim.fence,
            completion_state="failed",
        )

    current = database.fetch_one(
        "SELECT state FROM conversation_commit WHERE commit_id = ?",
        (staged["commit_id"],),
    )
    assert current["state"] == "staged"
    assert _operation_state(coordinator, owner, claim).state is OperationState.RUNNING


def test_default_publication_still_completes_operation(
    database: PlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    _create_chat(database, chat_id)
    coordinator = _coordinator(database)
    owner, _, claim = _claim(coordinator, chat_id=chat_id)
    repository = _repository(database, coordinator)
    staged = repository.stage_commit(
        chat_id=chat_id,
        owner_user_id=OWNER,
        request_generation=claim.operation.request_generation,
        operation_fence=claim.fence,
        operation_owner=owner,
        connection_generation=claim.operation.connection_generation,
    )

    repository.publish_commit(
        commit_id=staged["commit_id"],
        owner_user_id=OWNER,
        messages=[{"role": "assistant", "content": "Complete answer"}],
        canvas_components=[],
        operation_fence=claim.fence,
    )

    assert _operation_state(coordinator, owner, claim).state is OperationState.COMPLETED


def test_terminalization_failure_rolls_back_conversation_publication(
    database: PlaneTestRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_id = str(uuid.uuid4())
    _create_chat(database, chat_id)
    coordinator = _coordinator(database)
    owner, _, claim = _claim(coordinator, chat_id=chat_id)
    repository = _repository(database, coordinator)
    staged = repository.stage_commit(
        chat_id=chat_id,
        owner_user_id=OWNER,
        request_generation=claim.operation.request_generation,
        operation_fence=claim.fence,
        operation_owner=owner,
        connection_generation=claim.operation.connection_generation,
    )
    terminalize = coordinator.terminalize

    def fail_after_terminalize(*args, **kwargs):
        terminalize(*args, **kwargs)
        raise RuntimeError("terminalization transaction aborted")

    monkeypatch.setattr(coordinator, "terminalize", fail_after_terminalize)

    with pytest.raises(RuntimeError, match="transaction aborted"):
        repository.publish_commit(
            commit_id=staged["commit_id"],
            owner_user_id=OWNER,
            messages=[{"role": "assistant", "content": "must roll back"}],
            canvas_components=[],
            operation_fence=claim.fence,
            completion_state="failed",
        )

    publication = database.fetch_one(
        "SELECT state FROM conversation_commit WHERE commit_id = ?",
        (staged["commit_id"],),
    )
    conversation = database.fetch_one(
        "SELECT render_revision FROM chats WHERE id = ?", (chat_id,)
    )
    assert publication["state"] == "staged"
    assert conversation["render_revision"] == 0
    assert _operation_state(coordinator, owner, claim).state is OperationState.RUNNING


def test_repeated_unfenced_publication_returns_existing_commit(
    database: PlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    _create_chat(database, chat_id)
    repository = ConversationCommitRepository(
        plane_runtime=database,
        plane_repositories=database.repositories,
    )
    staged = repository.stage_commit(
        chat_id=chat_id,
        owner_user_id=OWNER,
        request_generation=uuid.uuid4(),
    )
    args = {
        "commit_id": staged["commit_id"],
        "owner_user_id": OWNER,
        "messages": [{"role": "assistant", "content": "one answer"}],
        "canvas_components": [],
    }

    first = repository.publish_commit(**args)
    replay = repository.publish_commit(
        **{**args, "messages": [], "canvas_components": []}
    )

    assert replay == first
