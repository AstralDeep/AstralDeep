"""PostgreSQL regressions for goal outcomes through the public snapshot publisher."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from orchestrator.conversation_publication import ConversationPublicationStage
from orchestrator.orchestrator import Orchestrator
from orchestrator.work_admission import OperationState
from tests import test_goal_publication as publication_tests
from tests.helpers.voice_plane_runtime import PlaneTestRuntime


pytest_plugins = ("tests.test_goal_publication",)


def _orchestrator(
    database: PlaneTestRuntime,
    repository,
) -> Orchestrator:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.workspace = SimpleNamespace(
        live_components=lambda *_: [],
        live_layouts=lambda *_: [],
    )
    orchestrator.conversation_commits = repository

    async def discard_delivery(*_args, **_kwargs):
        return None

    orchestrator._deliver_committed_conversation_snapshot = discard_delivery
    return orchestrator


def _stage_atomic(database, coordinator, repository, *, completion_state="completed"):
    chat_id = str(uuid.uuid4())
    publication_tests._create_chat(database, chat_id)
    owner, _, claim = publication_tests._claim(coordinator, chat_id=chat_id)
    staged = repository.stage_commit(
        chat_id=chat_id,
        owner_user_id=publication_tests.OWNER,
        request_generation=claim.operation.request_generation,
        operation_fence=claim.fence,
        operation_owner=owner,
        connection_generation=claim.operation.connection_generation,
    )
    repository.append_staged_message(
        commit_id=staged["commit_id"],
        owner_user_id=publication_tests.OWNER,
        role="assistant",
        content="Useful partial answer",
        operation_fence=claim.fence,
    )
    stage = ConversationPublicationStage(
        history=repository._history,
        commit_id=staged["commit_id"],
        chat_id=chat_id,
        user_id=publication_tests.OWNER,
        base_render_revision=staged["base_render_revision"],
        next_render_revision=staged["base_render_revision"] + 1,
        operation_fence=claim.fence,
        completion_state=completion_state,
    )
    return chat_id, owner, claim, staged, stage


def test_public_snapshot_publisher_persists_partial_atomic_result_as_failed(database):
    coordinator = publication_tests._coordinator(database)
    repository = publication_tests._repository(database, coordinator)
    chat_id, owner, claim, staged, stage = _stage_atomic(
        database,
        coordinator,
        repository,
        completion_state="failed",
    )
    orchestrator = _orchestrator(database, repository)

    result = asyncio.run(
        orchestrator._publish_conversation_snapshot(
            None,
            stage=stage,
            request_generation=str(claim.operation.request_generation),
        )
    )

    assert result["state"] == "committed"
    assert stage.committed is True
    assert publication_tests._operation_state(coordinator, owner, claim).state is OperationState.FAILED
    snapshot = repository.build_snapshot(
        chat_id=chat_id,
        owner_user_id=publication_tests.OWNER,
        connection_generation=uuid.uuid4(),
        request_generation=staged["request_generation"],
        snapshot_purpose="commit",
    )
    assert snapshot["transcript"][0]["parts"] == [
        {"type": "text", "text": "Useful partial answer"}
    ]


def test_public_snapshot_publisher_persists_partial_voice_result_as_failed(database):
    coordinator = publication_tests._coordinator(database)
    repository = publication_tests._repository(database, coordinator)
    chat_id, owner, claim, staged = publication_tests._voice_stage(
        database,
        repository,
        coordinator,
    )
    stage = ConversationPublicationStage(
        history=repository._history,
        commit_id=staged["commit_id"],
        chat_id=chat_id,
        user_id=publication_tests.OWNER,
        base_render_revision=staged["base_render_revision"],
        next_render_revision=staged["base_render_revision"] + 1,
        operation_fence=claim.fence,
        publication_role="assistant_result",
        execution_base_render_revision=staged["execution_base_render_revision"],
        completion_state="failed",
    )
    orchestrator = _orchestrator(database, repository)

    result = asyncio.run(
        orchestrator._publish_conversation_snapshot(
            None,
            stage=stage,
            request_generation=staged["request_generation"],
        )
    )

    assert result["state"] == "committed"
    assert stage.committed is True
    assert publication_tests._operation_state(coordinator, owner, claim).state is OperationState.FAILED
    snapshot = repository.build_snapshot(
        chat_id=chat_id,
        owner_user_id=publication_tests.OWNER,
        connection_generation=uuid.uuid4(),
        request_generation=staged["request_generation"],
        snapshot_purpose="commit",
    )
    assert [
        part["text"]
        for message in snapshot["transcript"]
        for part in message["parts"]
    ] == ["question", "Useful partial answer"]


def test_public_snapshot_publisher_persists_partial_atomic_result_as_cancelled(database):
    coordinator = publication_tests._coordinator(database)
    repository = publication_tests._repository(database, coordinator)
    chat_id, owner, claim, staged, stage = _stage_atomic(
        database,
        coordinator,
        repository,
        completion_state="cancelled",
    )
    orchestrator = _orchestrator(database, repository)

    result = asyncio.run(
        orchestrator._publish_conversation_snapshot(
            None,
            stage=stage,
            request_generation=staged["request_generation"],
        )
    )

    assert result["state"] == "committed"
    assert stage.committed is True
    operation = publication_tests._operation_state(coordinator, owner, claim)
    assert operation.state is OperationState.CANCELLED
    assert operation.terminal_code == "goal_cancelled"
    assert operation.safe_summary == "Request cancelled; conversation committed"
    snapshot = repository.build_snapshot(
        chat_id=chat_id,
        owner_user_id=publication_tests.OWNER,
        connection_generation=uuid.uuid4(),
        request_generation=staged["request_generation"],
        snapshot_purpose="commit",
    )
    assert snapshot["transcript"][0]["parts"] == [
        {"type": "text", "text": "Useful partial answer"}
    ]


def test_public_snapshot_publisher_persists_partial_voice_result_as_cancelled(database):
    coordinator = publication_tests._coordinator(database)
    repository = publication_tests._repository(database, coordinator)
    chat_id, owner, claim, staged = publication_tests._voice_stage(
        database,
        repository,
        coordinator,
    )
    stage = ConversationPublicationStage(
        history=repository._history,
        commit_id=staged["commit_id"],
        chat_id=chat_id,
        user_id=publication_tests.OWNER,
        base_render_revision=staged["base_render_revision"],
        next_render_revision=staged["base_render_revision"] + 1,
        operation_fence=claim.fence,
        publication_role="assistant_result",
        execution_base_render_revision=staged["execution_base_render_revision"],
        completion_state="cancelled",
    )
    orchestrator = _orchestrator(database, repository)

    result = asyncio.run(
        orchestrator._publish_conversation_snapshot(
            None,
            stage=stage,
            request_generation=staged["request_generation"],
        )
    )

    assert result["state"] == "committed"
    assert stage.committed is True
    operation = publication_tests._operation_state(coordinator, owner, claim)
    assert operation.state is OperationState.CANCELLED
    assert operation.terminal_code == "goal_cancelled"
    assert operation.safe_summary == "Request cancelled; conversation committed"
    snapshot = repository.build_snapshot(
        chat_id=chat_id,
        owner_user_id=publication_tests.OWNER,
        connection_generation=uuid.uuid4(),
        request_generation=staged["request_generation"],
        snapshot_purpose="commit",
    )
    assert [
        part["text"]
        for message in snapshot["transcript"]
        for part in message["parts"]
    ] == ["question", "Useful partial answer"]


def test_public_snapshot_publisher_keeps_default_stage_completion(database):
    coordinator = publication_tests._coordinator(database)
    repository = publication_tests._repository(database, coordinator)
    _, owner, claim, _, stage = _stage_atomic(
        database,
        coordinator,
        repository,
    )
    orchestrator = _orchestrator(database, repository)

    asyncio.run(
        orchestrator._publish_conversation_snapshot(
            None,
            stage=stage,
            request_generation=str(claim.operation.request_generation),
        )
    )

    assert publication_tests._operation_state(coordinator, owner, claim).state is OperationState.COMPLETED


def test_invalid_stage_completion_state_has_no_publication_or_operation_effect(database):
    coordinator = publication_tests._coordinator(database)
    repository = publication_tests._repository(database, coordinator)
    chat_id = str(uuid.uuid4())
    publication_tests._create_chat(database, chat_id)
    owner, _, claim = publication_tests._claim(coordinator, chat_id=chat_id)
    commit_id = str(uuid.uuid4())

    with pytest.raises(ValueError, match="completion_state"):
        ConversationPublicationStage(
            history=repository._history,
            commit_id=commit_id,
            chat_id=chat_id,
            user_id=publication_tests.OWNER,
            base_render_revision=0,
            next_render_revision=1,
            operation_fence=claim.fence,
            completion_state="retryable",
        )

    assert database.fetch_one(
        "SELECT 1 FROM conversation_commit WHERE commit_id = ?", (commit_id,)
    ) is None
    assert database.fetch_one(
        "SELECT render_revision FROM chats WHERE id = ?", (chat_id,)
    )["render_revision"] == 0
    assert publication_tests._operation_state(coordinator, owner, claim).state is OperationState.RUNNING
