"""Deep policy coordination for Plane-owned generated-agent publication.

AstralPlane owns the durable journal and immutable filesystem mechanics.  This
module owns only product workflow identity and, once composed, the sequencing
between those two Plane boundaries.  Publication identities are derived from
the exact claimed draft generation so a process restart cannot create a second
target for the same durable attempt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astralplane import (
    GENERATED_AGENT_BUNDLE_CONTRACT,
    AgentRevisionRecord,
    BundlePublicationKey,
    BundleRecoveryDisposition,
    DraftPublicationRecord,
    ExecutionFence as PlaneExecutionFence,
    FinalizedBundle,
    GeneratedAgentPublicationResultMetadata,
    PublishedBundle,
    StagedBundleReceipt,
    canonical_generated_agent_manifest_digest,
    generated_agent_publication_operation_binding,
    generated_agent_publication_paths,
    generated_agent_publication_recovery_operation_binding,
    runtime_metadata_for_manifest,
)

from orchestrator.work_admission import (
    AcceptedAdmission,
    AcceptedSubmission,
    AdmissionClass,
    ExecutionFence,
    OperationClaim,
    OperationOwner,
    OperationRecord,
    OperationRequest,
    OperationState,
    OwnerScope,
    RefusedAdmission,
    WorkAdmissionCoordinator,
)


_SAFE_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_IDENTITY_DOMAIN = "astraldeep.generated-agent-publication/v1"
_RECOVERY_IDENTITY_DOMAIN = "astraldeep.generated-agent-publication-recovery/v1"
_FAILURE_SUMMARY = "Generated-agent publication failed safely."
_CANCELLATION_SUMMARY = "Generated-agent publication was cancelled."
_EXPIRED_CLAIM_SUMMARY = (
    "Generated-agent generation expired before durable publication began."
)
_MAX_INVENTORY_PAGE = 1_000
_JOURNAL_OVERFLOW_MARKER = "inventory:journal-overflow"
_EXPIRED_CLAIM_OVERFLOW_MARKER = "inventory:prejournal-overflow"
_PUBLISHED_REVISION_STATES = frozenset(
    {"prepared", "starting", "ready", "active", "retired", "failed"}
)
_LOGGER = logging.getLogger(__name__)


class GeneratedAgentPublicationError(RuntimeError):
    """Base error with an explicit draft-claim ownership disposition."""

    claim_managed: bool = False


class GeneratedAgentPublicationPreIntentError(GeneratedAgentPublicationError):
    """Publication intent was not durable; the caller still owns its claim."""


class GeneratedAgentPublicationManagedError(GeneratedAgentPublicationError):
    """The service durably terminalized the claim after journal begin."""

    claim_managed = True


class GeneratedAgentPublicationRecoveryPendingError(GeneratedAgentPublicationError):
    """A durable nonterminal intent owns the claim and requires recovery."""

    claim_managed = True


class GeneratedAgentPublicationManagedCancellation(GeneratedAgentPublicationError):
    """Cause marker attached to caller cancellation after journal begin."""

    claim_managed = True


@dataclass(frozen=True, slots=True)
class GeneratedAgentPublicationIdentity:
    """Replay-stable UUID4-shaped identities for one claimed generation."""

    publication_id: uuid.UUID
    target_revision_id: uuid.UUID
    promotion_token: uuid.UUID
    submission_id: uuid.UUID
    request_generation: uuid.UUID


@dataclass(frozen=True, slots=True)
class GeneratedAgentPublicationRequest:
    """All immutable inputs for one journaled generated-agent publication."""

    owner_id: str
    draft_uuid: str
    source_state_revision: int
    generation_claim_id: str
    target_agent_id: str
    bundle: FinalizedBundle
    runtime_contract_version: int
    release_lock_digest: str
    generation_result: GeneratedAgentPublicationResultMetadata
    compatibility_state: str = "compatible"

    def __post_init__(self) -> None:
        generated_agent_publication_identity(
            owner_id=self.owner_id,
            draft_uuid=self.draft_uuid,
            source_state_revision=self.source_state_revision,
            generation_claim_id=self.generation_claim_id,
            target_agent_id=self.target_agent_id,
        )
        if not isinstance(self.bundle, FinalizedBundle):
            raise TypeError("bundle must be a Plane FinalizedBundle")
        if (
            type(self.runtime_contract_version) is not int
            or self.runtime_contract_version <= 0
        ):
            raise ValueError("runtime_contract_version must be positive")
        if (
            not isinstance(self.release_lock_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.release_lock_digest) is None
        ):
            raise ValueError("release_lock_digest must be lowercase SHA-256")
        if not isinstance(
            self.generation_result, GeneratedAgentPublicationResultMetadata
        ):
            raise TypeError("generation_result must be Plane result metadata")


@dataclass(frozen=True, slots=True)
class GeneratedAgentPublicationResult:
    """Exact committed journal, revision, filesystem, and report evidence."""

    publication: DraftPublicationRecord
    revision: AgentRevisionRecord
    published: PublishedBundle
    generation_result: GeneratedAgentPublicationResultMetadata
    claim_managed: bool = field(default=True, init=False)


@dataclass(frozen=True, slots=True)
class GeneratedAgentRecoveryReport:
    """Bounded evidence from one recovery pass."""

    inspected: int
    recovered: int
    failed: int
    skipped_live: int
    degraded_publication_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GeneratedAgentPublicationReadiness:
    """Readiness evidence without exposing owner or manifest contents."""

    ready: bool
    unresolved_count: int
    unresolved_publication_ids: tuple[str, ...]
    ignored_live_count: int


class GenerationClaimLostError(RuntimeError):
    """The exact draft-generation claim can no longer authorize effects."""


class _ExpiredClaimBecameJournaled(RuntimeError):
    """The pre-journal reclaim raced with a durable publication intent."""


class GenerationClaimHeartbeat:
    """Renew one DB-time claim and surface lease loss at workflow boundaries.

    The supplied callback is synchronous because production renewal runs in a
    caller-owned Plane transaction.  It always executes off the event loop.
    ``close`` joins through repeated caller cancellation so a renewal worker is
    never orphaned while its database outcome is still ambiguous.
    """

    def __init__(
        self,
        renew: Callable[[], Any | None],
        *,
        interval_seconds: float,
        task_name: str,
    ) -> None:
        if not callable(renew):
            raise TypeError("renew must be callable")
        if not isinstance(interval_seconds, (int, float)) or isinstance(
            interval_seconds, bool
        ):
            raise TypeError("interval_seconds must be numeric")
        if not 0 < float(interval_seconds) < float("inf"):
            raise ValueError("interval_seconds must be positive")
        if not isinstance(task_name, str) or not task_name:
            raise ValueError("task_name must be non-empty")
        self._renew = renew
        self._interval_seconds = float(interval_seconds)
        self._task_name = task_name
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._closing = False

    def start(self) -> None:
        """Start exactly one renewal loop on the current event loop."""

        if self._task is not None:
            raise RuntimeError("generation claim heartbeat is already started")
        self._task = asyncio.create_task(self._run(), name=self._task_name)
        self._task.add_done_callback(self._record_completion)

    def assert_healthy(self) -> None:
        """Fail before the next effect when renewal lost durable authority."""

        failure = self._failure
        if failure is not None:
            raise GenerationClaimLostError(
                "draft generation claim renewal failed"
            ) from failure
        task = self._task
        if task is None:
            raise RuntimeError("generation claim heartbeat is not started")
        if task.done() and not self._closing:
            raise GenerationClaimLostError(
                "draft generation claim heartbeat stopped unexpectedly"
            )

    async def close(self) -> None:
        """Stop and join renewal before claim finalization or publication."""

        task = self._task
        if task is None:
            return
        self._closing = True
        self._stop.set()
        cancellation: asyncio.CancelledError | None = None
        loop = asyncio.get_running_loop()
        completed: asyncio.Future[None] = loop.create_future()

        def signal_completion(_task: asyncio.Task[None]) -> None:
            if not completed.done():
                completed.set_result(None)

        task.add_done_callback(signal_completion)
        while not completed.done():
            try:
                await asyncio.shield(completed)
            except asyncio.CancelledError as exc:
                cancellation = exc
        failure: BaseException | None = None
        if task.cancelled():
            failure = GenerationClaimLostError(
                "draft generation claim heartbeat was cancelled"
            )
        else:
            try:
                task.result()
            except BaseException as exc:
                failure = exc
        if failure is None:
            failure = self._failure
        if cancellation is not None:
            if failure is not None:
                raise cancellation from failure
            raise cancellation
        if failure is not None:
            raise GenerationClaimLostError(
                "draft generation claim renewal failed"
            ) from failure

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            renewed = await asyncio.to_thread(self._renew)
            if renewed is None:
                raise GenerationClaimLostError(
                    "draft generation claim is stale or expired"
                )

    def _record_completion(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            self._failure = GenerationClaimLostError(
                "draft generation claim heartbeat was cancelled"
            )
            return
        try:
            task.result()
        except Exception as exc:
            self._failure = exc


def generated_agent_publication_identity(
    *,
    owner_id: str,
    draft_uuid: str,
    source_state_revision: int,
    generation_claim_id: str,
    target_agent_id: str,
) -> GeneratedAgentPublicationIdentity:
    """Derive every external identity from one exact draft claim.

    UUID version bits are forced to v4 because the established Plane schema and
    path contract require UUID4 text.  Entropy remains the SHA-256 digest of a
    canonical, domain-separated tuple; no host clock, process id, mapping order,
    or Python hash seed enters the result.
    """

    if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 512:
        raise ValueError("owner_id must be non-empty and bounded")
    try:
        normalized_draft = str(uuid.UUID(str(draft_uuid)))
        normalized_claim = str(uuid.UUID(str(generation_claim_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("draft and claim identities must be UUIDs") from exc
    if uuid.UUID(normalized_draft).version != 4:
        raise ValueError("draft_uuid must be a UUID4")
    if uuid.UUID(normalized_claim).version != 4:
        raise ValueError("generation_claim_id must be a UUID4")
    if type(source_state_revision) is not int or source_state_revision < 0:
        raise ValueError("source_state_revision must be non-negative")
    if (
        not isinstance(target_agent_id, str)
        or _SAFE_AGENT_ID.fullmatch(target_agent_id) is None
        or target_agent_id in {".", ".."}
    ):
        raise ValueError("target_agent_id is not a safe bounded identity")

    common = {
        "domain": _IDENTITY_DOMAIN,
        "draft_uuid": normalized_draft,
        "generation_claim_id": normalized_claim,
        "owner_id": owner_id,
        "source_state_revision": source_state_revision,
        "target_agent_id": target_agent_id,
    }

    def derive(purpose: str) -> uuid.UUID:
        encoded = json.dumps(
            {**common, "purpose": purpose},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return uuid.UUID(bytes=hashlib.sha256(encoded).digest()[:16], version=4)

    return GeneratedAgentPublicationIdentity(
        publication_id=derive("publication"),
        target_revision_id=derive("revision"),
        promotion_token=derive("promotion"),
        submission_id=derive("submission"),
        request_generation=derive("request-generation"),
    )


@dataclass(slots=True)
class _WorkerOutcome:
    result: Any = None
    error: BaseException | None = None
    cancellation: asyncio.CancelledError | None = None


@dataclass(frozen=True, slots=True)
class _ExpiredClaimInventory:
    claims: tuple[Any, ...]
    raw_count: int
    next_cursor: tuple[datetime, str] | None = None
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class _PublicationInventory:
    publications: tuple[Any, ...]
    next_cursor: tuple[datetime, str] | None
    has_more: bool


@dataclass(slots=True)
class _PublicationAttempt:
    request: GeneratedAgentPublicationRequest | None
    task: asyncio.Task[GeneratedAgentPublicationResult] | None = None
    waiters: int = 0
    accepting_waiters: bool = True
    publication: Any | None = None
    deep_fence: ExecutionFence | None = None
    plane_fence: PlaneExecutionFence | None = None
    staged_receipt: StagedBundleReceipt | None = None
    published: PublishedBundle | None = None
    cancellation_event: threading.Event = field(default_factory=threading.Event)
    snapshot_lock: threading.RLock = field(default_factory=threading.RLock)
    io_lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> tuple[Any, PlaneExecutionFence]:
        with self.snapshot_lock:
            if self.publication is None or self.plane_fence is None:
                raise GenerationClaimLostError(
                    "publication attempt is not journal-bound"
                )
            return self.publication, self.plane_fence

    def set_publication(self, publication: Any) -> None:
        with self.snapshot_lock:
            self.publication = publication


class GeneratedAgentPublicationService:
    """Coordinate Deep policy over Plane's journal and immutable store.

    The service never performs database or filesystem work on the event loop.
    A small process-local lock protects only admission/current-attempt snapshots;
    it is never held while calling Plane or waiting for worker completion.
    """

    def __init__(
        self,
        *,
        plane_runtime: Any,
        plane_repositories: Any,
        bundle_store: Any,
        work_admission: WorkAdmissionCoordinator,
        claim_lease_seconds: int = 300,
        heartbeat_interval_seconds: float = 10.0,
        recovery_interval_seconds: float = 30.0,
        recovery_batch_size: int = 100,
    ) -> None:
        if plane_runtime is None or not callable(
            getattr(plane_runtime, "transaction", None)
        ):
            raise TypeError("plane_runtime must expose transaction()")
        for name in ("generated_agent_publications", "agents", "draft_agents"):
            if getattr(plane_repositories, name, None) is None:
                raise TypeError(f"plane_repositories.{name} is required")
        for name in (
            "stage",
            "promote_staged",
            "quarantine_staged",
            "quarantine_receipt",
            "recover",
            "load",
        ):
            if not callable(getattr(bundle_store, name, None)):
                raise TypeError(f"bundle_store.{name} is required")
        if not isinstance(work_admission, WorkAdmissionCoordinator):
            raise TypeError("work_admission must be a WorkAdmissionCoordinator")
        if type(claim_lease_seconds) is not int or not 1 <= claim_lease_seconds <= 1800:
            raise ValueError("claim_lease_seconds must be in 1..1800")
        for value, name in (
            (heartbeat_interval_seconds, "heartbeat_interval_seconds"),
            (recovery_interval_seconds, "recovery_interval_seconds"),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not 0 < float(value) < float("inf")
            ):
                raise ValueError(f"{name} must be positive")
        if type(recovery_batch_size) is not int or not 1 <= recovery_batch_size <= 1000:
            raise ValueError("recovery_batch_size must be in 1..1000")

        self._runtime = plane_runtime
        self._repositories = plane_repositories
        self._journal = plane_repositories.generated_agent_publications
        self._agents = plane_repositories.agents
        self._drafts = plane_repositories.draft_agents
        self._store = bundle_store
        self._admission = work_admission
        self._claim_lease_seconds = claim_lease_seconds
        self._heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self._recovery_interval_seconds = float(recovery_interval_seconds)
        self._recovery_batch_size = recovery_batch_size
        self._state_lock = threading.RLock()
        self._attempts: dict[tuple[str, str, int], _PublicationAttempt] = {}
        self._recovery_attempts: dict[str, _PublicationAttempt] = {}
        self._closing = False
        self._started = False
        self._recovery_running = False
        self._journal_recovery_cursor: tuple[datetime, str] | None = None
        self._expired_claim_recovery_cursor: tuple[datetime, str] | None = None
        self._recovery_owner_task: asyncio.Task[Any] | None = None
        self._recovery_stop: asyncio.Event | None = None
        self._recovery_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start bounded recurring recovery on the current event loop."""

        with self._state_lock:
            if self._closing:
                raise RuntimeError("generated-agent publication service is closing")
            if self._started:
                return
            self._started = True
            self._recovery_stop = asyncio.Event()
            task = asyncio.create_task(
                self._recovery_loop(),
                name="generated-agent-publication-recovery",
            )
            self._recovery_task = task

    async def publish(
        self,
        request: GeneratedAgentPublicationRequest,
    ) -> GeneratedAgentPublicationResult:
        """Publish once, joining exact in-process callers by source identity."""

        if not isinstance(request, GeneratedAgentPublicationRequest):
            raise TypeError("request must be GeneratedAgentPublicationRequest")
        with self._state_lock:
            if self._closing:
                raise GeneratedAgentPublicationPreIntentError(
                    "generated-agent publication service is closing"
                )
        try:
            terminal = await self.load_published(
                owner_id=request.owner_id,
                draft_uuid=request.draft_uuid,
                source_state_revision=request.source_state_revision,
            )
        except asyncio.CancelledError as cancelled:
            joined_result = getattr(cancelled, "_joined_worker_result", None)
            joined_error = getattr(cancelled, "_joined_worker_error", None)
            if joined_error is not None or joined_result is not None:
                if isinstance(
                    joined_error,
                    GeneratedAgentPublicationRecoveryPendingError,
                ):
                    marker = joined_error
                else:
                    marker = GeneratedAgentPublicationRecoveryPendingError(
                        "authoritative publication replay was ambiguous at cancellation"
                    )
                    marker.__cause__ = joined_error
                raise cancelled from marker
            raise
        except GeneratedAgentPublicationError:
            raise
        except Exception as exc:
            raise GeneratedAgentPublicationRecoveryPendingError(
                "durable publication absence could not be established"
            ) from exc
        if terminal is not None:
            self._assert_terminal_matches_request(terminal, request)
            return terminal
        key = (request.owner_id, request.draft_uuid, request.source_state_revision)
        with self._state_lock:
            if self._closing:
                raise GeneratedAgentPublicationPreIntentError(
                    "generated-agent publication service is closing"
                )
            attempt = self._attempts.get(key)
            if attempt is None or not attempt.accepting_waiters:
                attempt = _PublicationAttempt(request=request)
                task = asyncio.create_task(
                    self._publish_attempt(attempt),
                    name=f"generated-agent-publication:{request.draft_uuid}",
                )
                attempt.task = task
                self._attempts[key] = attempt
                task.add_done_callback(
                    lambda completed, source=key, current=attempt: self._attempt_done(
                        source, current, completed
                    )
                )
            elif attempt.request != request:
                raise GeneratedAgentPublicationRecoveryPendingError(
                    "a live exact-source publication owns the generation claim and "
                    "has different immutable inputs"
                )
            attempt.waiters += 1
            task = attempt.task
        if task is None:  # pragma: no cover - guarded by construction above.
            raise RuntimeError("publication task was not retained")

        released = False
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as caller_cancel:
            joined_cancelled_child = False
            shared_attempt_owned = False
            with self._state_lock:
                attempt.waiters -= 1
                released = True
                if attempt.waiters == 0 and not task.done():
                    attempt.accepting_waiters = False
                    task.cancel()
                    joined_cancelled_child = True
                elif attempt.waiters > 0:
                    shared_attempt_owned = True
            if joined_cancelled_child:
                joined = await self._join_task(task)
                marker = self._cancellation_marker(attempt, joined.error)
                raise caller_cancel from marker
            marker = self._cancellation_marker(
                attempt,
                None,
                shared_attempt_owned=shared_attempt_owned,
            )
            if marker is not None:
                raise caller_cancel from marker
            raise
        finally:
            if not released:
                with self._state_lock:
                    if attempt.waiters > 0:
                        attempt.waiters -= 1

    async def load_published(
        self,
        *,
        owner_id: str,
        draft_uuid: str,
        source_state_revision: int,
    ) -> GeneratedAgentPublicationResult | None:
        """Load exact terminal bytes and persisted reports without regeneration."""

        def read_records() -> (
            tuple[
                DraftPublicationRecord,
                AgentRevisionRecord,
                GeneratedAgentPublicationResultMetadata,
                Any,
            ]
            | None
        ):
            with self._runtime.transaction() as transaction:
                publication = self._journal.get_by_source(
                    transaction,
                    owner_id=owner_id,
                    draft_uuid=draft_uuid,
                    source_state_revision=source_state_revision,
                )
                if publication is None or publication.state != "published":
                    return None
                revision = self._agents.get_revision(
                    transaction,
                    owner_id=owner_id,
                    agent_id=publication.target_agent_id,
                    revision_id=publication.target_revision_id,
                )
                agent = self._agents.get_agent(
                    transaction,
                    owner_id=owner_id,
                    agent_id=publication.target_agent_id,
                )
                draft = self._drafts.get_draft_by_uuid(
                    transaction,
                    owner_id=owner_id,
                    draft_uuid=draft_uuid,
                )
                if revision is None or agent is None or draft is None:
                    raise GeneratedAgentPublicationRecoveryPendingError(
                        "published journal provenance is incomplete"
                    )
                try:
                    identity = generated_agent_publication_identity(
                        owner_id=publication.owner_id,
                        draft_uuid=publication.draft_uuid,
                        source_state_revision=publication.source_state_revision,
                        generation_claim_id=publication.generation_claim_id,
                        target_agent_id=publication.target_agent_id,
                    )
                    manifest = revision.manifest or {}
                    runtime_metadata = runtime_metadata_for_manifest(
                        GENERATED_AGENT_BUNDLE_CONTRACT,
                        manifest,
                    )
                    expected_manifest_digest = (
                        canonical_generated_agent_manifest_digest(manifest)
                    )
                except (TypeError, ValueError) as exc:
                    raise GeneratedAgentPublicationRecoveryPendingError(
                        "published journal provenance is invalid"
                    ) from exc
                if (
                    publication.owner_id != owner_id
                    or publication.draft_uuid != draft_uuid
                    or publication.source_state_revision != source_state_revision
                    or publication.publication_id != str(identity.publication_id)
                    or publication.target_revision_id
                    != str(identity.target_revision_id)
                    or publication.artifact_digest != revision.artifact_digest
                    or publication.manifest_digest != expected_manifest_digest
                    or revision.artifact_relative_path
                    != publication.revision_relative_path
                    or revision.revision_id != publication.target_revision_id
                    or revision.agent_id != publication.target_agent_id
                    or revision.owner_id != publication.owner_id
                    or agent.agent_id != publication.target_agent_id
                    or agent.owner_id != publication.owner_id
                    or agent.deleted_at is not None
                    or revision.promotion_token != str(identity.promotion_token)
                    or revision.runtime_contract_version
                    != manifest["runtime_contract_version"]
                    or revision.release_lock_digest
                    != manifest["required_runtime_lock_sha256"]
                    or revision.artifact_digest != manifest["bundle_sha256"]
                    or revision.compatibility_state != "compatible"
                    or revision.state not in _PUBLISHED_REVISION_STATES
                    or draft.status != "generated"
                    or draft.generation_claim_id is not None
                    or draft.published_revision_id != publication.target_revision_id
                ):
                    raise GeneratedAgentPublicationRecoveryPendingError(
                        "published journal provenance does not match terminal records"
                    )
                result = GeneratedAgentPublicationResultMetadata(
                    error_message=draft.error_message,
                    security_report=draft.security_report,
                    validation_report=draft.validation_report,
                    required_credentials=draft.required_credentials,
                )
                return publication, revision, result, runtime_metadata

        records = await self._offload(read_records)
        if records is None:
            return None
        publication, revision, generation_result, runtime_metadata = records
        if revision.artifact_digest is None or publication.manifest_digest is None:
            raise GeneratedAgentPublicationRecoveryPendingError(
                "published journal digests are incomplete"
            )
        published = await self._offload(
            lambda: self._store.load(
                publication.revision_relative_path,
                expected_digest=revision.artifact_digest,
                expected_manifest_digest=publication.manifest_digest,
            )
        )
        if (
            published.bundle_relative_path != publication.revision_relative_path
            or published.bundle_sha256 != revision.artifact_digest
            or published.manifest_sha256 != publication.manifest_digest
            or published.manifest != revision.manifest
            or published.runtime_metadata != runtime_metadata
        ):
            raise GeneratedAgentPublicationRecoveryPendingError(
                "published filesystem evidence changed terminal provenance"
            )
        return GeneratedAgentPublicationResult(
            publication=publication,
            revision=revision,
            published=published,
            generation_result=generation_result,
        )

    async def recover_once(self) -> GeneratedAgentRecoveryReport:
        """Expire stale workers and reconcile a bounded nonterminal inventory."""

        with self._state_lock:
            if self._closing or self._recovery_running:
                return GeneratedAgentRecoveryReport(0, 0, 0, 0, ())
            self._recovery_running = True
            self._recovery_owner_task = asyncio.current_task()
        try:
            await self._offload(self._admission.expire_execution_leases)
            journal_inventory = await self._offload(self._next_journal_recovery_page)
            expired_inventory = await self._offload(
                self._next_expired_claim_recovery_page
            )
            publications = journal_inventory.publications
            expired_claims = expired_inventory.claims
            recovered = failed = skipped_live = 0
            degraded: list[str] = []
            if journal_inventory.has_more:
                degraded.append(_JOURNAL_OVERFLOW_MARKER)
            if expired_inventory.has_more:
                degraded.append(_EXPIRED_CLAIM_OVERFLOW_MARKER)
            for publication in publications:
                if await self._is_live_attempt(publication):
                    skipped_live += 1
                    continue
                try:
                    disposition = await self._recover_publication(publication)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    degraded.append(publication.publication_id)
                    continue
                if disposition == "recovered":
                    recovered += 1
                elif disposition == "failed":
                    failed += 1
                elif disposition == "live":
                    skipped_live += 1
                else:
                    degraded.append(publication.publication_id)
            for expired_claim in expired_claims:
                marker = self._expired_claim_marker(expired_claim)
                outcome = await self._offload_outcome(
                    lambda claim=expired_claim: self._terminalize_expired_claim(claim)
                )
                self._raise_outcome_cancellation(outcome)
                if outcome.error is None:
                    failed += 1
                else:
                    degraded.append(marker)
            return GeneratedAgentRecoveryReport(
                inspected=len(publications) + len(expired_claims),
                recovered=recovered,
                failed=failed,
                skipped_live=skipped_live,
                degraded_publication_ids=tuple(degraded),
            )
        finally:
            with self._state_lock:
                self._recovery_running = False
                self._recovery_owner_task = None

    async def readiness(self) -> GeneratedAgentPublicationReadiness:
        """Fail readiness for unresolved durable rows, excluding exact live work."""

        await self._offload(self._admission.expire_execution_leases)
        journal_inventory = await self._offload(
            lambda: self._publication_inventory(
                limit=self._recovery_batch_size,
                after=None,
            )
        )
        expired_inventory = await self._offload(
            lambda: self._expired_claim_inventory(
                limit=self._recovery_batch_size,
                after=None,
            )
        )
        publications = journal_inventory.publications
        expired_claims = expired_inventory.claims
        unresolved: list[str] = []
        live = 0
        for publication in publications:
            if await self._is_live_attempt(publication):
                live += 1
            else:
                unresolved.append(publication.publication_id)
        unresolved.extend(self._expired_claim_marker(claim) for claim in expired_claims)
        if journal_inventory.has_more:
            unresolved.append(_JOURNAL_OVERFLOW_MARKER)
        if expired_inventory.has_more:
            unresolved.append(_EXPIRED_CLAIM_OVERFLOW_MARKER)
        return GeneratedAgentPublicationReadiness(
            ready=not unresolved,
            unresolved_count=len(unresolved),
            unresolved_publication_ids=tuple(unresolved),
            ignored_live_count=live,
        )

    async def close(self) -> None:
        """Stop admission, cancel active workflows, and join every retained task."""

        with self._state_lock:
            if (
                self._closing
                and not self._attempts
                and self._recovery_task is None
                and self._recovery_owner_task is None
                and not self._recovery_running
            ):
                return
            self._closing = True
            recovery_stop = self._recovery_stop
            recovery_task = self._recovery_task
            recovery_owner_task = self._recovery_owner_task
            attempts = tuple(self._attempts.values())
        if recovery_stop is not None:
            recovery_stop.set()
        if recovery_task is not None and not recovery_task.done():
            recovery_task.cancel()
        if (
            recovery_owner_task is not None
            and recovery_owner_task is not recovery_task
            and recovery_owner_task is not asyncio.current_task()
            and not recovery_owner_task.done()
        ):
            recovery_owner_task.cancel()
        for attempt in attempts:
            if attempt.task is not None and not attempt.task.done():
                attempt.task.cancel()

        cancellation: asyncio.CancelledError | None = None
        first_error: BaseException | None = None
        for task in tuple(
            task
            for task in (
                recovery_task,
                recovery_owner_task
                if recovery_owner_task is not asyncio.current_task()
                else None,
                *(attempt.task for attempt in attempts),
            )
            if task is not None
        ):
            joined = await self._join_task(task)
            cancellation = cancellation or joined.cancellation
            if (
                joined.error is not None
                and not isinstance(joined.error, asyncio.CancelledError)
                and first_error is None
            ):
                first_error = joined.error
        with self._state_lock:
            self._recovery_task = None
            self._attempts.clear()
        if cancellation is not None:
            if first_error is not None:
                raise cancellation from first_error
            raise cancellation
        if first_error is not None:
            raise first_error

    async def _publish_attempt(
        self,
        attempt: _PublicationAttempt,
    ) -> GeneratedAgentPublicationResult:
        request = attempt.request
        identity = generated_agent_publication_identity(
            owner_id=request.owner_id,
            draft_uuid=request.draft_uuid,
            source_state_revision=request.source_state_revision,
            generation_claim_id=request.generation_claim_id,
            target_agent_id=request.target_agent_id,
        )
        try:
            binding = generated_agent_publication_operation_binding(
                owner_id=request.owner_id,
                publication_id=str(identity.publication_id),
                draft_uuid=request.draft_uuid,
                source_state_revision=request.source_state_revision,
                generation_claim_id=request.generation_claim_id,
                target_agent_id=request.target_agent_id,
                target_revision_id=str(identity.target_revision_id),
                bundle=request.bundle,
                runtime_contract_version=request.runtime_contract_version,
                release_lock_digest=request.release_lock_digest,
                promotion_token=str(identity.promotion_token),
                compatibility_state=request.compatibility_state,
            )
            deep_fence = await self._admit_and_claim(
                binding=binding,
                owner_id=request.owner_id,
                submission_id=identity.submission_id,
                request_generation=identity.request_generation,
                attempt=attempt,
            )
            plane_fence = self._to_plane_fence(deep_fence)
            attempt.deep_fence = deep_fence
            attempt.plane_fence = plane_fence
            paths = generated_agent_publication_paths(
                draft_uuid=request.draft_uuid,
                source_state_revision=request.source_state_revision,
                publication_id=str(identity.publication_id),
                target_agent_id=request.target_agent_id,
                target_revision_id=str(identity.target_revision_id),
            )
        except asyncio.CancelledError as cancelled:
            observed = await self._offload_outcome(lambda: self._find_intent(request))
            if isinstance(
                observed.error, GeneratedAgentPublicationRecoveryPendingError
            ):
                raise cancelled from observed.error
            if observed.error is not None:
                marker = GeneratedAgentPublicationRecoveryPendingError(
                    "publication intent absence could not be established"
                )
                marker.__cause__ = observed.error
                raise cancelled from marker
            if observed.error is None and observed.result is not None:
                marker = GeneratedAgentPublicationManagedCancellation(
                    "a durable publication intent owns the cancelled request"
                )
                raise cancelled from marker
            terminal = await self._terminalize_pre_intent(attempt, cancelled=True)
            if terminal.error is not None:
                raise cancelled from terminal.error
            raise cancelled
        except Exception as exc:
            observed = await self._offload_outcome(lambda: self._find_intent(request))
            if observed.cancellation is not None:
                if observed.error is not None:
                    cause = self._chain_error(observed.error, exc)
                    marker = GeneratedAgentPublicationRecoveryPendingError(
                        "publication intent absence could not be established"
                    )
                    marker.__cause__ = cause
                    raise observed.cancellation from marker
                if observed.result is not None:
                    marker = GeneratedAgentPublicationManagedCancellation(
                        "a durable publication intent owns the cancelled request"
                    )
                    marker.__cause__ = exc
                    raise observed.cancellation from marker
                terminal = await self._terminalize_pre_intent(
                    attempt,
                    cancelled=True,
                )
                cause = self._chain_error(terminal.error, exc)
                raise observed.cancellation from cause
            if isinstance(
                observed.error, GeneratedAgentPublicationRecoveryPendingError
            ):
                raise observed.error from exc
            if observed.error is not None:
                cause = self._chain_error(observed.error, exc)
                raise GeneratedAgentPublicationRecoveryPendingError(
                    "publication intent absence could not be established"
                ) from cause
            if observed.error is None and observed.result is not None:
                publication = observed.result
                if publication.state == "published":
                    terminal = await self.load_published(
                        owner_id=request.owner_id,
                        draft_uuid=request.draft_uuid,
                        source_state_revision=request.source_state_revision,
                    )
                    if terminal is not None:
                        self._assert_terminal_matches_request(terminal, request)
                        return terminal
                if publication.state == "failed":
                    raise GeneratedAgentPublicationManagedError(
                        "durable publication already failed"
                    ) from exc
                raise GeneratedAgentPublicationRecoveryPendingError(
                    "another exact durable publication attempt is in progress"
                ) from exc
            terminal = await self._terminalize_pre_intent(attempt, cancelled=False)
            if terminal.cancellation is not None:
                cause = self._chain_error(terminal.error, exc)
                raise terminal.cancellation from cause
            cause = self._chain_error(terminal.error, exc)
            raise GeneratedAgentPublicationPreIntentError(
                "generated-agent publication admission failed"
            ) from cause

        try:
            intent = await self._begin_intent(
                request=request,
                identity=identity,
                paths=paths,
                plane_fence=plane_fence,
            )
            attempt.set_publication(intent.publication)
        except asyncio.CancelledError as cancelled:
            intent = getattr(cancelled, "_generated_publication_intent", None)
            if intent is None:
                observed = await self._offload_outcome(
                    lambda: self._find_intent(request)
                )
                if isinstance(
                    observed.error, GeneratedAgentPublicationRecoveryPendingError
                ):
                    raise cancelled from observed.error
                if observed.error is not None:
                    marker = GeneratedAgentPublicationRecoveryPendingError(
                        "committed publication intent could not be read back"
                    )
                    marker.__cause__ = observed.error
                    raise cancelled from marker
                intent = observed.result if observed.error is None else None
            if intent is None:
                terminal = await self._terminalize_pre_intent(
                    attempt,
                    cancelled=True,
                )
                if terminal.error is not None:
                    raise cancelled from terminal.error
                raise cancelled
            attempt.set_publication(
                intent.publication if hasattr(intent, "publication") else intent
            )
            cleanup_error = await self._cancel_managed_attempt(
                attempt,
                GenerationClaimHeartbeat(
                    lambda: None,
                    interval_seconds=self._heartbeat_interval_seconds,
                    task_name="unused-publication-heartbeat",
                ),
            )
            marker = GeneratedAgentPublicationManagedCancellation(
                "publication intent committed before cancellation was observed"
            )
            marker.__cause__ = cleanup_error
            raise cancelled from marker
        except Exception as exc:
            observed = await self._offload_outcome(lambda: self._find_intent(request))
            if observed.cancellation is not None:
                if observed.error is not None:
                    cause = self._chain_error(observed.error, exc)
                    marker = GeneratedAgentPublicationRecoveryPendingError(
                        "committed publication intent could not be read back"
                    )
                    marker.__cause__ = cause
                    raise observed.cancellation from marker
                intent = observed.result
                if intent is None:
                    terminal = await self._terminalize_pre_intent(
                        attempt,
                        cancelled=True,
                    )
                    cause = self._chain_error(terminal.error, exc)
                    raise observed.cancellation from cause
                attempt.set_publication(
                    intent.publication if hasattr(intent, "publication") else intent
                )
                cleanup_error = await self._cancel_managed_attempt(
                    attempt,
                    GenerationClaimHeartbeat(
                        lambda: None,
                        interval_seconds=self._heartbeat_interval_seconds,
                        task_name="unused-publication-heartbeat",
                    ),
                )
                marker = GeneratedAgentPublicationManagedCancellation(
                    "publication intent committed before cancellation was observed"
                )
                marker.__cause__ = self._chain_error(cleanup_error, exc)
                raise observed.cancellation from marker
            if isinstance(
                observed.error, GeneratedAgentPublicationRecoveryPendingError
            ):
                raise observed.error from exc
            if observed.error is not None:
                cause = self._chain_error(observed.error, exc)
                raise GeneratedAgentPublicationRecoveryPendingError(
                    "committed publication intent could not be read back"
                ) from cause
            intent = observed.result if observed.error is None else None
            if intent is None:
                terminal = await self._terminalize_pre_intent(
                    attempt,
                    cancelled=False,
                )
                if terminal.cancellation is not None:
                    cause = self._chain_error(terminal.error, exc)
                    raise terminal.cancellation from cause
                cause = self._chain_error(terminal.error, exc)
                raise GeneratedAgentPublicationPreIntentError(
                    "generated-agent publication intent was not established"
                ) from cause
            attempt.set_publication(
                intent.publication if hasattr(intent, "publication") else intent
            )
            cleanup = await self._fail_managed_attempt(
                attempt,
                GenerationClaimHeartbeat(
                    lambda: None,
                    interval_seconds=self._heartbeat_interval_seconds,
                    task_name="unused-publication-heartbeat",
                ),
            )
            if cleanup.cancellation is not None:
                marker = GeneratedAgentPublicationManagedCancellation(
                    "publication failure cleanup observed caller cancellation"
                )
                marker.__cause__ = self._chain_error(cleanup.error, exc)
                raise cleanup.cancellation from marker
            if cleanup.error is not None:
                raise GeneratedAgentPublicationRecoveryPendingError(
                    "durable publication intent requires recovery"
                ) from cleanup.error
            raise GeneratedAgentPublicationManagedError(
                "publication intent failed and was terminalized"
            ) from exc

        heartbeat = GenerationClaimHeartbeat(
            lambda: self._renew_attempt(attempt),
            interval_seconds=self._heartbeat_interval_seconds,
            task_name=f"generated-agent-publication-lease:{identity.publication_id}",
        )
        heartbeat.start()
        try:
            heartbeat.assert_healthy()
            try:
                attempt.staged_receipt = await self._offload(
                    lambda: self._store.stage(
                        request.bundle,
                        key=self._bundle_key(request, identity),
                        fence_check=lambda phase: self._fence_check(attempt, phase),
                        cancellation_event=attempt.cancellation_event,
                    ),
                    cancellation_event=attempt.cancellation_event,
                )
            except asyncio.CancelledError as cancelled:
                joined_result = getattr(cancelled, "_joined_worker_result", None)
                if isinstance(joined_result, StagedBundleReceipt):
                    attempt.staged_receipt = joined_result
                raise
            heartbeat.assert_healthy()
            staged = await self._journal_transition(
                attempt,
                "mark_staged",
            )
            attempt.set_publication(staged)
            validated = await self._journal_transition(
                attempt,
                "mark_validated",
                artifact_digest=attempt.staged_receipt.bundle_sha256,
                manifest_digest=attempt.staged_receipt.manifest_sha256,
                generation_result=request.generation_result,
            )
            attempt.set_publication(validated)
            heartbeat.assert_healthy()
            try:
                attempt.published = await self._offload(
                    lambda: self._store.promote_staged(
                        attempt.staged_receipt,
                        fence_check=lambda phase: self._fence_check(attempt, phase),
                        cancellation_event=attempt.cancellation_event,
                    ),
                    cancellation_event=attempt.cancellation_event,
                )
            except asyncio.CancelledError as cancelled:
                joined_result = getattr(cancelled, "_joined_worker_result", None)
                if isinstance(joined_result, PublishedBundle):
                    attempt.published = joined_result
                if attempt.published is None:
                    raise
                raise
            except Exception as exc:
                raise GeneratedAgentPublicationRecoveryPendingError(
                    "filesystem promotion outcome requires durable recovery"
                ) from exc

            await heartbeat.close()
            committed = await self._commit_published_with_reconciliation(attempt)
            attempt.set_publication(committed)
            terminal = await self.load_published(
                owner_id=request.owner_id,
                draft_uuid=request.draft_uuid,
                source_state_revision=request.source_state_revision,
            )
            if terminal is None:
                raise GeneratedAgentPublicationRecoveryPendingError(
                    "committed publication could not be replayed"
                )
            return terminal
        except asyncio.CancelledError as cancelled:
            attempt.cancellation_event.set()
            cleanup_error = await self._cancel_managed_attempt(attempt, heartbeat)
            if cleanup_error is not None and attempt.published is not None:
                marker: GeneratedAgentPublicationError = (
                    GeneratedAgentPublicationRecoveryPendingError(
                        "published filesystem bytes require durable database recovery"
                    )
                )
                marker.__cause__ = cleanup_error
            else:
                marker = GeneratedAgentPublicationManagedCancellation(
                    "service retained claim ownership through cancellation"
                )
                if cleanup_error is not None:
                    marker.__cause__ = cleanup_error
            raise cancelled from marker
        except GeneratedAgentPublicationRecoveryPendingError as pending:
            close_outcome = await self._close_heartbeat_outcome(heartbeat)
            terminal_outcome = await self._terminalize_recovery_pending(attempt)
            cleanup = self._merge_worker_outcomes(
                close_outcome,
                terminal_outcome,
            )
            if cleanup.cancellation is not None:
                pending.__cause__ = self._chain_error(cleanup.error, pending.__cause__)
                raise cleanup.cancellation from pending
            if cleanup.error is not None:
                raise pending from cleanup.error
            raise
        except Exception as exc:
            cleanup = await self._fail_managed_attempt(attempt, heartbeat)
            if cleanup.cancellation is not None:
                marker = GeneratedAgentPublicationManagedCancellation(
                    "publication failure cleanup observed caller cancellation"
                )
                marker.__cause__ = self._chain_error(cleanup.error, exc)
                raise cleanup.cancellation from marker
            if cleanup.error is not None:
                raise GeneratedAgentPublicationRecoveryPendingError(
                    "publication failure could not be terminalized"
                ) from cleanup.error
            raise GeneratedAgentPublicationManagedError(
                "generated-agent publication failed and was terminalized"
            ) from exc

    async def _admit_and_claim(
        self,
        *,
        binding: Any,
        owner_id: str,
        submission_id: uuid.UUID,
        request_generation: uuid.UUID,
        attempt: _PublicationAttempt | None = None,
    ) -> ExecutionFence:
        owner = OperationOwner(OwnerScope.USER, owner_id, None)
        request = OperationRequest(
            operation_kind=binding.operation_kind,
            admission_class=AdmissionClass.SYSTEM,
            owner=owner,
            submission_id=submission_id,
            idempotency_namespace=binding.idempotency_namespace,
            idempotency_key=binding.idempotency_key,
            normalized_input_digest=binding.normalized_input_digest,
            chat_id=None,
            parent_operation_id=binding.parent_operation_id,
            connection_generation=None,
            request_generation=request_generation,
        )
        admitted = await self._offload(lambda: self._admission.submit(request))
        if isinstance(admitted, RefusedAdmission) or not isinstance(
            admitted, AcceptedAdmission
        ):
            code = (
                admitted.code
                if isinstance(admitted, RefusedAdmission)
                else "invalid_result"
            )
            raise GeneratedAgentPublicationPreIntentError(
                f"publication admission refused: {code}"
            )
        persisted = await self._offload(
            lambda: self._load_persisted_admission(
                request=request,
                expected_operation_id=admitted.operation_id,
            )
        )
        self._assert_operation_identity(
            persisted,
            request=request,
            expected_operation_id=admitted.operation_id,
            stage="persisted",
        )
        try:
            claim = await self._offload(
                lambda: self._admission.claim_operation(
                    AdmissionClass.SYSTEM,
                    admitted.operation_id,
                )
            )
        except asyncio.CancelledError as cancelled:
            claimed = getattr(cancelled, "_joined_worker_result", None)
            if claimed is not None:
                try:
                    self._assert_claim_identity(
                        claimed,
                        request=request,
                        expected_operation_id=admitted.operation_id,
                    )
                except GeneratedAgentPublicationPreIntentError as identity_error:
                    raise cancelled from identity_error
                if attempt is not None:
                    attempt.deep_fence = claimed.fence
            raise
        except Exception as claim_error:
            try:
                claim = await self._offload(
                    lambda: self._reselect_uncertain_claim(
                        request=request,
                        expected_operation_id=admitted.operation_id,
                    )
                )
            except asyncio.CancelledError as cancelled:
                recovered = getattr(cancelled, "_joined_worker_result", None)
                recovery_error = getattr(cancelled, "_joined_worker_error", None)
                cause = self._chain_error(recovery_error, claim_error)
                if recovered is not None:
                    try:
                        self._assert_claim_identity(
                            recovered,
                            request=request,
                            expected_operation_id=admitted.operation_id,
                        )
                    except GeneratedAgentPublicationPreIntentError as identity_error:
                        cause = self._chain_error(identity_error, cause)
                    else:
                        if attempt is not None:
                            attempt.deep_fence = recovered.fence
                raise cancelled from cause
            except Exception as recovery_error:
                cause = self._chain_error(recovery_error, claim_error)
                raise GeneratedAgentPublicationPreIntentError(
                    "publication operation claim outcome could not be "
                    "reconciled exactly"
                ) from cause
        if claim is None:
            raise GeneratedAgentPublicationPreIntentError(
                "publication operation was not available for exact handoff"
            )
        self._assert_claim_identity(
            claim,
            request=request,
            expected_operation_id=admitted.operation_id,
        )
        if attempt is not None:
            attempt.deep_fence = claim.fence
        return claim.fence

    def _load_persisted_admission(
        self,
        *,
        request: OperationRequest,
        expected_operation_id: uuid.UUID,
    ) -> OperationRecord:
        submission = self._admission.reconcile_submission(
            owner=request.owner,
            submission_id=request.submission_id,
        )
        if (
            not isinstance(submission, AcceptedSubmission)
            or submission.operation.operation_id != expected_operation_id
        ):
            raise GeneratedAgentPublicationPreIntentError(
                "publication persisted submission identity did not match the "
                "exact request"
            )
        operation = self._admission.repository.get_operation_for_administration(
            expected_operation_id
        )
        if not isinstance(operation, OperationRecord):
            raise GeneratedAgentPublicationPreIntentError(
                "publication persisted operation was unavailable for exact "
                "identity validation"
            )
        return operation

    def _reselect_uncertain_claim(
        self,
        *,
        request: OperationRequest,
        expected_operation_id: uuid.UUID,
    ) -> OperationClaim:
        operation = self._admission.repository.get_operation_for_administration(
            expected_operation_id
        )
        if not isinstance(operation, OperationRecord):
            raise GeneratedAgentPublicationPreIntentError(
                "publication uncertain claim operation was unavailable"
            )
        self._assert_operation_identity(
            operation,
            request=request,
            expected_operation_id=expected_operation_id,
            stage="uncertain claim",
        )
        if not (
            operation.state is OperationState.RUNNING
            and type(operation.execution_generation) is int
            and operation.execution_generation > 0
            and isinstance(operation.execution_lease_token, uuid.UUID)
        ):
            raise GeneratedAgentPublicationPreIntentError(
                "publication uncertain claim was not an exact running execution"
            )
        prior_fence = ExecutionFence(
            operation_id=operation.operation_id,
            execution_generation=operation.execution_generation,
            execution_lease_token=operation.execution_lease_token,
        )
        selected_fence = self._admission.reselect_execution(prior_fence)
        if not (
            isinstance(selected_fence, ExecutionFence)
            and selected_fence.operation_id == expected_operation_id
            and type(selected_fence.execution_generation) is int
            and selected_fence.execution_generation
            == prior_fence.execution_generation + 1
            and isinstance(selected_fence.execution_lease_token, uuid.UUID)
            and selected_fence.execution_lease_token
            != prior_fence.execution_lease_token
        ):
            raise GeneratedAgentPublicationPreIntentError(
                "publication uncertain claim reselection returned an invalid fence"
            )
        selected = self._admission.repository.get_operation_for_administration(
            expected_operation_id
        )
        if not isinstance(selected, OperationRecord):
            raise GeneratedAgentPublicationPreIntentError(
                "publication reselected operation was unavailable"
            )
        self._assert_operation_identity(
            selected,
            request=request,
            expected_operation_id=expected_operation_id,
            stage="reselected",
        )
        if not (
            selected.state is OperationState.RUNNING
            and selected.execution_generation == selected_fence.execution_generation
            and selected.execution_lease_token == selected_fence.execution_lease_token
        ):
            raise GeneratedAgentPublicationPreIntentError(
                "publication reselected operation did not retain the exact fence"
            )
        return OperationClaim(operation=selected, fence=selected_fence)

    @staticmethod
    def _assert_operation_identity(
        operation: OperationRecord,
        *,
        request: OperationRequest,
        expected_operation_id: uuid.UUID,
        stage: str,
    ) -> None:
        expected = {
            "operation_id": expected_operation_id,
            "operation_kind": request.operation_kind,
            "admission_class": request.admission_class,
            "owner_scope": request.owner.owner_scope,
            "owner_user_id": request.owner.owner_user_id,
            "connection_scope_id": request.owner.connection_scope_id,
            "idempotency_namespace": request.idempotency_namespace,
            "idempotency_key": request.idempotency_key,
            "normalized_input_digest": request.normalized_input_digest,
            "chat_id": request.chat_id,
            "parent_operation_id": request.parent_operation_id,
            "connection_generation": request.connection_generation,
            "request_generation": request.request_generation,
        }
        if any(getattr(operation, field_name, object()) != value for field_name, value in expected.items()):
            raise GeneratedAgentPublicationPreIntentError(
                f"publication {stage} operation identity did not match the "
                "exact request"
            )

    @classmethod
    def _assert_claim_identity(
        cls,
        claim: Any,
        *,
        request: OperationRequest,
        expected_operation_id: uuid.UUID,
    ) -> None:
        if not isinstance(claim, OperationClaim):
            raise GeneratedAgentPublicationPreIntentError(
                "publication claimed operation was invalid"
            )
        cls._assert_operation_identity(
            claim.operation,
            request=request,
            expected_operation_id=expected_operation_id,
            stage="claimed",
        )
        if (
            claim.fence.operation_id != claim.operation.operation_id
            or claim.fence.execution_generation
            != claim.operation.execution_generation
            or claim.fence.execution_lease_token
            != claim.operation.execution_lease_token
        ):
            raise GeneratedAgentPublicationPreIntentError(
                "publication claimed operation fence did not match the exact "
                "operation"
            )

    async def _begin_intent(
        self,
        *,
        request: GeneratedAgentPublicationRequest,
        identity: GeneratedAgentPublicationIdentity,
        paths: Any,
        plane_fence: PlaneExecutionFence,
    ) -> Any:
        def begin() -> Any:
            with self._runtime.transaction() as transaction:
                return self._journal.begin_intent(
                    transaction,
                    owner_id=request.owner_id,
                    publication_id=str(identity.publication_id),
                    draft_uuid=request.draft_uuid,
                    source_state_revision=request.source_state_revision,
                    generation_claim_id=request.generation_claim_id,
                    target_agent_id=request.target_agent_id,
                    target_revision_id=str(identity.target_revision_id),
                    staging_relative_path=paths.staging_relative_path,
                    revision_relative_path=paths.revision_relative_path,
                    bundle=request.bundle,
                    runtime_contract_version=request.runtime_contract_version,
                    release_lock_digest=request.release_lock_digest,
                    promotion_token=str(identity.promotion_token),
                    attempt=plane_fence,
                    compatibility_state=request.compatibility_state,
                )

        try:
            return await self._offload(begin)
        except asyncio.CancelledError as cancelled:
            result = getattr(cancelled, "_joined_worker_result", None)
            if result is not None:
                # A committed begin owns the claim even when its awaiter was cancelled.
                setattr(cancelled, "_generated_publication_intent", result)
            raise

    async def _journal_transition(
        self,
        attempt: _PublicationAttempt,
        method_name: str,
        **kwargs: Any,
    ) -> Any:
        try:
            return await self._offload(
                lambda: self._journal_transition_sync(
                    attempt,
                    method_name,
                    **kwargs,
                )
            )
        except asyncio.CancelledError as cancelled:
            result = getattr(cancelled, "_joined_worker_result", None)
            if result is not None:
                attempt.set_publication(result)
            raise

    async def _commit_published_with_reconciliation(
        self,
        attempt: _PublicationAttempt,
        *,
        generation_result: GeneratedAgentPublicationResultMetadata | None = None,
    ) -> Any:
        try:
            return await self._offload(
                lambda: self._journal_terminal_transition_sync(
                    attempt,
                    "commit_published",
                    operation_state=OperationState.COMPLETED,
                    operation_terminal_code=None,
                    operation_safe_summary=None,
                    operation_retry_after_ms=None,
                    generation_result=generation_result,
                )
            )
        except asyncio.CancelledError as cancelled:
            refreshed = await self._offload_outcome(
                lambda: self._refresh_terminal_publication_sync(
                    attempt,
                    publication_state="published",
                    operation_state=OperationState.COMPLETED,
                    operation_terminal_code=None,
                    operation_safe_summary=None,
                    operation_retry_after_ms=None,
                )
            )
            cause = self._chain_error(refreshed.error, cancelled.__cause__)
            raise cancelled from cause
        except Exception as exc:
            refreshed = await self._offload_outcome(
                lambda: self._refresh_terminal_publication_sync(
                    attempt,
                    publication_state="published",
                    operation_state=OperationState.COMPLETED,
                    operation_terminal_code=None,
                    operation_safe_summary=None,
                    operation_retry_after_ms=None,
                )
            )
            if refreshed.cancellation is not None:
                cause = self._chain_error(refreshed.error, exc)
                raise refreshed.cancellation from cause
            if refreshed.error is not None:
                raise exc from refreshed.error
            if refreshed.result.state == "published":
                return refreshed.result
            raise

    async def _fail_publication_with_reconciliation(
        self,
        attempt: _PublicationAttempt,
        *,
        failure_code: str,
        safe_error_message: str,
        operation_state: OperationState,
    ) -> _WorkerOutcome:
        failed = await self._offload_outcome(
            lambda: self._journal_terminal_transition_sync(
                attempt,
                "fail",
                operation_state=operation_state,
                operation_terminal_code=failure_code,
                operation_safe_summary=safe_error_message,
                operation_retry_after_ms=None,
                failure_code=failure_code,
                safe_error_message=safe_error_message,
            )
        )
        if failed.error is None:
            return failed
        refreshed = await self._offload_outcome(
            lambda: self._refresh_terminal_publication_sync(
                attempt,
                publication_state="failed",
                operation_state=operation_state,
                operation_terminal_code=failure_code,
                operation_safe_summary=safe_error_message,
                operation_retry_after_ms=None,
            )
        )
        cancellation = failed.cancellation or refreshed.cancellation
        if refreshed.error is None and refreshed.result.state == "failed":
            return _WorkerOutcome(
                result=refreshed.result,
                cancellation=cancellation,
            )
        return self._merge_worker_outcomes(failed, refreshed)

    def _renew_attempt(self, attempt: _PublicationAttempt) -> Any:
        with attempt.io_lock:
            deep_fence = attempt.deep_fence
            if deep_fence is None:
                raise GenerationClaimLostError("operation fence is unavailable")
            self._admission.renew_execution_lease(deep_fence)
            expected, plane_fence = attempt.snapshot()
            with self._runtime.transaction() as transaction:
                return self._journal.renew_generation_claim(
                    transaction,
                    expected=expected,
                    attempt=plane_fence,
                    lease_seconds=self._claim_lease_seconds,
                )

    def _fence_check(self, attempt: _PublicationAttempt, _phase: str) -> None:
        with attempt.io_lock:
            expected, plane_fence = attempt.snapshot()
            with self._runtime.transaction() as transaction:
                self._journal.assert_current_attempt(
                    transaction,
                    expected=expected,
                    attempt=plane_fence,
                )

    async def _cancel_managed_attempt(
        self,
        attempt: _PublicationAttempt,
        heartbeat: GenerationClaimHeartbeat,
    ) -> BaseException | None:
        cleanup = await self._close_heartbeat_outcome(heartbeat)
        if attempt.published is not None:
            current, _fence = attempt.snapshot()
            if current.state != "published":
                commit_task = asyncio.create_task(
                    self._commit_published_with_reconciliation(attempt)
                )
                outcome = await self._join_task(commit_task)
                cleanup = self._merge_worker_outcomes(cleanup, outcome)
                if outcome.error is not None:
                    pending = await self._terminalize_recovery_pending(attempt)
                    cleanup = self._merge_worker_outcomes(
                        cleanup,
                        pending,
                    )
                    return self._outcome_primary_error(cleanup)
                attempt.set_publication(outcome.result)
            else:
                terminal = await self._offload_outcome(
                    lambda: self._refresh_terminal_publication_sync(
                        attempt,
                        publication_state="published",
                        operation_state=OperationState.COMPLETED,
                        operation_terminal_code=None,
                        operation_safe_summary=None,
                        operation_retry_after_ms=None,
                    )
                )
                cleanup = self._merge_worker_outcomes(cleanup, terminal)
                if terminal.error is not None:
                    return self._outcome_primary_error(cleanup)
                attempt.set_publication(terminal.result)
            return self._outcome_primary_error(cleanup)
        if attempt.staged_receipt is not None:
            quarantine = await self._offload_outcome(
                lambda: self._store.quarantine_staged(attempt.staged_receipt)
            )
            cleanup = self._merge_worker_outcomes(cleanup, quarantine)
            if quarantine.error is not None:
                pending = await self._terminalize_recovery_pending(attempt)
                cleanup = self._merge_worker_outcomes(
                    cleanup,
                    pending,
                )
                return self._outcome_primary_error(cleanup)
        failed = await self._fail_publication_with_reconciliation(
            attempt,
            failure_code="caller_cancelled",
            safe_error_message=_CANCELLATION_SUMMARY,
            operation_state=OperationState.CANCELLED,
        )
        cleanup = self._merge_worker_outcomes(cleanup, failed)
        if failed.error is not None:
            pending = await self._terminalize_recovery_pending(attempt)
            cleanup = self._merge_worker_outcomes(cleanup, pending)
            return self._outcome_primary_error(cleanup)
        attempt.set_publication(failed.result)
        return self._outcome_primary_error(cleanup)

    async def _fail_managed_attempt(
        self,
        attempt: _PublicationAttempt,
        heartbeat: GenerationClaimHeartbeat,
    ) -> _WorkerOutcome:
        heartbeat_outcome = await self._close_heartbeat_outcome(heartbeat)
        cleanup = _WorkerOutcome(
            error=heartbeat_outcome.error
            if heartbeat_outcome.cancellation is not None
            else None,
            cancellation=heartbeat_outcome.cancellation,
        )
        if attempt.published is not None:
            pending = await self._terminalize_recovery_pending(attempt)
            cleanup = self._merge_worker_outcomes(cleanup, pending)
            return self._merge_worker_outcomes(
                cleanup,
                _WorkerOutcome(
                    error=GeneratedAgentPublicationRecoveryPendingError(
                        "published bytes require database recovery"
                    )
                ),
            )
        if attempt.staged_receipt is not None:
            quarantine = await self._offload_outcome(
                lambda: self._store.quarantine_staged(attempt.staged_receipt)
            )
            cleanup = self._merge_worker_outcomes(cleanup, quarantine)
            if quarantine.error is not None:
                pending = await self._terminalize_recovery_pending(attempt)
                return self._merge_worker_outcomes(cleanup, pending)
        failed = await self._fail_publication_with_reconciliation(
            attempt,
            failure_code="publication_failed",
            safe_error_message=_FAILURE_SUMMARY,
            operation_state=OperationState.FAILED,
        )
        cleanup = self._merge_worker_outcomes(cleanup, failed)
        if failed.error is not None:
            pending = await self._terminalize_recovery_pending(attempt)
            return self._merge_worker_outcomes(cleanup, pending)
        attempt.set_publication(failed.result)
        return cleanup

    async def _terminalize_recovery_pending(
        self,
        attempt: _PublicationAttempt,
    ) -> _WorkerOutcome:
        return await self._offload_outcome(
            lambda: self._terminalize_operation_sync(
                attempt,
                state=OperationState.RETRYABLE,
                terminal_code="publication_recovery_pending",
                safe_summary="Generated-agent publication recovery is pending.",
                retry_after_ms=1_000,
            )
        )

    async def _terminalize_recovery_cancellation(
        self,
        attempt: _PublicationAttempt,
    ) -> _WorkerOutcome:
        publication, _fence = attempt.snapshot()
        if publication.state == "published":
            return await self._offload_outcome(
                lambda: self._terminalize_operation_sync(
                    attempt,
                    state=OperationState.COMPLETED,
                    terminal_code=None,
                    safe_summary=None,
                    retry_after_ms=None,
                )
            )
        if publication.state == "failed":
            return await self._offload_outcome(
                lambda: self._terminalize_operation_sync(
                    attempt,
                    state=OperationState.FAILED,
                    terminal_code="publication_recovery_failed",
                    safe_summary=_FAILURE_SUMMARY,
                    retry_after_ms=None,
                )
            )
        return await self._terminalize_recovery_pending(attempt)

    async def _terminalize_pre_intent(
        self,
        attempt: _PublicationAttempt,
        *,
        cancelled: bool,
    ) -> _WorkerOutcome:
        if attempt.deep_fence is None:
            return _WorkerOutcome()
        return await self._offload_outcome(
            lambda: self._terminalize_operation_sync(
                attempt,
                state=OperationState.CANCELLED if cancelled else OperationState.FAILED,
                terminal_code="caller_cancelled"
                if cancelled
                else "publication_intent_failed",
                safe_summary=_CANCELLATION_SUMMARY if cancelled else _FAILURE_SUMMARY,
                retry_after_ms=None,
            )
        )

    async def _terminalize_operation(
        self,
        attempt: _PublicationAttempt,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str | None,
        retry_after_ms: int | None,
    ) -> Any:
        return await self._offload(
            lambda: self._terminalize_operation_sync(
                attempt,
                state=state,
                terminal_code=terminal_code,
                safe_summary=safe_summary,
                retry_after_ms=retry_after_ms,
            )
        )

    def _terminalize_operation_sync(
        self,
        attempt: _PublicationAttempt,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str | None,
        retry_after_ms: int | None,
    ) -> Any:
        if attempt.deep_fence is None:
            return None
        return self._admission.terminalize(
            attempt.deep_fence,
            state=state,
            terminal_code=terminal_code,
            safe_summary=safe_summary,
            retry_after_ms=retry_after_ms,
        )

    def _journal_transition_sync(
        self,
        attempt: _PublicationAttempt,
        method_name: str,
        **kwargs: Any,
    ) -> Any:
        with attempt.io_lock:
            expected, plane_fence = attempt.snapshot()
            with self._runtime.transaction() as transaction:
                result = getattr(self._journal, method_name)(
                    transaction,
                    expected=expected,
                    attempt=plane_fence,
                    **kwargs,
                )
            attempt.set_publication(result)
            return result

    def _journal_terminal_transition_sync(
        self,
        attempt: _PublicationAttempt,
        method_name: str,
        *,
        operation_state: OperationState,
        operation_terminal_code: str | None,
        operation_safe_summary: str | None,
        operation_retry_after_ms: int | None,
        **kwargs: Any,
    ) -> Any:
        """Commit the journal and its Deep operation in one Plane transaction."""

        if attempt.deep_fence is None:
            raise GeneratedAgentPublicationRecoveryPendingError(
                "terminal publication transition has no exact operation fence"
            )
        with attempt.io_lock:
            expected, plane_fence = attempt.snapshot()
            with self._runtime.transaction() as transaction:
                result = getattr(self._journal, method_name)(
                    transaction,
                    expected=expected,
                    attempt=plane_fence,
                    **kwargs,
                )
                operation = self._admission.terminalize(
                    attempt.deep_fence,
                    state=operation_state,
                    terminal_code=operation_terminal_code,
                    safe_summary=operation_safe_summary,
                    retry_after_ms=operation_retry_after_ms,
                    transaction=transaction,
                )
                self._assert_terminal_operation(
                    operation,
                    attempt.deep_fence,
                    state=operation_state,
                    terminal_code=operation_terminal_code,
                    safe_summary=operation_safe_summary,
                    retry_after_ms=operation_retry_after_ms,
                )
            attempt.set_publication(result)
            return result

    def _refresh_terminal_publication_sync(
        self,
        attempt: _PublicationAttempt,
        *,
        publication_state: str,
        operation_state: OperationState,
        operation_terminal_code: str | None,
        operation_safe_summary: str | None,
        operation_retry_after_ms: int | None,
    ) -> Any:
        """Reconcile a terminal transaction whose acknowledgement was ambiguous."""

        if attempt.deep_fence is None:
            raise GeneratedAgentPublicationRecoveryPendingError(
                "terminal publication reconciliation has no exact operation fence"
            )
        with attempt.io_lock:
            expected, _fence = attempt.snapshot()
            with self._runtime.transaction() as transaction:
                current = self._journal.get_by_source(
                    transaction,
                    owner_id=expected.owner_id,
                    draft_uuid=expected.draft_uuid,
                    source_state_revision=expected.source_state_revision,
                )
                self._assert_same_publication(current, expected)
                if current.state == publication_state:
                    if (
                        current.operation_id == str(attempt.deep_fence.operation_id)
                        and current.operation_execution_generation
                        == attempt.deep_fence.execution_generation
                    ):
                        operation = self._admission.terminalize(
                            attempt.deep_fence,
                            state=operation_state,
                            terminal_code=operation_terminal_code,
                            safe_summary=operation_safe_summary,
                            retry_after_ms=operation_retry_after_ms,
                            transaction=transaction,
                        )
                        self._assert_terminal_operation(
                            operation,
                            attempt.deep_fence,
                            state=operation_state,
                            terminal_code=operation_terminal_code,
                            safe_summary=operation_safe_summary,
                            retry_after_ms=operation_retry_after_ms,
                        )
                    else:
                        self._assert_successor_terminal_operation(
                            transaction,
                            publication=current,
                        )
            attempt.set_publication(current)
            return current

    @staticmethod
    def _assert_same_publication(current: Any | None, expected: Any) -> None:
        if current is None or (
            current.publication_id != expected.publication_id
            or current.generation_claim_id != expected.generation_claim_id
            or current.target_agent_id != expected.target_agent_id
            or current.target_revision_id != expected.target_revision_id
        ):
            raise GeneratedAgentPublicationRecoveryPendingError(
                "publication commit outcome could not be reconciled exactly"
            )

    @staticmethod
    def _assert_terminal_operation(
        operation: Any,
        fence: ExecutionFence,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str | None,
        retry_after_ms: int | None,
    ) -> None:
        if (
            operation is None
            or operation.operation_id != fence.operation_id
            or operation.execution_generation != fence.execution_generation
            or operation.state is not state
            or operation.terminal_code != terminal_code
            or operation.safe_summary != safe_summary
            or operation.retry_after_ms != retry_after_ms
        ):
            raise GeneratedAgentPublicationRecoveryPendingError(
                "terminal publication operation could not be reconciled exactly"
            )

    def _assert_successor_terminal_operation(
        self,
        transaction: Any,
        *,
        publication: Any,
    ) -> None:
        try:
            operation_id = uuid.UUID(publication.operation_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise GeneratedAgentPublicationRecoveryPendingError(
                "terminal publication successor operation identity is invalid"
            ) from exc
        operation = self._admission.repository.get_operation_for_administration(
            operation_id,
            for_update=True,
            transaction=transaction,
        )
        expected_state = (
            OperationState.COMPLETED
            if publication.state == "published"
            else OperationState.CANCELLED
            if publication.failure_code == "caller_cancelled"
            else OperationState.FAILED
        )
        expected_code = None if publication.state == "published" else publication.failure_code
        if (
            operation is None
            or operation.operation_id != operation_id
            or operation.execution_generation
            != publication.operation_execution_generation
            or operation.state is not expected_state
            or operation.terminal_code != expected_code
        ):
            raise GeneratedAgentPublicationRecoveryPendingError(
                "terminal successor publication operation is not authoritative"
            )

    async def _recover_publication(self, publication: Any) -> str:
        revision, draft = await self._offload(
            lambda: self._load_recovery_records(publication)
        )
        binding = generated_agent_publication_recovery_operation_binding(
            publication, revision
        )
        submission_id, request_generation = self._recovery_identities(binding)
        attempt = _PublicationAttempt(request=None, publication=publication)
        try:
            deep_fence = await self._admit_and_claim(
                binding=binding,
                owner_id=publication.owner_id,
                submission_id=submission_id,
                request_generation=request_generation,
                attempt=attempt,
            )
        except asyncio.CancelledError as cancelled:
            terminal = await self._terminalize_pre_intent(attempt, cancelled=True)
            cause = self._chain_error(terminal.error, cancelled.__cause__)
            raise cancelled from cause
        except GeneratedAgentPublicationPreIntentError:
            return "degraded"
        plane_fence = self._to_plane_fence(deep_fence)
        attempt.deep_fence = deep_fence
        attempt.plane_fence = plane_fence

        def rebind() -> Any:
            with self._runtime.transaction() as transaction:
                return self._journal.rebind_recovery_attempt(
                    transaction,
                    expected=publication,
                    new_attempt=plane_fence,
                    lease_seconds=self._claim_lease_seconds,
                )

        try:
            rebound = await self._offload(rebind)
        except asyncio.CancelledError as cancelled:
            rebound = getattr(cancelled, "_joined_worker_result", None)
            if rebound is None:
                terminal = await self._terminalize_pre_intent(
                    attempt,
                    cancelled=True,
                )
            else:
                attempt.set_publication(rebound)
                terminal = await self._terminalize_recovery_cancellation(attempt)
            cause = self._chain_error(terminal.error, cancelled.__cause__)
            raise cancelled from cause
        except Exception as exc:
            terminal = await self._terminalize_pre_intent(attempt, cancelled=False)
            if terminal.cancellation is not None:
                cause = self._chain_error(terminal.error, exc)
                raise terminal.cancellation from cause
            if terminal.error is not None:
                raise exc from terminal.error
            raise
        attempt.set_publication(rebound)
        heartbeat = GenerationClaimHeartbeat(
            lambda: self._renew_attempt(attempt),
            interval_seconds=self._heartbeat_interval_seconds,
            task_name=f"generated-agent-recovery-lease:{rebound.publication_id}",
        )
        with self._state_lock:
            self._recovery_attempts[rebound.publication_id] = attempt
        heartbeat.start()
        try:
            return await self._recover_bound_publication(
                attempt=attempt,
                rebound=rebound,
                revision=revision,
                draft=draft,
                heartbeat=heartbeat,
            )
        except asyncio.CancelledError as cancelled:
            attempt.cancellation_event.set()
            close_outcome = await self._close_heartbeat_outcome(heartbeat)
            pending_outcome = await self._terminalize_recovery_cancellation(attempt)
            cleanup = self._merge_worker_outcomes(close_outcome, pending_outcome)
            cause = self._chain_error(cleanup.error, cancelled.__cause__)
            raise cancelled from cause
        except Exception as exc:
            close_outcome = await self._close_heartbeat_outcome(heartbeat)
            pending_outcome = await self._terminalize_recovery_pending(attempt)
            cleanup = self._merge_worker_outcomes(close_outcome, pending_outcome)
            if cleanup.cancellation is not None:
                cause = self._chain_error(cleanup.error, exc)
                raise cleanup.cancellation from cause
            if cleanup.error is not None:
                raise exc from cleanup.error
            raise
        finally:
            final_close = await self._close_heartbeat_outcome(heartbeat)
            with self._state_lock:
                if self._recovery_attempts.get(rebound.publication_id) is attempt:
                    self._recovery_attempts.pop(rebound.publication_id, None)
            self._raise_outcome_cancellation(final_close)

    async def _recover_bound_publication(
        self,
        *,
        attempt: _PublicationAttempt,
        rebound: Any,
        revision: Any,
        draft: Any,
        heartbeat: GenerationClaimHeartbeat,
    ) -> str:
        key = BundlePublicationKey(
            scope_id=rebound.target_agent_id,
            staging_id=rebound.draft_uuid,
            source_revision=rebound.source_state_revision,
            publication_id=rebound.publication_id,
            revision_id=rebound.target_revision_id,
        )
        expected_metadata = runtime_metadata_for_manifest(
            GENERATED_AGENT_BUNDLE_CONTRACT,
            revision.manifest,
        )
        heartbeat.assert_healthy()
        recovery = await self._offload(
            lambda: self._store.recover(
                key=key,
                expected_bundle_sha256=revision.artifact_digest,
                expected_manifest_sha256=self._manifest_digest(rebound, revision),
                expected_runtime_metadata=expected_metadata,
                fence_check=lambda phase: self._fence_check(attempt, phase),
                cancellation_event=attempt.cancellation_event,
            ),
            cancellation_event=attempt.cancellation_event,
        )
        if recovery.disposition in {
            BundleRecoveryDisposition.FOREIGN,
            BundleRecoveryDisposition.COLLISION,
        }:
            await self._close_heartbeat_safely(heartbeat)
            pending = await self._terminalize_recovery_pending(attempt)
            self._raise_outcome_cancellation(pending)
            return "degraded"

        if rebound.state != "validated":
            if (
                recovery.published is not None
                and recovery.published.receipt is not None
            ):
                quarantined = await self._offload_outcome(
                    lambda: self._store.quarantine_receipt(recovery.published.receipt)
                )
                self._raise_outcome_cancellation(quarantined)
                if quarantined.error is not None:
                    await self._close_heartbeat_safely(heartbeat)
                    pending = await self._terminalize_recovery_pending(attempt)
                    self._raise_outcome_cancellation(pending)
                    return "degraded"
            failed = await self._fail_publication_with_reconciliation(
                attempt,
                failure_code="incomplete_publication_recovery",
                safe_error_message=_FAILURE_SUMMARY,
                operation_state=OperationState.FAILED,
            )
            self._raise_outcome_cancellation(failed)
            if failed.error is not None:
                await self._close_heartbeat_safely(heartbeat)
                pending = await self._terminalize_recovery_pending(attempt)
                self._raise_outcome_cancellation(pending)
                return "degraded"
            attempt.set_publication(failed.result)
            await self._close_heartbeat_safely(heartbeat)
            return "failed"

        if (
            recovery.disposition
            in {
                BundleRecoveryDisposition.FINAL_VALID,
                BundleRecoveryDisposition.STAGING_PROMOTED,
            }
            and recovery.published is not None
        ):
            await self._close_heartbeat_safely(heartbeat)
            committed = await self._commit_published_with_reconciliation(
                attempt,
                generation_result=self._generation_result_from_draft(draft),
            )
            attempt.set_publication(committed)
            return "recovered"

        failed = await self._fail_publication_with_reconciliation(
            attempt,
            failure_code="publication_bytes_unrecoverable",
            safe_error_message=_FAILURE_SUMMARY,
            operation_state=OperationState.FAILED,
        )
        self._raise_outcome_cancellation(failed)
        if failed.error is not None:
            await self._close_heartbeat_safely(heartbeat)
            pending = await self._terminalize_recovery_pending(attempt)
            self._raise_outcome_cancellation(pending)
            return "degraded"
        attempt.set_publication(failed.result)
        await self._close_heartbeat_safely(heartbeat)
        return "failed"

    def _load_recovery_records(self, publication: Any) -> tuple[Any, Any]:
        with self._runtime.transaction() as transaction:
            revision = self._agents.get_revision(
                transaction,
                owner_id=publication.owner_id,
                agent_id=publication.target_agent_id,
                revision_id=publication.target_revision_id,
            )
            draft = self._drafts.get_draft_by_uuid(
                transaction,
                owner_id=publication.owner_id,
                draft_uuid=publication.draft_uuid,
            )
        if revision is None or draft is None or revision.manifest is None:
            raise GeneratedAgentPublicationRecoveryPendingError(
                "publication recovery provenance is incomplete"
            )
        return revision, draft

    def _list_reconcilable(
        self,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[Any, ...]:
        with self._runtime.transaction() as transaction:
            return self._journal.list_reconcilable_for_administration(
                transaction,
                limit=limit,
                after_created_at=None if after is None else after[0],
                after_publication_id=None if after is None else after[1],
            )

    @staticmethod
    def _publication_cursor(publication: Any) -> tuple[datetime, str]:
        if not isinstance(publication.created_at, datetime):
            raise GeneratedAgentPublicationRecoveryPendingError(
                "publication recovery cursor timestamp is invalid"
            )
        if (
            not isinstance(publication.publication_id, str)
            or not publication.publication_id
        ):
            raise GeneratedAgentPublicationRecoveryPendingError(
                "publication recovery cursor identity is invalid"
            )
        return publication.created_at, publication.publication_id

    @staticmethod
    def _expired_claim_cursor(claim: Any) -> tuple[datetime, str]:
        if not isinstance(claim.generation_claim_expires_at, datetime):
            raise GeneratedAgentPublicationRecoveryPendingError(
                "expired claim recovery cursor timestamp is invalid"
            )
        if not isinstance(claim.draft_id, str) or not claim.draft_id:
            raise GeneratedAgentPublicationRecoveryPendingError(
                "expired claim recovery cursor identity is invalid"
            )
        return claim.generation_claim_expires_at, claim.draft_id

    def _publication_inventory(
        self,
        *,
        limit: int,
        after: tuple[datetime, str] | None,
    ) -> _PublicationInventory:
        query_limit = min(_MAX_INVENTORY_PAGE, limit + 1)
        publications = self._list_reconcilable(
            limit=query_limit,
            after=after,
        )
        selected = publications[:limit]
        has_more = len(publications) > limit
        next_cursor = self._publication_cursor(selected[-1]) if selected else None
        if not has_more and limit == _MAX_INVENTORY_PAGE and next_cursor is not None:
            has_more = bool(self._list_reconcilable(limit=1, after=next_cursor))
        return _PublicationInventory(selected, next_cursor, has_more)

    def _expired_claim_inventory(
        self,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> _ExpiredClaimInventory:
        if limit <= 0:
            return _ExpiredClaimInventory((), 0)
        query_limit = min(_MAX_INVENTORY_PAGE, limit + 1)
        with self._runtime.transaction() as transaction:
            expired = self._drafts.list_expired_generation_claims_for_administration(
                transaction,
                limit=query_limit,
                after_generation_claim_expires_at=None if after is None else after[0],
                after_draft_id=None if after is None else after[1],
            )
            selected = expired[:limit]
            orphaned = tuple(
                claim
                for claim in selected
                if self._journal.get_by_source(
                    transaction,
                    owner_id=claim.owner_id,
                    draft_uuid=claim.draft_uuid,
                    source_state_revision=claim.state_revision,
                )
                is None
            )
            next_cursor = self._expired_claim_cursor(selected[-1]) if selected else None
            has_more = len(expired) > limit
            if (
                not has_more
                and limit == _MAX_INVENTORY_PAGE
                and next_cursor is not None
            ):
                probe = self._drafts.list_expired_generation_claims_for_administration(
                    transaction,
                    limit=1,
                    after_generation_claim_expires_at=next_cursor[0],
                    after_draft_id=next_cursor[1],
                )
                has_more = bool(probe)
            return _ExpiredClaimInventory(
                orphaned,
                len(selected),
                next_cursor,
                has_more,
            )

    def _next_journal_recovery_page(self) -> _PublicationInventory:
        with self._state_lock:
            after = self._journal_recovery_cursor
        inventory = self._publication_inventory(
            limit=self._recovery_batch_size,
            after=after,
        )
        if not inventory.publications and after is not None:
            inventory = self._publication_inventory(
                limit=self._recovery_batch_size,
                after=None,
            )
        with self._state_lock:
            self._journal_recovery_cursor = inventory.next_cursor
        return inventory

    def _next_expired_claim_recovery_page(self) -> _ExpiredClaimInventory:
        with self._state_lock:
            after = self._expired_claim_recovery_cursor
        inventory = self._expired_claim_inventory(
            limit=self._recovery_batch_size,
            after=after,
        )
        if inventory.raw_count == 0 and after is not None:
            inventory = self._expired_claim_inventory(
                limit=self._recovery_batch_size,
                after=None,
            )
        with self._state_lock:
            self._expired_claim_recovery_cursor = inventory.next_cursor
        return inventory

    def _terminalize_expired_claim(self, claim: Any) -> Any:
        claim_id = claim.generation_claim_id
        if claim_id is None:
            raise GenerationClaimLostError(
                "expired generation claim identity is missing"
            )
        with self._runtime.transaction() as transaction:
            reclaimed = self._drafts.reclaim_expired_generation_claim(
                transaction,
                owner_id=claim.owner_id,
                draft_id=claim.draft_id,
                expected_revision=claim.state_revision,
                claim_id=claim_id,
                lease_seconds=self._claim_lease_seconds,
            )
            publication = self._journal.get_by_source(
                transaction,
                owner_id=claim.owner_id,
                draft_uuid=claim.draft_uuid,
                source_state_revision=claim.state_revision,
            )
            if publication is not None:
                raise _ExpiredClaimBecameJournaled(
                    "expired generation claim acquired a durable publication"
                )
            return self._drafts.finish_generation(
                transaction,
                owner_id=reclaimed.owner_id,
                draft_id=reclaimed.draft_id,
                expected_revision=reclaimed.state_revision,
                claim_id=claim_id,
                status="error",
                error_message=_EXPIRED_CLAIM_SUMMARY,
                security_report=None,
                validation_report=None,
                required_credentials=None,
            )

    @staticmethod
    def _expired_claim_marker(claim: Any) -> str:
        encoded = json.dumps(
            {
                "claim_id": claim.generation_claim_id,
                "draft_uuid": claim.draft_uuid,
                "state_revision": claim.state_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "prejournal:" + hashlib.sha256(encoded).hexdigest()[:32]

    def _find_intent(self, request: GeneratedAgentPublicationRequest) -> Any | None:
        identity = generated_agent_publication_identity(
            owner_id=request.owner_id,
            draft_uuid=request.draft_uuid,
            source_state_revision=request.source_state_revision,
            generation_claim_id=request.generation_claim_id,
            target_agent_id=request.target_agent_id,
        )
        with self._runtime.transaction() as transaction:
            publication = self._journal.get_by_source(
                transaction,
                owner_id=request.owner_id,
                draft_uuid=request.draft_uuid,
                source_state_revision=request.source_state_revision,
            )
        if publication is None:
            return None
        if (
            publication.publication_id != str(identity.publication_id)
            or publication.generation_claim_id != request.generation_claim_id
            or publication.target_agent_id != request.target_agent_id
            or publication.target_revision_id != str(identity.target_revision_id)
        ):
            raise GeneratedAgentPublicationRecoveryPendingError(
                "publication source is occupied by a different durable intent"
            )
        return publication

    async def _is_live_attempt(self, publication: Any) -> bool:
        def durable_check() -> bool:
            if (
                publication.operation_id is None
                or publication.operation_execution_generation is None
            ):
                return False
            try:
                operation_id = uuid.UUID(publication.operation_id)
            except (TypeError, ValueError, AttributeError):
                return False
            operation = self._admission.repository.get_operation_for_administration(
                operation_id
            )
            if (
                operation is None
                or operation.state is not OperationState.RUNNING
                or operation.execution_generation
                != publication.operation_execution_generation
                or operation.execution_lease_token is None
            ):
                return False
            fence = PlaneExecutionFence(
                operation_id=operation.operation_id,
                execution_generation=operation.execution_generation,
                execution_lease_token=operation.execution_lease_token,
            )
            with self._runtime.transaction() as transaction:
                self._journal.assert_current_attempt(
                    transaction,
                    expected=publication,
                    attempt=fence,
                )
            return True

        outcome = await self._offload_outcome(durable_check)
        self._raise_outcome_cancellation(outcome)
        return outcome.error is None and outcome.result is True

    async def _recovery_loop(self) -> None:
        stop = self._recovery_stop
        if stop is None:
            return
        try:
            while not stop.is_set():
                try:
                    await self.recover_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception(
                        "generated-agent publication recovery pass failed"
                    )
                try:
                    await asyncio.wait_for(stop.wait(), self._recovery_interval_seconds)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _to_plane_fence(fence: ExecutionFence) -> PlaneExecutionFence:
        return PlaneExecutionFence(
            operation_id=fence.operation_id,
            execution_generation=fence.execution_generation,
            execution_lease_token=fence.execution_lease_token,
        )

    @staticmethod
    def _recovery_identities(binding: Any) -> tuple[uuid.UUID, uuid.UUID]:
        encoded = json.dumps(
            {
                "domain": _RECOVERY_IDENTITY_DOMAIN,
                "idempotency_key": binding.idempotency_key,
                "normalized_input_digest": binding.normalized_input_digest,
                "operation_kind": binding.operation_kind,
                "parent_operation_id": str(binding.parent_operation_id),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        def derive(suffix: bytes) -> uuid.UUID:
            return uuid.UUID(
                bytes=hashlib.sha256(encoded + suffix).digest()[:16], version=4
            )

        return derive(b":submission"), derive(b":request-generation")

    @staticmethod
    def _bundle_key(
        request: GeneratedAgentPublicationRequest,
        identity: GeneratedAgentPublicationIdentity,
    ) -> BundlePublicationKey:
        return BundlePublicationKey(
            scope_id=request.target_agent_id,
            staging_id=request.draft_uuid,
            source_revision=request.source_state_revision,
            publication_id=str(identity.publication_id),
            revision_id=str(identity.target_revision_id),
        )

    @staticmethod
    def _assert_terminal_matches_request(
        terminal: GeneratedAgentPublicationResult,
        request: GeneratedAgentPublicationRequest,
    ) -> None:
        identity = generated_agent_publication_identity(
            owner_id=request.owner_id,
            draft_uuid=request.draft_uuid,
            source_state_revision=request.source_state_revision,
            generation_claim_id=request.generation_claim_id,
            target_agent_id=request.target_agent_id,
        )
        publication = terminal.publication
        revision = terminal.revision
        if (
            publication.publication_id != str(identity.publication_id)
            or publication.generation_claim_id != request.generation_claim_id
            or publication.target_agent_id != request.target_agent_id
            or publication.target_revision_id != str(identity.target_revision_id)
            or revision.revision_id != str(identity.target_revision_id)
            or revision.promotion_token != str(identity.promotion_token)
            or revision.runtime_contract_version != request.runtime_contract_version
            or revision.release_lock_digest != request.release_lock_digest
            or revision.compatibility_state != request.compatibility_state
            or terminal.published.bundle_sha256 != request.bundle.bundle_sha256
            or terminal.published.manifest_json != request.bundle.manifest_json
            or terminal.generation_result != request.generation_result
        ):
            raise GeneratedAgentPublicationManagedError(
                "terminal publication replay changed immutable request identity"
            )

    @staticmethod
    def _generation_result_from_draft(
        draft: Any,
    ) -> GeneratedAgentPublicationResultMetadata:
        return GeneratedAgentPublicationResultMetadata(
            error_message=draft.error_message,
            security_report=draft.security_report,
            validation_report=draft.validation_report,
            required_credentials=draft.required_credentials,
        )

    @staticmethod
    def _manifest_digest(publication: Any, revision: Any) -> str:
        return publication.manifest_digest or canonical_generated_agent_manifest_digest(
            revision.manifest
        )

    async def _offload(
        self,
        callback: Callable[[], Any],
        *,
        cancellation_event: threading.Event | None = None,
    ) -> Any:
        outcome = await self._offload_outcome(
            callback,
            cancellation_event=cancellation_event,
        )
        if outcome.cancellation is not None:
            setattr(outcome.cancellation, "_joined_worker_result", outcome.result)
            setattr(outcome.cancellation, "_joined_worker_error", outcome.error)
            if outcome.error is not None:
                raise outcome.cancellation from outcome.error
            raise outcome.cancellation
        if outcome.error is not None:
            raise outcome.error
        return outcome.result

    async def _offload_outcome(
        self,
        callback: Callable[[], Any],
        *,
        cancellation_event: threading.Event | None = None,
    ) -> _WorkerOutcome:
        task = asyncio.create_task(asyncio.to_thread(callback))
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.done() and task.cancelled():
                    break
                cancellation = cancellation or exc
                if cancellation_event is not None:
                    cancellation_event.set()
            except BaseException:
                if task.done():
                    break
                raise
        try:
            return _WorkerOutcome(result=task.result(), cancellation=cancellation)
        except BaseException as exc:
            return _WorkerOutcome(error=exc, cancellation=cancellation)

    async def _join_task(self, task: asyncio.Task[Any]) -> _WorkerOutcome:
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.done() and task.cancelled():
                    break
                cancellation = cancellation or exc
            except BaseException:
                if task.done():
                    break
                raise
        if task.cancelled():
            try:
                task.result()
            except asyncio.CancelledError as exc:
                return _WorkerOutcome(error=exc, cancellation=cancellation)
        try:
            return _WorkerOutcome(result=task.result(), cancellation=cancellation)
        except BaseException as exc:
            return _WorkerOutcome(error=exc, cancellation=cancellation)

    async def _close_heartbeat_safely(
        self,
        heartbeat: GenerationClaimHeartbeat,
    ) -> BaseException | None:
        task = asyncio.create_task(heartbeat.close())
        joined = await self._join_task(task)
        if joined.cancellation is not None:
            if joined.error is not None:
                raise joined.cancellation from joined.error
            raise joined.cancellation
        return joined.error

    async def _close_heartbeat_outcome(
        self,
        heartbeat: GenerationClaimHeartbeat,
    ) -> _WorkerOutcome:
        try:
            return _WorkerOutcome(error=await self._close_heartbeat_safely(heartbeat))
        except asyncio.CancelledError as cancelled:
            return _WorkerOutcome(
                error=cancelled.__cause__,
                cancellation=cancelled,
            )

    @staticmethod
    def _merge_cleanup_error(
        heartbeat_outcome: _WorkerOutcome,
        later_error: BaseException | None,
    ) -> BaseException | None:
        merged = GeneratedAgentPublicationService._merge_worker_outcomes(
            heartbeat_outcome,
            _WorkerOutcome(error=later_error),
        )
        return GeneratedAgentPublicationService._outcome_primary_error(merged)

    @staticmethod
    def _merge_worker_outcomes(
        earlier: _WorkerOutcome,
        later: _WorkerOutcome,
    ) -> _WorkerOutcome:
        error = GeneratedAgentPublicationService._chain_error(
            later.error,
            earlier.error,
        )
        return _WorkerOutcome(
            result=later.result,
            error=error,
            cancellation=earlier.cancellation or later.cancellation,
        )

    @staticmethod
    def _outcome_primary_error(outcome: _WorkerOutcome) -> BaseException | None:
        if outcome.cancellation is not None:
            if outcome.error is not None and outcome.cancellation.__cause__ is None:
                outcome.cancellation.__cause__ = outcome.error
            return outcome.cancellation
        return outcome.error

    @staticmethod
    def _raise_outcome_cancellation(outcome: _WorkerOutcome) -> None:
        if outcome.cancellation is None:
            return
        if outcome.error is not None:
            raise outcome.cancellation from outcome.error
        raise outcome.cancellation

    @staticmethod
    def _chain_error(
        error: BaseException | None,
        prior: BaseException | None,
    ) -> BaseException | None:
        if error is None:
            return prior
        if prior is not None and error is not prior and error.__cause__ is None:
            error.__cause__ = prior
        return error

    def _cancellation_marker(
        self,
        attempt: _PublicationAttempt,
        error: BaseException | None,
        *,
        shared_attempt_owned: bool = False,
    ) -> BaseException | None:
        with attempt.snapshot_lock:
            managed = shared_attempt_owned or attempt.publication is not None
        if not managed:
            if (
                isinstance(error, asyncio.CancelledError)
                and error.__cause__ is not None
            ):
                return error.__cause__
            return error
        marker: GeneratedAgentPublicationError
        if isinstance(error, asyncio.CancelledError) and isinstance(
            error.__cause__, GeneratedAgentPublicationError
        ):
            marker = error.__cause__
        elif isinstance(error, GeneratedAgentPublicationRecoveryPendingError):
            marker = error
        else:
            marker = GeneratedAgentPublicationManagedCancellation(
                "service retained claim ownership through caller cancellation"
            )
            marker.__cause__ = error
        return marker

    def _attempt_done(
        self,
        key: tuple[str, str, int],
        attempt: _PublicationAttempt,
        _task: asyncio.Task[Any],
    ) -> None:
        with self._state_lock:
            if self._attempts.get(key) is attempt:
                self._attempts.pop(key, None)


__all__ = (
    "GenerationClaimHeartbeat",
    "GenerationClaimLostError",
    "GeneratedAgentPublicationError",
    "GeneratedAgentPublicationIdentity",
    "GeneratedAgentPublicationManagedCancellation",
    "GeneratedAgentPublicationManagedError",
    "GeneratedAgentPublicationPreIntentError",
    "GeneratedAgentPublicationReadiness",
    "GeneratedAgentPublicationRecoveryPendingError",
    "GeneratedAgentPublicationRequest",
    "GeneratedAgentPublicationResult",
    "GeneratedAgentPublicationService",
    "GeneratedAgentRecoveryReport",
    "generated_agent_publication_identity",
)
