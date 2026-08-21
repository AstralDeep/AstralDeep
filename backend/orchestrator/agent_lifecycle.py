"""
Agent Lifecycle Manager for AstralDeep.

Manages the full lifecycle of user-created agents:
  pending → generating → generated → testing → analyzing →
  approved/pending_review/rejected → live

Handles code generation, security analysis, file I/O,
subprocess management, and approval flow.
"""
import ast
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import inspect
import json
import logging
import os
from pathlib import PurePosixPath
import re
import shutil
import sys
import time
import uuid
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
)

from astralplane import GeneratedAgentPublicationResultMetadata
from astralplane.authority import AuthorityPopulation
from astralplane.repositories import RepositoryConflictError

from orchestrator.agent_generator import (
    BYO_BUNDLE_FILENAMES,
    BYO_RUNTIME_CONTRACT_VERSION,
    BYO_RUNTIME_LOCK_SHA256,
    AgentCodeGenerator,
)
from orchestrator.generated_agent_publication import (
    GenerationClaimHeartbeat,
    GeneratedAgentPublicationManagedError,
    GeneratedAgentPublicationRecoveryPendingError,
    GeneratedAgentPublicationRequest,
    GeneratedAgentPublicationResult,
    GeneratedAgentPublicationService,
    generated_agent_publication_identity,
)
from orchestrator.lets_lifecycle import (
    GovernedLifecycleCoordinator,
    LifecycleConvergence,
    LetsLifecycleError,
)
from orchestrator.user_agents import (
    PersonalAgentRuntimeRepository,
    StaleRuntimeGenerationError,
)
from orchestrator.work_admission import (
    ExecutionFence,
    OperationState,
    StaleExecutionFenceError,
)
from orchestrator.agent_validator import AgentSpecValidator
from orchestrator.code_security import (
    CodeSecurityAnalyzer,
    Severity,
    blocks_execution,
)
from shared.process_supervision import (
    ProcessOwner,
    ProcessSupervisor,
    TerminationReason,
)
from shared.protocol import AgentLifecycle

logger = logging.getLogger("AgentLifecycle")


def _json_mapping_default(value: object) -> dict[str, object]:
    """Thaw Plane's detached immutable JSON mappings for canonical encoding."""

    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"unsupported manifest value: {type(value).__name__}")


def _attach_generated_publication(
    state: Mapping[str, Any],
    result: GeneratedAgentPublicationResult,
) -> Dict[str, Any]:
    """Attach re-opened Plane bundle evidence to one detached draft result."""

    if not isinstance(result, GeneratedAgentPublicationResult):
        raise TypeError("generated publication result is required")
    published = result.published
    revision = result.revision
    detached = dict(state)
    detached["files"] = {
        **dict(published.files),
        "manifest.json": published.manifest_json,
    }
    detached["runtime_manifest"] = published.manifest_dict()
    detached["bundle_sha256"] = published.bundle_sha256
    detached["manifest_sha256"] = published.manifest_sha256
    detached["artifact_relative_path"] = published.bundle_relative_path
    detached["publication_id"] = str(result.publication.publication_id)
    detached["revision_id"] = str(revision.revision_id)
    detached["published_revision_state"] = revision.state
    detached["published_revision_failure_code"] = revision.failure_code
    detached["runtime_contract_version"] = revision.runtime_contract_version
    detached["required_runtime_lock_sha256"] = revision.release_lock_digest
    return detached


async def _join_task_through_cancellation(
    task: asyncio.Task[Any],
) -> tuple[Any, Optional[asyncio.CancelledError]]:
    """Join one retained task while recording repeated caller cancellation."""

    result, error, cancellation = await _join_task_outcome_through_cancellation(
        task
    )
    if error is not None:
        raise error
    return result, cancellation


async def _join_task_outcome_through_cancellation(
    task: asyncio.Task[Any],
) -> tuple[Any, BaseException | None, Optional[asyncio.CancelledError]]:
    """Join a task and retain caller cancellation even when the task fails."""

    cancellation: Optional[asyncio.CancelledError] = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            caller_is_cancelling = bool(
                current is not None and current.cancelling()
            )
            if caller_is_cancelling:
                cancellation = cancellation or exc
            if task.done():
                break
        except BaseException:
            break
    try:
        return task.result(), None, cancellation
    except BaseException as error:
        return None, error, cancellation

# Statuses
PENDING = "pending"
GENERATING = "generating"
GENERATED = "generated"
TESTING = "testing"
ANALYZING = "analyzing"
APPROVED = "approved"
PENDING_REVIEW = "pending_review"
REJECTED = "rejected"
VALIDATING = "validating"
LIVE = "live"
ERROR = "error"

_GENERATION_CLAIM_RENEW_INTERVAL_SECONDS = 60.0

# Generation targets (058 T008)
BACKEND_TARGET = "backend"   # 027: server-hosted, run here as a subprocess
BYO_TARGET = "byo"           # 058: self-contained bundle, run on the owner's desktop

#: ``draft_agents.origin`` of a user-authored, client-hosted agent. Its code is
#: NEVER executed on this host (058 SC-002) — the draft row exists only to carry
#: the authoring journey.
BYO_ORIGIN = "byo_client"

AGENT_LIFECYCLE_LABELS = {
    "starting": "Starting",
    "online": "Online",
    "updating": "Updating",
    "failed": "Failed",
    "offline": "Offline",
}


def canonical_agent_lifecycle(
    *,
    agent_id: str,
    revision_id: Optional[str],
    runtime_instance_id: Optional[str],
    lifecycle_generation: int,
    state_revision: int,
    state: str,
    reason_code: Optional[str] = None,
    updated_at: Optional[datetime] = None,
) -> AgentLifecycle:
    """Build and validate one canonical personal-agent lifecycle projection."""

    timestamp = updated_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    frame = AgentLifecycle(
        agent_id=agent_id,
        revision_id=revision_id,
        runtime_instance_id=runtime_instance_id,
        lifecycle_generation=lifecycle_generation,
        state_revision=state_revision,
        state=state,
        reason_code=reason_code,
        label=AGENT_LIFECYCLE_LABELS.get(state, state.replace("_", " ").title()),
        updated_at=timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    )
    frame.validate()
    return frame


async def publish_agent_lifecycle(
    orchestrator: Any,
    owner_user_id: str,
    **projection: Any,
) -> int:
    """Publish one validated lifecycle frame only to the owning user's UIs.

    Durable runtime/revision state remains authoritative; this function never
    allocates or increments a generation. Callers pass the committed fence and
    state revision after their database transaction succeeds.

    Returns:
        Number of owner sockets that accepted the projection.
    """

    if not isinstance(owner_user_id, str) or not owner_user_id:
        raise ValueError("owner_user_id must be non-empty")
    frame = canonical_agent_lifecycle(**projection)
    payload = frame.to_json()
    sessions = getattr(orchestrator, "ui_sessions", {}) or {}
    clients = tuple(getattr(orchestrator, "ui_clients", ()) or ())
    delivered = 0
    for websocket in clients:
        claims = sessions.get(websocket) or {}
        if claims.get("sub") != owner_user_id:
            continue
        if await orchestrator._safe_send(websocket, payload):
            delivered += 1
    return delivered


class RevisionActivationError(RuntimeError):
    """Safe terminal failure while preparing or promoting one BYO revision."""

    @property
    def code(self) -> str:
        return str(self)


class RevisionActivationRecoveryPendingError(RevisionActivationError):
    """Promotion authority could not be resolved after a lost acknowledgement."""


