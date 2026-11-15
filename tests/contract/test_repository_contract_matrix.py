"""Cross-repository contract matrix proving every public repository in
astralplane.repositories behaves consistently under replay, scope, race, and failure,
rather than trusting docstrings as proof.
"""

from __future__ import annotations

import importlib
import sys
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import astralplane
import astralplane.api as api
import astralplane.authority as authority

_ROOT = Path(__file__).resolve().parents[2]
_OWNER = "contract-owner"
_CHAIN = "contract-chain"
_WORKER = "contract-worker"
_UUID4 = "00000000-0000-4000-8000-000000000001"
_NOW = datetime(2026, 8, 14, 18, tzinfo=UTC)


class VisibleDriverError(RuntimeError):
    pass


class FailingExecutor:
    def __init__(self, failure: VisibleDriverError) -> None:
        self.failure = failure
        self.calls: list[tuple[str, str, object]] = []
        self.commit_called = False
        self.rollback_called = False

    def _fail(self, operation: str, statement: str, parameters: object) -> Any:
        self.calls.append((operation, statement, parameters))
        raise self.failure

    def execute(self, statement: str, parameters: object = ()) -> Any:
        return self._fail("execute", statement, parameters)

    def fetch_one(self, statement: str, parameters: object = ()) -> Any:
        return self._fail("fetch_one", statement, parameters)

    def fetch_all(self, statement: str, parameters: object = ()) -> Any:
        return self._fail("fetch_all", statement, parameters)

    @contextmanager
    def savepoint(self, name: str) -> Any:
        del name
        yield self

    def commit(self) -> None:
        self.commit_called = True

    def rollback(self) -> None:
        self.rollback_called = True


Probe = Callable[[api.RepositoryCatalog, FailingExecutor], None]


@dataclass(frozen=True, slots=True)
class RepositoryContract:
    key: str
    repository_factory: Callable[[], object]
    source_path: str
    probe: Probe
    attribution_value: str
    scope_sql: str
    concurrency_evidence: tuple[str, ...]
    idempotency_evidence: tuple[str, ...]
    idempotency_not_applicable: str | None
    failure_evidence: tuple[str, ...]


