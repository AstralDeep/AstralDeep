"""User-agent registry accessors (feature 057).

The durable ``user_agent`` table — one row per user-authored, client-hosted
agent. Canonical owner key is ``owner_user_id`` (the OIDC ``sub``); the boundary
binds to it and never to a card field or email. ``status`` is the durable
lifecycle (authoring|validated|live|disabled); running/offline is DERIVED from
socket presence and is never stored here.

Also home to ``can_user_use_agent`` — the owner-isolation predicate the boundary
enforces in three places (grant endpoint, dispatch gate, tool-list build) so a
private user agent is invisible/unusable to non-owners (FR-016/019).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import PurePosixPath
import re
import time
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence
import uuid

from astralplane.authority import AuthorityPopulation
from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.agents import (
    AgentHostSessionRecord as PlaneHostSessionRecord,
    AgentRevisionRecord as PlaneAgentRevisionRecord,
    AgentRuntimeInstanceRecord as PlaneRuntimeInstanceRecord,
    AgentRuntimeRequestRecord as PlaneRuntimeRequestRecord,
    UserAgentRecord as PlaneUserAgentRecord,
)

from orchestrator.lets_lifecycle import (
    GovernedLifecycleCoordinator,
    GovernedRuntime,
    LifecycleConvergence,
)
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)
from orchestrator.work_admission import (
    ExecutionFence,
    OperationState,
    StaleExecutionFenceError,
    WorkAdmissionConflictError,
    WorkAdmissionRepository,
)
from shared.protocol import RuntimeFence


def _now_ms() -> int:
    return int(time.time() * 1000)


_STRICT_SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_RUNTIME_TERMINAL_STATES = frozenset({"stopped", "failed", "offline", "superseded"})
_REQUEST_TERMINAL_STATES = frozenset(
    {"completed", "failed", "cancelled", "retryable"}
)
_MAX_GENERATION = (1 << 63) - 1


class PersonalAgentRuntimeError(RuntimeError):
    """Base class for safe personal-agent runtime repository failures."""


class HostRegistrationRefused(PersonalAgentRuntimeError):
    """Structured, non-sensitive host-registration refusal."""

    def __init__(self, code: str, details: Mapping[str, Any]) -> None:
        self.code = code
        self.details = MappingProxyType(dict(details))
        super().__init__(code)


class PersonalAgentNotFoundError(PersonalAgentRuntimeError):
    """The owner-scoped personal agent or runtime identity does not exist."""


class UserAgentOwnershipConflict(PersonalAgentRuntimeError):
    """An immutable agent ID is already bound to a different owner."""


class AgentDeletedError(PersonalAgentRuntimeError):
    """The durable agent tombstone prevents the requested mutation."""


class AgentOfflineError(PersonalAgentRuntimeError):
    """No exact current online authoritative runtime can accept the request."""


class StaleRuntimeGenerationError(PersonalAgentRuntimeError):
    """One or more immutable runtime/request fence fields are stale."""

    code = "stale_runtime_generation"


@dataclass(frozen=True)
class RuntimeCompatibilityPolicy:
    """Injected candidate-owned BYO runtime compatibility policy."""

    runtime_contract_version: int
    runtime_lock_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.runtime_contract_version) is not int
            or self.runtime_contract_version <= 0
        ):
            raise ValueError("runtime contract version must be a positive integer")
        if not _SHA256_RE.fullmatch(self.runtime_lock_sha256):
            raise ValueError("runtime lock digest must be 64 lowercase hex characters")


@dataclass(frozen=True)
class HostSessionFence:
    owner_user_id: str
    host_id: str
    host_session_id: str
    connection_scope_id: str
    host_generation: int


@dataclass(frozen=True)
class HostSessionRecord:
    host_session_id: str
    host_id: str
    owner_user_id: str
    connection_scope_id: str
    platform: str
    client_version: str
    host_generation: int
    supersedes_session_id: Optional[str]
    supported_runtime_contract_versions: tuple[int, ...]
    runtime_contract_version: int
    release_lock_digest: str
    state: str
    inventory_state: str
    eligible_since: Any
    accepted_at: Any
    last_seen_at: Any
    disconnected_at: Any
    inventory_reconciled_at: Any
    failure_code: Optional[str]

    @property
    def fence(self) -> HostSessionFence:
        return HostSessionFence(
            owner_user_id=self.owner_user_id,
            host_id=self.host_id,
            host_session_id=self.host_session_id,
            connection_scope_id=self.connection_scope_id,
            host_generation=self.host_generation,
        )


@dataclass(frozen=True)
class AgentRevisionRecord:
    revision_id: str
    agent_id: str
    owner_user_id: str
    revision_number: int
    parent_revision_id: Optional[str]
    previous_good_revision_id: Optional[str]
    artifact_digest: str
    manifest: Mapping[str, Any]
    artifact_relative_path: str
    runtime_contract_version: int
    release_lock_digest: str
    compatibility_state: str
    state: str
    promotion_token: str
    state_revision: int


@dataclass(frozen=True)
class RuntimeInstanceRecord:
    fence: RuntimeFence
    operation_id: Optional[str]
    operation_execution_generation: int
    state: str
    is_authoritative: bool
    state_revision: int
    created_at: Any
    started_at: Any
    registered_at: Any
    last_heartbeat_sequence: Optional[int]
    ready_at: Any
    last_liveness_at: Any
    terminal_at: Any
    failure_code: Optional[str]
    # Reconnect/client lifecycle projection context.  Ordinary transition
    # methods may return a row without these joined user-agent pointers; the
    # latest-runtime hydration query includes them so host-facing ``ready`` can
    # never be mistaken for invocable public ``online``.
    active_revision_id: Optional[str] = field(default=None, compare=False)
    authoritative_instance_id: Optional[str] = field(default=None, compare=False)


@dataclass(frozen=True)
class RuntimeRequestFence:
    runtime: RuntimeFence
    request_id: str
    request_generation: str
    operation_id: str
    operation_execution_generation: int
    operation_execution_lease_token: Optional[str]


@dataclass(frozen=True)
class RuntimeRequestRecord:
    fence: RuntimeRequestFence
    state: str
    state_revision: int
    assigned_at: Any
    terminal_at: Any
    terminal_code: Optional[str]
    result_digest: Optional[str]


@dataclass(frozen=True)
class HostSelection:
    session: Optional[HostSessionRecord]
    previous_session_id: Optional[str]
    changed: bool
    lifecycle_generation: int


@dataclass(frozen=True)
class SelectedSessionRevision:
    """The exact selected host and active immutable revision for one agent."""

    host: HostSessionRecord
    revision: AgentRevisionRecord
    lifecycle_generation: int


@dataclass(frozen=True)
class HostInventoryEntry:
    agent_id: str
    revision_id: str
    bundle_sha256: str
    runtime_contract_version: int
    required_runtime_lock_sha256: str


@dataclass(frozen=True)
class HostInventorySelectedDelivery:
    delivery_id: str
    runtime_instance_id: str
    lifecycle_generation: int
    runtime_contract_version: int
    required_runtime_lock_sha256: str
    bundle_sha256: str


@dataclass(frozen=True)
class HostInventoryAction:
    agent_id: str
    revision_id: str
    action: str
    reason_code: Optional[str]
    selected_delivery: Optional[HostInventorySelectedDelivery]


@dataclass(frozen=True)
class HostInventoryReconciliation:
    host: HostSessionRecord
    inventory_id: str
    actions: tuple[HostInventoryAction, ...]
    reconciled_at: Any


@dataclass(frozen=True)
class SelectedRecoveryDelivery:
    host: HostSessionRecord
    revision: AgentRevisionRecord
    instance: RuntimeInstanceRecord


@dataclass(frozen=True)
class RuntimeSettlement:
    instance: RuntimeInstanceRecord
    settled_request_ids: tuple[str, ...]
    settlement_code: Optional[str] = None


@dataclass(frozen=True)
class HostDisconnectResult:
    settled_request_ids: tuple[str, ...]
    settlements: tuple[RuntimeSettlement, ...]
    selected_sessions: Mapping[str, Optional[str]]


@dataclass(frozen=True)
class AgentTombstone:
    agent_id: str
    owner_user_id: str
    lifecycle_generation: int
    state_revision: int
    deleted_at: int


@dataclass(frozen=True)
class AgentTombstoneCleanup:
    tombstone: AgentTombstone
    settlements: tuple[RuntimeSettlement, ...]
    settled_request_ids: tuple[str, ...]


@dataclass(frozen=True)
class GovernedByoAgentLifecycle:
    """Owner-scoped LETS event adapter for durable BYO runtime fences.

    ``PersonalAgentRuntimeRepository`` remains a synchronous neutral data-plane
    repository. The asynchronous host boundary invokes this adapter only after
    its Astral transaction commits and before it publishes routing authority.
    This keeps network lifecycle convergence out of Plane transactions while
    still ensuring that a runtime is never advertised online first.
    """

    coordinator: GovernedLifecycleCoordinator

    def __post_init__(self) -> None:
        if not isinstance(self.coordinator, GovernedLifecycleCoordinator):
            raise TypeError("governed lifecycle coordinator is required")

    async def admit_or_resume(
        self,
        *,
        owner_user_id: str,
        runtime: RuntimeInstanceRecord,
        declared_scopes: Sequence[str],
        executor_conformant: bool,
    ) -> LifecycleConvergence:
        fence = runtime.fence
        return await self.coordinator.admit_or_resume_runtime(
            GovernedRuntime(
                owner_id=owner_user_id,
                agent_id=fence.agent_id,
                runtime_id=fence.runtime_instance_id,
                runtime_generation=fence.lifecycle_generation,
                population=AuthorityPopulation.BYO_USER,
                declared_scopes=tuple(declared_scopes),
                executor_conformant=executor_conformant,
            )
        )

    async def host_lost(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
    ) -> LifecycleConvergence:
        return await self.coordinator.quiesce_current(
            owner_id=owner_user_id,
            agent_id=agent_id,
            population=AuthorityPopulation.BYO_USER,
        )

    async def retire_runtime(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        runtime_id: str | None = None,
        runtime_generation: int | None = None,
    ) -> LifecycleConvergence:
        if runtime_id is not None or runtime_generation is not None:
            if runtime_id is None or runtime_generation is None:
                raise ValueError(
                    "runtime_id and runtime_generation must be supplied together"
                )
            return await self.coordinator.close_runtime_generation(
                owner_id=owner_user_id,
                agent_id=agent_id,
                runtime_id=runtime_id,
                runtime_generation=runtime_generation,
                population=AuthorityPopulation.BYO_USER,
            )
        return await self.coordinator.close_current(
            owner_id=owner_user_id,
            agent_id=agent_id,
            population=AuthorityPopulation.BYO_USER,
        )

    async def revoke_agent(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        reason_code: str,
    ) -> LifecycleConvergence:
        return await self.coordinator.revoke_current(
            owner_id=owner_user_id,
            agent_id=agent_id,
            population=AuthorityPopulation.BYO_USER,
            reason_code=reason_code,
        )

    async def renew_if_due(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        now_ns: int,
        renewal_window_ns: int,
    ) -> LifecycleConvergence:
        return await self.coordinator.renew_current_if_due(
            owner_id=owner_user_id,
            agent_id=agent_id,
            population=AuthorityPopulation.BYO_USER,
            now_ns=now_ns,
            renewal_window_ns=renewal_window_ns,
        )


def _uuid4_text(value: Any, field_name: str) -> str:
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID4") from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ValueError(f"{field_name} must be a UUID4")
    return str(parsed)


def _required_text(value: Any, field_name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field_name} must be a bounded non-empty string")
    return value


def _canonical_text_tuple(
    values: Sequence[str] | None,
    field_name: str,
    *,
    maximum: int = 512,
) -> tuple[str, ...]:
    """Normalize one ordered declaration exactly as Plane persists it."""

    if values is None:
        return ()
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{field_name} must be a sequence of strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _required_text(value, field_name, maximum=maximum)
        if text not in seen:
            normalized.append(text)
            seen.add(text)
    return tuple(normalized)


def _sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be 64 lowercase hex characters")
    return value


def _safe_code(value: Any, field_name: str = "failure_code") -> str:
    if not isinstance(value, str) or not _SAFE_CODE_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a safe canonical code")
    return value


def _plain_json(value: Any) -> Any:
    """Detach immutable JSON containers without weakening type validation."""

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _frozen_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _frozen_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_frozen_json(item) for item in value)
    return value


def _agent_dict(record: PlaneUserAgentRecord) -> Dict[str, Any]:
    """Preserve the established Deep projection over a detached Plane row."""

    return {
        "agent_id": record.agent_id,
        "owner_user_id": record.owner_id,
        "owner_email": record.owner_email,
        "display_name": record.display_name,
        "status": record.status,
        "declared_tools": list(record.declared_tools),
        "declared_scopes": list(record.declared_scopes),
        "declared_egress": (
            None if record.declared_egress is None else list(record.declared_egress)
        ),
        "constitution_version": record.constitution_version,
        "validated_at": record.validated_at,
        "revalidation_required": record.revalidation_required,
        "draft_id": record.draft_id,
        "host_client_id": record.host_client_id,
        "host_session_id": record.host_session_id,
        "host_last_seen_at": record.host_last_seen_at,
        "is_public": record.is_public,
        "deleted_at": record.deleted_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "active_revision_id": record.active_revision_id,
        "last_known_good_revision_id": record.last_known_good_revision_id,
        "selected_host_session_id": record.selected_host_session_id,
        "authoritative_instance_id": record.authoritative_instance_id,
        "lifecycle_generation": record.lifecycle_generation,
        "generation_counter": record.generation_counter,
        "state_revision": record.state_revision,
        "validated_policy_revision": record.validated_policy_revision,
    }


class UserAgentRegistry:
    """Deep lifecycle policy over Plane's typed user-agent registry."""

    def __init__(
        self,
        *,
        plane_runtime=None,
        plane_repositories=None,
        plane_repository=None,
    ) -> None:
        repository, runtime = repository_from(
            "agents",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=None,
        )
        if runtime is None:
            raise ValueError("an initialized Plane runtime is required")
        self._agents = PlaneRepositoryContext(
            repository=plane_repository or repository,
            plane_runtime=runtime,
        )

    @staticmethod
    def _classify(record: PlaneUserAgentRecord | None) -> PlaneUserAgentRecord:
        if record is None:
            raise PersonalAgentNotFoundError("personal agent not found")
        if record.deleted_at is not None:
            raise AgentDeletedError("agent_deleted")
        return record

    def create(
        self,
        *,
        agent_id: str,
        owner_user_id: str,
        display_name: str,
        owner_email: Optional[str] = None,
        draft_id: Optional[str] = None,
        declared_tools: Optional[List[str]] = None,
        declared_scopes: Optional[List[str]] = None,
        declared_egress: Optional[List[str]] = None,
    ) -> PlaneUserAgentRecord:
        now = _now_ms()
        with self._agents.transaction() as transaction:
            self._agents.repository.lock_owner(
                transaction,
                owner_id=owner_user_id,
            )
            existing = self._agents.repository.get_agent_for_administration(
                transaction,
                agent_id=agent_id,
                for_update=True,
            )
            if existing is None:
                return self._agents.repository.create_agent(
                    transaction,
                    agent_id=agent_id,
                    owner_id=owner_user_id,
                    owner_email=owner_email,
                    display_name=display_name,
                    draft_id=draft_id,
                    declared_tools=declared_tools or (),
                    declared_scopes=declared_scopes or (),
                    declared_egress=declared_egress,
                    observed_at=now,
                )
            if existing.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            if existing.owner_id != owner_user_id:
                raise UserAgentOwnershipConflict(
                    "agent id is already bound to a different owner"
                )
            try:
                return self._agents.repository.compare_and_set_agent(
                    transaction,
                    owner_id=owner_user_id,
                    agent_id=agent_id,
                    expected_revision=existing.state_revision,
                    updates={
                        "owner_email": owner_email,
                        "display_name": display_name,
                        "draft_id": draft_id,
                        "declared_tools": declared_tools or (),
                        "declared_scopes": declared_scopes or (),
                        "declared_egress": declared_egress,
                        "updated_at": now,
                    },
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "user-agent authoring update is stale"
                ) from exc

    def admit_authoring_target(
        self,
        *,
        agent_id: str,
        owner_user_id: str,
        display_name: str,
        draft_id: str,
        expected_constitution_version: str,
        revises_agent_id: Optional[str] = None,
        owner_email: Optional[str] = None,
        declared_tools: Optional[Sequence[str]] = None,
        declared_scopes: Optional[Sequence[str]] = None,
        declared_egress: Optional[Sequence[str]] = None,
    ) -> PlaneUserAgentRecord:
        """Admit an immutable authoring target without rewriting an incumbent.

        A revision is identified by the persisted ``revises_agent_id`` and may
        reuse only that exact, owner-bound, non-deleted row.  A new target is
        inserted only when absent.  Its retries are read-only exact-semantic
        replays, including the post-promotion ``live`` replay case.
        """

        agent_id = _required_text(agent_id, "agent_id")
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        display_name = _required_text(
            display_name,
            "display_name",
            maximum=1024,
        )
        draft_id = _required_text(draft_id, "draft_id")
        expected_constitution_version = _required_text(
            expected_constitution_version,
            "expected_constitution_version",
        )
        if revises_agent_id is not None:
            revises_agent_id = _required_text(
                revises_agent_id,
                "revises_agent_id",
            )
            if revises_agent_id != agent_id:
                raise ValueError("revises_agent_id must equal the authoring target")
        tools = _canonical_text_tuple(declared_tools, "declared_tools")
        scopes = _canonical_text_tuple(declared_scopes, "declared_scopes")
        egress_values = _canonical_text_tuple(declared_egress, "declared_egress")
        egress = egress_values or None

        def classify_existing(record: PlaneUserAgentRecord) -> PlaneUserAgentRecord:
            if record.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            if record.owner_id != owner_user_id:
                raise UserAgentOwnershipConflict(
                    "agent id is already bound to a different owner"
                )
            if revises_agent_id is not None:
                return record
            lifecycle_matches = (
                record.status == "authoring"
                and record.constitution_version is None
            ) or (
                record.status == "live"
                and record.constitution_version
                == expected_constitution_version
            )
            if not (
                lifecycle_matches
                and record.draft_id == draft_id
                and record.display_name == display_name
                and record.declared_tools == tools
                and record.declared_scopes == scopes
                and record.declared_egress == egress
            ):
                raise StaleRuntimeGenerationError(
                    "user-agent authoring target replay changed semantics"
                )
            return record

        now = _now_ms()
        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            existing = repository.get_agent_for_administration(
                transaction,
                agent_id=agent_id,
                for_update=True,
            )
            if existing is not None:
                return classify_existing(existing)
            if revises_agent_id is not None:
                raise PersonalAgentNotFoundError("personal agent not found")
            try:
                created = repository.create_agent(
                    transaction,
                    agent_id=agent_id,
                    owner_id=owner_user_id,
                    owner_email=owner_email,
                    display_name=display_name,
                    draft_id=draft_id,
                    declared_tools=tools,
                    declared_scopes=scopes,
                    declared_egress=egress,
                    observed_at=now,
                )
            except RepositoryConflictError as exc:
                # A different owner lock can race for the globally unique ID.
                # Plane's INSERT uses ON CONFLICT, so this transaction remains
                # usable for exact classification of the winning row.
                existing = repository.get_agent_for_administration(
                    transaction,
                    agent_id=agent_id,
                    for_update=True,
                )
                if existing is None:
                    raise StaleRuntimeGenerationError(
                        "user-agent authoring target create is stale"
                    ) from exc
                return classify_existing(existing)
            return classify_existing(created)

    def get(self, agent_id: str) -> PlaneUserAgentRecord | None:
        return self._agents.call(
            self._agents.repository.get_agent_for_administration,
            agent_id=agent_id,
        )

    def list_for_owner(self, owner_user_id: str) -> tuple[PlaneUserAgentRecord, ...]:
        return self._agents.call(
            self._agents.repository.list_agents,
            owner_id=owner_user_id,
            include_deleted=False,
            limit=2000,
        )

    @staticmethod
    def _ownership_dict(record) -> dict[str, object]:
        return {
            "agent_id": record.agent_id,
            "owner_email": record.owner_email,
            "is_public": record.is_public,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def get_agent_ownership(self, agent_id: str) -> dict[str, object] | None:
        record = self._agents.call(
            self._agents.repository.get_ownership,
            agent_id=agent_id,
        )
        return None if record is None else self._ownership_dict(record)

    def set_agent_ownership(
        self,
        agent_id: str,
        owner_email: str,
        is_public: bool = False,
    ) -> dict[str, object]:
        try:
            record = self._agents.call(
                self._agents.repository.upsert_ownership,
                agent_id=agent_id,
                owner_email=owner_email,
                is_public=bool(is_public),
                observed_at=_now_ms(),
            )
        except RepositoryConflictError as exc:
            raise UserAgentOwnershipConflict(
                "agent id is already bound to a different owner"
            ) from exc
        return self._ownership_dict(record)

    def set_agent_visibility(self, agent_id: str, is_public: bool) -> bool:
        with self._agents.transaction() as transaction:
            current = self._agents.repository.get_ownership(
                transaction,
                agent_id=agent_id,
            )
            if current is None:
                return False
            self._agents.repository.set_visibility(
                transaction,
                agent_id=agent_id,
                owner_email=current.owner_email,
                is_public=bool(is_public),
                updated_at=_now_ms(),
            )
        return True

    def get_all_agent_ownership(self) -> list[dict[str, object]]:
        records = self._agents.call(
            self._agents.repository.list_ownership_for_administration,
            limit=5000,
        )
        if len(records) >= 5000:
            raise PersonalAgentRuntimeError(
                "agent ownership inventory exceeds the bounded administration read"
            )
        return [self._ownership_dict(record) for record in records]

    def get_agent_is_safe(self, agent_id: str) -> bool:
        record = self._agents.call(
            self._agents.repository.get_trust,
            agent_id=agent_id,
        )
        return bool(record is not None and record.is_safe)

    def upsert_agent_safe(
        self,
        agent_id: str,
        is_safe: bool,
        *,
        marked_by: str,
    ) -> bool:
        record = self._agents.call(
            self._agents.repository.set_trust,
            agent_id=agent_id,
            is_safe=bool(is_safe),
            marked_by=marked_by,
        )
        return bool(record.prior_state)

    def reset_agent_safe(self, agent_id: str, *, marked_by: str) -> bool:
        record = self._agents.call(
            self._agents.repository.set_trust,
            agent_id=agent_id,
            is_safe=False,
            marked_by=marked_by,
            reset_for_revision=True,
        )
        return bool(record.prior_state)

    def _update(
        self,
        agent_id: str,
        updates: Mapping[str, object],
    ) -> PlaneUserAgentRecord:
        with self._agents.transaction() as transaction:
            current = self._classify(
                self._agents.repository.get_agent_for_administration(
                    transaction,
                    agent_id=agent_id,
                    for_update=True,
                )
            )
            try:
                return self._agents.repository.compare_and_set_agent(
                    transaction,
                    owner_id=current.owner_id,
                    agent_id=agent_id,
                    expected_revision=current.state_revision,
                    updates=updates,
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "user-agent lifecycle CAS is stale"
                ) from exc

    def mark_validated(
        self,
        agent_id: str,
        constitution_version: Optional[str],
        *,
        declared_tools: Optional[List[str]] = None,
        declared_scopes: Optional[List[str]] = None,
    ) -> PlaneUserAgentRecord:
        now = _now_ms()
        updates: Dict[str, object] = {
            "status": "validated",
            "constitution_version": constitution_version,
            "validated_at": now,
            "revalidation_required": False,
            "updated_at": now,
        }
        if declared_tools is not None:
            updates["declared_tools"] = declared_tools
        if declared_scopes is not None:
            updates["declared_scopes"] = declared_scopes
        return self._update(agent_id, updates)

    def go_live(
        self,
        agent_id: str,
        *,
        host_client_id: Optional[str] = None,
        host_session_id: Optional[str] = None,
    ) -> PlaneUserAgentRecord:
        now = _now_ms()
        with self._agents.transaction() as transaction:
            current = self._classify(
                self._agents.repository.get_agent_for_administration(
                    transaction,
                    agent_id=agent_id,
                    for_update=True,
                )
            )
            try:
                updated = self._agents.repository.compare_and_set_agent(
                    transaction,
                    owner_id=current.owner_id,
                    agent_id=agent_id,
                    expected_revision=current.state_revision,
                    updates={
                        "status": "live",
                        "host_client_id": host_client_id,
                        "host_session_id": host_session_id,
                        "host_last_seen_at": now,
                        "updated_at": now,
                    },
                )
                self._agents.repository.upsert_ownership(
                    transaction,
                    agent_id=agent_id,
                    owner_email=updated.owner_email or updated.owner_id,
                    is_public=False,
                    observed_at=now,
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "user-agent live transition is stale"
                ) from exc
        return updated

    def touch_liveness(self, agent_id: str) -> PlaneUserAgentRecord:
        return self._update(agent_id, {"host_last_seen_at": _now_ms()})

    def mark_revalidation_required(
        self,
        agent_id: str,
        required: bool = True,
    ) -> PlaneUserAgentRecord:
        return self._update(
            agent_id,
            {"revalidation_required": bool(required), "updated_at": _now_ms()},
        )

    def soft_delete(self, agent_id: str) -> PlaneUserAgentRecord:
        now = _now_ms()
        with self._agents.transaction() as transaction:
            current = self._classify(
                self._agents.repository.get_agent_for_administration(
                    transaction,
                    agent_id=agent_id,
                    for_update=True,
                )
            )
            try:
                return self._agents.repository.tombstone_agent(
                    transaction,
                    owner_id=current.owner_id,
                    agent_id=agent_id,
                    expected_revision=current.state_revision,
                    deleted_at=now,
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "user-agent tombstone CAS is stale"
                ) from exc


def _registry_from(value: Any) -> UserAgentRegistry:
    if isinstance(value, UserAgentRegistry):
        return value
    bound = getattr(value, "user_agent_registry", None)
    if isinstance(bound, UserAgentRegistry):
        return bound
    runtime = getattr(value, "plane_runtime", None)
    repositories = getattr(value, "plane_repositories", None)
    if runtime is None:
        raise TypeError("user-agent registry requires the application Plane runtime")
    return UserAgentRegistry(
        plane_runtime=runtime,
        plane_repositories=repositories,
    )


def create_user_agent(registry, **kwargs) -> None:
    _registry_from(registry).create(**kwargs)


def admit_authoring_target(registry, **kwargs) -> PlaneUserAgentRecord:
    """Atomically admit a new or revision authoring target."""

    return _registry_from(registry).admit_authoring_target(**kwargs)


def get_user_agent(registry, agent_id: str) -> Optional[Dict[str, Any]]:
    record = _registry_from(registry).get(agent_id)
    return None if record is None else _agent_dict(record)


def is_user_agent(registry, agent_id: str) -> bool:
    return get_user_agent(registry, agent_id) is not None


def list_user_agents(registry, owner_user_id: str) -> List[Dict[str, Any]]:
    """The owner's agents, most-recent first, excluding soft-deleted rows."""

    return [
        _agent_dict(record)
        for record in _registry_from(registry).list_for_owner(owner_user_id)
    ]


def mark_validated(
    registry,
    agent_id: str,
    constitution_version: Optional[str],
    *,
    declared_tools: Optional[List[str]] = None,
    declared_scopes: Optional[List[str]] = None,
) -> None:
    _registry_from(registry).mark_validated(
        agent_id,
        constitution_version,
        declared_tools=declared_tools,
        declared_scopes=declared_scopes,
    )


def go_live(
    registry,
    agent_id: str,
    *,
    host_client_id: Optional[str] = None,
    host_session_id: Optional[str] = None,
) -> None:
    _registry_from(registry).go_live(
        agent_id,
        host_client_id=host_client_id,
        host_session_id=host_session_id,
    )


def touch_liveness(registry, agent_id: str) -> None:
    _registry_from(registry).touch_liveness(agent_id)


def mark_revalidation_required(
    registry,
    agent_id: str,
    required: bool = True,
) -> None:
    _registry_from(registry).mark_revalidation_required(agent_id, required)


def soft_delete(registry, agent_id: str) -> None:
    _registry_from(registry).soft_delete(agent_id)


#: Reserved id prefixes/stems a user agent may never register as (Constitution H).
_RESERVED_PREFIXES = ("__",)


def authorize_registration(db, owner_sub: str, agent_id: str, *,
                           reserved_ids: Optional[frozenset] = None):
    """Owner-binding decision for a user-agent tunnel registration (058 T002,
    FR-002/FR-015). Returns ``(ok, reason)``, fail-closed.

    The owner is the authenticated session ``sub`` (never a card field). A
    registration is admitted ONLY when the ``user_agent`` row exists, is owned by
    ``owner_sub``, is in a runnable ``status`` (``validated``/``live``), and is not
    flagged ``revalidation_required``. Reserved/colliding ids are refused
    (Constitution H). This is the single security decision the tunnel registration
    path depends on; it derives authority solely from the orchestrator's own
    record."""
    if not owner_sub or not agent_id:
        return False, "missing owner or agent id"
    if agent_id.startswith(_RESERVED_PREFIXES):
        return False, "reserved agent id"
    if reserved_ids and agent_id in reserved_ids:
        return False, "agent id collides with a built-in or reserved agent"
    try:
        ua = get_user_agent(db, agent_id)
    except Exception:
        return False, "registry lookup failed"
    if ua is None:
        return False, "no user-agent registry record for this agent id"
    if ua.get("owner_user_id") != owner_sub:
        return False, "agent is owned by a different user"
    if ua.get("deleted_at") is not None:
        return False, "agent is deleted"
    if ua.get("status") not in ("validated", "live"):
        return False, f"agent is not ready to run (status={ua.get('status')})"
    if ua.get("revalidation_required"):
        return False, "agent must re-pass Analyze before it can run again"
    return True, ""


def can_user_use_agent(db, user_id: str, agent_id: str) -> bool:
    """User-agent owner-isolation predicate.

    A **user agent** (feature 057 — private, client-hosted, owner-scoped) is
    usable/manageable ONLY by its owner (``user_agent.owner_user_id``). For any
    **non-user-agent** (built-in agents, the public catalog, drafts) this returns
    ``True`` and the normal per-user permission gate governs access — this
    predicate is NOT a general access check, it only enforces user-agent owner
    isolation, so it never blocks a user from managing their own permissions on a
    shared/built-in agent (including private built-ins usable via the safe-agent
    baseline). Enforced at the grant endpoint, the dispatch gate, and tool-list
    build (FR-016/019). Fail-closed: an unreadable owner on a known user agent
    denies non-owners."""
    if not user_id or not agent_id:
        return False
    try:
        ua = get_user_agent(db, agent_id)
    except Exception:
        # Fail closed only for a definite user-agent id; an errored lookup on an
        # unknown id must not lock out built-ins, so treat unknown as allowed.
        return True
    if ua is None:
        return True   # not a user agent → existing gates apply
    if ua.get("deleted_at") is not None:
        return False
    return ua.get("owner_user_id") == user_id


class PersonalAgentRuntimeRepository:
    """Deep lifecycle policy over the application-scoped Plane authorities.

    Every state transition uses one caller-owned Plane transaction. Owner locks,
    exact immutable fences, and the shared WorkAdmission repository are composed
    together; no mutable process-local map is an authority for selection,
    liveness, or settlement.
    """

    def __init__(
        self,
        database: Any | None = None,
        *,
        compatibility_policy: RuntimeCompatibilityPolicy,
        operation_repository: WorkAdmissionRepository,
        operation_retention: timedelta = timedelta(hours=24),
        uuid_factory: Any = uuid.uuid4,
        plane_runtime: Any | None = None,
        plane_repositories: Any | None = None,
        plane_repository: Any | None = None,
    ) -> None:
        if not isinstance(compatibility_policy, RuntimeCompatibilityPolicy):
            raise TypeError("compatibility_policy must be RuntimeCompatibilityPolicy")
        if operation_retention <= timedelta(0):
            raise ValueError("operation_retention must be positive")
        if not callable(uuid_factory):
            raise TypeError("uuid_factory must be callable")
        if operation_repository is None:
            raise TypeError("operation_repository must be the shared Plane authority")
        if database is not None:
            raise TypeError(
                "database persistence injection is retired; inject plane_runtime"
            )
        repository, runtime = repository_from(
            "agents",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=None,
        )
        if runtime is None:
            raise TypeError("an initialized Plane runtime is required")
        self._agents = PlaneRepositoryContext(
            repository=plane_repository or repository,
            plane_runtime=runtime,
        )
        self._policy = compatibility_policy
        self._operations = operation_repository
        self._operation_retention = operation_retention
        self._uuid_factory = uuid_factory

    def _new_uuid(self, field_name: str) -> str:
        return _uuid4_text(self._uuid_factory(), field_name)

    @staticmethod
    def _host_from_plane(record: PlaneHostSessionRecord) -> HostSessionRecord:
        return HostSessionRecord(
            host_session_id=record.host_session_id,
            host_id=record.host_id,
            owner_user_id=record.owner_id,
            connection_scope_id=record.connection_scope_id,
            platform=record.platform,
            client_version=record.client_version,
            host_generation=record.host_generation,
            supersedes_session_id=record.supersedes_session_id,
            supported_runtime_contract_versions=(
                record.supported_runtime_contract_versions
            ),
            runtime_contract_version=record.runtime_contract_version,
            release_lock_digest=record.release_lock_digest,
            state=record.state,
            inventory_state=record.inventory_state,
            eligible_since=record.eligible_since,
            accepted_at=record.accepted_at,
            last_seen_at=record.last_seen_at,
            disconnected_at=record.disconnected_at,
            inventory_reconciled_at=record.inventory_reconciled_at,
            failure_code=record.failure_code,
        )

    @staticmethod
    def _revision_from_plane(
        record: PlaneAgentRevisionRecord,
    ) -> AgentRevisionRecord:
        required = (
            record.artifact_digest,
            record.manifest,
            record.artifact_relative_path,
            record.runtime_contract_version,
            record.release_lock_digest,
            record.promotion_token,
        )
        if any(value is None for value in required):
            raise PersonalAgentRuntimeError("agent revision record is incomplete")
        return AgentRevisionRecord(
            revision_id=record.revision_id,
            agent_id=record.agent_id,
            owner_user_id=record.owner_id,
            revision_number=record.revision_number,
            parent_revision_id=record.parent_revision_id,
            previous_good_revision_id=record.previous_good_revision_id,
            artifact_digest=str(record.artifact_digest),
            manifest=_frozen_json(record.manifest),
            artifact_relative_path=str(record.artifact_relative_path),
            runtime_contract_version=int(record.runtime_contract_version),
            release_lock_digest=str(record.release_lock_digest),
            compatibility_state=record.compatibility_state,
            state=record.state,
            promotion_token=str(record.promotion_token),
            state_revision=record.state_revision,
        )

    @staticmethod
    def _runtime_from_plane(
        record: PlaneRuntimeInstanceRecord,
        *,
        agent: PlaneUserAgentRecord | None = None,
    ) -> RuntimeInstanceRecord:
        fence = RuntimeFence(
            agent_id=record.agent_id,
            host_id=record.host_id,
            host_session_id=record.host_session_id,
            delivery_id=record.delivery_id,
            revision_id=record.revision_id,
            runtime_instance_id=record.runtime_instance_id,
            process_id=record.process_id,
            lifecycle_generation=record.lifecycle_generation,
        )
        fence.validate(allow_prelaunch=fence.process_id is None)
        return RuntimeInstanceRecord(
            fence=fence,
            operation_id=record.operation_id,
            operation_execution_generation=(
                record.operation_execution_generation
            ),
            state=record.state,
            is_authoritative=record.is_authoritative,
            state_revision=record.state_revision,
            created_at=record.created_at,
            started_at=record.started_at,
            registered_at=record.registered_at,
            last_heartbeat_sequence=record.last_heartbeat_sequence,
            ready_at=record.ready_at,
            last_liveness_at=record.last_liveness_at,
            terminal_at=record.terminal_at,
            failure_code=record.failure_code,
            active_revision_id=(
                None if agent is None else agent.active_revision_id
            ),
            authoritative_instance_id=(
                None if agent is None else agent.authoritative_instance_id
            ),
        )

    @classmethod
    def _request_from_plane(
        cls,
        record: PlaneRuntimeRequestRecord,
        *,
        runtime: PlaneRuntimeInstanceRecord,
        operation_execution_lease_token: str | None,
    ) -> RuntimeRequestRecord:
        if record.operation_id is None:
            raise PersonalAgentRuntimeError(
                "runtime request operation identity is unavailable"
            )
        runtime_record = cls._runtime_from_plane(runtime)
        return RuntimeRequestRecord(
            fence=RuntimeRequestFence(
                runtime=runtime_record.fence,
                request_id=record.request_id,
                request_generation=record.request_generation,
                operation_id=record.operation_id,
                operation_execution_generation=(
                    record.operation_execution_generation
                ),
                operation_execution_lease_token=(
                    operation_execution_lease_token
                ),
            ),
            state=record.state,
            state_revision=record.state_revision,
            assigned_at=record.assigned_at,
            terminal_at=record.terminal_at,
            terminal_code=record.terminal_code,
            result_digest=record.result_digest,
        )

    @staticmethod
    def _validate_host_registration(
        *,
        owner_user_id: Any,
        connection_scope_id: Any,
        host_id: Any,
        platform: Any,
        client_version: Any,
        supported_runtime_contract_versions: Any,
        runtime_lock_sha256: Any,
    ) -> tuple[str, str, str, str, str, tuple[int, ...], str]:
        fields = (
            ("owner_user_id", lambda: _required_text(owner_user_id, "owner_user_id")),
            (
                "connection_scope_id",
                lambda: _uuid4_text(connection_scope_id, "connection_scope_id"),
            ),
            ("host_id", lambda: _uuid4_text(host_id, "host_id")),
        )
        validated: list[str] = []
        for field_name, validator in fields:
            try:
                validated.append(validator())
            except ValueError as exc:
                raise HostRegistrationRefused(
                    "invalid_host_registration", {"field": field_name}
                ) from exc
        if platform not in {"windows", "macos"}:
            raise HostRegistrationRefused(
                "invalid_host_registration", {"field": "platform"}
            )
        if (
            not isinstance(client_version, str)
            or len(client_version) > 128
            or not _STRICT_SEMVER_RE.fullmatch(client_version)
        ):
            raise HostRegistrationRefused(
                "invalid_host_registration", {"field": "client_version"}
            )
        versions = supported_runtime_contract_versions
        if (
            not isinstance(versions, Sequence)
            or isinstance(versions, (str, bytes))
            or not versions
            or any(type(item) is not int or item <= 0 for item in versions)
            or len(set(versions)) != len(versions)
            or len(versions) > 32
        ):
            raise HostRegistrationRefused(
                "invalid_host_registration",
                {"field": "supported_runtime_contract_versions"},
            )
        try:
            lock_digest = _sha256(runtime_lock_sha256, "runtime_lock_sha256")
        except ValueError as exc:
            raise HostRegistrationRefused(
                "invalid_host_registration", {"field": "runtime_lock_sha256"}
            ) from exc
        return (
            validated[0],
            validated[1],
            validated[2],
            platform,
            client_version,
            tuple(sorted(versions)),
            lock_digest,
        )

    def register_host_session(
        self,
        *,
        owner_user_id: str,
        connection_scope_id: str,
        host_id: str,
        platform: str,
        client_version: str,
        supported_runtime_contract_versions: Sequence[int],
        runtime_lock_sha256: str,
    ) -> HostSessionRecord:
        """Validate first, then allocate and persist one server-owned session."""

        (
            owner_user_id,
            connection_scope_id,
            host_id,
            platform,
            client_version,
            versions,
            runtime_lock_sha256,
        ) = self._validate_host_registration(
            owner_user_id=owner_user_id,
            connection_scope_id=connection_scope_id,
            host_id=host_id,
            platform=platform,
            client_version=client_version,
            supported_runtime_contract_versions=supported_runtime_contract_versions,
            runtime_lock_sha256=runtime_lock_sha256,
        )
        required_version = self._policy.runtime_contract_version
        if required_version not in versions:
            raise HostRegistrationRefused(
                "runtime_contract_unsupported",
                {
                    "required_runtime_contract_version": required_version,
                    "supported_runtime_contract_versions": list(versions),
                },
            )
        if runtime_lock_sha256 != self._policy.runtime_lock_sha256:
            raise HostRegistrationRefused(
                "runtime_lock_mismatch",
                {
                    "expected_sha256_prefix": self._policy.runtime_lock_sha256[:12],
                    "actual_sha256_prefix": runtime_lock_sha256[:12],
                },
            )

        session_id = self._new_uuid("host_session_id")
        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            prior_rows = list(
                repository.list_host_sessions(
                    transaction,
                    owner_id=owner_user_id,
                    host_id=host_id,
                    for_update=True,
                    limit=1000,
                )
            )
            previous = prior_rows[0] if prior_rows else None
            generation = previous.host_generation + 1 if previous else 1
            if generation > _MAX_GENERATION:
                raise PersonalAgentRuntimeError("host generation exhausted")
            current_time = datetime.now(UTC)
            eligible_since = (
                min(row.eligible_since for row in prior_rows)
                if previous
                else current_time
            )
            prior_session_ids = [
                row.host_session_id
                for row in prior_rows
                if row.state == "connected"
            ]
            try:
                for prior_session_id in prior_session_ids:
                    repository.transition_host_session(
                        transaction,
                        owner_id=owner_user_id,
                        host_session_id=prior_session_id,
                        expected_state="connected",
                        updates={
                            "state": "disconnected",
                            "disconnected_at": current_time,
                            "last_seen_at": current_time,
                            "failure_code": "host_lost",
                        },
                    )
                accepted = repository.create_host_session(
                    transaction,
                    host_session_id=session_id,
                    host_id=host_id,
                    owner_id=owner_user_id,
                    connection_scope_id=connection_scope_id,
                    platform=platform,
                    client_version=client_version,
                    host_generation=generation,
                    supersedes_session_id=(
                        None if previous is None else previous.host_session_id
                    ),
                    supported_runtime_contract_versions=versions,
                    runtime_contract_version=required_version,
                    release_lock_digest=runtime_lock_sha256,
                    eligible_since=eligible_since,
                    accepted_at=current_time,
                    last_seen_at=current_time,
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "host registration generation is stale"
                ) from exc

            # Superseding the same stable host is a host-loss boundary for its
            # old sessions. Settle those exact runtimes before rebinding sticky
            # agent pointers to the new, inventory-pending server session.
            if prior_session_ids:
                self._terminalize_session_instances_plane(
                    transaction,
                    owner_user_id=owner_user_id,
                    host_session_ids=prior_session_ids,
                    failure_code="host_lost",
                )
                for agent in repository.list_agents(
                    transaction,
                    owner_id=owner_user_id,
                    include_deleted=True,
                    limit=2000,
                ):
                    if (
                        agent.deleted_at is not None
                        or agent.selected_host_session_id not in prior_session_ids
                    ):
                        continue
                    locked_agent = repository.get_agent(
                        transaction,
                        owner_id=owner_user_id,
                        agent_id=agent.agent_id,
                        for_update=True,
                    )
                    if locked_agent is not None:
                        self._select_host_plane(transaction, locked_agent)
        return self._host_from_plane(accepted)

    @staticmethod
    def _inventory_entry(value: Any) -> HostInventoryEntry:
        if isinstance(value, HostInventoryEntry):
            raw = {
                "agent_id": value.agent_id,
                "revision_id": value.revision_id,
                "bundle_sha256": value.bundle_sha256,
                "runtime_contract_version": value.runtime_contract_version,
                "required_runtime_lock_sha256": value.required_runtime_lock_sha256,
            }
        elif isinstance(value, Mapping):
            raw = dict(value)
        else:
            raise ValueError("inventory entry must be an object")
        required = {
            "agent_id",
            "revision_id",
            "bundle_sha256",
            "runtime_contract_version",
            "required_runtime_lock_sha256",
        }
        if set(raw) != required:
            raise ValueError("inventory entry fields are invalid")
        agent_id = _required_text(raw["agent_id"], "agent_id", maximum=255)
        revision_id = _uuid4_text(raw["revision_id"], "revision_id")
        bundle_sha256 = _sha256(raw["bundle_sha256"], "bundle_sha256")
        runtime_contract_version = raw["runtime_contract_version"]
        if type(runtime_contract_version) is not int or runtime_contract_version <= 0:
            raise ValueError("runtime_contract_version must be a positive integer")
        required_runtime_lock_sha256 = _sha256(
            raw["required_runtime_lock_sha256"],
            "required_runtime_lock_sha256",
        )
        return HostInventoryEntry(
            agent_id=agent_id,
            revision_id=revision_id,
            bundle_sha256=bundle_sha256,
            runtime_contract_version=runtime_contract_version,
            required_runtime_lock_sha256=required_runtime_lock_sha256,
        )

    @classmethod
    def _inventory_entries(
        cls, entries: Sequence[Any]
    ) -> tuple[HostInventoryEntry, ...]:
        if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
            raise ValueError("inventory entries must be an array")
        if len(entries) > 1_000:
            raise ValueError("inventory exceeds 1000 entries")
        validated = tuple(cls._inventory_entry(value) for value in entries)
        keys = {(entry.agent_id, entry.revision_id) for entry in validated}
        if len(keys) != len(validated):
            raise ValueError("inventory entries must have unique agent/revision pairs")
        return validated

    @staticmethod
    def _inventory_action_without_delivery(
        entry: HostInventoryEntry, *, action: str, reason_code: str
    ) -> HostInventoryAction:
        return HostInventoryAction(
            agent_id=entry.agent_id,
            revision_id=entry.revision_id,
            action=action,
            reason_code=reason_code,
            selected_delivery=None,
        )

    def reconcile_host_inventory(
        self,
        fence: HostSessionFence,
        *,
        inventory_id: str,
        entries: Sequence[Any],
        delivery_operation_fences: Optional[
            Mapping[tuple[str, str], ExecutionFence]
        ] = None,
    ) -> HostInventoryReconciliation:
        """Validate, decide, allocate selected deliveries, and commit as one unit.

        A start action is possible only for an exact retained active revision on
        this selected server-issued session.  Its running delivery operation
        must be supplied under the same ``(agent_id, revision_id)`` key.  Any
        malformed entry, missing/extra operation fence, stale pointer, or failed
        allocation rolls the whole transaction back and leaves inventory
        pending, so the host cannot start a partial response.
        """

        inventory_id = _uuid4_text(inventory_id, "inventory_id")
        validated_entries = self._inventory_entries(entries)
        supplied_operations = dict(delivery_operation_fences or {})
        for key, operation_fence in supplied_operations.items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or not isinstance(key[0], str)
                or not isinstance(key[1], str)
                or not isinstance(operation_fence, ExecutionFence)
            ):
                raise ValueError("delivery operation fence mapping is invalid")

        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=fence.owner_user_id)
            host = repository.get_host_session(
                transaction,
                owner_id=fence.owner_user_id,
                host_session_id=fence.host_session_id,
                for_update=True,
            )
            if host is None or self._host_from_plane(host).fence != fence:
                raise StaleRuntimeGenerationError("host session fence is stale")
            if host.state != "connected":
                raise StaleRuntimeGenerationError("host session is disconnected")
            if host.inventory_state != "pending":
                raise StaleRuntimeGenerationError("host inventory is not pending")
            if not (
                host.runtime_contract_version
                == self._policy.runtime_contract_version
                and host.release_lock_digest == self._policy.runtime_lock_sha256
            ):
                raise StaleRuntimeGenerationError("host compatibility fence is stale")

            agent_ids = sorted({entry.agent_id for entry in validated_entries})
            agents: dict[str, PlaneUserAgentRecord] = {}
            for agent_id in agent_ids:
                agent = repository.get_agent(
                    transaction,
                    owner_id=fence.owner_user_id,
                    agent_id=agent_id,
                    for_update=True,
                )
                if agent is not None:
                    # A retained bundle of a live agent whose host went away
                    # follows the same sticky selection delivery used, so the
                    # restarted desktop gets a start action instead of
                    # keep_stopped/host_not_selected forever.
                    agents[agent_id] = self._ensure_host_selection(transaction, agent)

            revisions: dict[str, PlaneAgentRevisionRecord] = {}
            for entry in validated_entries:
                if entry.agent_id not in agents:
                    continue
                revision = repository.get_revision(
                    transaction,
                    owner_id=fence.owner_user_id,
                    agent_id=entry.agent_id,
                    revision_id=entry.revision_id,
                    for_update=True,
                )
                if revision is not None:
                    revisions[entry.revision_id] = revision

            decisions: list[
                tuple[
                    HostInventoryEntry,
                    HostInventoryAction,
                    Optional[PlaneAgentRevisionRecord],
                ]
            ] = []
            start_keys: set[tuple[str, str]] = set()
            for entry in validated_entries:
                agent = agents.get(entry.agent_id)
                revision = revisions.get(entry.revision_id)
                if agent is None:
                    action = self._inventory_action_without_delivery(
                        entry, action="delete", reason_code="agent_unknown"
                    )
                elif agent.deleted_at is not None:
                    action = self._inventory_action_without_delivery(
                        entry, action="delete", reason_code="agent_deleted"
                    )
                elif revision is None:
                    action = self._inventory_action_without_delivery(
                        entry, action="delete", reason_code="revision_unknown"
                    )
                elif revision.artifact_digest != entry.bundle_sha256:
                    action = self._inventory_action_without_delivery(
                        entry, action="delete", reason_code="bundle_digest_mismatch"
                    )
                elif (
                    int(revision.runtime_contract_version or 0)
                    != entry.runtime_contract_version
                    or entry.runtime_contract_version
                    != self._policy.runtime_contract_version
                ):
                    action = self._inventory_action_without_delivery(
                        entry,
                        action="delete",
                        reason_code="runtime_contract_unsupported",
                    )
                elif (
                    revision.release_lock_digest
                    != entry.required_runtime_lock_sha256
                    or entry.required_runtime_lock_sha256
                    != self._policy.runtime_lock_sha256
                ):
                    action = self._inventory_action_without_delivery(
                        entry, action="delete", reason_code="runtime_lock_mismatch"
                    )
                elif revision.compatibility_state != "compatible":
                    action = self._inventory_action_without_delivery(
                        entry, action="delete", reason_code="revision_incompatible"
                    )
                elif revision.state in {"failed", "retired", "legacy_pending"}:
                    action = self._inventory_action_without_delivery(
                        entry, action="delete", reason_code="revision_obsolete"
                    )
                elif (
                    agent.active_revision_id != entry.revision_id
                    or revision.state != "active"
                ):
                    action = self._inventory_action_without_delivery(
                        entry, action="keep_stopped", reason_code="revision_not_active"
                    )
                elif (
                    agent.selected_host_session_id != fence.host_session_id
                ):
                    action = self._inventory_action_without_delivery(
                        entry, action="keep_stopped", reason_code="host_not_selected"
                    )
                else:
                    key = (entry.agent_id, entry.revision_id)
                    start_keys.add(key)
                    action = HostInventoryAction(
                        agent_id=entry.agent_id,
                        revision_id=entry.revision_id,
                        action="start",
                        reason_code=None,
                        selected_delivery=None,
                    )
                decisions.append((entry, action, revision))

            if set(supplied_operations) != start_keys:
                raise ValueError(
                    "delivery operations must exactly match inventory start actions"
                )
            for key in sorted(start_keys):
                self._assert_operation_locked(
                    transaction,
                    supplied_operations[key],
                    owner_user_id=fence.owner_user_id,
                )

            actions: list[HostInventoryAction] = []
            for entry, action, revision in decisions:
                if action.action != "start":
                    actions.append(action)
                    continue
                agent = agents[entry.agent_id]
                if revision is None:  # pragma: no cover - decision invariant
                    raise RuntimeError("start action has no revision")
                instance = self._create_prelaunch_instance_plane(
                    transaction,
                    agent=agent,
                    host=host,
                    revision=revision,
                    operation_fence=supplied_operations[
                        (entry.agent_id, entry.revision_id)
                    ],
                    allow_inventory_pending=True,
                    operation_already_validated=True,
                )
                actions.append(
                    HostInventoryAction(
                        agent_id=entry.agent_id,
                        revision_id=entry.revision_id,
                        action="start",
                        reason_code=None,
                        selected_delivery=HostInventorySelectedDelivery(
                            delivery_id=instance.fence.delivery_id,
                            runtime_instance_id=instance.fence.runtime_instance_id,
                            lifecycle_generation=instance.fence.lifecycle_generation,
                            runtime_contract_version=self._policy.runtime_contract_version,
                            required_runtime_lock_sha256=self._policy.runtime_lock_sha256,
                            bundle_sha256=entry.bundle_sha256,
                        ),
                    )
                )

            observed_at = datetime.now(UTC)
            try:
                reconciled_host = repository.transition_host_session(
                    transaction,
                    owner_id=fence.owner_user_id,
                    host_session_id=fence.host_session_id,
                    expected_state="connected",
                    updates={
                        "inventory_state": "reconciled",
                        "inventory_reconciled_at": observed_at,
                        "last_seen_at": observed_at,
                        "failure_code": None,
                    },
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "host inventory fence is stale"
                ) from exc
        host_record = self._host_from_plane(reconciled_host)
        return HostInventoryReconciliation(
            host=host_record,
            inventory_id=inventory_id,
            actions=tuple(actions),
            reconciled_at=host_record.inventory_reconciled_at,
        )

    def mark_inventory_reconciled(
        self, fence: HostSessionFence
    ) -> HostSessionRecord:
        """Commit an explicitly empty inventory (compatibility convenience)."""

        result = self.reconcile_host_inventory(
            fence,
            inventory_id=self._new_uuid("inventory_id"),
            entries=(),
        )
        return result.host

    def _select_host_plane(
        self,
        transaction: Any,
        agent: PlaneUserAgentRecord,
    ) -> HostSelection:
        repository = self._agents.repository
        previous_session_id = agent.selected_host_session_id
        selected = None
        if previous_session_id is not None:
            previous = repository.get_host_session(
                transaction,
                owner_id=agent.owner_id,
                host_session_id=previous_session_id,
            )
            if previous is not None:
                same_host = repository.list_host_sessions(
                    transaction,
                    owner_id=agent.owner_id,
                    state="connected",
                    host_id=previous.host_id,
                    limit=1000,
                )
                selected = same_host[0] if same_host else None
        if selected is None:
            connected = repository.list_host_sessions(
                transaction,
                owner_id=agent.owner_id,
                state="connected",
                limit=1000,
            )
            selected = (
                None
                if not connected
                else min(
                    connected,
                    key=lambda host: (
                        host.eligible_since,
                        host.host_id,
                        host.host_session_id,
                    ),
                )
            )
        selected_session_id = None if selected is None else selected.host_session_id
        changed = previous_session_id != selected_session_id
        lifecycle_generation = agent.lifecycle_generation
        if changed:
            lifecycle_generation = max(
                agent.generation_counter,
                agent.lifecycle_generation,
            ) + 1
            if lifecycle_generation > _MAX_GENERATION:
                raise PersonalAgentRuntimeError(
                    "agent lifecycle generation exhausted"
                )
            try:
                repository.compare_and_set_agent(
                    transaction,
                    owner_id=agent.owner_id,
                    agent_id=agent.agent_id,
                    expected_revision=agent.state_revision,
                    updates={
                        "selected_host_session_id": selected_session_id,
                        "generation_counter": lifecycle_generation,
                        "lifecycle_generation": lifecycle_generation,
                        "updated_at": _now_ms(),
                    },
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "agent selection revision is stale"
                ) from exc
        return HostSelection(
            session=(None if selected is None else self._host_from_plane(selected)),
            previous_session_id=previous_session_id,
            changed=changed,
            lifecycle_generation=lifecycle_generation,
        )

    def _ensure_host_selection(
        self, transaction: Any, agent: PlaneUserAgentRecord
    ) -> PlaneUserAgentRecord:
        """Re-select a host for a live agent whose selected session is gone.

        The selection is made at delivery and cleared when that host session
        is lost; nothing re-made it, so a personal agent never came back after
        its desktop client restarted — inventory reconciliation answered
        ``host_not_selected`` for every retained bundle, forever (feature 077
        live finding). This applies the same sticky rule delivery uses
        (:meth:`_select_host_plane`: the same ``host_id``'s new session first,
        else the longest-connected host) whenever the agent is live with an
        active revision and its selected session is absent or not connected.
        Returns the re-read agent record.
        """
        if agent.deleted_at is not None or agent.active_revision_id is None:
            return agent
        repository = self._agents.repository
        selected_id = agent.selected_host_session_id
        if selected_id is not None:
            current = repository.get_host_session(
                transaction,
                owner_id=agent.owner_id,
                host_session_id=selected_id,
            )
            if current is not None and current.state == "connected":
                return agent
        selection = self._select_host_plane(transaction, agent)
        if not selection.changed:
            return agent
        refreshed = repository.get_agent(
            transaction,
            owner_id=agent.owner_id,
            agent_id=agent.agent_id,
            for_update=True,
        )
        return agent if refreshed is None else refreshed

    def select_host_for_agent(
        self, *, owner_user_id: str, agent_id: str
    ) -> HostSelection:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        agent_id = _required_text(agent_id, "agent_id", maximum=255)
        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            if agent is None:
                raise PersonalAgentNotFoundError("personal agent not found")
            if agent.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            return self._select_host_plane(transaction, agent)

    def get_selected_session_revision(
        self,
        fence: HostSessionFence,
        *,
        agent_id: str,
    ) -> SelectedSessionRevision:
        """Return the active revision only when ``fence`` is the exact selection.

        This lookup intentionally permits either pending or reconciled inventory
        so the host-frame adapter can determine which retained entries need
        delivery-operation fences before committing one inventory transaction.
        It never makes the session delivery eligible by itself.
        """

        agent_id = _required_text(agent_id, "agent_id", maximum=255)
        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=fence.owner_user_id)
            host = repository.get_host_session(
                transaction,
                owner_id=fence.owner_user_id,
                host_session_id=fence.host_session_id,
                for_update=True,
            )
            if host is None or not (
                host.host_id == _uuid4_text(fence.host_id, "host_id")
                and host.connection_scope_id
                == _uuid4_text(fence.connection_scope_id, "connection_scope_id")
                and host.host_generation == fence.host_generation
            ):
                raise StaleRuntimeGenerationError("host session fence is stale")
            if host.state != "connected" or host.inventory_state not in {
                "pending",
                "reconciled",
            }:
                raise StaleRuntimeGenerationError("host session is not selectable")
            agent = repository.get_agent(
                transaction,
                owner_id=fence.owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            if agent is None:
                raise PersonalAgentNotFoundError("personal agent not found")
            if agent.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            agent = self._ensure_host_selection(transaction, agent)
            if agent.selected_host_session_id != fence.host_session_id:
                raise AgentOfflineError("host session is not selected for this agent")
            active_revision_id = agent.active_revision_id
            if active_revision_id is None:
                raise AgentOfflineError("personal agent has no active revision")
            revision = repository.get_revision(
                transaction,
                owner_id=fence.owner_user_id,
                agent_id=agent_id,
                revision_id=active_revision_id,
            )
            if not (
                revision is not None
                and revision.state == "active"
                and revision.compatibility_state == "compatible"
                and revision.runtime_contract_version
                == self._policy.runtime_contract_version
                and revision.release_lock_digest == self._policy.runtime_lock_sha256
            ):
                raise AgentOfflineError("personal agent has no compatible active revision")
            return SelectedSessionRevision(
                host=self._host_from_plane(host),
                revision=self._revision_from_plane(revision),
                lifecycle_generation=agent.lifecycle_generation,
            )

    @staticmethod
    def _validate_manifest(manifest: Any) -> tuple[Mapping[str, Any], str]:
        if not isinstance(manifest, Mapping):
            raise ValueError("manifest must be an object")
        try:
            detached = _plain_json(manifest)
            canonical = json.dumps(
                detached,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("manifest must be bounded canonical JSON") from exc
        if len(canonical.encode("utf-8")) > 64 * 1024:
            raise ValueError("manifest exceeds 64 KiB")
        return _frozen_json(json.loads(canonical)), canonical

    def create_revision(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        artifact_digest: str,
        manifest: Mapping[str, Any],
        artifact_relative_path: str,
        runtime_contract_version: int,
        release_lock_digest: str,
        parent_revision_id: Optional[str] = None,
    ) -> AgentRevisionRecord:
        """Insert one immutable compatible revision under the owner/agent lock."""

        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        agent_id = _required_text(agent_id, "agent_id", maximum=255)
        artifact_digest = _sha256(artifact_digest, "artifact_digest")
        release_lock_digest = _sha256(
            release_lock_digest, "release_lock_digest"
        )
        if runtime_contract_version != self._policy.runtime_contract_version:
            raise ValueError("revision runtime contract is incompatible")
        if release_lock_digest != self._policy.runtime_lock_sha256:
            raise ValueError("revision runtime lock is incompatible")
        if (
            not isinstance(artifact_relative_path, str)
            or not artifact_relative_path
            or "\\" in artifact_relative_path
            or PurePosixPath(artifact_relative_path).is_absolute()
            or ".." in PurePosixPath(artifact_relative_path).parts
            or len(artifact_relative_path) > 1024
        ):
            raise ValueError("artifact_relative_path must remain beneath revision root")
        immutable_manifest, canonical_manifest = self._validate_manifest(manifest)
        parent_revision_id = (
            None
            if parent_revision_id is None
            else _uuid4_text(parent_revision_id, "parent_revision_id")
        )
        revision_id = self._new_uuid("revision_id")
        promotion_token = self._new_uuid("promotion_token")
        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            if agent is None:
                raise PersonalAgentNotFoundError("personal agent not found")
            if agent.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            if parent_revision_id is not None:
                parent = repository.get_revision(
                    transaction,
                    owner_id=owner_user_id,
                    agent_id=agent_id,
                    revision_id=parent_revision_id,
                )
                if parent is None:
                    raise StaleRuntimeGenerationError("parent revision is stale")
            latest = repository.list_revisions(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                limit=1,
            )
            revision_number = 0 if not latest else latest[0].revision_number + 1
            try:
                revision = repository.create_revision(
                    transaction,
                    revision_id=revision_id,
                    agent_id=agent_id,
                    owner_id=owner_user_id,
                    revision_number=revision_number,
                    parent_revision_id=parent_revision_id,
                    previous_good_revision_id=agent.active_revision_id,
                    artifact_digest=artifact_digest,
                    manifest=json.loads(canonical_manifest),
                    artifact_relative_path=artifact_relative_path,
                    runtime_contract_version=runtime_contract_version,
                    release_lock_digest=release_lock_digest,
                    compatibility_state="compatible",
                    state="prepared",
                    promotion_token=promotion_token,
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "agent revision identity is stale"
                ) from exc
        record = self._revision_from_plane(revision)
        if record.manifest != immutable_manifest:
            raise RuntimeError("persisted revision manifest changed")
        return record

    def _locked_plane_runtime(
        self,
        transaction: Any,
        fence: RuntimeFence,
        *,
        allow_prelaunch: bool,
        require_current_host: bool,
        allow_bound_process_replay: bool = False,
        allow_deleted_agent: bool = False,
        allow_unbound_process_read: bool = False,
    ) -> tuple[
        PlaneRuntimeInstanceRecord,
        PlaneUserAgentRecord,
        PlaneHostSessionRecord,
        PlaneAgentRevisionRecord,
    ]:
        """Lock and validate the typed rows behind one immutable runtime fence.

        ``allow_unbound_process_read`` admits a fence that carries the process
        the host just spawned while the durable instance is still pre-launch
        (``process_id`` unbound): that is exactly the host's first ``starting``
        frame, whose metadata read precedes the process binding. Read-only
        callers only — a mutation still needs the exact fence.
        """

        fence.validate(allow_prelaunch=allow_prelaunch)
        repository = self._agents.repository
        unresolved = repository.get_runtime_instance_for_administration(
            transaction,
            runtime_instance_id=fence.runtime_instance_id,
        )
        if unresolved is None:
            raise StaleRuntimeGenerationError("runtime fence is stale")
        repository.lock_owner(transaction, owner_id=unresolved.owner_id)
        runtime = repository.get_runtime_instance(
            transaction,
            owner_id=unresolved.owner_id,
            runtime_instance_id=fence.runtime_instance_id,
            for_update=True,
        )
        if runtime is None:
            raise StaleRuntimeGenerationError("runtime fence is stale")
        observed_fence = self._runtime_from_plane(runtime).fence
        fence_matches = observed_fence == fence
        if (
            not fence_matches
            and allow_bound_process_replay
            and fence.process_id is None
            and observed_fence.process_id is not None
        ):
            fence_matches = replace(observed_fence, process_id=None) == fence
        if (
            not fence_matches
            and allow_unbound_process_read
            and observed_fence.process_id is None
            and fence.process_id is not None
        ):
            fence_matches = replace(fence, process_id=None) == observed_fence
        if not fence_matches:
            raise StaleRuntimeGenerationError("runtime fence is stale")
        agent = repository.get_agent(
            transaction,
            owner_id=runtime.owner_id,
            agent_id=runtime.agent_id,
            for_update=True,
        )
        if agent is None:
            raise StaleRuntimeGenerationError("runtime agent binding is stale")
        if agent.deleted_at is not None and not allow_deleted_agent:
            raise AgentDeletedError("agent_deleted")
        host = repository.get_host_session(
            transaction,
            owner_id=runtime.owner_id,
            host_session_id=runtime.host_session_id,
            for_update=True,
        )
        revision = repository.get_revision(
            transaction,
            owner_id=runtime.owner_id,
            agent_id=runtime.agent_id,
            revision_id=runtime.revision_id,
            for_update=True,
        )
        if (
            host is None
            or revision is None
            or host.host_id != runtime.host_id
        ):
            raise StaleRuntimeGenerationError("runtime host binding is stale")
        if require_current_host and not (
            host.state == "connected"
            and agent.selected_host_session_id == runtime.host_session_id
        ):
            raise StaleRuntimeGenerationError("selected host session is stale")
        return runtime, agent, host, revision

    def _assert_operation_locked(
        self,
        cursor: Any,
        fence: ExecutionFence,
        *,
        owner_user_id: str,
    ) -> Any:
        try:
            operation = self._operations.assert_current_execution(
                fence, transaction=cursor
            )
        except StaleExecutionFenceError as exc:
            raise StaleRuntimeGenerationError(
                "operation execution fence is stale"
            ) from exc
        if operation.owner_user_id != owner_user_id:
            raise StaleRuntimeGenerationError("operation owner fence is stale")
        return operation

    def _assert_runtime_operation_plane(
        self,
        transaction: Any,
        runtime: PlaneRuntimeInstanceRecord,
    ) -> ExecutionFence:
        if runtime.operation_id is None:
            raise StaleRuntimeGenerationError("runtime operation fence is absent")
        operation = self._operations.get_operation_for_administration(
            uuid.UUID(runtime.operation_id),
            for_update=True,
            transaction=transaction,
        )
        if not (
            operation is not None
            and operation.state is OperationState.RUNNING
            and operation.owner_user_id == runtime.owner_id
            and operation.execution_generation
            == runtime.operation_execution_generation
            and operation.execution_lease_token is not None
        ):
            raise StaleRuntimeGenerationError("runtime operation fence is stale")
        return ExecutionFence(
            operation_id=operation.operation_id,
            execution_generation=operation.execution_generation,
            execution_lease_token=operation.execution_lease_token,
        )

    def _create_prelaunch_instance_plane(
        self,
        transaction: Any,
        *,
        agent: PlaneUserAgentRecord,
        host: PlaneHostSessionRecord,
        revision: PlaneAgentRevisionRecord,
        operation_fence: ExecutionFence,
        allow_inventory_pending: bool,
        operation_already_validated: bool,
    ) -> RuntimeInstanceRecord:
        allowed_inventory_states = (
            {"pending", "reconciled"}
            if allow_inventory_pending
            else {"reconciled"}
        )
        if not (
            agent.selected_host_session_id == host.host_session_id
            and host.owner_id == agent.owner_id
            and host.state == "connected"
            and host.inventory_state in allowed_inventory_states
            and host.runtime_contract_version
            == self._policy.runtime_contract_version
            and host.release_lock_digest == self._policy.runtime_lock_sha256
        ):
            raise StaleRuntimeGenerationError(
                "host session is not delivery eligible"
            )
        if not (
            revision.agent_id == agent.agent_id
            and revision.owner_id == agent.owner_id
            and revision.compatibility_state == "compatible"
            and revision.state in {"prepared", "ready", "active"}
            and revision.runtime_contract_version
            == self._policy.runtime_contract_version
            and revision.release_lock_digest == self._policy.runtime_lock_sha256
        ):
            raise StaleRuntimeGenerationError("revision is not delivery eligible")
        if not operation_already_validated:
            self._assert_operation_locked(
                transaction,
                operation_fence,
                owner_user_id=agent.owner_id,
            )
        runtime_instance_id = self._new_uuid("runtime_instance_id")
        delivery_id = self._new_uuid("delivery_id")
        lifecycle_generation = max(
            agent.generation_counter,
            agent.lifecycle_generation,
        ) + 1
        if lifecycle_generation > _MAX_GENERATION:
            raise PersonalAgentRuntimeError("agent lifecycle generation exhausted")
        try:
            self._agents.repository.compare_and_set_agent(
                transaction,
                owner_id=agent.owner_id,
                agent_id=agent.agent_id,
                expected_revision=agent.state_revision,
                updates={
                    "generation_counter": lifecycle_generation,
                    "updated_at": _now_ms(),
                },
            )
            runtime = self._agents.repository.create_runtime_instance(
                transaction,
                runtime_instance_id=runtime_instance_id,
                agent_id=agent.agent_id,
                owner_id=agent.owner_id,
                host_id=host.host_id,
                host_session_id=host.host_session_id,
                delivery_id=delivery_id,
                revision_id=revision.revision_id,
                lifecycle_generation=lifecycle_generation,
                runtime_contract_version=self._policy.runtime_contract_version,
                operation_id=str(operation_fence.operation_id),
                operation_execution_generation=(
                    operation_fence.execution_generation
                ),
                state="delivering",
            )
        except RepositoryConflictError as exc:
            raise StaleRuntimeGenerationError(
                "runtime generation allocation is stale"
            ) from exc
        return self._runtime_from_plane(runtime)

    def create_prelaunch_instance(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        host_session_id: str,
        revision_id: str,
        operation_fence: ExecutionFence,
    ) -> RuntimeInstanceRecord:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        agent_id = _required_text(agent_id, "agent_id", maximum=255)
        host_session_id = _uuid4_text(host_session_id, "host_session_id")
        revision_id = _uuid4_text(revision_id, "revision_id")
        if not isinstance(operation_fence, ExecutionFence):
            raise TypeError("operation_fence must be ExecutionFence")
        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            if agent is None:
                raise PersonalAgentNotFoundError("personal agent not found")
            if agent.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            host = repository.get_host_session(
                transaction,
                owner_id=owner_user_id,
                host_session_id=host_session_id,
                for_update=True,
            )
            revision = repository.get_revision(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
            if host is None or revision is None:
                raise StaleRuntimeGenerationError("delivery identity is stale")
            return self._create_prelaunch_instance_plane(
                transaction,
                agent=agent,
                host=host,
                revision=revision,
                operation_fence=operation_fence,
                allow_inventory_pending=False,
                operation_already_validated=False,
            )

    def create_selected_recovery_instance(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        operation_fence: ExecutionFence,
    ) -> SelectedRecoveryDelivery:
        """Allocate one fresh recovery runtime for the durable selected standby.

        This is the post-disconnect/failover seam. It resolves the current
        selected reconciled host and already-active revision under the same
        owner transaction that allocates the delivery/runtime generation. A
        current authority or another non-terminal recovery makes the request
        stale, preventing duplicate starts from concurrent disconnect handlers.
        """

        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        agent_id = _required_text(agent_id, "agent_id", maximum=255)
        if not isinstance(operation_fence, ExecutionFence):
            raise TypeError("operation_fence must be ExecutionFence")
        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            if agent is None:
                raise PersonalAgentNotFoundError("personal agent not found")
            if agent.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            host_session_id = agent.selected_host_session_id
            revision_id = agent.active_revision_id
            if host_session_id is None or revision_id is None:
                raise AgentOfflineError(
                    "personal agent has no selected host and active revision"
                )
            if agent.authoritative_instance_id is not None:
                raise StaleRuntimeGenerationError(
                    "personal agent already has an authoritative runtime"
                )
            host = repository.get_host_session(
                transaction,
                owner_id=owner_user_id,
                host_session_id=host_session_id,
                for_update=True,
            )
            revision = repository.get_revision(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
            if not (
                host is not None
                and host.state == "connected"
                and host.inventory_state == "reconciled"
                and revision is not None
                and revision.state == "active"
                and revision.compatibility_state == "compatible"
            ):
                raise AgentOfflineError(
                    "selected standby is not eligible for active revision recovery"
                )
            runtimes = repository.list_runtime_instances(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                host_session_id=host_session_id,
                for_update=True,
                limit=1000,
            )
            if any(
                runtime.revision_id == revision_id
                and runtime.state not in _RUNTIME_TERMINAL_STATES
                for runtime in runtimes
            ):
                raise StaleRuntimeGenerationError(
                    "selected recovery runtime is already pending"
                )
            instance = self._create_prelaunch_instance_plane(
                transaction,
                agent=agent,
                host=host,
                revision=revision,
                operation_fence=operation_fence,
                allow_inventory_pending=False,
                operation_already_validated=False,
            )
            return SelectedRecoveryDelivery(
                host=self._host_from_plane(host),
                revision=self._revision_from_plane(revision),
                instance=instance,
            )

    def bind_runtime_process(
        self,
        fence: RuntimeFence,
        *,
        process_id: str,
        expected_state_revision: int,
    ) -> RuntimeInstanceRecord:
        """Bind the host's logical process UUID exactly once on ``starting``."""

        if fence.process_id is not None:
            raise ValueError("prelaunch fence process_id must be null")
        fence.validate(allow_prelaunch=True)
        process_id = _uuid4_text(process_id, "process_id")
        if type(expected_state_revision) is not int or expected_state_revision < 0:
            raise ValueError("expected_state_revision must be non-negative")
        with self._agents.transaction() as transaction:
            row, agent, host, _revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=True,
                require_current_host=True,
                allow_bound_process_replay=True,
            )
            if host.inventory_state != "reconciled":
                raise StaleRuntimeGenerationError("selected host session is stale")
            existing_process_id = row.process_id
            if existing_process_id is not None:
                if existing_process_id == process_id and row.state in {
                    "starting",
                    "ready",
                    "online",
                    "updating",
                    "stopping",
                    "stopped",
                    "failed",
                    "offline",
                }:
                    return self._runtime_from_plane(row, agent=agent)
                raise StaleRuntimeGenerationError("runtime process is already bound")
            if not (
                row.state == "delivering"
                and row.state_revision == expected_state_revision
            ):
                raise StaleRuntimeGenerationError("prelaunch runtime revision is stale")
            self._assert_runtime_operation_plane(transaction, row)
            try:
                updated = self._agents.repository.transition_runtime_instance(
                    transaction,
                    owner_id=row.owner_id,
                    runtime_instance_id=row.runtime_instance_id,
                    expected_revision=expected_state_revision,
                    expected_states=("delivering",),
                    updates={
                        "process_id": process_id,
                        "state": "starting",
                        "started_at": datetime.now(UTC),
                    },
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "prelaunch bind CAS is stale"
                ) from exc
        return self._runtime_from_plane(updated, agent=agent)

    def accept_runtime_registration(
        self,
        fence: RuntimeFence,
        *,
        runtime_contract_version: int,
        bundle_sha256: str,
    ) -> RuntimeInstanceRecord:
        """Durably accept the exact bound child's first registration frame."""

        fence.validate(allow_prelaunch=False)
        bundle_sha256 = _sha256(bundle_sha256, "bundle_sha256")
        with self._agents.transaction() as transaction:
            row, agent, _host, revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=False,
                require_current_host=True,
            )
            if not (
                runtime_contract_version == row.runtime_contract_version
                == self._policy.runtime_contract_version
                and bundle_sha256 == revision.artifact_digest
                and revision.release_lock_digest == self._policy.runtime_lock_sha256
                and revision.runtime_contract_version
                == self._policy.runtime_contract_version
            ):
                raise StaleRuntimeGenerationError(
                    "runtime registration compatibility fence is stale"
                )
            if row.registered_at is not None:
                return self._runtime_from_plane(row, agent=agent)
            if row.state != "starting":
                raise StaleRuntimeGenerationError("runtime registration state is stale")
            self._assert_runtime_operation_plane(transaction, row)
            try:
                updated = self._agents.repository.transition_runtime_instance(
                    transaction,
                    owner_id=row.owner_id,
                    runtime_instance_id=row.runtime_instance_id,
                    expected_revision=row.state_revision,
                    expected_states=("starting",),
                    updates={"registered_at": datetime.now(UTC)},
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "runtime registration CAS is stale"
                ) from exc
        return self._runtime_from_plane(updated, agent=agent)

    def record_runtime_heartbeat(
        self,
        fence: RuntimeFence,
        *,
        heartbeat_sequence: int,
    ) -> RuntimeInstanceRecord:
        """Advance durable liveness only for a strictly larger sequence."""

        fence.validate(allow_prelaunch=False)
        if (
            type(heartbeat_sequence) is not int
            or heartbeat_sequence <= 0
            or heartbeat_sequence > _MAX_GENERATION
        ):
            raise ValueError("heartbeat_sequence must be a positive BIGINT")
        with self._agents.transaction() as transaction:
            row, agent, _host, _revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=False,
                require_current_host=True,
            )
            if row.registered_at is None or row.state not in {
                "starting",
                "ready",
                "online",
                "updating",
            }:
                raise StaleRuntimeGenerationError(
                    "heartbeat arrived before accepted registration"
                )
            current_sequence = row.last_heartbeat_sequence
            if current_sequence is not None and heartbeat_sequence <= current_sequence:
                return self._runtime_from_plane(row, agent=agent)
            try:
                updated = self._agents.repository.transition_runtime_instance(
                    transaction,
                    owner_id=row.owner_id,
                    runtime_instance_id=row.runtime_instance_id,
                    expected_revision=row.state_revision,
                    expected_states=("starting", "ready", "online", "updating"),
                    updates={
                        "last_heartbeat_sequence": heartbeat_sequence,
                        "last_liveness_at": datetime.now(UTC),
                    },
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError("heartbeat CAS is stale") from exc
        return self._runtime_from_plane(updated, agent=agent)

    def mark_runtime_ready(self, fence: RuntimeFence) -> RuntimeInstanceRecord:
        """Accept ready only after durable registration and liveness proof."""

        fence.validate(allow_prelaunch=False)
        with self._agents.transaction() as transaction:
            row, agent, _host, _revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=False,
                require_current_host=True,
            )
            if row.state == "ready":
                return self._runtime_from_plane(row, agent=agent)
            if not (
                row.state == "starting"
                and row.registered_at is not None
                and row.last_heartbeat_sequence is not None
                and row.last_liveness_at is not None
            ):
                raise StaleRuntimeGenerationError(
                    "runtime is not registered and live"
                )
            self._assert_runtime_operation_plane(transaction, row)
            try:
                updated = self._agents.repository.transition_runtime_instance(
                    transaction,
                    owner_id=row.owner_id,
                    runtime_instance_id=row.runtime_instance_id,
                    expected_revision=row.state_revision,
                    expected_states=("starting",),
                    updates={"state": "ready", "ready_at": datetime.now(UTC)},
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError("ready CAS is stale") from exc
        return self._runtime_from_plane(updated, agent=agent)

    def promote_recovered_runtime(
        self, fence: RuntimeFence
    ) -> RuntimeInstanceRecord:
        """Atomically restore authority for a retained already-active revision.

        Inventory recovery creates a fresh fenced runtime for the immutable
        revision already named by ``active_revision_id``.  Once that child has
        registered, proved liveness, and reached ready, this transition installs
        only the new runtime/lifecycle authority.  It deliberately does not
        mutate revision state or the last-known-good revision pointer.
        """

        fence.validate(allow_prelaunch=False)
        with self._agents.transaction() as transaction:
            row, agent, host, revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=False,
                require_current_host=True,
            )
            already_authoritative = (
                row.state == "online"
                and row.is_authoritative
                and agent.authoritative_instance_id == fence.runtime_instance_id
                and agent.lifecycle_generation == fence.lifecycle_generation
            )
            if already_authoritative:
                # New promotions settle atomically below. Repair a runtime
                # promoted by an older build only when its exact delivery
                # operation is still current; an already-terminal operation is
                # the normal idempotent replay path.
                try:
                    replay_operation = self._assert_runtime_operation_plane(
                        transaction, row
                    )
                except StaleRuntimeGenerationError:
                    return self._runtime_from_plane(row, agent=agent)
                self._operations.terminalize(
                    replay_operation,
                    state=OperationState.COMPLETED,
                    terminal_code=None,
                    safe_summary=None,
                    retry_after_ms=None,
                    now=None,
                    retention=self._operation_retention,
                    transaction=transaction,
                )
                return self._runtime_from_plane(row, agent=agent)
            if not (
                row.state == "ready"
                and not row.is_authoritative
                and row.registered_at is not None
                and row.last_heartbeat_sequence is not None
                and row.last_liveness_at is not None
                and row.ready_at is not None
                and host.state == "connected"
                and host.inventory_state == "reconciled"
                and agent.selected_host_session_id == fence.host_session_id
                and agent.active_revision_id == fence.revision_id
                and revision.state == "active"
                and revision.compatibility_state == "compatible"
                and agent.generation_counter == fence.lifecycle_generation
                and row.lifecycle_generation == fence.lifecycle_generation
                and agent.authoritative_instance_id is None
            ):
                raise StaleRuntimeGenerationError(
                    "recovered runtime promotion fence is stale"
                )
            operation_fence = self._assert_runtime_operation_plane(
                transaction, row
            )
            observed_at = datetime.now(UTC)
            try:
                promoted = self._agents.repository.transition_runtime_instance(
                    transaction,
                    owner_id=row.owner_id,
                    runtime_instance_id=row.runtime_instance_id,
                    expected_revision=row.state_revision,
                    expected_states=("ready",),
                    updates={"state": "online", "is_authoritative": True},
                )
                updated_agent = self._agents.repository.compare_and_set_agent(
                    transaction,
                    owner_id=agent.owner_id,
                    agent_id=agent.agent_id,
                    expected_revision=agent.state_revision,
                    updates={
                        "authoritative_instance_id": row.runtime_instance_id,
                        "lifecycle_generation": row.lifecycle_generation,
                        "status": "live",
                        "updated_at": int(observed_at.timestamp() * 1000),
                    },
                )
                self._operations.terminalize(
                    operation_fence,
                    state=OperationState.COMPLETED,
                    terminal_code=None,
                    safe_summary=None,
                    retry_after_ms=None,
                    now=None,
                    retention=self._operation_retention,
                    transaction=transaction,
                )
            except (RepositoryConflictError, StaleExecutionFenceError) as exc:
                raise StaleRuntimeGenerationError(
                    "recovered runtime promotion CAS is stale"
                ) from exc
        return self._runtime_from_plane(promoted, agent=updated_agent)

    def get_runtime_instance(self, runtime_instance_id: str) -> RuntimeInstanceRecord:
        runtime_instance_id = _uuid4_text(
            runtime_instance_id, "runtime_instance_id"
        )
        with self._agents.transaction() as transaction:
            record = self._agents.repository.get_runtime_instance_for_administration(
                transaction,
                runtime_instance_id=runtime_instance_id,
            )
            if record is None:
                raise PersonalAgentNotFoundError("runtime instance not found")
            return self._runtime_from_plane(record)

    def stage_runtime_failure(
        self,
        fence: RuntimeFence,
        *,
        failure_code: str,
    ) -> RuntimeInstanceRecord:
        """Revoke runtime authority without claiming its process has exited.

        A host ``failed``/``offline`` state frame precedes process-tree cleanup.
        Persist ``stopping`` first so promotion/routing cannot race that report;
        the exact later exit frame remains the physical-stop commit boundary.
        """

        fence.validate(allow_prelaunch=False)
        failure_code = _safe_code(failure_code)
        with self._agents.transaction() as transaction:
            row, agent, _host, _revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=False,
                require_current_host=False,
            )
            if row.state in _RUNTIME_TERMINAL_STATES:
                return self._runtime_from_plane(row, agent=agent)
            if row.state == "stopping":
                return self._runtime_from_plane(row, agent=agent)
            if row.state not in {"starting", "ready", "online", "updating"}:
                raise StaleRuntimeGenerationError(
                    "runtime failure state is stale"
                )
            try:
                staged = self._agents.repository.transition_runtime_instance(
                    transaction,
                    owner_id=row.owner_id,
                    runtime_instance_id=row.runtime_instance_id,
                    expected_revision=row.state_revision,
                    expected_states=(row.state,),
                    updates={
                        "state": "stopping",
                        "is_authoritative": False,
                        "failure_code": failure_code,
                    },
                )
                updated_agent = agent
                if agent.authoritative_instance_id == row.runtime_instance_id:
                    updated_agent = self._agents.repository.compare_and_set_agent(
                        transaction,
                        owner_id=agent.owner_id,
                        agent_id=agent.agent_id,
                        expected_revision=agent.state_revision,
                        updates={
                            "authoritative_instance_id": None,
                            "updated_at": int(datetime.now(UTC).timestamp() * 1000),
                        },
                    )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "runtime failure staging CAS is stale"
                ) from exc
        return self._runtime_from_plane(staged, agent=updated_agent)

    def get_runtime_revision(self, fence: RuntimeFence) -> AgentRevisionRecord:
        """Resolve immutable revision metadata under the exact runtime fence."""

        fence.validate(allow_prelaunch=fence.process_id is None)
        with self._agents.transaction() as transaction:
            # The host's first ``starting`` frame already names the process it
            # spawned while the instance is still pre-launch on this side; the
            # binding itself happens right after this read (and re-checks the
            # exact pre-launch fence). Feature 077 live finding: without this
            # every real first start was refused as 'runtime fence is stale'.
            _runtime, _agent, _host, revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=fence.process_id is None,
                require_current_host=False,
                allow_unbound_process_read=True,
            )
            return self._revision_from_plane(revision)

    def list_latest_runtime_instances(
        self, *, owner_user_id: str
    ) -> tuple[RuntimeInstanceRecord, ...]:
        """Return the newest durable runtime generation for each live agent.

        This is the reconnect/hydration source for ``agent_lifecycle``.  Socket
        maps are intentionally excluded: a client that was absent for a child
        exit or host loss must still receive the committed terminal state.
        """

        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        with self._agents.transaction() as transaction:
            runtimes = self._agents.repository.list_latest_runtime_instances(
                transaction,
                owner_id=owner_user_id,
                limit=500,
            )
            hydrated = []
            for runtime in runtimes:
                agent = self._agents.repository.get_agent(
                    transaction,
                    owner_id=owner_user_id,
                    agent_id=runtime.agent_id,
                )
                if agent is None or agent.deleted_at is not None:
                    continue
                hydrated.append(self._runtime_from_plane(runtime, agent=agent))
            return tuple(hydrated)

    def list_expired_runtime_candidates(
        self,
        *,
        startup_timeout_seconds: float,
        liveness_timeout_seconds: float,
        limit: int = 1000,
    ) -> tuple[Any, ...]:
        """Discover a bounded cross-owner expiry set using PostgreSQL time.

        Returned rows are advisory only.  The terminalization methods repeat
        the exact owner/state/deadline predicate under a row lock in the same
        transaction as settlement.
        """

        return self._agents.call(
            self._agents.repository.list_expired_runtime_candidates_for_administration,
            startup_timeout_seconds=startup_timeout_seconds,
            liveness_timeout_seconds=liveness_timeout_seconds,
            limit=limit,
        )

    def get_current_online_authority_if_present(
        self, *, owner_user_id: str, agent_id: str
    ) -> RuntimeInstanceRecord | None:
        """Resolve exact authority, distinguishing clean absence from damage.

        Every owner, selected-session, active-revision, lifecycle, compatibility,
        and online-authority relation is checked in the same transaction.  The
        later request assignment repeats these checks under row locks before a
        frame is sent; this lookup never turns a process-local cache into an
        authority. ``None`` is returned only for a known, non-deleted agent with
        no authoritative runtime pointer.
        """

        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        agent_id = _required_text(agent_id, "agent_id", maximum=255)
        with self._agents.transaction() as transaction:
            self._agents.repository.lock_owner(
                transaction,
                owner_id=owner_user_id,
            )
            agent = self._agents.repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
            )
            if agent is None:
                raise PersonalAgentNotFoundError("personal agent not found")
            if agent.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            if agent.authoritative_instance_id is None:
                return None
            runtime_instance_id = agent.authoritative_instance_id
            runtime = self._agents.repository.get_runtime_instance(
                transaction,
                owner_id=owner_user_id,
                runtime_instance_id=runtime_instance_id,
            )
            host = (
                None
                if runtime is None
                else self._agents.repository.get_host_session(
                    transaction,
                    owner_id=owner_user_id,
                    host_session_id=runtime.host_session_id,
                )
            )
            revision = (
                None
                if runtime is None
                else self._agents.repository.get_revision(
                    transaction,
                    owner_id=owner_user_id,
                    agent_id=agent_id,
                    revision_id=runtime.revision_id,
                )
            )
            if not (
                runtime is not None
                and host is not None
                and revision is not None
                and runtime.agent_id == agent_id
                and runtime.process_id is not None
                and runtime.state == "online"
                and runtime.is_authoritative
                and agent.authoritative_instance_id == runtime_instance_id
                and agent.selected_host_session_id == runtime.host_session_id
                and agent.active_revision_id == runtime.revision_id
                and runtime.lifecycle_generation == agent.lifecycle_generation
                and host.host_id == runtime.host_id
                and host.state == "connected"
                and host.inventory_state == "reconciled"
                and host.runtime_contract_version
                == self._policy.runtime_contract_version
                and host.release_lock_digest == self._policy.runtime_lock_sha256
                and revision.state == "active"
                and revision.compatibility_state == "compatible"
                and revision.runtime_contract_version
                == self._policy.runtime_contract_version
                and revision.release_lock_digest == self._policy.runtime_lock_sha256
            ):
                raise AgentOfflineError("personal agent has no exact online authority")
            return self._runtime_from_plane(runtime, agent=agent)

    def get_current_online_authority(
        self, *, owner_user_id: str, agent_id: str
    ) -> RuntimeInstanceRecord:
        """Resolve the one exact routable runtime or raise when it is absent."""

        runtime = self.get_current_online_authority_if_present(
            owner_user_id=owner_user_id,
            agent_id=agent_id,
        )
        if runtime is None:
            raise AgentOfflineError("personal agent has no online authority")
        return runtime

    def assign_request(
        self,
        runtime_fence: RuntimeFence,
        *,
        operation_fence: ExecutionFence,
        request_generation: Optional[str] = None,
    ) -> RuntimeRequestRecord:
        """Persist a call against the exact current online authority before send."""

        runtime_fence.validate(allow_prelaunch=False)
        if not isinstance(operation_fence, ExecutionFence):
            raise TypeError("operation_fence must be ExecutionFence")
        request_id = self._new_uuid("request_id")
        request_generation = (
            self._new_uuid("request_generation")
            if request_generation is None
            else _uuid4_text(request_generation, "request_generation")
        )
        with self._agents.transaction() as transaction:
            row, agent, _host, _revision = self._locked_plane_runtime(
                transaction,
                runtime_fence,
                allow_prelaunch=False,
                require_current_host=True,
            )
            if not (
                row.state == "online"
                and row.is_authoritative
                and agent.authoritative_instance_id
                == runtime_fence.runtime_instance_id
                and agent.active_revision_id == runtime_fence.revision_id
                and agent.lifecycle_generation
                == runtime_fence.lifecycle_generation
                and row.registered_at is not None
                and row.last_heartbeat_sequence is not None
                and row.last_liveness_at is not None
            ):
                raise AgentOfflineError("agent_offline")
            operation = self._assert_operation_locked(
                transaction,
                operation_fence,
                owner_user_id=row.owner_id,
            )
            if operation.request_generation not in {
                None,
                uuid.UUID(request_generation),
            }:
                raise StaleRuntimeGenerationError(
                    "operation request generation is stale"
                )
            try:
                bound_operation = self._operations.bind_request_generation(
                    operation_fence,
                    uuid.UUID(request_generation),
                    transaction=transaction,
                )
                assigned = self._agents.repository.create_runtime_request(
                    transaction,
                    request_id=request_id,
                    request_generation=request_generation,
                    runtime_instance_id=row.runtime_instance_id,
                    agent_id=row.agent_id,
                    owner_id=row.owner_id,
                    operation_id=str(operation_fence.operation_id),
                    operation_execution_generation=(
                        operation_fence.execution_generation
                    ),
                )
            except (
                RepositoryConflictError,
                StaleExecutionFenceError,
                WorkAdmissionConflictError,
            ) as exc:
                raise StaleRuntimeGenerationError(
                    "operation request generation CAS is stale"
                ) from exc
        lease_token = bound_operation.execution_lease_token
        return self._request_from_plane(
            assigned,
            runtime=row,
            operation_execution_lease_token=(
                None if lease_token is None else str(lease_token)
            ),
        )

    def get_runtime_request(self, request_id: str) -> RuntimeRequestRecord:
        request_id = _uuid4_text(request_id, "request_id")
        with self._agents.transaction() as transaction:
            request = self._agents.repository.get_runtime_request_for_administration(
                transaction,
                request_id=request_id,
            )
            if request is None:
                raise PersonalAgentNotFoundError("runtime request not found")
            runtime = self._agents.repository.get_runtime_instance(
                transaction,
                owner_id=request.owner_id,
                runtime_instance_id=request.runtime_instance_id,
            )
            if runtime is None:
                raise PersonalAgentRuntimeError(
                    "runtime request instance is unavailable"
                )
            operation = (
                None
                if request.operation_id is None
                else self._operations.get_operation_for_administration(
                    uuid.UUID(request.operation_id),
                    transaction=transaction,
                )
            )
            lease_token = (
                None
                if operation is None
                else operation.execution_lease_token
            )
            return self._request_from_plane(
                request,
                runtime=runtime,
                operation_execution_lease_token=(
                    None if lease_token is None else str(lease_token)
                ),
            )

    def settle_request(
        self,
        fence: RuntimeRequestFence,
        *,
        state: str,
        terminal_code: Optional[str] = None,
        result_digest: Optional[str] = None,
    ) -> RuntimeRequestRecord:
        """Settle one result and its operation atomically under the full fence."""

        if state not in _REQUEST_TERMINAL_STATES:
            raise ValueError("request terminal state is invalid")
        _uuid4_text(fence.request_id, "request_id")
        _uuid4_text(fence.request_generation, "request_generation")
        _uuid4_text(fence.operation_id, "operation_id")
        fence.runtime.validate(allow_prelaunch=False)
        if state == "completed":
            if terminal_code is not None:
                raise ValueError("completed requests cannot have a terminal code")
            if result_digest is not None:
                result_digest = _sha256(result_digest, "result_digest")
        else:
            terminal_code = _safe_code(terminal_code, "terminal_code")
            if result_digest is not None:
                raise ValueError("non-completed requests cannot have a result digest")
        with self._agents.transaction() as transaction:
            unresolved = (
                self._agents.repository.get_runtime_request_for_administration(
                    transaction,
                    request_id=fence.request_id,
                )
            )
            if unresolved is None:
                raise StaleRuntimeGenerationError("request fence is stale")
            self._agents.repository.lock_owner(
                transaction,
                owner_id=unresolved.owner_id,
            )
            request = self._agents.repository.get_runtime_request(
                transaction,
                owner_id=unresolved.owner_id,
                request_id=fence.request_id,
                for_update=True,
            )
            runtime, agent, _host, _revision = self._locked_plane_runtime(
                transaction,
                fence.runtime,
                allow_prelaunch=False,
                require_current_host=True,
            )
            if not (
                request is not None
                and request.request_generation == fence.request_generation
                and request.runtime_instance_id == runtime.runtime_instance_id
                and request.agent_id == runtime.agent_id
                and request.operation_id == fence.operation_id
                and request.operation_execution_generation
                == fence.operation_execution_generation
            ):
                raise StaleRuntimeGenerationError("request fence is stale")
            if request.state in _REQUEST_TERMINAL_STATES:
                if (
                    request.state == state
                    and request.terminal_code == terminal_code
                    and request.result_digest == result_digest
                ):
                    return self._request_from_plane(
                        request,
                        runtime=runtime,
                        operation_execution_lease_token=(
                            fence.operation_execution_lease_token
                        ),
                    )
                raise StaleRuntimeGenerationError("request is already terminal")
            if agent.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            if not (
                runtime.state == "online"
                and runtime.is_authoritative
                and agent.authoritative_instance_id
                == fence.runtime.runtime_instance_id
                and agent.selected_host_session_id
                == fence.runtime.host_session_id
                and agent.active_revision_id == fence.runtime.revision_id
                and agent.lifecycle_generation
                == fence.runtime.lifecycle_generation
            ):
                raise StaleRuntimeGenerationError("request runtime authority is stale")
            if fence.operation_execution_lease_token is None:
                raise StaleRuntimeGenerationError("operation lease fence is absent")
            operation_fence = ExecutionFence(
                operation_id=uuid.UUID(fence.operation_id),
                execution_generation=fence.operation_execution_generation,
                execution_lease_token=uuid.UUID(
                    fence.operation_execution_lease_token
                ),
            )
            self._assert_operation_locked(
                transaction,
                operation_fence,
                owner_user_id=request.owner_id,
            )
            operation = self._operations.get_operation_for_administration(
                uuid.UUID(fence.operation_id),
                for_update=True,
                transaction=transaction,
            )
            if (
                operation is None
                or operation.request_generation != uuid.UUID(fence.request_generation)
            ):
                raise StaleRuntimeGenerationError(
                    "operation request generation is stale"
                )
            operation_state = {
                "completed": OperationState.COMPLETED,
                "failed": OperationState.FAILED,
                "cancelled": OperationState.CANCELLED,
                "retryable": OperationState.RETRYABLE,
            }[state]
            try:
                settled = self._agents.repository.transition_runtime_request(
                    transaction,
                    owner_id=request.owner_id,
                    request_id=request.request_id,
                    expected_revision=request.state_revision,
                    expected_states=("assigned", "running"),
                    updates={
                        "state": state,
                        "terminal_at": datetime.now(UTC),
                        "terminal_code": terminal_code,
                        "result_digest": result_digest,
                    },
                )
                self._operations.terminalize(
                    operation_fence,
                    state=operation_state,
                    terminal_code=terminal_code,
                    safe_summary=None,
                    retry_after_ms=(0 if state == "retryable" else None),
                    now=None,
                    retention=self._operation_retention,
                    transaction=transaction,
                )
            except (
                RepositoryConflictError,
                StaleExecutionFenceError,
                WorkAdmissionConflictError,
            ) as exc:
                raise StaleRuntimeGenerationError(
                    "request settlement CAS is stale"
                ) from exc
        return self._request_from_plane(
            settled,
            runtime=runtime,
            operation_execution_lease_token=fence.operation_execution_lease_token,
        )

    def _terminalize_request_operations_plane(
        self,
        transaction: Any,
        requests: Sequence[PlaneRuntimeRequestRecord],
        *,
        failure_code: str,
    ) -> None:
        for request in requests:
            if request.operation_id is None:
                continue
            operation = self._operations.get_operation_for_administration(
                uuid.UUID(request.operation_id),
                for_update=True,
                transaction=transaction,
            )
            if not (
                operation is not None
                and operation.state is OperationState.RUNNING
                and operation.execution_lease_token is not None
                and operation.owner_user_id == request.owner_id
                and operation.request_generation
                == uuid.UUID(request.request_generation)
                and operation.execution_generation
                == request.operation_execution_generation
            ):
                continue
            self._operations.terminalize(
                ExecutionFence(
                    operation_id=operation.operation_id,
                    execution_generation=operation.execution_generation,
                    execution_lease_token=operation.execution_lease_token,
                ),
                state=OperationState.RETRYABLE,
                terminal_code=failure_code,
                safe_summary=None,
                retry_after_ms=0,
                now=None,
                retention=self._operation_retention,
                transaction=transaction,
            )

    def _terminalize_runtime_operation_plane(
        self,
        transaction: Any,
        runtime: PlaneRuntimeInstanceRecord,
        *,
        failure_code: str,
        operation_state: OperationState = OperationState.RETRYABLE,
    ) -> None:
        if runtime.operation_id is None:
            return
        operation = self._operations.get_operation_for_administration(
            uuid.UUID(runtime.operation_id),
            for_update=True,
            transaction=transaction,
        )
        if not (
            operation is not None
            and operation.state is OperationState.RUNNING
            and operation.execution_lease_token is not None
            and operation.owner_user_id == runtime.owner_id
            and operation.execution_generation
            == runtime.operation_execution_generation
        ):
            return
        self._operations.terminalize(
            ExecutionFence(
                operation_id=operation.operation_id,
                execution_generation=operation.execution_generation,
                execution_lease_token=operation.execution_lease_token,
            ),
            state=operation_state,
            terminal_code=failure_code,
            safe_summary=None,
            retry_after_ms=(0 if operation_state is OperationState.RETRYABLE else None),
            now=None,
            retention=self._operation_retention,
            transaction=transaction,
        )

    def _terminalize_instance_plane(
        self,
        transaction: Any,
        runtime: PlaneRuntimeInstanceRecord,
        agent: PlaneUserAgentRecord,
        *,
        failure_code: str,
        operation_state: OperationState = OperationState.RETRYABLE,
    ) -> RuntimeSettlement:
        if runtime.state in _RUNTIME_TERMINAL_STATES:
            return RuntimeSettlement(
                instance=self._runtime_from_plane(runtime, agent=agent),
                settled_request_ids=(),
            )
        repository = self._agents.repository
        self._terminalize_runtime_operation_plane(
            transaction,
            runtime,
            failure_code=failure_code,
            operation_state=operation_state,
        )
        requests = repository.list_runtime_requests(
            transaction,
            owner_id=runtime.owner_id,
            runtime_instance_id=runtime.runtime_instance_id,
            states=("assigned", "running"),
            for_update=True,
            limit=500,
        )
        self._terminalize_request_operations_plane(
            transaction,
            requests,
            failure_code=failure_code,
        )
        observed_at = datetime.now(UTC)
        try:
            for request in requests:
                repository.transition_runtime_request(
                    transaction,
                    owner_id=request.owner_id,
                    request_id=request.request_id,
                    expected_revision=request.state_revision,
                    expected_states=("assigned", "running"),
                    updates={
                        "state": "retryable",
                        "terminal_at": observed_at,
                        "terminal_code": failure_code,
                        "result_digest": None,
                    },
                )
            terminal = repository.transition_runtime_instance(
                transaction,
                owner_id=runtime.owner_id,
                runtime_instance_id=runtime.runtime_instance_id,
                expected_revision=runtime.state_revision,
                expected_states=(
                    "delivering",
                    "starting",
                    "ready",
                    "online",
                    "updating",
                    "stopping",
                ),
                updates={
                    "state": "failed" if runtime.process_id is None else "offline",
                    "is_authoritative": False,
                    "terminal_at": observed_at,
                    "failure_code": failure_code,
                },
            )
            updated_agent = agent
            if agent.authoritative_instance_id == runtime.runtime_instance_id:
                updated_agent = repository.compare_and_set_agent(
                    transaction,
                    owner_id=agent.owner_id,
                    agent_id=agent.agent_id,
                    expected_revision=agent.state_revision,
                    updates={
                        "authoritative_instance_id": None,
                        "updated_at": int(observed_at.timestamp() * 1000),
                    },
                )
        except RepositoryConflictError as exc:
            raise StaleRuntimeGenerationError(
                "runtime terminalization CAS is stale"
            ) from exc
        return RuntimeSettlement(
            instance=self._runtime_from_plane(terminal, agent=updated_agent),
            settled_request_ids=tuple(request.request_id for request in requests),
        )

    def terminalize_runtime(
        self, fence: RuntimeFence, *, failure_code: str
    ) -> RuntimeSettlement:
        """Atomically fence one known failed instance and all assigned calls."""

        fence.validate(allow_prelaunch=fence.process_id is None)
        failure_code = _safe_code(failure_code)
        with self._agents.transaction() as transaction:
            row, agent, _host, _revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=fence.process_id is None,
                require_current_host=False,
            )
            if row.state in _RUNTIME_TERMINAL_STATES:
                if row.failure_code == failure_code:
                    return RuntimeSettlement(
                        instance=self._runtime_from_plane(row, agent=agent),
                        settled_request_ids=(),
                    )
                raise StaleRuntimeGenerationError("runtime is already terminal")
            if agent.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            return self._terminalize_instance_plane(
                transaction,
                row,
                agent,
                failure_code=failure_code,
            )

    def record_runtime_physical_exit(
        self,
        fence: RuntimeFence,
        *,
        proof_code: str,
    ) -> RuntimeSettlement:
        """Persist exact process-exit proof under the full runtime fence.

        Revision activation owns the delivery operation disposition while it
        stages a candidate failure, retires a prior runtime, or resets a
        RETRYABLE attempt, so a staged/terminal runtime retains that operation
        outcome.  For an otherwise-live runtime, the exact host exit frame is
        also its failure boundary and settles any still-running delivery
        operation retryably. Tombstoned agents retain ``agent_deleted`` as the
        semantic request/operation disposition while the runtime row records
        the orthogonal physical-exit proof.
        """

        fence.validate(allow_prelaunch=False)
        proof_code = _safe_code(proof_code, "proof_code")
        if proof_code not in {"child_exited", "agent_offline"}:
            raise ValueError("runtime physical-exit proof code is invalid")
        with self._agents.transaction() as transaction:
            row, agent, _host, _revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=False,
                require_current_host=False,
                allow_deleted_agent=True,
            )

            repository = self._agents.repository
            semantic_code = (
                "agent_deleted" if agent.deleted_at is not None else proof_code
            )
            # This helper is itself first-terminal-wins and only mutates a
            # still-RUNNING delivery operation. Permanent FAILED, RETRYABLE,
            # CANCELLED, and COMPLETED lifecycle dispositions remain intact,
            # while a state-only ``stopping`` row cannot strand RUNNING work.
            self._terminalize_runtime_operation_plane(
                transaction,
                row,
                failure_code=semantic_code,
            )
            requests = repository.list_runtime_requests(
                transaction,
                owner_id=row.owner_id,
                runtime_instance_id=row.runtime_instance_id,
                states=("assigned", "running"),
                for_update=True,
                limit=500,
            )
            self._terminalize_request_operations_plane(
                transaction,
                requests,
                failure_code=semantic_code,
            )
            observed_at = datetime.now(UTC)
            try:
                for request in requests:
                    repository.transition_runtime_request(
                        transaction,
                        owner_id=request.owner_id,
                        request_id=request.request_id,
                        expected_revision=request.state_revision,
                        expected_states=("assigned", "running"),
                        updates={
                            "state": "retryable",
                            "terminal_at": observed_at,
                            "terminal_code": semantic_code,
                            "result_digest": None,
                        },
                    )

                if row.state in _RUNTIME_TERMINAL_STATES:
                    terminal = row
                    if row.failure_code != proof_code:
                        terminal = repository.transition_runtime_instance(
                            transaction,
                            owner_id=row.owner_id,
                            runtime_instance_id=row.runtime_instance_id,
                            expected_revision=row.state_revision,
                            expected_states=(row.state,),
                            updates={"failure_code": proof_code},
                        )
                else:
                    terminal = repository.transition_runtime_instance(
                        transaction,
                        owner_id=row.owner_id,
                        runtime_instance_id=row.runtime_instance_id,
                        expected_revision=row.state_revision,
                        expected_states=(row.state,),
                        updates={
                            "state": "offline",
                            "is_authoritative": False,
                            "terminal_at": observed_at,
                            "failure_code": proof_code,
                        },
                    )

                updated_agent = agent
                if (
                    agent.deleted_at is None
                    and agent.authoritative_instance_id == row.runtime_instance_id
                ):
                    updated_agent = repository.compare_and_set_agent(
                        transaction,
                        owner_id=agent.owner_id,
                        agent_id=agent.agent_id,
                        expected_revision=agent.state_revision,
                        updates={
                            "authoritative_instance_id": None,
                            "updated_at": int(observed_at.timestamp() * 1000),
                        },
                    )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "runtime physical-exit CAS is stale"
                ) from exc

            return RuntimeSettlement(
                instance=self._runtime_from_plane(terminal, agent=updated_agent),
                settled_request_ids=tuple(request.request_id for request in requests),
                settlement_code=(
                    semantic_code if semantic_code != proof_code else None
                ),
            )

    def terminalize_expired_startup(
        self,
        fence: RuntimeFence,
        *,
        timeout_seconds: float,
    ) -> RuntimeSettlement:
        """Atomically fail one still-starting runtime after its DB-time deadline.

        This covers both a pre-launch ``delivering`` recovery that never binds a
        process and a bound ``starting`` child that never becomes ready. The
        state/deadline recheck and runtime/delivery-operation settlement share
        one transaction, so a concurrent ready transition wins cleanly instead
        of being killed from a stale process-local timer.
        """

        fence.validate(allow_prelaunch=fence.process_id is None)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= 300
        ):
            raise ValueError("timeout_seconds must be in (0, 300]")
        with self._agents.transaction() as transaction:
            row, agent, _host, _revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=fence.process_id is None,
                require_current_host=False,
            )
            if row.state in _RUNTIME_TERMINAL_STATES:
                if row.failure_code == "child_registration_timeout":
                    return RuntimeSettlement(
                        instance=self._runtime_from_plane(row, agent=agent),
                        settled_request_ids=(),
                    )
                raise StaleRuntimeGenerationError("runtime is already terminal")
            if agent.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            if row.state not in {"delivering", "starting"}:
                raise StaleRuntimeGenerationError(
                    "runtime is no longer awaiting startup"
                )
            expired = self._agents.repository.lock_runtime_if_startup_expired(
                transaction,
                owner_id=row.owner_id,
                runtime_instance_id=row.runtime_instance_id,
                timeout_seconds=float(timeout_seconds),
            )
            if expired is None:
                raise StaleRuntimeGenerationError(
                    "runtime startup deadline has not elapsed"
                )
            return self._terminalize_instance_plane(
                transaction,
                expired,
                agent,
                failure_code="child_registration_timeout",
            )

    def terminalize_expired_liveness(
        self,
        fence: RuntimeFence,
        *,
        timeout_seconds: float = 5.0,
    ) -> RuntimeSettlement:
        """Atomically apply the DB-receipt-time child-hang boundary."""

        fence.validate(allow_prelaunch=False)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= 60
        ):
            raise ValueError("timeout_seconds must be in (0, 60]")
        with self._agents.transaction() as transaction:
            row, agent, _host, _revision = self._locked_plane_runtime(
                transaction,
                fence,
                allow_prelaunch=False,
                require_current_host=False,
            )
            if row.state in _RUNTIME_TERMINAL_STATES:
                if row.failure_code == "child_hung":
                    return RuntimeSettlement(
                        instance=self._runtime_from_plane(row, agent=agent),
                        settled_request_ids=(),
                    )
                raise StaleRuntimeGenerationError("runtime is already terminal")
            if agent.deleted_at is not None:
                raise AgentDeletedError("agent_deleted")
            if row.state not in {"ready", "online", "updating"} or (
                row.last_liveness_at is None
            ):
                raise StaleRuntimeGenerationError(
                    "runtime has no live heartbeat authority"
                )
            expired = self._agents.repository.lock_runtime_if_liveness_expired(
                transaction,
                owner_id=row.owner_id,
                runtime_instance_id=row.runtime_instance_id,
                timeout_seconds=float(timeout_seconds),
            )
            if expired is None:
                raise StaleRuntimeGenerationError(
                    "runtime liveness deadline has not elapsed"
                )
            return self._terminalize_instance_plane(
                transaction,
                expired,
                agent,
                failure_code="child_hung",
            )

    def _terminalize_session_instances_plane(
        self,
        transaction: Any,
        *,
        owner_user_id: str,
        host_session_ids: Sequence[str],
        failure_code: str,
    ) -> tuple[RuntimeSettlement, ...]:
        repository = self._agents.repository
        settlements: list[RuntimeSettlement] = []
        for host_session_id in host_session_ids:
            runtimes = repository.list_runtime_instances(
                transaction,
                owner_id=owner_user_id,
                host_session_id=host_session_id,
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
            for runtime in sorted(
                runtimes,
                key=lambda item: (item.agent_id, item.runtime_instance_id),
            ):
                agent = repository.get_agent(
                    transaction,
                    owner_id=owner_user_id,
                    agent_id=runtime.agent_id,
                    for_update=True,
                )
                if agent is None:
                    raise StaleRuntimeGenerationError(
                        "runtime agent binding is stale"
                    )
                runtime_failure_code = failure_code
                if runtime.state == "stopping" and runtime.failure_code is not None:
                    # Revision activation already persisted the authoritative
                    # candidate disposition before requesting a physical stop.
                    # A concurrent host disconnect must prove the process gone,
                    # but must not rewrite that permanent failure as host_lost or
                    # turn its exact FAILED delivery operation into a retry.
                    runtime_failure_code = _safe_code(runtime.failure_code)
                settlements.append(
                    self._terminalize_instance_plane(
                        transaction,
                        runtime,
                        agent,
                        failure_code=runtime_failure_code,
                    )
                )
        return tuple(settlements)

    def disconnect_host_session(
        self,
        fence: HostSessionFence,
        *,
        failure_code: str = "host_lost",
    ) -> HostDisconnectResult:
        """Persist host loss, settle its exact calls, then select standbys."""

        failure_code = _safe_code(failure_code)
        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=fence.owner_user_id)
            host = repository.get_host_session(
                transaction,
                owner_id=fence.owner_user_id,
                host_session_id=fence.host_session_id,
                for_update=True,
            )
            if host is None or self._host_from_plane(host).fence != fence:
                raise StaleRuntimeGenerationError("host session fence is stale")
            if host.state == "connected":
                observed_at = datetime.now(UTC)
                try:
                    repository.transition_host_session(
                        transaction,
                        owner_id=fence.owner_user_id,
                        host_session_id=fence.host_session_id,
                        expected_state="connected",
                        updates={
                            "state": "disconnected",
                            "disconnected_at": observed_at,
                            "last_seen_at": observed_at,
                            "failure_code": failure_code,
                        },
                    )
                except RepositoryConflictError as exc:
                    raise StaleRuntimeGenerationError(
                        "host disconnect CAS is stale"
                    ) from exc
            elif host.state != "disconnected":
                raise StaleRuntimeGenerationError("host session is incompatible")
            settlements = self._terminalize_session_instances_plane(
                transaction,
                owner_user_id=fence.owner_user_id,
                host_session_ids=(fence.host_session_id,),
                failure_code=failure_code,
            )
            selections: dict[str, Optional[str]] = {}
            for agent in repository.list_agents(
                transaction,
                owner_id=fence.owner_user_id,
                include_deleted=True,
                limit=2000,
            ):
                if (
                    agent.deleted_at is not None
                    or agent.selected_host_session_id != fence.host_session_id
                ):
                    continue
                locked_agent = repository.get_agent(
                    transaction,
                    owner_id=fence.owner_user_id,
                    agent_id=agent.agent_id,
                    for_update=True,
                )
                if locked_agent is None:
                    continue
                selection = self._select_host_plane(transaction, locked_agent)
                selections[agent.agent_id] = (
                    None
                    if selection.session is None
                    else selection.session.host_session_id
                )
        return HostDisconnectResult(
            settled_request_ids=tuple(
                request_id
                for settlement in settlements
                for request_id in settlement.settled_request_ids
            ),
            settlements=settlements,
            selected_sessions=MappingProxyType(selections),
        )

    def tombstone_agent(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        expected_state_revision: Optional[int] = None,
    ) -> AgentTombstone:
        """Commit the deletion generation and clear pointers before cleanup."""

        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        agent_id = _required_text(agent_id, "agent_id", maximum=255)
        if expected_state_revision is not None and (
            type(expected_state_revision) is not int or expected_state_revision < 0
        ):
            raise ValueError("expected_state_revision must be non-negative")
        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            row = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            if row is None:
                raise PersonalAgentNotFoundError("personal agent not found")
            if row.deleted_at is not None:
                return AgentTombstone(
                    agent_id=agent_id,
                    owner_user_id=owner_user_id,
                    lifecycle_generation=row.lifecycle_generation,
                    state_revision=row.state_revision,
                    deleted_at=row.deleted_at,
                )
            if (
                expected_state_revision is not None
                and row.state_revision != expected_state_revision
            ):
                raise StaleRuntimeGenerationError("agent tombstone revision is stale")
            generation = max(
                row.generation_counter,
                row.lifecycle_generation,
            ) + 1
            if generation > _MAX_GENERATION:
                raise PersonalAgentRuntimeError("agent lifecycle generation exhausted")
            deleted_at = _now_ms()
            try:
                updated = repository.compare_and_set_agent(
                    transaction,
                    owner_id=owner_user_id,
                    agent_id=agent_id,
                    expected_revision=row.state_revision,
                    updates={
                        "status": "disabled",
                        "deleted_at": deleted_at,
                        "updated_at": deleted_at,
                        "generation_counter": generation,
                        "lifecycle_generation": generation,
                        "active_revision_id": None,
                        "selected_host_session_id": None,
                        "authoritative_instance_id": None,
                        "host_client_id": None,
                        "host_session_id": None,
                        "host_last_seen_at": None,
                    },
                )
            except RepositoryConflictError as exc:
                raise StaleRuntimeGenerationError(
                    "agent tombstone CAS is stale"
                ) from exc
            if updated.deleted_at is None:  # pragma: no cover - repository invariant
                raise RuntimeError("agent tombstone did not persist deletion time")
            return AgentTombstone(
                agent_id=agent_id,
                owner_user_id=owner_user_id,
                lifecycle_generation=updated.lifecycle_generation,
                state_revision=updated.state_revision,
                deleted_at=updated.deleted_at,
            )

    def cleanup_tombstoned_agent(
        self, tombstone: AgentTombstone
    ) -> AgentTombstoneCleanup:
        """Settle every older runtime only after the exact tombstone committed.

        The tombstone generation/state revision/deletion timestamp and cleared
        pointers are rechecked before touching runtimes. This preserves the
        required delete-first ordering while avoiding ``_locked_runtime``'s
        intentional rejection of frames arriving after deletion.
        """

        if not isinstance(tombstone, AgentTombstone):
            raise TypeError("tombstone must be an AgentTombstone")
        owner_user_id = _required_text(
            tombstone.owner_user_id, "owner_user_id"
        )
        agent_id = _required_text(tombstone.agent_id, "agent_id", maximum=255)
        if (
            type(tombstone.lifecycle_generation) is not int
            or tombstone.lifecycle_generation <= 0
            or type(tombstone.state_revision) is not int
            or tombstone.state_revision <= 0
            or type(tombstone.deleted_at) is not int
            or tombstone.deleted_at <= 0
        ):
            raise ValueError("tombstone generations and timestamp must be positive")
        with self._agents.transaction() as transaction:
            repository = self._agents.repository
            repository.lock_owner(transaction, owner_id=owner_user_id)
            agent = repository.get_agent(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
                for_update=True,
            )
            if not (
                agent is not None
                and agent.status == "disabled"
                and agent.deleted_at is not None
                and agent.deleted_at == tombstone.deleted_at
                and agent.lifecycle_generation
                == tombstone.lifecycle_generation
                and agent.generation_counter
                == tombstone.lifecycle_generation
                and agent.state_revision == tombstone.state_revision
                and agent.active_revision_id is None
                and agent.selected_host_session_id is None
                and agent.authoritative_instance_id is None
            ):
                raise StaleRuntimeGenerationError(
                    "agent tombstone cleanup fence is stale"
                )
            runtimes = repository.list_runtime_instances(
                transaction,
                owner_id=owner_user_id,
                agent_id=agent_id,
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
            settlements = tuple(
                self._terminalize_instance_plane(
                    transaction,
                    runtime,
                    agent,
                    failure_code="agent_deleted",
                )
                for runtime in sorted(
                    (
                        item
                        for item in runtimes
                        if item.lifecycle_generation
                        < tombstone.lifecycle_generation
                    ),
                    key=lambda item: (
                        item.lifecycle_generation,
                        item.runtime_instance_id,
                    ),
                )
            )
            settled_request_ids = tuple(
                request_id
                for settlement in settlements
                for request_id in settlement.settled_request_ids
            )
            return AgentTombstoneCleanup(
                tombstone=tombstone,
                settlements=settlements,
                settled_request_ids=settled_request_ids,
            )
