"""Concurrent voice acceptance and terminal publication proofs for 065."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

import psycopg2
import pytest
from psycopg2 import sql

from orchestrator.async_tasks import DurableUserTurnWebSocket
from orchestrator.conversation_publication import (
    ConversationCompletionSummary,
    ConversationPublicationStage,
    activate_conversation_publication,
    canonical_components_sha256,
    canonical_layouts_sha256,
    completion_summary_from_content,
    merge_conversation_publication,
    reset_conversation_publication,
)
from orchestrator.history import (
    ConversationCommitRepository,
    HistoryManager,
)
from orchestrator.history import ConversationSnapshotInvalid
from orchestrator.voice_coordinator import RecognitionStart
from orchestrator.voice_sessions import (
    CreateSession,
    TranscriptSubmission,
    VoiceSessionRepository,
)
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
from shared.voice_transcript import (
    TranscriptProofBinding,
    issue_transcript_proof,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    params = psycopg2.extensions.parse_dsn(_build_database_url())
    name = f"astraldeep_voice_publication_{uuid.uuid4().hex}"
    try:
        admin = psycopg2.connect(**params)
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
            )
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
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(name)
                    )
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
            config_revision="voice-publication-065",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.INTERACTIVE,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=8,
            queue_limit=8,
            max_wait_ms=5_000,
            config_revision="voice-publication-065",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.VOICE_INTERACTIVE,
            parent_class_name=AdmissionClass.INTERACTIVE,
            active_limit=4,
            queue_limit=0,
            max_wait_ms=0,
            config_revision="voice-publication-065",
        ),
    )


def _coordinator(database: Database) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=_classes(),
        repository=PostgresWorkAdmissionRepository(database),
        operation_retention=timedelta(hours=24),
    )


def _create_active_session(
    repository: VoiceSessionRepository,
    database: Database,
    *,
    user_id: str,
    chat_id: str,
):
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'Concurrent voice', 1, 1)",
        (chat_id, user_id),
    )
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
    session = repository.create_session(request, now=NOW).session
    asyncio.run(
        repository.claim_control_lease(
            user_id=user_id,
            session_id=session.session_id,
            generation=session.generation,
            owner_id="publication-test",
            now=NOW,
        )
    )
    repository.assign_worker(
        user_id=user_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        assignment_id=str(uuid.uuid4()),
        worker_identity=f"worker-{uuid.uuid4().hex}",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    repository.apply_chat_context(
        user_id=user_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        expected_media_grant_revision=session.media_grant_revision,
        control_owner_id="publication-test",
        visible_chat_id=chat_id,
        chat_context_revision=session.chat_context_revision,
        now=NOW,
    )
    return repository.mark_session_active(
        user_id=user_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        expected_media_grant_revision=session.media_grant_revision,
        now=NOW,
    )


def _admit_turn(
    repository: VoiceSessionRepository,
    session,
    *,
    text: str,
    now: datetime,
):
    start = RecognitionStart(
        session_id=session.session_id,
        generation=session.generation,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
        client_turn_id=str(uuid.uuid4()),
        media_grant_revision=session.media_grant_revision,
        chat_id=session.visible_chat_id,
        chat_context_revision=session.chat_context_revision,
    )
    bound = asyncio.run(
        repository.bind_worker_recognition(
            start=start,
            control_owner_id="publication-test",
            now=now,
        )
    ).turn
    binding = TranscriptProofBinding(
        session_id=session.session_id,
        generation=session.generation,
        media_grant_revision=session.media_grant_revision,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
        turn_id=bound.turn_id,
        client_turn_id=bound.client_turn_id,
        submission_id=bound.submission_id,
        request_generation=bound.request_generation,
        chat_id=bound.chat_id,
        chat_context_revision=bound.chat_context_revision,
        detected_language="en",
    )
    proof = issue_transcript_proof(b"p" * 32, binding, text, now=now)
    return repository.admit_transcript(
        TranscriptSubmission(
            user_id=session.user_id,
            session_id=session.session_id,
            generation=session.generation,
            media_grant_revision=session.media_grant_revision,
            turn_id=bound.turn_id,
            client_turn_id=bound.client_turn_id,
            submission_id=bound.submission_id,
            request_generation=bound.request_generation,
            chat_id=bound.chat_id,
            chat_context_revision=bound.chat_context_revision,
            source_participant_identity=session.worker_identity,
            detected_language="en",
            text=proof.canonical_text,
            text_digest_sha256=proof.text_digest_sha256,
            transcript_proof=proof.transcript_proof,
            proof_expires_at=proof.proof_expires_at,
        ),
        worker_control_secret=b"p" * 32,
        now=now + timedelta(milliseconds=100),
    )


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
        AdmissionClass.VOICE_INTERACTIVE, result.operation_id
    )
    assert claim is not None
    return owner, connection_generation, claim


def _component(component_id: str, content: str) -> dict[str, str]:
    return {
        "type": "text",
        "component_id": component_id,
        "content": content,
    }


def _write_candidate(
    database: Database,
    *,
    commit_id: str,
    revision: int,
    components: list[dict[str, str]],
) -> None:
    database.execute(
        "DELETE FROM saved_components WHERE conversation_commit_id = ?",
        (commit_id,),
    )
    row = database.fetch_one(
        "SELECT chat_id, owner_user_id FROM conversation_commit "
        "WHERE commit_id = ?",
        (commit_id,),
    )
    assert row is not None
    for position, component in enumerate(components):
        database.execute(
            "INSERT INTO saved_components ("
            "id, chat_id, user_id, component_data, component_type, title, "
            "created_at, component_id, position, updated_at, "
            "conversation_commit_id, committed_render_revision"
            ") VALUES (?, ?, ?, ?, 'text', ?, 2, ?, ?, 2, ?, ?)",
            (
                str(uuid.uuid4()),
                row["chat_id"],
                row["owner_user_id"],
                json.dumps(component, sort_keys=True),
                component["content"],
                component["component_id"],
                position,
                commit_id,
                revision,
            ),
        )


def test_merge_is_deterministic_and_invalid_candidate_layout_falls_back() -> None:
    base = [_component("shared", "base")]
    candidate = [_component("shared", "candidate")]
    latest = [_component("shared", "latest"), _component("other", "new")]
    base_layout = [{
        "layout_key": "main",
        "position": 0,
        "layout": [{"type": "ref", "component_id": "shared"}],
    }]
    invalid_candidate_layout = [{
        "layout_key": "main",
        "position": 0,
        "layout": [{"type": "ref", "component_id": "missing"}],
    }]

    merged = merge_conversation_publication(
        base_components=base,
        candidate_components=candidate,
        latest_components=latest,
        base_layouts=base_layout,
        candidate_layouts=invalid_candidate_layout,
        latest_layouts=base_layout,
    )

    assert list(merged.components) == latest
    assert list(merged.layouts) == base_layout
    assert merged.component_conflicts == ("shared",)
    assert merged.layout_conflicts == ("main",)
    assert canonical_components_sha256(base) == canonical_components_sha256(
        [{"content": "base", "component_id": "shared", "type": "text"}]
    )

    flat = merge_conversation_publication(
        base_components=base,
        candidate_components=[],
        latest_components=base,
        base_layouts=base_layout,
        candidate_layouts=invalid_candidate_layout,
        latest_layouts=[],
    )
    assert flat.components == ()
    assert flat.layouts == ()
    assert flat.layout_conflicts == ("main",)


def test_three_view_merge_rebases_non_conflicting_additions_and_deletions() -> None:
    base_components = [
        _component("keep", "base"),
        _component("delete", "base"),
    ]
    candidate_components = [
        _component("keep", "base"),
        _component("candidate-only", "candidate"),
    ]
    latest_components = [
        _component("keep", "latest"),
        _component("delete", "base"),
        _component("latest-only", "latest"),
    ]
    base_layouts = [
        {
            "layout_key": "delete-layout",
            "position": 0,
            "layout": [{"type": "ref", "component_id": "delete"}],
        }
    ]
    candidate_layouts = [
        {
            "layout_key": "candidate-layout",
            "position": 1,
            "layout": [
                {"type": "ref", "component_id": "candidate-only"}
            ],
        }
    ]

    merged = merge_conversation_publication(
        base_components=base_components,
        candidate_components=candidate_components,
        latest_components=latest_components,
        base_layouts=base_layouts,
        candidate_layouts=candidate_layouts,
        latest_layouts=base_layouts,
    )

    assert list(merged.components) == [
        _component("keep", "latest"),
        _component("latest-only", "latest"),
        _component("candidate-only", "candidate"),
    ]
    assert list(merged.layouts) == candidate_layouts
    assert merged.component_conflicts == ()
    assert merged.layout_conflicts == ()


def test_atomic_completion_summary_requires_both_explicit_fields() -> None:
    summary = ConversationCompletionSummary(
        summary_text="  The report is complete.  ",
        summary_source="generated_tool_summary",
    )
    assert summary.summary_text == "The report is complete."
    assert completion_summary_from_content(
        {
            "type": "text",
            "summary_text": summary.summary_text,
            "summary_source": summary.summary_source,
        }
    ) == summary
    assert completion_summary_from_content(
        {"type": "text", "summary": "legacy 200-character scrape"}
    ) is None
    assert completion_summary_from_content(
        {"type": "text", "summary_text": "partial"}
    ) is None

    stage = ConversationPublicationStage(
        history=object(),
        commit_id=str(uuid.uuid4()),
        chat_id=str(uuid.uuid4()),
        user_id="summary-owner",
        base_render_revision=0,
        next_render_revision=1,
    )
    stage.set_completion_summary(
        text=summary.summary_text,
        source=summary.summary_source,
    )
    assert stage.summary_text == summary.summary_text
    assert stage.summary_source == summary.summary_source
    with pytest.raises(ValueError, match="supplied together"):
        ConversationPublicationStage(
            history=object(),
            commit_id=str(uuid.uuid4()),
            chat_id=str(uuid.uuid4()),
            user_id="summary-owner",
            base_render_revision=0,
            next_render_revision=1,
            summary_text="missing source",
        )


def test_two_voice_results_overlap_and_publish_once_in_completion_order(
    database: Database,
) -> None:
    user_id = f"voice-publication-{uuid.uuid4().hex}"
    chat_id = str(uuid.uuid4())
    voice = VoiceSessionRepository(database)
    session = _create_active_session(
        voice, database, user_id=user_id, chat_id=chat_id
    )
    database.execute(
        "INSERT INTO saved_components ("
        "id, chat_id, user_id, component_data, component_type, title, "
        "created_at, component_id, position, updated_at"
        ") VALUES (?, ?, ?, ?, 'text', 'base', 1, 'shared', 0, 1)",
        (
            str(uuid.uuid4()),
            chat_id,
            user_id,
            json.dumps(_component("shared", "base"), sort_keys=True),
        ),
    )
    first_turn = _admit_turn(
        voice, session, text="first request", now=NOW + timedelta(seconds=1)
    )
    second_turn = _admit_turn(
        voice, session, text="second request", now=NOW + timedelta(seconds=2)
    )
    assert first_turn.turn.result_request_generation is not None
    assert second_turn.turn.result_request_generation is not None

    coordinator = _coordinator(database)
    commits = ConversationCommitRepository(
        database, operation_coordinator=coordinator
    )
    owner1, connection1, claim1 = _claim(
        coordinator,
        user_id=user_id,
        chat_id=chat_id,
        request_generation=first_turn.turn.request_generation,
    )
    owner2, connection2, claim2 = _claim(
        coordinator,
        user_id=user_id,
        chat_id=chat_id,
        request_generation=second_turn.turn.request_generation,
    )
    acceptance_calls: list[str] = []

    def accept(admission, connection_generation, claim):
        def callback(**correlation):
            acceptance_calls.append(admission.turn.turn_id)
            return voice.accept_transcript(
                user_id=user_id,
                turn_id=admission.turn.turn_id,
                message_id=correlation["message_id"],
                accepted_connection_generation=str(connection_generation),
                acceptance_commit_id=correlation["acceptance_commit_id"],
                result_commit_id=correlation["result_commit_id"],
                operation_id=str(claim.operation.operation_id),
                now=NOW + timedelta(seconds=3),
                transaction=correlation["cursor"],
            )

        return callback

    first = commits.accept_voice_turn(
        chat_id=chat_id,
        owner_user_id=user_id,
        request_generation=first_turn.turn.request_generation,
        result_request_generation=(
            first_turn.turn.result_request_generation
        ),
        connection_generation=connection1,
        user_content="first request",
        operation_fence=claim1.fence,
        operation_owner=owner1,
        accept_turn=accept(first_turn, connection1, claim1),
    )
    second = commits.accept_voice_turn(
        chat_id=chat_id,
        owner_user_id=user_id,
        request_generation=second_turn.turn.request_generation,
        result_request_generation=(
            second_turn.turn.result_request_generation
        ),
        connection_generation=connection2,
        user_content="second request",
        operation_fence=claim2.fence,
        operation_owner=owner2,
        accept_turn=accept(second_turn, connection2, claim2),
    )
    assert first["acceptance"]["committed_render_revision"] == 1
    assert second["acceptance"]["committed_render_revision"] == 2

    history = object.__new__(HistoryManager)
    history.db = database
    stage = ConversationPublicationStage(
        history=history,
        commit_id=first["result"]["commit_id"],
        chat_id=chat_id,
        user_id=user_id,
        base_render_revision=1,
        next_render_revision=2,
        operation_fence=claim1.fence,
        publication_role="assistant_result",
        execution_base_render_revision=1,
    )
    token = activate_conversation_publication(stage)
    try:
        isolated = history.get_chat(chat_id, user_id=user_id)
    finally:
        reset_conversation_publication(token)
    assert [message["content"] for message in isolated["messages"]] == [
        "first request"
    ]

    first_candidate = [
        _component("shared", "first"),
        _component("only-first", "first-only"),
    ]
    second_candidate = [
        _component("shared", "second"),
        _component("only-second", "second-only"),
    ]
    _write_candidate(
        database,
        commit_id=first["result"]["commit_id"],
        revision=2,
        components=first_candidate,
    )
    _write_candidate(
        database,
        commit_id=second["result"]["commit_id"],
        revision=3,
        components=second_candidate,
    )
    commits.append_staged_message(
        commit_id=first["result"]["commit_id"],
        owner_user_id=user_id,
        role="assistant",
        content=[{"type": "text", "content": "first completed"}],
        operation_fence=claim1.fence,
    )
    commits.append_staged_message(
        commit_id=second["result"]["commit_id"],
        owner_user_id=user_id,
        role="assistant",
        content=[{"type": "text", "content": "second completed"}],
        operation_fence=claim2.fence,
    )

    second_commit = commits.publish_voice_result(
        commit_id=second["result"]["commit_id"],
        owner_user_id=user_id,
        canvas_components=second_candidate,
        canvas_layouts=[],
        operation_fence=claim2.fence,
    )
    assert second_commit["committed_render_revision"] == 3
    assert coordinator.query_operation(
        owner=owner2, operation_id=claim2.operation.operation_id
    ).state is OperationState.COMPLETED
    assert coordinator.query_operation(
        owner=owner1, operation_id=claim1.operation.operation_id
    ).state is OperationState.RUNNING

    first_commit = commits.publish_voice_result(
        commit_id=first["result"]["commit_id"],
        owner_user_id=user_id,
        canvas_components=first_candidate,
        canvas_layouts=[],
        operation_fence=claim1.fence,
    )
    assert first_commit["committed_render_revision"] == 4
    assert first_commit["publication_rebase_count"] == 1
    assert acceptance_calls == [
        first_turn.turn.turn_id,
        second_turn.turn.turn_id,
    ]
    assert coordinator.query_operation(
        owner=owner1, operation_id=claim1.operation.operation_id
    ).state is OperationState.COMPLETED

    snapshot = commits.build_snapshot(
        chat_id=chat_id,
        owner_user_id=user_id,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.UUID(
            first_turn.turn.result_request_generation
        ),
        snapshot_purpose="commit",
    )
    assert snapshot["render_revision"] == 4
    assert [
        component["content"] for component in snapshot["canvas"]["components"]
    ] == ["second", "second-only", "first-only"]
    assert [
        part["text"]
        for message in snapshot["transcript"]
        for part in message["parts"]
        if part["type"] == "text"
    ] == [
        "first request",
        "second request",
        "second completed",
        "first completed",
    ]
    first_content = commits.committed_assistant_content(
        commit_id=first["result"]["commit_id"],
        owner_user_id=user_id,
    )
    assert first_content[-1]["type"] == "alert"
    assert "newer canvas version was preserved" in first_content[-1][
        "message"
    ]
    linked = database.fetch_one(
        "SELECT message_id, acceptance_commit_id, result_commit_id, "
        "operation_id, result_request_generation FROM voice_turn "
        "WHERE turn_id = ?",
        (first_turn.turn.turn_id,),
    )
    assert linked == {
        "message_id": first["message_id"],
        "acceptance_commit_id": first["acceptance"]["commit_id"],
        "result_commit_id": first["result"]["commit_id"],
        "operation_id": str(claim1.operation.operation_id),
        "result_request_generation": (
            first_turn.turn.result_request_generation
        ),
    }
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM saved_components WHERE chat_id = ?",
        (chat_id,),
    )["count"] == 8


def test_voice_acceptance_rolls_back_every_link_when_correlation_fails(
    database: Database,
) -> None:
    user_id = f"voice-rollback-{uuid.uuid4().hex}"
    chat_id = str(uuid.uuid4())
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'Rollback', 1, 1)",
        (chat_id, user_id),
    )
    coordinator = _coordinator(database)
    commits = ConversationCommitRepository(
        database, operation_coordinator=coordinator
    )
    request_generation = str(uuid.uuid4())
    owner, connection_generation, claim = _claim(
        coordinator,
        user_id=user_id,
        chat_id=chat_id,
        request_generation=request_generation,
    )

    def fail_correlation(**_kwargs):
        raise RuntimeError("correlation failed")

    with pytest.raises(RuntimeError, match="correlation failed"):
        commits.accept_voice_turn(
            chat_id=chat_id,
            owner_user_id=user_id,
            request_generation=request_generation,
            result_request_generation=uuid.uuid4(),
            connection_generation=connection_generation,
            user_content="must not survive",
            operation_fence=claim.fence,
            operation_owner=owner,
            accept_turn=fail_correlation,
        )
    assert database.fetch_one(
        "SELECT render_revision, conversation_commit_id FROM chats "
        "WHERE id = ?",
        (chat_id,),
    ) == {"render_revision": 0, "conversation_commit_id": None}
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM conversation_commit WHERE chat_id = ?",
        (chat_id,),
    )["count"] == 0
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM messages WHERE chat_id = ?",
        (chat_id,),
    )["count"] == 0
    coordinator.terminalize(
        claim.fence,
        state=OperationState.FAILED,
        terminal_code="operation_failed",
        safe_summary="Rolled back",
        retry_after_ms=None,
    )


def test_acceptance_copies_full_workspace_and_abort_cleans_only_private_result(
    database: Database,
) -> None:
    user_id = f"voice-abort-{uuid.uuid4().hex}"
    chat_id = str(uuid.uuid4())
    component = _component("copied-component", "authoritative")
    layout = {
        "layout_key": "copied-layout",
        "position": 0,
        "layout": [
            {"type": "ref", "component_id": "copied-component"}
        ],
    }
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'Copy and abort', 1, 1)",
        (chat_id, user_id),
    )
    database.execute(
        "INSERT INTO saved_components ("
        "id, chat_id, user_id, component_data, component_type, title, "
        "created_at, component_id, position, updated_at"
        ") VALUES (?, ?, ?, ?, 'text', 'authoritative', 1, "
        "'copied-component', 0, 1)",
        (str(uuid.uuid4()), chat_id, user_id, json.dumps(component)),
    )
    database.execute(
        "INSERT INTO workspace_layout ("
        "chat_id, user_id, layout_key, position, layout, created_at, "
        "updated_at) VALUES (?, ?, 'copied-layout', 0, ?, 1, 1)",
        (chat_id, user_id, json.dumps(layout["layout"])),
    )
    coordinator = _coordinator(database)
    commits = ConversationCommitRepository(
        database, operation_coordinator=coordinator
    )
    request_generation = str(uuid.uuid4())
    owner, connection_generation, claim = _claim(
        coordinator,
        user_id=user_id,
        chat_id=chat_id,
        request_generation=request_generation,
    )
    accepted = commits.accept_voice_turn(
        chat_id=chat_id,
        owner_user_id=user_id,
        request_generation=request_generation,
        result_request_generation=uuid.uuid4(),
        connection_generation=connection_generation,
        user_content="copy this view",
        operation_fence=claim.fence,
        operation_owner=owner,
        accept_turn=lambda **correlation: correlation.copy(),
    )
    acceptance_id = accepted["acceptance"]["commit_id"]
    result_id = accepted["result"]["commit_id"]
    assert accepted["components"] == [component]
    assert accepted["layouts"] == [layout]
    assert database.fetch_one(
        "SELECT publication_role, execution_base_commit_id FROM "
        "conversation_commit WHERE commit_id = ?",
        (acceptance_id,),
    ) == {
        "publication_role": "user_acceptance",
        "execution_base_commit_id": None,
    }
    result_anchor = database.fetch_one(
        "SELECT publication_role, parent_commit_id, "
        "execution_base_commit_id, execution_base_render_revision, "
        "execution_base_components_sha256, execution_base_layouts_sha256 "
        "FROM conversation_commit WHERE commit_id = ?",
        (result_id,),
    )
    assert result_anchor == {
        "publication_role": "assistant_result",
        "parent_commit_id": acceptance_id,
        "execution_base_commit_id": acceptance_id,
        "execution_base_render_revision": 1,
        "execution_base_components_sha256": canonical_components_sha256(
            accepted["components"]
        ),
        "execution_base_layouts_sha256": canonical_layouts_sha256(
            accepted["layouts"]
        ),
    }
    snapshot = commits.build_snapshot(
        chat_id=chat_id,
        owner_user_id=user_id,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.UUID(request_generation),
        snapshot_purpose="commit",
    )
    assert [message["role"] for message in snapshot["transcript"]] == [
        "user"
    ]
    assert snapshot["canvas"]["components"] == [component]
    acceptance_layout = database.fetch_one(
        "SELECT layout FROM workspace_layout WHERE "
        "conversation_commit_id = ? AND layout_key = 'copied-layout'",
        (acceptance_id,),
    )
    assert json.loads(acceptance_layout["layout"]) == layout["layout"]

    # An impossible mutation of the immutable acceptance view must fail closed
    # against the digest stored on the private result anchor.
    tampered = _component("copied-component", "tampered")
    database.execute(
        "UPDATE saved_components SET component_data = ? WHERE "
        "conversation_commit_id = ? AND component_id = 'copied-component'",
        (json.dumps(tampered), acceptance_id),
    )
    with pytest.raises(
        ConversationSnapshotInvalid,
        match="execution base digest changed",
    ):
        commits.publish_voice_result(
            commit_id=result_id,
            owner_user_id=user_id,
            canvas_components=accepted["components"],
            canvas_layouts=accepted["layouts"],
            operation_fence=claim.fence,
        )

    aborted = commits.abort_commit(
        commit_id=result_id,
        owner_user_id=user_id,
    )
    assert aborted["state"] == "aborted"
    assert database.fetch_one(
        "SELECT execution_base_commit_id FROM conversation_commit "
        "WHERE commit_id = ?",
        (result_id,),
    )["execution_base_commit_id"] is None
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM saved_components WHERE "
        "conversation_commit_id = ?",
        (result_id,),
    )["count"] == 0
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM workspace_layout WHERE "
        "conversation_commit_id = ?",
        (result_id,),
    )["count"] == 0
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM saved_components WHERE "
        "conversation_commit_id = ?",
        (acceptance_id,),
    )["count"] == 1
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM workspace_layout WHERE "
        "conversation_commit_id = ?",
        (acceptance_id,),
    )["count"] == 1
    coordinator.terminalize(
        claim.fence,
        state=OperationState.FAILED,
        terminal_code="operation_failed",
        safe_summary="Aborted",
        retry_after_ms=None,
    )


@pytest.mark.asyncio
async def test_durable_execution_socket_forwards_only_rejection_and_scrubs() -> None:
    class Origin:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_text(self, data: str) -> None:
            self.sent.append(json.loads(data))

    origin = Origin()
    socket = DurableUserTurnWebSocket(origin, user_id="owner-065")
    assert socket.llm_context_user_id == "owner-065"
    await socket.send_json({"type": "chat_status", "message": "secret"})
    await socket.send_json({"type": "voice_submission_rejected", "reason": "x"})
    assert origin.sent == [
        {"type": "voice_submission_rejected", "reason": "x"}
    ]
    socket.scrub()
    assert socket.llm_context_user_id is None
    assert "owner-065" not in repr(socket)
    await socket.send_json({"type": "voice_submission_rejected"})
    assert len(origin.sent) == 1


def test_source_contains_no_legacy_result_scrape() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "orchestrator" / "orchestrator.py"
    ).read_text(encoding="utf-8")
    finish = source.split("async def _finish_voice_chat_dispatch", 1)[1]
    finish = finish.split("async def handle_chat_message", 1)[0]
    assert "committed_assistant_content" in finish
    assert "item[\"id\"] > turn.message_id" not in finish
