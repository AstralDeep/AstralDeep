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

from orchestrator.agent_generator import (
    BYO_BUNDLE_FILENAMES,
    BYO_RUNTIME_CONTRACT_VERSION,
    BYO_RUNTIME_LOCK_SHA256,
    AgentCodeGenerator,
)
from orchestrator.artifact_publication import ImmutableAgentArtifactStore
from orchestrator.user_agents import (
    AgentDeletedError,
    PersonalAgentRuntimeRepository,
    StaleRuntimeGenerationError,
)
from orchestrator.work_admission import ExecutionFence, OperationState
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

    def fail_candidate(self, candidate: CandidateRevision, failure_code: str) -> None: ...

    def recovery_plan(self, owner_user_id: str, agent_id: str) -> RecoveryPlan: ...


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

    runtime_repository: PersonalAgentRuntimeRepository

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_repository, PersonalAgentRuntimeRepository):
            raise TypeError(
                "runtime_repository must be PersonalAgentRuntimeRepository"
            )

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
        self._required_text(request.owner_user_id, "owner_user_id", 255)
        self._required_text(request.agent_id, "agent_id", 255)
        self._uuid_text(request.revision_id, "revision_id")
        self._uuid_text(request.host_session_id, "host_session_id")
        if not isinstance(request.operation_fence, ExecutionFence):
            raise TypeError("operation_fence must be ExecutionFence")
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

    def _mark_revision_failed_only(
        self, request: CandidatePreparation, failure_code: str
    ) -> None:
        with self._runtime._transaction() as cursor:
            self._runtime._lock_owner(cursor, request.owner_user_id)
            cursor.execute(
                """
                UPDATE user_agent_revision
                SET state = 'failed', failed_at = clock_timestamp(),
                    failure_code = %s, state_revision = state_revision + 1
                WHERE revision_id = %s AND agent_id = %s AND owner_user_id = %s
                  AND state IN ('prepared', 'starting', 'ready')
                """,
                (
                    failure_code,
                    request.revision_id,
                    request.agent_id,
                    request.owner_user_id,
                ),
            )

    def prepare_candidate(self, request: CandidatePreparation) -> CandidateRevision:
        """Insert immutable revision metadata, then reserve its runtime fence."""

        _manifest, canonical_manifest = self._validate_preparation(request)
        promotion_token: Optional[str] = None
        previous_revision_id: Optional[str] = None
        previous_runtime_id: Optional[str] = None
        with self._runtime._transaction() as cursor:
            self._runtime._lock_owner(cursor, request.owner_user_id)
            try:
                agent = self._runtime._locked_agent(
                    cursor,
                    owner_user_id=request.owner_user_id,
                    agent_id=request.agent_id,
                )
            except AgentDeletedError as exc:
                raise RevisionActivationError("agent_deleted") from exc
            if (
                agent["selected_host_session_id"] is None
                or str(agent["selected_host_session_id"])
                != request.host_session_id
            ):
                raise StaleRuntimeGenerationError(
                    "selected host session is stale"
                )
            cursor.execute(
                """
                SELECT state, inventory_state, owner_user_id
                FROM agent_host_session WHERE host_session_id = %s FOR UPDATE
                """,
                (request.host_session_id,),
            )
            host = cursor.fetchone()
            if not (
                host is not None
                and str(host["owner_user_id"]) == request.owner_user_id
                and host["state"] == "connected"
            ):
                raise StaleRuntimeGenerationError(
                    "selected host session is stale"
                )
            if host["inventory_state"] != "reconciled":
                raise RevisionActivationError("inventory_required")
            previous_revision_id = (
                None
                if agent["active_revision_id"] is None
                else str(agent["active_revision_id"])
            )
            previous_runtime_id = (
                None
                if agent["authoritative_instance_id"] is None
                else str(agent["authoritative_instance_id"])
            )
            cursor.execute(
                "SELECT * FROM user_agent_revision WHERE revision_id = %s FOR UPDATE",
                (request.revision_id,),
            )
            revision = cursor.fetchone()
            if revision is None:
                cursor.execute(
                    "SELECT COALESCE(max(revision_number), -1) + 1 AS number "
                    "FROM user_agent_revision WHERE agent_id = %s",
                    (request.agent_id,),
                )
                revision_number = int(cursor.fetchone()["number"])
                promotion_token = self._runtime._new_uuid("promotion_token")
                cursor.execute(
                    """
                    INSERT INTO user_agent_revision (
                        revision_id, agent_id, owner_user_id, revision_number,
                        parent_revision_id, previous_good_revision_id,
                        artifact_digest, manifest_json, artifact_relative_path,
                        runtime_contract_version, release_lock_digest,
                        compatibility_state, state, promotion_token
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s,
                        'compatible', 'prepared', %s
                    )
                    """,
                    (
                        request.revision_id,
                        request.agent_id,
                        request.owner_user_id,
                        revision_number,
                        previous_revision_id,
                        previous_revision_id,
                        request.bundle_sha256,
                        canonical_manifest,
                        request.artifact_relative_path,
                        request.runtime_contract_version,
                        request.required_runtime_lock_sha256,
                        promotion_token,
                    ),
                )
            else:
                manifest = revision["manifest_json"]
                if isinstance(manifest, str):
                    manifest = json.loads(manifest)
                persisted_manifest = json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                if not (
                    str(revision["agent_id"]) == request.agent_id
                    and str(revision["owner_user_id"]) == request.owner_user_id
                    and revision["artifact_digest"] == request.bundle_sha256
                    and persisted_manifest == canonical_manifest
                    and revision["artifact_relative_path"]
                    == request.artifact_relative_path
                    and int(revision["runtime_contract_version"])
                    == request.runtime_contract_version
                    and revision["release_lock_digest"]
                    == request.required_runtime_lock_sha256
                    and revision["state"] in {"prepared", "starting", "ready"}
                ):
                    raise StaleRuntimeGenerationError(
                        "revision identity is already bound to different bytes"
                    )
                promotion_token = str(revision["promotion_token"])
                previous_revision_id = (
                    None
                    if revision["previous_good_revision_id"] is None
                    else str(revision["previous_good_revision_id"])
                )

            cursor.execute(
                """
                SELECT runtime_instance_id FROM agent_runtime_instance
                WHERE revision_id = %s AND agent_id = %s AND owner_user_id = %s
                  AND state NOT IN ('stopped', 'failed', 'offline', 'superseded')
                ORDER BY created_at DESC, runtime_instance_id DESC
                LIMIT 1
                """,
                (
                    request.revision_id,
                    request.agent_id,
                    request.owner_user_id,
                ),
            )
            existing_runtime = cursor.fetchone()

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
                self._mark_revision_failed_only(request, "bundle_install_failed")
                raise
            runtime_instance_id = runtime.fence.runtime_instance_id
        else:
            runtime_instance_id = str(existing_runtime["runtime_instance_id"])
        return CandidateRevision(
            owner_user_id=request.owner_user_id,
            agent_id=request.agent_id,
            revision_id=request.revision_id,
            promotion_token=str(promotion_token),
            runtime_instance_id=runtime_instance_id,
            previous_active_revision_id=previous_revision_id,
            previous_runtime_instance_id=previous_runtime_id,
        )

    def mark_candidate_starting(self, candidate: CandidateRevision) -> None:
        with self._runtime._transaction() as cursor:
            self._runtime._lock_owner(cursor, candidate.owner_user_id)
            cursor.execute(
                """
                UPDATE user_agent_revision
                SET state = 'starting', state_revision = state_revision + 1
                WHERE revision_id = %s AND agent_id = %s AND owner_user_id = %s
                  AND promotion_token = %s AND state = 'prepared'
                """,
                (
                    candidate.revision_id,
                    candidate.agent_id,
                    candidate.owner_user_id,
                    candidate.promotion_token,
                ),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT state FROM user_agent_revision WHERE revision_id = %s",
                    (candidate.revision_id,),
                )
                row = cursor.fetchone()
                if row is None or row["state"] not in {"starting", "ready", "active"}:
                    raise StaleRuntimeGenerationError(
                        "candidate starting transition is stale"
                    )

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
        with self._runtime._transaction() as cursor:
            self._runtime._lock_owner(cursor, candidate.owner_user_id)
            cursor.execute(
                """
                UPDATE user_agent_revision
                SET state = 'ready', confirmed_at = clock_timestamp(),
                    state_revision = state_revision + 1
                WHERE revision_id = %s AND agent_id = %s AND owner_user_id = %s
                  AND promotion_token = %s AND state IN ('prepared', 'starting')
                """,
                (
                    candidate.revision_id,
                    candidate.agent_id,
                    candidate.owner_user_id,
                    candidate.promotion_token,
                ),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT state FROM user_agent_revision WHERE revision_id = %s",
                    (candidate.revision_id,),
                )
                row = cursor.fetchone()
                if row is None or row["state"] not in {"ready", "active"}:
                    raise StaleRuntimeGenerationError(
                        "candidate ready transition is stale"
                    )
        return candidate

    def promote_candidate(self, candidate: CandidateRevision) -> PromotionCommit:
        """Atomically move every authoritative pointer to one ready candidate."""

        with self._runtime._transaction() as cursor:
            self._runtime._lock_owner(cursor, candidate.owner_user_id)
            agent = self._runtime._locked_agent(
                cursor,
                owner_user_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
            )
            cursor.execute(
                "SELECT * FROM user_agent_revision WHERE revision_id = %s FOR UPDATE",
                (candidate.revision_id,),
            )
            revision = cursor.fetchone()
            cursor.execute(
                "SELECT * FROM agent_runtime_instance "
                "WHERE runtime_instance_id = %s FOR UPDATE",
                (candidate.runtime_instance_id,),
            )
            runtime = cursor.fetchone()
            if revision is None or runtime is None:
                raise StaleRuntimeGenerationError("candidate promotion fence is stale")

            active_revision = (
                None
                if agent["active_revision_id"] is None
                else str(agent["active_revision_id"])
            )
            authoritative_runtime = (
                None
                if agent["authoritative_instance_id"] is None
                else str(agent["authoritative_instance_id"])
            )
            if (
                active_revision == candidate.revision_id
                and authoritative_runtime == candidate.runtime_instance_id
                and revision["state"] == "active"
                and runtime["state"] == "online"
                and bool(runtime["is_authoritative"])
            ):
                try:
                    replay_operation = (
                        self._runtime._assert_runtime_operation_locked(
                            cursor, runtime
                        )
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
                        transaction=cursor,
                    )
                return PromotionCommit(
                    owner_user_id=candidate.owner_user_id,
                    agent_id=candidate.agent_id,
                    revision_id=candidate.revision_id,
                    runtime_instance_id=candidate.runtime_instance_id,
                    previous_revision_id=(
                        None
                        if revision["previous_good_revision_id"] is None
                        else str(revision["previous_good_revision_id"])
                    ),
                    previous_runtime_instance_id=None,
                )
            if not (
                active_revision == candidate.previous_active_revision_id
                and authoritative_runtime
                == candidate.previous_runtime_instance_id
                and str(revision["agent_id"]) == candidate.agent_id
                and str(revision["owner_user_id"]) == candidate.owner_user_id
                and str(revision["promotion_token"]) == candidate.promotion_token
                and revision["state"] == "ready"
                and str(runtime["agent_id"]) == candidate.agent_id
                and str(runtime["owner_user_id"]) == candidate.owner_user_id
                and str(runtime["revision_id"]) == candidate.revision_id
                and runtime["state"] == "ready"
                and not bool(runtime["is_authoritative"])
                and str(agent["selected_host_session_id"])
                == str(runtime["host_session_id"])
            ):
                raise StaleRuntimeGenerationError("candidate promotion fence is stale")
            operation_fence = self._runtime._assert_runtime_operation_locked(
                cursor, runtime
            )

            if authoritative_runtime is not None:
                cursor.execute(
                    """
                    UPDATE agent_runtime_instance
                    SET state = CASE
                            WHEN state IN ('stopped', 'failed', 'offline', 'superseded')
                                THEN state
                            ELSE 'stopping'
                        END,
                        is_authoritative = FALSE,
                        state_revision = state_revision + 1
                    WHERE runtime_instance_id = %s AND is_authoritative = TRUE
                    """,
                    (authoritative_runtime,),
                )
            cursor.execute(
                """
                UPDATE agent_runtime_instance
                SET state = 'online', is_authoritative = TRUE,
                    state_revision = state_revision + 1
                WHERE runtime_instance_id = %s AND revision_id = %s
                  AND state = 'ready' AND is_authoritative = FALSE
                """,
                (candidate.runtime_instance_id, candidate.revision_id),
            )
            if cursor.rowcount != 1:
                raise StaleRuntimeGenerationError("candidate runtime promotion is stale")
            cursor.execute(
                """
                UPDATE user_agent_revision
                SET state = 'active', promoted_at = clock_timestamp(),
                    state_revision = state_revision + 1
                WHERE revision_id = %s AND promotion_token = %s AND state = 'ready'
                """,
                (candidate.revision_id, candidate.promotion_token),
            )
            if cursor.rowcount != 1:
                raise StaleRuntimeGenerationError("candidate revision promotion is stale")
            if active_revision is not None:
                cursor.execute(
                    """
                    UPDATE user_agent_revision
                    SET state = 'retired', state_revision = state_revision + 1
                    WHERE revision_id = %s AND state = 'active'
                    """,
                    (active_revision,),
                )
            cursor.execute(
                """
                UPDATE user_agent
                SET active_revision_id = %s,
                    last_known_good_revision_id = %s,
                    authoritative_instance_id = %s,
                    lifecycle_generation = %s,
                    status = 'live', state_revision = state_revision + 1,
                    updated_at = (extract(epoch from clock_timestamp()) * 1000)::bigint
                WHERE agent_id = %s AND owner_user_id = %s
                  AND state_revision = %s
                """,
                (
                    candidate.revision_id,
                    active_revision,
                    candidate.runtime_instance_id,
                    int(runtime["lifecycle_generation"]),
                    candidate.agent_id,
                    candidate.owner_user_id,
                    int(agent["state_revision"]),
                ),
            )
            if cursor.rowcount != 1:
                raise StaleRuntimeGenerationError("agent pointer promotion is stale")
            self._runtime._operations.terminalize(
                operation_fence,
                state=OperationState.COMPLETED,
                terminal_code=None,
                safe_summary=None,
                retry_after_ms=None,
                now=None,
                retention=self._runtime._operation_retention,
                transaction=cursor,
            )

        return PromotionCommit(
            owner_user_id=candidate.owner_user_id,
            agent_id=candidate.agent_id,
            revision_id=candidate.revision_id,
            runtime_instance_id=candidate.runtime_instance_id,
            previous_revision_id=active_revision,
            previous_runtime_instance_id=authoritative_runtime,
        )

    def fail_candidate(
        self, candidate: CandidateRevision, failure_code: str
    ) -> None:
        if self._SAFE_FAILURE.fullmatch(failure_code or "") is None:
            raise ValueError("candidate failure code is invalid")
        with self._runtime._transaction() as cursor:
            self._runtime._lock_owner(cursor, candidate.owner_user_id)
            cursor.execute(
                """
                SELECT active_revision_id FROM user_agent
                WHERE agent_id = %s AND owner_user_id = %s FOR UPDATE
                """,
                (candidate.agent_id, candidate.owner_user_id),
            )
            agent = cursor.fetchone()
            if agent is None:
                raise StaleRuntimeGenerationError("candidate agent is stale")
            if (
                agent["active_revision_id"] is not None
                and str(agent["active_revision_id"]) == candidate.revision_id
            ):
                return
            cursor.execute(
                "SELECT * FROM agent_runtime_instance "
                "WHERE runtime_instance_id = %s FOR UPDATE",
                (candidate.runtime_instance_id,),
            )
            runtime = cursor.fetchone()
            if runtime is not None and runtime["state"] not in {
                "stopped",
                "failed",
                "offline",
                "superseded",
            }:
                self._runtime._terminalize_instance_row_locked(
                    cursor, runtime, failure_code=failure_code
                )
            cursor.execute(
                """
                UPDATE user_agent_revision
                SET state = 'failed', failed_at = clock_timestamp(),
                    failure_code = %s, state_revision = state_revision + 1
                WHERE revision_id = %s AND agent_id = %s AND owner_user_id = %s
                  AND promotion_token = %s
                  AND state IN ('prepared', 'starting', 'ready')
                """,
                (
                    failure_code,
                    candidate.revision_id,
                    candidate.agent_id,
                    candidate.owner_user_id,
                    candidate.promotion_token,
                ),
            )

    def recovery_plan(self, owner_user_id: str, agent_id: str) -> RecoveryPlan:
        owner_user_id = self._required_text(owner_user_id, "owner_user_id", 255)
        agent_id = self._required_text(agent_id, "agent_id", 255)
        with self._runtime._transaction() as cursor:
            self._runtime._lock_owner(cursor, owner_user_id)
            agent = self._runtime._locked_agent(
                cursor, owner_user_id=owner_user_id, agent_id=agent_id
            )
            active_revision = (
                None
                if agent["active_revision_id"] is None
                else str(agent["active_revision_id"])
            )
            authoritative_runtime = (
                None
                if agent["authoritative_instance_id"] is None
                else str(agent["authoritative_instance_id"])
            )
            cursor.execute(
                """
                SELECT * FROM agent_runtime_instance
                WHERE agent_id = %s AND owner_user_id = %s
                  AND state NOT IN ('stopped', 'failed', 'offline', 'superseded')
                ORDER BY created_at, runtime_instance_id
                FOR UPDATE
                """,
                (agent_id, owner_user_id),
            )
            runtimes = list(cursor.fetchall())
            authoritative = next(
                (
                    row
                    for row in runtimes
                    if authoritative_runtime is not None
                    and str(row["runtime_instance_id"]) == authoritative_runtime
                    and active_revision is not None
                    and str(row["revision_id"]) == active_revision
                    and row["state"] == "online"
                    and bool(row["is_authoritative"])
                ),
                None,
            )
            keep_runtime_id = (
                None
                if authoritative is None
                else str(authoritative["runtime_instance_id"])
            )
            stop_runtime_ids = tuple(
                str(row["runtime_instance_id"])
                for row in runtimes
                if str(row["runtime_instance_id"]) != keep_runtime_id
            )
            for row in runtimes:
                if str(row["runtime_instance_id"]) == keep_runtime_id:
                    continue
                self._runtime._terminalize_instance_row_locked(
                    cursor,
                    row,
                    failure_code="revision_promotion_failed",
                )
            if active_revision is not None:
                cursor.execute(
                    """
                    UPDATE user_agent_revision
                    SET state = 'failed', failed_at = clock_timestamp(),
                        failure_code = 'revision_promotion_failed',
                        state_revision = state_revision + 1
                    WHERE agent_id = %s AND owner_user_id = %s
                      AND revision_id <> %s
                      AND state IN ('prepared', 'starting', 'ready')
                    """,
                    (agent_id, owner_user_id, active_revision),
                )
            else:
                cursor.execute(
                    """
                    UPDATE user_agent_revision
                    SET state = 'failed', failed_at = clock_timestamp(),
                        failure_code = 'revision_promotion_failed',
                        state_revision = state_revision + 1
                    WHERE agent_id = %s AND owner_user_id = %s
                      AND state IN ('prepared', 'starting', 'ready')
                    """,
                    (agent_id, owner_user_id),
                )
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
            stop_runtime_instance_ids=stop_runtime_ids,
        )


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

    async def _fail_precommit_candidate(
        self, candidate: CandidateRevision, failure_code: str
    ) -> None:
        try:
            self._store.fail_candidate(candidate, failure_code)
        except Exception:
            logger.exception(
                "candidate failure transition failed",
                extra={"failure_code": failure_code},
            )
        try:
            await _await_if_needed(self._stop_runtime(candidate.runtime_instance_id))
        except Exception:
            logger.exception(
                "candidate stop failed",
                extra={"failure_code": failure_code},
            )

    async def activate(
        self, request: CandidatePreparation
    ) -> RevisionActivationResult:
        """Prepare and activate one revision through a single commit boundary."""

        candidate: Optional[CandidateRevision] = None
        commit: Optional[PromotionCommit] = None
        phase = "preparation"
        committed = False
        try:
            candidate = self._store.prepare_candidate(request)
            self._fault("after_prepare", candidate)

            phase = "start"
            self._fault("before_start", candidate)
            started_runtime_id = await _await_if_needed(
                self._start_candidate(candidate)
            )
            if started_runtime_id != candidate.runtime_instance_id:
                raise RevisionActivationError("stale_runtime_generation")
            self._store.mark_candidate_starting(candidate)
            self._fault("after_start", candidate)

            phase = "ready"
            self._fault("before_ready", candidate)
            ready_runtime_id = await _await_if_needed(
                self._await_candidate_ready(candidate)
            )
            candidate = self._store.confirm_candidate_ready(
                candidate, ready_runtime_id
            )
            self._fault("after_ready", candidate)

            phase = "promotion"
            self._fault("before_promote", candidate)
            commit = self._store.promote_candidate(candidate)
            committed = True
            self._fault("after_promote_commit", candidate)
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
                if candidate is not None:
                    failure_code = {
                        "preparation": "bundle_install_failed",
                        "start": "child_start_failed",
                        "ready": "child_registration_timeout",
                        "promotion": "revision_promotion_failed",
                    }[phase]
                    await self._fail_precommit_candidate(candidate, failure_code)
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
            return RevisionActivationResult(
                commit=commit,
                prior_runtime_stopped=False,
                cleanup_pending=False,
            )

        self._fault("before_prior_stop", candidate)
        try:
            await _await_if_needed(self._stop_runtime(previous_runtime))
        except Exception:
            logger.exception(
                "prior runtime stop failed after revision promotion",
                extra={"revision_id": commit.revision_id},
            )
            return RevisionActivationResult(
                commit=commit,
                prior_runtime_stopped=False,
                cleanup_pending=True,
            )
        self._fault("after_prior_stop", candidate)
        return RevisionActivationResult(
            commit=commit,
            prior_runtime_stopped=True,
            cleanup_pending=False,
        )

    async def reconcile_after_crash(
        self, owner_user_id: str, agent_id: str
    ) -> RecoveryPlan:
        """Stop every non-authoritative candidate named by durable recovery."""

        plan = self._store.recovery_plan(owner_user_id, agent_id)
        for runtime_instance_id in plan.stop_runtime_instance_ids:
            try:
                await _await_if_needed(self._stop_runtime(runtime_instance_id))
            except Exception:
                logger.exception(
                    "non-authoritative runtime stop failed during recovery",
                    extra={"runtime_instance_id": runtime_instance_id},
                )
        return plan


