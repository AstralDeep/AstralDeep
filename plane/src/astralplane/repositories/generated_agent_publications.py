"""Database-side intent, lifecycle, and reconciliation fences for generated-agent
artifact publication; immutable_bundle_store.py commits bytes separately, reconciled
after a crash rather than as one transaction.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from astralplane.contracts import Transaction
from astralplane.database.dialect import dialect_of
from astralplane.immutable_bundle_store import (
    BundlePublicationKey,
    BundlePublicationPaths,
    FinalizedBundle,
    ImmutableBundleContract,
    paths_for,
)
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_text,
    _canonical_json,
    _non_negative_int,
    _positive_int,
    _required_id,
)
from astralplane.repositories import generated_agent_publication_sqlite as sqlite_publication
from astralplane.repositories.agents import AgentRepository, AgentRevisionRecord
from astralplane.repositories.drafts import (
    _GENERATED_PUBLICATION_MUTATION_CAPABILITY,
    DraftAgentRecord,
    DraftAgentRepository,
    DraftPublicationRecord,
    _publication,
)
from astralplane.repositories.work_admission import (
    ExecutionFence,
    OperationRecord,
    OperationState,
    OwnerScope,
    WorkAdmissionRepository,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_NONTERMINAL_STATES = frozenset({"claimed", "staged", "validated"})
_TERMINAL_OPERATION_STATES = frozenset(
    {
        OperationState.COMPLETED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.RETRYABLE,
    }
)

GENERATED_AGENT_PUBLICATION_OPERATION_KIND = "generated_agent_publication"
GENERATED_AGENT_PUBLICATION_RECOVERY_OPERATION_KIND = "generated_agent_publication_recovery"
GENERATED_AGENT_PUBLICATION_IDEMPOTENCY_NAMESPACE = "generated-agent-publication"
GENERATED_AGENT_PUBLICATION_RECOVERY_IDEMPOTENCY_NAMESPACE = "generated-agent-publication-recovery"
GENERATED_AGENT_BUNDLE_CONTRACT = ImmutableBundleContract(
    file_names=(
        "agent_main.py",
        "astralprims_ui.py",
        "protected_executor.py",
        "mcp_tools.py",
    ),
    scope_identity_field="agent_id",
    required_text_metadata_fields=(
        "agent_name",
        "description",
        "constitution_version",
    ),
    nonempty_text_metadata_fields=("constitution_version",),
)


@dataclass(frozen=True, slots=True)
class GeneratedAgentPublicationOperationBinding:
    operation_kind: str
    idempotency_namespace: str
    idempotency_key: str
    normalized_input_digest: str
    parent_operation_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class GeneratedAgentPublicationIntent:
    publication: DraftPublicationRecord
    revision: AgentRevisionRecord
    replayed: bool


@dataclass(frozen=True, slots=True)
class GeneratedAgentPublicationResultMetadata:
    error_message: str | None = None
    security_report: str | None = None
    validation_report: str | None = None
    required_credentials: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "error_message",
            _optional_result_text(
                self.error_message,
                "error_message",
                maximum=8_192,
            ),
        )
        for field_name in (
            "security_report",
            "validation_report",
            "required_credentials",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_result_text(
                    getattr(self, field_name),
                    field_name,
                    maximum=1_048_576,
                ),
            )


def canonical_generated_agent_manifest_digest(manifest: Mapping[str, Any]) -> str:
    if not isinstance(manifest, Mapping):
        raise RepositoryValidationError("manifest must be a JSON object")
    canonical = _canonical_json(manifest, "manifest") + "\n"
    encoded = canonical.encode("utf-8")
    if len(encoded) > 65_536:
        raise RepositoryValidationError("manifest must not exceed 64 KiB")
    return hashlib.sha256(encoded).hexdigest()


def generated_agent_publication_paths(
    *,
    draft_uuid: str,
    source_state_revision: int,
    publication_id: str,
    target_agent_id: str,
    target_revision_id: str,
) -> BundlePublicationPaths:
    try:
        return paths_for(
            BundlePublicationKey(
                scope_id=target_agent_id,
                staging_id=draft_uuid,
                source_revision=source_state_revision,
                publication_id=publication_id,
                revision_id=target_revision_id,
            )
        )
    except (TypeError, ValueError) as exc:
        raise RepositoryValidationError("publication path identity is invalid") from exc


def generated_agent_publication_operation_binding(
    *,
    owner_id: str,
    publication_id: str,
    draft_uuid: str,
    source_state_revision: int,
    generation_claim_id: str,
    target_agent_id: str,
    target_revision_id: str,
    bundle: FinalizedBundle,
    runtime_contract_version: int,
    release_lock_digest: str,
    promotion_token: str,
    compatibility_state: str = "compatible",
) -> GeneratedAgentPublicationOperationBinding:
    owner_id = _required_id(owner_id, "owner_id")
    publication_id = _uuid_text(publication_id, "publication_id")
    draft_uuid = _uuid_text(draft_uuid, "draft_uuid")
    source_state_revision = _non_negative_int(source_state_revision, "source_state_revision")
    generation_claim_id = _uuid_text(generation_claim_id, "generation_claim_id")
    target_agent_id = _required_id(target_agent_id, "target_agent_id", maximum=255)
    target_revision_id = _uuid_text(target_revision_id, "target_revision_id")
    bundle = _generated_agent_bundle(bundle)
    artifact_digest = bundle.bundle_sha256
    manifest = bundle.manifest
    release_lock_digest = _digest(release_lock_digest, "release_lock_digest")
    promotion_token = _uuid_text(promotion_token, "promotion_token")
    runtime_contract_version = _positive_int(runtime_contract_version, "runtime_contract_version")
    compatibility_state = _compatibility_state(compatibility_state)
    manifest_json = _validated_manifest(
        manifest,
        target_agent_id=target_agent_id,
        target_revision_id=target_revision_id,
        artifact_digest=artifact_digest,
        runtime_contract_version=runtime_contract_version,
        release_lock_digest=release_lock_digest,
    )
    if bundle.manifest_json != manifest_json + "\n":
        raise RepositoryValidationError("generated-agent manifest bytes are not canonical")
    canonical_paths = generated_agent_publication_paths(
        draft_uuid=draft_uuid,
        source_state_revision=source_state_revision,
        publication_id=publication_id,
        target_agent_id=target_agent_id,
        target_revision_id=target_revision_id,
    )
    input_digest = _publication_input_digest(
        owner_id=owner_id,
        publication_id=publication_id,
        draft_uuid=draft_uuid,
        source_state_revision=source_state_revision,
        generation_claim_id=generation_claim_id,
        target_agent_id=target_agent_id,
        target_revision_id=target_revision_id,
        staging_relative_path=canonical_paths.staging_relative_path,
        revision_relative_path=canonical_paths.revision_relative_path,
        artifact_digest=artifact_digest,
        manifest_digest=bundle.manifest_sha256,
        runtime_contract_version=runtime_contract_version,
        release_lock_digest=release_lock_digest,
        promotion_token=promotion_token,
        compatibility_state=compatibility_state,
    )
    return GeneratedAgentPublicationOperationBinding(
        operation_kind=GENERATED_AGENT_PUBLICATION_OPERATION_KIND,
        idempotency_namespace=GENERATED_AGENT_PUBLICATION_IDEMPOTENCY_NAMESPACE,
        idempotency_key=publication_id,
        normalized_input_digest=input_digest,
        parent_operation_id=None,
    )


def generated_agent_publication_recovery_operation_binding(
    publication: DraftPublicationRecord,
    revision: AgentRevisionRecord,
) -> GeneratedAgentPublicationOperationBinding:
    publication = _publication_snapshot(publication)
    if publication.state not in _NONTERMINAL_STATES:
        raise RepositoryValidationError("only a nonterminal publication can be recovered")
    _assert_revision_for_publication(revision, publication)
    if publication.operation_id is None:
        raise RepositoryConflictError("publication prior operation was already purged")
    try:
        parent_operation_id = uuid.UUID(publication.operation_id)
    except ValueError as exc:
        raise RepositoryDataError("publication operation identity is invalid") from exc
    return GeneratedAgentPublicationOperationBinding(
        operation_kind=GENERATED_AGENT_PUBLICATION_RECOVERY_OPERATION_KIND,
        idempotency_namespace=(GENERATED_AGENT_PUBLICATION_RECOVERY_IDEMPOTENCY_NAMESPACE),
        idempotency_key=(
            f"{publication.publication_id}:{publication.state_revision}:{parent_operation_id}"
        ),
        normalized_input_digest=_publication_input_digest_from_records(publication, revision),
        parent_operation_id=parent_operation_id,
    )


class GeneratedAgentPublicationRepository:
    def __init__(
        self,
        *,
        agents: AgentRepository | None = None,
        drafts: DraftAgentRepository | None = None,
        work_admission: WorkAdmissionRepository | None = None,
    ) -> None:
        self._agents = agents or AgentRepository()
        self._drafts = drafts or DraftAgentRepository()
        self._work_admission = work_admission or WorkAdmissionRepository()

    def begin_intent(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        publication_id: str,
        draft_uuid: str,
        source_state_revision: int,
        generation_claim_id: str,
        target_agent_id: str,
        target_revision_id: str,
        staging_relative_path: str,
        revision_relative_path: str,
        bundle: FinalizedBundle,
        runtime_contract_version: int,
        release_lock_digest: str,
        promotion_token: str,
        attempt: ExecutionFence,
        compatibility_state: str = "compatible",
    ) -> GeneratedAgentPublicationIntent:
        owner_id = _required_id(owner_id, "owner_id")
        publication_id = _uuid_text(publication_id, "publication_id")
        draft_uuid = _uuid_text(draft_uuid, "draft_uuid")
        source_state_revision = _non_negative_int(source_state_revision, "source_state_revision")
        generation_claim_id = _uuid_text(generation_claim_id, "generation_claim_id")
        target_agent_id = _required_id(target_agent_id, "target_agent_id", maximum=255)
        target_revision_id = _uuid_text(target_revision_id, "target_revision_id")
        staging_relative_path = _relative_path(staging_relative_path, "staging_relative_path")
        revision_relative_path = _relative_path(revision_relative_path, "revision_relative_path")
        bundle = _generated_agent_bundle(bundle)
        artifact_digest = bundle.bundle_sha256
        manifest = bundle.manifest
        release_lock_digest = _digest(release_lock_digest, "release_lock_digest")
        promotion_token = _uuid_text(promotion_token, "promotion_token")
        runtime_contract_version = _positive_int(
            runtime_contract_version, "runtime_contract_version"
        )
        compatibility_state = _compatibility_state(compatibility_state)
        manifest_json = _validated_manifest(
            manifest,
            target_agent_id=target_agent_id,
            target_revision_id=target_revision_id,
            artifact_digest=artifact_digest,
            runtime_contract_version=runtime_contract_version,
            release_lock_digest=release_lock_digest,
        )
        if bundle.manifest_json != manifest_json + "\n":
            raise RepositoryValidationError("generated-agent manifest bytes are not canonical")
        canonical_paths = generated_agent_publication_paths(
            draft_uuid=draft_uuid,
            source_state_revision=source_state_revision,
            publication_id=publication_id,
            target_agent_id=target_agent_id,
            target_revision_id=target_revision_id,
        )
        if (
            staging_relative_path != canonical_paths.staging_relative_path
            or revision_relative_path != canonical_paths.revision_relative_path
        ):
            raise RepositoryValidationError("publication paths are not canonical for identity")
        operation_binding = generated_agent_publication_operation_binding(
            owner_id=owner_id,
            publication_id=publication_id,
            draft_uuid=draft_uuid,
            source_state_revision=source_state_revision,
            generation_claim_id=generation_claim_id,
            target_agent_id=target_agent_id,
            target_revision_id=target_revision_id,
            bundle=bundle,
            runtime_contract_version=runtime_contract_version,
            release_lock_digest=release_lock_digest,
            promotion_token=promotion_token,
            compatibility_state=compatibility_state,
        )

        self._agents.lock_owner(transaction, owner_id=owner_id)
        draft = self._drafts.get_draft_by_uuid(
            transaction,
            owner_id=owner_id,
            draft_uuid=draft_uuid,
            for_update=True,
        )
        if draft is None:
            raise RepositoryNotFoundError("owner-scoped draft was not found")

        existing = self._drafts.get_publication_by_source(
            transaction,
            owner_id=owner_id,
            draft_uuid=draft_uuid,
            source_state_revision=source_state_revision,
            for_update=True,
        )
        if existing is not None:
            _assert_begin_replay(
                existing,
                publication_id=publication_id,
                generation_claim_id=generation_claim_id,
                target_agent_id=target_agent_id,
                target_revision_id=target_revision_id,
                staging_relative_path=staging_relative_path,
                revision_relative_path=revision_relative_path,
            )
            revision = self._agents.get_revision(
                transaction,
                owner_id=owner_id,
                agent_id=target_agent_id,
                revision_id=target_revision_id,
                for_update=True,
            )
            if revision is None:
                raise RepositoryDataError("publication target revision is missing")
            _assert_revision_intent(
                revision,
                artifact_digest=artifact_digest,
                manifest_json=manifest_json,
                revision_relative_path=revision_relative_path,
                runtime_contract_version=runtime_contract_version,
                release_lock_digest=release_lock_digest,
                promotion_token=promotion_token,
                compatibility_state=compatibility_state,
                require_prepared=existing.state in _NONTERMINAL_STATES,
            )
            _assert_revision_for_publication(revision, existing)
            _assert_terminal_or_current_draft(draft, existing)
            if existing.state in _NONTERMINAL_STATES:
                self._assert_current_bound_attempt(
                    transaction,
                    publication=existing,
                    revision=revision,
                    attempt=attempt,
                )
                self._assert_live_claim(transaction, draft=draft)
            else:
                self._assert_terminal_replay_attempt(
                    transaction,
                    publication=existing,
                    revision=revision,
                    attempt=attempt,
                )
            return GeneratedAgentPublicationIntent(existing, revision, True)

        operation = self._assert_current_operation(
            transaction,
            owner_id=owner_id,
            attempt=attempt,
        )
        _assert_original_operation_binding(operation, operation_binding)

        _assert_current_draft(
            draft,
            _prospective_publication(
                publication_id=publication_id,
                draft_uuid=draft_uuid,
                owner_id=owner_id,
                source_state_revision=source_state_revision,
                generation_claim_id=generation_claim_id,
                target_agent_id=target_agent_id,
                target_revision_id=target_revision_id,
                operation_id=str(attempt.operation_id),
                operation_execution_generation=attempt.execution_generation,
                staging_relative_path=staging_relative_path,
                revision_relative_path=revision_relative_path,
            ),
        )
        self._assert_live_claim(transaction, draft=draft)

        agent = self._agents.get_agent(
            transaction,
            owner_id=owner_id,
            agent_id=target_agent_id,
            for_update=True,
        )
        if agent is None or agent.deleted_at is not None:
            raise RepositoryNotFoundError("owner-scoped target agent was not found")
        latest = self._agents.list_revisions(
            transaction, owner_id=owner_id, agent_id=target_agent_id, limit=1
        )
        revision_number = 0 if not latest else latest[0].revision_number + 1
        parent_revision_id = agent.active_revision_id
        revision = self._agents.create_revision(
            transaction,
            revision_id=target_revision_id,
            agent_id=target_agent_id,
            owner_id=owner_id,
            revision_number=revision_number,
            parent_revision_id=parent_revision_id,
            previous_good_revision_id=parent_revision_id,
            artifact_digest=artifact_digest,
            manifest=manifest,
            artifact_relative_path=revision_relative_path,
            runtime_contract_version=runtime_contract_version,
            release_lock_digest=release_lock_digest,
            compatibility_state=compatibility_state,
            state="prepared",
            promotion_token=promotion_token,
        )
        publication = self._drafts._create_publication(
            transaction,
            _capability=_GENERATED_PUBLICATION_MUTATION_CAPABILITY,
            publication_id=publication_id,
            draft_uuid=draft_uuid,
            owner_id=owner_id,
            source_state_revision=source_state_revision,
            generation_claim_id=generation_claim_id,
            target_agent_id=target_agent_id,
            target_revision_id=target_revision_id,
            operation_id=str(attempt.operation_id),
            operation_execution_generation=attempt.execution_generation,
            staging_relative_path=staging_relative_path,
            revision_relative_path=revision_relative_path,
        )
        return GeneratedAgentPublicationIntent(publication, revision, False)

    def get_by_source(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        draft_uuid: str,
        source_state_revision: int,
    ) -> DraftPublicationRecord | None:
        return self._drafts.get_publication_by_source(
            transaction,
            owner_id=owner_id,
            draft_uuid=draft_uuid,
            source_state_revision=source_state_revision,
        )

    def get_by_target_revision(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        target_agent_id: str,
        target_revision_id: str,
    ) -> DraftPublicationRecord | None:
        return self._drafts.get_publication_by_target_revision(
            transaction,
            owner_id=owner_id,
            target_agent_id=target_agent_id,
            target_revision_id=target_revision_id,
        )

    def list_reconcilable_for_administration(
        self,
        transaction: Transaction,
        *,
        limit: int = 100,
        after_created_at: datetime | None = None,
        after_publication_id: str | None = None,
    ) -> tuple[DraftPublicationRecord, ...]:
        return self._drafts.list_reconcilable_publications_for_administration(
            transaction,
            limit=limit,
            after_created_at=after_created_at,
            after_publication_id=after_publication_id,
        )

    def assert_current_attempt(
        self,
        transaction: Transaction,
        *,
        expected: DraftPublicationRecord,
        attempt: ExecutionFence,
    ) -> DraftPublicationRecord:
        expected = _publication_snapshot(expected)
        if expected.state not in _NONTERMINAL_STATES:
            raise RepositoryValidationError("only a nonterminal publication has a current attempt")
        self._agents.lock_owner(transaction, owner_id=expected.owner_id)
        current, _draft, revision = self._lock_context(
            transaction,
            expected=expected,
            require_live_claim=True,
        )
        _assert_snapshot(current, expected)
        self._assert_current_bound_attempt(
            transaction,
            publication=current,
            revision=revision,
            attempt=attempt,
        )
        return current

    def renew_generation_claim(
        self,
        transaction: Transaction,
        *,
        expected: DraftPublicationRecord,
        attempt: ExecutionFence,
        lease_seconds: int = 300,
    ) -> DraftAgentRecord:
        expected = _publication_snapshot(expected)
        if expected.state not in _NONTERMINAL_STATES:
            raise RepositoryValidationError("only a nonterminal publication claim can be renewed")
        lease_seconds = _positive_int(lease_seconds, "lease_seconds")
        if lease_seconds > 1800:
            raise RepositoryValidationError("lease_seconds must not exceed 1800")
        self._agents.lock_owner(transaction, owner_id=expected.owner_id)
        current, draft, revision = self._lock_context(
            transaction,
            expected=expected,
            require_live_claim=True,
        )
        _assert_snapshot(current, expected)
        self._assert_current_bound_attempt(
            transaction,
            publication=current,
            revision=revision,
            attempt=attempt,
        )
        renewed = self._drafts.renew_generation_claim(
            transaction,
            owner_id=expected.owner_id,
            draft_id=draft.draft_id,
            expected_revision=expected.source_state_revision,
            claim_id=expected.generation_claim_id,
            lease_seconds=lease_seconds,
        )
        _assert_current_draft(renewed, current)
        return renewed

    def rebind_recovery_attempt(
        self,
        transaction: Transaction,
        *,
        expected: DraftPublicationRecord,
        new_attempt: ExecutionFence,
        lease_seconds: int = 300,
    ) -> DraftPublicationRecord:
        expected = _publication_snapshot(expected)
        if expected.state not in _NONTERMINAL_STATES:
            raise RepositoryValidationError("only nonterminal publications can be rebound")
        lease_seconds = _positive_int(lease_seconds, "lease_seconds")
        if lease_seconds > 1800:
            raise RepositoryValidationError("lease_seconds must not exceed 1800")
        self._agents.lock_owner(transaction, owner_id=expected.owner_id)
        new_operation = self._assert_current_operation(
            transaction,
            owner_id=expected.owner_id,
            attempt=new_attempt,
        )
        current, draft, revision = self._lock_context(
            transaction,
            expected=expected,
            require_live_claim=False,
        )
        if _is_rebind_replay(current, expected, new_attempt):
            _assert_bound_operation_identity(new_operation, current, revision)
            return current
        _assert_snapshot(current, expected)
        _assert_current_draft(draft, expected)
        self._assert_recovery_lineage(
            transaction,
            publication=current,
            revision=revision,
            new_operation=new_operation,
            new_attempt=new_attempt,
        )

        if dialect_of(transaction).is_sqlite:
            return _publication(
                sqlite_publication.rebind(transaction, expected, new_attempt, lease_seconds)
            )

        row = transaction.fetch_one(
            """
            WITH eligible AS (
                SELECT publication.publication_id, draft.id AS draft_id
                FROM draft_artifact_publication AS publication
                JOIN draft_agents AS draft
                  ON draft.draft_uuid = publication.draft_uuid
                 AND draft.user_id = publication.owner_user_id
                WHERE publication.publication_id = %s
                  AND publication.owner_user_id = %s
                  AND publication.state_revision = %s
                  AND publication.state = %s
                  AND publication.draft_uuid = %s
                  AND publication.source_state_revision = %s
                  AND publication.generation_claim_id = %s
                  AND publication.target_agent_id = %s
                  AND publication.target_revision_id = %s
                  AND publication.staging_relative_path = %s
                  AND publication.revision_relative_path = %s
                  AND publication.operation_id IS NOT DISTINCT FROM %s
                  AND publication.operation_execution_generation IS NOT DISTINCT FROM %s
                  AND publication.artifact_digest IS NOT DISTINCT FROM %s
                  AND publication.manifest_digest IS NOT DISTINCT FROM %s
                  AND draft.state_revision = publication.source_state_revision
                  AND draft.generation_claim_id = publication.generation_claim_id
                  AND draft.target_agent_id = publication.target_agent_id
                  AND draft.status = 'generating'
                  AND draft.published_revision_id IS NULL
                FOR UPDATE OF publication, draft
            ), renewed AS (
                UPDATE draft_agents AS draft SET
                    generation_claim_expires_at =
                        GREATEST(
                            draft.generation_claim_expires_at,
                            clock_timestamp() + (%s * interval '1 second')
                        ),
                    updated_at =
                        (extract(epoch from clock_timestamp()) * 1000)::bigint
                FROM eligible
                WHERE draft.id = eligible.draft_id
                RETURNING eligible.publication_id
            )
            UPDATE draft_artifact_publication AS publication SET
                operation_id = %s,
                operation_execution_generation = %s,
                state_revision = publication.state_revision + 1
            FROM renewed
            WHERE publication.publication_id = renewed.publication_id
            RETURNING publication.*
            """,
            (
                expected.publication_id,
                expected.owner_id,
                expected.state_revision,
                expected.state,
                expected.draft_uuid,
                expected.source_state_revision,
                expected.generation_claim_id,
                expected.target_agent_id,
                expected.target_revision_id,
                expected.staging_relative_path,
                expected.revision_relative_path,
                expected.operation_id,
                expected.operation_execution_generation,
                expected.artifact_digest,
                expected.manifest_digest,
                lease_seconds,
                str(new_attempt.operation_id),
                new_attempt.execution_generation,
            ),
        )
        if row is None:
            raise RepositoryConflictError("publication recovery fence is stale")
        return _publication(row)

    def _assert_recovery_lineage(
        self,
        transaction: Transaction,
        *,
        publication: DraftPublicationRecord,
        revision: AgentRevisionRecord,
        new_operation: OperationRecord,
        new_attempt: ExecutionFence,
    ) -> None:
        if publication.operation_id is None:
            raise RepositoryConflictError("publication prior operation was already purged")
        try:
            prior_operation_id = uuid.UUID(publication.operation_id)
        except ValueError as exc:
            raise RepositoryDataError("publication operation identity is invalid") from exc
        if prior_operation_id == new_attempt.operation_id:
            prior_generation = publication.operation_execution_generation
            if prior_generation is None or prior_generation >= new_attempt.execution_generation:
                raise RepositoryConflictError("publication recovery attempt is not newer")
            _assert_bound_operation_identity(
                new_operation,
                publication,
                revision,
                allow_newer_generation=True,
            )
            return
        prior = self._work_admission.get_operation_for_administration(
            transaction,
            operation_id=prior_operation_id,
            for_update=True,
        )
        if prior is None:
            raise RepositoryConflictError("publication prior operation was already purged")
        _assert_bound_operation_identity(prior, publication, revision)
        if prior.state not in _TERMINAL_OPERATION_STATES:
            raise RepositoryConflictError("publication prior operation is still live")
        required = generated_agent_publication_recovery_operation_binding(publication, revision)
        _assert_recovery_operation_binding(new_operation, required)

    def mark_staged(
        self,
        transaction: Transaction,
        *,
        expected: DraftPublicationRecord,
        attempt: ExecutionFence,
    ) -> DraftPublicationRecord:
        return self._simple_transition(
            transaction,
            expected=expected,
            attempt=attempt,
            expected_state="claimed",
            result_state="staged",
            updates={"state": "staged"},
        )

    def mark_validated(
        self,
        transaction: Transaction,
        *,
        expected: DraftPublicationRecord,
        attempt: ExecutionFence,
        artifact_digest: str,
        manifest_digest: str,
        generation_result: GeneratedAgentPublicationResultMetadata,
    ) -> DraftPublicationRecord:
        expected = _publication_snapshot(expected)
        if expected.state != "staged":
            raise RepositoryValidationError("validation requires a staged publication")
        artifact_digest = _digest(artifact_digest, "artifact_digest")
        manifest_digest = _digest(manifest_digest, "manifest_digest")
        generation_result = _generation_result(generation_result)
        self._agents.lock_owner(transaction, owner_id=expected.owner_id)
        current, draft, revision = self._lock_context(
            transaction, expected=expected, require_live_claim=True
        )
        if _is_transition_replay(
            current,
            expected,
            state="validated",
            artifact_digest=artifact_digest,
            manifest_digest=manifest_digest,
        ):
            self._assert_current_bound_attempt(
                transaction,
                publication=current,
                revision=revision,
                attempt=attempt,
            )
            _assert_generation_result(draft, generation_result)
            return current
        _assert_snapshot(current, expected)
        self._assert_current_bound_attempt(
            transaction,
            publication=current,
            revision=revision,
            attempt=attempt,
        )
        if revision.artifact_digest != artifact_digest:
            raise RepositoryConflictError("validated artifact digest changed revision identity")
        if canonical_generated_agent_manifest_digest(revision.manifest or {}) != manifest_digest:
            raise RepositoryConflictError("validated manifest digest changed revision identity")
        if dialect_of(transaction).is_sqlite:
            return _publication(
                sqlite_publication.validate(
                    transaction,
                    expected,
                    attempt,
                    artifact_digest,
                    manifest_digest,
                    generation_result,
                )
            )
        row = transaction.fetch_one(
            """
            WITH eligible AS (
                SELECT publication.publication_id, draft.id AS draft_id
                FROM draft_artifact_publication AS publication
                JOIN draft_agents AS draft
                  ON draft.draft_uuid = publication.draft_uuid
                 AND draft.user_id = publication.owner_user_id
                JOIN user_agent_revision AS revision
                  ON revision.revision_id = publication.target_revision_id
                 AND revision.agent_id = publication.target_agent_id
                 AND revision.owner_user_id = publication.owner_user_id
                WHERE publication.publication_id = %s
                  AND publication.owner_user_id = %s
                  AND publication.state_revision = %s
                  AND publication.state = 'staged'
                  AND publication.operation_id = %s
                  AND publication.operation_execution_generation = %s
                  AND draft.state_revision = publication.source_state_revision
                  AND draft.generation_claim_id = publication.generation_claim_id
                  AND draft.generation_claim_expires_at > clock_timestamp()
                  AND draft.target_agent_id = publication.target_agent_id
                  AND draft.status = 'generating'
                  AND draft.published_revision_id IS NULL
                  AND revision.state = 'prepared'
                  AND revision.artifact_digest = %s
                  AND revision.artifact_relative_path = publication.revision_relative_path
                FOR UPDATE OF publication, draft, revision
            ), persisted_result AS (
                UPDATE draft_agents AS draft SET
                    error_message = %s,
                    security_report = %s,
                    validation_report = %s,
                    required_credentials = %s,
                    updated_at =
                        (extract(epoch from clock_timestamp()) * 1000)::bigint
                FROM eligible
                WHERE draft.id = eligible.draft_id
                RETURNING eligible.publication_id
            )
            UPDATE draft_artifact_publication AS publication SET
                state = 'validated',
                artifact_digest = %s,
                manifest_digest = %s,
                state_revision = publication.state_revision + 1
            FROM persisted_result
            WHERE publication.publication_id = persisted_result.publication_id
            RETURNING publication.*
            """,
            (
                expected.publication_id,
                expected.owner_id,
                expected.state_revision,
                str(attempt.operation_id),
                attempt.execution_generation,
                artifact_digest,
                generation_result.error_message,
                generation_result.security_report,
                generation_result.validation_report,
                generation_result.required_credentials,
                artifact_digest,
                manifest_digest,
            ),
        )
        if row is None:
            raise RepositoryConflictError("publication validation fence is stale")
        return _publication(row)

    def fail(
        self,
        transaction: Transaction,
        *,
        expected: DraftPublicationRecord,
        attempt: ExecutionFence,
        failure_code: str,
        safe_error_message: str,
    ) -> DraftPublicationRecord:
        expected = _publication_snapshot(expected)
        if expected.state not in _NONTERMINAL_STATES:
            raise RepositoryValidationError("only a nonterminal publication can fail")
        failure_code = _failure_code(failure_code)
        safe_error_message = _bounded_text(
            safe_error_message,
            "safe_error_message",
            maximum=8_192,
        )
        self._agents.lock_owner(transaction, owner_id=expected.owner_id)
        current, draft, revision = self._lock_context(
            transaction, expected=expected, require_live_claim=True
        )
        if _is_transition_replay(
            current,
            expected,
            state="failed",
            failure_code=failure_code,
        ):
            self._assert_terminal_replay_attempt(
                transaction,
                publication=current,
                revision=revision,
                attempt=attempt,
            )
            _assert_failed_draft_result(draft, safe_error_message)
            return current
        _assert_snapshot(current, expected)
        self._assert_current_bound_attempt(
            transaction,
            publication=current,
            revision=revision,
            attempt=attempt,
        )
        if dialect_of(transaction).is_sqlite:
            return _publication(
                sqlite_publication.fail(
                    transaction, expected, attempt, failure_code, safe_error_message
                )
            )
        row = transaction.fetch_one(
            """
            WITH eligible AS (
                SELECT publication.publication_id, draft.id AS draft_id,
                       revision.revision_id
                FROM draft_artifact_publication AS publication
                JOIN draft_agents AS draft
                  ON draft.draft_uuid = publication.draft_uuid
                 AND draft.user_id = publication.owner_user_id
                JOIN user_agent_revision AS revision
                  ON revision.revision_id = publication.target_revision_id
                 AND revision.agent_id = publication.target_agent_id
                 AND revision.owner_user_id = publication.owner_user_id
                WHERE publication.publication_id = %s
                  AND publication.owner_user_id = %s
                  AND publication.state_revision = %s
                  AND publication.state = %s
                  AND publication.operation_id = %s
                  AND publication.operation_execution_generation = %s
                  AND draft.state_revision = publication.source_state_revision
                  AND draft.generation_claim_id = publication.generation_claim_id
                  AND draft.generation_claim_expires_at > clock_timestamp()
                  AND draft.target_agent_id = publication.target_agent_id
                  AND draft.status = 'generating'
                  AND draft.published_revision_id IS NULL
                  AND revision.state = 'prepared'
                  AND revision.artifact_relative_path = publication.revision_relative_path
                FOR UPDATE OF publication, draft, revision
            ), failed_draft AS (
                UPDATE draft_agents AS draft SET
                    generation_claim_id = NULL,
                    generation_claim_expires_at = NULL,
                    status = 'error',
                    error_message = %s,
                    state_revision = draft.state_revision + 1,
                    updated_at =
                        (extract(epoch from clock_timestamp()) * 1000)::bigint
                FROM eligible
                WHERE draft.id = eligible.draft_id
                RETURNING eligible.publication_id, eligible.revision_id
            ), failed_revision AS (
                UPDATE user_agent_revision AS revision SET
                    state = 'failed',
                    failed_at = clock_timestamp(),
                    failure_code = %s,
                    state_revision = revision.state_revision + 1
                FROM failed_draft
                WHERE revision.revision_id = failed_draft.revision_id
                  AND revision.state = 'prepared'
                RETURNING failed_draft.publication_id
            )
            UPDATE draft_artifact_publication AS publication SET
                state = 'failed',
                failed_at = clock_timestamp(),
                failure_code = %s,
                state_revision = publication.state_revision + 1
            FROM failed_revision
            WHERE publication.publication_id = failed_revision.publication_id
            RETURNING publication.*
            """,
            (
                expected.publication_id,
                expected.owner_id,
                expected.state_revision,
                expected.state,
                str(attempt.operation_id),
                attempt.execution_generation,
                safe_error_message,
                failure_code,
                failure_code,
            ),
        )
        if row is None:
            raise RepositoryConflictError("publication failure fence is stale")
        return _publication(row)

    def commit_published(
        self,
        transaction: Transaction,
        *,
        expected: DraftPublicationRecord,
        attempt: ExecutionFence,
        generation_result: GeneratedAgentPublicationResultMetadata | None = None,
    ) -> DraftPublicationRecord:
        expected = _publication_snapshot(expected)
        if expected.state != "validated":
            raise RepositoryValidationError("publication commit requires validated state")
        if expected.artifact_digest is None or expected.manifest_digest is None:
            raise RepositoryValidationError("validated publication digests are required")
        self._agents.lock_owner(transaction, owner_id=expected.owner_id)
        current, draft, revision = self._lock_context(
            transaction, expected=expected, require_live_claim=True
        )
        persisted_result = _persisted_generation_result(draft)
        if generation_result is not None:
            supplied_result = _generation_result(generation_result)
            if supplied_result != persisted_result:
                raise RepositoryConflictError(
                    "publication commit changed persisted generation result metadata"
                )
        if _is_transition_replay(current, expected, state="published"):
            self._assert_terminal_replay_attempt(
                transaction,
                publication=current,
                revision=revision,
                attempt=attempt,
            )
            _assert_published_draft_result(draft, current, persisted_result)
            return current
        _assert_snapshot(current, expected)
        self._assert_current_bound_attempt(
            transaction,
            publication=current,
            revision=revision,
            attempt=attempt,
        )
        if dialect_of(transaction).is_sqlite:
            return _publication(
                sqlite_publication.publish(transaction, expected, attempt, persisted_result)
            )
        row = transaction.fetch_one(
            """
            WITH eligible AS (
                SELECT publication.publication_id, draft.id AS draft_id
                FROM draft_artifact_publication AS publication
                JOIN draft_agents AS draft
                  ON draft.draft_uuid = publication.draft_uuid
                 AND draft.user_id = publication.owner_user_id
                JOIN user_agent_revision AS revision
                  ON revision.revision_id = publication.target_revision_id
                 AND revision.agent_id = publication.target_agent_id
                 AND revision.owner_user_id = publication.owner_user_id
                WHERE publication.publication_id = %s
                  AND publication.owner_user_id = %s
                  AND publication.state_revision = %s
                  AND publication.state = 'validated'
                  AND publication.operation_id = %s
                  AND publication.operation_execution_generation = %s
                  AND publication.artifact_digest = %s
                  AND publication.manifest_digest = %s
                  AND draft.state_revision = publication.source_state_revision
                  AND draft.generation_claim_id = publication.generation_claim_id
                  AND draft.generation_claim_expires_at > clock_timestamp()
                  AND draft.target_agent_id = publication.target_agent_id
                  AND draft.status = 'generating'
                  AND draft.published_revision_id IS NULL
                  AND draft.error_message IS NOT DISTINCT FROM %s
                  AND draft.security_report IS NOT DISTINCT FROM %s
                  AND draft.validation_report IS NOT DISTINCT FROM %s
                  AND draft.required_credentials IS NOT DISTINCT FROM %s
                  AND revision.state = 'prepared'
                  AND revision.artifact_digest = publication.artifact_digest
                  AND revision.artifact_relative_path = publication.revision_relative_path
                FOR UPDATE OF publication, draft, revision
            ), published_draft AS (
                UPDATE draft_agents AS draft SET
                    generation_claim_id = NULL,
                    generation_claim_expires_at = NULL,
                    status = 'generated',
                    published_revision_id = %s,
                    state_revision = draft.state_revision + 1,
                    updated_at =
                        (extract(epoch from clock_timestamp()) * 1000)::bigint
                FROM eligible
                WHERE draft.id = eligible.draft_id
                RETURNING eligible.publication_id
            )
            UPDATE draft_artifact_publication AS publication SET
                state = 'published',
                published_at = clock_timestamp(),
                state_revision = publication.state_revision + 1
            FROM published_draft
            WHERE publication.publication_id = published_draft.publication_id
            RETURNING publication.*
            """,
            (
                expected.publication_id,
                expected.owner_id,
                expected.state_revision,
                str(attempt.operation_id),
                attempt.execution_generation,
                expected.artifact_digest,
                expected.manifest_digest,
                persisted_result.error_message,
                persisted_result.security_report,
                persisted_result.validation_report,
                persisted_result.required_credentials,
                expected.target_revision_id,
            ),
        )
        if row is None:
            raise RepositoryConflictError("publication commit fence is stale")
        return _publication(row)

    def _simple_transition(
        self,
        transaction: Transaction,
        *,
        expected: DraftPublicationRecord,
        attempt: ExecutionFence,
        expected_state: str,
        result_state: str,
        updates: Mapping[str, object],
    ) -> DraftPublicationRecord:
        expected = _publication_snapshot(expected)
        if not isinstance(attempt, ExecutionFence):
            raise RepositoryValidationError("attempt must be an ExecutionFence")
        if expected.state != expected_state:
            raise RepositoryValidationError(
                f"publication transition requires {expected_state} state"
            )
        self._agents.lock_owner(transaction, owner_id=expected.owner_id)
        current, _draft, revision = self._lock_context(
            transaction, expected=expected, require_live_claim=True
        )
        if _is_transition_replay(current, expected, state=result_state):
            self._assert_current_bound_attempt(
                transaction,
                publication=current,
                revision=revision,
                attempt=attempt,
            )
            return current
        _assert_snapshot(current, expected)
        self._assert_current_bound_attempt(
            transaction,
            publication=current,
            revision=revision,
            attempt=attempt,
        )
        return self._drafts._transition_publication(
            transaction,
            _capability=_GENERATED_PUBLICATION_MUTATION_CAPABILITY,
            owner_id=expected.owner_id,
            publication_id=expected.publication_id,
            expected_revision=expected.state_revision,
            expected_state=expected_state,
            updates=updates,
        )

    def _assert_current_operation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        attempt: ExecutionFence,
    ) -> OperationRecord:
        if not isinstance(attempt, ExecutionFence):
            raise RepositoryValidationError("attempt must be an ExecutionFence")
        operation = self._work_admission.assert_current_execution(transaction, attempt)
        if not isinstance(operation, OperationRecord):
            raise RepositoryDataError("work admission returned an invalid operation record")
        if operation.owner_scope is not OwnerScope.USER or operation.owner_user_id != owner_id:
            raise RepositoryConflictError("publication operation owner fence is stale")
        return operation

    def _assert_current_bound_attempt(
        self,
        transaction: Transaction,
        *,
        publication: DraftPublicationRecord,
        revision: AgentRevisionRecord,
        attempt: ExecutionFence,
    ) -> OperationRecord:
        _assert_bound_attempt(publication, attempt)
        operation = self._assert_current_operation(
            transaction,
            owner_id=publication.owner_id,
            attempt=attempt,
        )
        _assert_bound_operation_identity(operation, publication, revision)
        return operation

    def _assert_terminal_replay_attempt(
        self,
        transaction: Transaction,
        *,
        publication: DraftPublicationRecord,
        revision: AgentRevisionRecord,
        attempt: ExecutionFence,
    ) -> OperationRecord:
        if not isinstance(attempt, ExecutionFence):
            raise RepositoryValidationError("attempt must be an ExecutionFence")
        _assert_bound_attempt(publication, attempt)
        if publication.operation_id is None:
            raise RepositoryConflictError("publication terminal attempt was already purged")
        operation = self._work_admission.get_operation_for_administration(
            transaction,
            operation_id=attempt.operation_id,
            for_update=True,
        )
        if operation is None:
            raise RepositoryConflictError("publication terminal attempt was already purged")
        _assert_bound_operation_identity(operation, publication, revision)
        if operation.execution_generation != attempt.execution_generation:
            raise RepositoryConflictError("publication terminal attempt generation is stale")
        if operation.state is OperationState.RUNNING:
            if operation.execution_lease_token != attempt.execution_lease_token:
                raise RepositoryConflictError("publication terminal attempt token is stale")
        elif operation.state not in _TERMINAL_OPERATION_STATES:
            raise RepositoryConflictError("publication terminal attempt is not authentic")
        return operation

    def _lock_context(
        self,
        transaction: Transaction,
        *,
        expected: DraftPublicationRecord,
        require_live_claim: bool,
    ) -> tuple[DraftPublicationRecord, DraftAgentRecord, AgentRevisionRecord]:
        current = self._drafts.get_publication(
            transaction,
            owner_id=expected.owner_id,
            publication_id=expected.publication_id,
            for_update=True,
        )
        if current is None:
            raise RepositoryNotFoundError("owner-scoped publication was not found")
        draft = self._drafts.get_draft_by_uuid(
            transaction,
            owner_id=expected.owner_id,
            draft_uuid=expected.draft_uuid,
            for_update=True,
        )
        if draft is None:
            raise RepositoryDataError("publication source draft is missing")
        revision = self._agents.get_revision(
            transaction,
            owner_id=expected.owner_id,
            agent_id=expected.target_agent_id,
            revision_id=expected.target_revision_id,
            for_update=True,
        )
        if revision is None:
            raise RepositoryDataError("publication target revision is missing")
        _assert_terminal_or_current_draft(draft, current)
        _assert_revision_for_publication(revision, current)
        if require_live_claim and current.state in _NONTERMINAL_STATES:
            self._assert_live_claim(transaction, draft=draft)
        return current, draft, revision

    @staticmethod
    def _assert_live_claim(
        transaction: Transaction,
        *,
        draft: DraftAgentRecord,
    ) -> None:
        row = transaction.fetch_one(
            """
            SELECT 1 AS claim_is_live FROM draft_agents
            WHERE id = %s AND user_id = %s AND draft_uuid = %s
              AND state_revision = %s AND generation_claim_id = %s
              AND generation_claim_expires_at > clock_timestamp()
              AND status = 'generating' AND published_revision_id IS NULL
            """,
            (
                draft.draft_id,
                draft.owner_id,
                draft.draft_uuid,
                draft.state_revision,
                draft.generation_claim_id,
            ),
        )
        if row is None:
            raise RepositoryConflictError("draft generation claim fence is stale")


def _validated_manifest(
    manifest: Mapping[str, Any],
    *,
    target_agent_id: str,
    target_revision_id: str,
    artifact_digest: str,
    runtime_contract_version: int,
    release_lock_digest: str,
) -> str:
    if not isinstance(manifest, Mapping):
        raise RepositoryValidationError("manifest must be a JSON object")
    canonical = _canonical_json(manifest, "manifest")
    canonical_generated_agent_manifest_digest(manifest)
    required_fields = {
        "manifest_version",
        "runtime_contract_version",
        "required_runtime_lock_sha256",
        "digest_algorithm",
        "bundle_sha256",
        "files",
        GENERATED_AGENT_BUNDLE_CONTRACT.scope_identity_field,
        GENERATED_AGENT_BUNDLE_CONTRACT.revision_identity_field,
        *GENERATED_AGENT_BUNDLE_CONTRACT.required_text_metadata_fields,
    }
    if set(manifest) != required_fields:
        raise RepositoryValidationError("manifest v2 fields are invalid")
    if type(manifest.get("manifest_version")) is not int or manifest.get("manifest_version") != 2:
        raise RepositoryValidationError("manifest version must be exactly 2")
    if manifest.get("agent_id") != target_agent_id:
        raise RepositoryValidationError("manifest agent_id does not match target")
    if manifest.get("revision_id") != target_revision_id:
        raise RepositoryValidationError("manifest revision_id does not match target")
    for field in ("agent_name", "description"):
        if not isinstance(manifest.get(field), str):
            raise RepositoryValidationError(f"manifest {field} must be text")
    if not isinstance(manifest.get("constitution_version"), str) or not manifest.get(
        "constitution_version"
    ):
        raise RepositoryValidationError("manifest constitution_version must be present")
    if manifest.get("bundle_sha256") != artifact_digest:
        raise RepositoryValidationError("manifest bundle digest does not match artifact")
    if manifest.get("digest_algorithm") != "sha256":
        raise RepositoryValidationError("manifest digest algorithm must be sha256")
    if (
        type(manifest.get("runtime_contract_version")) is not int
        or manifest.get("runtime_contract_version") != runtime_contract_version
    ):
        raise RepositoryValidationError("manifest runtime contract does not match revision")
    if manifest.get("required_runtime_lock_sha256") != release_lock_digest:
        raise RepositoryValidationError("manifest runtime lock does not match revision")
    manifest_files = manifest.get("files")
    if (
        not isinstance(manifest_files, Sequence)
        or isinstance(manifest_files, (str, bytes, bytearray))
        or tuple(item.get("name") if isinstance(item, Mapping) else None for item in manifest_files)
        != GENERATED_AGENT_BUNDLE_CONTRACT.file_names
    ):
        raise RepositoryValidationError("manifest file inventory is invalid")
    for item in manifest_files:
        if not isinstance(item, Mapping) or set(item) != {
            "name",
            "sha256",
            "size_bytes",
        }:
            raise RepositoryValidationError("manifest file record shape is invalid")
        _digest(item.get("sha256"), "manifest file sha256")
        size_bytes = item.get("size_bytes")
        if (
            type(size_bytes) is not int
            or size_bytes < 0
            or size_bytes > GENERATED_AGENT_BUNDLE_CONTRACT.max_file_bytes
        ):
            raise RepositoryValidationError("manifest file size is invalid")
    return canonical


def _publication_input_digest(
    *,
    owner_id: str,
    publication_id: str,
    draft_uuid: str,
    source_state_revision: int,
    generation_claim_id: str,
    target_agent_id: str,
    target_revision_id: str,
    staging_relative_path: str,
    revision_relative_path: str,
    artifact_digest: str,
    manifest_digest: str,
    runtime_contract_version: int,
    release_lock_digest: str,
    promotion_token: str,
    compatibility_state: str,
) -> str:
    canonical = _canonical_json(
        {
            "schema": "astralplane.generated-agent-publication.v1",
            "owner_id": owner_id,
            "publication_id": publication_id,
            "draft_uuid": draft_uuid,
            "source_state_revision": source_state_revision,
            "generation_claim_id": generation_claim_id,
            "target_agent_id": target_agent_id,
            "target_revision_id": target_revision_id,
            "staging_relative_path": staging_relative_path,
            "revision_relative_path": revision_relative_path,
            "artifact_digest": artifact_digest,
            "manifest_digest": manifest_digest,
            "runtime_contract_version": runtime_contract_version,
            "release_lock_digest": release_lock_digest,
            "promotion_token": promotion_token,
            "compatibility_state": compatibility_state,
        },
        "publication_operation_identity",
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _publication_input_digest_from_records(
    publication: DraftPublicationRecord,
    revision: AgentRevisionRecord,
) -> str:
    if (
        revision.artifact_digest is None
        or revision.manifest is None
        or revision.runtime_contract_version is None
        or revision.release_lock_digest is None
        or revision.promotion_token is None
    ):
        raise RepositoryDataError("publication revision identity is incomplete")
    return _publication_input_digest(
        owner_id=publication.owner_id,
        publication_id=publication.publication_id,
        draft_uuid=publication.draft_uuid,
        source_state_revision=publication.source_state_revision,
        generation_claim_id=publication.generation_claim_id,
        target_agent_id=publication.target_agent_id,
        target_revision_id=publication.target_revision_id,
        staging_relative_path=publication.staging_relative_path,
        revision_relative_path=publication.revision_relative_path,
        artifact_digest=revision.artifact_digest,
        manifest_digest=canonical_generated_agent_manifest_digest(revision.manifest),
        runtime_contract_version=revision.runtime_contract_version,
        release_lock_digest=revision.release_lock_digest,
        promotion_token=revision.promotion_token,
        compatibility_state=revision.compatibility_state,
    )


def _assert_operation_owner(operation: OperationRecord, owner_id: str) -> None:
    if operation.owner_scope is not OwnerScope.USER or operation.owner_user_id != owner_id:
        raise RepositoryConflictError("publication operation owner fence is stale")


def _assert_original_operation_binding(
    operation: OperationRecord,
    required: GeneratedAgentPublicationOperationBinding,
) -> None:
    if (
        operation.operation_kind != required.operation_kind
        or operation.idempotency_namespace != required.idempotency_namespace
        or operation.idempotency_key != required.idempotency_key
        or operation.normalized_input_digest != required.normalized_input_digest
        or operation.parent_operation_id is not None
    ):
        raise RepositoryConflictError("publication operation identity is not designated")


def _assert_recovery_operation_binding(
    operation: OperationRecord,
    required: GeneratedAgentPublicationOperationBinding,
) -> None:
    if (
        operation.operation_kind != required.operation_kind
        or operation.idempotency_namespace != required.idempotency_namespace
        or operation.idempotency_key != required.idempotency_key
        or operation.normalized_input_digest != required.normalized_input_digest
        or operation.parent_operation_id != required.parent_operation_id
    ):
        raise RepositoryConflictError("publication recovery lineage is not designated")


def _assert_bound_operation_identity(
    operation: OperationRecord,
    publication: DraftPublicationRecord,
    revision: AgentRevisionRecord,
    *,
    allow_newer_generation: bool = False,
) -> None:
    _assert_operation_owner(operation, publication.owner_id)
    expected_digest = _publication_input_digest_from_records(publication, revision)
    if operation.normalized_input_digest != expected_digest:
        raise RepositoryConflictError("publication operation input digest is stale")
    if operation.operation_kind == GENERATED_AGENT_PUBLICATION_OPERATION_KIND:
        if (
            operation.idempotency_namespace != GENERATED_AGENT_PUBLICATION_IDEMPOTENCY_NAMESPACE
            or operation.idempotency_key != publication.publication_id
        ):
            raise RepositoryConflictError("publication operation identity is not designated")
    elif operation.operation_kind == GENERATED_AGENT_PUBLICATION_RECOVERY_OPERATION_KIND:
        parts = (operation.idempotency_key or "").split(":")
        if (
            operation.idempotency_namespace
            != GENERATED_AGENT_PUBLICATION_RECOVERY_IDEMPOTENCY_NAMESPACE
            or len(parts) != 3
            or parts[0] != publication.publication_id
            or not parts[1].isdigit()
            or int(parts[1]) > publication.state_revision
        ):
            raise RepositoryConflictError("publication recovery identity is not designated")
        try:
            encoded_parent = uuid.UUID(parts[2])
        except ValueError as exc:
            raise RepositoryConflictError(
                "publication recovery parent identity is invalid"
            ) from exc
        if operation.parent_operation_id != encoded_parent:
            raise RepositoryConflictError("publication recovery parent identity is stale")
    else:
        raise RepositoryConflictError("publication operation kind is not designated")
    stored_generation = publication.operation_execution_generation
    if stored_generation is None or (
        operation.execution_generation != stored_generation
        and not (allow_newer_generation and operation.execution_generation > stored_generation)
    ):
        raise RepositoryConflictError("publication operation generation is stale")


def _assert_begin_replay(
    publication: DraftPublicationRecord,
    *,
    publication_id: str,
    generation_claim_id: str,
    target_agent_id: str,
    target_revision_id: str,
    staging_relative_path: str,
    revision_relative_path: str,
) -> None:
    if (
        publication.publication_id != publication_id
        or publication.generation_claim_id != generation_claim_id
        or publication.target_agent_id != target_agent_id
        or publication.target_revision_id != target_revision_id
        or publication.staging_relative_path != staging_relative_path
        or publication.revision_relative_path != revision_relative_path
    ):
        raise RepositoryConflictError("publication source replay changed immutable identity")


def _assert_revision_intent(
    revision: AgentRevisionRecord,
    *,
    artifact_digest: str,
    manifest_json: str,
    revision_relative_path: str,
    runtime_contract_version: int,
    release_lock_digest: str,
    promotion_token: str,
    compatibility_state: str,
    require_prepared: bool,
) -> None:
    persisted_manifest = (
        None
        if revision.manifest is None
        else _canonical_json(revision.manifest, "persisted_manifest")
    )
    if (
        revision.artifact_digest != artifact_digest
        or persisted_manifest != manifest_json
        or revision.artifact_relative_path != revision_relative_path
        or revision.runtime_contract_version != runtime_contract_version
        or revision.release_lock_digest != release_lock_digest
        or revision.promotion_token != promotion_token
        or revision.compatibility_state != compatibility_state
        or (require_prepared and revision.state != "prepared")
    ):
        raise RepositoryConflictError("publication revision replay changed immutable bytes")


def _assert_terminal_or_current_draft(
    draft: DraftAgentRecord, publication: DraftPublicationRecord
) -> None:
    if publication.state in _NONTERMINAL_STATES:
        _assert_current_draft(draft, publication)
        return
    if publication.state == "published":
        if not (
            draft.published_revision_id == publication.target_revision_id
            and draft.status == "generated"
            and draft.generation_claim_id is None
            and draft.generation_claim_expires_at is None
            and draft.state_revision == publication.source_state_revision + 1
        ):
            raise RepositoryDataError("published journal and draft pointer disagree")
        return
    if publication.state == "failed" and not (
        draft.published_revision_id is None
        and draft.status == "error"
        and draft.generation_claim_id is None
        and draft.generation_claim_expires_at is None
        and draft.state_revision == publication.source_state_revision + 1
    ):
        raise RepositoryDataError("failed journal and draft terminal state disagree")


def _assert_published_draft_result(
    draft: DraftAgentRecord,
    publication: DraftPublicationRecord,
    result: GeneratedAgentPublicationResultMetadata,
) -> None:
    if (
        draft.published_revision_id != publication.target_revision_id
        or draft.error_message != result.error_message
        or draft.security_report != result.security_report
        or draft.validation_report != result.validation_report
        or draft.required_credentials != result.required_credentials
    ):
        raise RepositoryConflictError("publication replay changed generation result metadata")


def _assert_generation_result(
    draft: DraftAgentRecord,
    result: GeneratedAgentPublicationResultMetadata,
) -> None:
    if _persisted_generation_result(draft) != result:
        raise RepositoryConflictError("publication replay changed generation result metadata")


def _persisted_generation_result(
    draft: DraftAgentRecord,
) -> GeneratedAgentPublicationResultMetadata:
    try:
        return GeneratedAgentPublicationResultMetadata(
            error_message=draft.error_message,
            security_report=draft.security_report,
            validation_report=draft.validation_report,
            required_credentials=draft.required_credentials,
        )
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted generation result metadata is invalid") from exc


def _assert_failed_draft_result(
    draft: DraftAgentRecord,
    safe_error_message: str,
) -> None:
    if draft.error_message != safe_error_message:
        raise RepositoryConflictError("publication replay changed safe error message")


def _assert_current_draft(draft: DraftAgentRecord, publication: DraftPublicationRecord) -> None:
    if (
        draft.draft_uuid != publication.draft_uuid
        or draft.owner_id != publication.owner_id
        or draft.state_revision != publication.source_state_revision
        or draft.generation_claim_id != publication.generation_claim_id
        or draft.target_agent_id != publication.target_agent_id
        or draft.status != "generating"
        or draft.published_revision_id is not None
    ):
        raise RepositoryConflictError("publication draft/claim/source fence is stale")


def _assert_revision_for_publication(
    revision: AgentRevisionRecord, publication: DraftPublicationRecord
) -> None:
    state_matches = (
        (publication.state in _NONTERMINAL_STATES and revision.state == "prepared")
        or (publication.state == "failed" and revision.state == "failed")
        or (
            publication.state == "published"
            and revision.state in {"prepared", "starting", "ready", "active", "retired", "failed"}
        )
    )
    try:
        canonical_paths = generated_agent_publication_paths(
            draft_uuid=publication.draft_uuid,
            source_state_revision=publication.source_state_revision,
            publication_id=publication.publication_id,
            target_agent_id=publication.target_agent_id,
            target_revision_id=publication.target_revision_id,
        )
    except RepositoryValidationError as exc:
        raise RepositoryConflictError("publication path identity is invalid") from exc
    if (
        revision.revision_id != publication.target_revision_id
        or revision.agent_id != publication.target_agent_id
        or revision.owner_id != publication.owner_id
        or not state_matches
        or publication.staging_relative_path != canonical_paths.staging_relative_path
        or publication.revision_relative_path != canonical_paths.revision_relative_path
        or revision.artifact_relative_path != publication.revision_relative_path
        or revision.artifact_digest is None
        or revision.manifest is None
        or revision.runtime_contract_version is None
        or revision.release_lock_digest is None
        or revision.promotion_token is None
    ):
        raise RepositoryConflictError("publication target revision fence is stale")
    try:
        _validated_manifest(
            revision.manifest,
            target_agent_id=publication.target_agent_id,
            target_revision_id=publication.target_revision_id,
            artifact_digest=revision.artifact_digest,
            runtime_contract_version=revision.runtime_contract_version,
            release_lock_digest=revision.release_lock_digest,
        )
    except RepositoryValidationError as exc:
        raise RepositoryConflictError("publication persisted manifest is invalid") from exc
    if (
        publication.artifact_digest is not None
        and publication.artifact_digest != revision.artifact_digest
    ):
        raise RepositoryConflictError("publication artifact digest fence is stale")
    if publication.manifest_digest is not None and publication.manifest_digest != (
        canonical_generated_agent_manifest_digest(revision.manifest)
    ):
        raise RepositoryConflictError("publication manifest digest fence is stale")


def _assert_snapshot(current: DraftPublicationRecord, expected: DraftPublicationRecord) -> None:
    if current != expected:
        raise RepositoryConflictError("publication state revision fence is stale")


def _assert_bound_attempt(publication: DraftPublicationRecord, attempt: ExecutionFence) -> None:
    if (
        publication.operation_id != str(attempt.operation_id)
        or publication.operation_execution_generation != attempt.execution_generation
    ):
        raise RepositoryConflictError("publication operation attempt fence is stale")


def _same_publication_identity(left: DraftPublicationRecord, right: DraftPublicationRecord) -> bool:
    return (
        left.publication_id,
        left.draft_uuid,
        left.owner_id,
        left.source_state_revision,
        left.generation_claim_id,
        left.target_agent_id,
        left.target_revision_id,
        left.operation_id,
        left.operation_execution_generation,
        left.staging_relative_path,
        left.revision_relative_path,
        left.artifact_digest,
        left.manifest_digest,
    ) == (
        right.publication_id,
        right.draft_uuid,
        right.owner_id,
        right.source_state_revision,
        right.generation_claim_id,
        right.target_agent_id,
        right.target_revision_id,
        right.operation_id,
        right.operation_execution_generation,
        right.staging_relative_path,
        right.revision_relative_path,
        right.artifact_digest,
        right.manifest_digest,
    )


def _is_transition_replay(
    current: DraftPublicationRecord,
    expected: DraftPublicationRecord,
    *,
    state: str,
    artifact_digest: str | None = None,
    manifest_digest: str | None = None,
    failure_code: str | None = None,
) -> bool:
    expected_artifact = expected.artifact_digest if artifact_digest is None else artifact_digest
    expected_manifest = expected.manifest_digest if manifest_digest is None else manifest_digest
    return (
        _same_publication_identity(
            current,
            DraftPublicationRecord(
                publication_id=expected.publication_id,
                draft_uuid=expected.draft_uuid,
                owner_id=expected.owner_id,
                source_state_revision=expected.source_state_revision,
                generation_claim_id=expected.generation_claim_id,
                target_agent_id=expected.target_agent_id,
                target_revision_id=expected.target_revision_id,
                operation_id=expected.operation_id,
                operation_execution_generation=expected.operation_execution_generation,
                staging_relative_path=expected.staging_relative_path,
                revision_relative_path=expected.revision_relative_path,
                artifact_digest=expected_artifact,
                manifest_digest=expected_manifest,
                state=expected.state,
                state_revision=expected.state_revision,
                created_at=expected.created_at,
                published_at=expected.published_at,
                failed_at=expected.failed_at,
                failure_code=expected.failure_code,
            ),
        )
        and current.state == state
        and current.state_revision == expected.state_revision + 1
        and current.failure_code == failure_code
    )


def _is_rebind_replay(
    current: DraftPublicationRecord,
    expected: DraftPublicationRecord,
    new_attempt: ExecutionFence,
) -> bool:
    return (
        current.publication_id == expected.publication_id
        and current.draft_uuid == expected.draft_uuid
        and current.owner_id == expected.owner_id
        and current.source_state_revision == expected.source_state_revision
        and current.generation_claim_id == expected.generation_claim_id
        and current.target_agent_id == expected.target_agent_id
        and current.target_revision_id == expected.target_revision_id
        and current.staging_relative_path == expected.staging_relative_path
        and current.revision_relative_path == expected.revision_relative_path
        and current.artifact_digest == expected.artifact_digest
        and current.manifest_digest == expected.manifest_digest
        and current.state == expected.state
        and current.state_revision == expected.state_revision + 1
        and current.operation_id == str(new_attempt.operation_id)
        and current.operation_execution_generation == new_attempt.execution_generation
    )


def _publication_snapshot(value: object) -> DraftPublicationRecord:
    if not isinstance(value, DraftPublicationRecord):
        raise RepositoryValidationError("expected must be a DraftPublicationRecord")
    return value


def _prospective_publication(
    *,
    publication_id: str,
    draft_uuid: str,
    owner_id: str,
    source_state_revision: int,
    generation_claim_id: str,
    target_agent_id: str,
    target_revision_id: str,
    operation_id: str,
    operation_execution_generation: int,
    staging_relative_path: str,
    revision_relative_path: str,
) -> DraftPublicationRecord:
    from datetime import UTC, datetime

    return DraftPublicationRecord(
        publication_id=publication_id,
        draft_uuid=draft_uuid,
        owner_id=owner_id,
        source_state_revision=source_state_revision,
        generation_claim_id=generation_claim_id,
        target_agent_id=target_agent_id,
        target_revision_id=target_revision_id,
        operation_id=operation_id,
        operation_execution_generation=operation_execution_generation,
        staging_relative_path=staging_relative_path,
        revision_relative_path=revision_relative_path,
        artifact_digest=None,
        manifest_digest=None,
        state="claimed",
        state_revision=0,
        created_at=datetime.min.replace(tzinfo=UTC),
        published_at=None,
        failed_at=None,
        failure_code=None,
    )


def _uuid_text(value: object, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RepositoryValidationError(f"{field} must be a UUID") from exc


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RepositoryValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _relative_path(value: object, field: str) -> str:
    path = _bounded_text(value, field, maximum=2048)
    parts = path.split("/")
    if (
        "\\" in path
        or path.startswith("/")
        or _WINDOWS_DRIVE.match(path) is not None
        or any(part in {"", ".", ".."} for part in parts)
        or "/".join(parts) != path
    ):
        raise RepositoryValidationError(f"{field} must be canonical POSIX-relative")
    return path


def _compatibility_state(value: object) -> str:
    if value not in {"compatible", "incompatible"}:
        raise RepositoryValidationError("compatibility_state is invalid")
    return str(value)


def _generated_agent_bundle(value: object) -> FinalizedBundle:
    if not isinstance(value, FinalizedBundle):
        raise RepositoryValidationError("bundle must be a FinalizedBundle")
    if value.contract != GENERATED_AGENT_BUNDLE_CONTRACT:
        raise RepositoryValidationError("bundle does not use the generated-agent contract")
    return value


def _generation_result(
    value: object,
) -> GeneratedAgentPublicationResultMetadata:
    if not isinstance(value, GeneratedAgentPublicationResultMetadata):
        raise RepositoryValidationError(
            "generation_result must be GeneratedAgentPublicationResultMetadata"
        )
    return value


def _optional_result_text(
    value: object,
    field: str,
    *,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RepositoryValidationError(f"{field} must be text when supplied")
    return _bounded_text(value, field, maximum=maximum, allow_empty=True)


def _failure_code(value: object) -> str:
    if not isinstance(value, str) or _FAILURE_CODE.fullmatch(value) is None:
        raise RepositoryValidationError("failure_code must be bounded snake_case")
    return value


__all__ = (
    "GENERATED_AGENT_BUNDLE_CONTRACT",
    "GENERATED_AGENT_PUBLICATION_IDEMPOTENCY_NAMESPACE",
    "GENERATED_AGENT_PUBLICATION_OPERATION_KIND",
    "GENERATED_AGENT_PUBLICATION_RECOVERY_IDEMPOTENCY_NAMESPACE",
    "GENERATED_AGENT_PUBLICATION_RECOVERY_OPERATION_KIND",
    "GeneratedAgentPublicationIntent",
    "GeneratedAgentPublicationOperationBinding",
    "GeneratedAgentPublicationRepository",
    "GeneratedAgentPublicationResultMetadata",
    "canonical_generated_agent_manifest_digest",
    "generated_agent_publication_operation_binding",
    "generated_agent_publication_paths",
    "generated_agent_publication_recovery_operation_binding",
)
