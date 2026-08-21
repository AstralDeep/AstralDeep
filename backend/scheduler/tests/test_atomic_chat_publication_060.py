"""Atomic scheduled-chat publication contracts for feature 060 (T028/T029)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import timedelta
from types import MethodType, SimpleNamespace
from typing import Iterator

import pytest

from orchestrator.history import ConversationCommitRepository
from orchestrator.orchestrator import Orchestrator
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.workspace import WorkspaceManager
from orchestrator.scheduled_publication import (
    ScheduledPublicationEscapeError,
    stage_scheduled_history,
)
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    WorkAdmissionCoordinator,
)
from scheduler.runner import JobRunner
from scheduler.tests.plane_runtime import (
    ensure_plane_runtime,
    scheduled_job_store as ScheduledJobStore,
    work_admission_repository,
)
from scheduler.store import (
    EffectIdempotencyConflictError,
    ScheduledAttempt,
    StaleOccurrenceClaimError,
)
from tests.helpers.voice_plane_runtime import (
    PlaneTestRuntime,
    history_manager,
    isolated_plane_runtime,
)


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[PlaneTestRuntime]:
    """Create one isolated database initialized only by AstralPlane."""

    with isolated_plane_runtime("atomic_chat") as runtime:
        yield runtime


@pytest.fixture
def clean_database(postgres_database: PlaneTestRuntime) -> PlaneTestRuntime:
    db = postgres_database
    ensure_plane_runtime(db)
    db.execute("DELETE FROM effect_ledger")
    db.execute("DELETE FROM job_run")
    db.execute("DELETE FROM scheduled_occurrence")
    db.execute("DELETE FROM scheduled_job")
    db.execute("DELETE FROM workspace_layout")
    db.execute("DELETE FROM saved_components")
    db.execute("DELETE FROM messages")
    db.execute("UPDATE chats SET conversation_commit_id = NULL")
    db.execute("DELETE FROM conversation_commit")
    db.execute("DELETE FROM chats")
    db.execute("DELETE FROM operation_submission_result")
    db.execute(
        "UPDATE operation_admission_slot SET operation_id = NULL, "
        "lease_token = NULL, lease_expires_at = NULL"
    )
    db.execute("DELETE FROM operation_record")
    return db


def _coordinator(db: PlaneTestRuntime) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=8,
                queue_limit=0,
                max_wait_ms=None,
                config_revision="atomic-chat-060-test",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.SCHEDULED,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=2,
                queue_limit=8,
                max_wait_ms=30_000,
                config_revision="atomic-chat-060-test",
            ),
        ),
        repository=work_admission_repository(db),
        operation_retention=timedelta(hours=24),
        slot_lease=timedelta(seconds=90),
    )


def _started_attempt(
    db: PlaneTestRuntime, *, label: str, explicit_chat: bool = True
) -> tuple[ScheduledJobStore, ScheduledAttempt, str, str]:
    coordinator = _coordinator(db)
    store = ScheduledJobStore(db, coordinator=coordinator)
    user_id = f"owner-{label}"
    chat_id = str(uuid.uuid4()) if explicit_chat else ""
    now_ms = int(
        db.fetch_one(
            "SELECT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT AS n"
        )["n"]
    )
    if explicit_chat:
        db.execute(
            "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
            "VALUES (?, ?, 'Existing chat', ?, ?)",
            (chat_id, user_id, now_ms, now_ms),
        )
    job = store.create_job(
        user_id,
        name=f"Atomic {label}",
        instruction=f"publish {label}",
        schedule_kind="interval",
        schedule_expr="1h",
        timezone="UTC",
        consented_scopes=[],
        agent_id=None,
        target_chat_id=chat_id if explicit_chat else None,
        next_run_at=now_ms - 1_000,
        offline_grant_id=None,
    )
    if not explicit_chat:
        chat_id = str(job["id"])
    claim = store.materialize_and_claim_due(
        "atomic-chat-test", limit=1, lease_seconds=15
    )[0]
    attempt = store.allocate_attempt(claim)
    if attempt.execution_fence is None:
        selected = store.claim_attempt_execution(attempt)
        assert selected is not None
        attempt = selected
    return store, store.start_attempt(attempt), user_id, chat_id


def _digest(attempt: ScheduledAttempt) -> str:
    return hashlib.sha256(
        f"chat:{attempt.claim.occurrence_id}".encode("utf-8")
    ).hexdigest()


def _orchestrator(
    db: PlaneTestRuntime,
    store: ScheduledJobStore | None = None,
    *,
    visible_counts: list[int],
) -> Orchestrator:
    history = history_manager(db)
    orch = Orchestrator.__new__(Orchestrator)
    orch.history = history
    orch.workspace = WorkspaceManager(history)
    orch.work_admission = store._require_coordinator() if store is not None else None
    orch.conversation_commits = ConversationCommitRepository(
        db, operation_coordinator=orch.work_admission
    )
    orch.ui_sessions = {}
    orch.ui_clients = []
    orch._ws_active_chat = {}
    orch._conversation_scopes = {}
    orch._workspace_locks = {}
    orch._chat_recorders = {}
    orch.audit_recorder = None
    orch._llm_store = SimpleNamespace(get_system=lambda: None)

    class _ConfiguredLLMStore:
        async def get_system(self):
            return object()

    orch._llm_store = _ConfiguredLLMStore()

    async def handle_chat_message(
        self,
        websocket,
        message,
        chat_id,
        *,
        user_id=None,
        **_kwargs,
    ):
        await asyncio.to_thread(
            self.history.add_message, chat_id, "user", message, user_id
        )
        await asyncio.to_thread(
            self.history.add_message,
            chat_id,
            "assistant",
            [{"type": "text", "content": "staged answer"}],
            user_id,
        )
        visible_counts.append(
            int(
                await asyncio.to_thread(
                    lambda: db.fetch_one(
                        "SELECT COUNT(*) AS n FROM messages "
                        "WHERE chat_id = ? AND user_id = ?",
                        (chat_id, user_id),
                    )["n"]
                )
            )
        )
        await websocket.send_text(json.dumps({"type": "text", "text": "staged answer"}))

    orch.handle_chat_message = MethodType(handle_chat_message, orch)
    return orch


@pytest.mark.asyncio
async def test_scheduled_messages_are_invisible_until_effect_publish_commit(
    clean_database: PlaneTestRuntime,
) -> None:
    store, attempt, user_id, chat_id = _started_attempt(clean_database, label="commit")
    assert uuid.UUID(chat_id).version == 4
    digest = _digest(attempt)
    reservation = store.reserve_atomic_chat_effect(
        attempt,
        effect_key=chat_id,
        payload_digest=digest,
    )
    assert reservation.state == "reserved"
    visible_counts: list[int] = []
    orch = _orchestrator(clean_database, store, visible_counts=visible_counts)

    summary = await orch.run_scheduled_turn(
        user_id=user_id,
        chat_id=chat_id,
        instruction="publish commit",
        agent_id=None,
        access_token="opaque-test-token",
        allowed_scopes=[],
        correlation_id=str(attempt.claim.occurrence_id),
        scheduled_attempt=attempt,
        scheduled_store=store,
        effect_kind="chat_history",
        effect_key=chat_id,
        payload_digest=digest,
    )

    assert visible_counts == [0]
    assert summary == "staged answer"
    rows = clean_database.fetch_all(
        "SELECT role, content, conversation_commit_id, commit_position, "
        "committed_render_revision FROM messages WHERE chat_id = ? ORDER BY id",
        (chat_id,),
    )
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert [row["commit_position"] for row in rows] == [0, 1]
    assert {row["committed_render_revision"] for row in rows} == {1}
    assert len({str(row["conversation_commit_id"]) for row in rows}) == 1
    chat = clean_database.fetch_one(
        "SELECT render_revision, conversation_commit_id FROM chats WHERE id = ?",
        (chat_id,),
    )
    assert chat["render_revision"] == 1
    assert str(chat["conversation_commit_id"]) == str(rows[0]["conversation_commit_id"])
    snapshot = orch.conversation_commits.build_snapshot(
        chat_id=chat_id,
        owner_user_id=user_id,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
        snapshot_purpose="hydration",
    )
    assert snapshot["render_revision"] == 1
    assert [message["role"] for message in snapshot["transcript"]] == [
        "user",
        "assistant",
    ]
    effect = clean_database.fetch_one(
        "SELECT state FROM effect_ledger WHERE occurrence_id = ? "
        "AND effect_kind = 'chat_history' AND effect_key = ?",
        (str(attempt.claim.occurrence_id), chat_id),
    )
    assert effect == {"state": "published"}


@pytest.mark.asyncio
async def test_fault_before_publish_rolls_back_messages_and_reserved_replays(
    clean_database: PlaneTestRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, attempt, user_id, chat_id = _started_attempt(
        clean_database, label="rollback"
    )
    digest = _digest(attempt)
    store.reserve_atomic_chat_effect(
        attempt,
        effect_key=chat_id,
        payload_digest=digest,
    )
    orch = _orchestrator(clean_database, store, visible_counts=[])
    original = store._plane.repository.publish_reserved_effect

    def crash_before_publish(*_args, **_kwargs):
        raise RuntimeError("injected pre-publication crash")

    monkeypatch.setattr(
        store._plane.repository,
        "publish_reserved_effect",
        crash_before_publish,
    )
    with pytest.raises(RuntimeError, match="injected pre-publication crash"):
        await orch.run_scheduled_turn(
            user_id=user_id,
            chat_id=chat_id,
            instruction="publish rollback",
            agent_id=None,
            access_token="opaque-test-token",
            allowed_scopes=[],
            correlation_id=str(attempt.claim.occurrence_id),
            scheduled_attempt=attempt,
            scheduled_store=store,
            effect_kind="chat_history",
            effect_key=chat_id,
            payload_digest=digest,
        )

    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
        )["n"]
        == 0
    )
    assert clean_database.fetch_one(
        "SELECT state FROM effect_ledger WHERE occurrence_id = ? "
        "AND effect_kind = 'chat_history' AND effect_key = ?",
        (str(attempt.claim.occurrence_id), chat_id),
    ) == {"state": "reserved"}

    monkeypatch.setattr(store._plane.repository, "publish_reserved_effect", original)
    clean_database.execute(
        "UPDATE scheduled_occurrence SET lease_expires_at = "
        "clock_timestamp() - INTERVAL '1 second' WHERE occurrence_id = ?",
        (str(attempt.claim.occurrence_id),),
    )
    recovered_claim = store.materialize_and_claim_due(
        "atomic-chat-recovery", limit=1, lease_seconds=15
    )[0]
    assert recovered_claim.occurrence_id == attempt.claim.occurrence_id
    assert recovered_claim.attempt_number == attempt.claim.attempt_number + 1
    recovered_attempt = store.allocate_attempt(recovered_claim)
    if recovered_attempt.execution_fence is None:
        selected = store.claim_attempt_execution(recovered_attempt)
        assert selected is not None
        recovered_attempt = selected
    recovered_attempt = store.start_attempt(recovered_attempt)
    recovered = store.reserve_atomic_chat_effect(
        recovered_attempt,
        effect_key=chat_id,
        payload_digest=digest,
    )
    assert recovered.state == "reserved"
    await orch.run_scheduled_turn(
        user_id=user_id,
        chat_id=chat_id,
        instruction="publish rollback",
        agent_id=None,
        access_token="opaque-test-token",
        allowed_scopes=[],
        correlation_id=str(recovered_attempt.claim.occurrence_id),
        scheduled_attempt=recovered_attempt,
        scheduled_store=store,
        effect_kind="chat_history",
        effect_key=chat_id,
        payload_digest=digest,
    )
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
        )["n"]
        == 2
    )

    # Even a direct defensive replay cannot append a second visible turn.
    await orch.run_scheduled_turn(
        user_id=user_id,
        chat_id=chat_id,
        instruction="publish rollback",
        agent_id=None,
        access_token="opaque-test-token",
        allowed_scopes=[],
        correlation_id=str(recovered_attempt.claim.occurrence_id),
        scheduled_attempt=recovered_attempt,
        scheduled_store=store,
        effect_kind="chat_history",
        effect_key=chat_id,
        payload_digest=digest,
    )
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
        )["n"]
        == 2
    )


@pytest.mark.asyncio
async def test_fallback_chat_is_created_only_in_atomic_publication(
    clean_database: PlaneTestRuntime,
) -> None:
    store, attempt, user_id, chat_id = _started_attempt(
        clean_database,
        label="fallback",
        explicit_chat=False,
    )
    assert uuid.UUID(chat_id).version == 4
    assert chat_id == str(attempt.job["id"])
    digest = _digest(attempt)
    store.reserve_atomic_chat_effect(
        attempt,
        effect_key=chat_id,
        payload_digest=digest,
    )
    orch = _orchestrator(clean_database, store, visible_counts=[])

    assert (
        clean_database.fetch_one("SELECT id FROM chats WHERE id = ?", (chat_id,))
        is None
    )
    await orch.run_scheduled_turn(
        user_id=user_id,
        chat_id=None,
        instruction="publish fallback",
        agent_id=None,
        access_token="opaque-test-token",
        allowed_scopes=[],
        correlation_id=str(attempt.claim.occurrence_id),
        scheduled_attempt=attempt,
        scheduled_store=store,
        effect_kind="chat_history",
        effect_key=chat_id,
        payload_digest=digest,
    )

    chat = clean_database.fetch_one(
        "SELECT user_id, title FROM chats WHERE id = ?", (chat_id,)
    )
    assert chat == {"user_id": user_id, "title": "publish fallback"}
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
        )["n"]
        == 2
    )


@pytest.mark.asyncio
async def test_history_projection_is_task_local_and_late_writes_fail_closed(
    clean_database: PlaneTestRuntime,
) -> None:
    history = history_manager(clean_database)
    user_id = "owner-stage"
    chat_id = "chat-stage"
    clean_database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'Existing', 1, 1)",
        (chat_id, user_id),
    )
    history.add_message(chat_id, "assistant", "prior", user_id=user_id)
    release_late_write = asyncio.Event()

    with stage_scheduled_history(
        history=history,
        chat_id=chat_id,
        user_id=user_id,
        create_chat_if_missing=False,
        agent_id=None,
    ) as stage:
        history.add_message(chat_id, "user", "staged", user_id=user_id)
        history.update_chat_title(chat_id, "Staged title", user_id=user_id)
        projection = history.get_chat(chat_id, user_id=user_id)
        assert projection is not None
        assert [message["content"] for message in projection["messages"]] == [
            "prior",
            "staged",
        ]
        assert projection["title"] == "Staged title"
        with pytest.raises(ScheduledPublicationEscapeError):
            history.add_message("other-chat", "user", "escape", user_id=user_id)

        async def late_write() -> None:
            await release_late_write.wait()
            await asyncio.to_thread(
                history.add_message,
                chat_id,
                "assistant",
                "too late",
                user_id,
            )

        inherited = asyncio.create_task(late_write())

    batch = stage.batch()
    assert len(batch.messages) == 1
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
        )["n"]
        == 1
    )
    release_late_write.set()
    with pytest.raises(ScheduledPublicationEscapeError, match="sealed"):
        await inherited
    ordinary = history.get_chat(chat_id, user_id=user_id)
    assert ordinary is not None
    assert [message["content"] for message in ordinary["messages"]] == ["prior"]


def test_atomic_reservation_conflict_and_stale_claim_fail_closed(
    clean_database: PlaneTestRuntime,
) -> None:
    store, attempt, _user_id, chat_id = _started_attempt(
        clean_database, label="conflict"
    )
    digest = _digest(attempt)
    store.reserve_atomic_chat_effect(
        attempt,
        effect_key=chat_id,
        payload_digest=digest,
    )
    with pytest.raises(EffectIdempotencyConflictError):
        store.reserve_atomic_chat_effect(
            attempt,
            effect_key=chat_id,
            payload_digest="f" * 64,
        )

    clean_database.execute(
        "UPDATE scheduled_occurrence "
        "SET claim_generation = claim_generation + 1, lease_token = ? "
        "WHERE occurrence_id = ?",
        (str(uuid.uuid4()), str(attempt.claim.occurrence_id)),
    )
    with pytest.raises(StaleOccurrenceClaimError):
        store.reserve_atomic_chat_effect(
            attempt,
            effect_key=chat_id,
            payload_digest=digest,
        )


@pytest.mark.asyncio
async def test_exact_identity_and_mutating_scope_defenses_run_before_handler(
    clean_database: PlaneTestRuntime,
) -> None:
    store, attempt, user_id, chat_id = _started_attempt(clean_database, label="defense")
    digest = _digest(attempt)
    store.reserve_atomic_chat_effect(
        attempt,
        effect_key=chat_id,
        payload_digest=digest,
    )
    orch = _orchestrator(clean_database, store, visible_counts=[])

    with pytest.raises(ValueError, match="full effect identity"):
        await orch.run_scheduled_turn(
            user_id=user_id,
            chat_id=chat_id,
            instruction="blocked",
            agent_id=None,
            access_token="opaque-test-token",
            allowed_scopes=[],
            correlation_id=str(attempt.claim.occurrence_id),
            scheduled_attempt=attempt,
        )
    with pytest.raises(ValueError, match="target/effect"):
        await orch.run_scheduled_turn(
            user_id=user_id,
            chat_id=chat_id,
            instruction="blocked",
            agent_id=None,
            access_token="opaque-test-token",
            allowed_scopes=[],
            correlation_id=str(attempt.claim.occurrence_id),
            scheduled_attempt=attempt,
            scheduled_store=store,
            effect_kind="chat_history",
            effect_key="wrong-chat",
            payload_digest=digest,
        )
    attempt.job["consented_scopes"] = ["tools:write"]
    with pytest.raises(PermissionError, match="downstream idempotency"):
        await orch.run_scheduled_turn(
            user_id=user_id,
            chat_id=chat_id,
            instruction="blocked",
            agent_id=None,
            access_token="opaque-test-token",
            allowed_scopes=[],
            correlation_id=str(attempt.claim.occurrence_id),
            scheduled_attempt=attempt,
            scheduled_store=store,
            effect_kind="chat_history",
            effect_key=chat_id,
            payload_digest=digest,
        )
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
        )["n"]
        == 0
    )


@pytest.mark.asyncio
async def test_legacy_turn_and_scheduled_workspace_suppression_are_preserved(
    clean_database: PlaneTestRuntime,
) -> None:
    user_id = "owner-legacy"
    chat_id = "chat-legacy"
    now_ms = 1
    clean_database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'Legacy', ?, ?)",
        (chat_id, user_id, now_ms, now_ms),
    )
    visible_counts: list[int] = []
    orch = _orchestrator(clean_database, visible_counts=visible_counts)

    summary = await orch.run_scheduled_turn(
        user_id=user_id,
        chat_id=chat_id,
        instruction="legacy execution",
        agent_id=None,
        access_token="opaque-test-token",
        allowed_scopes=[],
        correlation_id="legacy-correlation",
    )

    assert summary == "staged answer"
    assert visible_counts == [2]
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
        )["n"]
        == 2
    )

    rendered: list[list[dict]] = []

    async def send_ui_render(self, _websocket, components, **_kwargs):
        rendered.append(components)

    orch.send_ui_render = MethodType(send_ui_render, orch)
    orch.workspace = SimpleNamespace(
        upsert=lambda *_args, **_kwargs: pytest.fail(
            "scheduled workspace upsert escaped staging"
        )
    )
    with stage_scheduled_history(
        history=orch.history,
        chat_id=chat_id,
        user_id=user_id,
        create_chat_if_missing=False,
        agent_id=None,
    ):
        result = await orch._send_or_replace_components(
            SimpleNamespace(),
            [{"type": "text", "content": "scheduled component"}],
            chat_id,
            user_id,
        )
        await orch._design_turn_post_done(
            SimpleNamespace(),
            chat_id,
            user_id,
            "request",
            [{"type": "text", "content": "scheduled component"}],
        )
    assert result == []
    assert len(rendered) == 1


def test_scheduler_observability_uses_only_bounded_dimensions() -> None:
    observability = RuntimeObservability(deployment_instance="atomic_chat_test")
    runner = JobRunner(
        SimpleNamespace(runtime_observability=observability),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    runner._observe_scheduler(
        "claim_recovered",
        {"agent_id": None},
        result_code="claim_recovered",
    )
    runner._observe_effect("reserved", effect_kind="chat_history")
    runner._observe_effect("published", effect_kind="chat_history")
    samples = {sample.name: sample for sample in observability.snapshot()}

    assert samples["scheduler_claim_recovered_total"].labels == {
        "deployment_instance": "atomic_chat_test",
        "job_type": "scheduled_chat",
        "result_code": "claim_recovered",
    }
    assert samples["scheduler_effect_reserved_total"].labels == {
        "deployment_instance": "atomic_chat_test",
        "effect_kind": "chat_history",
    }
    assert samples["scheduler_effect_published_total"].value == 1


@pytest.mark.parametrize("scope", ("tools:write", "tools:execute"))
def test_mutating_scheduled_scopes_are_ineligible(scope: str) -> None:
    runner = JobRunner(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    decision = runner.assess_job(
        {
            "handler_kind": "scheduled_chat",
            "consented_scopes": ["tools:read", scope],
        }
    )

    assert decision.eligible is False
    assert decision.code == "handler_downstream_idempotency_unreviewed"
    assert decision.retryable is False
