"""Tests for src/astralplane/api.py: the public composition facade — repository
factories, blob/materialization/purge wiring, health, initialize/reconcile lifecycle,
and runtime construction.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import astralplane
import astralplane.api as public_api
from astralplane.api import (
    AsyncPlaneRuntime,
    AtlasPageAppendCommand,
    AtlasPageEditChange,
    AtlasPageEditCommand,
    AtlasPageRevisionBatch,
    AtlasPageRevisionCursor,
    AtlasPageRevisionRecord,
    AuthorityRepository,
    PlaneHealth,
    PlaneRuntime,
    RepositoryCatalog,
    create_agent_management_repository,
    create_agent_repository,
    create_artifact_repository,
    create_atlas_link_repository,
    create_atlas_page_repository,
    create_attachment_materialization_coordinator,
    create_attachment_parser_repository,
    create_audit_repository,
    create_audit_retention_repository,
    create_authority_repository,
    create_background_task_repository,
    create_capability_publication_repository,
    create_chat_step_repository,
    create_conversation_file_repository,
    create_credential_repository,
    create_draft_agent_repository,
    create_durable_purge_executor,
    create_encrypted_llm_config_repository,
    create_generated_agent_publication_repository,
    create_harness_cleanup_repository,
    create_history_repository,
    create_identity_repository,
    create_knowledge_repository,
    create_maintenance_repository,
    create_mesh_enrollment_repository,
    create_notification_repository,
    create_observation_repository,
    create_offline_grant_repository,
    create_outbox_store,
    create_personalization_graph_repository,
    create_plane_runtime,
    create_preferences_repository,
    create_project_repository,
    create_purge_store,
    create_quality_audit_repository,
    create_remote_operation_proposal_repository,
    create_remote_repository,
    create_repository_catalog,
    create_revocation_repository,
    create_saved_component_repository,
    create_scheduler_repository,
    create_share_grant_repository,
    create_streaming_blob_store,
    create_tool_policy_state_repository,
    create_tracked_job_repository,
    create_tutorial_repository,
    create_voice_repository,
    create_work_admission_repository,
    create_workspace_repository,
)
from astralplane.audit_retention import AuditRetentionRepository
from astralplane.authority import AuthorityRepository as PublicAuthorityRepository
from astralplane.blob_store import ExplicitRootStreamingBlobStore, StreamingBlobStore
from astralplane.compatibility import CONTRACT_VERSION, CompatibilityState
from astralplane.contracts import IsolationLevel, ReconciliationHookIdentity
from astralplane.database.bootstrap import BootStatus
from astralplane.database.pool import PoolSnapshot
from astralplane.database.revision import SCHEMA_REVISION
from astralplane.errors import InitializationError, PoolShutdownError
from astralplane.outbox import PostgresOutboxStore
from astralplane.purge import (
    DurablePurgeExecutor,
    PostgresPurgeStore,
    PurgeScheduleResult,
)
from astralplane.reconciliation import (
    RECONCILIATION_ADVISORY_LOCK,
    ReconciliationHookReport,
    ReconciliationReport,
)
from astralplane.repositories.agent_management import AgentManagementRepository
from astralplane.repositories.agents import AgentRepository
from astralplane.repositories.artifacts import (
    ArtifactRepository,
    AttachmentMaterializationCoordinator,
    AttachmentRepository,
    BlobMetadataRepository,
    MaterializationRepository,
)
from astralplane.repositories.atlas_links import AtlasLinkRepository
from astralplane.repositories.attachment_parsers import AttachmentParserRepository
from astralplane.repositories.audit import AuditRepository
from astralplane.repositories.background_tasks import BackgroundTaskRepository
from astralplane.repositories.capability_publications import CapabilityPublicationRepository
from astralplane.repositories.chat_steps import ChatStepRepository
from astralplane.repositories.conversation_files import ConversationFileRepository
from astralplane.repositories.credentials import CredentialRepository
from astralplane.repositories.drafts import DraftAgentRepository
from astralplane.repositories.generated_agent_publications import (
    GeneratedAgentPublicationRepository,
)
from astralplane.repositories.harness_cleanup import HarnessCleanupRepository
from astralplane.repositories.history import HistoryRepository
from astralplane.repositories.identity import IdentityRepository
from astralplane.repositories.knowledge import KnowledgeRepository
from astralplane.repositories.maintenance import MaintenanceRepository
from astralplane.repositories.mesh_enrollment import MeshEnrollmentRepository
from astralplane.repositories.notifications import NotificationRepository
from astralplane.repositories.observations import ObservationRepository
from astralplane.repositories.offline_grants import OfflineGrantRepository
from astralplane.repositories.personalization_graph import PersonalizationGraphRepository
from astralplane.repositories.preferences import PreferencesRepository
from astralplane.repositories.projects import ProjectRepository
from astralplane.repositories.quality_audit import QualityAuditRepository
from astralplane.repositories.remote import RemoteRepository
from astralplane.repositories.remote_proposals import RemoteOperationProposalRepository
from astralplane.repositories.revocations import RevocationQueueRepository
from astralplane.repositories.saved_components import SavedComponentRepository
from astralplane.repositories.scheduler import SchedulerRepository
from astralplane.repositories.secrets import EncryptedLLMConfigRepository
from astralplane.repositories.share_grants import ShareGrantRepository
from astralplane.repositories.tool_policy import ToolPolicyStateRepository
from astralplane.repositories.tracked_jobs import TrackedJobRepository
from astralplane.repositories.tutorials import TutorialRepository
from astralplane.repositories.voice import VoiceRepository
from astralplane.repositories.work_admission import WorkAdmissionRepository
from astralplane.repositories.workspaces import WorkspaceRepository

_DIGEST = "a" * 64


class StubPool:
    def __init__(self) -> None:
        self.closed = False
        self.cleanup_failed = False
        self.fail_close = False

    @property
    def snapshot(self) -> PoolSnapshot:
        return PoolSnapshot(
            borrowed=0,
            closed=self.closed,
            cleanup_complete=self.closed and not self.cleanup_failed,
            cleanup_failed=self.cleanup_failed,
            cleanup_attempts=1 if self.closed else 0,
        )

    def close(self) -> None:
        if self.fail_close:
            raise PoolShutdownError(
                "driver pool cleanup failed; admission stays closed and a later"
                " close call retries cleanup once per call",
                metadata={"attempts": 1},
            )
        self.closed = True


class StubDatabase:
    def __init__(self) -> None:
        self.isolations: list[IsolationLevel | None] = []

    @contextmanager
    def transaction(self, *, isolation: IsolationLevel | None = None) -> Iterator[object]:
        self.isolations.append(isolation)
        yield "transaction"


class StubInitializer:
    def __init__(self, *, status: BootStatus = BootStatus.NEW) -> None:
        self.status = status
        self.expected: list[str] = []

    def initialize(self, *, expected_revision: str) -> object:
        self.expected.append(expected_revision)
        self.status = BootStatus.READY
        return "initialized"


class StubReconciler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def run(self, *, schema_revision: str, context: Mapping[str, object]) -> ReconciliationReport:
        self.calls.append((schema_revision, context))
        return ReconciliationReport(
            schema_revision=schema_revision,
            plan_digest=_DIGEST,
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            hooks=(
                ReconciliationHookReport(
                    hook=ReconciliationHookIdentity(name="hook", version="v1"),
                    attempt=1,
                    already_complete=False,
                    result_digest=_DIGEST,
                ),
            ),
            durably_complete=True,
        )


def _runtime(
    *,
    status: BootStatus = BootStatus.NEW,
    expected_contract_version: str = CONTRACT_VERSION,
) -> tuple[PlaneRuntime, StubPool, StubDatabase, StubInitializer, StubReconciler]:
    pool = StubPool()
    database = StubDatabase()
    initializer = StubInitializer(status=status)
    reconciler = StubReconciler()
    runtime = PlaneRuntime(
        pool=pool,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        initializer=initializer,  # type: ignore[arg-type]
        reconciler=reconciler,  # type: ignore[arg-type]
        expected_contract_version=expected_contract_version,
    )
    return runtime, pool, database, initializer, reconciler


def test_explicit_repository_factories_return_the_declared_types() -> None:
    expected = (
        (create_identity_repository, IdentityRepository),
        (create_agent_management_repository, AgentManagementRepository),
        (create_agent_repository, AgentRepository),
        (create_draft_agent_repository, DraftAgentRepository),
        (
            create_generated_agent_publication_repository,
            GeneratedAgentPublicationRepository,
        ),
        (create_tool_policy_state_repository, ToolPolicyStateRepository),
        (create_credential_repository, CredentialRepository),
        (create_offline_grant_repository, OfflineGrantRepository),
        (create_share_grant_repository, ShareGrantRepository),
        (create_chat_step_repository, ChatStepRepository),
        (create_conversation_file_repository, ConversationFileRepository),
        (create_saved_component_repository, SavedComponentRepository),
        (create_history_repository, HistoryRepository),
        (create_workspace_repository, WorkspaceRepository),
        (create_atlas_link_repository, AtlasLinkRepository),
        (create_artifact_repository, ArtifactRepository),
        (create_attachment_parser_repository, AttachmentParserRepository),
        (create_preferences_repository, PreferencesRepository),
        (create_knowledge_repository, KnowledgeRepository),
        (create_personalization_graph_repository, PersonalizationGraphRepository),
        (create_scheduler_repository, SchedulerRepository),
        (create_background_task_repository, BackgroundTaskRepository),
        (create_capability_publication_repository, CapabilityPublicationRepository),
        (create_work_admission_repository, WorkAdmissionRepository),
        (create_maintenance_repository, MaintenanceRepository),
        (create_mesh_enrollment_repository, MeshEnrollmentRepository),
        (create_notification_repository, NotificationRepository),
        (create_project_repository, ProjectRepository),
        (create_observation_repository, ObservationRepository),
        (create_tracked_job_repository, TrackedJobRepository),
        (create_quality_audit_repository, QualityAuditRepository),
        (create_harness_cleanup_repository, HarnessCleanupRepository),
        (create_tutorial_repository, TutorialRepository),
        (create_voice_repository, VoiceRepository),
        (create_remote_repository, RemoteRepository),
        (create_remote_operation_proposal_repository, RemoteOperationProposalRepository),
        (create_revocation_repository, RevocationQueueRepository),
        (create_encrypted_llm_config_repository, EncryptedLLMConfigRepository),
        (create_audit_repository, AuditRepository),
        (create_audit_retention_repository, AuditRetentionRepository),
        (create_authority_repository, AuthorityRepository),
        (create_outbox_store, PostgresOutboxStore),
        (create_purge_store, PostgresPurgeStore),
    )

    for factory, repository_type in expected:
        assert isinstance(factory(), repository_type)


def test_public_draft_creation_api_exposes_initial_provenance_fields() -> None:
    parameters = inspect.signature(create_draft_agent_repository().create_draft).parameters

    assert parameters["plan_json"].default is None
    assert parameters["constitution_version"].default is None


def test_repository_catalog_is_complete_immutable_and_fresh() -> None:
    first = create_repository_catalog()
    second = create_repository_catalog()

    assert isinstance(first, RepositoryCatalog)
    assert tuple(first.as_mapping()) == (
        "assignments",
        "agent_management",
        "agents",
        "artifacts",
        "attachment_parsers",
        "audit",
        "audit_retention",
        "authority",
        "background_tasks",
        "chat_queue",
        "chat_read_receipts",
        "chat_steps",
        "derived_chat",
        "event_log",
        "event_roster",
        "learning_ledger",
        "runs",
        "conversation_files",
        "credentials",
        "draft_agents",
        "generated_agent_publications",
        "encrypted_llm_config",
        "encrypted_typesafe_credential",
        "framework_credentials",
        "history",
        "harness_cleanup",
        "identity",
        "knowledge",
        "maintenance",
        "mesh_enrollment",
        "verification_credentials",
        "notifications",
        "push_deliveries",
        "projects",
        "observations",
        "offline_grants",
        "outbox",
        "preferences",
        "personalization_graph",
        "purge",
        "quality_audit",
        "remote",
        "remote_operation_proposals",
        "revocations",
        "saved_components",
        "scheduler",
        "share_grants",
        "stop_epochs",
        "tool_policy_state",
        "tracked_jobs",
        "tutorials",
        "voice",
        "work_admission",
        "workspaces",
        "completion_wake",
        "computer_use",
        "capability_publications",
        "atlas_pages",
        "atlas_links",
    )
    assert first.history is not second.history
    assert first.generated_agent_publications is not second.generated_agent_publications
    with pytest.raises(TypeError):
        first.as_mapping()["history"] = second.history  # type: ignore[index]


def test_configured_blob_contract_exposes_reads_and_fenced_staging_only(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "durable-blobs").resolve()
    blobs = create_streaming_blob_store(root=root)

    assert isinstance(blobs, StreamingBlobStore)
    assert isinstance(blobs, ExplicitRootStreamingBlobStore)
    for forbidden in (
        "put",
        "get",
        "write_chunks",
        "awrite_chunks",
        "stage_chunks",
        "astage_chunks",
        "delete_key",
        "delete_prefix",
        "delete_owner",
    ):
        assert not hasattr(StreamingBlobStore, forbidden)
        assert not hasattr(blobs, forbidden)
    blobs.close()


def test_durable_purge_factory_accepts_the_one_configured_streaming_store(
    tmp_path: Path,
) -> None:
    blobs = create_streaming_blob_store(root=(tmp_path / "streaming-blobs").resolve())
    purge_store = create_purge_store()
    runtime, _, _, _, _ = _runtime(status=BootStatus.READY)

    executor = create_durable_purge_executor(
        database=runtime,
        purge_store=purge_store,
        blobs=blobs,
    )

    assert isinstance(blobs, StreamingBlobStore)
    assert isinstance(executor, DurablePurgeExecutor)
    assert astralplane.DurablePurgeExecutor is DurablePurgeExecutor
    assert astralplane.PurgeScheduleResult is PurgeScheduleResult
    blobs.close()


def test_materialization_coordinator_factory_binds_runtime_catalog_and_blob_root(
    tmp_path: Path,
) -> None:
    blobs = create_streaming_blob_store(root=(tmp_path / "streaming-blobs").resolve())
    runtime, _, _, _, _ = _runtime(status=BootStatus.READY)
    catalog = create_repository_catalog()

    coordinator = create_attachment_materialization_coordinator(
        database=runtime,
        materializations=catalog.artifacts.materializations,
        blobs=blobs,
    )

    assert isinstance(coordinator, AttachmentMaterializationCoordinator)
    assert astralplane.AttachmentMaterializationCoordinator is (
        AttachmentMaterializationCoordinator
    )
    coordinator.close()
    blobs.close()


def test_public_attachment_surfaces_cannot_bypass_materialization_or_purge() -> None:
    for repository, forbidden in (
        (MaterializationRepository, ("register",)),
        (AttachmentRepository, ("soft_delete", "soft_delete_all")),
        (BlobMetadataRepository, ("relocate",)),
        (
            PostgresPurgeStore,
            ("enqueue", "mark_purged", "mark_failed", "mark_manual_review"),
        ),
    ):
        for name in forbidden:
            assert not hasattr(repository, name)

    for name in (
        "put",
        "write_chunks",
        "awrite_chunks",
        "stage_chunks",
        "astage_chunks",
        "delete_key",
        "delete_prefix",
        "delete_owner",
    ):
        assert not hasattr(ExplicitRootStreamingBlobStore, name)


def test_health_reports_readiness_and_detached_shape() -> None:
    runtime, pool, _, _, _ = _runtime(status=BootStatus.READY)

    health = runtime.health()

    assert isinstance(health, PlaneHealth)
    assert health.ready
    assert health.to_dict()["ready"] is True
    pool.close()
    assert not runtime.health().ready


def test_runtime_close_propagates_the_typed_shutdown_failure() -> None:
    runtime, pool, _, _, _ = _runtime(status=BootStatus.READY)
    pool.fail_close = True

    with pytest.raises(PoolShutdownError) as error:
        runtime.close()

    assert error.value.code == "pool_shutdown_failed"
    assert error.value.metadata == (("attempts", "1"),)


def test_health_reports_pool_cleanup_state_truthfully() -> None:
    runtime, pool, _, _, _ = _runtime(status=BootStatus.READY)

    assert runtime.health().to_dict()["pool"] == {
        "borrowed": 0,
        "closed": False,
        "cleanup_complete": False,
        "cleanup_failed": False,
        "cleanup_attempts": 0,
    }

    pool.closed = True
    pool.cleanup_failed = True
    reported = runtime.health().to_dict()["pool"]
    assert reported["closed"] is True
    assert reported["cleanup_complete"] is False
    assert reported["cleanup_failed"] is True
    assert reported["cleanup_attempts"] == 1
    assert not runtime.health().ready


def test_initialize_rejects_incompatible_composition_before_runner() -> None:
    runtime, _, _, initializer, _ = _runtime(expected_contract_version="other/v1")

    report = runtime.inspect_compatibility()
    assert report.state is CompatibilityState.INCOMPATIBLE
    with pytest.raises(InitializationError) as caught:
        runtime.initialize()

    assert caught.value.metadata == (("reasons", "contract_version_mismatch"),)
    assert not initializer.expected


def test_initialize_delegates_exact_revision_and_unlocks_transactions() -> None:
    runtime, _, database, initializer, _ = _runtime()

    with pytest.raises(InitializationError), runtime.transaction():
        pytest.fail("an uninitialized runtime exposed a transaction")

    assert runtime.initialize(expected_revision="067.001") == "initialized"
    assert initializer.expected == ["067.001"]
    with runtime.transaction(isolation=IsolationLevel.SERIALIZABLE) as transaction:
        assert transaction == "transaction"
    assert database.isolations == [IsolationLevel.SERIALIZABLE]


def test_reconcile_uses_pinned_runner_only_after_ready() -> None:
    runtime, _, _, _, reconciler = _runtime()
    with pytest.raises(InitializationError):
        runtime.reconcile(context={})

    runtime.initialize()
    result = runtime.reconcile(context={"tenant": "opaque"})

    assert result.durably_complete
    assert reconciler.calls == [(SCHEMA_REVISION, {"tenant": "opaque"})]


def test_close_is_idempotent_and_keeps_runtime_closed() -> None:
    runtime, pool, _, _, _ = _runtime(status=BootStatus.READY)

    runtime.close()
    runtime.close()

    assert pool.closed
    with pytest.raises(InitializationError), runtime.transaction():
        pytest.fail("a closed runtime exposed a transaction")


class DriverPoolStub:
    def getconn(self) -> Any:
        raise AssertionError("construction must not borrow a connection")

    def putconn(self, connection: Any, *, close: bool = False) -> None:
        raise AssertionError((connection, close))

    def closeall(self) -> None:
        pass


class CoordinatorStub:
    def coordinate(self, **_: object) -> Any:
        raise AssertionError("construction must not coordinate reconciliation")


@dataclass(frozen=True)
class HookStub:
    name: str = "bootstrap"
    version: str = "v1"

    def reconcile(self, context: Mapping[str, object]) -> Mapping[str, object]:
        return context


def test_create_plane_runtime_is_inert_and_uses_supplied_catalog() -> None:
    catalog = create_repository_catalog()

    runtime = create_plane_runtime(
        DriverPoolStub(),
        identity="plane-api-test-factory",
        coordinator=CoordinatorStub(),  # type: ignore[arg-type]
        reconcilers=(HookStub(),),
        repositories=catalog,
    )

    assert runtime.repositories is catalog
    assert runtime.health().boot_status is BootStatus.NEW
    assert runtime.inspect_compatibility().compatible
    runtime.close()


def test_create_plane_runtime_supplies_the_durable_postgres_coordinator() -> None:
    runtime = create_plane_runtime(
        DriverPoolStub(),
        identity="plane-api-test-default-coordinator",
        reconcilers=(HookStub(),),
    )

    assert runtime.health().boot_status is BootStatus.NEW
    assert runtime._reconciler._coordinator.__class__.__name__ == (
        "PostgresReconciliationCoordinator"
    )
    runtime.close()


def test_package_root_exports_the_stable_composition_facade() -> None:
    import astralplane

    assert astralplane.PlaneRuntime is PlaneRuntime
    assert astralplane.RepositoryCatalog is RepositoryCatalog
    assert astralplane.AsyncPlaneRuntime is AsyncPlaneRuntime
    assert astralplane.AuthorityRepository is PublicAuthorityRepository
    assert astralplane.create_authority_repository is create_authority_repository
    assert astralplane.create_atlas_page_repository is create_atlas_page_repository
    assert astralplane.AtlasPageAppendCommand is AtlasPageAppendCommand
    assert astralplane.AtlasPageEditChange is AtlasPageEditChange
    assert astralplane.AtlasPageEditCommand is AtlasPageEditCommand
    assert astralplane.AtlasPageRevisionBatch is AtlasPageRevisionBatch
    assert astralplane.AtlasPageRevisionCursor is AtlasPageRevisionCursor
    assert astralplane.AtlasPageRevisionRecord is AtlasPageRevisionRecord
    assert astralplane.create_atlas_link_repository is create_atlas_link_repository
    assert astralplane.create_observation_repository is create_observation_repository
    assert astralplane.create_attachment_parser_repository is create_attachment_parser_repository
    assert not hasattr(astralplane, "create_blob_store_contract")
    assert astralplane.create_background_task_repository is create_background_task_repository
    assert astralplane.create_work_admission_repository is create_work_admission_repository
    assert astralplane.create_identity_repository is create_identity_repository
    assert astralplane.create_knowledge_repository is create_knowledge_repository
    assert astralplane.create_maintenance_repository is create_maintenance_repository
    assert astralplane.create_harness_cleanup_repository is create_harness_cleanup_repository
    assert (
        astralplane.create_personalization_graph_repository
        is create_personalization_graph_repository
    )
    assert astralplane.create_credential_repository is create_credential_repository
    assert astralplane.create_offline_grant_repository is create_offline_grant_repository
    assert astralplane.create_share_grant_repository is create_share_grant_repository
    assert astralplane.create_chat_step_repository is create_chat_step_repository
    assert astralplane.create_conversation_file_repository is create_conversation_file_repository
    assert astralplane.create_saved_component_repository is create_saved_component_repository
    assert astralplane.create_agent_repository is create_agent_repository
    assert astralplane.create_draft_agent_repository is create_draft_agent_repository
    assert (
        astralplane.create_generated_agent_publication_repository
        is create_generated_agent_publication_repository
    )
    assert astralplane.GeneratedAgentPublicationRepository is GeneratedAgentPublicationRepository
    assert hasattr(astralplane, "GeneratedAgentPublicationIntent")
    assert callable(astralplane.canonical_generated_agent_manifest_digest)
    for public_name in (
        "GENERATED_AGENT_BUNDLE_CONTRACT",
        "GENERATED_AGENT_PUBLICATION_IDEMPOTENCY_NAMESPACE",
        "GENERATED_AGENT_PUBLICATION_OPERATION_KIND",
        "GENERATED_AGENT_PUBLICATION_RECOVERY_IDEMPOTENCY_NAMESPACE",
        "GENERATED_AGENT_PUBLICATION_RECOVERY_OPERATION_KIND",
        "BundlePublicationKey",
        "BundlePublicationPaths",
        "BundlePublicationReceipt",
        "BundleRecoveryDisposition",
        "BundleRecoveryResult",
        "DraftAgentRecord",
        "DraftPublicationRecord",
        "AgentRevisionRecord",
        "ExecutionFence",
        "FinalizedBundle",
        "GeneratedAgentPublicationOperationBinding",
        "GeneratedAgentPublicationResultMetadata",
        "ImmutableBundleContract",
        "ImmutableBundleStore",
        "OperationOwner",
        "OperationRecord",
        "OperationRequest",
        "OperationState",
        "OwnerScope",
        "PublishedBundle",
        "StagedBundleReceipt",
        "ArtifactCollisionError",
        "ArtifactIntegrityError",
        "ArtifactPublicationError",
        "ArtifactPublicationRevokedError",
        "ArtifactReconciliationError",
        "canonical_bundle_digest",
        "generated_agent_publication_operation_binding",
        "generated_agent_publication_paths",
        "generated_agent_publication_recovery_operation_binding",
        "paths_for",
        "runtime_metadata_for_manifest",
    ):
        assert getattr(astralplane, public_name) is getattr(public_api, public_name)
        assert public_name in astralplane.__all__
        assert public_name in public_api.__all__
    assert "create_publication" not in astralplane.__all__
    assert "transition_publication" not in astralplane.__all__
    assert astralplane.create_tool_policy_state_repository is create_tool_policy_state_repository
    assert astralplane.create_tracked_job_repository is create_tracked_job_repository
    assert astralplane.create_tutorial_repository is create_tutorial_repository
    assert astralplane.create_quality_audit_repository is create_quality_audit_repository
    assert (
        astralplane.create_remote_operation_proposal_repository
        is create_remote_operation_proposal_repository
    )
    assert astralplane.create_plane_runtime is create_plane_runtime
    assert astralplane.create_repository_catalog is create_repository_catalog