class AgentLifecycleManager:
    """Manages draft agent creation, testing, approval, and promotion to live."""

    def __init__(
        self,
        db,
        orchestrator=None,
        process_supervisor=None,
        *,
        byo_runtime_contract_version: int = BYO_RUNTIME_CONTRACT_VERSION,
        byo_runtime_lock_sha256: str = BYO_RUNTIME_LOCK_SHA256,
        artifact_store: Optional[ImmutableAgentArtifactStore] = None,
    ):
        """
        Args:
            db: Database instance with draft_agents CRUD methods
            orchestrator: Orchestrator instance (for LLM client reuse and WS broadcasts)
        """
        self.db = db
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
        self._artifact_store = artifact_store
        self._draft_processes: Dict[str, Any] = {}  # draft_id -> supervised process
        self._agents_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', 'agents')
        )

    @property
    def artifact_store(self) -> ImmutableAgentArtifactStore:
        """Persistent immutable BYO revision store, initialized on first use."""

        if self._artifact_store is None:
            self._artifact_store = ImmutableAgentArtifactStore()
        return self._artifact_store

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

    def _append_log(self, draft_id: str, message: str):
        """Append a message to the draft's generation_log."""
        draft = self.db.get_draft_agent(draft_id)
        if not draft:
            return
        log = json.loads(draft.get("generation_log") or "[]")
        log.append({"message": message, "timestamp": int(time.time() * 1000)})
        self.db.update_draft_agent(draft_id, generation_log=json.dumps(log))

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
                                 config_resolver=None) -> tuple:
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
            await asyncio.to_thread(
                self._append_log,
                draft_id,
                f"Spec validation {'passed' if report.passed else 'failed'}: "
                f"{report.tools_passed}/{report.tools_tested} tools passed",
            )

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
                await asyncio.to_thread(self._append_log, draft_id, f"Auto-fix attempt {attempt + 1}: {fix_prompt[:200]}")

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
                        await asyncio.to_thread(self._append_log, draft_id, f"Auto-fix produced syntax error: {e}")
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
                        await asyncio.to_thread(
                            self._append_log,
                            draft_id,
                            "Auto-fix refused by security analysis "
                            f"(max_severity={fix_report.max_severity}); "
                            "keeping the last clean code.",
                        )
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
                    await asyncio.to_thread(self._append_log, draft_id, f"Auto-fix failed: {e}")
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

    async def create_draft(self, user_id: str, agent_name: str, description: str,
                            tools_spec: List[Dict] = None, skill_tags: List[str] = None,
                            packages: List[str] = None,
                            revises_agent_id: Optional[str] = None) -> Dict[str, Any]:
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

        await asyncio.to_thread(
            self.db.create_draft_agent,
            draft_id=draft_id,
            user_id=user_id,
            agent_name=agent_name.strip(),
            agent_slug=slug,
            description=description.strip(),
            tools_spec=json.dumps(tools_spec) if tools_spec else None,
            skill_tags=json.dumps(skill_tags) if skill_tags else None,
            packages=json.dumps(packages) if packages else None,
            revises_agent_id=revises_agent_id,
        )

        logger.info(f"Created draft agent '{agent_name}' (id={draft_id}, slug={slug}) for user {user_id}")
        return await asyncio.to_thread(self.db.get_draft_agent, draft_id)

    # Generate Code

    async def generate_code(self, draft_id: str, websocket=None, *,
                            target: str = BACKEND_TARGET,
                            agent_id: Optional[str] = None,
                            expected_state_revision: Optional[int] = None,
                            generation_claim_id: Optional[str] = None) -> Dict[str, Any]:
        """Generate the 3 agent files for a draft.

        Args:
            target: ``backend`` (027 — server-hosted: agent.py + mcp_server.py +
                mcp_tools.py, run as a subprocess here) or ``byo`` (060 — the
                self-contained desktop bundle: agent_main.py +
                astralprims_ui.py + mcp_tools.py plus its deterministic runtime
                manifest, delivered to the owner's host and never run here).
            agent_id: the identity to bake into the generated card. Defaults to
                the slug-derived ``<slug>-1``; BYO passes the owner-namespaced id.
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
        claimed = await asyncio.to_thread(
            self.db.claim_draft_generation,
            draft_id=draft_id,
            owner_user_id=owner_user_id,
            expected_revision=expected_state_revision,
            claim_id=generation_claim_id,
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
        slug = draft["agent_slug"]
        agent_name = draft["agent_name"]
        description = draft["description"]
        tools_spec = json.loads(draft["tools_spec"]) if draft.get("tools_spec") else []
        skill_tags = json.loads(draft["skill_tags"]) if draft.get("skill_tags") else []
        packages = json.loads(draft["packages"]) if draft.get("packages") else []
        agent_id = agent_id or self.generator.default_agent_id(slug)

        async def finish_generation(
            status: str,
            *,
            error_message: Optional[str] = None,
            security_report: Optional[str] = None,
            validation_report: Optional[str] = None,
            required_credentials: Optional[str] = None,
        ) -> Dict[str, Any]:
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

        # BYO codegen uses the OWNER's LLM, not the admin-managed system credential.
        # A user authoring their own private agent is an INTERACTIVE turn on their
        # own socket — the same LLM that drafted every authoring phase — so their
        # code is generated with their model too. (The generator's default resolver
        # is the system config, which feature 054 reserves for background work and
        # which is unset on deployments that never configured a system LLM.) Falls
        # back to the system resolver when we can't identify the owner (e.g. a
        # server-side call with no socket).
        codegen_resolver = None
        if is_byo and websocket is not None and self.orchestrator is not None:
            _orch = self.orchestrator
            try:
                _uid = _orch._llm_context_user_id(websocket)
                if _uid:
                    codegen_resolver = lambda: _orch._llm_store.get_sync(_uid)  # noqa: E731
            except Exception:
                logger.debug("byo codegen: owner LLM resolution unavailable, "
                             "falling back to system resolver", exc_info=True)

        await asyncio.to_thread(self._append_log, draft_id, "Starting code generation...")

        try:
            # Step 1: Generate template files (no LLM needed)
            await self._send_progress(websocket, draft_id, "generating_template",
                                       "Generating agent template files...", GENERATING)
            await asyncio.to_thread(self._append_log, draft_id, "Generating template files...")

            revision_id = None
            if is_byo:
                from orchestrator.agent_constitution import AGENT_CONSTITUTION_VERSION
                revision_id = str(uuid.uuid4())
                template_files = self.generator.generate_byo_scaffold(
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
            await asyncio.to_thread(self._append_log, draft_id, "Generating tool implementations...")

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

            all_files = {**template_files, "mcp_tools.py": tools_code}

            # Step 2.5: Syntax validation on ALL generated files
            await self._send_progress(websocket, draft_id, "syntax_check",
                                       "Validating Python syntax...", GENERATING)
            await asyncio.to_thread(self._append_log, draft_id, "Validating syntax of generated files...")

            for fname, code in all_files.items():
                if not fname.endswith(".py"):
                    continue
                try:
                    compile(code, f"{slug}/{fname}", "exec")
                except SyntaxError as e:
                    error_msg = f"Syntax error in {fname} (line {e.lineno}): {e.msg}"
                    logger.error(f"Generated code has syntax error: {error_msg}")
                    state = await finish_generation(
                        ERROR, error_message=error_msg
                    )
                    await self._send_progress(websocket, draft_id, "syntax_error",
                                               error_msg, ERROR)
                    await asyncio.to_thread(self._append_log, draft_id, f"SYNTAX ERROR: {error_msg}")
                    return state

            # Step 2.6 (BYO): the bundle must be self-contained — the desktop host
            # ships no backend package, so a `from shared…` import is a dead agent
            # on the user's machine. Gate the LLM's file, don't just ask for it.
            if is_byo:
                bad = self._byo_import_violations(all_files)
                if bad:
                    error_msg = ("Generated bundle is not self-contained "
                                 f"(forbidden imports: {bad}). Not delivered.")
                    state = await finish_generation(
                        ERROR, error_message=error_msg
                    )
                    await self._send_progress(websocket, draft_id, "not_self_contained",
                                               error_msg, ERROR)
                    await asyncio.to_thread(self._append_log, draft_id, f"BYO GATE: {error_msg}")
                    return state

            # Step 3: Security analysis
            await self._send_progress(websocket, draft_id, "security_scan",
                                       "Running security analysis...", GENERATING)
            await asyncio.to_thread(self._append_log, draft_id, "Running security analysis on generated code...")

            report = self.security.analyze(tools_code, filename=f"{slug}/mcp_tools.py")

            # Pre-execution nefarious-activity gate: HIGH (os.environ access,
            # globals()/setattr tricks, obfuscation) blocks alongside CRITICAL
            # — nothing below this line may run code the analyzer flagged.
            if blocks_execution(report):
                state = await finish_generation(
                    ERROR,
                    security_report=json.dumps(report.to_dict()),
                    error_message="Security analysis found blocking issues in generated code.",
                )
                await self._send_progress(websocket, draft_id, "security_failed",
                                           "Security analysis found blocking issues. Code was not written.",
                                           ERROR, detail=report.to_dict())
                await asyncio.to_thread(self._append_log, draft_id, f"Security analysis FAILED: {report.recommendation}")
                return state

            # Step 4: Write server-hosted draft working files to disk. BYO
            # executable bytes are not written into the shared slug directory;
            # they remain in memory until the immutable revision publisher
            # commits the complete, validated bundle below.
            await self._send_progress(websocket, draft_id, "writing_files",
                                       "Writing agent files...", GENERATING)
            await asyncio.to_thread(self._append_log, draft_id, "Writing agent files to disk...")

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
            )

            # An auto-fix round could reintroduce a backend import — re-gate the
            # code we are actually about to hand the host.
            if is_byo:
                bad = self._byo_import_violations({"mcp_tools.py": tools_code})
                if bad:
                    error_msg = ("Auto-fixed bundle is not self-contained "
                                 f"(forbidden imports: {bad}). Not delivered.")
                    state = await finish_generation(
                        ERROR, error_message=error_msg
                    )
                    await self._send_progress(websocket, draft_id, "not_self_contained",
                                               error_msg, ERROR)
                    await asyncio.to_thread(self._append_log, draft_id, f"BYO GATE: {error_msg}")
                    return state

            # Step 5.5: Extract required credentials declared by LLM
            required_creds = self._extract_required_credentials(tools_code)
            if required_creds:
                await asyncio.to_thread(self._append_log, draft_id, f"Detected {len(required_creds)} required credential(s)")
                await self._send_progress(
                    websocket, draft_id, "credentials_detected",
                    f"This agent requires {len(required_creds)} credential(s). You'll need to provide them before testing.",
                    GENERATING,
                    detail={"required_credentials": required_creds},
                )

            # Step 6: Finalize and durably publish the exact BYO revision before
            # reporting generation success. Runtime start/ready/promotion remains
            # a separate lifecycle transaction owned by AgentRevisionActivator.
            finalized = None
            published_artifact = None
            publication_id = None
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
                publication_id = str(uuid.uuid4())
                draft_uuid = str(draft.get("draft_uuid") or draft_id)
                source_state_revision = claimed_revision

                def publication_fence(_boundary: str) -> None:
                    current = self.db.get_draft_agent(draft_id)
                    if not current:
                        raise RuntimeError("draft publication fence is stale")
                    if (
                        str(current.get("user_id") or "") != owner_user_id
                        or str(current.get("draft_uuid") or draft_id) != draft_uuid
                        or int(current.get("state_revision") or 0)
                        != source_state_revision
                        or str(current.get("generation_claim_id") or "")
                        != generation_claim_id
                    ):
                        raise RuntimeError("draft publication fence is stale")

                await self._send_progress(
                    websocket,
                    draft_id,
                    "publishing_artifact",
                    "Publishing immutable agent revision...",
                    GENERATING,
                )
                published_artifact = await asyncio.to_thread(
                    self.artifact_store.publish,
                    finalized,
                    draft_uuid=draft_uuid,
                    source_state_revision=source_state_revision,
                    publication_id=publication_id,
                    agent_id=agent_id,
                    revision_id=revision_id,
                    fence_check=publication_fence,
                )

            # Only a fully published revision may transition to generated.
            update_kwargs = {
                "security_report": json.dumps(report.to_dict()) if report.findings else None,
                "validation_report": json.dumps(validation_report.to_dict()),
                "required_credentials": json.dumps(required_creds) if required_creds else None,
            }
            if not validation_report.passed:
                update_kwargs["error_message"] = (
                    f"Spec validation failed: {validation_report.tools_passed}/"
                    f"{validation_report.tools_tested} tools passed. "
                    "You can still test manually or refine the agent."
                )

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
            await asyncio.to_thread(self._append_log, draft_id, "Code generation complete!")

            # Hand the caller the re-opened immutable bundle (mcp_tools.py may
            # have been auto-fixed since first generation). The v2 metadata
            # envelope remains outside the three-file artifact hash.
            if is_byo:
                if finalized is None or published_artifact is None:
                    raise RuntimeError("immutable BYO publication was not completed")
                state["files"] = {
                    **dict(published_artifact.files),
                    "manifest.json": published_artifact.manifest_json,
                }
                state["runtime_manifest"] = published_artifact.manifest_dict()
                state["bundle_sha256"] = published_artifact.bundle_sha256
                state["manifest_sha256"] = published_artifact.manifest_sha256
                state["artifact_relative_path"] = (
                    published_artifact.artifact_relative_path
                )
                state["publication_id"] = publication_id
                state["revision_id"] = revision_id
                state["runtime_contract_version"] = (
                    self._byo_runtime_contract_version
                )
                state["required_runtime_lock_sha256"] = (
                    self._byo_runtime_lock_sha256
                )
            else:
                state["files"] = final_files
            return state

        except Exception as e:
            logger.error(f"Code generation failed for draft {draft_id}: {e}")
            state = await finish_generation(ERROR, error_message=str(e))
            await self._send_progress(websocket, draft_id, "error",
                                       f"Code generation failed: {e}", ERROR)
            await asyncio.to_thread(self._append_log, draft_id, f"ERROR: {e}")
            return state

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

        proc = self.process_supervisor.spawn(
            process_id=uuid.uuid4(),
            owner=ProcessOwner(owner_kind="draft_agent", owner_id=draft_id),
            argv=(python_exe, agent_script, "--port", str(port)),
            cwd=agent_dir,
            **sandbox_kwargs,
        )
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
        self._purge_agent_permission_rows(runtime_agent_id)

        logger.info(f"Deleted draft agent {draft_id} ({draft['agent_name']})")
        return True

    def _purge_agent_permission_rows(self, agent_id: str) -> None:
        """Remove agent_scopes / tool_overrides / tool_permissions /
        agent_ownership rows for a retired draft's runtime agent id
        (best-effort — deletion must not fail the discard)."""
        for table in ("agent_scopes", "tool_overrides", "tool_permissions",
                      "agent_ownership"):
            try:
                self.db.execute(f"DELETE FROM {table} WHERE agent_id = ?",  # noqa: S608 — fixed table list
                                (agent_id,))
            except Exception:
                logger.debug("draft permission purge failed (%s/%s)",
                             table, agent_id, exc_info=True)

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
            rows = self.db.fetch_all(
                "SELECT DISTINCT agent_id FROM agent_scopes WHERE agent_id LIKE '%-1'")
            if agent_ids is not None:
                rows = [r for r in rows if r["agent_id"] in set(agent_ids)]
            known_slugs = {d["agent_slug"] for d in (self.db.list_draft_agents() or [])} \
                if hasattr(self.db, "list_draft_agents") else None
            for row in rows:
                agent_id = row["agent_id"]
                slug = agent_id[:-2].replace("-", "_")
                if known_slugs is not None:
                    has_draft_row = slug in known_slugs
                else:
                    has_draft_row = bool(self.db.get_draft_agent_by_slug(slug))
                if has_draft_row:
                    continue
                agent_dir = os.path.join(self._agents_dir, slug)
                dir_exists = os.path.isdir(agent_dir)
                if dir_exists and not os.path.exists(os.path.join(agent_dir, ".draft")):
                    continue  # real (bundled/approved) agent directory
                self._purge_agent_permission_rows(agent_id)
                purged += 1
                logger.info("Purged leaked draft permissions: %s "
                            "(dir_exists=%s)", agent_id, dir_exists)
        except Exception:
            logger.warning("orphaned-draft permission sweep failed", exc_info=True)
        return purged
