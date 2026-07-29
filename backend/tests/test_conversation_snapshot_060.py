"""Feature 060 atomic conversation commit and snapshot contract tests.

The integration cases use a throwaway PostgreSQL database. They prove that
staged/incomplete work is invisible, publication is one transaction, and a
snapshot is built from one owner-scoped repeatable view.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Iterator

import psycopg2
import pytest
from psycopg2 import sql

from orchestrator.history import (
    ConversationCommitConflict,
    ConversationCommitRepository,
    ConversationNotFound,
    ConversationSnapshotInvalid,
    HistoryManager,
    augment_conversation_snapshot_for_target,
)
from orchestrator.conversation_publication import reset_conversation_publication
from orchestrator.orchestrator import Orchestrator
from orchestrator.workspace import WorkspaceManager
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    PostgresWorkAdmissionRepository,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
from rote.capabilities import DeviceProfile
from shared.database import Database, _build_database_url


OWNER = "conversation-owner-060"
CHAT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_CHAT_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[Database]:
    params = psycopg2.extensions.parse_dsn(_build_database_url())
    name = f"astraldeep_conversation_{uuid.uuid4().hex}"
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
    dsn = psycopg2.extensions.make_dsn(**database_params)
    prior_pool_setting = os.environ.get("DB_POOL_DISABLE")
    os.environ["DB_POOL_DISABLE"] = "1"
    try:
        yield Database(dsn)
    finally:
        if prior_pool_setting is None:
            os.environ.pop("DB_POOL_DISABLE", None)
        else:
            os.environ["DB_POOL_DISABLE"] = prior_pool_setting
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


@pytest.fixture
def database(postgres_database: Database) -> Database:
    connection = postgres_database._get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM message_attachment")
            cursor.execute("DELETE FROM user_attachments")
            cursor.execute("DELETE FROM workspace_layout")
            cursor.execute("DELETE FROM saved_components")
            cursor.execute("DELETE FROM messages")
            cursor.execute("UPDATE chats SET conversation_commit_id = NULL")
            cursor.execute("DELETE FROM conversation_commit")
            cursor.execute("DELETE FROM chats")
            cursor.execute("DELETE FROM operation_submission_result")
            cursor.execute(
                "UPDATE operation_admission_slot SET operation_id = NULL, "
                "lease_token = NULL, lease_expires_at = NULL"
            )
            cursor.execute("DELETE FROM operation_record")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return postgres_database


def _create_chat(database: Database, chat_id: str = CHAT_ID, owner: str = OWNER) -> None:
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'Conversation 060', ?, ?)",
        (chat_id, owner, 1_752_664_800_000, 1_752_664_860_000),
    )


def _repository(database: Database, coordinator=None) -> ConversationCommitRepository:
    return ConversationCommitRepository(database, operation_coordinator=coordinator)


def _snapshot(repository: ConversationCommitRepository, **overrides):
    values = {
        "chat_id": CHAT_ID,
        "owner_user_id": OWNER,
        "connection_generation": uuid.uuid4(),
        "request_generation": uuid.uuid4(),
        "snapshot_purpose": "hydration",
    }
    values.update(overrides)
    return repository.build_snapshot(**values)


def _coordinator(database: Database) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=4,
                queue_limit=0,
                max_wait_ms=None,
                config_revision="conversation-060",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=4,
                queue_limit=8,
                max_wait_ms=5_000,
                config_revision="conversation-060",
            ),
        ),
        repository=PostgresWorkAdmissionRepository(database),
        operation_retention=timedelta(hours=24),
    )


def _claim(
    coordinator: WorkAdmissionCoordinator,
    *,
    request_generation: uuid.UUID | None = None,
):
    owner = OperationOwner(OwnerScope.USER, OWNER, None)
    submission = uuid.uuid4()
    request = OperationRequest(
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=owner,
        submission_id=submission,
        idempotency_namespace="conversation_commit",
        idempotency_key=str(submission),
        normalized_input_digest="ab" * 32,
        chat_id=CHAT_ID,
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=request_generation or uuid.uuid4(),
    )
    accepted = coordinator.submit(request)
    assert accepted.accepted
    claim = coordinator.claim_operation(AdmissionClass.INTERACTIVE, accepted.operation_id)
    assert claim is not None
    return owner, claim


def test_legacy_revision_zero_is_coherent_visible_and_owner_scoped(database: Database) -> None:
    _create_chat(database)
    _create_chat(database, OTHER_CHAT_ID, "other-owner")
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'assistant', ?, ?)",
        (CHAT_ID, OWNER, "Unicode survives: 雪", 1_752_664_801_000),
    )
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'tool', ?, ?)",
        (
            CHAT_ID,
            OWNER,
            json.dumps(["first", 7, {"total": 21}], ensure_ascii=False),
            1_752_664_802_000,
        ),
    )
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'assistant', ?, ?)",
        (CHAT_ID, OWNER, "{malformed", 1_752_664_803_000),
    )
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'tool', ?, ?)",
        (CHAT_ID, OWNER, "null", 1_752_664_803_100),
    )
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'tool', ?, ?)",
        (CHAT_ID, OWNER, "[]", 1_752_664_803_200),
    )
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'tool', ?, ?)",
        (
            CHAT_ID,
            OWNER,
            json.dumps({"z": [2, 1], "a": {"ok": True}}),
            1_752_664_803_300,
        ),
    )
    component = {"type": "text", "content": "Legacy canvas"}
    database.execute(
        "INSERT INTO saved_components "
        "(id, chat_id, user_id, component_data, component_type, title, created_at, "
        "component_id, position, updated_at) VALUES (?, ?, ?, ?, 'text', ?, ?, ?, 0, ?)",
        (
            str(uuid.uuid4()),
            CHAT_ID,
            OWNER,
            json.dumps(component),
            "Legacy canvas",
            1_752_664_804_000,
            "legacy-canvas",
            1_752_664_804_000,
        ),
    )
    attachment_id = str(uuid.uuid4())
    database.execute(
        "INSERT INTO user_attachments "
        "(attachment_id, user_id, filename, content_type, category, extension, "
        "size_bytes, sha256, storage_path, created_at, deleted_at) "
        "VALUES (?, ?, 'evidence.txt', 'text/plain', 'document', '.txt', 4, ?, ?, ?, NULL)",
        (attachment_id, OWNER, "cd" * 32, "/synthetic/evidence.txt", 1_752_664_800_500),
    )
    first_id = database.fetch_one(
        "SELECT id FROM messages WHERE chat_id = ? ORDER BY id LIMIT 1", (CHAT_ID,)
    )["id"]
    database.execute(
        "INSERT INTO message_attachment "
        "(id, chat_id, message_id, attachment_id, user_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), CHAT_ID, str(first_id), attachment_id, OWNER, 1_752_664_801_500),
    )

    snapshot = _snapshot(_repository(database))

    assert set(snapshot) == {
        "type",
        "schema_version",
        "snapshot_id",
        "chat_id",
        "connection_generation",
        "request_generation",
        "snapshot_purpose",
        "render_revision",
        "committed_at",
        "transcript",
        "canvas",
    }
    assert snapshot["render_revision"] == 0
    assert snapshot["canvas"]["target"] == "canvas"
    assert snapshot["canvas"]["components"][0]["component_id"] == "legacy-canvas"
    assert snapshot["transcript"][0]["parts"] == [
        {"type": "text", "text": "Unicode survives: 雪"}
    ]
    assert snapshot["transcript"][0]["attachments"] == [
        {
            "attachment_id": attachment_id,
            "filename": "evidence.txt",
            "category": "document",
        }
    ]
    assert [part["type"] for part in snapshot["transcript"][1]["parts"]] == [
        "text",
        "structured",
        "structured",
    ]
    assert [part["type"] for part in snapshot["transcript"][2]["parts"]] == [
        "recovery",
        "structured",
    ]
    assert [part["type"] for part in snapshot["transcript"][3]["parts"]] == [
        "recovery",
        "structured",
    ]
    assert snapshot["transcript"][4]["parts"] == [
        {"type": "structured", "value": [], "plain_text": "[]"}
    ]
    assert snapshot["transcript"][5]["parts"] == [
        {
            "type": "structured",
            "value": {"a": {"ok": True}, "z": [2, 1]},
            "plain_text": "a: ok: true; z: 2, 1",
        }
    ]
    with pytest.raises(ConversationNotFound):
        _snapshot(_repository(database), owner_user_id="other-owner")


@pytest.mark.parametrize("boundary", ["after_messages", "after_canvas", "before_publish"])
def test_fault_at_each_publication_boundary_exposes_only_prior_commit(
    database: Database, boundary: str
) -> None:
    _create_chat(database)
    repository = _repository(database)
    commit = repository.stage_commit(
        chat_id=CHAT_ID,
        owner_user_id=OWNER,
        request_generation=uuid.uuid4(),
    )

    def fail(point: str) -> None:
        if point == boundary:
            raise RuntimeError(f"fault:{point}")

    with pytest.raises(RuntimeError, match=f"fault:{boundary}"):
        repository.publish_commit(
            commit_id=commit["commit_id"],
            owner_user_id=OWNER,
            messages=[{"role": "assistant", "content": "must stay invisible"}],
            canvas_components=[
                {"type": "text", "component_id": "candidate", "content": "candidate"}
            ],
            fault_hook=fail,
        )

    snapshot = _snapshot(repository)
    assert snapshot["render_revision"] == 0
    assert snapshot["transcript"] == []
    assert snapshot["canvas"] == {"target": "canvas", "components": []}
    row = database.fetch_one(
        "SELECT state FROM conversation_commit WHERE commit_id = ?",
        (commit["commit_id"],),
    )
    assert row["state"] == "staged"


def test_atomic_publish_advances_once_and_explicit_empty_canvas_clears(database: Database) -> None:
    _create_chat(database)
    repository = _repository(database)
    first = repository.stage_commit(
        chat_id=CHAT_ID,
        owner_user_id=OWNER,
        request_generation=uuid.uuid4(),
    )
    result = repository.publish_commit(
        commit_id=first["commit_id"],
        owner_user_id=OWNER,
        messages=[
            {"role": "user", "content": "Question"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "component_id": "answer", "content": "Answer"}
                ],
            },
        ],
        canvas_components=[
            {"type": "text", "component_id": "canvas-answer", "content": "Canvas"}
        ],
    )
    assert result["committed_render_revision"] == 1
    replay = repository.publish_commit(
        commit_id=first["commit_id"],
        owner_user_id=OWNER,
        messages=[],
        canvas_components=[],
    )
    assert replay == result
    snapshot = _snapshot(
        repository,
        snapshot_purpose="commit",
        request_generation=first["request_generation"],
    )
    assert snapshot["render_revision"] == 1
    assert len(snapshot["transcript"]) == 2
    # feature 063: a text primitive is lifted to a chat text part (words preserved);
    # UI components live only on the canvas, never in the transcript.
    assert snapshot["transcript"][1]["parts"][0]["type"] == "text"
    assert snapshot["transcript"][1]["parts"][0]["text"] == "Answer"
    assert snapshot["canvas"]["components"][0]["content"] == "Canvas"

    second = repository.stage_commit(
        chat_id=CHAT_ID,
        owner_user_id=OWNER,
        request_generation=uuid.uuid4(),
    )
    repository.publish_commit(
        commit_id=second["commit_id"],
        owner_user_id=OWNER,
        messages=[{"role": "assistant", "content": "Canvas cleared"}],
        canvas_components=[],
    )
    cleared = _snapshot(
        repository,
        snapshot_purpose="commit",
        request_generation=second["request_generation"],
    )
    assert cleared["render_revision"] == 2
    assert cleared["canvas"] == {"target": "canvas", "components": []}
    with pytest.raises(ConversationSnapshotInvalid, match="request generation"):
        _snapshot(repository, snapshot_purpose="commit")


def test_stale_base_and_stale_operation_fence_cannot_publish(database: Database) -> None:
    _create_chat(database)
    coordinator = _coordinator(database)
    owner, claim = _claim(coordinator)
    repository = _repository(database, coordinator)
    first = repository.stage_commit(
        chat_id=CHAT_ID,
        owner_user_id=OWNER,
        request_generation=claim.operation.request_generation,
        operation_fence=claim.fence,
        operation_owner=owner,
        connection_generation=claim.operation.connection_generation,
    )
    stale = repository.stage_commit(
        chat_id=CHAT_ID,
        owner_user_id=OWNER,
        request_generation=uuid.uuid4(),
    )
    repository.publish_commit(
        commit_id=first["commit_id"],
        owner_user_id=OWNER,
        messages=[{"role": "assistant", "content": "Committed under fence"}],
        canvas_components=[],
        operation_fence=claim.fence,
    )
    projection = coordinator.query_operation(owner=owner, operation_id=claim.operation.operation_id)
    assert projection.state is OperationState.COMPLETED

    with pytest.raises(ConversationCommitConflict):
        repository.publish_commit(
            commit_id=stale["commit_id"],
            owner_user_id=OWNER,
            messages=[{"role": "assistant", "content": "stale"}],
            canvas_components=[],
        )
    with pytest.raises(StaleExecutionFenceError):
        repository.stage_commit(
            chat_id=CHAT_ID,
            owner_user_id=OWNER,
            request_generation=uuid.uuid4(),
            operation_fence=claim.fence,
            operation_owner=owner,
            connection_generation=claim.operation.connection_generation,
        )


def test_fenced_stage_cannot_publish_through_unfenced_api(database: Database) -> None:
    _create_chat(database)
    coordinator = _coordinator(database)
    owner, claim = _claim(coordinator)
    repository = _repository(database, coordinator)
    staged = repository.stage_commit(
        chat_id=CHAT_ID,
        owner_user_id=OWNER,
        request_generation=claim.operation.request_generation,
        operation_fence=claim.fence,
        operation_owner=owner,
        connection_generation=claim.operation.connection_generation,
    )

    with pytest.raises(ConversationCommitConflict, match="operation fence"):
        repository.publish_commit(
            commit_id=staged["commit_id"],
            owner_user_id=OWNER,
            messages=[{"role": "assistant", "content": "must not escape fence"}],
            canvas_components=[],
        )

    assert _snapshot(repository)["render_revision"] == 0
    projection = coordinator.query_operation(
        owner=owner, operation_id=claim.operation.operation_id
    )
    assert projection.state is OperationState.RUNNING


def test_stage_rejects_wrong_operation_request_generation(database: Database) -> None:
    _create_chat(database)
    coordinator = _coordinator(database)
    owner, claim = _claim(coordinator)
    repository = _repository(database, coordinator)

    with pytest.raises(ConversationCommitConflict, match="request generation"):
        repository.stage_commit(
            chat_id=CHAT_ID,
            owner_user_id=OWNER,
            request_generation=uuid.uuid4(),
            operation_fence=claim.fence,
            operation_owner=owner,
            connection_generation=claim.operation.connection_generation,
        )

    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM conversation_commit"
    )["count"] == 0


def test_nonzero_revision_requires_a_complete_current_commit_anchor(database: Database) -> None:
    _create_chat(database)
    database.execute(
        "UPDATE chats SET render_revision = 1, snapshot_committed_at = now() "
        "WHERE id = ? AND user_id = ?",
        (CHAT_ID, OWNER),
    )

    with pytest.raises(ConversationSnapshotInvalid, match="commit anchor"):
        _snapshot(_repository(database))


def test_web_presentation_is_exact_post_adaptation_and_never_semantic(database: Database) -> None:
    _create_chat(database)
    repository = _repository(database)
    commit = repository.stage_commit(
        chat_id=CHAT_ID,
        owner_user_id=OWNER,
        request_generation=uuid.uuid4(),
    )
    repository.publish_commit(
        commit_id=commit["commit_id"],
        owner_user_id=OWNER,
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "component_id": "rail", "content": "Rail <safe>"}
                ],
            }
        ],
        canvas_components=[
            {"type": "text", "component_id": "canvas", "content": "Canvas <safe>"}
        ],
    )
    semantic = _snapshot(repository)
    original = copy.deepcopy(semantic)
    web = augment_conversation_snapshot_for_target(
        semantic, DeviceProfile.default(), target="web"
    )

    assert semantic == original
    # feature 063: the transcript is text-only — the rail text primitive is lifted to a
    # text part (no _presentation), and only canvas components carry web _presentation.
    assert web["transcript"][0]["parts"][0]["type"] == "text"
    assert web["transcript"][0]["parts"][0]["text"] == "Rail <safe>"
    for component in web["canvas"]["components"]:
        assert set(component["_presentation"]) == {"target", "html", "workspace"}
        assert component["_presentation"]["target"] == "web"
        assert set(component["_presentation"]["workspace"]) == {"export", "share"}
        assert "data-component-id" in component["_presentation"]["html"]
        assert "<safe>" not in component["_presentation"]["html"]

    native = augment_conversation_snapshot_for_target(
        web, DeviceProfile.default(), target="native"
    )
    assert native == original


def test_new_literal_strings_round_trip_as_text_not_json(database: Database) -> None:
    _create_chat(database)
    repository = _repository(database)
    commit = repository.stage_commit(
        chat_id=CHAT_ID,
        owner_user_id=OWNER,
        request_generation=uuid.uuid4(),
    )
    repository.publish_commit(
        commit_id=commit["commit_id"],
        owner_user_id=OWNER,
        messages=[
            {"role": "user", "content": "[]"},
            {"role": "assistant", "content": "null"},
            {"role": "tool", "content": "7"},
        ],
        canvas_components=[],
    )

    assert [message["parts"] for message in _snapshot(repository)["transcript"]] == [
        [{"type": "text", "text": "[]"}],
        [{"type": "text", "text": "null"}],
        [{"type": "text", "text": "7"}],
    ]


def test_structured_type_field_is_not_misclassified_and_blank_text_recovers(
    database: Database,
) -> None:
    _create_chat(database)
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'tool', ?, ?), (?, ?, 'assistant', ?, ?)",
        (
            CHAT_ID,
            OWNER,
            json.dumps({"type": "invoice", "total": 21}),
            1_752_664_801_000,
            CHAT_ID,
            OWNER,
            json.dumps({"type": "text", "value": 21}),
            1_752_664_802_000,
        ),
    )

    transcript = _snapshot(_repository(database))["transcript"]
    assert transcript[0]["parts"] == [
        {
            "type": "structured",
            "value": {"type": "invoice", "total": 21},
            "plain_text": "total: 21; type: invoice",
        }
    ]
    assert [part["type"] for part in transcript[1]["parts"]] == [
        "recovery",
        "structured",
    ]


def test_nonfinite_saved_json_recovers_visibly(database: Database) -> None:
    _create_chat(database)
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'tool', '[NaN]', ?)",
        (CHAT_ID, OWNER, 1_752_664_803_500),
    )

    parts = _snapshot(_repository(database))["transcript"][0]["parts"]
    assert [part["type"] for part in parts] == ["recovery", "structured"]
    assert parts[0]["code"] == "saved_content_unrenderable"


def test_transient_scope_is_exact_sequenced_and_never_relabels_snapshots() -> None:
    orchestrator = object.__new__(Orchestrator)
    socket = object()
    orchestrator._conversation_scopes = {
        id(socket): {
            "chat_id": CHAT_ID,
            "connection_generation": "33333333-3333-4333-8333-333333333333",
            "request_generation": "44444444-4444-4444-8444-444444444444",
            "purpose": "commit",
            "base_render_revision": 7,
            "frame_sequence": 0,
        }
    }
    orchestrator._ws_active_chat = {id(socket): CHAT_ID}

    first = json.loads(
        orchestrator._scope_conversation_transient(
            socket,
            json.dumps({"type": "ui_render", "target": "chat", "components": []}),
        )
    )
    second = json.loads(
        orchestrator._scope_conversation_transient(
            socket,
            json.dumps({"type": "ui_upsert", "chat_id": CHAT_ID, "ops": []}),
        )
    )
    assert first["base_render_revision"] == second["base_render_revision"] == 7
    assert (first["frame_sequence"], second["frame_sequence"]) == (1, 2)
    assert first["request_generation"] == second["request_generation"]

    snapshot = {"type": "conversation_snapshot", "render_revision": 8}
    assert json.loads(
        orchestrator._scope_conversation_transient(socket, json.dumps(snapshot))
    ) == snapshot
    assert orchestrator._conversation_scopes[id(socket)]["frame_sequence"] == 2


def test_client_authored_presentation_is_rejected_before_durable_write(database: Database) -> None:
    _create_chat(database)
    repository = _repository(database)
    commit = repository.stage_commit(
        chat_id=CHAT_ID,
        owner_user_id=OWNER,
        request_generation=uuid.uuid4(),
    )
    with pytest.raises(ValueError, match="_presentation"):
        repository.publish_commit(
            commit_id=commit["commit_id"],
            owner_user_id=OWNER,
            messages=[],
            canvas_components=[
                {
                    "type": "text",
                    "content": "unsafe",
                    "_presentation": {
                        "target": "web",
                        "html": "<b>unsafe</b>",
                        "workspace": {"export": True, "share": True},
                    },
                }
            ],
        )
    with pytest.raises(ValueError, match="_presentation"):
        repository.publish_commit(
            commit_id=commit["commit_id"],
            owner_user_id=OWNER,
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "content": "unsafe transcript component",
                            "_presentation": {
                                "target": "web",
                                "html": "<b>unsafe</b>",
                                "workspace": {"export": True, "share": True},
                            },
                        }
                    ],
                }
            ],
            canvas_components=[],
        )


@pytest.mark.asyncio
async def test_production_turn_seam_emits_one_complete_post_rote_commit_snapshot(
    database: Database,
) -> None:
    await asyncio.to_thread(_create_chat, database)
    coordinator = await asyncio.to_thread(_coordinator, database)
    owner, claim = await asyncio.to_thread(_claim, coordinator)

    class Socket:
        def __init__(self) -> None:
            self.frames = []

        async def send_text(self, data: str) -> None:
            self.frames.append(json.loads(data))

    class Rote:
        profile = DeviceProfile.default()

        def adapt(self, _socket, components):
            return copy.deepcopy(components)

        def get_profile(self, _socket):
            return self.profile

    socket = Socket()
    history = object.__new__(HistoryManager)
    history.db = database
    orchestrator = object.__new__(Orchestrator)
    orchestrator.history = history
    orchestrator.workspace = WorkspaceManager(history)
    orchestrator.work_admission = coordinator
    orchestrator.conversation_commits = _repository(database, coordinator)
    orchestrator.rote = Rote()
    orchestrator.ui_clients = [socket]
    orchestrator.ui_sessions = {socket: {"sub": OWNER}}
    orchestrator._ws_active_chat = {}
    orchestrator._conversation_scopes = {}

    stage, token, request_generation = (
        await orchestrator._begin_conversation_publication(
            socket,
            chat_id=CHAT_ID,
            user_id=OWNER,
            operation_context={
                "operation": claim.operation,
                "owner": owner,
                "execution_fence": claim.fence,
            },
        )
    )
    assert stage is not None and token is not None
    try:
        await orchestrator._append_conversation_message(
            stage,
            chat_id=CHAT_ID,
            user_id=OWNER,
            role="user",
            content="Make this durable",
        )
        await orchestrator._append_conversation_message(
            stage,
            chat_id=CHAT_ID,
            user_id=OWNER,
            role="assistant",
            content=[{"type": "text", "content": "Committed response"}],
        )
        await orchestrator.workspace.aupsert(
            CHAT_ID,
            OWNER,
            [{"type": "text", "component_id": "canvas", "content": "Canvas"}],
        )
        assert await asyncio.to_thread(
            orchestrator.workspace.upsert_layout,
            CHAT_ID,
            OWNER,
            "turn-layout",
            [{"type": "ref", "component_id": "canvas"}],
        )

        # Every other reader remains on the prior complete revision.
        prior_snapshot = await asyncio.to_thread(
            _snapshot, _repository(database)
        )
        prior_chat = await asyncio.to_thread(history.get_chat, CHAT_ID, OWNER)
        prior_recents = await asyncio.to_thread(
            history.get_recent_chats, user_id=OWNER
        )
        assert prior_snapshot["transcript"] == []
        assert prior_chat["messages"] == []
        assert prior_recents == []

        committed = await orchestrator._publish_conversation_snapshot(
            socket,
            stage=stage,
            request_generation=request_generation,
        )
    finally:
        reset_conversation_publication(token)

    assert committed["committed_render_revision"] == 1
    snapshots = [
        frame for frame in socket.frames if frame.get("type") == "conversation_snapshot"
    ]
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot["snapshot_purpose"] == "commit"
    assert snapshot["request_generation"] == request_generation
    assert snapshot["render_revision"] == 1
    assert [message["role"] for message in snapshot["transcript"]] == [
        "user",
        "assistant",
    ]
    assert snapshot["canvas"]["components"][0]["content"] == "Canvas"
    assert snapshot["canvas"]["components"][0]["_presentation"]["target"] == "web"
    layout = await asyncio.to_thread(
        database.fetch_one,
        "SELECT layout_key FROM workspace_layout WHERE chat_id = ? AND user_id = ?",
        (CHAT_ID, OWNER),
    )
    persisted_chat = await asyncio.to_thread(history.get_chat, CHAT_ID, OWNER)
    persisted_recents = await asyncio.to_thread(
        history.get_recent_chats, user_id=OWNER
    )
    operation = await asyncio.to_thread(
        coordinator.query_operation,
        owner=owner,
        operation_id=claim.operation.operation_id,
    )
    assert layout["layout_key"] == "turn-layout"
    assert len(persisted_chat["messages"]) == 2
    assert persisted_recents[0]["id"] == CHAT_ID
    assert operation.state is OperationState.COMPLETED


def test_revisioned_chats_reject_every_legacy_message_and_canvas_write(
    database: Database,
) -> None:
    """Once revision 1 exists, no unversioned row can escape beside it."""

    _create_chat(database)
    repository = _repository(database)
    staged = repository.stage_commit(
        chat_id=CHAT_ID,
        owner_user_id=OWNER,
        request_generation=uuid.uuid4(),
    )
    repository.publish_commit(
        commit_id=staged["commit_id"],
        owner_user_id=OWNER,
        messages=[{"role": "assistant", "content": "revision one"}],
        canvas_components=[
            {"type": "text", "component_id": "stable", "content": "one"}
        ],
    )

    history = object.__new__(HistoryManager)
    history.db = database
    workspace = WorkspaceManager(history)
    current = workspace.get_by_component_id(CHAT_ID, OWNER, "stable")
    assert current is not None

    with pytest.raises(RuntimeError, match="publication stage"):
        history.add_message(CHAT_ID, "assistant", "legacy leak", user_id=OWNER)
    with pytest.raises(RuntimeError, match="publication stage"):
        history.save_component(
            CHAT_ID,
            {"type": "text", "content": "legacy leak"},
            "text",
            user_id=OWNER,
        )
    with pytest.raises(RuntimeError, match="publication stage"):
        history.delete_component(current["id"], user_id=OWNER)
    with pytest.raises(RuntimeError, match="publication stage"):
        history.replace_components(
            [current["id"]],
            [{"component_data": {"type": "text", "content": "leak"}}],
            CHAT_ID,
            user_id=OWNER,
        )
    with pytest.raises(RuntimeError, match="publication stage"):
        workspace.upsert(
            CHAT_ID, OWNER, [{"type": "text", "content": "legacy leak"}]
        )
    with pytest.raises(RuntimeError, match="publication stage"):
        workspace.remove(CHAT_ID, OWNER, "stable")
    with pytest.raises(RuntimeError, match="publication stage"):
        workspace.upsert_layout(CHAT_ID, OWNER, "legacy", [])

    snapshot = _snapshot(repository)
    assert snapshot["render_revision"] == 1
    assert [part["text"] for part in snapshot["transcript"][0]["parts"]] == [
        "revision one"
    ]
    assert snapshot["canvas"]["components"][0]["content"] == "one"


def test_source_contract_has_no_client_or_filesystem_authority() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "orchestrator" / "history.py"
    ).read_text(encoding="utf-8")
    section = source[
        source.index("class ConversationCommitRepository") : source.index("class HistoryManager")
    ]
    assert "localStorage" not in section
    assert "QSettings" not in section
    assert "open(" not in section
