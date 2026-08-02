"""Owner/chat deletion and authorization-revocation proofs for Feature 065."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterator

import psycopg2
import pytest
from psycopg2 import sql

from orchestrator.history import ConversationCommitRepository, HistoryManager
from orchestrator.orchestrator import (
    ConnectionContext,
    Orchestrator,
    _ConnectionIngressFrame,
)
from orchestrator.voice_sessions import CreateSession, VoiceSessionRepository
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    PostgresWorkAdmissionRepository,
    WorkAdmissionCoordinator,
)
from shared.database import Database, _build_database_url


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    params = psycopg2.extensions.parse_dsn(_build_database_url())
    name = f"astraldeep_voice_delete_{uuid.uuid4().hex}"
    try:
        admin = psycopg2.connect(**params)
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        admin.close()
    except Exception as exc:  # pragma: no cover - environment gate
        pytest.skip(f"cannot create isolated PostgreSQL database: {exc}")

    database_params = dict(params)
    database_params["dbname"] = name
    prior_pool_setting = os.environ.get("DB_POOL_DISABLE")
    os.environ["DB_POOL_DISABLE"] = "1"
    try:
        yield Database(psycopg2.extensions.make_dsn(**database_params))
    finally:
        if prior_pool_setting is None:
            os.environ.pop("DB_POOL_DISABLE", None)
        else:
            os.environ["DB_POOL_DISABLE"] = prior_pool_setting
        Database.close()
        try:
            admin = psycopg2.connect(**params)
            admin.autocommit = True
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
                )
            admin.close()
        except Exception:
            pass


def _classes() -> tuple[AdmissionClassConfig, ...]:
    return (
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=8,
            queue_limit=0,
            max_wait_ms=0,
            config_revision="voice-chat-delete-065",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.INTERACTIVE,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=8,
            queue_limit=8,
            max_wait_ms=5_000,
            config_revision="voice-chat-delete-065",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.VOICE_INTERACTIVE,
            parent_class_name=AdmissionClass.INTERACTIVE,
            active_limit=4,
            queue_limit=0,
            max_wait_ms=0,
            config_revision="voice-chat-delete-065",
        ),
    )


def _coordinator(database: Database) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=_classes(),
        repository=PostgresWorkAdmissionRepository(database),
        operation_retention=timedelta(hours=24),
    )


def _insert_chat(database: Database, *, user_id: str, chat_id: str) -> None:
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'Voice lifecycle', 1, 1)",
        (chat_id, user_id),
    )


def _create_session(
    database: Database,
    repository: VoiceSessionRepository,
    *,
    user_id: str,
    chat_id: str,
):
    request = CreateSession(
        user_id=user_id,
        activation_id=str(uuid.uuid4()),
        device_id=str(uuid.uuid4()),
        device_kind="web",
        transport="livekit",
        room_name=f"room-{uuid.uuid4().hex}",
        participant_identity=f"client-{uuid.uuid4().hex}",
        visible_chat_id=chat_id,
        owner_connection_generation=str(uuid.uuid4()),
        control_binding_id=str(uuid.uuid4()),
        control_binding_expires_at=NOW + timedelta(minutes=10),
        lease_expires_at=NOW + timedelta(seconds=45),
        media_grant_nonce_hash=os.urandom(32),
        media_grant_issued_at=NOW,
        media_grant_expires_at=NOW + timedelta(minutes=5),
    )
    return repository.create_session(request, now=NOW).session


def _insert_turn(
    database: Database,
    repository: VoiceSessionRepository,
    *,
    session: Any,
    chat_id: str,
    chat_context_revision: int,
    state: str,
):
    turn_id = str(uuid.uuid4())
    result_request_generation = str(uuid.uuid4())
    detected_language = "en" if state == "submitting" else None
    spoken_output_policy = "full_recap" if state == "submitting" else "pending"
    output_reason = "ready" if state == "submitting" else "language_pending"
    database.execute(
        "INSERT INTO voice_turn ("
        "turn_id, client_turn_id, session_id, session_generation, "
        "media_grant_revision, user_id, chat_id, chat_context_revision, "
        "detected_language, spoken_output_policy, output_reason, "
        "execution_base_render_revision, submission_id, request_generation, "
        "result_request_generation, state, is_foreground, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, FALSE, ?, ?)",
        (
            turn_id,
            str(uuid.uuid4()),
            session.session_id,
            session.generation,
            session.media_grant_revision,
            session.user_id,
            chat_id,
            chat_context_revision,
            detected_language,
            spoken_output_policy,
            output_reason,
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            result_request_generation,
            state,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=1),
        ),
    )
    return repository.get_turn(user_id=session.user_id, turn_id=turn_id)


def _claim(
    coordinator: WorkAdmissionCoordinator,
    *,
    user_id: str,
    chat_id: str,
    request_generation: str,
):
    owner = OperationOwner(OwnerScope.USER, user_id, None)
    submission_id = uuid.uuid4()
    connection_generation = uuid.uuid4()
    result = coordinator.submit(
        OperationRequest(
            operation_kind="voice_chat_message",
            admission_class=AdmissionClass.VOICE_INTERACTIVE,
            owner=owner,
            submission_id=submission_id,
            idempotency_namespace="voice_chat_message",
            idempotency_key=str(submission_id),
            normalized_input_digest="ab" * 32,
            chat_id=chat_id,
            parent_operation_id=None,
            connection_generation=connection_generation,
            request_generation=uuid.UUID(request_generation),
        )
    )
    assert result.accepted
    claim = coordinator.claim_operation(
        AdmissionClass.VOICE_INTERACTIVE,
        result.operation_id,
    )
    assert claim is not None
    return owner, connection_generation, claim


def _accept_and_stage(
    database: Database,
    repository: VoiceSessionRepository,
    coordinator: WorkAdmissionCoordinator,
    *,
    turn: Any,
    content: str,
) -> SimpleNamespace:
    owner, connection_generation, claim = _claim(
        coordinator,
        user_id=turn.user_id,
        chat_id=turn.chat_id,
        request_generation=turn.request_generation,
    )
    commits = ConversationCommitRepository(
        database,
        operation_coordinator=coordinator,
    )

    def accept_turn(**correlation: Any):
        return repository.accept_transcript(
            user_id=turn.user_id,
            turn_id=turn.turn_id,
            message_id=correlation["message_id"],
            accepted_connection_generation=str(connection_generation),
            acceptance_commit_id=correlation["acceptance_commit_id"],
            result_commit_id=correlation["result_commit_id"],
            operation_id=str(claim.operation.operation_id),
            now=NOW + timedelta(seconds=2),
            transaction=correlation["cursor"],
        )

    accepted = commits.accept_voice_turn(
        chat_id=turn.chat_id,
        owner_user_id=turn.user_id,
        request_generation=turn.request_generation,
        result_request_generation=turn.result_request_generation,
        connection_generation=connection_generation,
        user_content=content,
        operation_fence=claim.fence,
        operation_owner=owner,
        accept_turn=accept_turn,
    )
    result_commit_id = accepted["result"]["commit_id"]
    candidate_revision = accepted["result"]["base_render_revision"] + 1
    commits.append_staged_message(
        commit_id=result_commit_id,
        owner_user_id=turn.user_id,
        role="assistant",
        content=[{"type": "text", "content": f"private {content}"}],
        operation_fence=claim.fence,
    )
    component_id = f"private-{uuid.uuid4().hex}"
    database.execute(
        "INSERT INTO saved_components ("
        "id, chat_id, user_id, component_data, component_type, title, "
        "created_at, component_id, position, updated_at, "
        "conversation_commit_id, committed_render_revision"
        ") VALUES (?, ?, ?, ?, 'text', 'private', 2, ?, 0, 2, ?, ?)",
        (
            str(uuid.uuid4()),
            turn.chat_id,
            turn.user_id,
            json.dumps(
                {
                    "type": "text",
                    "component_id": component_id,
                    "content": f"private {content}",
                },
                sort_keys=True,
            ),
            component_id,
            result_commit_id,
            candidate_revision,
        ),
    )
    database.execute(
        "INSERT INTO workspace_layout ("
        "chat_id, user_id, layout_key, position, layout, created_at, updated_at, "
        "conversation_commit_id, committed_render_revision"
        ") VALUES (?, ?, 'main', 0, ?, 2, 2, ?, ?)",
        (
            turn.chat_id,
            turn.user_id,
            json.dumps([{"type": "ref", "component_id": component_id}]),
            result_commit_id,
            candidate_revision,
        ),
    )
    return SimpleNamespace(
        owner=owner,
        connection_generation=connection_generation,
        claim=claim,
        acceptance_commit_id=accepted["acceptance"]["commit_id"],
        result_commit_id=result_commit_id,
        message_id=accepted["message_id"],
    )


def _history(database: Database) -> HistoryManager:
    history = object.__new__(HistoryManager)
    history.db = database
    return history


def _replay_frame(turn: Any) -> _ConnectionIngressFrame:
    origin = {
        "session_id": turn.session_id,
        "generation": turn.session_generation,
        "media_grant_revision": turn.media_grant_revision,
        "turn_id": turn.turn_id,
        "client_turn_id": turn.client_turn_id,
        "chat_context_revision": turn.chat_context_revision,
    }
    return _ConnectionIngressFrame(
        raw="",
        parsed={"payload": {"voice_origin": origin}},
        action="chat_message",
        surface="chat",
        chat_id=turn.chat_id,
        submission_id=uuid.UUID(turn.submission_id),
        request_generation=uuid.UUID(turn.request_generation),
        normalized_digest="ab" * 32,
        read_only=False,
        operation_kind="voice_chat_message",
        deadline_at_monotonic=None,
        deadline_at_utc=None,
    )


async def _replay_disposition(
    repository: VoiceSessionRepository,
    *,
    user_id: str,
    turn: Any,
) -> dict[str, Any]:
    websocket = object()
    connection_generation = uuid.uuid4()
    context = ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=connection_generation,
        registered=True,
    )
    sent: list[dict[str, Any]] = []

    async def safe_send(_websocket: object, data: str) -> bool:
        sent.append(json.loads(data))
        return True

    orchestrator = object.__new__(Orchestrator)
    orchestrator.voice_services = SimpleNamespace(repository=repository)
    orchestrator.ui_sessions = {websocket: {"sub": user_id}}
    orchestrator._safe_send = safe_send
    assert await orchestrator._replay_voice_ack_if_accepted(
        context,
        _replay_frame(turn),
    )
    assert len(sent) == 1
    return sent[0]


def test_hard_delete_fences_voice_and_retains_replay_tombstones(
    database: Database,
) -> None:
    user_id = f"voice-delete-{uuid.uuid4().hex}"
    chat_id = str(uuid.uuid4())
    _insert_chat(database, user_id=user_id, chat_id=chat_id)
    repository = VoiceSessionRepository(database)
    session = _create_session(
        database,
        repository,
        user_id=user_id,
        chat_id=chat_id,
    )
    unaccepted = _insert_turn(
        database,
        repository,
        session=session,
        chat_id=chat_id,
        chat_context_revision=1,
        state="recognizing",
    )
    submitting = _insert_turn(
        database,
        repository,
        session=session,
        chat_id=chat_id,
        chat_context_revision=1,
        state="submitting",
    )
    coordinator = _coordinator(database)
    staged = _accept_and_stage(
        database,
        repository,
        coordinator,
        turn=submitting,
        content="delete this chat",
    )

    receipt = _history(database).delete_chat(chat_id, user_id=user_id)

    assert receipt.chat_deleted
    assert not receipt.replayed
    assert receipt.reason == "deleted"
    assert [item.session_id for item in receipt.ended_sessions] == [session.session_id]
    assert receipt.announcement_session_keys == (
        (session.session_id, session.generation),
    )
    assert receipt.unaccepted_turn_ids == (unaccepted.turn_id,)
    assert receipt.accepted_turn_ids == (submitting.turn_id,)
    assert receipt.aborted_result_commit_ids == (staged.result_commit_id,)
    assert (
        database.fetch_one(
            "SELECT id FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        is None
    )

    ended = repository.get_session(
        user_id=user_id,
        session_id=session.session_id,
    )
    assert ended.state == "ended"
    assert ended.end_reason == "chat_deleted"
    assert ended.chat_unavailable_at is not None
    assert not ended.microphone_enabled
    assert not ended.foreground_active
    assert ended.control_owner_id is None

    rejected = repository.get_turn(user_id=user_id, turn_id=unaccepted.turn_id)
    assert rejected.state == rejected.terminal_kind == "abandoned"
    assert rejected.accepted_at is None
    assert rejected.rejection_reason == "chat_unavailable"
    assert rejected.rejection_retry_policy == "explicit_user_retry"
    assert rejected.submission_id == unaccepted.submission_id
    assert rejected.chat_id == chat_id

    accepted = repository.get_turn(user_id=user_id, turn_id=submitting.turn_id)
    assert accepted.state == accepted.terminal_kind == "abandoned"
    assert accepted.accepted_at is not None
    assert accepted.origin_chat_unavailable_at is not None
    assert accepted.origin_chat_unavailable_reason == "deleted"
    assert accepted.rejection_reason is None
    assert accepted.message_id is None
    assert accepted.acceptance_commit_id is None
    assert accepted.result_commit_id is None
    assert accepted.operation_id == str(staged.claim.operation.operation_id)
    assert (
        coordinator.query_operation(
            owner=staged.owner,
            operation_id=staged.claim.operation.operation_id,
        ).state
        is OperationState.RUNNING
    )

    replayed_rejection = repository.reject_transcript(
        user_id=user_id,
        turn_id=unaccepted.turn_id,
        reason="chat_unavailable",
        retry_policy="explicit_user_retry",
        now=NOW + timedelta(seconds=4),
    )
    assert replayed_rejection.replayed
    rejected_wire = asyncio.run(
        _replay_disposition(
            repository,
            user_id=user_id,
            turn=rejected,
        )
    )
    accepted_wire = asyncio.run(
        _replay_disposition(
            repository,
            user_id=user_id,
            turn=accepted,
        )
    )
    assert rejected_wire["type"] == "voice_submission_rejected"
    assert rejected_wire["retry_policy"] == "explicit_user_retry"
    assert accepted_wire["type"] == "voice_submission_rejected"
    assert accepted_wire["retry_policy"] == "none"
    assert accepted_wire["reason"] == "chat_unavailable"

    replay = _history(database).delete_chat(chat_id, user_id=user_id)
    assert replay.replayed
    assert not replay.chat_deleted
    assert replay.accepted_turn_ids == ()


def test_physical_delete_failure_rolls_back_the_voice_fence(
    database: Database,
) -> None:
    user_id = f"voice-delete-rollback-{uuid.uuid4().hex}"
    chat_id = str(uuid.uuid4())
    _insert_chat(database, user_id=user_id, chat_id=chat_id)
    repository = VoiceSessionRepository(database)
    session = _create_session(
        database,
        repository,
        user_id=user_id,
        chat_id=chat_id,
    )
    submitting = _insert_turn(
        database,
        repository,
        session=session,
        chat_id=chat_id,
        chat_context_revision=1,
        state="submitting",
    )
    coordinator = _coordinator(database)
    staged = _accept_and_stage(
        database,
        repository,
        coordinator,
        turn=submitting,
        content="rollback deletion",
    )
    database.execute(
        "CREATE TABLE voice_delete_blocker_065 ("
        "chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE RESTRICT)"
    )
    database.execute(
        "INSERT INTO voice_delete_blocker_065 (chat_id) VALUES (?)",
        (chat_id,),
    )
    try:
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            _history(database).delete_chat(chat_id, user_id=user_id)

        assert (
            database.fetch_one(
                "SELECT id FROM chats WHERE id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            is not None
        )
        live = repository.get_session(
            user_id=user_id,
            session_id=session.session_id,
        )
        assert live.ended_at is None
        assert live.chat_unavailable_at is None
        accepted = repository.get_turn(
            user_id=user_id,
            turn_id=submitting.turn_id,
        )
        assert accepted.state == "processing"
        assert accepted.origin_chat_unavailable_at is None
        assert accepted.origin_chat_unavailable_reason is None
        assert database.fetch_one(
            "SELECT state, execution_base_commit_id FROM conversation_commit "
            "WHERE commit_id = ?",
            (staged.result_commit_id,),
        ) == {
            "state": "staged",
            "execution_base_commit_id": staged.acceptance_commit_id,
        }
        for table in ("messages", "saved_components", "workspace_layout"):
            assert (
                database.fetch_one(
                    f"SELECT COUNT(*) AS count FROM {table} "
                    "WHERE conversation_commit_id = ?",
                    (staged.result_commit_id,),
                )["count"]
                == 1
            )
        assert (
            coordinator.query_operation(
                owner=staged.owner,
                operation_id=staged.claim.operation.operation_id,
            ).state
            is OperationState.RUNNING
        )
    finally:
        database.execute("DROP TABLE IF EXISTS voice_delete_blocker_065")

    assert (
        _history(database)
        .delete_chat(
            chat_id,
            user_id=user_id,
        )
        .chat_deleted
    )


def test_old_origin_revocation_preserves_current_session_and_other_stage(
    database: Database,
) -> None:
    user_id = f"voice-revoke-{uuid.uuid4().hex}"
    old_chat_id = str(uuid.uuid4())
    current_chat_id = str(uuid.uuid4())
    _insert_chat(database, user_id=user_id, chat_id=old_chat_id)
    _insert_chat(database, user_id=user_id, chat_id=current_chat_id)
    repository = VoiceSessionRepository(database)
    session = _create_session(
        database,
        repository,
        user_id=user_id,
        chat_id=old_chat_id,
    )
    old_unaccepted = _insert_turn(
        database,
        repository,
        session=session,
        chat_id=old_chat_id,
        chat_context_revision=1,
        state="submitting",
    )
    old_submitting = _insert_turn(
        database,
        repository,
        session=session,
        chat_id=old_chat_id,
        chat_context_revision=1,
        state="submitting",
    )
    coordinator = _coordinator(database)
    old_stage = _accept_and_stage(
        database,
        repository,
        coordinator,
        turn=old_submitting,
        content="old chat request",
    )

    database.execute(
        "UPDATE voice_session SET visible_chat_id = ?, chat_context_revision = 2, "
        "applied_visible_chat_id = ?, applied_chat_context_revision = 2, "
        "updated_at = ? WHERE session_id = ?",
        (
            current_chat_id,
            current_chat_id,
            NOW + timedelta(seconds=3),
            session.session_id,
        ),
    )
    current_session = repository.get_session(
        user_id=user_id,
        session_id=session.session_id,
    )
    current_submitting = _insert_turn(
        database,
        repository,
        session=current_session,
        chat_id=current_chat_id,
        chat_context_revision=2,
        state="submitting",
    )
    current_stage = _accept_and_stage(
        database,
        repository,
        coordinator,
        turn=current_submitting,
        content="current chat request",
    )

    receipt = _history(database).mark_chat_authorization_unavailable(
        old_chat_id,
        user_id=user_id,
    )

    assert not receipt.chat_deleted
    assert not receipt.replayed
    assert receipt.reason == "access_revoked"
    assert receipt.ended_sessions == ()
    assert receipt.unaccepted_turn_ids == (old_unaccepted.turn_id,)
    assert receipt.accepted_turn_ids == (old_submitting.turn_id,)
    assert receipt.aborted_result_commit_ids == (old_stage.result_commit_id,)
    assert receipt.announcement_session_keys == (
        (session.session_id, session.generation),
    )
    assert (
        database.fetch_one(
            "SELECT id FROM chats WHERE id = ? AND user_id = ?",
            (old_chat_id, user_id),
        )
        is not None
    )

    live = repository.get_session(
        user_id=user_id,
        session_id=session.session_id,
    )
    assert live.ended_at is None
    assert live.chat_unavailable_at is None
    assert live.visible_chat_id == current_chat_id

    rejected = repository.get_turn(
        user_id=user_id,
        turn_id=old_unaccepted.turn_id,
    )
    assert rejected.rejection_reason == "chat_unavailable"
    assert rejected.rejection_retry_policy == "explicit_user_retry"
    old_accepted = repository.get_turn(
        user_id=user_id,
        turn_id=old_submitting.turn_id,
    )
    assert old_accepted.origin_chat_unavailable_reason == "access_revoked"
    assert old_accepted.state == "abandoned"
    assert (
        coordinator.query_operation(
            owner=old_stage.owner,
            operation_id=old_stage.claim.operation.operation_id,
        ).state
        is OperationState.RUNNING
    )

    old_commit = database.fetch_one(
        "SELECT state, execution_base_commit_id FROM conversation_commit "
        "WHERE commit_id = ?",
        (old_stage.result_commit_id,),
    )
    assert old_commit == {
        "state": "aborted",
        "execution_base_commit_id": None,
    }
    for table in ("messages", "saved_components", "workspace_layout"):
        assert (
            database.fetch_one(
                f"SELECT COUNT(*) AS count FROM {table} "
                "WHERE conversation_commit_id = ?",
                (old_stage.result_commit_id,),
            )["count"]
            == 0
        )

    assert (
        database.fetch_one(
            "SELECT state FROM conversation_commit WHERE commit_id = ?",
            (current_stage.result_commit_id,),
        )["state"]
        == "staged"
    )
    for table in ("messages", "saved_components", "workspace_layout"):
        assert (
            database.fetch_one(
                f"SELECT COUNT(*) AS count FROM {table} "
                "WHERE conversation_commit_id = ?",
                (current_stage.result_commit_id,),
            )["count"]
            == 1
        )
    assert (
        repository.get_turn(
            user_id=user_id,
            turn_id=current_submitting.turn_id,
        ).state
        == "processing"
    )

    replay = _history(database).mark_chat_authorization_unavailable(
        old_chat_id,
        user_id=user_id,
    )
    assert replay.replayed
    assert replay.accepted_turn_ids == ()
    assert replay.aborted_result_commit_ids == ()

    deleted = _history(database).delete_chat(old_chat_id, user_id=user_id)
    assert deleted.chat_deleted
    old_tombstone = repository.get_turn(
        user_id=user_id,
        turn_id=old_submitting.turn_id,
    )
    assert old_tombstone.origin_chat_unavailable_reason == "access_revoked"
    assert (
        repository.get_session(
            user_id=user_id,
            session_id=session.session_id,
        ).ended_at
        is None
    )
    assert (
        database.fetch_one(
            "SELECT state FROM conversation_commit WHERE commit_id = ?",
            (current_stage.result_commit_id,),
        )["state"]
        == "staged"
    )

    foreign = _history(database).mark_chat_authorization_unavailable(
        current_chat_id,
        user_id=f"other-{uuid.uuid4().hex}",
    )
    assert foreign.replayed
    assert (
        database.fetch_one(
            "SELECT id FROM chats WHERE id = ? AND user_id = ?",
            (current_chat_id, user_id),
        )
        is not None
    )
