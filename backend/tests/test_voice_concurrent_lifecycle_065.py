"""Concurrent voice-turn lifecycle integration guards for Feature 065."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from orchestrator.voice_sessions import VoiceSessionRepository
from orchestrator.work_admission import OperationState
from tests.helpers.voice_plane_runtime import voice_session_repository
from tests.test_conversation_publication_voice_065 import (
    NOW,
    _admit_turn,
    _claim,
    _coordinator,
    _create_active_session,
)

pytest_plugins = ("tests.test_conversation_publication_voice_065",)


def _insert_message(database, *, user_id: str, chat_id: str, content: str) -> int:
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'user', ?, ?)",
        (chat_id, user_id, json.dumps(content), 2),
    )
    return int(
        database.fetch_one(
            "SELECT id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        )["id"]
    )


def _accept(
    repository: VoiceSessionRepository,
    database,
    admission,
    *,
    operation_id: uuid.UUID,
    now_offset: int,
):
    message_id = _insert_message(
        database,
        user_id=admission.turn.user_id,
        chat_id=admission.turn.chat_id,
        content=f"turn-{admission.turn.turn_id}",
    )
    return repository.accept_transcript(
        user_id=admission.turn.user_id,
        turn_id=admission.turn.turn_id,
        message_id=message_id,
        accepted_connection_generation=str(uuid.uuid4()),
        acceptance_commit_id=None,
        result_commit_id=None,
        operation_id=str(operation_id),
        now=NOW + timedelta(seconds=now_offset),
    ).turn


def _insert_chat(database, *, user_id: str, chat_id: str, title: str) -> None:
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, 1)",
        (chat_id, user_id, title),
    )


def test_navigation_keeps_origin_attribution_and_interruption_is_not_cancel(
    database,
) -> None:
    user_id = f"voice-concurrent-nav-{uuid.uuid4().hex}"
    first_chat = str(uuid.uuid4())
    second_chat = str(uuid.uuid4())
    repository = voice_session_repository(database)
    session = _create_active_session(
        repository,
        database,
        user_id=user_id,
        chat_id=first_chat,
    )
    _insert_chat(
        database,
        user_id=user_id,
        chat_id=second_chat,
        title="Second voice chat",
    )
    coordinator = _coordinator(database)

    first_admission = _admit_turn(
        repository,
        session,
        text="first request",
        now=NOW + timedelta(seconds=1),
    )
    first_owner, _connection, first_claim = _claim(
        coordinator,
        user_id=user_id,
        chat_id=first_chat,
        request_generation=first_admission.turn.request_generation,
    )
    first = _accept(
        repository,
        database,
        first_admission,
        operation_id=first_claim.operation.operation_id,
        now_offset=2,
    )

    pending = repository.request_chat_context_update(
        user_id=user_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        expected_media_grant_revision=session.media_grant_revision,
        expected_chat_context_revision=session.chat_context_revision,
        visible_chat_id=second_chat,
        now=NOW + timedelta(seconds=3),
    ).session
    current = repository.apply_chat_context(
        user_id=user_id,
        session_id=session.session_id,
        expected_generation=pending.generation,
        expected_media_grant_revision=pending.media_grant_revision,
        control_owner_id="publication-test",
        visible_chat_id=second_chat,
        chat_context_revision=pending.chat_context_revision,
        now=NOW + timedelta(seconds=4),
    ).session
    second_admission = _admit_turn(
        repository,
        current,
        text="second request",
        now=NOW + timedelta(seconds=5),
    )
    second_owner, _connection, second_claim = _claim(
        coordinator,
        user_id=user_id,
        chat_id=second_chat,
        request_generation=second_admission.turn.request_generation,
    )
    second = _accept(
        repository,
        database,
        second_admission,
        operation_id=second_claim.operation.operation_id,
        now_offset=6,
    )

    first_after_navigation = repository.get_turn(
        user_id=user_id,
        turn_id=first.turn_id,
    )
    assert first_after_navigation.chat_id == first_chat
    assert first_after_navigation.chat_context_revision == 1
    assert first_after_navigation.state == "processing"
    assert first_after_navigation.is_foreground is False
    assert second.chat_id == second_chat
    assert second.chat_context_revision == 2
    assert second.state == "processing"
    assert second.is_foreground is True
    assert (
        coordinator.query_operation(
            owner=first_owner,
            operation_id=first_claim.operation.operation_id,
        ).state
        is OperationState.RUNNING
    )
    assert (
        coordinator.query_operation(
            owner=second_owner,
            operation_id=second_claim.operation.operation_id,
        ).state
        is OperationState.RUNNING
    )

    cancelled = repository.terminalize_turn(
        user_id=user_id,
        turn_id=first.turn_id,
        terminal_kind="cancelled",
        result_commit_id=None,
        recap_source="terminal_status",
        sensitivity="non_sensitive",
        now=NOW + timedelta(seconds=7),
    ).turn
    assert cancelled.state == "cancelled"
    assert repository.get_turn(user_id=user_id, turn_id=second.turn_id).state == (
        "processing"
    )
    coordinator.terminalize(
        first_claim.fence,
        state=OperationState.CANCELLED,
        terminal_code="user_cancelled",
        safe_summary="Cancelled",
        retry_after_ms=None,
    )
    coordinator.terminalize(
        second_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )


def test_reverse_completions_terminalize_each_turn_once(database) -> None:
    user_id = f"voice-concurrent-complete-{uuid.uuid4().hex}"
    chat_id = str(uuid.uuid4())
    repository = voice_session_repository(database)
    session = _create_active_session(
        repository,
        database,
        user_id=user_id,
        chat_id=chat_id,
    )
    coordinator = _coordinator(database)
    admissions = [
        _admit_turn(
            repository,
            session,
            text=f"request {index}",
            now=NOW + timedelta(seconds=index),
        )
        for index in (1, 2)
    ]
    claims = [
        _claim(
            coordinator,
            user_id=user_id,
            chat_id=chat_id,
            request_generation=admission.turn.request_generation,
        )[2]
        for admission in admissions
    ]
    turns = [
        _accept(
            repository,
            database,
            admission,
            operation_id=claim.operation.operation_id,
            now_offset=index + 2,
        )
        for index, (admission, claim) in enumerate(zip(admissions, claims))
    ]

    completed_second = repository.terminalize_turn(
        user_id=user_id,
        turn_id=turns[1].turn_id,
        terminal_kind="succeeded",
        result_commit_id=None,
        recap_source="authoritative_summary",
        sensitivity="non_sensitive",
        now=NOW + timedelta(seconds=8),
    )
    assert completed_second.turn.state == "succeeded"
    assert repository.get_turn(user_id=user_id, turn_id=turns[0].turn_id).state == (
        "processing"
    )
    completed_first = repository.terminalize_turn(
        user_id=user_id,
        turn_id=turns[0].turn_id,
        terminal_kind="succeeded",
        result_commit_id=None,
        recap_source="authoritative_summary",
        sensitivity="non_sensitive",
        now=NOW + timedelta(seconds=9),
    )
    replay = repository.terminalize_turn(
        user_id=user_id,
        turn_id=turns[0].turn_id,
        terminal_kind="succeeded",
        result_commit_id=None,
        recap_source="authoritative_summary",
        sensitivity="non_sensitive",
        now=NOW + timedelta(seconds=10),
    )

    assert completed_first.turn.state == "succeeded"
    assert replay.replayed is True
    assert (
        database.fetch_one(
            "SELECT COUNT(*) AS count FROM voice_turn WHERE turn_id = ANY(?::uuid[]) "
            "AND state = 'succeeded'",
            ([turn.turn_id for turn in turns],),
        )["count"]
        == 2
    )
    for claim in claims:
        coordinator.terminalize(
            claim.fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="Completed",
            retry_after_ms=None,
        )


def test_unavailable_old_origin_cleans_voice_only_and_preserves_current_work(
    database,
) -> None:
    user_id = f"voice-concurrent-unavailable-{uuid.uuid4().hex}"
    old_chat = str(uuid.uuid4())
    current_chat = str(uuid.uuid4())
    repository = voice_session_repository(database)
    session = _create_active_session(
        repository,
        database,
        user_id=user_id,
        chat_id=old_chat,
    )
    _insert_chat(
        database,
        user_id=user_id,
        chat_id=current_chat,
        title="Current voice chat",
    )
    coordinator = _coordinator(database)
    old_admission = _admit_turn(
        repository,
        session,
        text="old request",
        now=NOW + timedelta(seconds=1),
    )
    old_owner, _connection, old_claim = _claim(
        coordinator,
        user_id=user_id,
        chat_id=old_chat,
        request_generation=old_admission.turn.request_generation,
    )
    old_turn = _accept(
        repository,
        database,
        old_admission,
        operation_id=old_claim.operation.operation_id,
        now_offset=2,
    )

    pending = repository.request_chat_context_update(
        user_id=user_id,
        session_id=session.session_id,
        expected_generation=1,
        expected_media_grant_revision=1,
        expected_chat_context_revision=1,
        visible_chat_id=current_chat,
        now=NOW + timedelta(seconds=3),
    ).session
    current = repository.apply_chat_context(
        user_id=user_id,
        session_id=session.session_id,
        expected_generation=1,
        expected_media_grant_revision=1,
        control_owner_id="publication-test",
        visible_chat_id=current_chat,
        chat_context_revision=pending.chat_context_revision,
        now=NOW + timedelta(seconds=4),
    ).session
    current_admission = _admit_turn(
        repository,
        current,
        text="current request",
        now=NOW + timedelta(seconds=5),
    )
    current_owner, _connection, current_claim = _claim(
        coordinator,
        user_id=user_id,
        chat_id=current_chat,
        request_generation=current_admission.turn.request_generation,
    )
    current_turn = _accept(
        repository,
        database,
        current_admission,
        operation_id=current_claim.operation.operation_id,
        now_offset=6,
    )

    receipt = repository.mark_chat_unavailable(
        user_id=user_id,
        chat_id=old_chat,
        reason="access_revoked",
        delete_chat=False,
        now=NOW + timedelta(seconds=7),
    )

    assert receipt.accepted_turn_ids == (old_turn.turn_id,)
    assert receipt.ended_sessions == ()
    assert repository.get_turn(user_id=user_id, turn_id=old_turn.turn_id).state == (
        "abandoned"
    )
    assert (
        repository.get_turn(
            user_id=user_id,
            turn_id=current_turn.turn_id,
        ).state
        == "processing"
    )
    assert (
        repository.get_session(
            user_id=user_id,
            session_id=session.session_id,
        ).visible_chat_id
        == current_chat
    )
    assert (
        coordinator.query_operation(
            owner=old_owner,
            operation_id=old_claim.operation.operation_id,
        ).state
        is OperationState.RUNNING
    )
    assert (
        coordinator.query_operation(
            owner=current_owner,
            operation_id=current_claim.operation.operation_id,
        ).state
        is OperationState.RUNNING
    )
    coordinator.terminalize(
        old_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed after origin cleanup",
        retry_after_ms=None,
    )
    coordinator.terminalize(
        current_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )
