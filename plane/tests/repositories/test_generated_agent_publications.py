"""Tests for astralplane.repositories.generated_agent_publications: manifest digesting,
begin-intent replay and fencing, claim/attempt lifecycle, and publication-path
derivation, using agent/draft/work stub doubles.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from _support import ScriptedTransaction

from astralplane.immutable_bundle_store import (
    FinalizedBundle,
    ImmutableBundleContract,
    canonical_bundle_digest,
)
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.agents import AgentRevisionRecord
from astralplane.repositories.drafts import (
    DraftAgentRecord,
    DraftPublicationRecord,
)
from astralplane.repositories.generated_agent_publications import (
    GENERATED_AGENT_BUNDLE_CONTRACT,
    GeneratedAgentPublicationRepository,
    GeneratedAgentPublicationResultMetadata,
    canonical_generated_agent_manifest_digest,
    generated_agent_publication_operation_binding,
    generated_agent_publication_paths,
    generated_agent_publication_recovery_operation_binding,
)
from astralplane.repositories.work_admission import (
    AdmissionClass,
    ExecutionFence,
    OperationRecord,
    OperationState,
    OwnerScope,
)

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
OWNER = "owner-publication"
DRAFT_UUID = "10000000-0000-4000-8000-000000000001"
CLAIM_ID = "20000000-0000-4000-8000-000000000001"
PUBLICATION_ID = "30000000-0000-4000-8000-000000000001"
REVISION_ID = "40000000-0000-4000-8000-000000000001"
PROMOTION_TOKEN = "50000000-0000-4000-8000-000000000001"
OPERATION_ID = uuid.UUID("60000000-0000-4000-8000-000000000001")
RECOVERY_OPERATION_ID = uuid.UUID("70000000-0000-4000-8000-000000000001")
LEASE_TOKEN = uuid.UUID("80000000-0000-4000-8000-000000000001")
RECOVERY_LEASE_TOKEN = uuid.UUID("90000000-0000-4000-8000-000000000001")
LOCK_DIGEST = "b" * 64
STAGING_PATH = f"staging/{DRAFT_UUID}/1/{PUBLICATION_ID}"
REVISION_PATH = f"revisions/agent-publication/{REVISION_ID}"
BUNDLE_FILES = {
    "agent_main.py": "main\n",
    "astralprims_ui.py": "ui\n",
    "protected_executor.py": "executor\n",
    "mcp_tools.py": "tools\n",
}
ARTIFACT_DIGEST = canonical_bundle_digest(
    BUNDLE_FILES,
    GENERATED_AGENT_BUNDLE_CONTRACT,
)
EMPTY_RESULT = GeneratedAgentPublicationResultMetadata()


def manifest() -> dict[str, object]:
    return {
        "agent_name": "Café Agent",
        "agent_id": "agent-publication",
        "bundle_sha256": ARTIFACT_DIGEST,
        "constitution_version": "0.1.0",
        "description": "Generated publication fixture",
        "digest_algorithm": "sha256",
        "required_runtime_lock_sha256": LOCK_DIGEST,
        "revision_id": REVISION_ID,
        "runtime_contract_version": 3,
        "files": [
            {
                "name": filename,
                "sha256": hashlib.sha256(BUNDLE_FILES[filename].encode("utf-8")).hexdigest(),
                "size_bytes": len(BUNDLE_FILES[filename].encode("utf-8")),
            }
            for filename in GENERATED_AGENT_BUNDLE_CONTRACT.file_names
        ],
        "manifest_version": 2,
    }


def bundle(**changes: object) -> FinalizedBundle:
    value = manifest()
    value.update(changes)
    manifest_json = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    return FinalizedBundle(
        contract=GENERATED_AGENT_BUNDLE_CONTRACT,
        files=BUNDLE_FILES,
        bundle_sha256=ARTIFACT_DIGEST,
        manifest=value,
        manifest_json=manifest_json,
    )


def draft(**changes: object) -> DraftAgentRecord:
    values: dict[str, object] = {
        "draft_id": "draft-publication",
        "owner_id": OWNER,
        "agent_name": "Publication Agent",
        "agent_slug": "publication-agent",
        "description": "test",
        "tools_spec": None,
        "skill_tags": None,
        "packages": None,
        "status": "generating",
        "generation_log": None,
        "security_report": None,
        "error_message": None,
        "port": None,
        "review_notes": None,
        "reviewed_by": None,
        "refinement_history": None,
        "validation_report": None,
        "required_credentials": None,
        "origin": "manual",
        "source_chat_id": None,
        "gap_fingerprint": None,
        "source_attachment_id": None,
        "revises_agent_id": None,
        "self_test": None,
        "phase": None,
        "clarify_answers": None,
        "plan_json": None,
        "analyze_result": None,
        "constitution_version": None,
        "host_binding": None,
        "draft_uuid": DRAFT_UUID,
        "target_agent_id": "agent-publication",
        "state_revision": 1,
        "generation_claim_id": CLAIM_ID,
        "generation_claim_expires_at": NOW + timedelta(minutes=5),
        "published_revision_id": None,
        "created_at": 1,
        "updated_at": 2,
    }
    values.update(changes)
    return DraftAgentRecord(**values)  # type: ignore[arg-type]


def publication(**changes: object) -> DraftPublicationRecord:
    values: dict[str, object] = {
        "publication_id": PUBLICATION_ID,
        "draft_uuid": DRAFT_UUID,
        "owner_id": OWNER,
        "source_state_revision": 1,
        "generation_claim_id": CLAIM_ID,
        "target_agent_id": "agent-publication",
        "target_revision_id": REVISION_ID,
        "operation_id": str(OPERATION_ID),
        "operation_execution_generation": 1,
        "staging_relative_path": STAGING_PATH,
        "revision_relative_path": REVISION_PATH,
        "artifact_digest": None,
        "manifest_digest": None,
        "state": "claimed",
        "state_revision": 0,
        "created_at": NOW,
        "published_at": None,
        "failed_at": None,
        "failure_code": None,
    }
    values.update(changes)
    return DraftPublicationRecord(**values)  # type: ignore[arg-type]


def publication_row(**changes: object) -> dict[str, object]:
    row = asdict(publication(**changes))
    row["owner_user_id"] = row.pop("owner_id")
    return row


def revision(**changes: object) -> AgentRevisionRecord:
    values: dict[str, object] = {
        "revision_id": REVISION_ID,
        "agent_id": "agent-publication",
        "owner_id": OWNER,
        "revision_number": 0,
        "parent_revision_id": None,
        "previous_good_revision_id": None,
        "artifact_digest": ARTIFACT_DIGEST,
        "manifest": manifest(),
        "artifact_relative_path": REVISION_PATH,
        "runtime_contract_version": 3,
        "release_lock_digest": LOCK_DIGEST,
        "compatibility_state": "compatible",
        "state": "prepared",
        "promotion_token": PROMOTION_TOKEN,
        "state_revision": 0,
        "created_at": NOW,
        "confirmed_at": None,
        "promoted_at": None,
        "failed_at": None,
        "failure_code": None,
    }
    values.update(changes)
    return AgentRevisionRecord(**values)  # type: ignore[arg-type]


def attempt(*, recovery: bool = False) -> ExecutionFence:
    return ExecutionFence(
        operation_id=RECOVERY_OPERATION_ID if recovery else OPERATION_ID,
        execution_generation=2 if recovery else 1,
        execution_lease_token=RECOVERY_LEASE_TOKEN if recovery else LEASE_TOKEN,
    )


def original_operation_binding():
    return generated_agent_publication_operation_binding(
        owner_id=OWNER,
        publication_id=PUBLICATION_ID,
        draft_uuid=DRAFT_UUID,
        source_state_revision=1,
        generation_claim_id=CLAIM_ID,
        target_agent_id="agent-publication",
        target_revision_id=REVISION_ID,
        bundle=bundle(),
        runtime_contract_version=3,
        release_lock_digest=LOCK_DIGEST,
        promotion_token=PROMOTION_TOKEN,
    )


def operation_record(
    fence: ExecutionFence,
    *,
    binding=None,
    state: OperationState = OperationState.RUNNING,
    owner_id: str = OWNER,
) -> OperationRecord:
    selected = binding or original_operation_binding()
    terminal = state in {
        OperationState.COMPLETED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.RETRYABLE,
    }
    return OperationRecord(
        operation_id=fence.operation_id,
        operation_kind=selected.operation_kind,
        admission_class=AdmissionClass.INTERACTIVE,
        owner_scope=OwnerScope.USER,
        owner_user_id=owner_id,
        connection_scope_id=None,
        idempotency_namespace=selected.idempotency_namespace,
        idempotency_key=selected.idempotency_key,
        normalized_input_digest=selected.normalized_input_digest,
        chat_id=None,
        parent_operation_id=selected.parent_operation_id,
        connection_generation=None,
        request_generation=None,
        state=state,
        phase_code=None,
        terminal_code=(
            "test_terminal" if terminal and state is not OperationState.COMPLETED else None
        ),
        safe_summary="Test terminal" if terminal else None,
        retry_after_ms=0 if state is OperationState.RETRYABLE else None,
        execution_generation=fence.execution_generation,
        execution_lease_token=None if terminal else fence.execution_lease_token,
        state_revision=1,
        accepted_at=NOW,
        updated_at=NOW,
        queue_deadline_at=None,
        started_at=NOW,
        terminal_at=NOW if terminal else None,
        cancel_requested_at=None,
        purge_after=NOW + timedelta(days=1) if terminal else None,
    )


class AgentStub:
    def __init__(self, *, current_revision: AgentRevisionRecord | None = None) -> None:
        self.current_revision = current_revision or revision()
        self.created_revision: dict[str, Any] | None = None
        self.locked_owners: list[str] = []

    def lock_owner(self, _transaction: object, *, owner_id: str) -> None:
        self.locked_owners.append(owner_id)

    def get_agent(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(active_revision_id=None, deleted_at=None)

    def list_revisions(self, *_args: object, **_kwargs: object) -> tuple[object, ...]:
        return ()

    def create_revision(self, _transaction: object, **kwargs: Any) -> AgentRevisionRecord:
        self.created_revision = kwargs
        self.current_revision = revision(
            revision_number=kwargs["revision_number"],
            parent_revision_id=kwargs["parent_revision_id"],
            previous_good_revision_id=kwargs["previous_good_revision_id"],
            artifact_digest=kwargs["artifact_digest"],
            manifest=kwargs["manifest"],
            artifact_relative_path=kwargs["artifact_relative_path"],
            runtime_contract_version=kwargs["runtime_contract_version"],
            release_lock_digest=kwargs["release_lock_digest"],
            compatibility_state=kwargs["compatibility_state"],
            state=kwargs["state"],
            promotion_token=kwargs["promotion_token"],
        )
        return self.current_revision

    def get_revision(self, *_args: object, **_kwargs: object) -> AgentRevisionRecord:
        return self.current_revision


class DraftStub:
    def __init__(
        self,
        *,
        current_draft: DraftAgentRecord | None = None,
        current_publication: DraftPublicationRecord | None = None,
    ) -> None:
        self.current_draft = current_draft or draft()
        self.current_publication = current_publication
        self.created_publication: dict[str, Any] | None = None
        self.transitioned: dict[str, Any] | None = None
        self.renewed_claim: dict[str, Any] | None = None
        self.reconcilable_inventory_kwargs: dict[str, object] | None = None

    def get_draft_by_uuid(self, *_args: object, **_kwargs: object) -> DraftAgentRecord:
        return self.current_draft

    def get_publication_by_source(
        self, *_args: object, **_kwargs: object
    ) -> DraftPublicationRecord | None:
        return self.current_publication

    def get_publication(self, *_args: object, **_kwargs: object) -> DraftPublicationRecord | None:
        return self.current_publication

    def _create_publication(self, _transaction: object, **kwargs: Any) -> DraftPublicationRecord:
        kwargs.pop("_capability")
        self.created_publication = kwargs
        self.current_publication = publication(
            publication_id=kwargs["publication_id"],
            draft_uuid=kwargs["draft_uuid"],
            owner_id=kwargs["owner_id"],
            source_state_revision=kwargs["source_state_revision"],
            generation_claim_id=kwargs["generation_claim_id"],
            target_agent_id=kwargs["target_agent_id"],
            target_revision_id=kwargs["target_revision_id"],
            operation_id=kwargs["operation_id"],
            operation_execution_generation=kwargs["operation_execution_generation"],
            staging_relative_path=kwargs["staging_relative_path"],
            revision_relative_path=kwargs["revision_relative_path"],
        )
        return self.current_publication

    def _transition_publication(
        self, _transaction: object, **kwargs: Any
    ) -> DraftPublicationRecord:
        kwargs.pop("_capability")
        self.transitioned = kwargs
        assert self.current_publication is not None
        self.current_publication = replace(
            self.current_publication,
            state_revision=self.current_publication.state_revision + 1,
            **kwargs["updates"],
        )
        return self.current_publication

    def renew_generation_claim(self, _transaction: object, **kwargs: Any) -> DraftAgentRecord:
        self.renewed_claim = kwargs
        return self.current_draft

    def get_publication_by_target_revision(
        self, *_args: object, **_kwargs: object
    ) -> DraftPublicationRecord | None:
        return self.current_publication

    def list_reconcilable_publications_for_administration(
        self, *_args: object, **kwargs: object
    ) -> tuple[DraftPublicationRecord, ...]:
        self.reconcilable_inventory_kwargs = kwargs
        return () if self.current_publication is None else (self.current_publication,)


class WorkStub:
    def __init__(
        self,
        owner_id: str = OWNER,
        *,
        current_operation: OperationRecord | None = None,
        prior_operation: OperationRecord | None = None,
    ) -> None:
        self.owner_id = owner_id
        self.current_operation = current_operation or operation_record(attempt(), owner_id=owner_id)
        self.prior_operation = prior_operation
        self.observed: list[ExecutionFence] = []
        self.prior_operation_lookups: list[uuid.UUID] = []

    def assert_current_execution(
        self, _transaction: object, fence: ExecutionFence
    ) -> OperationRecord:
        self.observed.append(fence)
        return self.current_operation

    def get_operation_for_administration(
        self,
        _transaction: object,
        *,
        operation_id: uuid.UUID,
        for_update: bool = False,
    ) -> OperationRecord | None:
        assert for_update
        self.prior_operation_lookups.append(operation_id)
        if self.prior_operation is not None and self.prior_operation.operation_id == operation_id:
            return self.prior_operation
        if self.current_operation.operation_id == operation_id:
            return self.current_operation
        return None


def repository(
    *,
    agents: AgentStub | None = None,
    drafts: DraftStub | None = None,
    work: WorkStub | None = None,
) -> tuple[GeneratedAgentPublicationRepository, AgentStub, DraftStub, WorkStub]:
    agent_store = agents or AgentStub()
    draft_store = drafts or DraftStub()
    work_store = work or WorkStub()
    result = GeneratedAgentPublicationRepository(
        agents=agent_store,  # type: ignore[arg-type]
        drafts=draft_store,  # type: ignore[arg-type]
        work_admission=work_store,  # type: ignore[arg-type]
    )
    return result, agent_store, draft_store, work_store


def begin(repository: GeneratedAgentPublicationRepository, transaction: object) -> object:
    return repository.begin_intent(transaction, **begin_kwargs())  # type: ignore[arg-type]


def begin_kwargs() -> dict[str, Any]:
    return {
        "owner_id": OWNER,
        "publication_id": PUBLICATION_ID,
        "draft_uuid": DRAFT_UUID,
        "source_state_revision": 1,
        "generation_claim_id": CLAIM_ID,
        "target_agent_id": "agent-publication",
        "target_revision_id": REVISION_ID,
        "staging_relative_path": STAGING_PATH,
        "revision_relative_path": REVISION_PATH,
        "bundle": bundle(),
        "runtime_contract_version": 3,
        "release_lock_digest": LOCK_DIGEST,
        "promotion_token": PROMOTION_TOKEN,
        "attempt": attempt(),
    }


def test_manifest_digest_reconstructs_canonical_json_with_one_lf() -> None:
    expected = hashlib.sha256(
        (
            json.dumps(manifest(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    ).hexdigest()
    assert canonical_generated_agent_manifest_digest(manifest()) == expected
    with pytest.raises(RepositoryValidationError, match="JSON object"):
        canonical_generated_agent_manifest_digest([])  # type: ignore[arg-type]


def test_begin_intent_atomically_prepares_non_routable_revision_and_journal() -> None:
    subject, agents, drafts, work = repository()
    transaction = ScriptedTransaction(one=[{"claim_is_live": 1}])

    result = begin(subject, transaction)

    assert not result.replayed  # type: ignore[attr-defined]
    assert result.revision.state == "prepared"  # type: ignore[attr-defined]
    assert drafts.created_publication is not None
    assert drafts.created_publication["operation_id"] == str(OPERATION_ID)
    assert drafts.created_publication["operation_execution_generation"] == 1
    assert agents.created_revision is not None
    assert agents.created_revision["artifact_relative_path"] == REVISION_PATH
    assert agents.created_revision["state"] == "prepared"
    assert work.observed == [attempt()]
    assert "active_revision_id" not in transaction.fetch_sql()


def test_begin_intent_replays_existing_source_without_allocating_new_identity() -> None:
    existing = publication(state="staged", state_revision=1)
    subject, agents, drafts, _work = repository(drafts=DraftStub(current_publication=existing))

    result = begin(subject, ScriptedTransaction(one=[{"claim_is_live": 1}]))

    assert result.replayed  # type: ignore[attr-defined]
    assert result.publication == existing  # type: ignore[attr-defined]
    assert agents.created_revision is None
    assert drafts.created_publication is None


def test_nonterminal_begin_replay_rechecks_claim_attempt_and_prepared_compatibility() -> None:
    existing = publication(state="staged", state_revision=1)
    subject, _agents, _drafts, _work = repository(drafts=DraftStub(current_publication=existing))
    expired = ScriptedTransaction(one=[None])
    with pytest.raises(RepositoryConflictError, match="generation claim"):
        begin(subject, expired)
    assert "clock_timestamp()" in expired.fetch_sql()

    subject, _agents, _drafts, _work = repository(drafts=DraftStub(current_publication=existing))
    with pytest.raises(RepositoryConflictError, match="operation attempt"):
        subject.begin_intent(
            ScriptedTransaction(),
            **{**begin_kwargs(), "attempt": attempt(recovery=True)},
        )

    for changed_revision in (
        revision(state="active"),
        revision(compatibility_state="incompatible"),
    ):
        subject, _agents, _drafts, _work = repository(
            agents=AgentStub(current_revision=changed_revision),
            drafts=DraftStub(current_publication=existing),
        )
        with pytest.raises(RepositoryConflictError, match="revision replay"):
            begin(subject, ScriptedTransaction())


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("staging_relative_path", f".staging/{DRAFT_UUID}/1/{PUBLICATION_ID}"),
        ("staging_relative_path", f"staging//{DRAFT_UUID}/1/{PUBLICATION_ID}"),
        ("staging_relative_path", f"staging/{DRAFT_UUID}/./1/{PUBLICATION_ID}"),
        ("staging_relative_path", rf"C:\staging\{DRAFT_UUID}\1\{PUBLICATION_ID}"),
        ("staging_relative_path", f"staging/{DRAFT_UUID}/2/{PUBLICATION_ID}"),
        ("revision_relative_path", f"revisions/other/{REVISION_ID}"),
        ("revision_relative_path", f"revisions/agent-publication//{REVISION_ID}"),
        ("revision_relative_path", rf"revisions\agent-publication\{REVISION_ID}"),
    ),
)
def test_begin_intent_rejects_every_noncanonical_or_aliased_path(
    field: str,
    value: str,
) -> None:
    subject, _agents, _drafts, _work = repository()
    kwargs = begin_kwargs()
    kwargs[field] = value
    with pytest.raises(RepositoryValidationError, match="canonical"):
        subject.begin_intent(ScriptedTransaction(), **kwargs)


def test_publication_path_helper_derives_the_only_storage_layout() -> None:
    paths = generated_agent_publication_paths(
        draft_uuid=DRAFT_UUID,
        source_state_revision=1,
        publication_id=PUBLICATION_ID,
        target_agent_id="agent-publication",
        target_revision_id=REVISION_ID,
    )
    assert paths.staging_relative_path == STAGING_PATH
    assert paths.revision_relative_path == REVISION_PATH
    with pytest.raises(RepositoryValidationError, match="path identity"):
        generated_agent_publication_paths(
            draft_uuid=DRAFT_UUID,
            source_state_revision=1,
            publication_id=PUBLICATION_ID,
            target_agent_id="agent/publication",
            target_revision_id=REVISION_ID,
        )


def test_begin_intent_rejects_changed_source_identity_and_manifest_fences() -> None:
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=publication(publication_id=str(uuid.uuid4())))
    )
    with pytest.raises(RepositoryConflictError, match="source replay"):
        begin(subject, ScriptedTransaction())

    subject, _agents, _drafts, _work = repository()
    with pytest.raises(RepositoryValidationError, match="agent_id"):
        subject.begin_intent(
            ScriptedTransaction(),
            owner_id=OWNER,
            publication_id=PUBLICATION_ID,
            draft_uuid=DRAFT_UUID,
            source_state_revision=1,
            generation_claim_id=CLAIM_ID,
            target_agent_id="agent-publication",
            target_revision_id=REVISION_ID,
            staging_relative_path=STAGING_PATH,
            revision_relative_path=REVISION_PATH,
            bundle=bundle(agent_id="other-agent"),
            runtime_contract_version=3,
            release_lock_digest=LOCK_DIGEST,
            promotion_token=PROMOTION_TOKEN,
            attempt=attempt(),
        )


def test_terminal_begin_replay_authenticates_the_stored_attempt_without_live_execution() -> None:
    manifest_digest = canonical_generated_agent_manifest_digest(manifest())
    published = publication(
        state="published",
        state_revision=3,
        artifact_digest=ARTIFACT_DIGEST,
        manifest_digest=manifest_digest,
        published_at=NOW,
    )
    terminal_draft = draft(
        status="generated",
        generation_claim_id=None,
        generation_claim_expires_at=None,
        published_revision_id=REVISION_ID,
        state_revision=2,
    )
    unrelated = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())
    work = WorkStub(
        current_operation=operation_record(unrelated),
        prior_operation=operation_record(
            attempt(),
            state=OperationState.COMPLETED,
        ),
    )
    subject, _agents, _drafts, _work = repository(
        agents=AgentStub(current_revision=revision(state="failed")),
        drafts=DraftStub(
            current_draft=terminal_draft,
            current_publication=published,
        ),
        work=work,
    )

    result = begin(subject, ScriptedTransaction())

    assert result.replayed  # type: ignore[attr-defined]
    assert result.publication == published  # type: ignore[attr-defined]
    assert work.observed == []
    with pytest.raises(RepositoryConflictError, match="operation attempt"):
        subject.begin_intent(
            ScriptedTransaction(),
            **{**begin_kwargs(), "attempt": unrelated},
        )

    failed = replace(
        published,
        state="failed",
        published_at=None,
        failed_at=NOW,
        failure_code="validation_failed",
    )
    failed_draft = replace(
        terminal_draft,
        status="error",
        published_revision_id=None,
    )
    subject, _agents, _drafts, _work = repository(
        agents=AgentStub(current_revision=revision(state="active")),
        drafts=DraftStub(
            current_draft=failed_draft,
            current_publication=failed,
        ),
        work=work,
    )
    with pytest.raises(RepositoryConflictError, match="target revision fence"):
        begin(subject, ScriptedTransaction())


def test_publication_boundary_rejects_a_valid_bundle_from_another_contract() -> None:
    other_contract = ImmutableBundleContract(
        file_names=GENERATED_AGENT_BUNDLE_CONTRACT.file_names,
        scope_identity_field="agent_id",
    )
    value = manifest()
    for field in ("agent_name", "description", "constitution_version"):
        value.pop(field)
    manifest_json = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    other_bundle = FinalizedBundle(
        contract=other_contract,
        files=BUNDLE_FILES,
        bundle_sha256=ARTIFACT_DIGEST,
        manifest=value,
        manifest_json=manifest_json,
    )
    subject, _agents, _drafts, _work = repository()
    with pytest.raises(RepositoryValidationError, match="generated-agent contract"):
        subject.begin_intent(
            ScriptedTransaction(),
            **{**begin_kwargs(), "bundle": other_bundle},
        )


def test_state_specific_staged_and_validated_transitions_enforce_digests() -> None:
    claimed = publication()
    subject, _agents, drafts, _work = repository(drafts=DraftStub(current_publication=claimed))
    staged = subject.mark_staged(
        ScriptedTransaction(one=[{"claim_is_live": 1}]),
        expected=claimed,
        attempt=attempt(),
    )
    assert staged.state == "staged"
    assert staged.state_revision == 1

    expected_manifest = canonical_generated_agent_manifest_digest(manifest())
    validation_transaction = ScriptedTransaction(
        one=[
            {"claim_is_live": 1},
            publication_row(
                state="validated",
                state_revision=2,
                artifact_digest=ARTIFACT_DIGEST,
                manifest_digest=expected_manifest,
            ),
        ]
    )
    validated = subject.mark_validated(
        validation_transaction,
        expected=staged,
        attempt=attempt(),
        artifact_digest=ARTIFACT_DIGEST,
        manifest_digest=expected_manifest,
        generation_result=EMPTY_RESULT,
    )
    assert validated.state == "validated"
    assert validated.artifact_digest == ARTIFACT_DIGEST
    assert validated.manifest_digest == expected_manifest
    assert drafts.transitioned is not None
    validation_sql = validation_transaction.fetch_sql()
    assert "persisted_result AS" in validation_sql
    assert "security_report = %s" in validation_sql
    assert "state_revision = draft.state_revision + 1" not in validation_sql

    wrong_subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=staged)
    )
    with pytest.raises(RepositoryConflictError, match="manifest digest"):
        wrong_subject.mark_validated(
            ScriptedTransaction(one=[{"claim_is_live": 1}]),
            expected=staged,
            attempt=attempt(),
            artifact_digest=ARTIFACT_DIGEST,
            manifest_digest="c" * 64,
            generation_result=EMPTY_RESULT,
        )


def test_validation_durably_anchors_results_for_crash_recovery_commit() -> None:
    staged = publication(state="staged", state_revision=1)
    manifest_digest = canonical_generated_agent_manifest_digest(manifest())
    result_metadata = GeneratedAgentPublicationResultMetadata(
        error_message="Validation found one non-blocking issue.",
        security_report='{"findings":[]}',
        validation_report='{"passed":true}',
        required_credentials='["api_key"]',
    )
    subject, _agents, _drafts, _work = repository(drafts=DraftStub(current_publication=staged))
    validation_transaction = ScriptedTransaction(
        one=[
            {"claim_is_live": 1},
            publication_row(
                state="validated",
                state_revision=2,
                artifact_digest=ARTIFACT_DIGEST,
                manifest_digest=manifest_digest,
            ),
        ]
    )
    validated = subject.mark_validated(
        validation_transaction,
        expected=staged,
        attempt=attempt(),
        artifact_digest=ARTIFACT_DIGEST,
        manifest_digest=manifest_digest,
        generation_result=result_metadata,
    )
    assert all(
        value in validation_transaction.calls[-1][2]
        for value in (
            result_metadata.error_message,
            result_metadata.security_report,
            result_metadata.validation_report,
            result_metadata.required_credentials,
        )
    )

    recovered_draft = draft(
        error_message=result_metadata.error_message,
        security_report=result_metadata.security_report,
        validation_report=result_metadata.validation_report,
        required_credentials=result_metadata.required_credentials,
    )
    recovered, _agents, _drafts, _work = repository(
        drafts=DraftStub(
            current_draft=recovered_draft,
            current_publication=validated,
        )
    )
    committed = recovered.commit_published(
        ScriptedTransaction(
            one=[
                {"claim_is_live": 1},
                publication_row(
                    state="published",
                    state_revision=3,
                    artifact_digest=ARTIFACT_DIGEST,
                    manifest_digest=manifest_digest,
                    published_at=NOW,
                ),
            ]
        ),
        expected=validated,
        attempt=attempt(),
    )
    assert committed.state == "published"

    replay_subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(
            current_draft=recovered_draft,
            current_publication=validated,
        )
    )
    with pytest.raises(RepositoryConflictError, match="result metadata"):
        replay_subject.mark_validated(
            ScriptedTransaction(one=[{"claim_is_live": 1}]),
            expected=staged,
            attempt=attempt(),
            artifact_digest=ARTIFACT_DIGEST,
            manifest_digest=manifest_digest,
            generation_result=replace(
                result_metadata,
                validation_report='{"passed":false}',
            ),
        )


def test_recovery_rebind_renews_exact_claim_and_cas_changes_only_attempt() -> None:
    expected = publication()
    drafts = DraftStub(current_publication=expected)
    recovery_binding = generated_agent_publication_recovery_operation_binding(expected, revision())
    work = WorkStub(
        current_operation=operation_record(attempt(recovery=True), binding=recovery_binding),
        prior_operation=operation_record(attempt(), state=OperationState.FAILED),
    )
    subject, _agents, _drafts, _work = repository(drafts=drafts, work=work)
    rebound_row = publication_row(
        operation_id=str(RECOVERY_OPERATION_ID),
        operation_execution_generation=2,
        state_revision=1,
    )
    transaction = ScriptedTransaction(one=[rebound_row])

    rebound = subject.rebind_recovery_attempt(
        transaction,
        expected=expected,
        new_attempt=attempt(recovery=True),
        lease_seconds=120,
    )

    assert rebound.operation_id == str(RECOVERY_OPERATION_ID)
    assert rebound.state == "claimed"
    assert rebound.source_state_revision == expected.source_state_revision
    sql = transaction.fetch_sql()
    assert "generation_claim_expires_at" in sql
    assert "IS NOT DISTINCT FROM" in sql
    assert "state_revision = publication.state_revision + 1" in sql

    with pytest.raises(RepositoryConflictError, match="recovery fence"):
        subject.rebind_recovery_attempt(
            ScriptedTransaction(one=[None]),
            expected=expected,
            new_attempt=attempt(recovery=True),
        )


def test_rebind_refuses_to_steal_a_distinct_live_operation() -> None:
    expected = publication()
    recovery_binding = generated_agent_publication_recovery_operation_binding(expected, revision())
    work = WorkStub(
        current_operation=operation_record(attempt(recovery=True), binding=recovery_binding),
        prior_operation=operation_record(attempt()),
    )
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=expected),
        work=work,
    )

    with pytest.raises(RepositoryConflictError, match="still live"):
        subject.rebind_recovery_attempt(
            ScriptedTransaction(),
            expected=expected,
            new_attempt=attempt(recovery=True),
        )
    assert work.prior_operation_lookups == [OPERATION_ID]


@pytest.mark.parametrize(
    "mutation",
    (
        {"parent_operation_id": None},
        {"operation_kind": "generated_agent_publication"},
        {"idempotency_namespace": "generated-agent-publication"},
        {"idempotency_key": "unrelated"},
        {"normalized_input_digest": "c" * 64},
    ),
)
def test_rebind_requires_the_exact_designated_child_operation_lineage(
    mutation: dict[str, object],
) -> None:
    expected = publication()
    required = generated_agent_publication_recovery_operation_binding(expected, revision())
    child = replace(
        operation_record(attempt(recovery=True), binding=required),
        **mutation,
    )
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=expected),
        work=WorkStub(
            current_operation=child,
            prior_operation=operation_record(attempt(), state=OperationState.FAILED),
        ),
    )

    with pytest.raises(RepositoryConflictError, match=r"recovery|operation"):
        subject.rebind_recovery_attempt(
            ScriptedTransaction(),
            expected=expected,
            new_attempt=attempt(recovery=True),
        )


def test_rebind_replay_reauthenticates_the_bound_child_identity() -> None:
    expected = publication()
    rebound = replace(
        expected,
        operation_id=str(RECOVERY_OPERATION_ID),
        operation_execution_generation=2,
        state_revision=1,
    )
    required = generated_agent_publication_recovery_operation_binding(expected, revision())
    authentic_child = operation_record(attempt(recovery=True), binding=required)
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=rebound),
        work=WorkStub(current_operation=authentic_child),
    )
    assert (
        subject.rebind_recovery_attempt(
            ScriptedTransaction(),
            expected=expected,
            new_attempt=attempt(recovery=True),
        )
        == rebound
    )

    unrelated_child = replace(authentic_child, parent_operation_id=None)
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=rebound),
        work=WorkStub(current_operation=unrelated_child),
    )
    with pytest.raises(RepositoryConflictError, match="parent"):
        subject.rebind_recovery_attempt(
            ScriptedTransaction(),
            expected=expected,
            new_attempt=attempt(recovery=True),
        )


def test_rebind_accepts_a_higher_reselected_generation_of_the_same_operation() -> None:
    expected = publication()
    reselected = ExecutionFence(
        operation_id=OPERATION_ID,
        execution_generation=2,
        execution_lease_token=RECOVERY_LEASE_TOKEN,
    )
    work = WorkStub(current_operation=operation_record(reselected))
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=expected),
        work=work,
    )
    rebound_row = publication_row(
        operation_execution_generation=2,
        state_revision=1,
    )

    rebound = subject.rebind_recovery_attempt(
        ScriptedTransaction(one=[rebound_row]),
        expected=expected,
        new_attempt=reselected,
    )

    assert rebound.operation_execution_generation == 2
    assert work.prior_operation_lookups == []


def test_assert_current_attempt_is_a_read_only_pre_move_fence() -> None:
    expected = publication(state="staged", state_revision=1)
    subject, _agents, _drafts, work = repository(drafts=DraftStub(current_publication=expected))
    transaction = ScriptedTransaction(one=[{"claim_is_live": 1}])

    assert (
        subject.assert_current_attempt(
            transaction,
            expected=expected,
            attempt=attempt(),
        )
        == expected
    )
    assert work.observed == [attempt()]
    assert all(call[0] != "execute" for call in transaction.calls)

    expired = ScriptedTransaction(one=[None])
    with pytest.raises(RepositoryConflictError, match="generation claim"):
        subject.assert_current_attempt(
            expired,
            expected=expected,
            attempt=attempt(),
        )
    assert "clock_timestamp()" in expired.fetch_sql()


def test_journal_claim_renewal_binds_snapshot_attempt_and_exact_draft_claim() -> None:
    expected = publication(state="staged", state_revision=1)
    drafts = DraftStub(current_publication=expected)
    subject, _agents, _drafts, _work = repository(drafts=drafts)

    renewed = subject.renew_generation_claim(
        ScriptedTransaction(one=[{"claim_is_live": 1}]),
        expected=expected,
        attempt=attempt(),
        lease_seconds=240,
    )

    assert renewed == draft()
    assert drafts.renewed_claim == {
        "owner_id": OWNER,
        "draft_id": "draft-publication",
        "expected_revision": 1,
        "claim_id": CLAIM_ID,
        "lease_seconds": 240,
    }

    with pytest.raises(RepositoryConflictError, match="operation attempt"):
        subject.renew_generation_claim(
            ScriptedTransaction(one=[{"claim_is_live": 1}]),
            expected=expected,
            attempt=attempt(recovery=True),
        )


def test_commit_published_is_one_db_statement_and_does_not_activate_revision() -> None:
    manifest_digest = canonical_generated_agent_manifest_digest(manifest())
    validated = publication(
        state="validated",
        state_revision=2,
        artifact_digest=ARTIFACT_DIGEST,
        manifest_digest=manifest_digest,
    )
    result_metadata = GeneratedAgentPublicationResultMetadata(
        error_message="Validation found one non-blocking issue.",
        security_report='{"findings":[]}',
        validation_report='{"passed":true}',
        required_credentials='["api_key"]',
    )
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(
            current_draft=draft(
                error_message=result_metadata.error_message,
                security_report=result_metadata.security_report,
                validation_report=result_metadata.validation_report,
                required_credentials=result_metadata.required_credentials,
            ),
            current_publication=validated,
        )
    )
    transaction = ScriptedTransaction(
        one=[
            {"claim_is_live": 1},
            publication_row(
                state="published",
                state_revision=3,
                artifact_digest=ARTIFACT_DIGEST,
                manifest_digest=manifest_digest,
                published_at=NOW,
            ),
        ]
    )

    committed = subject.commit_published(
        transaction,
        expected=validated,
        attempt=attempt(),
        generation_result=result_metadata,
    )

    assert committed.state == "published"
    sql = transaction.fetch_sql()
    assert "published_revision_id = %s" in sql
    assert "status = 'generated'" in sql
    assert "generation_claim_id = NULL" in sql
    assert "security_report IS NOT DISTINCT FROM %s" in sql
    assert "validation_report IS NOT DISTINCT FROM %s" in sql
    assert "required_credentials IS NOT DISTINCT FROM %s" in sql
    assert result_metadata.security_report in transaction.calls[-1][2]
    assert "active_revision_id" not in sql


def test_terminal_publish_replay_authenticates_stored_attempt_and_exact_metadata() -> None:
    manifest_digest = canonical_generated_agent_manifest_digest(manifest())
    validated = publication(
        state="validated",
        state_revision=2,
        artifact_digest=ARTIFACT_DIGEST,
        manifest_digest=manifest_digest,
    )
    published = replace(
        validated,
        state="published",
        state_revision=3,
        published_at=NOW,
    )
    result_metadata = GeneratedAgentPublicationResultMetadata(
        security_report='{"findings":[]}',
        validation_report='{"passed":true}',
    )
    terminal_draft = draft(
        status="generated",
        security_report=result_metadata.security_report,
        validation_report=result_metadata.validation_report,
        generation_claim_id=None,
        generation_claim_expires_at=None,
        published_revision_id=REVISION_ID,
        state_revision=2,
    )
    unrelated_fence = ExecutionFence(
        operation_id=uuid.uuid4(),
        execution_generation=1,
        execution_lease_token=uuid.uuid4(),
    )
    work = WorkStub(
        current_operation=operation_record(unrelated_fence),
        prior_operation=operation_record(attempt(), state=OperationState.COMPLETED),
    )
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(
            current_draft=terminal_draft,
            current_publication=published,
        ),
        work=work,
    )

    assert (
        subject.commit_published(
            ScriptedTransaction(),
            expected=validated,
            attempt=attempt(),
            generation_result=result_metadata,
        )
        == published
    )
    assert work.observed == []

    with pytest.raises(RepositoryConflictError, match="operation attempt"):
        subject.commit_published(
            ScriptedTransaction(),
            expected=validated,
            attempt=unrelated_fence,
            generation_result=result_metadata,
        )
    with pytest.raises(RepositoryConflictError, match="result metadata"):
        subject.commit_published(
            ScriptedTransaction(),
            expected=validated,
            attempt=attempt(),
            generation_result=replace(
                result_metadata,
                validation_report='{"passed":false}',
            ),
        )


def test_fail_atomically_terminalizes_journal_draft_and_prepared_revision() -> None:
    expected = publication(state="staged", state_revision=1)
    subject, _agents, _drafts, _work = repository(drafts=DraftStub(current_publication=expected))
    transaction = ScriptedTransaction(
        one=[
            {"claim_is_live": 1},
            publication_row(
                state="failed",
                state_revision=2,
                failed_at=NOW,
                failure_code="artifact_invalid",
            ),
        ]
    )

    failed = subject.fail(
        transaction,
        expected=expected,
        attempt=attempt(),
        failure_code="artifact_invalid",
        safe_error_message="Generated artifact failed validation.",
    )

    assert failed.state == "failed"
    sql = transaction.fetch_sql()
    assert "status = 'error'" in sql
    assert "UPDATE user_agent_revision" in sql
    assert "revision.state = 'prepared'" in sql
    assert "Generated artifact failed validation." in transaction.calls[-1][2]


def test_terminal_failure_replay_preserves_safe_message_and_authenticates_attempt() -> None:
    expected = publication(state="staged", state_revision=1)
    failed = replace(
        expected,
        state="failed",
        state_revision=2,
        failed_at=NOW,
        failure_code="artifact_invalid",
    )
    failed_draft = draft(
        status="error",
        error_message="Generated artifact failed validation.",
        generation_claim_id=None,
        generation_claim_expires_at=None,
        state_revision=2,
    )
    work = WorkStub(prior_operation=operation_record(attempt(), state=OperationState.FAILED))
    subject, _agents, _drafts, _work = repository(
        agents=AgentStub(current_revision=revision(state="failed")),
        drafts=DraftStub(
            current_draft=failed_draft,
            current_publication=failed,
        ),
        work=work,
    )
    assert (
        subject.fail(
            ScriptedTransaction(),
            expected=expected,
            attempt=attempt(),
            failure_code="artifact_invalid",
            safe_error_message="Generated artifact failed validation.",
        )
        == failed
    )
    assert work.observed == []

    with pytest.raises(RepositoryConflictError, match="safe error"):
        subject.fail(
            ScriptedTransaction(),
            expected=expected,
            attempt=attempt(),
            failure_code="artifact_invalid",
            safe_error_message="Different message.",
        )


def test_owner_claim_operation_and_snapshot_fences_fail_closed() -> None:
    subject, _agents, _drafts, _work = repository(work=WorkStub("other-owner"))
    with pytest.raises(RepositoryConflictError, match="operation owner"):
        begin(subject, ScriptedTransaction())

    expected = publication()
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(
            current_draft=draft(generation_claim_id=str(uuid.uuid4())),
            current_publication=expected,
        )
    )
    with pytest.raises(RepositoryConflictError, match="draft/claim/source"):
        subject.mark_staged(ScriptedTransaction(), expected=expected, attempt=attempt())

    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=replace(expected, state_revision=1))
    )
    with pytest.raises(RepositoryConflictError, match="state revision"):
        subject.mark_staged(
            ScriptedTransaction(one=[{"claim_is_live": 1}]),
            expected=expected,
            attempt=attempt(),
        )


def test_public_lookup_and_bounded_inventory_delegate_without_mutation() -> None:
    current = publication()
    draft_store = DraftStub(current_publication=current)
    subject, _agents, _drafts, _work = repository(drafts=draft_store)
    transaction = ScriptedTransaction()
    assert (
        subject.get_by_source(
            transaction,
            owner_id=OWNER,
            draft_uuid=DRAFT_UUID,
            source_state_revision=1,
        )
        == current
    )
    assert (
        subject.get_by_target_revision(
            transaction,
            owner_id=OWNER,
            target_agent_id="agent-publication",
            target_revision_id=REVISION_ID,
        )
        == current
    )
    assert subject.list_reconcilable_for_administration(
        transaction,
        limit=1,
        after_created_at=NOW,
        after_publication_id=PUBLICATION_ID,
    ) == (current,)
    assert draft_store.reconcilable_inventory_kwargs == {
        "limit": 1,
        "after_created_at": NOW,
        "after_publication_id": PUBLICATION_ID,
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"publication_id": "not-a-uuid"}, "publication_id must be a UUID"),
        ({"bundle": object()}, "FinalizedBundle"),
        ({"staging_relative_path": "../escape"}, "canonical POSIX-relative"),
        ({"compatibility_state": "legacy_pending"}, "compatibility_state"),
    ),
)
def test_begin_intent_rejects_invalid_identity_inputs(
    changes: dict[str, object], message: str
) -> None:
    subject, _agents, _drafts, _work = repository()
    kwargs = begin_kwargs()
    kwargs.update(changes)
    with pytest.raises(RepositoryValidationError, match=message):
        subject.begin_intent(ScriptedTransaction(), **kwargs)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("agent_id", "other", "agent_id"),
        ("revision_id", str(uuid.uuid4()), "revision_id"),
        ("runtime_contract_version", 4, "runtime contract"),
        ("required_runtime_lock_sha256", "c" * 64, "runtime lock"),
    ),
)
def test_begin_intent_rejects_manifest_identity_changes(
    field: str, value: object, message: str
) -> None:
    subject, _agents, _drafts, _work = repository()
    kwargs = begin_kwargs()
    kwargs["bundle"] = bundle(**{field: value})
    with pytest.raises(RepositoryValidationError, match=message):
        subject.begin_intent(ScriptedTransaction(), **kwargs)


def test_manifest_size_and_prerequisite_rows_fail_closed() -> None:
    oversized = manifest()
    oversized["padding"] = "x" * 65_536
    with pytest.raises(RepositoryValidationError, match="64 KiB"):
        canonical_generated_agent_manifest_digest(oversized)

    class MissingDraft(DraftStub):
        def get_draft_by_uuid(self, *_args: object, **_kwargs: object) -> None:
            return None

    subject, _agents, _drafts, _work = repository(drafts=MissingDraft())
    with pytest.raises(RepositoryNotFoundError, match="draft"):
        begin(subject, ScriptedTransaction())

    class MissingAgent(AgentStub):
        def get_agent(self, *_args: object, **_kwargs: object) -> None:
            return None

    subject, _agents, _drafts, _work = repository(agents=MissingAgent())
    with pytest.raises(RepositoryNotFoundError, match="target agent"):
        begin(subject, ScriptedTransaction(one=[{"claim_is_live": 1}]))

    class MissingRevision(AgentStub):
        def get_revision(self, *_args: object, **_kwargs: object) -> None:
            return None

    subject, _agents, _drafts, _work = repository(
        agents=MissingRevision(),
        drafts=DraftStub(current_publication=publication()),
    )
    with pytest.raises(RepositoryDataError, match="target revision"):
        begin(subject, ScriptedTransaction())


def test_generation_result_metadata_is_typed_and_bounded() -> None:
    with pytest.raises(RepositoryValidationError, match="security_report"):
        GeneratedAgentPublicationResultMetadata(
            security_report=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(RepositoryValidationError, match="maximum"):
        GeneratedAgentPublicationResultMetadata(
            validation_report="x" * 1_048_577,
        )
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=publication(state="staged", state_revision=1))
    )
    with pytest.raises(RepositoryValidationError, match="generation_result"):
        subject.mark_validated(
            ScriptedTransaction(),
            expected=publication(state="staged", state_revision=1),
            attempt=attempt(),
            artifact_digest=ARTIFACT_DIGEST,
            manifest_digest=canonical_generated_agent_manifest_digest(manifest()),
            generation_result=None,  # type: ignore[arg-type]
        )


def test_replay_paths_are_idempotent_under_the_same_durable_identity() -> None:
    expected = publication()
    staged = replace(expected, state="staged", state_revision=1)
    subject, _agents, _drafts, _work = repository(drafts=DraftStub(current_publication=staged))
    assert (
        subject.mark_staged(
            ScriptedTransaction(one=[{"claim_is_live": 1}]),
            expected=expected,
            attempt=attempt(),
        )
        == staged
    )

    manifest_digest = canonical_generated_agent_manifest_digest(manifest())
    validated = replace(
        staged,
        state="validated",
        state_revision=2,
        artifact_digest=ARTIFACT_DIGEST,
        manifest_digest=manifest_digest,
    )
    subject, _agents, _drafts, _work = repository(drafts=DraftStub(current_publication=validated))
    assert (
        subject.mark_validated(
            ScriptedTransaction(one=[{"claim_is_live": 1}]),
            expected=staged,
            attempt=attempt(),
            artifact_digest=ARTIFACT_DIGEST,
            manifest_digest=manifest_digest,
            generation_result=EMPTY_RESULT,
        )
        == validated
    )

    rebound = replace(
        expected,
        operation_id=str(RECOVERY_OPERATION_ID),
        operation_execution_generation=2,
        state_revision=1,
    )
    recovery_binding = generated_agent_publication_recovery_operation_binding(expected, revision())
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=rebound),
        work=WorkStub(
            current_operation=operation_record(attempt(recovery=True), binding=recovery_binding)
        ),
    )
    assert (
        subject.rebind_recovery_attempt(
            ScriptedTransaction(),
            expected=expected,
            new_attempt=attempt(recovery=True),
        )
        == rebound
    )

    failed = replace(
        staged,
        state="failed",
        state_revision=2,
        failed_at=NOW,
        failure_code="artifact_invalid",
    )
    failed_draft = draft(
        status="error",
        error_message="Generated artifact failed validation.",
        generation_claim_id=None,
        generation_claim_expires_at=None,
        state_revision=2,
    )
    subject, _agents, _drafts, _work = repository(
        agents=AgentStub(current_revision=revision(state="failed")),
        drafts=DraftStub(current_draft=failed_draft, current_publication=failed),
    )
    assert (
        subject.fail(
            ScriptedTransaction(),
            expected=staged,
            attempt=attempt(),
            failure_code="artifact_invalid",
            safe_error_message="Generated artifact failed validation.",
        )
        == failed
    )

    published = replace(
        validated,
        state="published",
        state_revision=3,
        published_at=NOW,
    )
    published_draft = draft(
        status="generated",
        generation_claim_id=None,
        generation_claim_expires_at=None,
        published_revision_id=REVISION_ID,
        state_revision=2,
    )
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(
            current_draft=published_draft,
            current_publication=published,
        )
    )
    assert (
        subject.commit_published(ScriptedTransaction(), expected=validated, attempt=attempt())
        == published
    )


def test_state_and_retry_argument_validation_is_explicit() -> None:
    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=publication())
    )
    with pytest.raises(RepositoryValidationError, match="nonterminal"):
        subject.rebind_recovery_attempt(
            ScriptedTransaction(),
            expected=publication(state="published"),
            new_attempt=attempt(recovery=True),
        )
    with pytest.raises(RepositoryValidationError, match="1800"):
        subject.rebind_recovery_attempt(
            ScriptedTransaction(),
            expected=publication(),
            new_attempt=attempt(recovery=True),
            lease_seconds=1801,
        )
    with pytest.raises(RepositoryValidationError, match="staged publication"):
        subject.mark_validated(
            ScriptedTransaction(),
            expected=publication(),
            attempt=attempt(),
            artifact_digest=ARTIFACT_DIGEST,
            manifest_digest="c" * 64,
            generation_result=EMPTY_RESULT,
        )
    with pytest.raises(RepositoryValidationError, match="nonterminal"):
        subject.fail(
            ScriptedTransaction(),
            expected=publication(state="failed"),
            attempt=attempt(),
            failure_code="artifact_invalid",
            safe_error_message="Generated artifact failed validation.",
        )
    with pytest.raises(RepositoryValidationError, match="validated state"):
        subject.commit_published(
            ScriptedTransaction(), expected=publication(state="staged"), attempt=attempt()
        )
    with pytest.raises(RepositoryValidationError, match="digests"):
        subject.commit_published(
            ScriptedTransaction(),
            expected=publication(state="validated"),
            attempt=attempt(),
        )
    with pytest.raises(RepositoryValidationError, match="claimed state"):
        subject.mark_staged(
            ScriptedTransaction(),
            expected=publication(state="staged"),
            attempt=attempt(),
        )
    with pytest.raises(RepositoryValidationError, match="DraftPublicationRecord"):
        subject.mark_staged(
            ScriptedTransaction(),
            expected=object(),  # type: ignore[arg-type]
            attempt=attempt(),
        )
    with pytest.raises(RepositoryValidationError, match="ExecutionFence"):
        subject.mark_staged(
            ScriptedTransaction(),
            expected=publication(),
            attempt=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(RepositoryValidationError, match="failure_code"):
        subject.fail(
            ScriptedTransaction(),
            expected=publication(),
            attempt=attempt(),
            failure_code="INVALID-CODE",
            safe_error_message="Generated artifact failed validation.",
        )


def test_missing_context_live_claim_and_attempt_or_digest_mismatch_are_conflicts() -> None:
    subject, _agents, _drafts, _work = repository(drafts=DraftStub())
    with pytest.raises(RepositoryNotFoundError, match="publication"):
        subject.mark_staged(ScriptedTransaction(), expected=publication(), attempt=attempt())

    class MissingDraft(DraftStub):
        def get_draft_by_uuid(self, *_args: object, **_kwargs: object) -> None:
            return None

    subject, _agents, _drafts, _work = repository(
        drafts=MissingDraft(current_publication=publication())
    )
    with pytest.raises(RepositoryDataError, match="source draft"):
        subject.mark_staged(ScriptedTransaction(), expected=publication(), attempt=attempt())

    class MissingRevision(AgentStub):
        def get_revision(self, *_args: object, **_kwargs: object) -> None:
            return None

    subject, _agents, _drafts, _work = repository(
        agents=MissingRevision(),
        drafts=DraftStub(current_publication=publication()),
    )
    with pytest.raises(RepositoryDataError, match="target revision"):
        subject.mark_staged(ScriptedTransaction(), expected=publication(), attempt=attempt())

    subject, _agents, _drafts, _work = repository(
        drafts=DraftStub(current_publication=publication())
    )
    with pytest.raises(RepositoryConflictError, match="generation claim"):
        subject.mark_staged(
            ScriptedTransaction(one=[None]), expected=publication(), attempt=attempt()
        )
    with pytest.raises(RepositoryConflictError, match="operation attempt"):
        subject.mark_staged(
            ScriptedTransaction(one=[{"claim_is_live": 1}]),
            expected=publication(),
            attempt=attempt(recovery=True),
        )

    manifest_digest = canonical_generated_agent_manifest_digest(manifest())
    validated = publication(
        state="validated",
        artifact_digest=ARTIFACT_DIGEST,
        manifest_digest=manifest_digest,
    )
    subject, _agents, _drafts, _work = repository(
        agents=AgentStub(current_revision=revision(artifact_digest="c" * 64)),
        drafts=DraftStub(current_publication=validated),
    )
    with pytest.raises(RepositoryConflictError, match="persisted manifest"):
        subject.commit_published(ScriptedTransaction(), expected=validated, attempt=attempt())


def test_statement_level_cas_failures_raise_conflicts() -> None:
    expected = publication(state="staged", state_revision=1)
    subject, _agents, _drafts, _work = repository(drafts=DraftStub(current_publication=expected))
    with pytest.raises(RepositoryConflictError, match="failure fence"):
        subject.fail(
            ScriptedTransaction(one=[{"claim_is_live": 1}, None]),
            expected=expected,
            attempt=attempt(),
            failure_code="artifact_invalid",
            safe_error_message="Generated artifact failed validation.",
        )

    manifest_digest = canonical_generated_agent_manifest_digest(manifest())
    validated = publication(
        state="validated",
        state_revision=2,
        artifact_digest=ARTIFACT_DIGEST,
        manifest_digest=manifest_digest,
    )
    subject, _agents, _drafts, _work = repository(drafts=DraftStub(current_publication=validated))
    with pytest.raises(RepositoryConflictError, match="commit fence"):
        subject.commit_published(
            ScriptedTransaction(one=[{"claim_is_live": 1}, None]),
            expected=validated,
            attempt=attempt(),
        )