@dataclass(frozen=True)
class CandidateAgentMetadata:
    """Server-approved agent metadata committed only with revision authority."""

    draft_id: str
    draft_state_revision: int
    display_name: str
    constitution_version: str
    validated_policy_revision: str
    declared_tools: tuple[str, ...]
    declared_scopes: tuple[str, ...]
    declared_egress: tuple[str, ...]

    def __post_init__(self) -> None:
        from orchestrator.agent_constitution import USER_AGENT_POLICY_REVISION

        for field_name, value, maximum in (
            ("draft_id", self.draft_id, 1024),
            ("display_name", self.display_name, 255),
            ("constitution_version", self.constitution_version, 128),
            ("validated_policy_revision", self.validated_policy_revision, 128),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > maximum
            ):
                raise ValueError(f"{field_name} must be non-empty and bounded")
        if (
            type(self.draft_state_revision) is not int
            or self.draft_state_revision < 0
        ):
            raise ValueError("draft_state_revision must be non-negative")
        for field_name, values, maximum in (
            ("declared_tools", self.declared_tools, 255),
            ("declared_scopes", self.declared_scopes, 255),
            ("declared_egress", self.declared_egress, 2048),
        ):
            if not isinstance(values, tuple) or len(values) > 64:
                raise TypeError(f"{field_name} must be a bounded tuple")
            if any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > maximum
                for value in values
            ) or len(set(values)) != len(values):
                raise ValueError(f"{field_name} entries must be unique bounded text")
        if self.validated_policy_revision != USER_AGENT_POLICY_REVISION:
            raise ValueError("validated_policy_revision is stale")

    @property
    def canonical_digest(self) -> str:
        """Bind delivery idempotency to the exact candidate metadata."""

        payload = json.dumps(
            {
                "constitution_version": self.constitution_version,
                "declared_egress": list(self.declared_egress),
                "declared_scopes": list(self.declared_scopes),
                "declared_tools": list(self.declared_tools),
                "display_name": self.display_name,
                "draft_id": self.draft_id,
                "draft_state_revision": self.draft_state_revision,
                "validated_policy_revision": self.validated_policy_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(
            b"astraldeep:personal-agent-candidate-metadata:v1\x00" + payload
        ).hexdigest()


@dataclass(frozen=True)
class CandidatePreparation:
    """Immutable inputs needed to durably prepare one candidate revision."""

    owner_user_id: str
    agent_id: str
    revision_id: str
    bundle_sha256: str
    runtime_manifest: Mapping[str, Any]
    artifact_relative_path: str
    runtime_contract_version: int
    required_runtime_lock_sha256: str
    host_session_id: str
    operation_fence: Any
    agent_metadata: CandidateAgentMetadata


@dataclass(frozen=True)
class ActiveRevisionReplay:
    """Exact immutable identity required to acknowledge an active replay."""

    owner_user_id: str
    agent_id: str
    revision_id: str
    bundle_sha256: str
    runtime_manifest: Mapping[str, Any]
    artifact_relative_path: str
    runtime_contract_version: int
    required_runtime_lock_sha256: str
    runtime_instance_id: str
    agent_metadata: CandidateAgentMetadata


@dataclass(frozen=True)
class CandidateRevision:
    """Durable candidate and the authority that existed before preparation."""

    owner_user_id: str
    agent_id: str
    revision_id: str
    promotion_token: str
    runtime_instance_id: str
    previous_active_revision_id: Optional[str]
    previous_runtime_instance_id: Optional[str]
    agent_metadata: CandidateAgentMetadata


@dataclass(frozen=True)
class PromotionCommit:
    """Result of the single transaction that changes routing authority."""

    owner_user_id: str
    agent_id: str
    revision_id: str
    runtime_instance_id: str
    previous_revision_id: Optional[str]
    previous_runtime_instance_id: Optional[str]


@dataclass(frozen=True)
class RecoveryPlan:
    """Actions derived only from durable active/authoritative pointers."""

    owner_user_id: str
    agent_id: str
    active_revision_id: Optional[str]
    authoritative_runtime_instance_id: Optional[str]
    start_revision_id: Optional[str]
    stop_runtime_instance_ids: tuple[str, ...]
    finalize_runtime_instance_ids: tuple[str, ...] = ()
    retry_reset_candidates: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RevisionRecoveryStatus:
    """Owner-locked durable state used to decide whether replay may recover."""

    owner_user_id: str
    agent_id: str
    revision_id: str
    revision_state: str
    runtime_instance_id: Optional[str]
    runtime_failure_code: Optional[str]
    operation_state: Optional[OperationState]
    operation_terminal_code: Optional[str]


@dataclass(frozen=True)
class PhysicalStopReceipt:
    """Exact host-stop acknowledgement retained until Plane finalization."""

    runtime_instance_id: str
    release: Callable[[], Awaitable[Any] | Any]

    def __post_init__(self) -> None:
        try:
            canonical_runtime_id = str(uuid.UUID(self.runtime_instance_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("stop receipt runtime_instance_id must be a UUID") from exc
        if canonical_runtime_id != self.runtime_instance_id:
            raise ValueError("stop receipt runtime_instance_id must be canonical")
        if not callable(self.release):
            raise TypeError("stop receipt release callback is required")


@dataclass(frozen=True)
class RevisionActivationResult:
    """Committed activation plus best-effort post-commit cleanup status."""

    commit: PromotionCommit
    prior_runtime_stopped: bool
    cleanup_pending: bool


class RevisionActivationStore(Protocol):
    """Narrow durable seam used by the two-phase revision coordinator."""

    def prepare_candidate(self, request: CandidatePreparation) -> CandidateRevision: ...

    def mark_candidate_starting(self, candidate: CandidateRevision) -> None: ...

    def confirm_candidate_ready(
        self, candidate: CandidateRevision, ready_runtime_instance_id: str
    ) -> CandidateRevision: ...

    def promote_candidate(self, candidate: CandidateRevision) -> PromotionCommit: ...

    def stage_candidate_failure(
        self, candidate: CandidateRevision, failure_code: str
    ) -> bool: ...

    def fail_candidate(self, candidate: CandidateRevision, failure_code: str) -> None: ...

    def recovery_plan(self, owner_user_id: str, agent_id: str) -> RecoveryPlan: ...

    def finalize_recovery_runtime(
        self, owner_user_id: str, agent_id: str, runtime_instance_id: str
    ) -> None: ...

    def stage_retryable_candidate_reset(
        self, owner_user_id: str, agent_id: str, revision_id: str
    ) -> str: ...

    def finalize_retryable_candidate_reset(
        self,
        owner_user_id: str,
        agent_id: str,
        revision_id: str,
        runtime_instance_id: str,
    ) -> None: ...

    def inspect_recovery_status(
        self, owner_user_id: str, agent_id: str, revision_id: str
    ) -> Optional[RevisionRecoveryStatus]: ...


@dataclass
class PostgresPersonalAgentRevisionStore:
    """Durable revision activation adapter over the feature-060 repository.

    ``PersonalAgentRuntimeRepository`` remains the owner of host/runtime fences.
    This adapter owns only the revision/pointer transaction that the repository
    intentionally does not expose.  It uses the repository's transaction and
    owner-lock seams so selection, runtime, and promotion serialize together.
    """

    _SHA256 = re.compile(r"[0-9a-f]{64}")
    _SAFE_FAILURE = re.compile(r"[a-z][a-z0-9_]{0,127}")
    _PERMANENT_ACTIVATION_FAILURES = frozenset(
        {
            "bundle_install_failed",
            "child_start_failed",
            "child_registration_timeout",
            "revision_promotion_failed",
        }
    )
    _NONTERMINAL_RUNTIME_STATES = (
        "delivering",
        "starting",
        "ready",
        "online",
        "updating",
        "stopping",
    )
    _TERMINAL_RUNTIME_STATES = frozenset(
        {"stopped", "failed", "offline", "superseded"}
    )
    _RETRY_RESET_PENDING = "revision_delivery_retry_reset_pending"
    _RETRY_RESET_COMPLETE = "revision_delivery_retry_reset"
    _PHYSICAL_EXIT_PROOFS = frozenset({"child_exited", "agent_offline"})

    runtime_repository: PersonalAgentRuntimeRepository

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_repository, PersonalAgentRuntimeRepository):
            raise TypeError(
                "runtime_repository must be PersonalAgentRuntimeRepository"
            )
        catalog = getattr(
            self.runtime_repository._agents.plane_runtime,
            "repositories",
            None,
        )
        drafts = getattr(catalog, "draft_agents", None)
        if drafts is None:
            raise TypeError("Plane draft-agent repository is required")
        self._drafts = drafts

    @property
    def _runtime(self) -> PersonalAgentRuntimeRepository:
        return self.runtime_repository

    @staticmethod
    def _required_text(value: Any, field_name: str, maximum: int = 1024) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ValueError(f"{field_name} must be non-empty and bounded")
        return value

    @staticmethod
    def _uuid_text(value: Any, field_name: str) -> str:
        try:
            return str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"{field_name} must be a UUID") from exc

    def _validate_preparation(
        self, request: CandidatePreparation
    ) -> tuple[Mapping[str, Any], str]:
        self._uuid_text(request.host_session_id, "host_session_id")
        if not isinstance(request.operation_fence, ExecutionFence):
            raise TypeError("operation_fence must be ExecutionFence")
        return self._validate_candidate_identity(request)

    def _validate_candidate_identity(
        self, request: CandidatePreparation | ActiveRevisionReplay
    ) -> tuple[Mapping[str, Any], str]:
        self._required_text(request.owner_user_id, "owner_user_id", 255)
        self._required_text(request.agent_id, "agent_id", 255)
        self._uuid_text(request.revision_id, "revision_id")
        if self._SHA256.fullmatch(request.bundle_sha256 or "") is None:
            raise ValueError("bundle_sha256 must be lowercase SHA-256")
        policy = self._runtime._policy
        if request.runtime_contract_version != policy.runtime_contract_version:
            raise ValueError("revision runtime contract is incompatible")
        if request.required_runtime_lock_sha256 != policy.runtime_lock_sha256:
            raise ValueError("revision runtime lock is incompatible")
        path = request.artifact_relative_path
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 1024
            or "\\" in path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
        ):
            raise ValueError("artifact path must remain beneath revision root")
        immutable_manifest, canonical_manifest = self._runtime._validate_manifest(
            request.runtime_manifest
        )
        required_manifest = {
            "revision_id": request.revision_id,
            "agent_id": request.agent_id,
            "bundle_sha256": request.bundle_sha256,
            "runtime_contract_version": request.runtime_contract_version,
            "required_runtime_lock_sha256": (
                request.required_runtime_lock_sha256
            ),
        }
        for key, expected in required_manifest.items():
            if immutable_manifest.get(key) != expected:
                raise ValueError(f"runtime manifest {key} does not match candidate")
        if (
            immutable_manifest.get("agent_name")
            != request.agent_metadata.display_name
            or immutable_manifest.get("constitution_version")
            != request.agent_metadata.constitution_version
        ):
            raise ValueError("runtime manifest agent metadata is stale")
        entries = immutable_manifest.get("files")
        if not (
            immutable_manifest.get("manifest_version") == 2
            and immutable_manifest.get("digest_algorithm") == "sha256"
            and isinstance(entries, Sequence)
            and not isinstance(entries, (str, bytes))
            and tuple(
                entry.get("name") if isinstance(entry, Mapping) else None
                for entry in entries
            )
            == BYO_BUNDLE_FILENAMES
        ):
            raise ValueError("runtime manifest file inventory is invalid")
        for entry in entries:
            if not (
                self._SHA256.fullmatch(str(entry.get("sha256") or ""))
                and type(entry.get("size_bytes")) is int
                and entry["size_bytes"] >= 0
            ):
                raise ValueError("runtime manifest file metadata is invalid")
        return immutable_manifest, canonical_manifest

    def assert_active_replay(
        self,
        request: ActiveRevisionReplay,
    ) -> None:
        """Authenticate a committed delivery replay before route projection."""

        _manifest, canonical_manifest = self._validate_candidate_identity(request)
        runtime_instance_id = self._uuid_text(
            request.runtime_instance_id,
            "runtime_instance_id",
        )
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=request.owner_user_id)
            self._assert_draft_metadata(
                transaction,
                owner_user_id=request.owner_user_id,
                agent_id=request.agent_id,
                revision_id=request.revision_id,
                metadata=request.agent_metadata,
            )
            agent = repository.get_agent(
                transaction,
                owner_id=request.owner_user_id,
                agent_id=request.agent_id,
                for_update=True,
            )
            revision = repository.get_revision(
                transaction,
                owner_id=request.owner_user_id,
                agent_id=request.agent_id,
                revision_id=request.revision_id,
                for_update=True,
            )
            runtime = repository.get_runtime_instance(
                transaction,
                owner_id=request.owner_user_id,
                runtime_instance_id=runtime_instance_id,
                for_update=True,
            )
            if agent is None or revision is None or runtime is None:
                raise StaleRuntimeGenerationError(
                    "active revision replay authority is unavailable"
                )
            manifest = revision.manifest
            if isinstance(manifest, str):
                try:
                    manifest = json.loads(manifest)
                except (TypeError, ValueError) as exc:
                    raise StaleRuntimeGenerationError(
                        "active revision manifest is invalid"
                    ) from exc
            persisted_manifest = json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                default=_json_mapping_default,
            )
            if not (
                agent.deleted_at is None
                and agent.active_revision_id == request.revision_id
                and agent.authoritative_instance_id == runtime_instance_id
                and self._agent_matches_metadata(agent, request.agent_metadata)
                and revision.agent_id == request.agent_id
                and revision.owner_id == request.owner_user_id
                and revision.state == "active"
                and revision.compatibility_state == "compatible"
                and revision.artifact_digest == request.bundle_sha256
                and persisted_manifest == canonical_manifest
                and revision.artifact_relative_path
                == request.artifact_relative_path
                and revision.runtime_contract_version
                == request.runtime_contract_version
                and revision.release_lock_digest
                == request.required_runtime_lock_sha256
                and runtime.agent_id == request.agent_id
                and runtime.revision_id == request.revision_id
                and runtime.state == "online"
                and runtime.is_authoritative
            ):
                raise StaleRuntimeGenerationError(
                    "active revision replay identity is stale"
                )

    def inspect_recovery_status(
        self,
        owner_user_id: str,
        agent_id: str,
        revision_id: str,
    ) -> Optional[RevisionRecoveryStatus]:
        """Read the exact candidate/operation disposition under the owner lock."""

        owner_user_id = self._required_text(owner_user_id, "owner_user_id", 255)
        agent_id = self._required_text(agent_id, "agent_id", 255)
        revision_id = self._uuid_text(revision_id, "revision_id")
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            if agent is None:
                raise StaleRuntimeGenerationError("candidate agent is stale")
            if agent.deleted_at is not None:
                raise RevisionActivationError("agent_deleted")
            revision = repository.get_revision(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                revision_id=revision_id,
                for_update=True,
            )
            if revision is None:
                return None
            runtime_inventory = repository.list_runtime_instances(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                states=None,
                for_update=True,
                limit=1000,
            )
            if len(runtime_inventory) >= 1000:
                raise RevisionActivationError("runtime_inventory_too_large")
            all_runtimes = tuple(
                runtime
                for runtime in runtime_inventory
                if runtime.revision_id == revision_id
            )
            nonterminal_runtimes = tuple(
                runtime
                for runtime in all_runtimes
                if runtime.state in self._NONTERMINAL_RUNTIME_STATES
            )
            if len(nonterminal_runtimes) > 1:
                raise StaleRuntimeGenerationError(
                    "candidate revision has multiple nonterminal runtimes"
                )
            runtime = (
                None if not nonterminal_runtimes else nonterminal_runtimes[0]
            )
            operation = None
            if runtime is not None:
                operation = self._retained_runtime_operation(
                    transaction,
                    runtime,
                    owner_user_id=owner_user_id,
                )
            convergence_runtime = runtime
            if convergence_runtime is None and all_runtimes:
                latest_runtime = all_runtimes[0]
                if (
                    latest_runtime.state in self._TERMINAL_RUNTIME_STATES
                    and latest_runtime.failure_code in self._PHYSICAL_EXIT_PROOFS
                ):
                    convergence_runtime = latest_runtime
            if (
                convergence_runtime is not None
                and operation is None
                and convergence_runtime.operation_id is None
            ):
                converged = self._converge_purged_operation_failure(
                    transaction,
                    agent=agent,
                    revision=revision,
                    runtime=convergence_runtime,
                    revision_runtimes=all_runtimes,
                )
                if converged is not None:
                    runtime, revision, _requires_stop = converged
                    return RevisionRecoveryStatus(
                        owner_user_id=owner_user_id,
                        agent_id=agent_id,
                        revision_id=revision_id,
                        revision_state=revision.state,
                        runtime_instance_id=runtime.runtime_instance_id,
                        runtime_failure_code="revision_promotion_failed",
                        operation_state=None,
                        operation_terminal_code=None,
                    )
            revision_disposition = self._mutable_revision_disposition(revision)
            permanent_failure_code = (
                None
                if runtime is None
                else self._permanent_runtime_failure_code(
                    runtime,
                    operation,
                    revision,
                )
            )
            if revision.state in {"prepared", "starting", "ready"}:
                terminal_attempts: list[tuple[Any, Any | None, Optional[str]]] = []
                for terminal_runtime in all_runtimes:
                    if terminal_runtime.state not in self._TERMINAL_RUNTIME_STATES:
                        continue
                    terminal_operation = self._retained_runtime_operation(
                        transaction,
                        terminal_runtime,
                        owner_user_id=owner_user_id,
                    )
                    terminal_failure_code = self._permanent_runtime_failure_code(
                        terminal_runtime,
                        terminal_operation,
                        revision if terminal_runtime is all_runtimes[0] else None,
                    )
                    terminal_attempts.append(
                        (
                            terminal_runtime,
                            terminal_operation,
                            terminal_failure_code,
                        )
                    )
                permanent_attempts = tuple(
                    attempt for attempt in terminal_attempts if attempt[2] is not None
                )
                if permanent_attempts:
                    if (
                        len(permanent_attempts) != 1
                        or not all_runtimes
                        or permanent_attempts[0][0].runtime_instance_id
                        != all_runtimes[0].runtime_instance_id
                    ):
                        raise StaleRuntimeGenerationError(
                            "terminal candidate recovery disposition is ambiguous"
                        )
                    if runtime is not None:
                        raise StaleRuntimeGenerationError(
                            "terminal candidate recovery disposition is ambiguous"
                        )
                    runtime, operation, permanent_failure_code = permanent_attempts[0]
                elif runtime is None and terminal_attempts:
                    latest_runtime, latest_operation, _ignored = terminal_attempts[0]
                    if latest_runtime.failure_code == self._RETRY_RESET_COMPLETE:
                        latest_operation = None
                    elif revision_disposition == self._RETRY_RESET_PENDING:
                        if (
                            latest_operation is not None
                            and latest_operation.state is not OperationState.RETRYABLE
                        ):
                            raise StaleRuntimeGenerationError(
                                "terminal candidate recovery disposition is ambiguous"
                            )
                        runtime = latest_runtime
                        operation = latest_operation
                    elif latest_operation is None or latest_operation.state not in {
                        OperationState.RUNNING,
                        OperationState.RETRYABLE,
                        OperationState.FAILED,
                        OperationState.CANCELLED,
                    }:
                        raise StaleRuntimeGenerationError(
                            "terminal candidate recovery disposition is unavailable"
                        )
                    if latest_operation is not None:
                        runtime = latest_runtime
                        operation = latest_operation
            elif (
                revision.state in {"failed", "retired"}
                and revision.failure_code in self._PERMANENT_ACTIVATION_FAILURES
            ):
                permanent_failure_code = revision.failure_code
                if all_runtimes:
                    runtime = all_runtimes[0]
                    operation = self._retained_runtime_operation(
                        transaction,
                        runtime,
                        owner_user_id=owner_user_id,
                    )
                    permanent_failure_code = self._permanent_runtime_failure_code(
                        runtime,
                        operation,
                        revision,
                    )
            operation_state = None if operation is None else operation.state
            if (
                operation_state is None
                and runtime is not None
                and revision_disposition == self._RETRY_RESET_PENDING
            ):
                operation_state = OperationState.RETRYABLE
            return RevisionRecoveryStatus(
                owner_user_id=owner_user_id,
                agent_id=agent_id,
                revision_id=revision_id,
                revision_state=revision.state,
                runtime_instance_id=(
                    None if runtime is None else runtime.runtime_instance_id
                ),
                runtime_failure_code=(
                    permanent_failure_code
                ),
                operation_state=operation_state,
                operation_terminal_code=(
                    None if operation is None else operation.terminal_code
                ),
            )

    @staticmethod
    def _json_value(raw: Any, field_name: str) -> Any:
        if not isinstance(raw, str) or not raw:
            raise StaleRuntimeGenerationError(
                f"candidate draft {field_name} is unavailable"
            )
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise StaleRuntimeGenerationError(
                f"candidate draft {field_name} is invalid"
            ) from exc

    def _assert_draft_metadata(
        self,
        transaction: Any,
        *,
        owner_user_id: str,
        agent_id: str,
        revision_id: str,
        metadata: CandidateAgentMetadata,
    ) -> Any:
        """Prove candidate metadata from the exact published draft revision."""

        draft = self._drafts.get_draft(
            transaction,
            owner_id=owner_user_id,
            draft_id=metadata.draft_id,
            for_update=True,
        )
        if draft is None:
            raise StaleRuntimeGenerationError("candidate draft is unavailable")
        if not (
            draft.owner_id == owner_user_id
            and draft.target_agent_id == agent_id
            and draft.published_revision_id == revision_id
            and draft.status == "generated"
            and draft.state_revision == metadata.draft_state_revision
            and draft.agent_name == metadata.display_name
            and draft.constitution_version == metadata.constitution_version
            and draft.revises_agent_id in {None, agent_id}
        ):
            raise StaleRuntimeGenerationError(
                "candidate draft publication provenance is stale"
            )

        plan = self._json_value(draft.plan_json, "plan")
        tools_spec = self._json_value(draft.tools_spec, "tools_spec")
        analyze_result = self._json_value(draft.analyze_result, "analyze_result")
        if not (
            isinstance(plan, Mapping)
            and isinstance(tools_spec, list)
            and isinstance(analyze_result, Mapping)
            and analyze_result.get("passed") is True
            and analyze_result.get("constitution_version")
            == metadata.constitution_version
            and analyze_result.get("policy_revision")
            == metadata.validated_policy_revision
        ):
            raise StaleRuntimeGenerationError(
                "candidate draft metadata shape is invalid"
            )
        plan_tools = plan.get("tools_used")
        plan_scopes = plan.get("declared_scopes")
        plan_egress = plan.get("declared_egress")
        if plan_egress is None:
            plan_egress = []
        if not (
            isinstance(plan_tools, list)
            and isinstance(plan_scopes, list)
            and isinstance(plan_egress, list)
            and tuple(plan_tools) == metadata.declared_tools
            and tuple(plan_scopes) == metadata.declared_scopes
            and tuple(plan_egress) == metadata.declared_egress
        ):
            raise StaleRuntimeGenerationError(
                "candidate draft declared authority is stale"
            )
        tool_names: list[str] = []
        tool_scopes: dict[str, str] = {}
        for tool in tools_spec:
            if not (
                isinstance(tool, Mapping)
                and isinstance(tool.get("name"), str)
                and isinstance(tool.get("scope"), str)
            ):
                raise StaleRuntimeGenerationError(
                    "candidate draft tools_spec is invalid"
                )
            tool_names.append(tool["name"])
            tool_scopes[tool["name"]] = tool["scope"]
        plan_tool_scopes = plan.get("tool_scopes")
        from orchestrator.tool_permissions import VALID_SCOPES

        if not (
            tuple(tool_names) == metadata.declared_tools
            and isinstance(plan_tool_scopes, Mapping)
            and dict(plan_tool_scopes) == tool_scopes
            and all(scope in VALID_SCOPES for scope in tool_scopes.values())
            and set(tool_scopes.values()).issubset(metadata.declared_scopes)
        ):
            raise StaleRuntimeGenerationError(
                "candidate draft tools_spec is stale"
            )
        return draft

    @staticmethod
    def _agent_matches_metadata(agent: Any, metadata: CandidateAgentMetadata) -> bool:
        return (
            agent.display_name == metadata.display_name
            and agent.draft_id == metadata.draft_id
            and tuple(agent.declared_tools) == metadata.declared_tools
            and tuple(agent.declared_scopes) == metadata.declared_scopes
            and tuple(agent.declared_egress or ()) == metadata.declared_egress
            and agent.constitution_version == metadata.constitution_version
            and agent.validated_policy_revision
            == metadata.validated_policy_revision
            and agent.status == "live"
            and agent.validated_at is not None
            and not agent.revalidation_required
        )

    def _mark_revision_failed_only(
        self, request: CandidatePreparation, failure_code: str
    ) -> None:
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=request.owner_user_id)
            revision = repository.get_revision(
                transaction,
                owner_id=request.owner_user_id,
                agent_id=request.agent_id,
                revision_id=request.revision_id,
                for_update=True,
            )
            if revision is None or revision.state not in {
                "prepared",
                "starting",
                "ready",
            }:
                return
            try:
                repository.transition_revision(
                    transaction,
                    owner_id=request.owner_user_id,
                    agent_id=request.agent_id,
                    revision_id=request.revision_id,
                    expected_revision=revision.state_revision,
                    expected_state=revision.state,
                    updates={
                        "state": "failed",
                        "failed_at": datetime.now(UTC),
                        "failure_code": failure_code,
                    },
                )
            except RepositoryConflictError:
                return

    def _recover_committed_prelaunch_instance(
        self, request: CandidatePreparation
    ) -> Optional[str]:
        """Recover an exact runtime when its create commit acknowledgement was lost.

        ``create_prelaunch_instance`` owns a separate Plane transaction from the
        immutable revision insert.  A transport/driver error can therefore be
        raised after that transaction committed.  Only the sole nonterminal row
        bound to this exact host and delivery-operation fence is valid evidence
        that the create succeeded; every other durable shape fails closed.
        """

        operation_id = str(request.operation_fence.operation_id)
        operation_generation = request.operation_fence.execution_generation
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=request.owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=request.owner_user_id,
                agent_id=request.agent_id,
                for_update=True,
            )
            revision = repository.get_revision(
                transaction,
                owner_id=request.owner_user_id,
                agent_id=request.agent_id,
                revision_id=request.revision_id,
                for_update=True,
            )
            if agent is None or revision is None:
                raise StaleRuntimeGenerationError(
                    "candidate prelaunch recovery identity is stale"
                )
            if agent.deleted_at is not None:
                raise RevisionActivationError("agent_deleted")
            runtime_inventory = repository.list_runtime_instances(
                transaction,
                owner_id=request.owner_user_id,
                agent_id=request.agent_id,
                states=None,
                for_update=True,
                limit=1000,
            )
            if len(runtime_inventory) >= 1000:
                raise RevisionActivationError("runtime_inventory_too_large")
            runtimes = tuple(
                runtime
                for runtime in runtime_inventory
                if runtime.revision_id == request.revision_id
            )
            matching = tuple(
                runtime
                for runtime in runtimes
                if runtime.host_session_id == request.host_session_id
                and runtime.operation_id == operation_id
                and runtime.operation_execution_generation == operation_generation
            )
            nonterminal = tuple(
                runtime
                for runtime in runtimes
                if runtime.state in self._NONTERMINAL_RUNTIME_STATES
            )
            if not matching:
                if nonterminal:
                    raise RevisionActivationRecoveryPendingError(
                        "revision_runtime_cleanup_pending"
                    )
                return None
            if len(matching) != 1 or matching[0] not in nonterminal:
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
            runtime = matching[0]
            operation = self._runtime_operation(
                transaction,
                runtime,
                owner_user_id=request.owner_user_id,
            )
            if len(nonterminal) != 1 or not (
                agent.selected_host_session_id == request.host_session_id
                and agent.active_revision_id != request.revision_id
                and agent.authoritative_instance_id != runtime.runtime_instance_id
                and revision.state in {"prepared", "starting", "ready"}
                and runtime.owner_id == request.owner_user_id
                and runtime.agent_id == request.agent_id
                and runtime.revision_id == request.revision_id
                and not runtime.is_authoritative
                and operation.state is OperationState.RUNNING
                and operation.execution_lease_token
                == request.operation_fence.execution_lease_token
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
            return runtime.runtime_instance_id

    def prepare_candidate(self, request: CandidatePreparation) -> CandidateRevision:
        """Insert immutable revision metadata, then reserve its runtime fence."""

        _manifest, canonical_manifest = self._validate_preparation(request)
        promotion_token: Optional[str] = None
        previous_revision_id: Optional[str] = None
        previous_runtime_id: Optional[str] = None
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=request.owner_user_id)
            self._assert_draft_metadata(
                transaction,
                owner_user_id=request.owner_user_id,
                agent_id=request.agent_id,
                revision_id=request.revision_id,
                metadata=request.agent_metadata,
            )
            agent = repository.get_agent(
                transaction,
                owner_id=request.owner_user_id,
                agent_id=request.agent_id,
                for_update=True,
            )
            if agent is None:
                raise StaleRuntimeGenerationError("candidate agent is stale")
            if agent.deleted_at is not None:
                raise RevisionActivationError("agent_deleted")
            if agent.selected_host_session_id != request.host_session_id:
                raise StaleRuntimeGenerationError(
                    "selected host session is stale"
                )
            host = repository.get_host_session(
                transaction,
                owner_id=request.owner_user_id,
                host_session_id=request.host_session_id,
                for_update=True,
            )
            if not (
                host is not None
                and host.owner_id == request.owner_user_id
                and host.state == "connected"
            ):
                raise StaleRuntimeGenerationError(
                    "selected host session is stale"
                )
            if host.inventory_state != "reconciled":
                raise RevisionActivationError("inventory_required")
            previous_revision_id = agent.active_revision_id
            previous_runtime_id = agent.authoritative_instance_id
            revision = repository.get_revision(
                transaction,
                owner_id=request.owner_user_id,
                agent_id=request.agent_id,
                revision_id=request.revision_id,
                for_update=True,
            )
            if revision is None:
                latest = repository.list_revisions(
                    transaction,
                    owner_id=request.owner_user_id,
                    agent_id=request.agent_id,
                    limit=1,
                )
                revision_number = (
                    0 if not latest else latest[0].revision_number + 1
                )
                promotion_token = self._runtime._new_uuid("promotion_token")
                try:
                    revision = repository.create_revision(
                        transaction,
                        revision_id=request.revision_id,
                        agent_id=request.agent_id,
                        owner_id=request.owner_user_id,
                        revision_number=revision_number,
                        parent_revision_id=previous_revision_id,
                        previous_good_revision_id=previous_revision_id,
                        artifact_digest=request.bundle_sha256,
                        manifest=json.loads(canonical_manifest),
                        artifact_relative_path=request.artifact_relative_path,
                        runtime_contract_version=request.runtime_contract_version,
                        release_lock_digest=(
                            request.required_runtime_lock_sha256
                        ),
                        compatibility_state="compatible",
                        state="prepared",
                        promotion_token=promotion_token,
                    )
                except RepositoryConflictError as exc:
                    raise StaleRuntimeGenerationError(
                        "revision identity is already bound to different bytes"
                    ) from exc
            else:
                manifest = revision.manifest
                if isinstance(manifest, str):
                    manifest = json.loads(manifest)
                persisted_manifest = json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                    default=_json_mapping_default,
                )
                if not (
                    revision.agent_id == request.agent_id
                    and revision.owner_id == request.owner_user_id
                    and revision.artifact_digest == request.bundle_sha256
                    and persisted_manifest == canonical_manifest
                    and revision.artifact_relative_path
                    == request.artifact_relative_path
                    and revision.runtime_contract_version
                    == request.runtime_contract_version
                    and revision.release_lock_digest
                    == request.required_runtime_lock_sha256
                    and revision.state in {"prepared", "starting", "ready"}
                ):
                    raise StaleRuntimeGenerationError(
                        "revision identity is already bound to different bytes"
                    )
                promotion_token = revision.promotion_token
                previous_revision_id = revision.previous_good_revision_id

            runtimes = repository.list_runtime_instances(
                transaction,
                owner_id=request.owner_user_id,
                agent_id=request.agent_id,
                states=(
                    "delivering",
                    "starting",
                    "ready",
                    "online",
                    "updating",
                    "stopping",
                ),
                for_update=True,
                limit=1000,
            )
            existing_runtimes = tuple(
                runtime
                for runtime in runtimes
                if runtime.revision_id == request.revision_id
            )
            if len(existing_runtimes) > 1:
                raise StaleRuntimeGenerationError(
                    "candidate revision has multiple nonterminal runtimes"
                )
            existing_runtime = (
                None if not existing_runtimes else existing_runtimes[0]
            )
            if existing_runtime is None and len(runtimes) >= 1000:
                raise RevisionActivationError("runtime_inventory_too_large")
            if existing_runtime is not None:
                operation = self._runtime_operation(
                    transaction,
                    existing_runtime,
                    owner_user_id=request.owner_user_id,
                )
                if not (
                    existing_runtime.host_session_id == request.host_session_id
                    and existing_runtime.operation_id
                    == str(request.operation_fence.operation_id)
                    and existing_runtime.operation_execution_generation
                    == request.operation_fence.execution_generation
                    and not existing_runtime.is_authoritative
                    and operation.state is OperationState.RUNNING
                    and operation.execution_lease_token
                    == request.operation_fence.execution_lease_token
                ):
                    raise RevisionActivationRecoveryPendingError(
                        "revision_runtime_cleanup_pending"
                    )

        if existing_runtime is None:
            try:
                runtime = self._runtime.create_prelaunch_instance(
                    owner_user_id=request.owner_user_id,
                    agent_id=request.agent_id,
                    host_session_id=request.host_session_id,
                    revision_id=request.revision_id,
                    operation_fence=request.operation_fence,
                )
            except Exception:
                recovered_runtime_id = self._recover_committed_prelaunch_instance(
                    request
                )
                if recovered_runtime_id is None:
                    self._mark_revision_failed_only(request, "bundle_install_failed")
                    raise
                runtime_instance_id = recovered_runtime_id
            else:
                runtime_instance_id = runtime.fence.runtime_instance_id
        else:
            runtime_instance_id = existing_runtime.runtime_instance_id
        if promotion_token is None:
            raise StaleRuntimeGenerationError(
                "candidate promotion token is unavailable"
            )
        return CandidateRevision(
            owner_user_id=request.owner_user_id,
            agent_id=request.agent_id,
            revision_id=request.revision_id,
            promotion_token=promotion_token,
            runtime_instance_id=runtime_instance_id,
            previous_active_revision_id=previous_revision_id,
            previous_runtime_instance_id=previous_runtime_id,
            agent_metadata=request.agent_metadata,
        )

    def mark_candidate_starting(self, candidate: CandidateRevision) -> None:
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=candidate.owner_user_id)
            revision = repository.get_revision(
                transaction,
                owner_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
                revision_id=candidate.revision_id,
                for_update=True,
            )
            if (
                revision is None
                or revision.promotion_token != candidate.promotion_token
            ):
                raise StaleRuntimeGenerationError(
                    "candidate starting transition is stale"
                )
            if revision.state in {"starting", "ready", "active"}:
                return
            if revision.state != "prepared":
                raise StaleRuntimeGenerationError(
                    "candidate starting transition is stale"
                )
            try:
                repository.transition_revision(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    agent_id=candidate.agent_id,
                    revision_id=candidate.revision_id,
                    expected_revision=revision.state_revision,
                    expected_state="prepared",
                    updates={"state": "starting"},
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "candidate starting transition is stale"
                ) from exc

    def confirm_candidate_ready(
        self, candidate: CandidateRevision, ready_runtime_instance_id: str
    ) -> CandidateRevision:
        ready_runtime_instance_id = self._uuid_text(
            ready_runtime_instance_id, "ready_runtime_instance_id"
        )
        if ready_runtime_instance_id != candidate.runtime_instance_id:
            raise StaleRuntimeGenerationError("ready runtime identity is stale")
        runtime = self._runtime.get_runtime_instance(ready_runtime_instance_id)
        if not (
            runtime.state == "ready"
            and runtime.fence.revision_id == candidate.revision_id
            and runtime.fence.agent_id == candidate.agent_id
        ):
            raise StaleRuntimeGenerationError("candidate runtime is not ready")
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=candidate.owner_user_id)
            revision = repository.get_revision(
                transaction,
                owner_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
                revision_id=candidate.revision_id,
                for_update=True,
            )
            if (
                revision is None
                or revision.promotion_token != candidate.promotion_token
            ):
                raise StaleRuntimeGenerationError(
                    "candidate ready transition is stale"
                )
            if revision.state in {"ready", "active"}:
                return candidate
            if revision.state not in {"prepared", "starting"}:
                raise StaleRuntimeGenerationError(
                    "candidate ready transition is stale"
                )
            try:
                repository.transition_revision(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    agent_id=candidate.agent_id,
                    revision_id=candidate.revision_id,
                    expected_revision=revision.state_revision,
                    expected_state=revision.state,
                    updates={
                        "state": "ready",
                        "confirmed_at": datetime.now(UTC),
                    },
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "candidate ready transition is stale"
                ) from exc
        return candidate

    def promote_candidate(self, candidate: CandidateRevision) -> PromotionCommit:
        """Atomically move every authoritative pointer to one ready candidate."""

        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=candidate.owner_user_id)
            self._assert_draft_metadata(
                transaction,
                owner_user_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
                revision_id=candidate.revision_id,
                metadata=candidate.agent_metadata,
            )
            agent = repository.get_agent(
                transaction,
                owner_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
                for_update=True,
            )
            revision = repository.get_revision(
                transaction,
                owner_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
                revision_id=candidate.revision_id,
                for_update=True,
            )
            runtime = repository.get_runtime_instance(
                transaction,
                owner_id=candidate.owner_user_id,
                runtime_instance_id=candidate.runtime_instance_id,
                for_update=True,
            )
            if agent is None or revision is None or runtime is None:
                raise StaleRuntimeGenerationError("candidate promotion fence is stale")
            if agent.deleted_at is not None:
                raise RevisionActivationError("agent_deleted")

            active_revision = agent.active_revision_id
            authoritative_runtime = agent.authoritative_instance_id
            if (
                active_revision == candidate.revision_id
                and authoritative_runtime == candidate.runtime_instance_id
                and revision.state == "active"
                and revision.previous_good_revision_id
                == candidate.previous_active_revision_id
                and runtime.state == "online"
                and runtime.is_authoritative
                and self._agent_matches_metadata(
                    agent,
                    candidate.agent_metadata,
                )
            ):
                try:
                    replay_operation = self._runtime._assert_runtime_operation_plane(
                        transaction, runtime
                    )
                except StaleRuntimeGenerationError:
                    replay_operation = None
                if replay_operation is not None:
                    self._runtime._operations.terminalize(
                        replay_operation,
                        state=OperationState.COMPLETED,
                        terminal_code=None,
                        safe_summary=None,
                        retry_after_ms=None,
                        now=None,
                        retention=self._runtime._operation_retention,
                        transaction=transaction,
                    )
                return PromotionCommit(
                    owner_user_id=candidate.owner_user_id,
                    agent_id=candidate.agent_id,
                    revision_id=candidate.revision_id,
                    runtime_instance_id=candidate.runtime_instance_id,
                    previous_revision_id=revision.previous_good_revision_id,
                    previous_runtime_instance_id=(
                        candidate.previous_runtime_instance_id
                    ),
                )
            if not (
                active_revision == candidate.previous_active_revision_id
                and authoritative_runtime
                == candidate.previous_runtime_instance_id
                and revision.agent_id == candidate.agent_id
                and revision.owner_id == candidate.owner_user_id
                and revision.promotion_token == candidate.promotion_token
                and revision.state == "ready"
                and runtime.agent_id == candidate.agent_id
                and runtime.owner_id == candidate.owner_user_id
                and runtime.revision_id == candidate.revision_id
                and runtime.state == "ready"
                and not runtime.is_authoritative
                and agent.selected_host_session_id == runtime.host_session_id
            ):
                raise StaleRuntimeGenerationError("candidate promotion fence is stale")
            operation_fence = self._runtime._assert_runtime_operation_plane(
                transaction, runtime
            )

            if authoritative_runtime is not None:
                previous_runtime = repository.get_runtime_instance(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    runtime_instance_id=authoritative_runtime,
                    for_update=True,
                )
                if previous_runtime is None or not previous_runtime.is_authoritative:
                    raise StaleRuntimeGenerationError(
                        "previous runtime authority is stale"
                    )
                previous_updates: dict[str, object] = {
                    "is_authoritative": False,
                    "failure_code": "revision_promotion_failed",
                }
                if previous_runtime.state not in {
                    "stopped",
                    "failed",
                    "offline",
                    "superseded",
                }:
                    previous_updates["state"] = "stopping"
                repository.transition_runtime_instance(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    runtime_instance_id=authoritative_runtime,
                    expected_revision=previous_runtime.state_revision,
                    expected_states=(previous_runtime.state,),
                    updates=previous_updates,
                )
            try:
                repository.transition_runtime_instance(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    runtime_instance_id=candidate.runtime_instance_id,
                    expected_revision=runtime.state_revision,
                    expected_states=("ready",),
                    updates={"state": "online", "is_authoritative": True},
                )
                repository.transition_revision(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    agent_id=candidate.agent_id,
                    revision_id=candidate.revision_id,
                    expected_revision=revision.state_revision,
                    expected_state="ready",
                    updates={
                        "state": "active",
                        "promoted_at": datetime.now(UTC),
                    },
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "candidate promotion transition is stale"
                ) from exc
            if active_revision is not None:
                previous_revision = repository.get_revision(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    agent_id=candidate.agent_id,
                    revision_id=active_revision,
                    for_update=True,
                )
                if previous_revision is None or previous_revision.state != "active":
                    raise StaleRuntimeGenerationError(
                        "previous revision authority is stale"
                    )
                repository.transition_revision(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    agent_id=candidate.agent_id,
                    revision_id=active_revision,
                    expected_revision=previous_revision.state_revision,
                    expected_state="active",
                    updates={"state": "retired"},
                )
            try:
                metadata = candidate.agent_metadata
                now_ms = int(datetime.now(UTC).timestamp() * 1000)
                repository.compare_and_set_agent(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    agent_id=candidate.agent_id,
                    expected_revision=agent.state_revision,
                    updates={
                        "display_name": metadata.display_name,
                        "draft_id": metadata.draft_id,
                        "declared_tools": metadata.declared_tools,
                        "declared_scopes": metadata.declared_scopes,
                        "declared_egress": metadata.declared_egress or None,
                        "constitution_version": metadata.constitution_version,
                        "validated_policy_revision": (
                            metadata.validated_policy_revision
                        ),
                        "validated_at": now_ms,
                        "revalidation_required": False,
                        "active_revision_id": candidate.revision_id,
                        "last_known_good_revision_id": active_revision,
                        "authoritative_instance_id": (
                            candidate.runtime_instance_id
                        ),
                        "lifecycle_generation": runtime.lifecycle_generation,
                        "status": "live",
                        "updated_at": now_ms,
                    },
                )
                self._runtime._operations.terminalize(
                    operation_fence,
                    state=OperationState.COMPLETED,
                    terminal_code=None,
                    safe_summary=None,
                    retry_after_ms=None,
                    now=None,
                    retention=self._runtime._operation_retention,
                    transaction=transaction,
                )
            except (RepositoryConflictError, StaleExecutionFenceError) as exc:
                raise StaleRuntimeGenerationError(
                    "candidate authority commit is stale"
                ) from exc

        return PromotionCommit(
            owner_user_id=candidate.owner_user_id,
            agent_id=candidate.agent_id,
            revision_id=candidate.revision_id,
            runtime_instance_id=candidate.runtime_instance_id,
            previous_revision_id=active_revision,
            previous_runtime_instance_id=authoritative_runtime,
        )

    def _runtime_operation(
        self,
        transaction: Any,
        runtime: Any,
        *,
        owner_user_id: str,
    ) -> Any:
        """Load and authenticate the delivery operation bound to one runtime."""

        if runtime.operation_id is None:
            raise StaleRuntimeGenerationError(
                "candidate delivery operation is unavailable"
            )
        operation = self._runtime._operations.get_operation_for_administration(
            uuid.UUID(runtime.operation_id),
            for_update=True,
            transaction=transaction,
        )
        if not (
            operation is not None
            and operation.owner_user_id == owner_user_id
            and operation.operation_kind == "agent_runtime_delivery"
            and operation.execution_generation
            == runtime.operation_execution_generation
        ):
            raise StaleRuntimeGenerationError(
                "candidate delivery operation identity is stale"
            )
        return operation

    def _retained_runtime_operation(
        self,
        transaction: Any,
        runtime: Any,
        *,
        owner_user_id: str,
    ) -> Any | None:
        """Load a delivery operation when its retention FK still exists.

        Plane intentionally clears ``agent_runtime_instance.operation_id`` when
        the terminal operation reaches its retention deadline.  Recovery must
        therefore authenticate a retained operation when present, while using
        the independently persisted revision disposition after that purge.
        """

        if runtime.operation_id is None:
            return None
        return self._runtime_operation(
            transaction,
            runtime,
            owner_user_id=owner_user_id,
        )

    def _mutable_revision_disposition(self, revision: Any) -> Optional[str]:
        """Validate the self-contained semantic marker on a mutable revision."""

        if revision.state not in {"prepared", "starting", "ready"}:
            return None
        disposition = revision.failure_code
        if disposition is None:
            return None
        if disposition in {
            *self._PERMANENT_ACTIVATION_FAILURES,
            self._RETRY_RESET_PENDING,
        }:
            return disposition
        raise StaleRuntimeGenerationError(
            "candidate revision disposition is invalid"
        )

    def _permanent_runtime_failure_code(
        self,
        runtime: Any,
        operation: Any | None,
        revision: Any | None = None,
    ) -> Optional[str]:
        """Recover one unambiguous permanent activation disposition."""

        operation_code = (
            operation.terminal_code
            if operation is not None
            and operation.state is OperationState.FAILED
            else None
        )
        revision_code = None
        if revision is not None:
            if revision.state in {"prepared", "starting", "ready"}:
                self._mutable_revision_disposition(revision)
            if revision.failure_code in self._PERMANENT_ACTIVATION_FAILURES:
                revision_code = revision.failure_code
        candidates = {
            candidate
            for candidate in (
                operation_code,
                revision_code,
                runtime.failure_code,
            )
            if candidate in self._PERMANENT_ACTIVATION_FAILURES
        }
        if len(candidates) > 1:
            raise StaleRuntimeGenerationError(
                "candidate permanent failure disposition is ambiguous"
            )
        return next(iter(candidates), None)

    def _converge_purged_operation_failure(
        self,
        transaction: Any,
        *,
        agent: Any,
        revision: Any,
        runtime: Any,
        revision_runtimes: Sequence[Any],
    ) -> Optional[tuple[Any, Any, bool]]:
        """Fail closed when terminal-operation retention won the crash race.

        A terminal delivery operation may be purged before Deep persists its
        semantic marker on the mutable revision.  The absent FK proves neither
        RETRYABLE nor a particular terminal code, so recovery converges only an
        exact latest attempt whose authority and history are unambiguous.  The
        returned flag names a process-bearing runtime that still needs exact
        physical-exit proof before revision finalization.
        """

        if not (
            revision.state in {"prepared", "starting", "ready"}
            and revision.failure_code is None
            and runtime.operation_id is None
            and type(runtime.operation_execution_generation) is int
            and runtime.operation_execution_generation > 0
            and not runtime.is_authoritative
            and agent.active_revision_id != revision.revision_id
            and agent.authoritative_instance_id != runtime.runtime_instance_id
            and revision_runtimes
            and revision_runtimes[0].runtime_instance_id
            == runtime.runtime_instance_id
        ):
            return None

        unfinished_attempts = tuple(
            candidate
            for candidate in revision_runtimes
            if candidate.failure_code != self._RETRY_RESET_COMPLETE
        )
        if not (
            len(unfinished_attempts) == 1
            and unfinished_attempts[0].runtime_instance_id
            == runtime.runtime_instance_id
        ):
            return None
        for completed_attempt in revision_runtimes[1:]:
            if not (
                completed_attempt.state in self._TERMINAL_RUNTIME_STATES
                and completed_attempt.failure_code == self._RETRY_RESET_COMPLETE
            ):
                return None
            completed_operation = self._retained_runtime_operation(
                transaction,
                completed_attempt,
                owner_user_id=agent.owner_id,
            )
            if (
                completed_operation is not None
                and completed_operation.state is not OperationState.RETRYABLE
            ):
                return None

        processless_prelaunch = (
            runtime.process_id is None
            and runtime.state == "delivering"
            and runtime.failure_code is None
        )
        process_bound = (
            runtime.process_id is not None
            and runtime.state in self._NONTERMINAL_RUNTIME_STATES
            and runtime.failure_code is None
        )
        retained_physical_proof = (
            runtime.process_id is not None
            and runtime.state in self._TERMINAL_RUNTIME_STATES
            and runtime.failure_code in self._PHYSICAL_EXIT_PROOFS
        )
        if not (
            processless_prelaunch or process_bound or retained_physical_proof
        ):
            return None

        repository = self._runtime._agents.repository
        failure_code = "revision_promotion_failed"
        observed_at = datetime.now(UTC)
        try:
            if process_bound:
                updated_revision = repository.transition_revision(
                    transaction,
                    owner_id=agent.owner_id,
                    agent_id=agent.agent_id,
                    revision_id=revision.revision_id,
                    expected_revision=revision.state_revision,
                    expected_state=revision.state,
                    updates={"failure_code": failure_code},
                )
                updated_runtime = repository.transition_runtime_instance(
                    transaction,
                    owner_id=agent.owner_id,
                    runtime_instance_id=runtime.runtime_instance_id,
                    expected_revision=runtime.state_revision,
                    expected_states=(runtime.state,),
                    updates={
                        "state": "stopping",
                        "is_authoritative": False,
                        "failure_code": failure_code,
                    },
                )
                return updated_runtime, updated_revision, True

            if processless_prelaunch:
                self._runtime._terminalize_instance_plane(
                    transaction,
                    runtime,
                    agent,
                    failure_code=failure_code,
                    operation_state=OperationState.FAILED,
                )
                updated_runtime = repository.get_runtime_instance(
                    transaction,
                    owner_id=agent.owner_id,
                    runtime_instance_id=runtime.runtime_instance_id,
                    for_update=True,
                )
                if updated_runtime is None:  # pragma: no cover - same transaction
                    raise RepositoryConflictError(
                        "purged-operation runtime disappeared"
                    )
            else:
                updated_runtime = repository.transition_runtime_instance(
                    transaction,
                    owner_id=agent.owner_id,
                    runtime_instance_id=runtime.runtime_instance_id,
                    expected_revision=runtime.state_revision,
                    expected_states=(runtime.state,),
                    updates={"failure_code": failure_code},
                )
            updated_revision = repository.transition_revision(
                transaction,
                owner_id=agent.owner_id,
                agent_id=agent.agent_id,
                revision_id=revision.revision_id,
                expected_revision=revision.state_revision,
                expected_state=revision.state,
                updates={
                    "state": "failed",
                    "failed_at": observed_at,
                    "failure_code": failure_code,
                },
            )
        except RepositoryConflictError as exc:
            raise StaleRuntimeGenerationError(
                "purged candidate operation convergence is stale"
            ) from exc
        return updated_runtime, updated_revision, False

    def _terminal_candidate_failure_code(
        self,
        runtime: Any,
        operation: Any | None,
        revision: Any | None = None,
    ) -> Optional[str]:
        """Return the immutable failure disposition for a terminal attempt."""

        permanent_failure_code = self._permanent_runtime_failure_code(
            runtime, operation, revision
        )
        if permanent_failure_code is not None:
            return permanent_failure_code
        if operation is None or operation.state not in {
            OperationState.FAILED,
            OperationState.CANCELLED,
        }:
            return None
        for candidate in (
            operation.terminal_code,
            runtime.failure_code,
            "revision_promotion_failed",
        ):
            if (
                isinstance(candidate, str)
                and self._SAFE_FAILURE.fullmatch(candidate) is not None
            ):
                return candidate
        return None

    def stage_candidate_failure(
        self, candidate: CandidateRevision, failure_code: str
    ) -> bool:
        """Persist cleanup intent without terminalizing a possibly-live child.

        The returned flag is true only when Plane has an exact process identity
        that must be stopped. Runtime and revision terminal state is deliberately
        deferred until :meth:`fail_candidate`. The exact delivery operation is
        failed in this staging transaction so lease expiry or an outer retryable
        settlement cannot erase a known-permanent activation disposition.
        """

        if (
            self._SAFE_FAILURE.fullmatch(failure_code or "") is None
            or failure_code not in self._PERMANENT_ACTIVATION_FAILURES
        ):
            raise ValueError("candidate permanent failure code is invalid")
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=candidate.owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
                for_update=True,
            )
            if agent is None:
                raise StaleRuntimeGenerationError("candidate agent is stale")
            if (
                agent.active_revision_id == candidate.revision_id
                or agent.authoritative_instance_id == candidate.runtime_instance_id
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_promotion_recovery_pending"
                )
            runtime = repository.get_runtime_instance(
                transaction,
                owner_id=candidate.owner_user_id,
                runtime_instance_id=candidate.runtime_instance_id,
                for_update=True,
            )
            revision = repository.get_revision(
                transaction,
                owner_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
                revision_id=candidate.revision_id,
                for_update=True,
            )
            if runtime is None or revision is None:
                raise StaleRuntimeGenerationError("candidate cleanup fence is stale")
            if not (
                runtime.agent_id == candidate.agent_id
                and runtime.owner_id == candidate.owner_user_id
                and runtime.revision_id == candidate.revision_id
                and not runtime.is_authoritative
                and revision.agent_id == candidate.agent_id
                and revision.owner_id == candidate.owner_user_id
                and revision.promotion_token == candidate.promotion_token
            ):
                raise StaleRuntimeGenerationError("candidate cleanup fence is stale")
            revision_disposition = self._mutable_revision_disposition(revision)
            if revision_disposition not in {None, failure_code}:
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
            operation = self._retained_runtime_operation(
                transaction,
                runtime,
                owner_user_id=candidate.owner_user_id,
            )
            terminal_runtime = runtime.state in self._TERMINAL_RUNTIME_STATES
            permanent_failure_code = self._permanent_runtime_failure_code(
                runtime,
                operation,
                revision,
            )
            if revision.state == "failed" and terminal_runtime:
                if operation is not None and operation.state not in {
                        OperationState.FAILED,
                        OperationState.CANCELLED,
                    }:
                    raise RevisionActivationRecoveryPendingError(
                        "revision_runtime_cleanup_pending"
                    )
                return False
            if revision.state not in {"prepared", "starting", "ready"}:
                raise StaleRuntimeGenerationError("candidate cleanup fence is stale")
            if operation is None and permanent_failure_code != failure_code:
                raise StaleRuntimeGenerationError(
                    "candidate delivery operation is unavailable"
                )
            if (
                operation is not None
                and operation.state is OperationState.RETRYABLE
                and permanent_failure_code != failure_code
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_retry_reset_required"
                )
            if operation is not None and operation.state not in {
                    OperationState.RUNNING,
                    OperationState.RETRYABLE,
                    OperationState.FAILED,
                    OperationState.CANCELLED,
                }:
                raise StaleRuntimeGenerationError(
                    "candidate delivery operation is not fail-safe"
                )
            if terminal_runtime:
                if permanent_failure_code != failure_code:
                    raise RevisionActivationRecoveryPendingError(
                        "revision_runtime_cleanup_pending"
                    )
                return False
            try:
                if revision.failure_code != failure_code:
                    repository.transition_revision(
                        transaction,
                        owner_id=candidate.owner_user_id,
                        agent_id=candidate.agent_id,
                        revision_id=candidate.revision_id,
                        expected_revision=revision.state_revision,
                        expected_state=revision.state,
                        updates={"failure_code": failure_code},
                    )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "candidate cleanup disposition is stale"
                ) from exc

            updates: dict[str, object] = {"failure_code": failure_code}
            requires_stop = runtime.process_id is not None
            if requires_stop:
                updates.update({"state": "stopping", "is_authoritative": False})
            try:
                repository.transition_runtime_instance(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    runtime_instance_id=candidate.runtime_instance_id,
                    expected_revision=runtime.state_revision,
                    expected_states=(runtime.state,),
                    updates=updates,
                )
                if operation is not None and operation.state is OperationState.RUNNING:
                    operation_fence = (
                        self._runtime._assert_runtime_operation_plane(
                            transaction, runtime
                        )
                    )
                    self._runtime._operations.terminalize(
                        operation_fence,
                        state=OperationState.FAILED,
                        terminal_code=failure_code,
                        safe_summary=None,
                        retry_after_ms=None,
                        now=None,
                        retention=self._runtime._operation_retention,
                        transaction=transaction,
                    )
            except (RepositoryConflictError, StaleExecutionFenceError) as exc:
                raise StaleRuntimeGenerationError(
                    "candidate cleanup staging is stale"
                ) from exc
            return requires_stop

    def fail_candidate(
        self, candidate: CandidateRevision, failure_code: str
    ) -> None:
        """Finalize a staged candidate only after exact physical cleanup."""

        if (
            self._SAFE_FAILURE.fullmatch(failure_code or "") is None
            or failure_code not in self._PERMANENT_ACTIVATION_FAILURES
        ):
            raise ValueError("candidate permanent failure code is invalid")
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=candidate.owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
                for_update=True,
            )
            if agent is None:
                raise StaleRuntimeGenerationError("candidate agent is stale")
            if (
                agent.active_revision_id == candidate.revision_id
                or agent.authoritative_instance_id == candidate.runtime_instance_id
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_promotion_recovery_pending"
                )
            runtime = repository.get_runtime_instance(
                transaction,
                owner_id=candidate.owner_user_id,
                runtime_instance_id=candidate.runtime_instance_id,
                for_update=True,
            )
            revision = repository.get_revision(
                transaction,
                owner_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
                revision_id=candidate.revision_id,
                for_update=True,
            )
            if runtime is None or revision is None:
                raise StaleRuntimeGenerationError("candidate cleanup fence is stale")
            if not (
                runtime.agent_id == candidate.agent_id
                and runtime.owner_id == candidate.owner_user_id
                and runtime.revision_id == candidate.revision_id
                and not runtime.is_authoritative
                and revision.agent_id == candidate.agent_id
                and revision.owner_id == candidate.owner_user_id
                and revision.promotion_token == candidate.promotion_token
            ):
                raise StaleRuntimeGenerationError("candidate cleanup fence is stale")
            self._mutable_revision_disposition(revision)
            operation = self._retained_runtime_operation(
                transaction,
                runtime,
                owner_user_id=candidate.owner_user_id,
            )
            terminal_runtime = runtime.state in self._TERMINAL_RUNTIME_STATES
            permanent_failure_code = self._permanent_runtime_failure_code(
                runtime,
                operation,
                revision,
            )
            if permanent_failure_code != failure_code:
                raise StaleRuntimeGenerationError("candidate cleanup fence is stale")
            if revision.state == "failed" and terminal_runtime:
                if operation is not None and operation.state not in {
                        OperationState.FAILED,
                        OperationState.CANCELLED,
                    }:
                    raise RevisionActivationRecoveryPendingError(
                        "revision_runtime_cleanup_pending"
                    )
                return
            if revision.state not in {"prepared", "starting", "ready"}:
                raise StaleRuntimeGenerationError("candidate cleanup fence is stale")
            if (
                operation is not None
                and operation.state is OperationState.RETRYABLE
                and permanent_failure_code is None
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_retry_reset_required"
                )
            if not terminal_runtime:
                self._runtime._terminalize_instance_plane(
                    transaction,
                    runtime,
                    agent,
                    failure_code=failure_code,
                    operation_state=OperationState.FAILED,
                )
            elif operation is not None and operation.state not in {
                    OperationState.FAILED,
                    OperationState.CANCELLED,
                    OperationState.RETRYABLE,
                }:
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
            try:
                repository.transition_revision(
                    transaction,
                    owner_id=candidate.owner_user_id,
                    agent_id=candidate.agent_id,
                    revision_id=candidate.revision_id,
                    expected_revision=revision.state_revision,
                    expected_state=revision.state,
                    updates={
                        "state": "failed",
                        "failed_at": datetime.now(UTC),
                        "failure_code": failure_code,
                    },
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "candidate failure transition is stale"
                ) from exc

    def recovery_plan(self, owner_user_id: str, agent_id: str) -> RecoveryPlan:
        """Stage durable cleanup debt without claiming a process has stopped."""

        owner_user_id = self._required_text(owner_user_id, "owner_user_id", 255)
        agent_id = self._required_text(agent_id, "agent_id", 255)
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            if agent is None:
                raise StaleRuntimeGenerationError("candidate agent is stale")
            if agent.deleted_at is not None:
                raise RevisionActivationError("agent_deleted")
            active_revision = agent.active_revision_id
            authoritative_runtime = agent.authoritative_instance_id
            runtimes = list(
                repository.list_runtime_instances(
                    transaction,
                    owner_id=owner_user_id,
                    agent_id=agent_id,
                    states=None,
                    for_update=True,
                    limit=1000,
                )
            )
            if len(runtimes) >= 1000:
                raise RevisionActivationError("runtime_inventory_too_large")
            authoritative = next(
                (
                    runtime
                    for runtime in runtimes
                    if authoritative_runtime is not None
                    and runtime.runtime_instance_id == authoritative_runtime
                    and active_revision is not None
                    and runtime.revision_id == active_revision
                    and runtime.state == "online"
                    and runtime.is_authoritative
                ),
                None,
            )
            keep_runtime_id = (
                None
                if authoritative is None
                else authoritative.runtime_instance_id
            )
            stop_runtime_ids: list[str] = []
            finalize_runtime_ids: list[str] = []
            retry_reset_candidates: list[tuple[str, str]] = []
            converged_revision_ids: set[str] = set()
            nonterminal_revision_ids = {
                runtime.revision_id
                for runtime in runtimes
                if runtime.state in self._NONTERMINAL_RUNTIME_STATES
            }
            if any(
                sum(
                    1
                    for runtime in runtimes
                    if runtime.revision_id == revision_id
                    and runtime.state in self._NONTERMINAL_RUNTIME_STATES
                )
                > 1
                for revision_id in nonterminal_revision_ids
            ):
                raise StaleRuntimeGenerationError(
                    "candidate revision has multiple nonterminal runtimes"
                )
            observed_terminal_revisions: set[str] = set()
            for runtime in runtimes:
                if runtime.runtime_instance_id == keep_runtime_id:
                    continue
                if runtime.revision_id in converged_revision_ids:
                    continue
                revision = repository.get_revision(
                    transaction,
                    owner_id=owner_user_id,
                    agent_id=agent_id,
                    revision_id=runtime.revision_id,
                    for_update=True,
                )
                if revision is None:
                    raise StaleRuntimeGenerationError(
                        "revision recovery fence is stale"
                    )
                mutable_candidate = revision.state in {
                    "prepared",
                    "starting",
                    "ready",
                }
                revision_disposition = (
                    self._mutable_revision_disposition(revision)
                    if mutable_candidate
                    else None
                )
                operation = self._retained_runtime_operation(
                    transaction,
                    runtime,
                    owner_user_id=owner_user_id,
                )
                if (
                    mutable_candidate
                    and revision_disposition is None
                    and operation is None
                    and runtime.operation_id is None
                ):
                    revision_runtimes = tuple(
                        candidate_runtime
                        for candidate_runtime in runtimes
                        if candidate_runtime.revision_id == runtime.revision_id
                    )
                    converged = self._converge_purged_operation_failure(
                        transaction,
                        agent=agent,
                        revision=revision,
                        runtime=runtime,
                        revision_runtimes=revision_runtimes,
                    )
                    if converged is not None:
                        converged_runtime, _revision, requires_stop = converged
                        converged_revision_ids.add(runtime.revision_id)
                        if requires_stop:
                            stop_runtime_ids.append(
                                converged_runtime.runtime_instance_id
                            )
                        continue
                permanent_failure_code = self._permanent_runtime_failure_code(
                    runtime,
                    operation,
                    revision if mutable_candidate else None,
                )
                if (
                    mutable_candidate
                    and runtime.state in self._NONTERMINAL_RUNTIME_STATES
                    and revision_disposition == self._RETRY_RESET_PENDING
                ):
                    if (
                        permanent_failure_code is not None
                        or (
                            operation is not None
                            and operation.state is not OperationState.RETRYABLE
                        )
                    ):
                        raise StaleRuntimeGenerationError(
                            "retryable candidate recovery disposition is ambiguous"
                        )
                    updates: dict[str, object] = {}
                    if runtime.process_id is not None and runtime.state != "stopping":
                        updates["state"] = "stopping"
                    if runtime.is_authoritative:
                        updates["is_authoritative"] = False
                    if runtime.failure_code not in {
                        self._RETRY_RESET_PENDING,
                        *self._PHYSICAL_EXIT_PROOFS,
                    }:
                        updates["failure_code"] = self._RETRY_RESET_PENDING
                    if updates:
                        try:
                            repository.transition_runtime_instance(
                                transaction,
                                owner_id=owner_user_id,
                                runtime_instance_id=runtime.runtime_instance_id,
                                expected_revision=runtime.state_revision,
                                expected_states=(runtime.state,),
                                updates=updates,
                            )
                        except RepositoryConflictError as exc:
                            raise StaleRuntimeGenerationError(
                                "retryable candidate recovery staging is stale"
                            ) from exc
                    retry_reset_candidates.append(
                        (revision.revision_id, runtime.runtime_instance_id)
                    )
                    continue
                if (
                    mutable_candidate
                    and runtime.state in self._NONTERMINAL_RUNTIME_STATES
                ):
                    for historical_runtime in runtimes:
                        if not (
                            historical_runtime.revision_id == runtime.revision_id
                            and historical_runtime.state
                            in self._TERMINAL_RUNTIME_STATES
                        ):
                            continue
                        historical_operation = self._retained_runtime_operation(
                            transaction,
                            historical_runtime,
                            owner_user_id=owner_user_id,
                        )
                        if (
                            self._permanent_runtime_failure_code(
                                historical_runtime,
                                historical_operation,
                            )
                            is not None
                        ):
                            raise StaleRuntimeGenerationError(
                                "terminal candidate recovery disposition is ambiguous"
                            )
                if runtime.state in self._TERMINAL_RUNTIME_STATES:
                    if (
                        mutable_candidate
                        and runtime.revision_id not in nonterminal_revision_ids
                        and runtime.revision_id
                        not in observed_terminal_revisions
                    ):
                        observed_terminal_revisions.add(runtime.revision_id)
                        terminal_attempts = tuple(
                            candidate_runtime
                            for candidate_runtime in runtimes
                            if candidate_runtime.revision_id
                            == runtime.revision_id
                            and candidate_runtime.state
                            in self._TERMINAL_RUNTIME_STATES
                        )
                        terminal_details: list[
                            tuple[Any, Any | None, Optional[str], Optional[str]]
                        ] = []
                        for terminal_attempt in terminal_attempts:
                            terminal_operation = self._retained_runtime_operation(
                                transaction,
                                terminal_attempt,
                                owner_user_id=owner_user_id,
                            )
                            terminal_code = (
                                self._permanent_runtime_failure_code(
                                    terminal_attempt,
                                    terminal_operation,
                                    (
                                        revision
                                        if terminal_attempt is terminal_attempts[0]
                                        else None
                                    ),
                                )
                            )
                            terminal_details.append(
                                (
                                    terminal_attempt,
                                    terminal_operation,
                                    terminal_code,
                                    self._terminal_candidate_failure_code(
                                        terminal_attempt,
                                        terminal_operation,
                                        (
                                            revision
                                            if terminal_attempt is terminal_attempts[0]
                                            else None
                                        ),
                                    ),
                                )
                            )
                        if revision_disposition == self._RETRY_RESET_PENDING:
                            retryable_attempts = tuple(
                                detail
                                for detail in terminal_details
                                if detail[0].failure_code
                                != self._RETRY_RESET_COMPLETE
                                and detail[2] is None
                                and (
                                    detail[1] is None
                                    or detail[1].state is OperationState.RETRYABLE
                                )
                            )
                            if (
                                len(retryable_attempts) != 1
                                or retryable_attempts[0][0].runtime_instance_id
                                != terminal_attempts[0].runtime_instance_id
                            ):
                                raise StaleRuntimeGenerationError(
                                    "retryable candidate recovery disposition is ambiguous"
                                )
                            retry_reset_candidates.append(
                                (
                                    revision.revision_id,
                                    retryable_attempts[0][0].runtime_instance_id,
                                )
                            )
                            continue
                        permanent_attempts = tuple(
                            detail
                            for detail in terminal_details
                            if detail[2] is not None
                        )
                        if permanent_attempts:
                            if (
                                len(permanent_attempts) != 1
                                or permanent_attempts[0][0].runtime_instance_id
                                != terminal_attempts[0].runtime_instance_id
                            ):
                                raise StaleRuntimeGenerationError(
                                    "terminal candidate recovery disposition is ambiguous"
                                )
                            selected_runtime = permanent_attempts[0][0]
                            selected_operation = permanent_attempts[0][1]
                            selected_failure_code = permanent_attempts[0][2]
                            if (
                                selected_operation is None
                                and revision_disposition
                                not in self._PERMANENT_ACTIVATION_FAILURES
                            ) or (
                                selected_operation is not None
                                and selected_operation.state
                                not in {
                                    OperationState.FAILED,
                                    OperationState.CANCELLED,
                                    OperationState.RETRYABLE,
                                }
                            ):
                                raise StaleRuntimeGenerationError(
                                    "terminal candidate recovery operation is not fail-safe"
                                )
                        elif terminal_details:
                            selected_runtime = terminal_details[0][0]
                            selected_operation = terminal_details[0][1]
                            selected_failure_code = terminal_details[0][3]
                            if (
                                selected_runtime.failure_code
                                == self._RETRY_RESET_COMPLETE
                                or (
                                    selected_operation is not None
                                    and selected_operation.state
                                    in {
                                        OperationState.RUNNING,
                                        OperationState.RETRYABLE,
                                    }
                                )
                            ):
                                continue
                            if selected_failure_code is None:
                                raise StaleRuntimeGenerationError(
                                    "terminal candidate recovery disposition is unavailable"
                                )
                        else:  # pragma: no cover - guarded by the outer runtime
                            continue
                        if selected_failure_code not in (
                            self._PERMANENT_ACTIVATION_FAILURES
                        ):
                            selected_failure_code = "revision_promotion_failed"
                        if revision.failure_code != selected_failure_code:
                            try:
                                repository.transition_revision(
                                    transaction,
                                    owner_id=owner_user_id,
                                    agent_id=agent_id,
                                    revision_id=revision.revision_id,
                                    expected_revision=revision.state_revision,
                                    expected_state=revision.state,
                                    updates={"failure_code": selected_failure_code},
                                )
                            except RepositoryConflictError as exc:
                                raise StaleRuntimeGenerationError(
                                    "revision recovery disposition is stale"
                                ) from exc
                        if (
                            selected_runtime.process_id is None
                            or selected_runtime.failure_code
                            in self._PHYSICAL_EXIT_PROOFS
                        ):
                            finalize_runtime_ids.append(
                                selected_runtime.runtime_instance_id
                            )
                        else:
                            stop_runtime_ids.append(
                                selected_runtime.runtime_instance_id
                            )
                    continue
                if mutable_candidate:
                    if operation is None and permanent_failure_code is None:
                        raise StaleRuntimeGenerationError(
                            "candidate recovery operation is unavailable"
                        )
                    if permanent_failure_code is None and operation is not None:
                        if operation.state in {
                            OperationState.RUNNING,
                            OperationState.RETRYABLE,
                        }:
                            # Another exact revision activation/retry owns this
                            # mutable attempt. Agent-wide active replay cleanup
                            # must never consume it.
                            continue
                        if operation.state not in {
                            OperationState.FAILED,
                            OperationState.CANCELLED,
                        }:
                            raise StaleRuntimeGenerationError(
                                "candidate recovery operation is not fail-safe"
                            )
                    if (
                        operation is not None
                        and operation.state is OperationState.RUNNING
                    ):
                        raise StaleRuntimeGenerationError(
                            "staged candidate operation is still running"
                        )
                    if operation is not None and operation.state not in {
                            OperationState.FAILED,
                            OperationState.CANCELLED,
                            OperationState.RETRYABLE,
                        }:
                        raise StaleRuntimeGenerationError(
                            "candidate recovery operation is not fail-safe"
                        )
                failure_code = (
                    permanent_failure_code
                    or self._terminal_candidate_failure_code(
                        runtime,
                        operation,
                        revision if mutable_candidate else None,
                    )
                    or runtime.failure_code
                    or "revision_promotion_failed"
                )
                if self._SAFE_FAILURE.fullmatch(failure_code) is None:
                    raise StaleRuntimeGenerationError(
                        "candidate recovery failure code is invalid"
                    )
                if (
                    mutable_candidate
                    and revision.failure_code != failure_code
                ):
                    if failure_code not in self._PERMANENT_ACTIVATION_FAILURES:
                        failure_code = "revision_promotion_failed"
                    try:
                        repository.transition_revision(
                            transaction,
                            owner_id=owner_user_id,
                            agent_id=agent_id,
                            revision_id=revision.revision_id,
                            expected_revision=revision.state_revision,
                            expected_state=revision.state,
                            updates={"failure_code": failure_code},
                        )
                    except RepositoryConflictError as exc:
                        raise StaleRuntimeGenerationError(
                            "revision recovery disposition is stale"
                        ) from exc
                updates: dict[str, object] = {}
                if runtime.failure_code != failure_code:
                    updates["failure_code"] = failure_code
                if runtime.process_id is not None and runtime.state != "stopping":
                    updates["state"] = "stopping"
                if runtime.is_authoritative:
                    updates["is_authoritative"] = False
                if updates:
                    try:
                        repository.transition_runtime_instance(
                            transaction,
                            owner_id=owner_user_id,
                            runtime_instance_id=runtime.runtime_instance_id,
                            expected_revision=runtime.state_revision,
                            expected_states=(runtime.state,),
                            updates=updates,
                        )
                    except RepositoryConflictError as exc:
                        raise StaleRuntimeGenerationError(
                            "revision recovery staging is stale"
                        ) from exc
                if runtime.process_id is None:
                    finalize_runtime_ids.append(runtime.runtime_instance_id)
                else:
                    stop_runtime_ids.append(runtime.runtime_instance_id)
        return RecoveryPlan(
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            active_revision_id=active_revision,
            authoritative_runtime_instance_id=keep_runtime_id,
            start_revision_id=(
                active_revision
                if active_revision is not None and keep_runtime_id is None
                else None
            ),
            stop_runtime_instance_ids=tuple(stop_runtime_ids),
            finalize_runtime_instance_ids=tuple(finalize_runtime_ids),
            retry_reset_candidates=tuple(retry_reset_candidates),
        )

    def finalize_recovery_runtime(
        self, owner_user_id: str, agent_id: str, runtime_instance_id: str
    ) -> None:
        """Finalize one staged loser after exact physical-stop confirmation."""

        owner_user_id = self._required_text(owner_user_id, "owner_user_id", 255)
        agent_id = self._required_text(agent_id, "agent_id", 255)
        runtime_instance_id = self._uuid_text(
            runtime_instance_id, "runtime_instance_id"
        )
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            runtime = repository.get_runtime_instance(
                transaction,
                owner_id=owner_user_id,
                runtime_instance_id=runtime_instance_id,
                for_update=True,
            )
            if agent is None or runtime is None or runtime.agent_id != agent_id:
                raise StaleRuntimeGenerationError(
                    "revision recovery runtime is unavailable"
                )
            if (
                agent.authoritative_instance_id == runtime_instance_id
                or (
                    agent.active_revision_id == runtime.revision_id
                    and runtime.is_authoritative
                )
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_promotion_recovery_pending"
                )
            revision = repository.get_revision(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                revision_id=runtime.revision_id,
                for_update=True,
            )
            if revision is None:
                raise StaleRuntimeGenerationError(
                    "revision recovery fence is stale"
                )
            terminal_runtime = runtime.state in self._TERMINAL_RUNTIME_STATES
            if (
                not terminal_runtime
                and runtime.process_id is not None
                and runtime.state != "stopping"
            ):
                raise StaleRuntimeGenerationError(
                    "revision recovery was not durably staged"
                )
            mutable_candidate = revision.state in {
                "prepared",
                "starting",
                "ready",
            }
            revision_disposition = (
                self._mutable_revision_disposition(revision)
                if mutable_candidate
                else None
            )
            if revision_disposition == self._RETRY_RESET_PENDING:
                raise RevisionActivationRecoveryPendingError(
                    "revision_retry_reset_required"
                )
            operation = self._retained_runtime_operation(
                transaction,
                runtime,
                owner_user_id=owner_user_id,
            )
            permanent_failure_code = self._permanent_runtime_failure_code(
                runtime,
                operation,
                revision if mutable_candidate else None,
            )
            if (
                mutable_candidate
                and operation is None
                and runtime.process_id is not None
                and permanent_failure_code is not None
                and not (
                    terminal_runtime
                    and runtime.failure_code in self._PHYSICAL_EXIT_PROOFS
                )
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_physical_exit_pending"
                )
            failure_code = (
                permanent_failure_code
                or self._terminal_candidate_failure_code(
                    runtime,
                    operation,
                    revision if mutable_candidate else None,
                )
                or runtime.failure_code
            )
            if (
                not isinstance(failure_code, str)
                or self._SAFE_FAILURE.fullmatch(failure_code) is None
            ):
                raise StaleRuntimeGenerationError(
                    "revision recovery was not durably staged"
                )
            if mutable_candidate:
                if operation is None and permanent_failure_code is None:
                    raise StaleRuntimeGenerationError(
                        "candidate recovery operation is unavailable"
                    )
                if (
                    operation is not None
                    and operation.state is OperationState.RETRYABLE
                    and permanent_failure_code is None
                ):
                    raise RevisionActivationRecoveryPendingError(
                        "revision_retry_reset_required"
                    )
                if operation is not None and operation.state not in {
                        OperationState.RUNNING,
                        OperationState.FAILED,
                        OperationState.CANCELLED,
                        OperationState.RETRYABLE,
                    }:
                    raise StaleRuntimeGenerationError(
                        "candidate recovery operation is not fail-safe"
                    )
            if not terminal_runtime:
                self._runtime._terminalize_instance_plane(
                    transaction,
                    runtime,
                    agent,
                    failure_code=failure_code,
                    operation_state=OperationState.FAILED,
                )
            elif (
                mutable_candidate
                and operation is not None
                and operation.state
                not in {
                    OperationState.FAILED,
                    OperationState.CANCELLED,
                    OperationState.RETRYABLE,
                }
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
            if (
                mutable_candidate
                and revision.revision_id != agent.active_revision_id
            ):
                try:
                    repository.transition_revision(
                        transaction,
                        owner_id=owner_user_id,
                        agent_id=agent_id,
                        revision_id=revision.revision_id,
                        expected_revision=revision.state_revision,
                        expected_state=revision.state,
                        updates={
                            "state": "failed",
                            "failed_at": datetime.now(UTC),
                            "failure_code": failure_code,
                        },
                    )
                except RepositoryConflictError as exc:
                    raise StaleRuntimeGenerationError(
                        "revision recovery transition is stale"
                    ) from exc

    def stage_retryable_candidate_reset(
        self, owner_user_id: str, agent_id: str, revision_id: str
    ) -> str:
        """Stage an exact RETRYABLE attempt for physical process cleanup."""

        owner_user_id = self._required_text(owner_user_id, "owner_user_id", 255)
        agent_id = self._required_text(agent_id, "agent_id", 255)
        revision_id = self._uuid_text(revision_id, "revision_id")
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            revision = repository.get_revision(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                revision_id=revision_id,
                for_update=True,
            )
            if agent is None or revision is None:
                raise StaleRuntimeGenerationError(
                    "retryable candidate is unavailable"
                )
            if agent.deleted_at is not None:
                raise RevisionActivationError("agent_deleted")
            if (
                revision.state not in {"prepared", "starting", "ready"}
                or agent.active_revision_id == revision_id
            ):
                raise StaleRuntimeGenerationError(
                    "retryable candidate revision is stale"
                )
            revision_disposition = self._mutable_revision_disposition(revision)
            if revision_disposition in self._PERMANENT_ACTIVATION_FAILURES:
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
            runtime_inventory = repository.list_runtime_instances(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                states=None,
                for_update=True,
                limit=1000,
            )
            if len(runtime_inventory) >= 1000:
                raise RevisionActivationError("runtime_inventory_too_large")
            runtimes = tuple(
                runtime
                for runtime in runtime_inventory
                if runtime.revision_id == revision_id
            )
            nonterminal_runtimes = tuple(
                runtime
                for runtime in runtimes
                if runtime.state in self._NONTERMINAL_RUNTIME_STATES
            )
            if len(nonterminal_runtimes) > 1:
                raise StaleRuntimeGenerationError(
                    "retryable candidate runtime identity is stale"
                )
            retryable_terminal_attempts: list[tuple[Any, Any | None]] = []
            observed_permanent_failure = False
            for terminal_runtime in runtimes:
                if terminal_runtime.state not in self._TERMINAL_RUNTIME_STATES:
                    continue
                terminal_operation = self._retained_runtime_operation(
                    transaction,
                    terminal_runtime,
                    owner_user_id=owner_user_id,
                )
                if (
                    self._permanent_runtime_failure_code(
                        terminal_runtime,
                        terminal_operation,
                    )
                    is not None
                ):
                    observed_permanent_failure = True
                if (
                    (
                        terminal_operation is not None
                        and terminal_operation.state is OperationState.RETRYABLE
                    )
                    or (
                        terminal_operation is None
                        and revision_disposition == self._RETRY_RESET_PENDING
                        and terminal_runtime is runtimes[0]
                    )
                ) and terminal_runtime.failure_code != self._RETRY_RESET_COMPLETE:
                    retryable_terminal_attempts.append(
                        (terminal_runtime, terminal_operation)
                    )
            if observed_permanent_failure:
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
            operation = None
            if nonterminal_runtimes:
                runtime = nonterminal_runtimes[0]
            else:
                if len(retryable_terminal_attempts) != 1:
                    raise StaleRuntimeGenerationError(
                        "retryable candidate runtime identity is stale"
                    )
                runtime, operation = retryable_terminal_attempts[0]
            if (
                runtime.is_authoritative
                or agent.authoritative_instance_id == runtime.runtime_instance_id
            ):
                raise StaleRuntimeGenerationError(
                    "retryable candidate runtime is authoritative"
                )
            if operation is None:
                operation = self._retained_runtime_operation(
                    transaction,
                    runtime,
                    owner_user_id=owner_user_id,
                )
            if operation is None and revision_disposition != self._RETRY_RESET_PENDING:
                raise StaleRuntimeGenerationError(
                    "candidate delivery operation is unavailable"
                )
            if (
                operation is not None
                and operation.state is not OperationState.RETRYABLE
            ):
                raise StaleRuntimeGenerationError(
                    "candidate delivery operation is not retryable"
                )
            if (
                self._permanent_runtime_failure_code(runtime, operation, revision)
                is not None
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
            if (
                runtime.state in self._NONTERMINAL_RUNTIME_STATES
                and runtime.failure_code
                not in {None, self._RETRY_RESET_PENDING}
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
            try:
                if revision.failure_code != self._RETRY_RESET_PENDING:
                    repository.transition_revision(
                        transaction,
                        owner_id=owner_user_id,
                        agent_id=agent_id,
                        revision_id=revision_id,
                        expected_revision=revision.state_revision,
                        expected_state=revision.state,
                        updates={"failure_code": self._RETRY_RESET_PENDING},
                    )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "retryable candidate reset disposition is stale"
                ) from exc

            updates: dict[str, object] = {}
            if runtime.state in self._NONTERMINAL_RUNTIME_STATES:
                if runtime.process_id is not None and runtime.state != "stopping":
                    updates["state"] = "stopping"
                if runtime.is_authoritative:
                    updates["is_authoritative"] = False
            preserve_exit_proof = (
                runtime.state in self._TERMINAL_RUNTIME_STATES
                and runtime.failure_code in self._PHYSICAL_EXIT_PROOFS
            )
            if (
                runtime.failure_code != self._RETRY_RESET_PENDING
                and not preserve_exit_proof
            ):
                updates["failure_code"] = self._RETRY_RESET_PENDING
            if updates:
                try:
                    repository.transition_runtime_instance(
                        transaction,
                        owner_id=owner_user_id,
                        runtime_instance_id=runtime.runtime_instance_id,
                        expected_revision=runtime.state_revision,
                        expected_states=(runtime.state,),
                        updates=updates,
                    )
                except RepositoryConflictError as exc:
                    raise StaleRuntimeGenerationError(
                        "retryable candidate reset staging is stale"
                    ) from exc
            return runtime.runtime_instance_id

    def finalize_retryable_candidate_reset(
        self,
        owner_user_id: str,
        agent_id: str,
        revision_id: str,
        runtime_instance_id: str,
    ) -> None:
        """Fence a RETRYABLE runtime only after exact physical-stop proof."""

        owner_user_id = self._required_text(owner_user_id, "owner_user_id", 255)
        agent_id = self._required_text(agent_id, "agent_id", 255)
        revision_id = self._uuid_text(revision_id, "revision_id")
        runtime_instance_id = self._uuid_text(
            runtime_instance_id, "runtime_instance_id"
        )
        with self._runtime._agents.transaction() as transaction:
            repository = self._runtime._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            revision = repository.get_revision(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                revision_id=revision_id,
                for_update=True,
            )
            runtime = repository.get_runtime_instance(
                transaction,
                owner_id=owner_user_id,
                runtime_instance_id=runtime_instance_id,
                for_update=True,
            )
            if agent is None or revision is None or runtime is None:
                raise StaleRuntimeGenerationError(
                    "retryable candidate reset identity is stale"
                )
            if agent.deleted_at is not None:
                raise RevisionActivationError("agent_deleted")
            if not (
                revision.state in {"prepared", "starting", "ready"}
                and revision.revision_id == runtime.revision_id
                and runtime.owner_id == owner_user_id
                and runtime.agent_id == agent_id
                and not runtime.is_authoritative
                and agent.active_revision_id != revision_id
                and agent.authoritative_instance_id != runtime_instance_id
            ):
                raise StaleRuntimeGenerationError(
                    "retryable candidate reset identity is stale"
                )
            revision_disposition = self._mutable_revision_disposition(revision)
            if revision_disposition in self._PERMANENT_ACTIVATION_FAILURES:
                raise RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
            operation = self._retained_runtime_operation(
                transaction,
                runtime,
                owner_user_id=owner_user_id,
            )
            if (
                operation is not None
                and operation.state is not OperationState.RETRYABLE
            ):
                raise StaleRuntimeGenerationError(
                    "candidate delivery operation is not retryable"
                )
            terminal_runtime = runtime.state in self._TERMINAL_RUNTIME_STATES
            if (
                terminal_runtime
                and runtime.failure_code == self._RETRY_RESET_COMPLETE
                and revision.state == "prepared"
                and revision_disposition is None
            ):
                return
            if revision_disposition != self._RETRY_RESET_PENDING:
                raise StaleRuntimeGenerationError(
                    "retryable candidate reset was not durably staged"
                )
            retained_physical_proof = terminal_runtime and (
                runtime.failure_code in self._PHYSICAL_EXIT_PROOFS
            )
            exact_processless_prelaunch = (
                not terminal_runtime
                and runtime.process_id is None
                and runtime.state == "delivering"
                and runtime.failure_code == self._RETRY_RESET_PENDING
            )
            if operation is None and not (
                retained_physical_proof or exact_processless_prelaunch
            ):
                raise StaleRuntimeGenerationError(
                    "retryable candidate physical exit proof is unavailable"
                )
            if runtime.failure_code not in {
                self._RETRY_RESET_PENDING,
                *self._PHYSICAL_EXIT_PROOFS,
            } or (
                not terminal_runtime
                and runtime.process_id is not None
                and runtime.state != "stopping"
            ):
                raise StaleRuntimeGenerationError(
                    "retryable candidate reset was not durably staged"
                )
            try:
                if terminal_runtime:
                    repository.transition_runtime_instance(
                        transaction,
                        owner_id=owner_user_id,
                        runtime_instance_id=runtime_instance_id,
                        expected_revision=runtime.state_revision,
                        expected_states=(runtime.state,),
                        updates={"failure_code": self._RETRY_RESET_COMPLETE},
                    )
                else:
                    self._runtime._terminalize_instance_plane(
                        transaction,
                        runtime,
                        agent,
                        failure_code=self._RETRY_RESET_COMPLETE,
                    )
                repository.transition_revision(
                    transaction,
                    owner_id=owner_user_id,
                    agent_id=agent_id,
                    revision_id=revision_id,
                    expected_revision=revision.state_revision,
                    expected_state=revision.state,
                    updates={
                        "state": "prepared",
                        "confirmed_at": None,
                        "failure_code": None,
                    },
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "retryable candidate reset finalization is stale"
                ) from exc


async def _await_if_needed(value: Any) -> Any:
    """Await callback results while permitting synchronous durable test seams."""

    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class AgentRevisionActivator:
    """Coordinate prepare/start/ready/promote without risking the old runtime.

    All pre-commit failures terminalize only the candidate.  The old runtime is
    not stopped until :meth:`RevisionActivationStore.promote_candidate` returns,
    which is the durable commit boundary.  A process crash is recovered from the
    store's active pointer rather than from whichever candidate was newest.
    """

    store: RevisionActivationStore
    start_candidate: Callable[[CandidateRevision], Awaitable[str] | str]
    await_candidate_ready: Callable[
        [CandidateRevision], Awaitable[str] | str
    ]
    stop_runtime: Callable[[str], Awaitable[Any] | Any]
    fault_hook: Optional[Callable[[str, CandidateRevision], None]] = None

    @property
    def _store(self) -> RevisionActivationStore:
        return self.store

    @property
    def _start_candidate(
        self,
    ) -> Callable[[CandidateRevision], Awaitable[str] | str]:
        return self.start_candidate

    @property
    def _await_candidate_ready(
        self,
    ) -> Callable[[CandidateRevision], Awaitable[str] | str]:
        return self.await_candidate_ready

    @property
    def _stop_runtime(self) -> Callable[[str], Awaitable[Any] | Any]:
        return self.stop_runtime

    @property
    def _fault_hook(self) -> Callable[[str, CandidateRevision], None]:
        return self.fault_hook or (lambda _boundary, _candidate: None)

    def _fault(self, boundary: str, candidate: CandidateRevision) -> None:
        self._fault_hook(boundary, candidate)

    async def _store_call(
        self,
        callback: Callable[..., Any],
        *args: Any,
    ) -> tuple[Any, BaseException | None, asyncio.CancelledError | None]:
        """Run one synchronous Plane-backed store call without blocking ASGI.

        The retained worker is always joined before cancellation is observed by
        the state machine.  That makes a committed CAS result authoritative
        even when the requesting task is cancelled while the database response
        is in flight.
        """

        task = asyncio.create_task(
            asyncio.to_thread(callback, *args),
            name=f"agent-revision-store-{getattr(callback, '__name__', 'call')}",
        )
        return await _join_task_outcome_through_cancellation(task)

    async def _confirm_physical_stop(
        self, runtime_instance_id: str
    ) -> PhysicalStopReceipt | None:
        confirmation = await _await_if_needed(self._stop_runtime(runtime_instance_id))
        if confirmation is False:
            raise RuntimeError("runtime physical stop was not confirmed")
        if confirmation is None:
            return None
        if not isinstance(confirmation, PhysicalStopReceipt):
            raise TypeError("runtime stop callback returned an invalid receipt")
        if confirmation.runtime_instance_id != runtime_instance_id:
            raise RuntimeError("runtime stop receipt identity is stale")
        return confirmation

    @staticmethod
    async def _release_stop_receipt(
        receipt: PhysicalStopReceipt | None,
    ) -> None:
        if receipt is None:
            return
        try:
            released = await _await_if_needed(receipt.release())
            if released is False:
                raise RuntimeError("runtime stop receipt release was refused")
        except Exception:
            # Plane is already authoritative at every call site. Retaining the
            # exact waiter is safer than allowing a late duplicate exit frame to
            # enter the generic RETRYABLE reducer.
            logger.exception(
                "runtime stop receipt release failed after durable finalization",
                extra={"runtime_instance_id": receipt.runtime_instance_id},
            )

    async def _fail_precommit_candidate(
        self, candidate: CandidateRevision, failure_code: str
    ) -> BaseException | None:
        remembered_cancellation: asyncio.CancelledError | None = None
        try:
            requires_stop, error, cancellation = await self._store_call(
                self._store.stage_candidate_failure,
                candidate,
                failure_code,
            )
            if error is not None:
                raise error
            if cancellation is not None:
                remembered_cancellation = cancellation
        except Exception as exc:
            logger.exception(
                "candidate cleanup staging failed",
                extra={"failure_code": failure_code},
            )
            return exc
        receipt: PhysicalStopReceipt | None = None
        if requires_stop:
            try:
                receipt = await self._confirm_physical_stop(
                    candidate.runtime_instance_id
                )
            except Exception as exc:
                logger.exception(
                    "candidate stop failed",
                    extra={"failure_code": failure_code},
                )
                return exc
        try:
            _result, error, cancellation = await self._store_call(
                self._store.fail_candidate,
                candidate,
                failure_code,
            )
            if error is not None:
                raise error
            if cancellation is not None:
                remembered_cancellation = remembered_cancellation or cancellation
        except Exception as exc:
            logger.exception(
                "candidate failure finalization failed",
                extra={"failure_code": failure_code},
            )
            return exc
        await self._release_stop_receipt(receipt)
        return remembered_cancellation

    async def reset_retryable_candidate(
        self, owner_user_id: str, agent_id: str, revision_id: str
    ) -> str:
        """Physically stop, then durably fence, one RETRYABLE child attempt."""

        remembered_cancellation: asyncio.CancelledError | None = None
        runtime_instance_id, error, cancellation = await self._store_call(
            self._store.stage_retryable_candidate_reset,
            owner_user_id,
            agent_id,
            revision_id,
        )
        if cancellation is not None:
            remembered_cancellation = cancellation
        if error is not None:
            if isinstance(
                error,
                (RevisionActivationError, StaleRuntimeGenerationError),
            ):
                if cancellation is not None:
                    raise cancellation from error
                raise error
            replayed, replay_error, replay_cancellation = await self._store_call(
                self._store.stage_retryable_candidate_reset,
                owner_user_id,
                agent_id,
                revision_id,
            )
            remembered_cancellation = (
                remembered_cancellation or replay_cancellation
            )
            if replay_error is not None:
                pending = RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
                pending.add_note(
                    "initial retry-reset staging error: "
                    f"{type(error).__name__}: {error}"
                )
                if remembered_cancellation is not None:
                    raise remembered_cancellation from pending
                raise pending from replay_error
            runtime_instance_id = replayed
        try:
            receipt = await self._confirm_physical_stop(runtime_instance_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RevisionActivationRecoveryPendingError(
                "revision_runtime_cleanup_pending"
            ) from exc

        _ignored, error, cancellation = await self._store_call(
            self._store.finalize_retryable_candidate_reset,
            owner_user_id,
            agent_id,
            revision_id,
            runtime_instance_id,
        )
        remembered_cancellation = remembered_cancellation or cancellation
        if error is not None:
            _ignored, replay_error, replay_cancellation = await self._store_call(
                self._store.finalize_retryable_candidate_reset,
                owner_user_id,
                agent_id,
                revision_id,
                runtime_instance_id,
            )
            remembered_cancellation = (
                remembered_cancellation or replay_cancellation
            )
            if replay_error is not None:
                pending = RevisionActivationRecoveryPendingError(
                    "revision_runtime_cleanup_pending"
                )
                pending.add_note(
                    "initial retry-reset finalization error: "
                    f"{type(error).__name__}: {error}"
                )
                if remembered_cancellation is not None:
                    raise remembered_cancellation from pending
                raise pending from replay_error
        await self._release_stop_receipt(receipt)
        if remembered_cancellation is not None:
            raise remembered_cancellation
        return runtime_instance_id

    async def activate(
        self, request: CandidatePreparation
    ) -> RevisionActivationResult:
        """Prepare and activate one revision through a single commit boundary."""

        candidate: Optional[CandidateRevision] = None
        commit: Optional[PromotionCommit] = None
        phase = "preparation"
        committed = False
        promotion_ambiguous = False
        remembered_cancellation: asyncio.CancelledError | None = None
        try:
            prepared, error, cancellation = await self._store_call(
                self._store.prepare_candidate,
                request,
            )
            if error is not None:
                if not isinstance(
                    error,
                    (RevisionActivationError, StaleRuntimeGenerationError),
                ):
                    replayed, replay_error, replay_cancellation = (
                        await self._store_call(
                            self._store.prepare_candidate,
                            request,
                        )
                    )
                    cancellation = cancellation or replay_cancellation
                    if replay_error is None:
                        prepared = replayed
                        error = None
                    else:
                        ambiguity = RevisionActivationRecoveryPendingError(
                            "revision_preparation_recovery_pending"
                        )
                        ambiguity.add_note(
                            "initial candidate preparation error: "
                            f"{type(error).__name__}: {error}"
                        )
                        if cancellation is not None:
                            raise cancellation from ambiguity
                        raise ambiguity from replay_error
                if error is not None:
                    if cancellation is not None:
                        raise cancellation from error
                    raise error
            candidate = prepared
            if cancellation is not None:
                raise cancellation
            self._fault("after_prepare", candidate)

            phase = "start"
            self._fault("before_start", candidate)
            started_runtime_id = await _await_if_needed(
                self._start_candidate(candidate)
            )
            if started_runtime_id != candidate.runtime_instance_id:
                raise RevisionActivationError("stale_runtime_generation")
            _ignored, error, cancellation = await self._store_call(
                self._store.mark_candidate_starting,
                candidate,
            )
            if error is not None:
                if cancellation is not None:
                    raise cancellation from error
                raise error
            if cancellation is not None:
                raise cancellation
            self._fault("after_start", candidate)

            phase = "ready"
            self._fault("before_ready", candidate)
            ready_runtime_id = await _await_if_needed(
                self._await_candidate_ready(candidate)
            )
            confirmed, error, cancellation = await self._store_call(
                self._store.confirm_candidate_ready,
                candidate,
                ready_runtime_id,
            )
            if error is not None:
                if cancellation is not None:
                    raise cancellation from error
                raise error
            candidate = confirmed
            if cancellation is not None:
                raise cancellation
            self._fault("after_ready", candidate)

            phase = "promotion"
            self._fault("before_promote", candidate)
            promoted, error, cancellation = await self._store_call(
                self._store.promote_candidate,
                candidate,
            )
            if error is not None:
                # Domain errors are raised from inside the transaction and are
                # therefore known pre-commit failures. An infrastructure error
                # can instead be a lost commit acknowledgement. Replay the exact
                # idempotent promotion after the retained worker exits so an
                # already-active candidate is recovered as the commit winner.
                if not isinstance(
                    error,
                    (RevisionActivationError, StaleRuntimeGenerationError),
                ):
                    replayed, replay_error, replay_cancellation = (
                        await self._store_call(
                            self._store.promote_candidate,
                            candidate,
                        )
                    )
                    cancellation = cancellation or replay_cancellation
                    if replay_error is None:
                        promoted = replayed
                        error = None
                    else:
                        ambiguity = RevisionActivationRecoveryPendingError(
                            "revision_promotion_recovery_pending"
                        )
                        ambiguity.add_note(
                            "initial promotion error: "
                            f"{type(error).__name__}: {error}"
                        )
                        promotion_ambiguous = True
                        if cancellation is not None:
                            raise cancellation from ambiguity
                        raise ambiguity from replay_error
            if error is not None:
                if cancellation is not None:
                    raise cancellation from error
                raise error
            commit = promoted
            committed = True
            if cancellation is not None:
                raise cancellation
            self._fault("after_promote_commit", candidate)
        except asyncio.CancelledError as cancellation:
            if committed and commit is not None:
                remembered_cancellation = cancellation
            elif promotion_ambiguous:
                # Authority could already be committed. Recovery, not local
                # candidate failure/stop, owns convergence in this state.
                raise
            else:
                cleanup_error: BaseException | None = None
                cleanup_cancellation: asyncio.CancelledError | None = None
                if candidate is not None:
                    failure_code = {
                        "preparation": "bundle_install_failed",
                        "start": "child_start_failed",
                        "ready": "child_registration_timeout",
                        "promotion": "revision_promotion_failed",
                    }[phase]
                    cleanup_task = asyncio.create_task(
                        self._fail_precommit_candidate(candidate, failure_code),
                        name=f"agent-revision-cancel-cleanup-{candidate.revision_id}",
                    )
                    cleanup_result, cleanup_task_error, cleanup_cancellation = (
                        await _join_task_outcome_through_cancellation(cleanup_task)
                    )
                    cleanup_error = cleanup_task_error or cleanup_result
                if cleanup_cancellation is not None and cleanup_error is None:
                    cleanup_error = cleanup_cancellation
                if cleanup_error is not None:
                    raise cancellation from cleanup_error
                raise
        except Exception as exc:
            if committed and commit is not None:
                # The database is already authoritative.  A local observer/fault
                # hook cannot truthfully turn that into promotion failure; carry
                # on to post-commit cleanup and let crash recovery retry it.
                logger.exception(
                    "post-commit revision observer failed; promotion remains active",
                    extra={"revision_id": commit.revision_id},
                )
            else:
                if isinstance(exc, RevisionActivationRecoveryPendingError):
                    # Neither failure cleanup nor runtime stop is safe while the
                    # durable promotion winner cannot be read authoritatively.
                    raise
                if candidate is not None:
                    failure_code = {
                        "preparation": "bundle_install_failed",
                        "start": "child_start_failed",
                        "ready": "child_registration_timeout",
                        "promotion": "revision_promotion_failed",
                    }[phase]
                    cleanup_task = asyncio.create_task(
                        self._fail_precommit_candidate(candidate, failure_code),
                        name=f"agent-revision-failure-cleanup-{candidate.revision_id}",
                    )
                    cleanup_error, cleanup_task_error, cleanup_cancellation = (
                        await _join_task_outcome_through_cancellation(cleanup_task)
                    )
                    cleanup_error = cleanup_task_error or cleanup_error
                    if cleanup_cancellation is not None:
                        raise cleanup_cancellation from (cleanup_error or exc)
                    if cleanup_error is not None:
                        pending = RevisionActivationRecoveryPendingError(
                            "revision_runtime_cleanup_pending"
                        )
                        pending.add_note(
                            "activation failure before cleanup: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        raise pending from cleanup_error
                if isinstance(exc, RevisionActivationError) and phase != "promotion":
                    raise
                code = {
                    "preparation": "bundle_install_failed",
                    "start": "child_start_failed",
                    "ready": "child_registration_timeout",
                    "promotion": "revision_promotion_failed",
                }[phase]
                raise RevisionActivationError(code) from exc

        if commit is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("revision activation lost its promotion commit")

        previous_runtime = commit.previous_runtime_instance_id
        if previous_runtime is None or previous_runtime == commit.runtime_instance_id:
            result = RevisionActivationResult(
                commit=commit,
                prior_runtime_stopped=False,
                cleanup_pending=False,
            )
            if remembered_cancellation is not None:
                raise remembered_cancellation
            return result

        self._fault("before_prior_stop", candidate)
        stop_error: BaseException | None = None
        try:
            receipt = await self._confirm_physical_stop(previous_runtime)
            _ignored, error, cancellation = await self._store_call(
                self._store.finalize_recovery_runtime,
                commit.owner_user_id,
                commit.agent_id,
                previous_runtime,
            )
            if error is not None:
                if cancellation is not None:
                    raise cancellation from error
                raise error
            if cancellation is not None:
                remembered_cancellation = remembered_cancellation or cancellation
            await self._release_stop_receipt(receipt)
        except Exception as exc:
            stop_error = exc
            logger.exception(
                "prior runtime stop failed after revision promotion",
                extra={"revision_id": commit.revision_id},
            )
            result = RevisionActivationResult(
                commit=commit,
                prior_runtime_stopped=False,
                cleanup_pending=True,
            )
            if remembered_cancellation is not None:
                raise remembered_cancellation from stop_error
            return result
        self._fault("after_prior_stop", candidate)
        result = RevisionActivationResult(
            commit=commit,
            prior_runtime_stopped=True,
            cleanup_pending=False,
        )
        if remembered_cancellation is not None:
            raise remembered_cancellation
        return result

    async def reconcile_after_crash(
        self, owner_user_id: str, agent_id: str
    ) -> RecoveryPlan:
        """Stop every non-authoritative candidate named by durable recovery."""

        plan, error, cancellation = await self._store_call(
            self._store.recovery_plan,
            owner_user_id,
            agent_id,
        )
        if error is not None:
            if cancellation is not None:
                raise cancellation from error
            raise error
        if cancellation is not None:
            raise cancellation
        stop_error: BaseException | None = None
        for revision_id, runtime_instance_id in plan.retry_reset_candidates:
            try:
                receipt = await self._confirm_physical_stop(runtime_instance_id)
                _ignored, error, finalization_cancellation = (
                    await self._store_call(
                        self._store.finalize_retryable_candidate_reset,
                        owner_user_id,
                        agent_id,
                        revision_id,
                        runtime_instance_id,
                    )
                )
                if error is not None:
                    if finalization_cancellation is not None:
                        raise finalization_cancellation from error
                    raise error
                await self._release_stop_receipt(receipt)
                if finalization_cancellation is not None:
                    raise finalization_cancellation
            except Exception as exc:
                stop_error = stop_error or exc
                logger.exception(
                    "retryable candidate reset failed during recovery",
                    extra={
                        "revision_id": revision_id,
                        "runtime_instance_id": runtime_instance_id,
                    },
                )
        for runtime_instance_id in plan.finalize_runtime_instance_ids:
            try:
                receipt = await self._confirm_physical_stop(runtime_instance_id)
                _ignored, error, finalization_cancellation = (
                    await self._store_call(
                        self._store.finalize_recovery_runtime,
                        owner_user_id,
                        agent_id,
                        runtime_instance_id,
                    )
                )
                if error is not None:
                    if finalization_cancellation is not None:
                        raise finalization_cancellation from error
                    raise error
                await self._release_stop_receipt(receipt)
                if finalization_cancellation is not None:
                    raise finalization_cancellation
            except Exception as exc:
                stop_error = stop_error or exc
                logger.exception(
                    "terminal candidate finalization failed during recovery",
                    extra={"runtime_instance_id": runtime_instance_id},
                )
        for runtime_instance_id in plan.stop_runtime_instance_ids:
            try:
                receipt = await self._confirm_physical_stop(runtime_instance_id)
                _ignored, error, finalization_cancellation = (
                    await self._store_call(
                        self._store.finalize_recovery_runtime,
                        owner_user_id,
                        agent_id,
                        runtime_instance_id,
                    )
                )
                if error is not None:
                    if finalization_cancellation is not None:
                        raise finalization_cancellation from error
                    raise error
                await self._release_stop_receipt(receipt)
                if finalization_cancellation is not None:
                    raise finalization_cancellation
            except Exception as exc:
                stop_error = stop_error or exc
                logger.exception(
                    "non-authoritative runtime stop failed during recovery",
                    extra={"runtime_instance_id": runtime_instance_id},
                )
        if stop_error is not None:
            raise RevisionActivationRecoveryPendingError(
                "revision_runtime_cleanup_pending"
            ) from stop_error
        return plan


class AgentLifecycleManager:
    """Manages draft agent creation, testing, approval, and promotion to live."""

    def __init__(
        self,
        db=None,
        orchestrator=None,
        process_supervisor=None,
        *,
        plane_runtime=None,
        plane_repositories=None,
        work_admission=None,
        draft_store=None,
        byo_runtime_contract_version: int = BYO_RUNTIME_CONTRACT_VERSION,
        byo_runtime_lock_sha256: str = BYO_RUNTIME_LOCK_SHA256,
        generated_agent_publication_service: (
            GeneratedAgentPublicationService | None
        ) = None,
        governed_lifecycle: Optional[GovernedLifecycleCoordinator] = None,
    ):
        """
        Args:
            db: Explicit test-only draft-store double retained for compatibility.
            orchestrator: Orchestrator instance (for LLM client reuse and WS broadcasts)
        """
        if draft_store is not None and db is not None:
            raise ValueError("bind either draft_store or db, not both")
        if draft_store is None and plane_runtime is not None:
            from orchestrator.draft_plane_store import PlaneDraftStore

            draft_store = PlaneDraftStore(
                plane_runtime=plane_runtime,
                plane_repositories=plane_repositories,
                work_admission=work_admission,
            )
        if draft_store is None:
            draft_store = db
        if draft_store is None:
            raise ValueError("draft persistence must be explicitly injected")
        self.draft_store = draft_store
        # Temporary internal alias while the policy state machine keeps its
        # long-standing method names. Production binds PlaneDraftStore here;
        # no Database pool or SQL surface reaches this manager.
        self.db = draft_store
        self.orchestrator = orchestrator
        # Feature 054: agent codegen is a system-context flow — it resolves
        # the admin-managed system LLM credential per generation call (no
        # env fallback exists anymore), so an admin save takes effect
        # without a restart.
        _llm_store = getattr(orchestrator, '_llm_store', None)
        self.generator = AgentCodeGenerator(
            config_resolver=(_llm_store.get_system_sync if _llm_store is not None else None),
        )
        self.security = CodeSecurityAnalyzer()
        self.validator = AgentSpecValidator()
        self.process_supervisor = (
            process_supervisor
            if process_supervisor is not None
            else ProcessSupervisor()
        )
        if byo_runtime_contract_version != BYO_RUNTIME_CONTRACT_VERSION:
            raise ValueError("unsupported BYO runtime contract version")
        if not re.fullmatch(r"[0-9a-f]{64}", byo_runtime_lock_sha256 or ""):
            raise ValueError("BYO runtime lock must be lowercase SHA-256")
        self._byo_runtime_contract_version = byo_runtime_contract_version
        self._byo_runtime_lock_sha256 = byo_runtime_lock_sha256
        self.generated_agent_publication_service = (
            generated_agent_publication_service
        )
        self.governed_lifecycle = governed_lifecycle
        self._draft_processes: Dict[str, Any] = {}  # draft_id -> supervised process
        self._agents_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', 'agents')
        )

    def bind_governed_lifecycle(
        self,
        coordinator: GovernedLifecycleCoordinator,
    ) -> None:
        """Install the composed Plane/LETS lifecycle boundary atomically."""

        if not isinstance(coordinator, GovernedLifecycleCoordinator):
            raise TypeError("governed lifecycle coordinator is required")
        self.governed_lifecycle = coordinator

    def _active_governed_lifecycle(self) -> GovernedLifecycleCoordinator | None:
        if self.governed_lifecycle is not None:
            return self.governed_lifecycle
        mode = (os.getenv("LETS_MODE", "off").strip().lower() or "off")
        if mode == "enforce":
            raise LetsLifecycleError("lifecycle_runtime_unavailable", retryable=True)
        return None

    @staticmethod
    def _runtime_agent_id(draft: Mapping[str, Any]) -> str:
        slug = str(draft.get("agent_slug") or "")
        if not slug:
            raise LetsLifecycleError("dynamic_agent_identity_missing")
        return f"{slug.replace('_', '-')}-1"

    @staticmethod
    def _declared_scopes_from_tools_file(tools_file: str) -> tuple[str, ...]:
        """Extract literal TOOL_REGISTRY scopes without executing generated code."""

        try:
            with open(tools_file, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=tools_file)
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise LetsLifecycleError("dynamic_agent_scopes_unavailable") from exc
        scopes: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "scope"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    scopes.add(value.value)
        if not scopes:
            scopes.add("tools:read")
        from orchestrator.lets_scope_profile import binding_for_scope

        try:
            for scope in scopes:
                binding_for_scope(scope)
        except (TypeError, ValueError) as exc:
            raise LetsLifecycleError("dynamic_agent_scope_invalid") from exc
        return tuple(sorted(scopes))

    async def _admit_dynamic_runtime(
        self,
        draft: Mapping[str, Any],
        *,
        runtime_id: str,
        tools_file: str,
    ) -> LifecycleConvergence:
        coordinator = self._active_governed_lifecycle()
        if coordinator is None:
            return LifecycleConvergence(protected=False)
        result = await coordinator.admit_new_runtime(
            owner_id=str(draft["user_id"]),
            agent_id=self._runtime_agent_id(draft),
            runtime_id=runtime_id,
            population=AuthorityPopulation.SERVER_DYNAMIC,
            declared_scopes=self._declared_scopes_from_tools_file(tools_file),
            executor_conformant=True,
        )
        binding = result.binding
        if binding is not None and binding.state.value == "active" and self.orchestrator:
            from orchestrator.governed_dispatch import DispatchRuntime

            audience = coordinator.service.config.executor_instance_id
            if not isinstance(audience, str) or not audience:
                if coordinator.service.config.mode == "enforce":
                    raise LetsLifecycleError("executor_audience_unavailable")
                return result
            self.orchestrator.register_governed_dispatch_runtime(
                DispatchRuntime(
                    owner_id=binding.owner_id,
                    agent_id=binding.agent_id,
                    population=binding.population.value,
                    runtime_id=binding.runtime_id,
                    runtime_generation=binding.runtime_generation,
                    executor_audience=audience,
                    executor_conformant=True,
                    dispatch_posture="protected_executor",
                )
            )
        return result

    async def _quiesce_dynamic_runtime(
        self,
        draft: Mapping[str, Any],
    ) -> LifecycleConvergence:
        coordinator = self._active_governed_lifecycle()
        if coordinator is None:
            return LifecycleConvergence(protected=False)
        result = await coordinator.quiesce_current(
            owner_id=str(draft["user_id"]),
            agent_id=self._runtime_agent_id(draft),
            population=AuthorityPopulation.SERVER_DYNAMIC,
        )
        if self.orchestrator:
            self.orchestrator.unregister_governed_dispatch_runtime(
                self._runtime_agent_id(draft)
            )
        return result

    async def _close_dynamic_runtime(
        self,
        draft: Mapping[str, Any],
    ) -> LifecycleConvergence:
        coordinator = self._active_governed_lifecycle()
        if coordinator is None:
            return LifecycleConvergence(protected=False)
        result = await coordinator.close_current(
            owner_id=str(draft["user_id"]),
            agent_id=self._runtime_agent_id(draft),
            population=AuthorityPopulation.SERVER_DYNAMIC,
        )
        if self.orchestrator:
            self.orchestrator.unregister_governed_dispatch_runtime(
                self._runtime_agent_id(draft)
            )
        return result

    async def _revoke_dynamic_runtime(
        self,
        draft: Mapping[str, Any],
        *,
        reason_code: str,
    ) -> LifecycleConvergence:
        coordinator = self._active_governed_lifecycle()
        if coordinator is None:
            return LifecycleConvergence(protected=False)
        result = await coordinator.revoke_current(
            owner_id=str(draft["user_id"]),
            agent_id=self._runtime_agent_id(draft),
            population=AuthorityPopulation.SERVER_DYNAMIC,
            reason_code=reason_code,
        )
        if self.orchestrator:
            self.orchestrator.unregister_governed_dispatch_runtime(
                self._runtime_agent_id(draft)
            )
        return result

    # Progress Callback

    async def _send_progress(self, websocket, draft_id: str, step: str,
                              message: str, status: str, detail: Dict = None):
        """Send progress update to the UI client."""
        if websocket:
            try:
                payload = {
                    "type": "agent_creation_progress",
                    "draft_id": draft_id,
                    "step": step,
                    "message": message,
                    "status": status,
                }
                if detail:
                    payload["detail"] = detail
                await websocket.send_text(json.dumps(payload))
            except Exception as e:
                logger.warning(f"Failed to send progress: {e}")

    def _append_log(
        self,
        draft_id: str,
        message: str,
        *,
        owner_user_id: str | None = None,
        expected_revision: int | None = None,
        claim_id: str | None = None,
    ) -> bool:
        """Append a message to the draft's generation_log."""

        fence_values = (owner_user_id, expected_revision, claim_id)
        has_fence = all(value is not None for value in fence_values)
        if any(value is not None for value in fence_values) and not has_fence:
            raise ValueError("generation log claim fence must be all-or-none")
        append = getattr(self.draft_store, "append_generation_log", None)
        if callable(append):
            if has_fence:
                return bool(
                    append(
                        draft_id,
                        message,
                        owner_user_id=owner_user_id,
                        expected_revision=expected_revision,
                        claim_id=claim_id,
                    )
                )
            return bool(append(draft_id, message))
        if has_fence:
            raise RuntimeError(
                "draft store cannot fence generation progress logging"
            )
        draft = self.db.get_draft_agent(draft_id)
        if not draft:
            return False
        log = json.loads(draft.get("generation_log") or "[]")
        log.append({"message": message, "timestamp": int(time.time() * 1000)})
        return bool(
            self.db.update_draft_agent(
                draft_id,
                generation_log=json.dumps(log),
            )
        )

    def _extract_required_credentials(self, tools_code: str) -> list:
        """Extract REQUIRED_CREDENTIALS from generated mcp_tools.py using AST (no exec)."""
        try:
            tree = ast.parse(tools_code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "REQUIRED_CREDENTIALS":
                            return ast.literal_eval(node.value)
        except Exception as e:
            logger.warning(f"Failed to extract REQUIRED_CREDENTIALS: {e}")
        return []

    # Spec Validation

    async def _validate_and_fix(self, draft_id: str, slug: str,
                                 tools_code: str, agent_name: str,
                                 description: str, websocket=None,
                                 max_retries: int = 2,
                                 static_only: bool = False,
                                 config_resolver=None,
                                 append_generation_log: Optional[
                                     Callable[[str], Awaitable[None]]
                                 ] = None) -> tuple:
        """Run spec validation with auto-fix retry loop.

        ``static_only`` (BYO, 058 G1/SC-002): the code under test is USER-AUTHORED
        and must never run on this host, so it is validated by pure AST inspection
        — registry shape, return contract, import allowlist. The orchestrator does
        not import it, exec it, or call its tools. Runtime behavior is the desktop
        host's business.

        Returns (final_code, validation_report).
        """
        for attempt in range(max_retries + 1):
            await self._send_progress(
                websocket, draft_id, "validating",
                f"Validating tool outputs against spec"
                f"{f' (attempt {attempt + 1})' if attempt > 0 else ''}...",
                VALIDATING,
            )

            if static_only:
                report = self.validator.validate_static(tools_code, slug)
            else:
                report = self.validator.validate(tools_code, slug, self._agents_dir)
            log_message = (
                f"Spec validation {'passed' if report.passed else 'failed'}: "
                f"{report.tools_passed}/{report.tools_tested} tools passed"
            )
            if append_generation_log is None:
                await asyncio.to_thread(
                    self._append_log,
                    draft_id,
                    log_message,
                )
            else:
                await append_generation_log(log_message)

            if report.passed:
                return tools_code, report

            if attempt < max_retries:
                # Build fix prompt from validation errors
                error_lines = []
                for f in report.findings:
                    if f.severity == "error":
                        prefix = f"[{f.tool_name}] " if f.tool_name else ""
                        error_lines.append(f"- {prefix}{f.message}")

                fix_prompt = (
                    "The generated tools FAILED spec validation with these errors:\n"
                    + "\n".join(error_lines)
                    + "\n\nFix ALL these issues. Ensure every tool returns "
                    "{'_ui_components': [c.to_dict() for c in components], '_data': {...}} "
                    "using the astralprims classes."
                )

                await self._send_progress(
                    websocket, draft_id, "auto_fixing",
                    f"Auto-fixing validation errors (attempt {attempt + 1}/{max_retries})...",
                    VALIDATING,
                )
                log_message = (
                    f"Auto-fix attempt {attempt + 1}: {fix_prompt[:200]}"
                )
                if append_generation_log is None:
                    await asyncio.to_thread(
                        self._append_log,
                        draft_id,
                        log_message,
                    )
                else:
                    await append_generation_log(log_message)

                try:
                    # The candidate is NOT promoted until it compiles. Assigning
                    # it to ``tools_code`` first meant a syntax-broken refinement
                    # (whose `continue` skips the disk write) became the value the
                    # function RETURNS — and on the BYO path that in-memory value
                    # is exactly what ships to the owner's host.
                    candidate = await self.generator.refine_tools_file(
                        current_code=tools_code,
                        user_message=fix_prompt,
                        agent_name=agent_name,
                        description=description,
                        self_contained=static_only,
                        config_resolver=config_resolver,
                    )

                    # Syntax check the fix
                    try:
                        compile(candidate, f"{slug}/mcp_tools.py", "exec")
                    except SyntaxError as e:
                        log_message = f"Auto-fix produced syntax error: {e}"
                        if append_generation_log is None:
                            await asyncio.to_thread(
                                self._append_log,
                                draft_id,
                                log_message,
                            )
                        else:
                            await append_generation_log(log_message)
                        continue  # Try again — keep the last COMPILING code

                    # Re-run the nefarious-activity gate on the refined bytes.
                    # The auto-fix loop used to be the one path where NEW code
                    # reached validator.validate() (which executes it) with a
                    # syntax check only — an LLM "fix" is exactly as untrusted
                    # as the original generation.
                    fix_report = self.security.analyze(
                        candidate, filename=f"{slug}/mcp_tools.py"
                    )
                    if blocks_execution(fix_report):
                        log_message = (
                            "Auto-fix refused by security analysis "
                            f"(max_severity={fix_report.max_severity}); "
                            "keeping the last clean code."
                        )
                        if append_generation_log is None:
                            await asyncio.to_thread(
                                self._append_log,
                                draft_id,
                                log_message,
                            )
                        else:
                            await append_generation_log(log_message)
                        continue

                    tools_code = candidate

                    # Server-hosted drafts retain their legacy editable working
                    # directory. BYO bytes stay in memory until the dedicated
                    # immutable publication seam has validated and committed the
                    # complete three-file revision.
                    if not static_only:
                        tools_file = os.path.join(
                            self._agents_dir, slug, "mcp_tools.py"
                        )
                        with open(tools_file, "w", encoding="utf-8") as fh:
                            fh.write(tools_code)

                except Exception as e:
                    log_message = f"Auto-fix failed: {e}"
                    if append_generation_log is None:
                        await asyncio.to_thread(
                            self._append_log,
                            draft_id,
                            log_message,
                        )
                    else:
                        await append_generation_log(log_message)
                    break

        return tools_code, report

    @staticmethod
    def _byo_import_violations(files: Dict[str, str]) -> List[str]:
        """Forbidden backend-coupling imports found anywhere in a BYO bundle."""
        from orchestrator.agent_generator import byo_import_violations
        found = []
        for fname, code in files.items():
            for pattern in byo_import_violations(code):
                found.append(f"{fname}: {pattern}")
        return found

    def _remove_draft_marker(self, slug: str):
        """Remove the .draft marker file when an agent is promoted to live."""
        marker = os.path.join(self._agents_dir, slug, ".draft")
        if os.path.exists(marker):
            os.remove(marker)
            logger.info(f"Removed .draft marker for {slug}")

    # Slug Sanitization

    def _sanitize_slug(self, name: str) -> str:
        """Convert agent name to a safe directory slug. Alphanumeric + underscores only."""
        slug = re.sub(r'[^a-z0-9]+', '_', name.lower().strip())
        slug = slug.strip('_')
        if not slug:
            slug = 'custom_agent'
        # Prevent path traversal
        slug = slug.replace('..', '').replace('/', '').replace('\\', '')
        return slug

    def _ensure_unique_slug(self, slug: str) -> str:
        """Ensure slug doesn't conflict with existing agent directories."""
        base_slug = slug
        counter = 1
        while os.path.exists(os.path.join(self._agents_dir, slug)):
            slug = f"{base_slug}_{counter}"
            counter += 1
        return slug

    # Create Draft

    async def create_draft(
        self,
        user_id: str,
        agent_name: str,
        description: str,
        tools_spec: List[Dict] = None,
        skill_tags: List[str] = None,
        packages: List[str] = None,
        revises_agent_id: Optional[str] = None,
        *,
        target_agent_id: Optional[str] = None,
        origin: str = "manual",
        source_chat_id: Optional[str] = None,
        gap_fingerprint: Optional[str] = None,
        source_attachment_id: Optional[str] = None,
        plan_json: Optional[str] = None,
        constitution_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new draft agent record."""
        # Validate
        if not agent_name or len(agent_name.strip()) < 2:
            raise ValueError("Agent name must be at least 2 characters")
        if not description or len(description.strip()) < 10:
            raise ValueError("Description must be at least 10 characters")
        if len(agent_name) > 100:
            raise ValueError("Agent name must be under 100 characters")

        draft_id = str(uuid.uuid4())
        # The storage slug is deliberately identity-suffixed. A filesystem
        # exists-check followed by insert is not an allocation primitive: two
        # replicas could observe the same free name. The immutable UUID suffix
        # makes same-name draft storage collision-free without a shared lock.
        slug_base = self._sanitize_slug(agent_name)[:48]
        slug = f"{slug_base}_{draft_id.replace('-', '')[:12]}"

        create_values = {
            "draft_id": draft_id,
            "user_id": user_id,
            "agent_name": agent_name.strip(),
            "agent_slug": slug,
            "description": description.strip(),
            "tools_spec": json.dumps(tools_spec) if tools_spec else None,
            "skill_tags": json.dumps(skill_tags) if skill_tags else None,
            "packages": json.dumps(packages) if packages else None,
            "origin": origin,
            "source_chat_id": source_chat_id,
            "gap_fingerprint": gap_fingerprint,
            "revises_agent_id": revises_agent_id,
            "target_agent_id": target_agent_id,
            "plan_json": plan_json,
            "constitution_version": constitution_version,
        }
        if source_attachment_id is not None:
            create_values["source_attachment_id"] = source_attachment_id
        await asyncio.to_thread(
            self.db.create_draft_agent,
            **create_values,
        )

        logger.info(f"Created draft agent '{agent_name}' (id={draft_id}, slug={slug}) for user {user_id}")
        return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

    # Generate Code

    async def generate_code(self, draft_id: str, websocket=None, *,
                            target: str = BACKEND_TARGET,
                            agent_id: Optional[str] = None,
                            expected_state_revision: Optional[int] = None,
                            generation_claim_id: Optional[str] = None) -> Dict[str, Any]:
        """Generate the complete executable file set for a draft.

        Args:
            target: ``backend`` (027 — server-hosted: agent.py + mcp_server.py +
                mcp_tools.py, run as a subprocess here) or ``byo`` (060 — the
                self-contained desktop bundle: agent_main.py +
                astralprims_ui.py + protected_executor.py + mcp_tools.py plus
                its deterministic runtime manifest, delivered to the owner's
                host and never run here).
            agent_id: the identity to bake into the generated card. BYO must use
                the immutable ``target_agent_id`` persisted on the draft; only
                legacy server-hosted drafts retain the slug-derived default.
            expected_state_revision: revision the caller observed. A stale value
                returns the current revision and a ``refresh`` action without
                running generation.
            generation_claim_id: optional UUID4 idempotency identity. Retries may
                reuse it only while the same live claim and revision remain.

        Returns the draft row, plus a ``files`` key holding the FINAL bundle
        (post auto-fix) on success. Callers deliver from that key — the generated
        source is otherwise only reachable off disk.
        """
        draft = await asyncio.to_thread(self.db.get_draft_agent, draft_id)
        if not draft:
            raise ValueError(f"Draft {draft_id} not found")

        # 058 SC-002 — the sandbox/exec decision is a property of the ROW, not of
        # the caller's argument. Keying it on ``target`` alone let any caller that
        # omits it (e.g. the REST endpoint POST /api/agents/drafts/{id}/generate)
        # run a BYO draft's user-authored code in-process. Derive it from origin,
        # and refuse target=backend for a BYO draft outright — the same structural
        # refusal start_draft_agent/approve_agent make.
        is_byo_origin = draft.get("origin") == BYO_ORIGIN
        if is_byo_origin and target != BYO_TARGET:
            raise ValueError(
                f"Draft {draft_id} is a BYO agent — it can only be generated as a "
                f"self-contained bundle for the owner's desktop host, never as a "
                f"server-hosted ('{target}') agent (058 SC-002)."
            )
        is_byo = is_byo_origin or (target == BYO_TARGET)
        if is_byo and self.generated_agent_publication_service is None:
            raise RuntimeError(
                "Plane generated-agent publication service is unavailable"
            )
        if is_byo:
            persisted_target_agent_id = draft.get("target_agent_id")
            if (
                not isinstance(persisted_target_agent_id, str)
                or not persisted_target_agent_id
            ):
                raise RuntimeError("BYO draft target_agent_id is unavailable")
            if agent_id is not None and agent_id != persisted_target_agent_id:
                raise ValueError("BYO agent_id does not match the draft target")
            agent_id = persisted_target_agent_id
        else:
            agent_id = agent_id or self.generator.default_agent_id(
                str(draft["agent_slug"])
            )

        observed_revision = int(draft.get("state_revision") or 0)
        if expected_state_revision is None:
            expected_state_revision = observed_revision
        if type(expected_state_revision) is not int or expected_state_revision < 0:
            raise ValueError("expected_state_revision must be non-negative")
        if generation_claim_id is None:
            generation_claim_id = str(uuid.uuid4())
        else:
            parsed_claim_id = uuid.UUID(str(generation_claim_id))
            if parsed_claim_id.version != 4:
                raise ValueError("generation_claim_id must be a UUID4")
            generation_claim_id = str(parsed_claim_id)

        owner_user_id = str(draft.get("user_id") or "")
        draft_uuid = str(draft.get("draft_uuid") or draft_id)
        if is_byo:
            # A lost response retry supplies the original observed revision and
            # claim id. Plane increments the draft revision exactly once when
            # that claim is acquired, so the durable journal source is the next
            # revision. Re-open exact published bytes before attempting a new
            # claim or invoking the model again.
            replay_source_revision = expected_state_revision + 1
            replay_identity = generated_agent_publication_identity(
                owner_id=owner_user_id,
                draft_uuid=draft_uuid,
                source_state_revision=replay_source_revision,
                generation_claim_id=generation_claim_id,
                target_agent_id=agent_id,
            )
            replay = await self.generated_agent_publication_service.load_published(
                owner_id=owner_user_id,
                draft_uuid=draft_uuid,
                source_state_revision=replay_source_revision,
            )
            if replay is not None:
                exact_replay = (
                    str(replay.publication.publication_id)
                    == str(replay_identity.publication_id)
                    and str(replay.revision.revision_id)
                    == str(replay_identity.target_revision_id)
                    and str(replay.revision.agent_id) == agent_id
                )
                current = dict(
                    await asyncio.to_thread(self.db.get_draft_agent, draft_id)
                    or {}
                )
                if not exact_replay:
                    current.update(
                        {
                            "status": "conflict",
                            "generation_outcome": "conflict",
                            "current_revision": int(
                                current.get("state_revision") or 0
                            ),
                            "refresh": "refresh",
                        }
                    )
                    return current
                current["generation_outcome"] = "replayed"
                return _attach_generated_publication(current, replay)

        claim_task = asyncio.create_task(
            asyncio.to_thread(
                self.db.claim_draft_generation,
                draft_id=draft_id,
                owner_user_id=owner_user_id,
                expected_revision=expected_state_revision,
                claim_id=generation_claim_id,
            ),
            name=f"agent-generation-claim-{draft_id}",
        )
        try:
            claimed = await asyncio.shield(claim_task)
        except asyncio.CancelledError as cancellation:
            claim_error: Optional[BaseException] = None
            try:
                claimed, _ = await _join_task_through_cancellation(
                    claim_task
                )
            except BaseException as caught_claim_error:
                claim_error = caught_claim_error
                lookup_task = asyncio.create_task(
                    asyncio.to_thread(
                        self.db.get_exact_live_draft_generation_claim,
                        draft_id=draft_id,
                        owner_user_id=owner_user_id,
                        expected_preclaim_revision=expected_state_revision,
                        claim_id=generation_claim_id,
                    ),
                    name=f"agent-generation-claim-lookup-{draft_id}",
                )
                try:
                    current, _ = await _join_task_through_cancellation(
                        lookup_task
                    )
                except BaseException as lookup_error:
                    lookup_error.__cause__ = claim_error
                    raise cancellation from lookup_error
                claimed = current
            if claimed is not None:
                cancellation_finish = asyncio.create_task(
                    asyncio.to_thread(
                        self.db.finish_draft_generation,
                        draft_id=draft_id,
                        owner_user_id=owner_user_id,
                        expected_revision=int(claimed["state_revision"]),
                        claim_id=generation_claim_id,
                        status=ERROR,
                        error_message="Code generation was cancelled.",
                    ),
                    name=f"agent-generation-claim-cancel-{draft_id}",
                )
                try:
                    await _join_task_through_cancellation(
                        cancellation_finish
                    )
                except BaseException as cleanup_error:
                    raise cancellation from cleanup_error
            if claim_error is not None:
                raise cancellation from claim_error
            raise
        except Exception as claim_error:
            # A database/network acknowledgement can be lost after the claim
            # transaction commits.  Resolve that ambiguity from the durable
            # owner+draft+claim identity before invoking the model; abandoning
            # an exact committed claim here would leave a live ``generating``
            # row that no publication-journal reconciler can yet discover.
            lookup_task = asyncio.create_task(
                asyncio.to_thread(
                    self.db.get_exact_live_draft_generation_claim,
                    draft_id=draft_id,
                    owner_user_id=owner_user_id,
                    expected_preclaim_revision=expected_state_revision,
                    claim_id=generation_claim_id,
                ),
                name=f"agent-generation-claim-lookup-{draft_id}",
            )
            current, lookup_error, lookup_cancellation = (
                await _join_task_outcome_through_cancellation(lookup_task)
            )
            if lookup_error is not None:
                ambiguity = RuntimeError(
                    "generation claim acknowledgement and authoritative "
                    "lookup both failed; retry with the same claim identity"
                )
                ambiguity.add_note(
                    "claim acknowledgement error: "
                    f"{type(claim_error).__name__}: {claim_error}"
                )
                if lookup_cancellation is not None:
                    ambiguity.__cause__ = lookup_error
                    raise lookup_cancellation from ambiguity
                raise ambiguity from lookup_error
            claimed = current
            if lookup_cancellation is not None:
                if claimed is not None:
                    cancellation_finish = asyncio.create_task(
                        asyncio.to_thread(
                            self.db.finish_draft_generation,
                            draft_id=draft_id,
                            owner_user_id=owner_user_id,
                            expected_revision=int(claimed["state_revision"]),
                            claim_id=generation_claim_id,
                            status=ERROR,
                            error_message="Code generation was cancelled.",
                        ),
                        name=f"agent-generation-claim-cancel-{draft_id}",
                    )
                    try:
                        await _join_task_through_cancellation(
                            cancellation_finish
                        )
                    except BaseException as cleanup_error:
                        raise lookup_cancellation from cleanup_error
                raise lookup_cancellation from claim_error
            if claimed is None:
                raise claim_error
            logger.warning(
                "generation claim acknowledgement was lost; continuing from "
                "the exact durable claim",
                exc_info=claim_error,
            )
        if claimed is None:
            current = dict(
                await asyncio.to_thread(self.db.get_draft_agent, draft_id) or {}
            )
            current.update(
                {
                    "status": "conflict",
                    "generation_outcome": "conflict",
                    "current_revision": int(current.get("state_revision") or 0),
                    "refresh": "refresh",
                }
            )
            return current

        # Every byte and every policy decision below is derived from the exact
        # claimed row. An edit increments state_revision and makes both artifact
        # publication and terminalization fail closed.
        draft = dict(claimed)
        claimed_revision = int(draft["state_revision"])
        claim_resolved = False
        claim_heartbeat: GenerationClaimHeartbeat | None = (
            GenerationClaimHeartbeat(
                lambda: self.db.renew_draft_generation(
                    draft_id=draft_id,
                    owner_user_id=owner_user_id,
                    expected_revision=claimed_revision,
                    claim_id=generation_claim_id,
                    lease_seconds=300,
                ),
                interval_seconds=_GENERATION_CLAIM_RENEW_INTERVAL_SECONDS,
                task_name=f"agent-generation-claim-heartbeat-{draft_id}",
            )
        )
        claim_heartbeat.start()

        async def append_generation_log(message: str) -> None:
            appended = await asyncio.to_thread(
                self._append_log,
                draft_id,
                message,
                owner_user_id=owner_user_id,
                expected_revision=claimed_revision,
                claim_id=generation_claim_id,
            )
            if not appended:
                raise RuntimeError(
                    "draft generation progress claim fence is stale"
                )

        def assert_generation_claim() -> None:
            heartbeat = claim_heartbeat
            if heartbeat is not None:
                heartbeat.assert_healthy()

        async def stop_generation_claim_heartbeat() -> None:
            nonlocal claim_heartbeat
            heartbeat = claim_heartbeat
            if heartbeat is None:
                return
            claim_heartbeat = None
            try:
                await heartbeat.close()
            except asyncio.CancelledError:
                raise
            except BaseException:
                # The terminal database transition below remains the authority
                # check.  If renewal actually lost the claim it returns a
                # conflict; a completed heartbeat worker is never left behind.
                logger.warning(
                    "draft generation claim heartbeat stopped with an error",
                    exc_info=True,
                )

        async def finish_generation(
            status: str,
            *,
            error_message: Optional[str] = None,
            security_report: Optional[str] = None,
            validation_report: Optional[str] = None,
            required_credentials: Optional[str] = None,
        ) -> Dict[str, Any]:
            nonlocal claim_resolved

            await stop_generation_claim_heartbeat()

            async def finish_operation() -> Dict[str, Any]:
                finished = await asyncio.to_thread(
                    self.db.finish_draft_generation,
                    draft_id=draft_id,
                    owner_user_id=owner_user_id,
                    expected_revision=claimed_revision,
                    claim_id=generation_claim_id,
                    status=status,
                    error_message=error_message,
                    security_report=security_report,
                    validation_report=validation_report,
                    required_credentials=required_credentials,
                )
                if finished is not None:
                    return dict(finished)
                current = dict(
                    await asyncio.to_thread(
                        self.db.get_draft_agent,
                        draft_id,
                    )
                    or {}
                )
                current.update(
                    {
                        "status": "conflict",
                        "generation_outcome": "conflict",
                        "current_revision": int(
                            current.get("state_revision") or 0
                        ),
                        "refresh": "refresh",
                    }
                )
                return current

            finish_task = asyncio.create_task(
                finish_operation(),
                name=f"agent-generation-finish-{draft_id}",
            )
            try:
                result = await asyncio.shield(finish_task)
            except asyncio.CancelledError as cancellation:
                try:
                    result, _ = await _join_task_through_cancellation(finish_task)
                except BaseException as finish_error:
                    raise cancellation from finish_error
                claim_resolved = True
                raise
            claim_resolved = True
            return result

        async def terminalize_cancelled_claim(
            cancellation: asyncio.CancelledError,
        ) -> None:
            if claim_resolved:
                return
            terminalization = asyncio.create_task(
                finish_generation(
                    ERROR,
                    error_message="Code generation was cancelled.",
                ),
                name=f"agent-generation-cancel-{draft_id}",
            )
            try:
                await _join_task_through_cancellation(terminalization)
            except BaseException as cleanup_error:
                raise cancellation from cleanup_error

        try:
            slug = draft["agent_slug"]
            agent_name = draft["agent_name"]
            description = draft["description"]
            tools_spec = (
                json.loads(draft["tools_spec"])
                if draft.get("tools_spec")
                else []
            )
            skill_tags = (
                json.loads(draft["skill_tags"])
                if draft.get("skill_tags")
                else []
            )
            packages = (
                json.loads(draft["packages"])
                if draft.get("packages")
                else []
            )
            # BYO codegen uses the OWNER's LLM, not the admin-managed system
            # credential. Owner context is resolved for each interactive call.
            codegen_resolver = None
            if (
                is_byo
                and websocket is not None
                and self.orchestrator is not None
            ):
                _orch = self.orchestrator
                try:
                    _uid = _orch._llm_context_user_id(websocket)
                    if _uid:
                        def owner_codegen_resolver():
                            return _orch._llm_store.get_sync(_uid)

                        codegen_resolver = owner_codegen_resolver
                except Exception:
                    logger.debug(
                        "byo codegen: owner LLM resolution unavailable, "
                        "falling back to system resolver",
                        exc_info=True,
                    )

            await append_generation_log("Starting code generation...")

            # Step 1: Generate template files (no LLM needed)
            await self._send_progress(websocket, draft_id, "generating_template",
                                       "Generating agent template files...", GENERATING)
            await append_generation_log("Generating template files...")

            revision_id = None
            publication_identity = None
            if is_byo:
                from orchestrator.agent_constitution import AGENT_CONSTITUTION_VERSION

                publication_identity = generated_agent_publication_identity(
                    owner_id=owner_user_id,
                    draft_uuid=draft_uuid,
                    source_state_revision=claimed_revision,
                    generation_claim_id=generation_claim_id,
                    target_agent_id=agent_id,
                )
                revision_id = str(publication_identity.target_revision_id)
                template_files = await asyncio.to_thread(
                    self.generator.generate_byo_scaffold,
                    agent_name=agent_name,
                    description=description,
                    agent_id=agent_id,
                    skill_tags=skill_tags,
                )
            else:
                template_files = self.generator.generate_template_files(
                    agent_name=agent_name,
                    description=description,
                    slug=slug,
                    skill_tags=skill_tags,
                    agent_id=agent_id,
                )

            # Step 2: Generate tools via LLM
            await self._send_progress(websocket, draft_id, "generating_tools",
                                       "Generating tool implementations with AI...", GENERATING)
            await append_generation_log("Generating tool implementations...")

            # Inject knowledge context if available
            knowledge_context = ""
            if hasattr(self.orchestrator, 'knowledge_index'):
                knowledge_context = self.orchestrator.knowledge_index.get_generation_context(description)

            # C-N4 evolutionary archive: condition codegen on past successful
            # exemplars for a similar capability gap. Flag-gated + fail-open —
            # OFF / empty archive leaves knowledge_context byte-identical.
            try:
                from orchestrator import draft_archive
                if draft_archive.archive_enabled():
                    fp = draft_archive.draft_fingerprint(draft)
                    knowledge_context = draft_archive.exemplar_prompt_for(
                        knowledge_context,
                        fp,
                        owner_user_id=owner_user_id,
                    )
            except Exception:  # pragma: no cover — conditioning is best-effort
                logger.debug("draft-archive: codegen conditioning skipped", exc_info=True)

            tools_code = await self.generator.generate_tools_file(
                agent_name=agent_name,
                description=description,
                tools_spec=tools_spec,
                packages=packages,
                knowledge_context=knowledge_context,
                self_contained=is_byo,
                config_resolver=codegen_resolver,
            )
            assert_generation_claim()

            all_files = {**template_files, "mcp_tools.py": tools_code}

            # Step 2.5: Syntax validation on ALL generated files
            await self._send_progress(websocket, draft_id, "syntax_check",
                                       "Validating Python syntax...", GENERATING)
            await append_generation_log("Validating syntax of generated files...")

            for fname, code in all_files.items():
                if not fname.endswith(".py"):
                    continue
                try:
                    compile(code, f"{slug}/{fname}", "exec")
                except SyntaxError as e:
                    error_msg = f"Syntax error in {fname} (line {e.lineno}): {e.msg}"
                    logger.error(f"Generated code has syntax error: {error_msg}")
                    await append_generation_log(f"SYNTAX ERROR: {error_msg}")
                    state = await finish_generation(ERROR, error_message=error_msg)
                    await self._send_progress(websocket, draft_id, "syntax_error",
                                               error_msg, ERROR)
                    return state

            # Step 2.6 (BYO): the bundle must be self-contained — the desktop host
            # ships no backend package, so a `from shared…` import is a dead agent
            # on the user's machine. Gate the LLM's file, don't just ask for it.
            if is_byo:
                bad = self._byo_import_violations(all_files)
                if bad:
                    error_msg = ("Generated bundle is not self-contained "
                                 f"(forbidden imports: {bad}). Not delivered.")
                    await append_generation_log(f"BYO GATE: {error_msg}")
                    state = await finish_generation(ERROR, error_message=error_msg)
                    await self._send_progress(websocket, draft_id, "not_self_contained",
                                               error_msg, ERROR)
                    return state

            # Step 3: Security analysis
            await self._send_progress(websocket, draft_id, "security_scan",
                                       "Running security analysis...", GENERATING)
            await append_generation_log("Running security analysis on generated code...")

            report = self.security.analyze(tools_code, filename=f"{slug}/mcp_tools.py")

            # Pre-execution nefarious-activity gate: HIGH (os.environ access,
            # globals()/setattr tricks, obfuscation) blocks alongside CRITICAL
            # — nothing below this line may run code the analyzer flagged.
            if blocks_execution(report):
                await append_generation_log(
                    f"Security analysis FAILED: {report.recommendation}"
                )
                state = await finish_generation(
                    ERROR,
                    security_report=json.dumps(report.to_dict()),
                    error_message="Security analysis found blocking issues in generated code.",
                )
                await self._send_progress(websocket, draft_id, "security_failed",
                                           "Security analysis found blocking issues. Code was not written.",
                                           ERROR, detail=report.to_dict())
                return state

            # Step 4: Write server-hosted draft working files to disk. BYO
            # executable bytes are not written into the shared slug directory;
            # they remain in memory until the immutable revision publisher
            # commits the complete, validated bundle below.
            assert_generation_claim()
            await self._send_progress(websocket, draft_id, "writing_files",
                                       "Writing agent files...", GENERATING)
            await append_generation_log("Writing agent files to disk...")

            if not is_byo:
                agent_dir = os.path.join(self._agents_dir, slug)
                os.makedirs(agent_dir, exist_ok=True)

                # Write draft marker — start.py skips directories with .draft
                with open(
                    os.path.join(agent_dir, ".draft"), "w", encoding="utf-8"
                ) as marker_file:
                    marker_file.write(draft_id)

                init_content = f'"""Auto-generated agent: {agent_name}"""\n'
                with open(
                    os.path.join(agent_dir, "__init__.py"), "w", encoding="utf-8"
                ) as init_file:
                    init_file.write(init_content)

                for filename, content in all_files.items():
                    filepath = os.path.join(agent_dir, filename)
                    with open(filepath, "w", encoding="utf-8") as generated_file:
                        generated_file.write(content)

            # Step 5: Spec validation (with auto-fix retry). The 027 validator
            # RUNS the generated tools; BYO (user-authored) code is validated
            # STATICALLY and is never imported, exec'd, or called on this host
            # (058 G1/SC-002).
            tools_code, validation_report = await self._validate_and_fix(
                draft_id=draft_id, slug=slug, tools_code=tools_code,
                agent_name=agent_name, description=description,
                websocket=websocket, static_only=is_byo,
                config_resolver=codegen_resolver,
                append_generation_log=append_generation_log,
            )
            assert_generation_claim()

            # An auto-fix round could reintroduce a backend import — re-gate the
            # code we are actually about to hand the host.
            if is_byo:
                bad = self._byo_import_violations({"mcp_tools.py": tools_code})
                if bad:
                    error_msg = ("Auto-fixed bundle is not self-contained "
                                 f"(forbidden imports: {bad}). Not delivered.")
                    await append_generation_log(f"BYO GATE: {error_msg}")
                    state = await finish_generation(ERROR, error_message=error_msg)
                    await self._send_progress(websocket, draft_id, "not_self_contained",
                                               error_msg, ERROR)
                    return state

            # Step 5.5: Extract required credentials declared by LLM
            required_creds = self._extract_required_credentials(tools_code)
            if required_creds:
                await append_generation_log(
                    f"Detected {len(required_creds)} required credential(s)"
                )
                await self._send_progress(
                    websocket, draft_id, "credentials_detected",
                    f"This agent requires {len(required_creds)} credential(s). You'll need to provide them before testing.",
                    GENERATING,
                    detail={"required_credentials": required_creds},
                )

            # Step 6: Finalize and durably publish the exact BYO revision before
            # reporting generation success. Runtime start/ready/promotion remains
            # a separate lifecycle transaction owned by AgentRevisionActivator.
            assert_generation_claim()
            update_kwargs = {
                "security_report": (
                    json.dumps(report.to_dict()) if report.findings else None
                ),
                "validation_report": json.dumps(validation_report.to_dict()),
                "required_credentials": (
                    json.dumps(required_creds) if required_creds else None
                ),
            }
            if not validation_report.passed:
                update_kwargs["error_message"] = (
                    f"Spec validation failed: {validation_report.tools_passed}/"
                    f"{validation_report.tools_tested} tools passed. "
                    "You can still test manually or refine the agent."
                )

            finalized = None
            published_result = None
            final_files = {**template_files, "mcp_tools.py": tools_code}
            if is_byo:
                finalized = self.generator.finalize_byo_bundle(
                    files=final_files,
                    agent_id=agent_id,
                    revision_id=revision_id,
                    agent_name=agent_name,
                    description=description,
                    constitution_version=AGENT_CONSTITUTION_VERSION,
                    required_runtime_lock_sha256=self._byo_runtime_lock_sha256,
                )
                await self._send_progress(
                    websocket,
                    draft_id,
                    "publishing_artifact",
                    "Publishing immutable agent revision...",
                    GENERATING,
                )
                if publication_identity is None:
                    raise RuntimeError("publication identity was not derived")
                service = self.generated_agent_publication_service
                if service is None:  # pragma: no cover - checked before claim.
                    raise RuntimeError(
                        "Plane generated-agent publication service is unavailable"
                    )
                await stop_generation_claim_heartbeat()
                request = GeneratedAgentPublicationRequest(
                    owner_id=owner_user_id,
                    draft_uuid=draft_uuid,
                    source_state_revision=claimed_revision,
                    generation_claim_id=generation_claim_id,
                    target_agent_id=agent_id,
                    bundle=finalized,
                    runtime_contract_version=self._byo_runtime_contract_version,
                    release_lock_digest=self._byo_runtime_lock_sha256,
                    generation_result=GeneratedAgentPublicationResultMetadata(
                        **update_kwargs
                    ),
                )
                try:
                    published_result = await service.publish(request)
                except asyncio.CancelledError as cancellation:
                    if getattr(cancellation.__cause__, "claim_managed", False):
                        claim_resolved = True
                    raise
                except GeneratedAgentPublicationRecoveryPendingError:
                    claim_resolved = True
                    raise
                except GeneratedAgentPublicationManagedError:
                    claim_resolved = True
                    state = dict(
                        await asyncio.to_thread(
                            self.db.get_draft_agent,
                            draft_id,
                        )
                        or {}
                    )
                    state["generation_outcome"] = "error"
                    await self._send_progress(
                        websocket,
                        draft_id,
                        "error",
                        "Generated bundle publication failed safely.",
                        ERROR,
                    )
                    return state
                claim_resolved = True
                state = dict(
                    await asyncio.to_thread(self.db.get_draft_agent, draft_id)
                    or {}
                )
                if state.get("status") != GENERATED:
                    raise GeneratedAgentPublicationRecoveryPendingError(
                        "published journal did not converge the draft state"
                    )
            else:
                state = await finish_generation(GENERATED, **update_kwargs)

            if state.get("generation_outcome") == "conflict":
                return state

            status_msg = (
                "Agent files generated and validated successfully!"
                if validation_report.passed
                else f"Agent generated but validation found issues "
                     f"({validation_report.tools_passed}/{validation_report.tools_tested} tools passed). "
                     "Review the validation report or refine the agent."
            )
            await self._send_progress(websocket, draft_id, "complete",
                                       status_msg, GENERATED,
                                       detail={
                                           "security": report.to_dict() if report.findings else None,
                                           "validation": validation_report.to_dict(),
                                       })

            # Hand the caller the re-opened immutable bundle (mcp_tools.py may
            # have been auto-fixed since first generation). The manifest is
            # metadata about Plane's exact immutable executable-file set.
            if is_byo:
                if finalized is None or published_result is None:
                    raise RuntimeError("immutable BYO publication was not completed")
                state = _attach_generated_publication(state, published_result)
            else:
                state["files"] = final_files
            return state

        except asyncio.CancelledError as cancellation:
            await terminalize_cancelled_claim(cancellation)
            raise
        except GeneratedAgentPublicationRecoveryPendingError:
            # A durable nonterminal journal row retains the claim. Returning a
            # generic draft conflict would hide the recovery obligation from
            # readiness and from the next bounded reconciliation pass.
            raise
        except Exception as e:
            logger.exception("Code generation failed for draft %s: %s", draft_id, e)
            if claim_resolved:
                raise
            try:
                state = await finish_generation(ERROR, error_message=str(e))
            except BaseException as cleanup_error:
                raise e from cleanup_error
            await self._send_progress(websocket, draft_id, "error",
                                       f"Code generation failed: {e}", ERROR)
            return state
        finally:
            await stop_generation_claim_heartbeat()

    # Start Draft Agent for Testing

    def _find_next_port(self) -> int:
        """Find the next available port for a draft agent."""
        start_port = int(os.environ.get("AGENT_PORT", 8003))
        max_agents = int(os.environ.get("MAX_AGENTS", 10))

        # Collect ports in use by connected agents
        used_ports = set()
        if self.orchestrator:
            for agent_id, url in getattr(self.orchestrator, 'agent_urls', {}).items():
                try:
                    port = int(url.split(':')[-1])
                    used_ports.add(port)
                except (ValueError, IndexError):
                    pass

        # Also check ports used by other draft agents
        for draft_id, proc in self._draft_processes.items():
            if proc.poll() is None:  # still running
                draft = self.db.get_draft_agent(draft_id)
                if draft and draft.get("port"):
                    used_ports.add(draft["port"])

        # Find first available port, starting after the static agents range
        # Static agents use start_port to start_port + max_agents
        # Draft agents start after that
        search_start = start_port + max_agents
        for port in range(search_start, search_start + 50):
            if port not in used_ports:
                return port

        raise RuntimeError("No available ports for draft agent")

    async def start_draft_agent(self, draft_id: str, websocket=None,
                                align_scopes: bool = True) -> Dict[str, Any]:
        """Start a draft agent subprocess for testing.

        ``align_scopes=False`` starts the process WITHOUT rewriting ownership
        or enabling all scopes — used when restarting an already-live agent
        (startup relaunch, revision swap), where the testing-mode defaults
        would clobber the user's saved permissions and reset a public agent to
        private.
        """
        draft = await asyncio.to_thread(self.db.get_draft_agent, draft_id)
        if not draft:
            raise ValueError(f"Draft {draft_id} not found")

        # 058 SC-002 — a BYO agent's code is the USER'S, and it runs on the
        # user's desktop host. Refuse structurally rather than trusting every
        # call site (the boot relaunch nearly Popen'd these): there is no
        # legitimate path that starts a byo_client draft on this host.
        if draft.get("origin") == BYO_ORIGIN:
            raise ValueError(
                f"Refusing to start BYO agent draft {draft_id} on the orchestrator "
                "host — user agents run on the owner's desktop client (058 SC-002)."
            )

        if draft["status"] not in (GENERATED, TESTING, APPROVED, LIVE):
            raise ValueError(f"Cannot start agent in status '{draft['status']}'. Generate code first.")

        slug = draft["agent_slug"]
        agent_dir = os.path.join(self._agents_dir, slug)
        agent_script = os.path.join(agent_dir, f"{slug}_agent.py")

        if not os.path.exists(agent_script):
            raise FileNotFoundError(f"Agent script not found: {agent_script}")

        # Stop existing process if any
        await self.stop_draft_agent(draft_id)

        port = await asyncio.to_thread(self._find_next_port)
        python_exe = sys.executable

        await self._send_progress(websocket, draft_id, "starting_agent",
                                   f"Starting agent on port {port}...", TESTING)
        await asyncio.to_thread(self._append_log, draft_id, f"Starting agent on port {port}...")

        # When enabled, wrap the generated-code child in an OS-level sandbox —
        # resource limits (fork-time preexec), a temp-scoped filesystem, and a
        # secret-scrubbed env. Flag-gated + fail-open: off / non-POSIX / any
        # setup error launches exactly as before.
        sandbox_kwargs: Dict[str, Any] = {}
        try:
            from orchestrator import sandbox as _sandbox
            if _sandbox.sandbox_enabled():
                tmpdir = os.path.join(agent_dir, "_sandbox_tmp")
                os.makedirs(tmpdir, exist_ok=True)
                limits = _sandbox.build_limits()
                preexec = _sandbox.make_preexec(limits)
                if preexec is not None:
                    sandbox_kwargs["preexec_fn"] = preexec
                sandbox_kwargs["env"] = _sandbox.sandbox_env(None, tmpdir)
                logger.info("C-S6 sandbox: launching draft %s with %s", draft_id, limits)
        except Exception:
            logger.exception("C-S6 sandbox setup failed; launching unsandboxed")
            sandbox_kwargs = {}

        process_id = uuid.uuid4()
        await self._admit_dynamic_runtime(
            draft,
            runtime_id=str(process_id),
            tools_file=os.path.join(agent_dir, "mcp_tools.py"),
        )
        try:
            proc = self.process_supervisor.spawn(
                process_id=process_id,
                owner=ProcessOwner(owner_kind="draft_agent", owner_id=draft_id),
                argv=(python_exe, agent_script, "--port", str(port)),
                cwd=agent_dir,
                **sandbox_kwargs,
            )
        except Exception:
            await self._close_dynamic_runtime(draft)
            raise
        self._draft_processes[draft_id] = proc

        await asyncio.to_thread(self.db.update_draft_agent, draft_id, status=TESTING, port=port)

        # Wait for agent to start up, then actively discover it with the orchestrator
        agent_id = f"{slug.replace('_', '-')}-1"
        agent_url = f"http://localhost:{port}"
        discovered = False

        if self.orchestrator:
            # Retry discovery a few times — the subprocess needs time to bind the port
            for attempt in range(6):
                await asyncio.sleep(2)
                # Check if process is still alive
                if proc.poll() is not None:
                    snapshot = await asyncio.to_thread(proc.wait)
                    stderr_out = b"\n".join(snapshot.stderr.lines).decode(
                        "utf-8", "replace"
                    )
                    error_msg = f"Agent process exited with code {proc.returncode}"
                    if stderr_out:
                        error_msg += f": {stderr_out[:500]}"
                    logger.error(error_msg)
                    await asyncio.to_thread(self.db.update_draft_agent, draft_id, status=ERROR, error_message=error_msg)
                    await self._close_dynamic_runtime(draft)
                    await self._send_progress(websocket, draft_id, "error", error_msg, ERROR)
                    await asyncio.to_thread(self._append_log, draft_id, f"ERROR: {error_msg}")
                    return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

                try:
                    await self.orchestrator.discover_agent(agent_url)
                    if agent_id in self.orchestrator.agents:
                        discovered = True
                        logger.info(f"Draft agent {agent_id} discovered on port {port}")
                        break
                except Exception as e:
                    logger.debug(f"Discovery attempt {attempt+1} for draft agent on port {port}: {e}")
        else:
            await asyncio.sleep(2)

        # Set ownership to creator (private by default). Skipped on relaunch
        # (align_scopes=False) so a user-set public flag is not reset.
        if align_scopes:
            user = await asyncio.to_thread(self.db.get_user, draft["user_id"])
            owner_email = user.get("email", draft["user_id"]) if user else draft["user_id"]
            await asyncio.to_thread(self.db.set_agent_ownership, agent_id, owner_email=owner_email, is_public=False)

        # Draft agents: all scopes ENABLED so the user can test tools.
        # Scopes get disabled when the agent is approved/moved to live.
        if self.orchestrator and align_scopes:
            await asyncio.to_thread(
                self.orchestrator.tool_permissions.set_agent_scopes,
                draft["user_id"], agent_id,
                {"tools:read": True, "tools:write": True, "tools:search": True, "tools:system": True}
            )
            # Per-(tool, kind) rows added by the permissions endpoint backfill
            # take priority over agent_scopes in is_tool_allowed. If the user
            # opened the permissions modal BEFORE starting the draft (when
            # scopes default to False), those rows would be False and would
            # shadow the True scope state we just wrote — leaving the user with
            # "scopes are enabled" but tools still blocked. Force the per-tool
            # rows to match the draft's True scope state so both layers agree.
            try:
                tool_scope_map = await asyncio.to_thread(self.orchestrator.tool_permissions.get_tool_scope_map, agent_id)
                for tool_name, required_scope in tool_scope_map.items():
                    await asyncio.to_thread(
                        self.orchestrator.tool_permissions.set_tool_permission,
                        draft["user_id"], agent_id, tool_name, required_scope, True
                    )
            except Exception as e:  # pragma: no cover — defensive
                logger.warning(f"Per-tool alignment failed for draft={agent_id}: {e}")

        if discovered:
            await self._send_progress(websocket, draft_id, "agent_started",
                                       f"Agent running on port {port} and registered with orchestrator.",
                                       TESTING)
            await asyncio.to_thread(self._append_log, draft_id, f"Agent started and discovered on port {port}")
        else:
            await self._send_progress(websocket, draft_id, "agent_started",
                                       f"Agent running on port {port} but not yet discovered. It may take a moment.",
                                       TESTING)
            await asyncio.to_thread(self._append_log, draft_id, f"Agent started on port {port} (discovery pending)")

        return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

    async def stop_draft_agent(self, draft_id: str) -> None:
        """Stop a running draft agent subprocess and unregister from orchestrator."""
        # Unregister from orchestrator so re-discovery works after refinement
        draft = await asyncio.to_thread(self.db.get_draft_agent, draft_id)
        if draft:
            # The durable quiesce intent must commit before routing is removed
            # or the physical process begins termination.
            await self._quiesce_dynamic_runtime(draft)
        if draft and self.orchestrator:
            slug = draft["agent_slug"]
            agent_id = f"{slug.replace('_', '-')}-1"
            port = draft.get("port")
            # Remove from orchestrator's registries
            self.orchestrator.agents.pop(agent_id, None)
            if port:
                agent_url = f"http://localhost:{port}"
                # Clean up agent_urls
                urls_to_remove = [k for k, v in self.orchestrator.agent_urls.items() if v == agent_url]
                for k in urls_to_remove:
                    del self.orchestrator.agent_urls[k]

        proc = self._draft_processes.get(draft_id)
        if proc:
            try:
                if proc.poll() is None:
                    await asyncio.to_thread(
                        lambda: proc.terminate(reason=TerminationReason.STOP)
                    )
                else:
                    await asyncio.to_thread(proc.wait, 0)
            finally:
                self._draft_processes.pop(draft_id, None)
            logger.info(f"Stopped draft agent process for {draft_id}")

    # Refine Agent

    async def refine_agent(self, draft_id: str, user_message: str,
                            websocket=None) -> Dict[str, Any]:
        """Refine an agent's tools based on user feedback."""
        draft = await asyncio.to_thread(self.db.get_draft_agent, draft_id)
        if not draft:
            raise ValueError(f"Draft {draft_id} not found")

        slug = draft["agent_slug"]
        is_byo = draft.get("origin") == BYO_ORIGIN
        tools_file = os.path.join(self._agents_dir, slug, "mcp_tools.py")

        if not os.path.exists(tools_file):
            raise FileNotFoundError("Agent tools file not found. Generate code first.")

        # Stop running agent (a BYO draft never has one — start_draft_agent
        # refuses byo_client origin — but the call is a harmless no-op).
        await self.stop_draft_agent(draft_id)

        await asyncio.to_thread(self.db.update_draft_agent, draft_id, status=GENERATING)
        await self._send_progress(websocket, draft_id, "refining",
                                   "Refining agent based on your feedback...", GENERATING)

        # Update refinement history
        history = json.loads(draft.get("refinement_history") or "[]")
        history.append({
            "role": "user",
            "content": user_message,
            "timestamp": int(time.time() * 1000),
        })

        try:
            # Read current code
            with open(tools_file, "r", encoding="utf-8") as f:
                current_code = f.read()

            # Refine via LLM
            await self._send_progress(websocket, draft_id, "generating_tools",
                                       "Generating updated tool implementations...", GENERATING)

            new_code = await self.generator.refine_tools_file(
                current_code=current_code,
                user_message=user_message,
                agent_name=draft["agent_name"],
                description=draft["description"],
                self_contained=is_byo,
            )

            # Syntax validation
            try:
                compile(new_code, f"{slug}/mcp_tools.py", "exec")
            except SyntaxError as e:
                error_msg = f"Refined code has syntax error (line {e.lineno}): {e.msg}"
                await asyncio.to_thread(
                    self.db.update_draft_agent,
                    draft_id, status=ERROR, error_message=error_msg,
                    refinement_history=json.dumps(history),
                )
                await self._send_progress(websocket, draft_id, "syntax_error",
                                           error_msg, ERROR)
                return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

            # Security analysis
            await self._send_progress(websocket, draft_id, "security_scan",
                                       "Running security analysis on updated code...", GENERATING)

            report = self.security.analyze(new_code, filename=f"{slug}/mcp_tools.py")

            # Pre-execution gate: HIGH blocks alongside CRITICAL (H4).
            if blocks_execution(report):
                await asyncio.to_thread(
                    self.db.update_draft_agent,
                    draft_id,
                    status=ERROR,
                    security_report=json.dumps(report.to_dict()),
                    error_message="Refinement produced code with blocking security issues.",
                    refinement_history=json.dumps(history),
                )
                await self._send_progress(websocket, draft_id, "security_failed",
                                           "Security analysis found blocking issues in updated code.",
                                           ERROR, detail=report.to_dict())
                return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

            # Write updated code
            with open(tools_file, "w", encoding="utf-8") as f:
                f.write(new_code)

            # Spec validation on refined code. The 027 validator EXECUTES the
            # tools, so a BYO draft's (user-authored) code gets the STATIC
            # validator instead — this entry point is reachable with any draft id
            # its owner holds, and user code never runs on this host (058 G1).
            if is_byo:
                validation_report = self.validator.validate_static(new_code, slug)
            else:
                validation_report = self.validator.validate(new_code, slug, self._agents_dir)
            await asyncio.to_thread(
                self._append_log,
                draft_id,
                f"Post-refinement validation: "
                f"{validation_report.tools_passed}/{validation_report.tools_tested} tools passed",
            )

            history.append({
                "role": "system",
                "content": (
                    "Code updated successfully."
                    if validation_report.passed
                    else f"Code updated but validation found issues: "
                         f"{validation_report.tools_passed}/{validation_report.tools_tested} tools passed."
                ),
                "timestamp": int(time.time() * 1000),
            })

            # Re-extract credentials from refined code
            required_creds = self._extract_required_credentials(new_code)

            await asyncio.to_thread(
                self.db.update_draft_agent,
                draft_id,
                status=GENERATED,
                security_report=json.dumps(report.to_dict()) if report.findings else None,
                validation_report=json.dumps(validation_report.to_dict()),
                refinement_history=json.dumps(history),
                required_credentials=json.dumps(required_creds) if required_creds else None,
            )

            status_msg = (
                "Agent updated and validated! You can test it again."
                if validation_report.passed
                else f"Agent updated but validation found issues "
                     f"({validation_report.tools_passed}/{validation_report.tools_tested} tools passed). "
                     "Review findings or refine further."
            )
            await self._send_progress(websocket, draft_id, "refinement_complete",
                                       status_msg, GENERATED,
                                       detail={
                                           "security": report.to_dict() if report.findings else None,
                                           "validation": validation_report.to_dict(),
                                       })
            await asyncio.to_thread(self._append_log, draft_id, f"Refinement complete: {user_message[:100]}")

            return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

        except Exception as e:
            logger.error(f"Refinement failed for draft {draft_id}: {e}")
            await asyncio.to_thread(self.db.update_draft_agent, draft_id, status=ERROR, error_message=str(e),
                                    refinement_history=json.dumps(history))
            await self._send_progress(websocket, draft_id, "error",
                                       f"Refinement failed: {e}", ERROR)
            return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

    # Auto-Fix Tool Errors

    def _find_draft_by_agent_id(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Look up a draft record by runtime agent_id (e.g. 'etf-agent-1'), any status."""
        # agent_id format is "{slug_with_hyphens}-1", reverse to get slug
        if not agent_id.endswith("-1"):
            return None
        slug = agent_id[:-2].replace('-', '_')  # "etf-agent" -> "etf_agent"
        return self.db.get_draft_agent_by_slug(slug)

    def _get_draft_by_agent_id(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Draft lookup gated to states where auto-fix is meaningful."""
        draft = self._find_draft_by_agent_id(agent_id)
        if draft and draft["status"] in (TESTING, GENERATED, LIVE):
            return draft
        return None

    async def auto_fix_tool_error(self, agent_id: str, tool_name: str,
                                   error_message: str, websocket=None) -> bool:
        """Automatically attempt to fix a tool error by refining the generated code.

        Returns True if a fix was attempted, False if this agent isn't a draft.
        """
        draft = self._get_draft_by_agent_id(agent_id)
        if not draft:
            return False

        # Auto-fix is only allowed for draft agents, not live ones
        if draft["status"] == LIVE:
            logger.info(f"Auto-fix skipped for live agent {agent_id} (tool '{tool_name}')")
            return False

        draft_id = draft["id"]
        slug = draft["agent_slug"]
        tools_file = os.path.join(self._agents_dir, slug, "mcp_tools.py")

        if not os.path.exists(tools_file):
            return False

        logger.info(f"Auto-fix triggered for draft {draft_id}: tool '{tool_name}' error: {error_message}")

        # Build a targeted refinement message from the error
        fix_message = (
            f"The tool '{tool_name}' is failing with this error:\n"
            f"  {error_message}\n\n"
            f"Please fix the implementation of '{tool_name}' so it handles this correctly. "
            f"Common issues include: missing parameters, wrong parameter types, "
            f"missing imports, incorrect API usage, or unhandled edge cases. "
            f"Fix ONLY the issue — do not change other tools."
        )

        await self._send_progress(websocket, draft_id, "auto_fix",
                                   f"Auto-fixing tool '{tool_name}': {error_message[:100]}...",
                                   GENERATING)
        self._append_log(draft_id, f"Auto-fix triggered for '{tool_name}': {error_message[:200]}")

        try:
            # Stop the running agent
            await self.stop_draft_agent(draft_id)

            # Read current code
            with open(tools_file, "r", encoding="utf-8") as f:
                current_code = f.read()

            # Refine via LLM
            new_code = await self.generator.refine_tools_file(
                current_code=current_code,
                user_message=fix_message,
                agent_name=draft["agent_name"],
                description=draft["description"],
            )

            # Syntax validation
            try:
                compile(new_code, f"{slug}/mcp_tools.py", "exec")
            except SyntaxError as e:
                logger.error(f"Auto-fix produced syntax error: {e}")
                await self._send_progress(websocket, draft_id, "auto_fix_failed",
                                           "Auto-fix produced invalid code (syntax error). Manual refinement needed.",
                                           TESTING)
                # Restart original agent
                await self.start_draft_agent(draft_id, websocket)
                return True

            # Security analysis — pre-execution gate: HIGH blocks alongside
            # CRITICAL (H4).
            report = self.security.analyze(new_code, filename=f"{slug}/mcp_tools.py")
            if blocks_execution(report):
                logger.error("Auto-fix produced code with blocking security issues")
                await self._send_progress(websocket, draft_id, "auto_fix_failed",
                                           "Auto-fix produced code with security issues. Manual refinement needed.",
                                           TESTING)
                await self.start_draft_agent(draft_id, websocket)
                return True

            # Write fixed code
            with open(tools_file, "w", encoding="utf-8") as f:
                f.write(new_code)

            # Update refinement history
            history = json.loads(draft.get("refinement_history") or "[]")
            history.append({
                "role": "system",
                "content": f"Auto-fix applied for tool '{tool_name}': {error_message[:200]}",
                "timestamp": int(time.time() * 1000),
            })
            self.db.update_draft_agent(draft_id, refinement_history=json.dumps(history))

            # Restart agent with fixed code
            await self.start_draft_agent(draft_id, websocket)

            await self._send_progress(websocket, draft_id, "auto_fix_complete",
                                       f"Auto-fix applied for tool '{tool_name}'. Agent restarted.",
                                       TESTING)
            self._append_log(draft_id, f"Auto-fix complete for '{tool_name}'")
            return True

        except Exception as e:
            logger.error(f"Auto-fix failed for draft {draft_id}: {e}")
            await self._send_progress(websocket, draft_id, "auto_fix_failed",
                                       f"Auto-fix failed: {e}", TESTING)
            # Try to restart the original agent
            try:
                await self.start_draft_agent(draft_id, websocket)
            except Exception:
                pass
            return True

    # Approve Agent

    async def approve_agent(self, draft_id: str, websocket=None) -> Dict[str, Any]:
        """Run comprehensive analysis and approve/reject the agent."""
        draft = await asyncio.to_thread(self.db.get_draft_agent, draft_id)
        if not draft:
            raise ValueError(f"Draft {draft_id} not found")

        # 058 — a BYO agent does not go live through the server-side approval
        # flow (it goes live when the owner's host registers it inward), and this
        # path both exec's the tools in-process and Popens them. Refuse: the draft
        # id is the user's own, so this entry point is otherwise reachable.
        if draft.get("origin") == BYO_ORIGIN:
            raise ValueError(
                f"Draft {draft_id} is a BYO agent — it goes live by registering "
                "from the owner's desktop host, not by server-side approval (058)."
            )

        slug = draft["agent_slug"]
        tools_file = os.path.join(self._agents_dir, slug, "mcp_tools.py")

        if not os.path.exists(tools_file):
            raise FileNotFoundError("Agent files not found. Generate code first.")

        await asyncio.to_thread(self.db.update_draft_agent, draft_id, status=ANALYZING)
        await self._send_progress(websocket, draft_id, "analyzing",
                                   "Running comprehensive security analysis...", ANALYZING)
        await asyncio.to_thread(self._append_log, draft_id, "Starting approval analysis...")

        try:
            # Step 1: Full code security analysis
            await self._send_progress(websocket, draft_id, "code_analysis",
                                       "Analyzing generated code...", ANALYZING)

            with open(tools_file, "r", encoding="utf-8") as f:
                tools_code = f.read()

            report = self.security.analyze(tools_code, filename=f"{slug}/mcp_tools.py")

            # Step 2: Verify code is syntactically valid and imports work
            await self._send_progress(websocket, draft_id, "syntax_check",
                                       "Verifying code syntax...", ANALYZING)

            try:
                compile(tools_code, f"{slug}/mcp_tools.py", "exec")
            except SyntaxError as e:
                await asyncio.to_thread(
                    self.db.update_draft_agent,
                    draft_id, status=REJECTED,
                    security_report=json.dumps(report.to_dict()),
                    error_message=f"Syntax error in generated code: {e}",
                )
                await self._send_progress(websocket, draft_id, "rejected",
                                           f"Code has syntax errors: {e}", REJECTED)
                return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

            # Step 3: Decision based on security findings — BEFORE spec
            # validation, because the 027 validator EXECUTES the tools
            # in-process. Flagged code must be refused or parked for admin
            # review without ever running (H4: the verdict used to be
            # computed here but enforced only after validate()).
            if report.max_severity == Severity.CRITICAL:
                # Critical findings are compromise events. Revoke any
                # previously admitted branch before reporting rejection.
                await self.stop_draft_agent(draft_id)
                await self._revoke_dynamic_runtime(
                    draft,
                    reason_code="security_compromise",
                )
                await asyncio.to_thread(
                    self.db.update_draft_agent,
                    draft_id, status=REJECTED,
                    security_report=json.dumps(report.to_dict()),
                    error_message="Critical security issues detected. Agent rejected.",
                )
                await self._send_progress(websocket, draft_id, "rejected",
                                           "Agent rejected: critical security issues found.",
                                           REJECTED, detail=report.to_dict())
                await asyncio.to_thread(self._append_log, draft_id, "REJECTED: Critical security issues")
                return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

            elif report.max_severity == Severity.HIGH:
                await asyncio.to_thread(
                    self.db.update_draft_agent,
                    draft_id, status=PENDING_REVIEW,
                    security_report=json.dumps(report.to_dict()),
                )
                await self._send_progress(websocket, draft_id, "pending_review",
                                           "Agent requires admin review before going live.",
                                           PENDING_REVIEW, detail=report.to_dict())
                await asyncio.to_thread(self._append_log, draft_id, "Sent to admin review queue (high-severity findings)")
                return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

            # Step 4: Spec validation (executes the tools; only reached by
            # code the security gate cleared).
            await self._send_progress(websocket, draft_id, "spec_validation",
                                       "Validating tools against spec...", ANALYZING)

            validation_report = self.validator.validate(tools_code, slug, self._agents_dir)
            await asyncio.to_thread(
                self.db.update_draft_agent,
                draft_id,
                validation_report=json.dumps(validation_report.to_dict()),
            )

            if not validation_report.passed:
                await asyncio.to_thread(
                    self.db.update_draft_agent,
                    draft_id, status=PENDING_REVIEW,
                    security_report=json.dumps(report.to_dict()),
                    error_message=(
                        f"Spec validation failed: {validation_report.tools_passed}/"
                        f"{validation_report.tools_tested} tools passed. "
                        "Requires review before going live."
                    ),
                )
                await self._send_progress(websocket, draft_id, "pending_review",
                                           "Agent has validation issues — requires review.",
                                           PENDING_REVIEW, detail={
                                               "security": report.to_dict(),
                                               "validation": validation_report.to_dict(),
                                           })
                await asyncio.to_thread(self._append_log, draft_id, "Sent to review: spec validation failed")
                return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

            else:
                # Clean or medium/low only → auto-approve
                self._remove_draft_marker(slug)
                agent_id = f"{slug.replace('_', '-')}-1"

                # Ensure agent process is running. Note: start_draft_agent
                # writes status=TESTING + sets ownership + populates
                # orchestrator.agents via discover_agent. We call it FIRST so
                # those side-effects happen, then restore status=LIVE below
                # (otherwise the TESTING write inside start_draft_agent would
                # clobber the LIVE flip on the auto-approval path).
                start_failed = False
                if draft_id not in self._draft_processes or \
                   self._draft_processes[draft_id].poll() is not None:
                    try:
                        started_state = await self.start_draft_agent(draft_id, websocket)
                        # start_draft_agent doesn't raise on subprocess crash —
                        # it writes status=ERROR and returns. Detect that here.
                        if started_state and started_state.get("status") == ERROR:
                            start_failed = True
                            logger.warning(
                                f"Approved draft {draft_id}: subprocess failed "
                                f"to start after promotion: "
                                f"{started_state.get('error_message')}"
                            )
                    except Exception as e:
                        start_failed = True
                        logger.warning(
                            f"Approved draft {draft_id}: subprocess start raised "
                            f"({e}); leaving draft in error state."
                        )

                # If we couldn't bring the agent process up, do NOT promote to
                # LIVE — that would produce a phantom-live entry the user can't
                # actually use. Leave the draft in its current (error) state and
                # surface the failure.
                if start_failed:
                    await self._send_progress(
                        websocket, draft_id, "error",
                        "Approval succeeded but the agent process failed to "
                        "start. Try again or refine the agent.",
                        ERROR,
                    )
                    await asyncio.to_thread(self._append_log, draft_id, "APPROVE: subprocess failed to start; left in error state")
                    return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

                # Re-assert ownership in case start_draft_agent was skipped
                # (process already running) — ownership must always exist
                # for a live agent so it shows up in send_dashboard.
                user = await asyncio.to_thread(self.db.get_user, draft["user_id"])
                owner_email = user.get("email", draft["user_id"]) if user else draft["user_id"]
                await asyncio.to_thread(self.db.set_agent_ownership, agent_id, owner_email=owner_email, is_public=False)

                # Restore LIVE status (guaranteed final write in this branch)
                await asyncio.to_thread(
                    self.db.update_draft_agent,
                    draft_id, status=LIVE,
                    security_report=json.dumps(report.to_dict()) if report.findings else None,
                )

                # Live agents: all scopes DISABLED — user must explicitly enable
                if self.orchestrator:
                    await asyncio.to_thread(
                        self.orchestrator.tool_permissions.set_agent_scopes,
                        draft["user_id"], agent_id,
                        {"tools:read": False, "tools:write": False, "tools:search": False, "tools:system": False}
                    )

                await self._send_progress(websocket, draft_id, "approved",
                                           "Agent approved and is now live!", LIVE,
                                           detail=report.to_dict() if report.findings else None)
                await asyncio.to_thread(self._append_log, draft_id, "APPROVED: Agent is now live")
                logger.info(
                    f"approve_agent: auto-promoted draft {draft_id} -> "
                    f"agent_id={agent_id} owner={owner_email}"
                )

                # Broadcast updated dashboard + agent_list to all UI clients
                # of the owning user so the live agents UI updates without a
                # manual page reload. Mirrors the per-user broadcast pattern
                # used elsewhere in orchestrator.py for permission updates.
                if self.orchestrator:
                    target_user_id = draft["user_id"]
                    for client in list(getattr(self.orchestrator, 'ui_clients', [])):
                        try:
                            client_user_id = self.orchestrator._get_user_id(client)
                        except Exception:
                            client_user_id = None
                        if client_user_id == target_user_id:
                            try:
                                asyncio.create_task(self.orchestrator.send_dashboard(client))
                                send_agent_list = getattr(self.orchestrator, 'send_agent_list', None)
                                if send_agent_list:
                                    asyncio.create_task(send_agent_list(client))
                            except Exception as broadcast_err:
                                logger.debug(
                                    f"approve_agent broadcast skipped for one "
                                    f"client: {broadcast_err}"
                                )

                return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

        except Exception as e:
            logger.error(f"Approval analysis failed for draft {draft_id}: {e}")
            await asyncio.to_thread(self.db.update_draft_agent, draft_id, status=ERROR, error_message=str(e))
            await self._send_progress(websocket, draft_id, "error",
                                       f"Approval analysis failed: {e}", ERROR)
            return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

    # Admin Review

    async def admin_review(self, draft_id: str, decision: str, admin_user_id: str,
                            notes: str = None, websocket=None) -> Dict[str, Any]:
        """Admin approves or rejects a draft agent pending review."""
        draft = self.db.get_draft_agent(draft_id)
        if not draft:
            raise ValueError(f"Draft {draft_id} not found")
        if draft["status"] != PENDING_REVIEW:
            raise ValueError(f"Draft is not pending review (status: {draft['status']})")

        if decision == "approve":
            self.db.update_draft_agent(
                draft_id, status=LIVE,
                reviewed_by=admin_user_id,
                review_notes=notes or "Approved by admin",
            )
            self._remove_draft_marker(draft["agent_slug"])
            # Live agents: all scopes DISABLED — user must explicitly enable
            agent_id = f"{draft['agent_slug'].replace('_', '-')}-1"
            if self.orchestrator:
                self.orchestrator.tool_permissions.set_agent_scopes(
                    draft["user_id"], agent_id,
                    {"tools:read": False, "tools:write": False, "tools:search": False, "tools:system": False}
                )
            self._append_log(draft_id, f"Admin approved by {admin_user_id}")

            # Start agent if not running
            if draft_id not in self._draft_processes or \
               self._draft_processes[draft_id].poll() is not None:
                await self.start_draft_agent(draft_id, websocket)

            return self.db.get_draft_agent(draft_id)

        elif decision == "reject":
            self.db.update_draft_agent(
                draft_id, status=REJECTED,
                reviewed_by=admin_user_id,
                review_notes=notes or "Rejected by admin",
            )
            await self.stop_draft_agent(draft_id)
            await self._close_dynamic_runtime(draft)
            self._append_log(draft_id, f"Admin rejected by {admin_user_id}: {notes or 'No reason given'}")
            return self.db.get_draft_agent(draft_id)

        else:
            raise ValueError(f"Invalid decision: {decision}. Must be 'approve' or 'reject'.")

    # Delete Draft

    async def delete_draft(self, draft_id: str) -> bool:
        """Delete a draft agent — stops process, removes files, deletes DB record."""
        draft = self.db.get_draft_agent(draft_id)
        if not draft:
            return False

        # Stop process and wait for it to fully terminate
        await self.stop_draft_agent(draft_id)
        # Enforced deletion aborts before local state is removed unless the
        # current external authority branch is durably revoked.
        await self._revoke_dynamic_runtime(draft, reason_code="agent_deleted")
        # Give the OS time to release file handles (Windows is slow to release)
        await asyncio.sleep(0.5)

        # Remove files — retry on Windows where handles may linger
        slug = draft["agent_slug"]
        agent_dir = os.path.join(self._agents_dir, slug)
        if os.path.exists(agent_dir):
            for attempt in range(3):
                try:
                    shutil.rmtree(agent_dir)
                    logger.info(f"Removed agent directory: {agent_dir}")
                    break
                except (PermissionError, OSError) as e:
                    if attempt < 2:
                        logger.debug(f"rmtree attempt {attempt + 1} failed for {agent_dir}: {e}, retrying...")
                        await asyncio.sleep(1)
                    else:
                        logger.warning(f"Could not fully remove {agent_dir}: {e}")
                        # Force-remove individual files then try the directory
                        for root, dirs, files in os.walk(agent_dir, topdown=False):
                            for name in files:
                                try:
                                    os.remove(os.path.join(root, name))
                                except OSError:
                                    pass
                            for name in dirs:
                                try:
                                    os.rmdir(os.path.join(root, name))
                                except OSError:
                                    pass
                        try:
                            os.rmdir(agent_dir)
                        except OSError:
                            logger.warning(f"Directory still locked: {agent_dir}")

        # Delete DB record
        self.db.delete_draft_agent(draft_id)

        # Purge the permission/ownership rows the test flow created for the
        # draft's runtime agent id. Without this they leak after discard: a
        # discarded draft's all-scopes-enabled rows persist, so its broken
        # generated tools keep dispatching in normal chats and shadow
        # first-party tools.
        runtime_agent_id = slug.replace("_", "-") + "-1"
        self._purge_agent_permission_rows(
            runtime_agent_id,
            owner_user_id=str(draft["user_id"]),
        )

        logger.info(f"Deleted draft agent {draft_id} ({draft['agent_name']})")
        return True

    def _purge_agent_permission_rows(
        self,
        agent_id: str,
        *,
        owner_user_id: str,
    ) -> None:
        """Remove owner-scoped policy and legacy ownership through Plane."""

        try:
            self.draft_store.purge_agent_state(
                owner_user_id=owner_user_id,
                agent_id=agent_id,
            )
        except Exception:
            logger.debug(
                "draft permission purge failed (%s/%s)",
                owner_user_id,
                agent_id,
                exc_info=True,
            )

    def reconcile_orphaned_draft_permissions(self, agent_ids=None) -> int:
        """Boot-time sweep: purge permission rows leaked by drafts discarded
        before the delete-time purge ran.

        An agent id is an orphaned draft when (a) no ``draft_agents`` row
        maps to it (approved-live agents keep their row with status
        ``live``) AND (b) its slug directory is either gone or still carries
        a ``.draft`` marker (a real bundled agent's directory exists without
        one). Live first-party agents are protected by (b); nothing else is
        touched. Returns the number of agent ids purged.

        Args:
            agent_ids: optional restriction of the candidate set (tests use
                this to stay scoped); None sweeps every scoped agent id.
        """
        purged = 0
        try:
            rows = [
                {"user_id": owner_id, "agent_id": agent_id}
                for owner_id, agent_id in (
                    self.draft_store.list_scoped_agent_owners_for_administration()
                )
            ]
            if agent_ids is not None:
                rows = [r for r in rows if r["agent_id"] in set(agent_ids)]
            known_slugs = {
                d["agent_slug"] for d in self.draft_store.list_draft_agents()
            }
            for row in rows:
                agent_id = row["agent_id"]
                slug = agent_id[:-2].replace("-", "_")
                has_draft_row = slug in known_slugs
                if has_draft_row:
                    continue
                agent_dir = os.path.join(self._agents_dir, slug)
                dir_exists = os.path.isdir(agent_dir)
                if dir_exists and not os.path.exists(os.path.join(agent_dir, ".draft")):
                    continue  # real (bundled/approved) agent directory
                self._purge_agent_permission_rows(
                    agent_id,
                    owner_user_id=str(row["user_id"]),
                )
                purged += 1
                logger.info("Purged leaked draft permissions: %s "
                            "(dir_exists=%s)", agent_id, dir_exists)
        except Exception:
            logger.warning("orphaned-draft permission sweep failed", exc_info=True)
        return purged
