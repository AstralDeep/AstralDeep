"""Composition facade exposing factory functions for every AstralPlane repository plus
PlaneRuntime's initialize/reconcile/transaction/close lifecycle; owns resource
composition only, never authorization policy.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from astralplane.async_runtime import AsyncPlaneRuntime
from astralplane.audit_retention import AuditRetentionRepository
from astralplane.authority import (
    AuthorityCompareAndSetConflictError,
    AuthorityIdempotencyConflictError,
    AuthorityRepository,
    ReceiptClaimConflictError,
    ReceiptWatermarkConflictError,
    create_authority_repository,
)
from astralplane.blob_store import ExplicitRootStreamingBlobStore, StreamingBlobStore
from astralplane.compatibility import (
    CONTRACT_VERSION,
    PACKAGE_VERSION,
    CompatibilityReport,
    inspect_compatibility,
)
from astralplane.contracts import (
    IsolationLevel,
    ProductReconciler,
    ReconciliationCoordinator,
    Transaction,
)
from astralplane.contracts import (
    PlaneDatabase as PlaneDatabaseContract,
)
from astralplane.database.baseline import (
    BaselineCompatibilityReport,
    BaselineCompatibilityState,
    BaselineInitializationReport,
    BaselineMigrationRunner,
    initialize_empty_database,
    inspect_baseline_compatibility,
)
from astralplane.database.bootstrap import BootInitializer, BootStatus, InitializationReport
from astralplane.database.dialect import POSTGRES, SQLITE, Dialect
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_REGISTRY,
    MigrationRunner,
)
from astralplane.database.pool import ConnectionPool, DriverPool, PoolSnapshot
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.revision import SCHEMA_REVISION, DataPlaneRevision
from astralplane.database.sqlite import create_sqlite_driver_pool, is_sqlite_url
from astralplane.database.sqlite_migrations import (
    SQLITE_DATA_PLANE_REVISION,
    SQLITE_MIGRATION_REGISTRY,
    SQLiteBootRunner,
)
from astralplane.database.sqlite_reconciliation import SQLiteReconciliationCoordinator
from astralplane.database.transaction import PlaneDatabase
from astralplane.endpoint_readiness import (
    ENDPOINT_READINESS_CONTRACT_VERSION,
    EndpointFence,
    EndpointFenceError,
    EndpointReadiness,
    EndpointReadinessError,
    EndpointReadinessState,
    EndpointTopology,
    inspect_endpoint_readiness,
)
from astralplane.errors import InitializationError, LockContentionError, is_lock_contention
from astralplane.immutable_bundle_store import (
    ArtifactCollisionError,
    ArtifactIntegrityError,
    ArtifactPublicationError,
    ArtifactPublicationRevokedError,
    ArtifactReconciliationError,
    BundlePublicationKey,
    BundlePublicationPaths,
    BundlePublicationReceipt,
    BundleRecoveryDisposition,
    BundleRecoveryResult,
    FinalizedBundle,
    ImmutableBundleContract,
    ImmutableBundleStore,
    PublishedBundle,
    StagedBundleReceipt,
    canonical_bundle_digest,
    paths_for,
    runtime_metadata_for_manifest,
)
from astralplane.joint_restore import (
    JOINT_RESTORE_CONTRACT_VERSION,
    JointRestoreCompletion,
    JointRestoreError,
    JointRestoreEvidence,
    RestoreBlobEntry,
    qualify_joint_restore,
    recover_joint_restore,
)
from astralplane.outbox import PostgresOutboxStore
from astralplane.purge import (
    DurablePurgeExecutor,
    PostgresPurgeStore,
)
from astralplane.reconciliation import ReconciliationReport, ReconciliationRunner
from astralplane.reconciliation_store import PostgresReconciliationCoordinator
from astralplane.recovery import (
    RestoredSessionRetirement,
    SessionRetirementError,
    retire_restored_sessions,
)
from astralplane.repositories.agent_management import AgentManagementRepository
from astralplane.repositories.agents import AgentRepository, AgentRevisionRecord
from astralplane.repositories.artifacts import (
    ArtifactRepository,
    AttachmentMaterializationCoordinator,
    MaterializationRepository,
)
from astralplane.repositories.assignments import AssignmentRepository
from astralplane.repositories.atlas_links import (
    AtlasLinkBatch,
    AtlasLinkChange,
    AtlasLinkCommandReceipt,
    AtlasLinkCursor,
    AtlasLinkRecord,
    AtlasLinkRepository,
    AtlasLinkSetCommand,
)
from astralplane.repositories.atlas_pages import (
    AtlasPageAppendChange,
    AtlasPageAppendCommand,
    AtlasPageBatch,
    AtlasPageChange,
    AtlasPageCommandReceipt,
    AtlasPageCreateCommand,
    AtlasPageCursor,
    AtlasPageDeleteCommand,
    AtlasPageEditChange,
    AtlasPageEditCommand,
    AtlasPageRecord,
    AtlasPageRepository,
    AtlasPageRevisionBatch,
    AtlasPageRevisionCursor,
    AtlasPageRevisionRecord,
)
from astralplane.repositories.attachment_parsers import AttachmentParserRepository
from astralplane.repositories.audit import AuditRepository
from astralplane.repositories.background_tasks import BackgroundTaskRepository
from astralplane.repositories.capability_publications import (
    CapabilityPublicationConflictError,
    CapabilityPublicationNotExpiredError,
    CapabilityPublicationRecord,
    CapabilityPublicationRepository,
    CapabilityPublicationState,
)
from astralplane.repositories.chat_queue import (
    ChatQueueRepository,
    QueuedFollowUpRecord,
)
from astralplane.repositories.chat_read_receipts import (
    ChatReadReceiptRecord,
    ChatReadReceiptRepository,
    ChatReadStateRecord,
)
from astralplane.repositories.chat_steps import ChatStepRepository
from astralplane.repositories.completion_wake import CompletionWakeRepository
from astralplane.repositories.computer_use import (
    ApprovalFence,
    ComputerSession,
    ComputerTaskGrant,
    ComputerUseRepository,
    HostEffectIntent,
    HostEffectIntentState,
)
from astralplane.repositories.conversation_files import ConversationFileRepository
from astralplane.repositories.credentials import CredentialRepository
from astralplane.repositories.derived_chat import (
    DerivedChatRecord,
    DerivedChatRepository,
    DerivedMessageRecord,
)
from astralplane.repositories.drafts import (
    DraftAgentRecord,
    DraftAgentRepository,
    DraftPublicationRecord,
)
from astralplane.repositories.framework_credentials import FrameworkCredentialRepository
from astralplane.repositories.generated_agent_publications import (
    GENERATED_AGENT_BUNDLE_CONTRACT,
    GENERATED_AGENT_PUBLICATION_IDEMPOTENCY_NAMESPACE,
    GENERATED_AGENT_PUBLICATION_OPERATION_KIND,
    GENERATED_AGENT_PUBLICATION_RECOVERY_IDEMPOTENCY_NAMESPACE,
    GENERATED_AGENT_PUBLICATION_RECOVERY_OPERATION_KIND,
    GeneratedAgentPublicationIntent,
    GeneratedAgentPublicationOperationBinding,
    GeneratedAgentPublicationRepository,
    GeneratedAgentPublicationResultMetadata,
    canonical_generated_agent_manifest_digest,
    generated_agent_publication_operation_binding,
    generated_agent_publication_paths,
    generated_agent_publication_recovery_operation_binding,
)
from astralplane.repositories.harness_bindings import (
    HarnessProfileIdentity,
    OperationHarnessBinding,
)
from astralplane.repositories.harness_cleanup import HarnessCleanupRepository
from astralplane.repositories.history import HistoryRepository
from astralplane.repositories.identity import IdentityRepository
from astralplane.repositories.knowledge import KnowledgeRepository
from astralplane.repositories.learning_ledger import (
    LearningItemRecord,
    LearningLedgerRepository,
    LedgerEntryRecord,
)
from astralplane.repositories.maintenance import MaintenanceRepository
from astralplane.repositories.mesh_enrollment import MeshEnrollmentRepository
from astralplane.repositories.mesh_events import (
    EventAppendResult,
    EventBodyRecord,
    EventCursor,
    EventFeedRecord,
    EventLogRepository,
    EventMeshRecord,
    EventRecord,
    ForkEvidenceRecord,
)
from astralplane.repositories.mesh_roster import (
    CheckpointContradiction,
    CheckpointRecord,
    CountersignatureRecord,
    EventRosterRepository,
    FeedBehindCheckpoint,
    RosterEntryRecord,
)
from astralplane.repositories.notifications import (
    NotificationDeliveryIdentity,
    NotificationEventKind,
    NotificationInboxEntry,
    NotificationPreferenceRecord,
    NotificationRepository,
)
from astralplane.repositories.observations import (
    ObservationBatch,
    ObservationCapture,
    ObservationCaptureCommand,
    ObservationCursor,
    ObservationHead,
    ObservationPage,
    ObservationPurge,
    ObservationRepository,
    ObservationUsage,
)
from astralplane.repositories.offline_grants import OfflineGrantRepository
from astralplane.repositories.personalization_graph import PersonalizationGraphRepository
from astralplane.repositories.preferences import PreferencesRepository
from astralplane.repositories.projects import (
    ProjectChatCursor,
    ProjectChatPage,
    ProjectChatRecord,
    ProjectCursor,
    ProjectPage,
    ProjectRecord,
    ProjectRepository,
)
from astralplane.repositories.push_deliveries import (
    ClaimedPushDelivery,
    PushDeliveryRecord,
    PushDeliveryRepository,
    PushDestinationRecord,
)
from astralplane.repositories.quality_audit import QualityAuditRepository
from astralplane.repositories.remote import RemoteRepository
from astralplane.repositories.remote_proposals import RemoteOperationProposalRepository
from astralplane.repositories.revocations import RevocationQueueRepository
from astralplane.repositories.runs import RunGoalRecord, RunLeaseRecord, RunRepository
from astralplane.repositories.saved_components import SavedComponentRepository
from astralplane.repositories.scheduler import SchedulerRepository
from astralplane.repositories.secrets import (
    EncryptedLLMConfigRepository,
    EncryptedTypeSafeCredentialRepository,
)
from astralplane.repositories.share_grants import ShareGrantRepository
from astralplane.repositories.stop_epochs import (
    OwnerStoppedError,
    OwnerStopRecord,
    PeerStopAcknowledgment,
    StopEpochConflictError,
    StopEpochRepository,
)
from astralplane.repositories.tool_policy import ToolPolicyStateRepository
from astralplane.repositories.tracked_jobs import TrackedJobRepository
from astralplane.repositories.tutorials import TutorialRepository
from astralplane.repositories.verification_credentials import (
    VerificationCredentialRepository,
)
from astralplane.repositories.voice import VoiceRepository
from astralplane.repositories.work_admission import (
    AdmissionClass,
    ExecutionFence,
    OperationOwner,
    OperationRecord,
    OperationRequest,
    OperationState,
    OwnerScope,
    WorkAdmissionRepository,
)
from astralplane.repositories.workspaces import WorkspaceRepository


def create_history_repository() -> HistoryRepository:
    return HistoryRepository()


def create_assignment_repository() -> AssignmentRepository:
    return AssignmentRepository()


def create_atlas_page_repository() -> AtlasPageRepository:
    return AtlasPageRepository()


def create_atlas_link_repository() -> AtlasLinkRepository:
    return AtlasLinkRepository()


def create_identity_repository() -> IdentityRepository:
    return IdentityRepository()


def create_agent_repository() -> AgentRepository:
    return AgentRepository()


def create_agent_management_repository() -> AgentManagementRepository:
    return AgentManagementRepository()


def create_draft_agent_repository() -> DraftAgentRepository:
    return DraftAgentRepository()


def create_generated_agent_publication_repository(
    *,
    agents: AgentRepository | None = None,
    drafts: DraftAgentRepository | None = None,
    work_admission: WorkAdmissionRepository | None = None,
) -> GeneratedAgentPublicationRepository:
    return GeneratedAgentPublicationRepository(
        agents=agents,
        drafts=drafts,
        work_admission=work_admission,
    )


def create_tool_policy_state_repository() -> ToolPolicyStateRepository:
    return ToolPolicyStateRepository()


def create_credential_repository() -> CredentialRepository:
    return CredentialRepository()


def create_offline_grant_repository() -> OfflineGrantRepository:
    return OfflineGrantRepository()


def create_mesh_enrollment_repository() -> MeshEnrollmentRepository:
    return MeshEnrollmentRepository()


def create_verification_credential_repository() -> VerificationCredentialRepository:
    return VerificationCredentialRepository()


def create_notification_repository() -> NotificationRepository:
    return NotificationRepository()


def create_chat_queue_repository() -> ChatQueueRepository:
    return ChatQueueRepository()


def create_push_delivery_repository() -> PushDeliveryRepository:
    return PushDeliveryRepository()


def create_project_repository() -> ProjectRepository:
    return ProjectRepository()


def create_observation_repository() -> ObservationRepository:
    return ObservationRepository()


def create_stop_epoch_repository() -> StopEpochRepository:
    return StopEpochRepository()


def create_framework_credential_repository() -> FrameworkCredentialRepository:
    return FrameworkCredentialRepository()


def create_share_grant_repository() -> ShareGrantRepository:
    return ShareGrantRepository()


def create_chat_read_receipt_repository() -> ChatReadReceiptRepository:
    return ChatReadReceiptRepository()


def create_derived_chat_repository() -> DerivedChatRepository:
    return DerivedChatRepository()


def create_learning_ledger_repository() -> LearningLedgerRepository:
    return LearningLedgerRepository()


def create_run_repository() -> RunRepository:
    return RunRepository()


def create_event_log_repository() -> EventLogRepository:
    return EventLogRepository()


def create_event_roster_repository() -> EventRosterRepository:
    return EventRosterRepository()


def create_chat_step_repository() -> ChatStepRepository:
    return ChatStepRepository()


def create_conversation_file_repository() -> ConversationFileRepository:
    return ConversationFileRepository()


def create_saved_component_repository() -> SavedComponentRepository:
    return SavedComponentRepository()


def create_workspace_repository() -> WorkspaceRepository:
    return WorkspaceRepository()


def create_completion_wake_repository() -> CompletionWakeRepository:
    return CompletionWakeRepository()


def create_computer_use_repository() -> ComputerUseRepository:
    return ComputerUseRepository()


def create_capability_publication_repository() -> CapabilityPublicationRepository:
    return CapabilityPublicationRepository()


def create_artifact_repository() -> ArtifactRepository:
    return ArtifactRepository()


def create_attachment_materialization_coordinator(
    *,
    database: PlaneDatabaseContract,
    materializations: MaterializationRepository,
    blobs: StreamingBlobStore,
) -> AttachmentMaterializationCoordinator:
    return AttachmentMaterializationCoordinator(
        database=database,
        repository=materializations,
        blobs=blobs,
    )


def create_attachment_parser_repository() -> AttachmentParserRepository:
    return AttachmentParserRepository()


def create_preferences_repository() -> PreferencesRepository:
    return PreferencesRepository()


def create_knowledge_repository() -> KnowledgeRepository:
    return KnowledgeRepository()


def create_personalization_graph_repository() -> PersonalizationGraphRepository:
    return PersonalizationGraphRepository()


def create_scheduler_repository() -> SchedulerRepository:
    return SchedulerRepository()


def create_background_task_repository() -> BackgroundTaskRepository:
    return BackgroundTaskRepository()


def create_work_admission_repository() -> WorkAdmissionRepository:
    return WorkAdmissionRepository()


def create_maintenance_repository() -> MaintenanceRepository:
    return MaintenanceRepository()


def create_tracked_job_repository() -> TrackedJobRepository:
    return TrackedJobRepository()


def create_quality_audit_repository() -> QualityAuditRepository:
    return QualityAuditRepository()


def create_harness_cleanup_repository() -> HarnessCleanupRepository:
    return HarnessCleanupRepository()


def create_tutorial_repository() -> TutorialRepository:
    return TutorialRepository()


def create_voice_repository() -> VoiceRepository:
    return VoiceRepository()


def create_remote_repository() -> RemoteRepository:
    return RemoteRepository()


def create_remote_operation_proposal_repository() -> RemoteOperationProposalRepository:
    return RemoteOperationProposalRepository()


def create_revocation_repository() -> RevocationQueueRepository:
    return RevocationQueueRepository()


def create_encrypted_llm_config_repository() -> EncryptedLLMConfigRepository:
    return EncryptedLLMConfigRepository()


def create_encrypted_typesafe_credential_repository() -> EncryptedTypeSafeCredentialRepository:
    return EncryptedTypeSafeCredentialRepository()


def create_audit_repository() -> AuditRepository:
    return AuditRepository()


def create_audit_retention_repository() -> AuditRetentionRepository:
    return AuditRetentionRepository()


def create_outbox_store() -> PostgresOutboxStore:
    return PostgresOutboxStore()


def create_purge_store() -> PostgresPurgeStore:
    return PostgresPurgeStore()


def create_durable_purge_executor(
    *,
    database: PlaneDatabaseContract,
    purge_store: PostgresPurgeStore,
    blobs: StreamingBlobStore,
) -> DurablePurgeExecutor:
    return DurablePurgeExecutor(
        database=database,
        store=purge_store,
        blobs=blobs,
    )


def create_streaming_blob_store(
    *,
    root: str | os.PathLike[str],
    io_chunk_bytes: int = 1024 * 1024,
    create_root: bool = True,
) -> StreamingBlobStore:
    return ExplicitRootStreamingBlobStore(
        root,
        io_chunk_bytes=io_chunk_bytes,
        create_root=create_root,
    )


@dataclass(frozen=True, slots=True)
class RepositoryCatalog:
    identity: IdentityRepository
    assignments: AssignmentRepository
    agents: AgentRepository
    agent_management: AgentManagementRepository
    draft_agents: DraftAgentRepository
    generated_agent_publications: GeneratedAgentPublicationRepository
    tool_policy_state: ToolPolicyStateRepository
    credentials: CredentialRepository
    offline_grants: OfflineGrantRepository
    framework_credentials: FrameworkCredentialRepository
    share_grants: ShareGrantRepository
    chat_queue: ChatQueueRepository
    chat_read_receipts: ChatReadReceiptRepository
    chat_steps: ChatStepRepository
    derived_chat: DerivedChatRepository
    event_log: EventLogRepository
    event_roster: EventRosterRepository
    learning_ledger: LearningLedgerRepository
    runs: RunRepository
    conversation_files: ConversationFileRepository
    saved_components: SavedComponentRepository
    history: HistoryRepository
    workspaces: WorkspaceRepository
    completion_wake: CompletionWakeRepository
    computer_use: ComputerUseRepository
    capability_publications: CapabilityPublicationRepository
    atlas_pages: AtlasPageRepository
    atlas_links: AtlasLinkRepository
    artifacts: ArtifactRepository
    attachment_parsers: AttachmentParserRepository
    preferences: PreferencesRepository
    knowledge: KnowledgeRepository
    personalization_graph: PersonalizationGraphRepository
    scheduler: SchedulerRepository
    background_tasks: BackgroundTaskRepository
    work_admission: WorkAdmissionRepository
    maintenance: MaintenanceRepository
    mesh_enrollment: MeshEnrollmentRepository
    verification_credentials: VerificationCredentialRepository
    notifications: NotificationRepository
    push_deliveries: PushDeliveryRepository
    projects: ProjectRepository
    observations: ObservationRepository
    stop_epochs: StopEpochRepository
    tracked_jobs: TrackedJobRepository
    quality_audit: QualityAuditRepository
    harness_cleanup: HarnessCleanupRepository
    tutorials: TutorialRepository
    voice: VoiceRepository
    remote: RemoteRepository
    remote_operation_proposals: RemoteOperationProposalRepository
    revocations: RevocationQueueRepository
    encrypted_llm_config: EncryptedLLMConfigRepository
    encrypted_typesafe_credential: EncryptedTypeSafeCredentialRepository
    audit: AuditRepository
    audit_retention: AuditRetentionRepository
    authority: AuthorityRepository
    outbox: PostgresOutboxStore
    purge: PostgresPurgeStore

    def as_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "assignments": self.assignments,
                "agent_management": self.agent_management,
                "agents": self.agents,
                "artifacts": self.artifacts,
                "attachment_parsers": self.attachment_parsers,
                "audit": self.audit,
                "audit_retention": self.audit_retention,
                "authority": self.authority,
                "background_tasks": self.background_tasks,
                "chat_queue": self.chat_queue,
                "chat_read_receipts": self.chat_read_receipts,
                "chat_steps": self.chat_steps,
                "derived_chat": self.derived_chat,
                "event_log": self.event_log,
                "event_roster": self.event_roster,
                "learning_ledger": self.learning_ledger,
                "runs": self.runs,
                "conversation_files": self.conversation_files,
                "credentials": self.credentials,
                "draft_agents": self.draft_agents,
                "generated_agent_publications": self.generated_agent_publications,
                "encrypted_llm_config": self.encrypted_llm_config,
                "encrypted_typesafe_credential": self.encrypted_typesafe_credential,
                "framework_credentials": self.framework_credentials,
                "history": self.history,
                "harness_cleanup": self.harness_cleanup,
                "identity": self.identity,
                "knowledge": self.knowledge,
                "maintenance": self.maintenance,
                "mesh_enrollment": self.mesh_enrollment,
                "verification_credentials": self.verification_credentials,
                "notifications": self.notifications,
                "push_deliveries": self.push_deliveries,
                "projects": self.projects,
                "observations": self.observations,
                "offline_grants": self.offline_grants,
                "outbox": self.outbox,
                "preferences": self.preferences,
                "personalization_graph": self.personalization_graph,
                "purge": self.purge,
                "quality_audit": self.quality_audit,
                "remote": self.remote,
                "remote_operation_proposals": self.remote_operation_proposals,
                "revocations": self.revocations,
                "saved_components": self.saved_components,
                "scheduler": self.scheduler,
                "share_grants": self.share_grants,
                "stop_epochs": self.stop_epochs,
                "tool_policy_state": self.tool_policy_state,
                "tracked_jobs": self.tracked_jobs,
                "tutorials": self.tutorials,
                "voice": self.voice,
                "work_admission": self.work_admission,
                "workspaces": self.workspaces,
                "completion_wake": self.completion_wake,
                "computer_use": self.computer_use,
                "capability_publications": self.capability_publications,
                "atlas_pages": self.atlas_pages,
                "atlas_links": self.atlas_links,
            }
        )


def create_repository_catalog() -> RepositoryCatalog:
    agents = create_agent_repository()
    drafts = create_draft_agent_repository()
    work_admission = create_work_admission_repository()
    return RepositoryCatalog(
        assignments=create_assignment_repository(),
        identity=create_identity_repository(),
        agents=agents,
        agent_management=create_agent_management_repository(),
        draft_agents=drafts,
        generated_agent_publications=create_generated_agent_publication_repository(
            agents=agents,
            drafts=drafts,
            work_admission=work_admission,
        ),
        tool_policy_state=create_tool_policy_state_repository(),
        credentials=create_credential_repository(),
        offline_grants=create_offline_grant_repository(),
        framework_credentials=create_framework_credential_repository(),
        share_grants=create_share_grant_repository(),
        chat_queue=create_chat_queue_repository(),
        chat_read_receipts=create_chat_read_receipt_repository(),
        chat_steps=create_chat_step_repository(),
        derived_chat=create_derived_chat_repository(),
        event_log=create_event_log_repository(),
        event_roster=create_event_roster_repository(),
        learning_ledger=create_learning_ledger_repository(),
        runs=create_run_repository(),
        conversation_files=create_conversation_file_repository(),
        saved_components=create_saved_component_repository(),
        history=create_history_repository(),
        workspaces=create_workspace_repository(),
        completion_wake=create_completion_wake_repository(),
        computer_use=create_computer_use_repository(),
        capability_publications=create_capability_publication_repository(),
        atlas_pages=create_atlas_page_repository(),
        atlas_links=create_atlas_link_repository(),
        artifacts=create_artifact_repository(),
        attachment_parsers=create_attachment_parser_repository(),
        preferences=create_preferences_repository(),
        knowledge=create_knowledge_repository(),
        personalization_graph=create_personalization_graph_repository(),
        scheduler=create_scheduler_repository(),
        background_tasks=create_background_task_repository(),
        work_admission=work_admission,
        maintenance=create_maintenance_repository(),
        mesh_enrollment=create_mesh_enrollment_repository(),
        verification_credentials=create_verification_credential_repository(),
        notifications=create_notification_repository(),
        push_deliveries=create_push_delivery_repository(),
        projects=create_project_repository(),
        observations=create_observation_repository(),
        stop_epochs=create_stop_epoch_repository(),
        tracked_jobs=create_tracked_job_repository(),
        quality_audit=create_quality_audit_repository(),
        harness_cleanup=create_harness_cleanup_repository(),
        tutorials=create_tutorial_repository(),
        voice=create_voice_repository(),
        remote=create_remote_repository(),
        remote_operation_proposals=create_remote_operation_proposal_repository(),
        revocations=create_revocation_repository(),
        encrypted_llm_config=create_encrypted_llm_config_repository(),
        encrypted_typesafe_credential=(create_encrypted_typesafe_credential_repository()),
        audit=create_audit_repository(),
        audit_retention=create_audit_retention_repository(),
        authority=create_authority_repository(),
        outbox=create_outbox_store(),
        purge=create_purge_store(),
    )


@dataclass(frozen=True, slots=True)
class PlaneHealth:
    compatibility: CompatibilityReport
    boot_status: BootStatus
    pool: PoolSnapshot

    @property
    def ready(self) -> bool:
        return (
            self.compatibility.compatible
            and self.boot_status is BootStatus.READY
            and not self.pool.closed
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "boot_status": self.boot_status.value,
            "compatibility": self.compatibility.to_dict(),
            "pool": {
                "borrowed": self.pool.borrowed,
                "closed": self.pool.closed,
                "cleanup_complete": self.pool.cleanup_complete,
                "cleanup_failed": self.pool.cleanup_failed,
                "cleanup_attempts": self.pool.cleanup_attempts,
            },
            "ready": self.ready,
        }


class PlaneRuntime:
    def __init__(
        self,
        *,
        pool: ConnectionPool,
        database: PlaneDatabase,
        initializer: BootInitializer,
        reconciler: ReconciliationRunner,
        repositories: RepositoryCatalog | None = None,
        expected_contract_version: str = CONTRACT_VERSION,
        observed_schema_revision: str = SCHEMA_REVISION,
        consumer_version: str = PACKAGE_VERSION,
        data_plane_revision: DataPlaneRevision = CURRENT_DATA_PLANE_REVISION,
    ) -> None:
        self._pool = pool
        self._database = database
        self._initializer = initializer
        self._reconciler = reconciler
        self.repositories = repositories or create_repository_catalog()
        self._expected_contract_version = expected_contract_version
        self._observed_schema_revision = observed_schema_revision
        self._consumer_version = consumer_version
        self._data_plane_revision = data_plane_revision

    def inspect_compatibility(self) -> CompatibilityReport:
        return inspect_compatibility(
            expected_contract_version=self._expected_contract_version,
            observed_schema_revision=self._observed_schema_revision,
            consumer_version=self._consumer_version,
            data_plane_revision=self._data_plane_revision,
        )

    def inspect_baseline_compatibility(self) -> BaselineCompatibilityReport:
        return inspect_baseline_compatibility(self._database)

    def initialize(self, *, expected_revision: str = SCHEMA_REVISION) -> InitializationReport:
        compatibility = self.inspect_compatibility()
        if not compatibility.compatible:
            raise InitializationError(
                "AstralPlane compatibility inspection rejected initialization",
                metadata={"reasons": ",".join(compatibility.reasons)},
            )
        return self._initializer.initialize(expected_revision=expected_revision)

    def reconcile(
        self,
        *,
        context: Mapping[str, object],
        schema_revision: str = SCHEMA_REVISION,
    ) -> ReconciliationReport:
        self._require_ready()
        return self._reconciler.run(schema_revision=schema_revision, context=context)

    @contextmanager
    def transaction(
        self,
        *,
        isolation: IsolationLevel | None = None,
    ) -> Iterator[Transaction]:
        self._require_ready()
        with self._database.transaction(isolation=isolation) as transaction:
            yield transaction

    def health(self) -> PlaneHealth:
        return PlaneHealth(
            compatibility=self.inspect_compatibility(),
            boot_status=self._initializer.status,
            pool=self._pool.snapshot,
        )

    def close(self) -> None:
        self._pool.close()

    def _require_ready(self) -> None:
        health = self.health()
        if not health.ready:
            raise InitializationError(
                "AstralPlane is not ready for ordinary durable-state work",
                metadata={
                    "boot_status": health.boot_status.value,
                    "compatible": health.compatibility.compatible,
                    "pool_closed": health.pool.closed,
                },
            )


def create_plane_runtime(
    driver_pool: DriverPool,
    *,
    identity: str,
    coordinator: ReconciliationCoordinator | None = None,
    reconcilers: Iterable[ProductReconciler],
    reconciliation_context: Mapping[str, object] | None = None,
    repositories: RepositoryCatalog | None = None,
    expected_contract_version: str = CONTRACT_VERSION,
    observed_schema_revision: str = SCHEMA_REVISION,
    consumer_version: str = PACKAGE_VERSION,
    dialect: Dialect = POSTGRES,
) -> PlaneRuntime:
    pool = ConnectionPool(driver_pool)
    database = PlaneDatabase(pool)
    boot_runner: object
    if dialect.is_sqlite:
        boot_runner = SQLiteBootRunner(
            database,
            MigrationRunner(
                database,
                revision=SQLITE_DATA_PLANE_REVISION,
                registry=SQLITE_MIGRATION_REGISTRY,
            ),
        )
    else:
        boot_runner = BaselineMigrationRunner(
            database,
            MigrationRunner(
                database,
                revision=CURRENT_DATA_PLANE_REVISION,
                registry=MIGRATION_REGISTRY,
            ),
        )
    if coordinator is None and dialect.is_sqlite:
        coordinator = SQLiteReconciliationCoordinator(
            pool, driver_pool.database_path, timeout_seconds=driver_pool.busy_timeout_seconds
        )
    reconciliation_runner = ReconciliationRunner(
        coordinator or PostgresReconciliationCoordinator(pool),
        reconcilers,
    )
    initializer = BootInitializer(
        identity,
        boot_runner,  # type: ignore[arg-type]
        reconciliation_runner,
        reconciliation_context=reconciliation_context,
    )
    return PlaneRuntime(
        pool=pool,
        database=database,
        initializer=initializer,
        reconciler=reconciliation_runner,
        repositories=repositories,
        expected_contract_version=expected_contract_version,
        observed_schema_revision=observed_schema_revision,
        consumer_version=consumer_version,
        data_plane_revision=SQLITE_DATA_PLANE_REVISION
        if dialect.is_sqlite
        else CURRENT_DATA_PLANE_REVISION,
    )


def create_sqlite_runtime(
    database_url: str,
    *,
    identity: str,
    reconcilers: Iterable[ProductReconciler],
    coordinator: ReconciliationCoordinator | None = None,
    reconciliation_context: Mapping[str, object] | None = None,
    repositories: RepositoryCatalog | None = None,
    expected_contract_version: str = CONTRACT_VERSION,
    observed_schema_revision: str = SCHEMA_REVISION,
    consumer_version: str = PACKAGE_VERSION,
    maximum_connections: int = 8,
    acquire_timeout_seconds: float = 30.0,
    busy_timeout_seconds: float = 10.0,
) -> PlaneRuntime:
    driver_pool = create_sqlite_driver_pool(
        database_url,
        maximum_connections=maximum_connections,
        acquire_timeout_seconds=acquire_timeout_seconds,
        busy_timeout_seconds=busy_timeout_seconds,
    )
    try:
        return create_plane_runtime(
            driver_pool,
            identity=identity,
            coordinator=coordinator,
            reconcilers=reconcilers,
            reconciliation_context=reconciliation_context,
            repositories=repositories,
            expected_contract_version=expected_contract_version,
            observed_schema_revision=observed_schema_revision,
            consumer_version=consumer_version,
            dialect=SQLITE,
        )
    except BaseException:
        with suppress(BaseException):
            driver_pool.closeall()
        raise


def create_runtime(
    database_url: str,
    *,
    identity: str,
    reconcilers: Iterable[ProductReconciler],
    **options: Any,
) -> PlaneRuntime:
    if is_sqlite_url(database_url):
        return create_sqlite_runtime(
            database_url, identity=identity, reconcilers=reconcilers, **options
        )
    return create_postgres_runtime(
        database_url, identity=identity, reconcilers=reconcilers, **options
    )


def create_postgres_runtime(
    database_url: str,
    *,
    identity: str,
    reconcilers: Iterable[ProductReconciler],
    coordinator: ReconciliationCoordinator | None = None,
    reconciliation_context: Mapping[str, object] | None = None,
    repositories: RepositoryCatalog | None = None,
    expected_contract_version: str = CONTRACT_VERSION,
    observed_schema_revision: str = SCHEMA_REVISION,
    consumer_version: str = PACKAGE_VERSION,
    minimum_connections: int = 2,
    maximum_connections: int = 10,
    acquire_timeout_seconds: float = 30.0,
    connect_timeout_seconds: int = 10,
    application_name: str | None = None,
) -> PlaneRuntime:
    driver_pool = create_postgres_driver_pool(
        database_url,
        minimum_connections=minimum_connections,
        maximum_connections=maximum_connections,
        acquire_timeout_seconds=acquire_timeout_seconds,
        connect_timeout_seconds=connect_timeout_seconds,
        application_name=(
            f"astralplane:{identity}" if application_name is None else application_name
        ),
    )
    try:
        return create_plane_runtime(
            driver_pool,
            identity=identity,
            coordinator=coordinator,
            reconcilers=reconcilers,
            reconciliation_context=reconciliation_context,
            repositories=repositories,
            expected_contract_version=expected_contract_version,
            observed_schema_revision=observed_schema_revision,
            consumer_version=consumer_version,
        )
    except BaseException:
        with suppress(BaseException):
            driver_pool.closeall()
        raise


__all__ = (
    "ENDPOINT_READINESS_CONTRACT_VERSION",
    "GENERATED_AGENT_BUNDLE_CONTRACT",
    "GENERATED_AGENT_PUBLICATION_IDEMPOTENCY_NAMESPACE",
    "GENERATED_AGENT_PUBLICATION_OPERATION_KIND",
    "GENERATED_AGENT_PUBLICATION_RECOVERY_IDEMPOTENCY_NAMESPACE",
    "GENERATED_AGENT_PUBLICATION_RECOVERY_OPERATION_KIND",
    "JOINT_RESTORE_CONTRACT_VERSION",
    "AdmissionClass",
    "AgentRevisionRecord",
    "ApprovalFence",
    "ArtifactCollisionError",
    "ArtifactIntegrityError",
    "ArtifactPublicationError",
    "ArtifactPublicationRevokedError",
    "ArtifactReconciliationError",
    "AssignmentRepository",
    "AsyncPlaneRuntime",
    "AtlasLinkBatch",
    "AtlasLinkChange",
    "AtlasLinkCommandReceipt",
    "AtlasLinkCursor",
    "AtlasLinkRecord",
    "AtlasLinkRepository",
    "AtlasLinkSetCommand",
    "AtlasPageAppendChange",
    "AtlasPageAppendCommand",
    "AtlasPageBatch",
    "AtlasPageChange",
    "AtlasPageCommandReceipt",
    "AtlasPageCreateCommand",
    "AtlasPageCursor",
    "AtlasPageDeleteCommand",
    "AtlasPageEditChange",
    "AtlasPageEditCommand",
    "AtlasPageRecord",
    "AtlasPageRepository",
    "AtlasPageRevisionBatch",
    "AtlasPageRevisionCursor",
    "AtlasPageRevisionRecord",
    "AttachmentMaterializationCoordinator",
    "AuthorityCompareAndSetConflictError",
    "AuthorityIdempotencyConflictError",
    "AuthorityRepository",
    "BaselineCompatibilityReport",
    "BaselineCompatibilityState",
    "BaselineInitializationReport",
    "BundlePublicationKey",
    "BundlePublicationPaths",
    "BundlePublicationReceipt",
    "BundleRecoveryDisposition",
    "BundleRecoveryResult",
    "CapabilityPublicationConflictError",
    "CapabilityPublicationNotExpiredError",
    "CapabilityPublicationRecord",
    "CapabilityPublicationRepository",
    "CapabilityPublicationState",
    "ChatQueueRepository",
    "ChatReadReceiptRecord",
    "ChatReadReceiptRepository",
    "ChatReadStateRecord",
    "CheckpointContradiction",
    "CheckpointRecord",
    "ClaimedPushDelivery",
    "ComputerSession",
    "ComputerTaskGrant",
    "ComputerUseRepository",
    "CountersignatureRecord",
    "DerivedChatRecord",
    "DerivedChatRepository",
    "DerivedMessageRecord",
    "DraftAgentRecord",
    "DraftPublicationRecord",
    "EndpointFence",
    "EndpointFenceError",
    "EndpointReadiness",
    "EndpointReadinessError",
    "EndpointReadinessState",
    "EndpointTopology",
    "EventAppendResult",
    "EventBodyRecord",
    "EventCursor",
    "EventFeedRecord",
    "EventLogRepository",
    "EventMeshRecord",
    "EventRecord",
    "EventRosterRepository",
    "ExecutionFence",
    "FeedBehindCheckpoint",
    "FinalizedBundle",
    "ForkEvidenceRecord",
    "GeneratedAgentPublicationIntent",
    "GeneratedAgentPublicationOperationBinding",
    "GeneratedAgentPublicationRepository",
    "GeneratedAgentPublicationResultMetadata",
    "HarnessProfileIdentity",
    "HostEffectIntent",
    "HostEffectIntentState",
    "ImmutableBundleContract",
    "ImmutableBundleStore",
    "JointRestoreCompletion",
    "JointRestoreError",
    "JointRestoreEvidence",
    "LearningItemRecord",
    "LearningLedgerRepository",
    "LedgerEntryRecord",
    "LockContentionError",
    "NotificationDeliveryIdentity",
    "NotificationEventKind",
    "NotificationInboxEntry",
    "NotificationPreferenceRecord",
    "NotificationRepository",
    "ObservationBatch",
    "ObservationCapture",
    "ObservationCaptureCommand",
    "ObservationCursor",
    "ObservationHead",
    "ObservationPage",
    "ObservationPurge",
    "ObservationRepository",
    "ObservationUsage",
    "OperationHarnessBinding",
    "OperationOwner",
    "OperationRecord",
    "OperationRequest",
    "OperationState",
    "OwnerScope",
    "OwnerStopRecord",
    "OwnerStoppedError",
    "PeerStopAcknowledgment",
    "PlaneHealth",
    "PlaneRuntime",
    "ProjectChatCursor",
    "ProjectChatPage",
    "ProjectChatRecord",
    "ProjectCursor",
    "ProjectPage",
    "ProjectRecord",
    "ProjectRepository",
    "PublishedBundle",
    "PushDeliveryRecord",
    "PushDeliveryRepository",
    "PushDestinationRecord",
    "QueuedFollowUpRecord",
    "ReceiptClaimConflictError",
    "ReceiptWatermarkConflictError",
    "RepositoryCatalog",
    "RestoreBlobEntry",
    "RestoredSessionRetirement",
    "RosterEntryRecord",
    "RunGoalRecord",
    "RunLeaseRecord",
    "RunRepository",
    "SessionRetirementError",
    "StagedBundleReceipt",
    "StopEpochConflictError",
    "StopEpochRepository",
    "canonical_bundle_digest",
    "canonical_generated_agent_manifest_digest",
    "create_agent_management_repository",
    "create_agent_repository",
    "create_artifact_repository",
    "create_assignment_repository",
    "create_atlas_link_repository",
    "create_atlas_page_repository",
    "create_attachment_materialization_coordinator",
    "create_attachment_parser_repository",
    "create_audit_repository",
    "create_audit_retention_repository",
    "create_authority_repository",
    "create_background_task_repository",
    "create_capability_publication_repository",
    "create_chat_queue_repository",
    "create_chat_read_receipt_repository",
    "create_chat_step_repository",
    "create_computer_use_repository",
    "create_conversation_file_repository",
    "create_credential_repository",
    "create_derived_chat_repository",
    "create_draft_agent_repository",
    "create_durable_purge_executor",
    "create_encrypted_llm_config_repository",
    "create_encrypted_typesafe_credential_repository",
    "create_event_log_repository",
    "create_event_roster_repository",
    "create_generated_agent_publication_repository",
    "create_harness_cleanup_repository",
    "create_history_repository",
    "create_identity_repository",
    "create_knowledge_repository",
    "create_learning_ledger_repository",
    "create_maintenance_repository",
    "create_notification_repository",
    "create_observation_repository",
    "create_offline_grant_repository",
    "create_outbox_store",
    "create_personalization_graph_repository",
    "create_plane_runtime",
    "create_postgres_runtime",
    "create_preferences_repository",
    "create_project_repository",
    "create_purge_store",
    "create_push_delivery_repository",
    "create_quality_audit_repository",
    "create_remote_operation_proposal_repository",
    "create_remote_repository",
    "create_repository_catalog",
    "create_revocation_repository",
    "create_run_repository",
    "create_runtime",
    "create_saved_component_repository",
    "create_scheduler_repository",
    "create_share_grant_repository",
    "create_sqlite_runtime",
    "create_stop_epoch_repository",
    "create_streaming_blob_store",
    "create_tool_policy_state_repository",
    "create_tracked_job_repository",
    "create_tutorial_repository",
    "create_voice_repository",
    "create_work_admission_repository",
    "create_workspace_repository",
    "generated_agent_publication_operation_binding",
    "generated_agent_publication_paths",
    "generated_agent_publication_recovery_operation_binding",
    "initialize_empty_database",
    "inspect_baseline_compatibility",
    "inspect_endpoint_readiness",
    "is_lock_contention",
    "paths_for",
    "qualify_joint_restore",
    "recover_joint_restore",
    "retire_restored_sessions",
    "runtime_metadata_for_manifest",
)