def _projects(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.projects.get(transaction, owner_id=_OWNER, project_id=_UUID4)


def _observations(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.observations.get_observation(
        transaction,
        owner_id=_OWNER,
        conversation_id="contract-conversation",
        observation_id=_UUID4,
    )


def _agent_management(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.agent_management.get_list_context(
        transaction,
        owner_id=_OWNER,
        ownership_limit=1,
    )


def _assignments(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.assignments.get_assignment(transaction, owner_id=_OWNER, assignment_id=_UUID4)


def _atlas_pages(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.atlas_pages.get_page(transaction, owner_id=_OWNER, page_id=_UUID4)


def _atlas_links(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.atlas_links.list_links(transaction, owner_id=_OWNER, page_id=_UUID4)


def _agents(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.agents.get_agent(transaction, owner_id=_OWNER, agent_id="agent-1")


def _background_tasks(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.background_tasks.get(transaction, owner_id=_OWNER, task_id="task-1")


def _framework_credentials(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.framework_credentials.list_for_owner(transaction, owner_id=_OWNER)


def _history(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.history.conversations.get(
        transaction,
        owner_id=_OWNER,
        conversation_id="conversation-1",
    )


def _harness_cleanup(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    from astralplane.repositories.harness_cleanup import HarnessCleanupProfile

    catalog.harness_cleanup.purge_run(
        transaction,
        profile=HarnessCleanupProfile.VERIFICATION,
        run_id="contract-run",
    )


def _identity(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.identity.get_identity(transaction, owner_id=_OWNER)


def _knowledge(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.knowledge.interactions.record_for_owner(
        transaction,
        owner_id=_OWNER,
        conversation_id="conversation-1",
        agent_id="agent-1",
        tool_name="search",
        success=True,
        error_message=None,
        response_time_ms=1,
        created_at=1,
    )


def _maintenance(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.maintenance.get_for_owner(
        transaction,
        owner_id=_OWNER,
        unit_id=_UUID4,
    )


def _mesh_enrollment(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.mesh_enrollment.get_mesh(
        transaction,
        owner_id=_OWNER,
        mesh_id="mesh-1",
    )


def _verification_credentials(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.verification_credentials.get_credential(
        transaction,
        owner_id=_OWNER,
        credential_id=_UUID4,
    )


def _notifications(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.notifications.get_preferences(transaction, owner_id=_OWNER)


def _push_deliveries(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.push_deliveries.get_destination(
        transaction,
        owner_id=_OWNER,
        destination_id=_UUID4,
    )


def _stop_epochs(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.stop_epochs.get(transaction, owner_id=_OWNER)


def _completion_wake(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.completion_wake.accept_wake_receipt(
        transaction,
        owner_id=_OWNER,
        subscription_id=_UUID4,
        idempotency_key="contract-key",
        observed_terminal="completed",
        observed_revision=1,
        accepted_at=1,
    )


def _computer_use(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.computer_use.get_session(transaction, owner_id=_OWNER, session_id=_UUID4)


def _capability_publications(
    catalog: api.RepositoryCatalog,
    transaction: FailingExecutor,
) -> None:
    catalog.capability_publications.get(
        transaction,
        owner_id=_OWNER,
        capability_id="capability-1",
        audience_id="audience-1",
    )


def _offline_grants(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.offline_grants.get_grant(
        transaction,
        owner_id=_OWNER,
        grant_id=_UUID4,
    )


def _workspaces(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.workspaces.canvas.get_scoped(
        transaction,
        owner_id=_OWNER,
        conversation_id="conversation-1",
        component_id="component-1",
    )


def _artifacts(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.artifacts.attachments.get(
        transaction,
        owner_id=_OWNER,
        attachment_id="attachment-1",
    )


def _attachment_parsers(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.attachment_parsers.get_owner_claim_by_gap(
        transaction,
        owner_id=_OWNER,
        gap_fingerprint="gap-1",
    )


def _chat_queue(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.chat_queue.list(transaction, owner_id=_OWNER, chat_id="contract-chat")


def _chat_read_receipts(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.chat_read_receipts.get(transaction, owner_id=_OWNER, chat_id="contract-chat")


def _derived_chat(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.derived_chat.get_chat_by_event(
        transaction, owner_id=_OWNER, event_id="sha256:" + "0" * 64
    )


def _event_log(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.event_log.get_event(
        transaction, owner_id=_OWNER, mesh_id="sha256:" + "1" * 64, event_id="sha256:" + "2" * 64
    )


def _event_roster(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.event_roster.get_entry(
        transaction, owner_id=_OWNER, mesh_id="sha256:" + "1" * 64, entry_id="sha256:" + "3" * 64
    )


def _learning_ledger(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.learning_ledger.get_entry(
        transaction, owner_id=_OWNER, mesh_id="sha256:" + "1" * 64, event_id="sha256:" + "4" * 64
    )


def _runs(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.runs.get_lease(
        transaction, owner_id=_OWNER, mesh_id="sha256:" + "1" * 64, run_id="contract-run"
    )


def _chat_steps(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.chat_steps.get_step(transaction, owner_id=_OWNER, step_id="step-1")


def _conversation_files(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.conversation_files.get_mapping(
        transaction,
        owner_id=_OWNER,
        mapping_id=1,
    )


def _credentials(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.credentials.get_credential(
        transaction,
        owner_id=_OWNER,
        agent_id="agent-1",
        credential_key="default",
    )


def _draft_agents(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.draft_agents.get_draft(
        transaction,
        owner_id=_OWNER,
        draft_id="draft-1",
    )


def _generated_agent_publications(
    catalog: api.RepositoryCatalog,
    transaction: FailingExecutor,
) -> None:
    catalog.generated_agent_publications.get_by_source(
        transaction,
        owner_id=_OWNER,
        draft_uuid=_UUID4,
        source_state_revision=1,
    )


def _preferences(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.preferences.feedback.get(
        transaction,
        owner_id=_OWNER,
        feedback_id="feedback-1",
    )


def _personalization_graph(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.personalization_graph.linked_ids(
        transaction,
        owner_id=_OWNER,
        memory_id="memory-1",
        limit=1,
    )


def _scheduler(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.scheduler.get_job(transaction, owner_id=_OWNER, job_id=_UUID4)


def _voice(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.voice.get_session(transaction, owner_id=_OWNER, session_id="session-1")


def _remote(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.remote.get_machine(transaction, owner_id=_OWNER, machine_id="machine-1")


def _remote_operation_proposals(
    catalog: api.RepositoryCatalog, transaction: FailingExecutor
) -> None:
    catalog.remote_operation_proposals.get(
        transaction,
        owner_id=_OWNER,
        proposal_id="proposal-1",
    )


def _revocations(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.revocations.pending_for_owner(transaction, owner_id=_OWNER, limit=1)


def _encrypted_llm_config(
    catalog: api.RepositoryCatalog,
    transaction: FailingExecutor,
) -> None:
    catalog.encrypted_llm_config.get_user(transaction, owner_id=_OWNER)


def _encrypted_typesafe_credential(
    catalog: api.RepositoryCatalog,
    transaction: FailingExecutor,
) -> None:
    catalog.encrypted_typesafe_credential.get_user(transaction, owner_id=_OWNER)


def _audit(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.audit.list_page(transaction, owner_id=_OWNER, limit=1)


def _audit_retention(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.audit_retention.load_anchor(
        transaction,
        chain_id=_CHAIN,
        first_retained_sequence=2,
    )


def _authority(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.authority.get_binding(
        transaction,
        owner_id=_OWNER,
        binding_id="binding-1",
    )


def _outbox(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.outbox.claim(
        transaction,
        worker_id=_WORKER,
        topics=("contract.topic",),
        now=_NOW,
        lease_duration=timedelta(seconds=30),
        limit=1,
    )


def _purge(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.purge.load(transaction, owner_id=_OWNER, tombstone_id="tombstone-1")


def _quality_audit(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.quality_audit.get_run(
        transaction,
        owner_id=_OWNER,
        run_id="run-1",
    )


def _saved_components(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.saved_components.get_scoped(
        transaction,
        owner_id=_OWNER,
        conversation_id="conversation-1",
        component_id="component-1",
    )


def _share_grants(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.share_grants.list_grants(transaction, owner_id=_OWNER, limit=1)


def _tool_policy_state(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.tool_policy_state.list_all_scopes(transaction, owner_id=_OWNER)


def _tracked_jobs(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.tracked_jobs.get(
        transaction,
        owner_id=_OWNER,
        tracked_job_id="tracked-1",
    )


def _tutorials(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    catalog.tutorials.get(transaction, step_id=1)


def _work_admission(catalog: api.RepositoryCatalog, transaction: FailingExecutor) -> None:
    from astralplane.repositories.work_admission import OperationOwner, OwnerScope

    catalog.work_admission.query_operation(
        transaction,
        OperationOwner(
            owner_scope=OwnerScope.USER,
            owner_user_id=_OWNER,
            connection_scope_id=None,
        ),
        uuid.UUID(_UUID4),
    )


REPOSITORY_CONTRACT_MATRIX = (
    RepositoryContract(
        "assignments",
        api.create_assignment_repository,
        "src/astralplane/repositories/assignments.py",
        _assignments,
        _OWNER,
        "owner_user_id=%s",
        ("FOR UPDATE SKIP LOCKED", "expected_control_epoch"),
        ("assignment_idempotency_conflict", "submission_digest"),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "agent_management",
        api.create_agent_management_repository,
        "src/astralplane/repositories/agent_management.py",
        _agent_management,
        _OWNER,
        "SELECT %s::text AS owner_id",
        (),
        (),
        "The projection is read-only; replay and write concurrency do not apply.",
        ("RepositoryDataError",),
    ),
    RepositoryContract(
        "agents",
        api.create_agent_repository,
        "src/astralplane/repositories/agents.py",
        _agents,
        _OWNER,
        "owner_user_id = %s",
        ("state_revision",),
        ("ON CONFLICT", "conflicting semantics"),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "artifacts",
        api.create_artifact_repository,
        "src/astralplane/repositories/artifacts.py",
        _artifacts,
        _OWNER,
        "user_id = %s",
        ("FOR UPDATE", "ON CONFLICT"),
        ("ON CONFLICT (attachment_id) DO NOTHING",),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "attachment_parsers",
        api.create_attachment_parser_repository,
        "src/astralplane/repositories/attachment_parsers.py",
        _attachment_parsers,
        _OWNER,
        "requested_by = %s",
        ("expected_updated_at",),
        ("ON CONFLICT (gap_fingerprint)", "GAP_ALREADY_CLAIMED"),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "audit",
        api.create_audit_repository,
        "src/astralplane/repositories/audit.py",
        _audit,
        _OWNER,
        "actor_user_id = %s",
        ("pg_advisory_xact_lock",),
        ("audit_idempotency_conflict",),
        None,
        ("PlaneError",),
    ),
    RepositoryContract(
        "audit_retention",
        api.create_audit_retention_repository,
        "src/astralplane/audit_retention.py",
        _audit_retention,
        _CHAIN,
        "owner_or_chain = %s",
        ("FOR UPDATE", "pg_advisory_xact_lock"),
        ("ON CONFLICT",),
        None,
        ("AuditRetentionError",),
    ),
    RepositoryContract(
        "authority",
        api.create_authority_repository,
        "src/astralplane/authority/repository.py",
        _authority,
        _OWNER,
        "owner_id = %s",
        ("FOR UPDATE", "expected_version", "savepoint"),
        ("ON CONFLICT", "AuthorityIdempotencyConflictError"),
        None,
        ("ReceiptClaimConflictError", "ReceiptWatermarkConflictError"),
    ),
    RepositoryContract(
        "background_tasks",
        api.create_background_task_repository,
        "src/astralplane/repositories/background_tasks.py",
        _background_tasks,
        _OWNER,
        "user_id = %s",
        ("operation_execution_generation IS NOT DISTINCT FROM %s",),
        ("ON CONFLICT (task_id)", "replay changed immutable state"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "chat_queue",
        api.create_chat_queue_repository,
        "src/astralplane/repositories/chat_queue.py",
        _chat_queue,
        _OWNER,
        "owner_id = %s",
        ("pg_advisory_xact_lock", "FOR UPDATE", "revision = %s"),
        ("ON CONFLICT (item_id)", "replay changed durable semantics"),
        None,
        (
            "RepositoryConflictError",
            "RepositoryDataError",
            "RepositoryNotFoundError",
            "RepositoryValidationError",
        ),
    ),
    RepositoryContract(
        "chat_read_receipts",
        api.create_chat_read_receipt_repository,
        "src/astralplane/repositories/chat_read_receipts.py",
        _chat_read_receipts,
        _OWNER,
        "owner_id = %s AND chat_id = %s",
        ("last_seen_position < EXCLUDED.last_seen_position", "revision = %s"),
        ("ON CONFLICT (owner_id, chat_id)", "read receipt revision is stale"),
        None,
        (
            "RepositoryConflictError",
            "RepositoryNotFoundError",
            "RepositoryValidationError",
        ),
    ),
    RepositoryContract(
        "chat_steps",
        api.create_chat_step_repository,
        "src/astralplane/repositories/chat_steps.py",
        _chat_steps,
        _OWNER,
        "user_id = %s",
        ("expected_status", "ended_at IS NULL"),
        ("ON CONFLICT (id)", "step replay changed immutable semantics"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "derived_chat",
        api.create_derived_chat_repository,
        "src/astralplane/repositories/derived_chat.py",
        _derived_chat,
        _OWNER,
        "event_id = %s AND user_id = %s",
        ("pg_advisory_xact_lock", "event already derived a different"),
        ("event already derived a different message", "chat identity already exists"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "event_log",
        api.create_event_log_repository,
        "src/astralplane/repositories/mesh_events.py",
        _event_log,
        _OWNER,
        "owner_id = %s AND mesh_id = %s AND event_id = %s",
        ("FOR UPDATE", "head_seq = %s", "feed is frozen by fork evidence"),
        ("event identity was reused with different semantics", "ON CONFLICT DO NOTHING"),
        None,
        ("RepositoryConflictError", "RepositoryDataError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "event_roster",
        api.create_event_roster_repository,
        "src/astralplane/repositories/mesh_roster.py",
        _event_roster,
        _OWNER,
        "owner_id = %s AND mesh_id = %s AND entry_id = %s",
        ("ON CONFLICT (owner_id, mesh_id, entry_id) DO NOTHING",),
        ("roster entry identity was reused with different semantics", "ON CONFLICT"),
        None,
        ("RepositoryConflictError", "RepositoryDataError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "learning_ledger",
        api.create_learning_ledger_repository,
        "src/astralplane/repositories/learning_ledger.py",
        _learning_ledger,
        _OWNER,
        "owner_id = %s AND mesh_id = %s AND event_id = %s",
        ("pg_advisory_xact_lock", "proposal already has a decision"),
        ("ledger event identity was reused with different semantics", "already_reversed"),
        None,
        ("RepositoryConflictError", "RepositoryDataError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "runs",
        api.create_run_repository,
        "src/astralplane/repositories/runs.py",
        _runs,
        _OWNER,
        "owner_id = %s AND mesh_id = %s AND run_id = %s",
        ("pg_advisory_xact_lock", "run already has an executor"),
        ("lease event identity was reused with different semantics", "goal event identity"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError", "RepositoryValidationError"),
    ),
    RepositoryContract(
        "conversation_files",
        api.create_conversation_file_repository,
        "src/astralplane/repositories/conversation_files.py",
        _conversation_files,
        _OWNER,
        "user_id = %s",
        (),
        (),
        "The serial-only legacy table has no stable caller idempotency identity.",
        ("RepositoryNotFoundError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "credentials",
        api.create_credential_repository,
        "src/astralplane/repositories/credentials.py",
        _credentials,
        _OWNER,
        "user_id = %s",
        ("expected_updated_at",),
        ("ON CONFLICT", "replay changed stored semantics"),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "draft_agents",
        api.create_draft_agent_repository,
        "src/astralplane/repositories/drafts.py",
        _draft_agents,
        _OWNER,
        "user_id = %s",
        ("expected_revision", "FOR UPDATE"),
        ("ON CONFLICT", "replay changed semantics"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "generated_agent_publications",
        api.create_generated_agent_publication_repository,
        "src/astralplane/repositories/generated_agent_publications.py",
        _generated_agent_publications,
        _OWNER,
        "user_id = %s",
        ("FOR UPDATE OF publication, draft", "state_revision"),
        ("_assert_begin_replay", "_is_transition_replay"),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "encrypted_llm_config",
        api.create_encrypted_llm_config_repository,
        "src/astralplane/repositories/secrets.py",
        _encrypted_llm_config,
        _OWNER,
        "user_id = %s",
        ("ON CONFLICT",),
        ("DO UPDATE SET",),
        None,
        ("RepositoryDataError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "encrypted_typesafe_credential",
        api.create_encrypted_typesafe_credential_repository,
        "src/astralplane/repositories/secrets.py",
        _encrypted_typesafe_credential,
        _OWNER,
        "user_id = %s",
        ("ON CONFLICT",),
        ("DO UPDATE SET",),
        None,
        ("RepositoryDataError", "RepositoryValidationError"),
    ),
    RepositoryContract(
        "framework_credentials",
        api.create_framework_credential_repository,
        "src/astralplane/repositories/framework_credentials.py",
        _framework_credentials,
        _OWNER,
        "WHERE owner_id=%s ORDER BY",
        ("pg_advisory_xact_lock(hashtextextended(%s,79))", "FOR UPDATE"),
        (),
        "mint-only issuance; each call always creates a new independent credential",
        ("RepositoryConflictError", "RepositoryNotFoundError", "RepositoryValidationError"),
    ),
    RepositoryContract(
        "history",
        api.create_history_repository,
        "src/astralplane/repositories/history.py",
        _history,
        _OWNER,
        "user_id = %s",
        ("ON CONFLICT",),
        ("idempotency identity",),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "harness_cleanup",
        api.create_harness_cleanup_repository,
        "src/astralplane/repositories/harness_cleanup.py",
        _harness_cleanup,
        r"\_\_verif\_\_contract-run\_%",
        "user_id LIKE %s",
        ("HarnessCleanupProfile", "literal_prefix"),
        ("DELETE FROM",),
        None,
        ("RepositoryValidationError",),
    ),
    RepositoryContract(
        "identity",
        api.create_identity_repository,
        "src/astralplane/repositories/identity.py",
        _identity,
        _OWNER,
        "WHERE id = %s",
        ("ON CONFLICT",),
        ("DO UPDATE SET",),
        None,
        ("RepositoryDataError",),
    ),
    RepositoryContract(
        "knowledge",
        api.create_knowledge_repository,
        "src/astralplane/repositories/knowledge.py",
        _knowledge,
        _OWNER,
        "chat.user_id = %s",
        ("computed_at = %s", "status = %s"),
        ("ON CONFLICT (agent_id, tool_name, window_end)", "advisory_xact_lock"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "maintenance",
        api.create_maintenance_repository,
        "src/astralplane/repositories/maintenance.py",
        _maintenance,
        _OWNER,
        "owner_user_id = %s",
        ("lease_token = %s", "claim_generation = %s", "state_revision = %s"),
        ("ON CONFLICT (unit_kind, idempotency_key)", "changed input membership"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "mesh_enrollment",
        api.create_mesh_enrollment_repository,
        "src/astralplane/repositories/mesh_enrollment.py",
        _mesh_enrollment,
        _OWNER,
        "owner_id = %s",
        ("FOR UPDATE", "record_version = %s", "membership_epoch"),
        ("ON CONFLICT", "replay changed"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError", "RepositoryValidationError"),
    ),
    RepositoryContract(
        "verification_credentials",
        api.create_verification_credential_repository,
        "src/astralplane/repositories/verification_credentials.py",
        _verification_credentials,
        _OWNER,
        "owner_id = %s",
        ("pg_advisory_xact_lock", "FOR UPDATE", "record_version = %s"),
        ("ON CONFLICT", "replay changed immutable semantics"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError", "RepositoryValidationError"),
    ),
    RepositoryContract(
        "notifications",
        api.create_notification_repository,
        "src/astralplane/repositories/notifications.py",
        _notifications,
        _OWNER,
        "owner_id = %s",
        ("FOR UPDATE", "revision = %s"),
        ("ON CONFLICT", "deduplication_key"),
        None,
        (
            "RepositoryConflictError",
            "RepositoryNotFoundError",
            "RepositoryValidationError",
            "RepositoryDataError",
        ),
    ),
    RepositoryContract(
        "push_deliveries",
        api.create_push_delivery_repository,
        "src/astralplane/repositories/push_deliveries.py",
        _push_deliveries,
        _OWNER,
        "owner_id = %s",
        (
            "FOR UPDATE OF delivery SKIP LOCKED",
            "FOR SHARE OF destination",
            "claim_id = %s",
        ),
        ("ON CONFLICT", "delivery_key"),
        None,
        (
            "RepositoryConflictError",
            "RepositoryDataError",
            "RepositoryNotFoundError",
            "RepositoryValidationError",
        ),
    ),
    RepositoryContract(
        "projects",
        api.create_project_repository,
        "src/astralplane/repositories/projects.py",
        _projects,
        _OWNER,
        "owner_id=%s AND project_id=%s",
        ("pg_advisory_xact_lock", "FOR UPDATE", "revision=%s"),
        ("create_digest", "identity was reused"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError", "RepositoryValidationError"),
    ),
    RepositoryContract(
        "observations",
        api.create_observation_repository,
        "src/astralplane/repositories/observations.py",
        _observations,
        _OWNER,
        "owner_id=%s AND conversation_id=%s",
        ("pg_advisory_xact_lock", "FOR UPDATE", "purged_at IS NULL", "capture_seq"),
        ("request_digest", "identity was reused"),
        None,
        ("RepositoryConflictError", "RepositoryDataError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "offline_grants",
        api.create_offline_grant_repository,
        "src/astralplane/repositories/offline_grants.py",
        _offline_grants,
        _OWNER,
        "user_id = %s",
        ("ON CONFLICT",),
        ("replay changed immutable semantics",),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "outbox",
        api.create_outbox_store,
        "src/astralplane/outbox.py",
        _outbox,
        _WORKER,
        "lease_owner = %s",
        ("FOR UPDATE SKIP LOCKED", "expected_version"),
        ("idempotency_key", "outbox_idempotency_conflict"),
        None,
        ("PlaneError",),
    ),
    RepositoryContract(
        "preferences",
        api.create_preferences_repository,
        "src/astralplane/repositories/preferences.py",
        _preferences,
        _OWNER,
        "user_id = %s",
        ("ON CONFLICT",),
        ("idempotency identity",),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "personalization_graph",
        api.create_personalization_graph_repository,
        "src/astralplane/repositories/personalization_graph.py",
        _personalization_graph,
        _OWNER,
        "link.user_id = %s",
        ("ON CONFLICT (user_id, memory_id, linked_id)",),
        ("ON CONFLICT (id)", "replay changed immutable semantics"),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "purge",
        api.create_purge_store,
        "src/astralplane/purge.py",
        _purge,
        _OWNER,
        "owner_id = %s",
        ("expected_version",),
        ("purge_idempotency_conflict",),
        None,
        ("PlaneError",),
    ),
    RepositoryContract(
        "quality_audit",
        api.create_quality_audit_repository,
        "src/astralplane/repositories/quality_audit.py",
        _quality_audit,
        _OWNER,
        "owner_id = %s",
        ("expected_status",),
        ("ON CONFLICT (id) DO NOTHING", "idempotency identity"),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "remote",
        api.create_remote_repository,
        "src/astralplane/repositories/remote.py",
        _remote,
        _OWNER,
        "owner_user_id = %s",
        ("expected_updated_at", "expected_failure_count"),
        ("ON CONFLICT", "conflicting semantics"),
        None,
        ("PlaneError",),
    ),
    RepositoryContract(
        "remote_operation_proposals",
        api.create_remote_operation_proposal_repository,
        "src/astralplane/repositories/remote_proposals.py",
        _remote_operation_proposals,
        _OWNER,
        "owner_user_id = %s",
        ("status = 'pending'", "expires_at"),
        ("ON CONFLICT", "conflicting semantics"),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "revocations",
        api.create_revocation_repository,
        "src/astralplane/repositories/revocations.py",
        _revocations,
        _OWNER,
        "user_id = %s",
        ("expected_attempts",),
        (),
        "Each enqueue is a distinct external revocation attempt; replay is caller-owned.",
        ("RepositoryNotFoundError",),
    ),
    RepositoryContract(
        "saved_components",
        api.create_saved_component_repository,
        "src/astralplane/repositories/workspaces.py",
        _saved_components,
        _OWNER,
        "user_id = %s",
        ("expected_updated_at",),
        ("ON CONFLICT", "idempotency identity"),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "scheduler",
        api.create_scheduler_repository,
        "src/astralplane/repositories/scheduler.py",
        _scheduler,
        _OWNER,
        "user_id = %s",
        ("FOR UPDATE", "SKIP LOCKED"),
        ("idempotency",),
        None,
        ("PlaneError",),
    ),
    RepositoryContract(
        "share_grants",
        api.create_share_grant_repository,
        "src/astralplane/repositories/share_grants.py",
        _share_grants,
        _OWNER,
        "user_id = %s",
        ("ON CONFLICT",),
        ("replay changed immutable semantics",),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "stop_epochs",
        api.create_stop_epoch_repository,
        "src/astralplane/repositories/stop_epochs.py",
        _stop_epochs,
        _OWNER,
        "owner_id = %s",
        ("FOR UPDATE", "revision = %s", "epoch = %s"),
        ("ON CONFLICT", "replay changed immutable"),
        None,
        ("RepositoryConflictError", "RepositoryValidationError"),
    ),
    RepositoryContract(
        "tool_policy_state",
        api.create_tool_policy_state_repository,
        "src/astralplane/repositories/tool_policy.py",
        _tool_policy_state,
        _OWNER,
        "user_id = %s",
        ("FOR UPDATE",),
        ("ON CONFLICT",),
        None,
        ("RepositoryDataError",),
    ),
    RepositoryContract(
        "tracked_jobs",
        api.create_tracked_job_repository,
        "src/astralplane/repositories/tracked_jobs.py",
        _tracked_jobs,
        _OWNER,
        "owner_user_id = %s",
        ("last_polled_at IS NOT DISTINCT FROM %s", "expected_fail_count"),
        ("ON CONFLICT DO NOTHING", "replay changed immutable state"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "tutorials",
        api.create_tutorial_repository,
        "src/astralplane/repositories/tutorials.py",
        _tutorials,
        1,
        "WHERE id = %s",
        ("expected_updated_at",),
        ("ON CONFLICT (slug) DO NOTHING",),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "voice",
        api.create_voice_repository,
        "src/astralplane/repositories/voice.py",
        _voice,
        _OWNER,
        "user_id = %s",
        ("expected_generation", "expected_state"),
        ("ON CONFLICT", "idempotency_conflict"),
        None,
        ("PlaneError",),
    ),
    RepositoryContract(
        "work_admission",
        api.create_work_admission_repository,
        "src/astralplane/repositories/work_admission.py",
        _work_admission,
        _OWNER,
        "owner_user_id = %s",
        ("FOR UPDATE", "execution_generation"),
        ("idempotency_key", "idempotency_conflict"),
        None,
        ("WorkAdmissionNotFoundError", "WorkAdmissionIntegrityError"),
    ),
    RepositoryContract(
        "workspaces",
        api.create_workspace_repository,
        "src/astralplane/repositories/workspaces.py",
        _workspaces,
        _OWNER,
        "user_id = %s",
        ("ON CONFLICT",),
        ("idempotency identity",),
        None,
        ("RepositoryConflictError", "RepositoryDataError"),
    ),
    RepositoryContract(
        "completion_wake",
        api.create_completion_wake_repository,
        "src/astralplane/repositories/completion_wake.py",
        _completion_wake,
        _UUID4,
        "subscription_id",
        ("revoked_at IS NULL", "rowcount"),
        ("idempotency_key", "replay"),
        None,
        ("RepositoryConflictError", "RepositoryNotFoundError", "RepositoryValidationError"),
    ),
    RepositoryContract(
        "computer_use",
        api.create_computer_use_repository,
        "src/astralplane/repositories/computer_use.py",
        _computer_use,
        _OWNER,
        "owner_id = %s",
        ("revision = %s", "state = 'pending'"),
        ("ON CONFLICT (session_id) DO NOTHING", "conflicting semantics"),
        None,
        (
            "RepositoryConflictError",
            "RepositoryNotFoundError",
            "RepositoryDataError",
            "RepositoryValidationError",
        ),
    ),
    RepositoryContract(
        "capability_publications",
        api.create_capability_publication_repository,
        "src/astralplane/repositories/capability_publications.py",
        _capability_publications,
        _OWNER,
        "owner_id = %s",
        ("FOR UPDATE", "state_revision = %s", "publication_state = 'published'"),
        ("ON CONFLICT", "replay", "capability publication revision is stale"),
        None,
        (
            "CapabilityPublicationConflictError",
            "RepositoryNotFoundError",
            "RepositoryDataError",
        ),
    ),
    RepositoryContract(
        "atlas_pages",
        api.create_atlas_page_repository,
        "src/astralplane/repositories/atlas_pages.py",
        _atlas_pages,
        _OWNER,
        "owner_id=%s AND page_id=%s",
        ("pg_advisory_xact_lock", "FOR UPDATE", "revision=%s", "deleted_at IS NULL"),
        ("request_digest", "command identity was reused", "expected_revision"),
        None,
        ("RepositoryConflictError", "RepositoryDataError", "RepositoryNotFoundError"),
    ),
    RepositoryContract(
        "atlas_links",
        api.create_atlas_link_repository,
        "src/astralplane/repositories/atlas_links.py",
        _atlas_links,
        _OWNER,
        "owner_id=%s AND page_id=%s",
        ("pg_advisory_xact_lock", "FOR UPDATE", "link revision changed", "deleted_at IS NULL"),
        ("request_digest", "command identity was reused"),
        None,
        ("RepositoryConflictError", "RepositoryDataError", "RepositoryNotFoundError"),
    ),
)


@dataclass(frozen=True, slots=True)
class BehavioralEvidence:
    key: str
    scope_node: str
    replay_node: str | None
    concurrency_node: str | None
    failure_node: str
    replay_not_applicable: str | None = None
    concurrency_not_applicable: str | None = None


BEHAVIORAL_EVIDENCE = (
    BehavioralEvidence(
        "assignments",
        "tests.repositories.test_assignments_postgres:test_public_assignment_contract_behaviors",
        "tests.repositories.test_assignments_postgres:test_public_assignment_contract_behaviors",
        "tests.repositories.test_assignments_postgres:test_public_assignment_contract_behaviors",
        "tests.repositories.test_assignments_postgres:test_public_assignment_contract_behaviors",
    ),
    BehavioralEvidence(
        "agent_management",
        "tests.repositories.test_agent_management:test_detail_context_is_three_query_typed_and_owner_scoped",
        None,
        None,
        "tests.repositories.test_agent_management:test_contexts_fail_closed_on_corruption_or_truncation",
        "read-only projection",
        "read-only projection",
    ),
    BehavioralEvidence(
        "agents",
        "tests.repositories.test_agents:test_ownership_is_immutable_and_visibility_write_requires_the_owner",
        "tests.repositories.test_agents:test_user_agent_create_is_same_owner_idempotent_and_tombstone_safe",
        "tests.repositories.test_agents:test_agent_update_uses_owner_and_revision_compare_and_set",
        "tests.repositories.test_agents:test_revision_reads_lists_transitions_and_replay_conflicts",
    ),
    BehavioralEvidence(
        "artifacts",
        "tests.repositories.test_artifacts:test_attachment_reads_are_owner_scoped_and_paginated",
        "tests.repositories.test_artifacts:test_pending_materialization_begins_and_replays_hidden_identity",
        "tests.repositories.test_artifacts:test_pending_lease_renews_by_db_clock_and_exact_version",
        "tests.repositories.test_artifacts:test_pending_materialization_conflicts_are_not_reported_as_success",
    ),
    BehavioralEvidence(
        "attachment_parsers",
        "tests.repositories.test_attachment_parsers:test_claim_pending_deduplicates_foreign_gap_without_leaking_provenance",
        "tests.repositories.test_attachment_parsers:test_claim_pending_accepts_an_owner_replay_without_another_write",
        "tests.repositories.test_attachment_parsers:test_administrative_promotion_is_a_pending_timestamp_cas",
        "tests.repositories.test_attachment_parsers:test_claim_pending_fails_closed_on_impossible_result_shapes",
    ),
    BehavioralEvidence(
        "audit",
        "tests.repositories.test_audit:test_owner_scoped_reads_and_limits",
        "tests.repositories.test_audit:test_append_continues_chain_and_is_idempotent",
        "tests.repositories.test_audit:test_append_continues_chain_and_is_idempotent",
        "tests.repositories.test_audit:test_append_conflict_and_inconsistent_return_are_visible",
    ),
    BehavioralEvidence(
        "audit_retention",
        "tests.test_audit_retention:test_prune_persists_anchor_before_delete_and_reports_count",
        "tests.test_audit_retention:test_repeat_prune_requires_and_reuses_valid_anchor",
        "tests.test_audit_retention:test_prune_persists_anchor_before_delete_and_reports_count",
        "tests.test_audit_retention:test_prune_failure_propagates_for_caller_rollback",
    ),
    BehavioralEvidence(
        "authority",
        "tests.authority.test_repository:test_binding_repository_is_owner_scoped_idempotent_and_cas_fenced",
        "tests.authority.test_repository:test_lifecycle_operations_replay_only_an_identical_request_fingerprint",
        "tests.authority.test_repository:test_receipt_claim_watermark_effect_and_outbox_share_one_savepoint",
        "tests.authority.test_repository:test_binding_conflicts_and_corrupt_rows_are_typed",
    ),
    BehavioralEvidence(
        "background_tasks",
        "tests.repositories.test_background_tasks:test_owner_get_and_bounded_list_never_cross_scope",
        "tests.repositories.test_background_tasks:test_create_replay_is_idempotent_but_conflicting_or_foreign_is_rejected",
        "tests.repositories.test_background_tasks:test_mark_notified_is_an_unambiguous_owner_cas",
        "tests.repositories.test_background_tasks:test_foreign_driver_row_fails_closed",
    ),
    BehavioralEvidence(
        "chat_queue",
        "tests.repositories.test_chat_queue_postgres:test_public_chat_queue_contract_behaviors",
        "tests.repositories.test_chat_queue_postgres:test_public_chat_queue_contract_behaviors",
        "tests.repositories.test_chat_queue_postgres:test_public_chat_queue_contract_behaviors",
        "tests.repositories.test_chat_queue_postgres:test_public_chat_queue_contract_behaviors",
    ),
    BehavioralEvidence(
        "chat_read_receipts",
        "tests.repositories.test_chat_read_receipts_postgres:test_public_chat_read_receipt_contract_behaviors",
        "tests.repositories.test_chat_read_receipts_postgres:test_public_chat_read_receipt_contract_behaviors",
        "tests.repositories.test_chat_read_receipts_postgres:test_public_chat_read_receipt_contract_behaviors",
        "tests.repositories.test_chat_read_receipts_postgres:test_public_chat_read_receipt_contract_behaviors",
    ),
    BehavioralEvidence(
        "chat_steps",
        "tests.repositories.test_chat_steps:test_get_and_list_are_owner_scoped_and_chronological",
        "tests.repositories.test_chat_steps:test_create_step_accepts_exact_replay_after_terminal_transition",
        "tests.repositories.test_chat_steps:test_finish_step_uses_owner_status_and_timestamp_fences",
        "tests.repositories.test_chat_steps:test_create_rolls_back_when_turn_counter_scope_is_lost",
    ),
    BehavioralEvidence(
        "derived_chat",
        "tests.repositories.test_derived_chat_postgres:test_public_derived_chat_contract_behaviors",
        "tests.repositories.test_derived_chat_postgres:test_public_derived_chat_contract_behaviors",
        "tests.repositories.test_derived_chat_postgres:test_public_derived_chat_contract_behaviors",
        "tests.repositories.test_derived_chat_postgres:test_public_derived_chat_contract_behaviors",
    ),
    BehavioralEvidence(
        "event_log",
        "tests.repositories.test_mesh_events_postgres:test_public_event_log_contract_behaviors",
        "tests.repositories.test_mesh_events_postgres:test_public_event_log_contract_behaviors",
        "tests.repositories.test_mesh_events_postgres:test_public_event_log_contract_behaviors",
        "tests.repositories.test_mesh_events_postgres:test_public_event_log_contract_behaviors",
    ),
    BehavioralEvidence(
        "event_roster",
        "tests.repositories.test_mesh_roster_postgres:test_public_event_roster_contract_behaviors",
        "tests.repositories.test_mesh_roster_postgres:test_public_event_roster_contract_behaviors",
        "tests.repositories.test_mesh_roster_postgres:test_public_event_roster_contract_behaviors",
        "tests.repositories.test_mesh_roster_postgres:test_public_event_roster_contract_behaviors",
    ),
    BehavioralEvidence(
        "learning_ledger",
        "tests.repositories.test_learning_ledger_postgres:test_public_learning_ledger_contract_behaviors",
        "tests.repositories.test_learning_ledger_postgres:test_public_learning_ledger_contract_behaviors",
        "tests.repositories.test_learning_ledger_postgres:test_public_learning_ledger_contract_behaviors",
        "tests.repositories.test_learning_ledger_postgres:test_public_learning_ledger_contract_behaviors",
    ),
    BehavioralEvidence(
        "runs",
        "tests.repositories.test_runs_postgres:test_public_run_contract_behaviors",
        "tests.repositories.test_runs_postgres:test_public_run_contract_behaviors",
        "tests.repositories.test_runs_postgres:test_public_run_contract_behaviors",
        "tests.repositories.test_runs_postgres:test_public_run_contract_behaviors",
    ),
    BehavioralEvidence(
        "conversation_files",
        "tests.repositories.test_conversation_files:test_get_and_list_are_owner_scoped_and_deterministically_ordered",
        "tests.repositories.test_conversation_files:test_delete_is_owner_and_conversation_scoped_and_idempotent",
        None,
        "tests.repositories.test_conversation_files:test_add_mapping_rejects_missing_or_foreign_conversation",
        concurrency_not_applicable="legacy serial identity has no mutable CAS state",
    ),
    BehavioralEvidence(
        "credentials",
        "tests.repositories.test_credentials:test_user_reads_lists_and_key_inventory_remain_owner_scoped",
        "tests.repositories.test_credentials:test_user_upsert_preserves_tuple_identity_and_redacts_ciphertext",
        "tests.repositories.test_credentials:test_machine_cas_and_owner_scoped_crud",
        "tests.repositories.test_credentials:test_machine_cas_rejects_stale_or_nonadvancing_revision",
    ),
    BehavioralEvidence(
        "draft_agents",
        "tests.repositories.test_drafts:test_create_and_reads_are_owner_scoped_with_immutable_uuid_aliases",
        "tests.repositories.test_drafts:test_transition_and_publication_replays_preserve_semantics",
        "tests.repositories.test_drafts:test_draft_update_and_generation_claim_use_owner_revision_and_lease_fences",
        "tests.repositories.test_drafts:test_stale_draft_revision_is_classified_without_cross_owner_disclosure",
    ),
    BehavioralEvidence(
        "generated_agent_publications",
        "tests.repositories.test_generated_agent_publications:test_public_lookup_and_bounded_inventory_delegate_without_mutation",
        "tests.repositories.test_generated_agent_publications:test_replay_paths_are_idempotent_under_the_same_durable_identity",
        "tests.repositories.test_generated_agent_publications:test_rebind_refuses_to_steal_a_distinct_live_operation",
        "tests.repositories.test_generated_agent_publications:test_owner_claim_operation_and_snapshot_fences_fail_closed",
    ),
    BehavioralEvidence(
        "encrypted_llm_config",
        "tests.repositories.test_secrets:test_user_read_is_owner_scoped_and_ciphertext_is_redacted",
        "tests.repositories.test_secrets:test_user_upsert_uses_native_parameters_and_returns_detached_record",
        "tests.repositories.test_secrets:test_user_upsert_before_deadline_is_one_fenced_statement",
        "tests.repositories.test_secrets:test_write_requires_exactly_one_returned_record",
    ),
    BehavioralEvidence(
        "encrypted_typesafe_credential",
        "tests.repositories.test_typesafe_credential:"
        "test_read_is_owner_scoped_and_ciphertext_is_redacted",
        "tests.repositories.test_typesafe_credential:"
        "test_upsert_lands_valid_and_binds_the_fingerprint",
        "tests.repositories.test_typesafe_credential:"
        "test_record_outcome_conditions_on_the_fingerprint",
        "tests.repositories.test_typesafe_credential:"
        "test_multi_row_writes_are_refused_as_data_errors",
    ),
    BehavioralEvidence(
        "framework_credentials",
        "tests.repositories.test_framework_credentials_postgres:"
        "test_public_framework_credential_contract_behaviors",
        None,
        "tests.repositories.test_framework_credentials_postgres:"
        "test_public_framework_credential_contract_behaviors",
        "tests.repositories.test_framework_credentials_postgres:"
        "test_public_framework_credential_contract_behaviors",
        replay_not_applicable=(
            "mint-only issuance; each call always creates a new independent credential"
        ),
    ),
    BehavioralEvidence(
        "history",
        "tests.repositories.test_history:test_conversation_get_can_lock_and_exposes_snapshot_authority_time",
        "tests.repositories.test_history:test_conversation_create_is_owner_scoped_and_replay_safe",
        "tests.repositories.test_history:test_conversation_queries_rename_cas_and_delete",
        "tests.repositories.test_history:test_conversation_create_rejects_foreign_and_changed_replay",
    ),
    BehavioralEvidence(
        "harness_cleanup",
        "tests.repositories.test_harness_cleanup:test_verification_cleanup_escapes_exact_boundary_and_never_deletes_audit",
        "tests.repositories.test_harness_cleanup:test_security_cleanup_accepts_prefixed_id_and_uses_fixed_extended_manifest",
        None,
        "tests.repositories.test_harness_cleanup:test_cleanup_rejects_arbitrary_profile_and_invalid_command_metadata",
        concurrency_not_applicable="fixed administrative cleanup has no mutable replay identity",
    ),
    BehavioralEvidence(
        "identity",
        "tests.repositories.test_identity:test_identity_reads_are_subject_scoped_and_admin_inventory_is_explicit",
        "tests.repositories.test_identity:test_identity_upsert_preserves_external_subject_and_returns_typed_record",
        "tests.repositories.test_identity:test_verified_external_identity_is_atomic_unique_and_preserves_preferences",
        "tests.repositories.test_identity:test_external_identity_rejects_cross_owner_and_nonce_replay_distinctly",
    ),
    BehavioralEvidence(
        "knowledge",
        "tests.repositories.test_knowledge:test_interactions_bind_owner_conversation_or_explicit_administration",
        "tests.repositories.test_knowledge:test_quality_insert_and_exact_replay_are_idempotent",
        "tests.repositories.test_knowledge:test_quality_changed_semantics_require_timestamp_cas",
        "tests.repositories.test_knowledge:test_corrupt_persisted_lifecycle_rows_fail_closed",
    ),
    BehavioralEvidence(
        "maintenance",
        "tests.repositories.test_maintenance:test_owner_administrative_reads_and_input_listing_are_separate",
        "tests.repositories.test_maintenance:test_create_unit_replay_reuses_stable_identity_and_rejects_changed_inputs",
        "tests.repositories.test_maintenance:test_bind_and_renew_are_exact_lease_generation_revision_cas",
        "tests.repositories.test_maintenance:test_owner_read_rejects_foreign_driver_row_and_pending_query_is_bounded",
    ),
    BehavioralEvidence(
        "mesh_enrollment",
        "tests.repositories.test_mesh_enrollment_postgres:test_bootstrap_is_atomic_owner_scoped_and_replay_safe",
        "tests.repositories.test_mesh_enrollment_postgres:test_invitation_lifecycle_is_atomic_and_single_use",
        "tests.repositories.test_mesh_enrollment_postgres:test_concurrent_invitation_consumption_admits_exactly_one",
        "tests.repositories.test_mesh_enrollment_postgres:test_invalid_enrollment_inputs_fail_closed",
    ),
    BehavioralEvidence(
        "verification_credentials",
        "tests.repositories.test_verification_credentials_postgres:test_owner_isolation_replay_and_limit_are_atomic",
        "tests.repositories.test_verification_credentials_postgres:test_revocation_cas_replay_and_challenge_single_use",
        "tests.repositories.test_verification_credentials_postgres:test_concurrent_revocation_never_returns_active",
        "tests.repositories.test_verification_credentials_postgres:test_invalid_inputs_and_foreign_rows_fail_closed",
    ),
    BehavioralEvidence(
        "notifications",
        "tests.test_notifications_089_005:test_preferences_read_is_owner_scoped",
        "tests.test_notifications_089_005:test_entry_exact_replay_returns_original",
        "tests.test_notifications_089_005:test_acknowledgment_stale_revision_conflicts",
        "tests.test_notifications_089_005:test_deleted_entry_cannot_be_acknowledged",
    ),
    BehavioralEvidence(
        "push_deliveries",
        "tests.repositories.test_push_deliveries_postgres:"
        "test_public_push_delivery_contract_behaviors",
        "tests.repositories.test_push_deliveries_postgres:"
        "test_public_push_delivery_contract_behaviors",
        "tests.repositories.test_push_deliveries_postgres:"
        "test_public_push_delivery_contract_behaviors",
        "tests.repositories.test_push_deliveries_postgres:"
        "test_public_push_delivery_contract_behaviors",
    ),
    BehavioralEvidence(
        "projects",
        "tests.test_projects:test_public_project_contract_behaviors",
        "tests.test_projects:test_public_project_contract_behaviors",
        "tests.test_projects:test_public_project_contract_behaviors",
        "tests.test_projects:test_public_project_contract_behaviors",
    ),
    BehavioralEvidence(
        "observations",
        "tests.repositories.test_observations_postgres:test_public_observation_contract_behaviors",
        "tests.repositories.test_observations_postgres:test_public_observation_contract_behaviors",
        "tests.repositories.test_observations_postgres:test_public_observation_contract_behaviors",
        "tests.repositories.test_observations_postgres:test_public_observation_contract_behaviors",
    ),
    BehavioralEvidence(
        "offline_grants",
        "tests.repositories.test_offline_grants:test_owner_get_and_exchange_lookup_use_owner_and_live_predicates",
        "tests.repositories.test_offline_grants:test_create_grant_accepts_exact_replay_after_lifecycle_change",
        "tests.repositories.test_offline_grants:test_owner_revoke_uses_one_timestamp_and_counts_transitions_only",
        "tests.repositories.test_offline_grants:test_owner_revoke_uses_one_timestamp_and_counts_transitions_only",
    ),
    BehavioralEvidence(
        "outbox",
        "tests.test_outbox:test_ack_retry_and_dead_letter_are_worker_and_version_fenced",
        "tests.test_outbox:test_idempotent_replay_is_a_noop_but_changed_semantics_fail_closed",
        "tests.test_outbox:test_claim_orders_available_work_and_uses_skip_locked_postgresql",
        "tests.test_outbox:test_invalid_database_records_and_row_counts_fail_closed",
    ),
    BehavioralEvidence(
        "preferences",
        "tests.repositories.test_preferences:test_feedback_submit_replay_and_owner_scope",
        "tests.repositories.test_preferences:test_feedback_submit_replay_and_owner_scope",
        "tests.repositories.test_preferences:test_feedback_amend_active_uses_owner_lifecycle_and_timestamp_cas",
        "tests.repositories.test_preferences:test_feedback_submit_handles_foreign_identity_and_semantic_conflict",
    ),
    BehavioralEvidence(
        "personalization_graph",
        "tests.repositories.test_personalization_graph:test_links_are_bidirectional_and_both_endpoints_are_owner_bound",
        "tests.repositories.test_personalization_graph:test_signal_create_and_replay_are_idempotent",
        "tests.repositories.test_personalization_graph:test_links_are_bidirectional_and_both_endpoints_are_owner_bound",
        "tests.repositories.test_personalization_graph:test_missing_endpoint_or_partial_pair_fails_closed",
    ),
    BehavioralEvidence(
        "purge",
        "tests.test_purge:test_wrong_owner_cannot_read_delete_or_transition_another_owners_blob",
        "tests.test_purge:test_attachment_schedule_atomically_soft_deletes_and_replays_first_intent",
        "tests.test_purge:test_concurrent_executor_winner_is_reconciled_as_idempotent_replay",
        "tests.test_purge:test_database_success_then_blob_failure_remains_visible_and_retryable",
    ),
    BehavioralEvidence(
        "quality_audit",
        "tests.repositories.test_quality_audit:test_case_create_get_lists_and_replay",
        "tests.repositories.test_quality_audit:test_run_create_get_latest_and_replay",
        "tests.repositories.test_quality_audit:test_case_verification_transition_is_cas_fenced",
        "tests.repositories.test_quality_audit:test_atomic_review_missing_replay_and_write_failures_are_distinct",
    ),
    BehavioralEvidence(
        "remote",
        "tests.repositories.test_remote:test_machine_resolve_list_delete_are_owner_scoped",
        "tests.repositories.test_remote:test_machine_create_replay_reads_and_conflict",
        "tests.repositories.test_remote:test_probe_preserves_first_trusted_key_and_compare_and_set",
        "tests.repositories.test_remote:test_execution_update_uses_owner_state_and_failure_fences",
    ),
    BehavioralEvidence(
        "remote_operation_proposals",
        "tests.repositories.test_remote_proposals:test_get_is_owner_scoped_and_rejects_foreign_rows",
        "tests.repositories.test_remote_proposals:test_create_and_exact_replay_are_idempotent",
        "tests.repositories.test_remote_proposals:test_decision_is_pending_owner_and_expiry_fenced",
        "tests.repositories.test_remote_proposals:test_create_rejects_changed_or_foreign_replay",
    ),
    BehavioralEvidence(
        "revocations",
        "tests.repositories.test_revocations:test_pending_queue_is_owner_scoped_and_ordered",
        None,
        "tests.repositories.test_revocations:test_attempt_increment_uses_owner_and_compare_and_set_fence",
        "tests.repositories.test_revocations:test_attempt_increment_reports_fence_or_owner_miss",
        replay_not_applicable="each enqueue represents a distinct external attempt",
    ),
    BehavioralEvidence(
        "saved_components",
        "tests.repositories.test_saved_components:test_saved_component_name_preserves_ordered_authoritative_read_and_cas",
        "tests.repositories.test_workspaces:test_canvas_create_and_exact_replay_preserve_owner_scope",
        "tests.repositories.test_saved_components:test_saved_component_name_preserves_ordered_authoritative_read_and_cas",
        "tests.repositories.test_workspaces:test_canvas_create_distinguishes_owner_missing_scope_and_semantic_conflict",
    ),
    BehavioralEvidence(
        "scheduler",
        "tests.repositories.test_scheduler:test_complete_job_contracts_are_owner_scoped",
        "tests.repositories.test_scheduler:test_run_now_materialization_replays_exact_owner_submission",
        "tests.repositories.test_scheduler:test_claim_assertion_attachment_start_and_retry_are_fenced",
        "tests.repositories.test_scheduler:test_job_conflict_is_visible",
    ),
    BehavioralEvidence(
        "share_grants",
        "tests.repositories.test_share_grants:test_owner_list_returns_metadata_only_and_is_bounded",
        "tests.repositories.test_share_grants:test_create_accepts_exact_digest_replay_after_open",
        "tests.repositories.test_share_grants:test_record_open_rechecks_digest_revocation_and_expiry_atomically",
        "tests.repositories.test_share_grants:test_record_open_rechecks_digest_revocation_and_expiry_atomically",
    ),
    BehavioralEvidence(
        "stop_epochs",
        "tests.repositories.test_stop_epochs_postgres:test_stop_owner_isolation",
        "tests.repositories.test_stop_epochs_postgres:test_stop_exact_replay",
        "tests.repositories.test_stop_epochs_postgres:test_stop_first_engagement_race",
        "tests.repositories.test_stop_epochs_postgres:test_stop_caller_failure_rolls_back",
    ),
    BehavioralEvidence(
        "tool_policy_state",
        "tests.repositories.test_tool_policy:test_scope_and_override_state_is_owner_scoped_and_neutral",
        "tests.repositories.test_tool_policy:test_legacy_override_backfill_never_overwrites_an_existing_choice",
        "tests.repositories.test_tool_policy:test_tool_selection_updates_one_locked_preferences_document_without_lost_merge",
        "tests.repositories.test_tool_policy:test_corrupt_preferences_fail_closed_and_cleanup_stays_bounded",
    ),
    BehavioralEvidence(
        "tracked_jobs",
        "tests.repositories.test_tracked_jobs:test_get_and_lists_are_explicit_about_owner_or_administration",
        "tests.repositories.test_tracked_jobs:test_create_and_exact_replay_are_idempotent",
        "tests.repositories.test_tracked_jobs:test_poll_update_is_owner_fail_count_and_timestamp_cas",
        "tests.repositories.test_tracked_jobs:test_owner_read_rejects_foreign_driver_row",
    ),
    BehavioralEvidence(
        "tutorials",
        "tests.repositories.test_tutorials:test_get_and_bounded_read_surfaces",
        "tests.repositories.test_tutorials:test_seed_is_idempotent_without_overwriting_existing_copy",
        "tests.repositories.test_tutorials:test_update_applies_only_real_changes_and_records_revision",
        "tests.repositories.test_tutorials:test_create_reports_duplicate_slug_and_impossible_conflict",
    ),
    BehavioralEvidence(
        "voice",
        "tests.repositories.test_voice:test_session_reads_hide_cross_owner_rows",
        "tests.repositories.test_voice:test_create_session_is_idempotent_and_owner_scoped",
        "tests.repositories.test_voice:test_session_transitions_use_generation_and_owner_fences",
        "tests.repositories.test_voice:test_session_transition_rejections_are_visible",
    ),
    BehavioralEvidence(
        "work_admission",
        "tests.repositories.test_work_admission:test_voice_per_owner_capacity_refuses_before_slot_selection_or_insert",
        "tests.repositories.test_work_admission:test_accepted_submission_replay_returns_original_without_inserting",
        "tests.repositories.test_work_admission:test_stale_fence_is_a_typed_compare_and_set_error",
        "tests.repositories.test_work_admission:test_repository_refuses_non_transaction_callers",
    ),
    BehavioralEvidence(
        "workspaces",
        "tests.repositories.test_workspaces:test_canvas_create_and_exact_replay_preserve_owner_scope",
        "tests.repositories.test_workspaces:test_publication_commit_at_head_is_atomic_and_replay_safe",
        "tests.repositories.test_workspaces:test_canvas_replace_distinguishes_missing_conflict_and_bad_fence",
        "tests.repositories.test_workspaces:test_canvas_create_distinguishes_owner_missing_scope_and_semantic_conflict",
    ),
    BehavioralEvidence(
        "completion_wake",
        "tests.test_completion_wake_089_004:test_delete_is_owner_fenced",
        "tests.test_completion_wake_089_004:test_accept_happy_path_inserts_fenced",
        "tests.test_completion_wake_089_004:test_accept_loses_race_to_revoke",
        "tests.test_completion_wake_089_004:test_revoke_loses_race_to_concurrent_revoke",
    ),
    BehavioralEvidence(
        "computer_use",
        "tests.repositories.test_computer_use_postgres:"
        "test_public_computer_use_contract_behaviors",
        "tests.repositories.test_computer_use_postgres:"
        "test_public_computer_use_contract_behaviors",
        "tests.repositories.test_computer_use_postgres:"
        "test_public_computer_use_contract_behaviors",
        "tests.repositories.test_computer_use_postgres:"
        "test_public_computer_use_contract_behaviors",
    ),
    BehavioralEvidence(
        "capability_publications",
        "tests.repositories.test_capability_publications_postgres:"
        "test_owner_scope_and_active_reads_never_cross_owners",
        "tests.repositories.test_capability_publications_postgres:"
        "test_exact_publish_replay_is_idempotent_and_changed_replay_conflicts",
        "tests.repositories.test_capability_publications_postgres:"
        "test_stale_withdraw_and_concurrent_first_publish_are_fenced",
        "tests.repositories.test_capability_publications_postgres:"
        "test_caller_failure_rolls_back_and_restart_reads_durable_state",
    ),
    BehavioralEvidence(
        "atlas_pages",
        "tests.repositories.test_atlas_revisions_postgres:"
        "test_public_atlas_revision_contract_behaviors",
        "tests.repositories.test_atlas_revisions_postgres:"
        "test_public_atlas_revision_contract_behaviors",
        "tests.repositories.test_atlas_revisions_postgres:"
        "test_public_atlas_revision_contract_behaviors",
        "tests.repositories.test_atlas_revisions_postgres:"
        "test_public_atlas_revision_contract_behaviors",
    ),
    BehavioralEvidence(
        "atlas_links",
        "tests.repositories.test_atlas_links_postgres:test_public_atlas_link_contract_behaviors",
        "tests.repositories.test_atlas_links_postgres:test_public_atlas_link_contract_behaviors",
        "tests.repositories.test_atlas_links_postgres:test_public_atlas_link_contract_behaviors",
        "tests.repositories.test_atlas_links_postgres:test_public_atlas_link_contract_behaviors",
    ),
)


def _flatten(value: object) -> tuple[object, ...]:
    if isinstance(value, Mapping):
        return tuple(item for nested in value.values() for item in _flatten(nested))
    if isinstance(value, (list, tuple)):
        return tuple(item for nested in value for item in _flatten(nested))
    return (value,)


def _run_behavioral_node(node: str) -> None:
    test_root = str(_ROOT / "tests")
    repository_test_root = str(_ROOT / "tests" / "repositories")
    for import_root in (test_root, repository_test_root):
        if import_root not in sys.path:
            sys.path.insert(0, import_root)
    module_name, separator, function_name = node.partition(":")
    assert separator and module_name.startswith("tests.") and function_name.startswith("test_")
    function = getattr(importlib.import_module(module_name), function_name)
    function()


def test_matrix_exactly_covers_the_public_repository_catalog() -> None:
    catalog = api.create_repository_catalog()
    mapping = catalog.as_mapping()

    assert tuple(case.key for case in REPOSITORY_CONTRACT_MATRIX) == tuple(mapping)
    for case in REPOSITORY_CONTRACT_MATRIX:
        assert type(mapping[case.key]) is type(case.repository_factory())

    assert tuple(case.key for case in BEHAVIORAL_EVIDENCE) == tuple(mapping)


@pytest.mark.parametrize("evidence", BEHAVIORAL_EVIDENCE, ids=lambda item: item.key)
def test_behavioral_scope_replay_concurrency_and_failure_contracts(
    evidence: BehavioralEvidence,
) -> None:
    assert bool(evidence.replay_node) != bool(evidence.replay_not_applicable)
    assert bool(evidence.concurrency_node) != bool(evidence.concurrency_not_applicable)
    nodes = (
        evidence.scope_node,
        evidence.replay_node,
        evidence.concurrency_node,
        evidence.failure_node,
    )
    for node in dict.fromkeys(node for node in nodes if node is not None):
        _run_behavioral_node(node)


@pytest.mark.parametrize("case", REPOSITORY_CONTRACT_MATRIX, ids=lambda case: case.key)
def test_scope_attribution_and_driver_failures_are_visible(case: RepositoryContract) -> None:
    failure = VisibleDriverError(case.key)
    transaction = FailingExecutor(failure)

    with pytest.raises(VisibleDriverError) as caught:
        case.probe(api.create_repository_catalog(), transaction)

    assert caught.value is failure
    assert len(transaction.calls) == 1
    _, statement, parameters = transaction.calls[0]
    assert case.scope_sql in statement
    assert case.attribution_value in _flatten(parameters)
    assert not transaction.commit_called
    assert not transaction.rollback_called


@pytest.mark.parametrize("case", REPOSITORY_CONTRACT_MATRIX, ids=lambda case: case.key)
def test_concurrency_idempotency_and_typed_failure_guards_are_explicit(
    case: RepositoryContract,
) -> None:
    source = (_ROOT / case.source_path).read_text(encoding="utf-8")

    for evidence in case.concurrency_evidence:
        assert evidence in source
    for evidence in case.idempotency_evidence:
        assert evidence in source
    for evidence in case.failure_evidence:
        assert evidence in source
    assert bool(case.idempotency_evidence) != bool(case.idempotency_not_applicable)


def test_repositories_leave_commit_and_rollback_to_the_caller() -> None:
    for source_path in {case.source_path for case in REPOSITORY_CONTRACT_MATRIX}:
        source = (_ROOT / source_path).read_text(encoding="utf-8")
        assert ".commit(" not in source, source_path
        assert ".rollback(" not in source, source_path


def test_authority_repository_and_conflicts_are_available_only_through_stable_facades() -> None:
    exported = (
        "AuthorityCompareAndSetConflictError",
        "AuthorityIdempotencyConflictError",
        "AuthorityRepository",
        "ReceiptClaimConflictError",
        "ReceiptWatermarkConflictError",
    )
    for name in exported:
        expected = getattr(authority, name)
        assert getattr(api, name) is expected
        assert getattr(astralplane, name) is expected

    assert astralplane.create_authority_repository is api.create_authority_repository
    assert authority.create_authority_repository is api.create_authority_repository
    assert isinstance(api.create_authority_repository(), authority.AuthorityRepository)
