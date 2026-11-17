"""PostgreSQL persistence for agent registry, trust, revisions, host sessions, and
runtime instances, fenced by compare-and-set predicates. Identity policy, admission,
and lifecycle decisions stay with AstralDeep's orchestrator.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.database.dialect import dialect_of
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _canonical_json,
    _non_negative_int,
    _positive_int,
    _required_id,
    _structured_json,
)
from astralplane.repositories.agent_models import (
    DeclarativeAgentCommand,
    DeclarativeAgentPreparation,
    DeclarativeAgentReceipt,
    DeclarativeAgentResult,
    _integer,
    definition_snapshot,
)

_AGENT_IDENTITY_LOCK_NAMESPACE = 1095980115

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_POLICY_MARKER_KEY = "user_agent_policy_revision"
_POLICY_RECONCILIATION_LOCK = "astralplane:user-agent-policy-reconciliation"


@dataclass(frozen=True, slots=True)
class AgentOwnershipRecord:
    agent_id: str
    owner_email: str
    is_public: bool
    created_at: int | None
    updated_at: int | None


@dataclass(frozen=True, slots=True)
class AgentTrustRecord:
    agent_id: str
    is_safe: bool
    marked_by: str | None
    marked_at: datetime | None
    prior_state: bool | None
    revised_reset_at: datetime | None


@dataclass(frozen=True, slots=True)
class UserAgentRecord:
    agent_id: str
    owner_id: str
    owner_email: str | None
    display_name: str
    status: str
    declared_tools: tuple[str, ...]
    declared_scopes: tuple[str, ...]
    declared_egress: tuple[str, ...] | None
    constitution_version: str | None
    validated_at: int | None
    revalidation_required: bool
    draft_id: str | None
    host_client_id: str | None
    host_session_id: str | None
    host_last_seen_at: int | None
    is_public: bool
    deleted_at: int | None
    created_at: int | None
    updated_at: int | None
    active_revision_id: str | None
    last_known_good_revision_id: str | None
    selected_host_session_id: str | None
    authoritative_instance_id: str | None
    lifecycle_generation: int
    generation_counter: int
    state_revision: int
    validated_policy_revision: str | None
    agent_kind: str = "executable"
    selected_definition_revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRevisionRecord:
    revision_id: str
    agent_id: str
    owner_id: str
    revision_number: int
    parent_revision_id: str | None
    previous_good_revision_id: str | None
    artifact_digest: str | None
    manifest: Mapping[str, Any] | None
    artifact_relative_path: str | None
    runtime_contract_version: int | None
    release_lock_digest: str | None
    compatibility_state: str
    state: str
    promotion_token: str | None
    state_revision: int
    created_at: datetime
    confirmed_at: datetime | None
    promoted_at: datetime | None
    failed_at: datetime | None
    failure_code: str | None
    revision_kind: str = "executable"
    definition_version: int | None = None
    definition_json: Mapping[str, Any] | None = field(default=None, repr=False)
    definition_digest: str | None = None


@dataclass(frozen=True, slots=True)
class AgentHostSessionRecord:
    host_session_id: str
    host_id: str
    owner_id: str
    connection_scope_id: str
    platform: str
    client_version: str
    host_generation: int
    supersedes_session_id: str | None
    supported_runtime_contract_versions: tuple[int, ...]
    runtime_contract_version: int
    release_lock_digest: str
    state: str
    inventory_state: str
    eligible_since: datetime
    accepted_at: datetime
    last_seen_at: datetime
    disconnected_at: datetime | None
    inventory_reconciled_at: datetime | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class AgentRuntimeInstanceRecord:
    runtime_instance_id: str
    agent_id: str
    owner_id: str
    host_id: str
    host_session_id: str
    delivery_id: str
    revision_id: str
    process_id: str | None
    lifecycle_generation: int
    runtime_contract_version: int
    operation_id: str | None
    operation_execution_generation: int
    state: str
    is_authoritative: bool
    state_revision: int
    created_at: datetime
    started_at: datetime | None
    registered_at: datetime | None
    last_heartbeat_sequence: int | None
    ready_at: datetime | None
    last_liveness_at: datetime | None
    terminal_at: datetime | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class AgentRuntimeExpiryCandidate:
    runtime_instance_id: str
    owner_id: str
    state: str
    reason: str


@dataclass(frozen=True, slots=True)
class AgentRuntimeRequestRecord:
    request_id: str
    request_generation: str
    operation_id: str | None
    operation_execution_generation: int
    runtime_instance_id: str
    agent_id: str
    owner_id: str
    state: str
    state_revision: int
    assigned_at: datetime
    terminal_at: datetime | None
    terminal_code: str | None
    result_digest: str | None


@dataclass(frozen=True, slots=True)
class AgentPolicyReconciliationResult:
    policy_revision: str
    marker_changed: bool
    agents_marked_for_revalidation: int


class AgentRepository:
    def lock_owner(self, transaction: Transaction, *, owner_id: str) -> None:
        owner_id = _required_id(owner_id, "owner_id")
        transaction.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (owner_id,))

    def reconcile_validation_policy_for_administration(
        self,
        transaction: Transaction,
        *,
        policy_revision: str,
    ) -> AgentPolicyReconciliationResult:
        revision = _required_id(policy_revision, "policy_revision", maximum=512)
        transaction.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (_POLICY_RECONCILIATION_LOCK,),
        )
        marker = transaction.fetch_one(
            "SELECT value FROM schema_meta WHERE key = %s FOR UPDATE",
            (_POLICY_MARKER_KEY,),
        )
        if marker is not None:
            observed = marker.get("value")
            if not isinstance(observed, str):
                raise RepositoryDataError("user-agent policy marker must be persisted text")
            if observed == revision:
                return AgentPolicyReconciliationResult(
                    policy_revision=revision,
                    marker_changed=False,
                    agents_marked_for_revalidation=0,
                )

        result = transaction.execute(
            """
            UPDATE user_agent
            SET revalidation_required = TRUE
            WHERE deleted_at IS NULL AND agent_kind='executable'
              AND validated_policy_revision IS DISTINCT FROM %s
              AND revalidation_required = FALSE
            """,
            (revision,),
        )
        if (
            isinstance(result.rowcount, bool)
            or not isinstance(result.rowcount, int)
            or result.rowcount < 0
        ):
            raise RepositoryDataError(
                "user-agent policy reconciliation returned an invalid row count"
            )
        marker_result = transaction.execute(
            """
            INSERT INTO schema_meta (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (_POLICY_MARKER_KEY, revision),
        )
        if marker_result.rowcount != 1:
            raise RepositoryDataError("user-agent policy marker write was not exact")
        return AgentPolicyReconciliationResult(
            policy_revision=revision,
            marker_changed=True,
            agents_marked_for_revalidation=result.rowcount,
        )

    def upsert_ownership(
        self,
        transaction: Transaction,
        *,
        agent_id: str,
        owner_email: str,
        is_public: bool,
        observed_at: int,
    ) -> AgentOwnershipRecord:
        agent_id = _required_id(agent_id, "agent_id", maximum=512)
        owner_email = _bounded_text(owner_email, "owner_email", maximum=1024)
        if not isinstance(is_public, bool):
            raise RepositoryValidationError("is_public must be boolean")
        observed_at = _non_negative_int(observed_at, "observed_at")
        _lock_unbound_agent_identity(transaction, agent_id)
        row = transaction.fetch_one(
            """
            INSERT INTO agent_ownership (
                agent_id, owner_email, is_public, created_at, updated_at
            ) SELECT %s, %s, %s, %s, %s
            WHERE NOT EXISTS (SELECT 1 FROM user_agent
                              WHERE agent_id=%s AND agent_kind<>'executable')
            ON CONFLICT (agent_id) DO UPDATE SET
                is_public = EXCLUDED.is_public,
                updated_at = EXCLUDED.updated_at
            WHERE agent_ownership.owner_email = EXCLUDED.owner_email
            RETURNING *
            """,
            (agent_id, owner_email, is_public, observed_at, observed_at, agent_id),
        )
        if row is None:
            existing = transaction.fetch_one(
                "SELECT * FROM agent_ownership WHERE agent_id = %s FOR SHARE",
                (agent_id,),
            )
            if existing is None:
                raise RepositoryConflictError("agent ownership upsert was not applied")
            raise RepositoryConflictError("agent identity is already bound to a different owner")
        return _ownership(row)

    def get_ownership(
        self, transaction: Transaction, *, agent_id: str
    ) -> AgentOwnershipRecord | None:
        agent_id = _required_id(agent_id, "agent_id", maximum=512)
        row = transaction.fetch_one(
            "SELECT * FROM agent_ownership WHERE agent_id = %s", (agent_id,)
        )
        return None if row is None else _ownership(row)

    def list_ownership_for_administration(
        self, transaction: Transaction, *, limit: int = 1000
    ) -> tuple[AgentOwnershipRecord, ...]:
        limit = _bounded_limit(limit, maximum=5000)
        rows = transaction.fetch_all(
            "SELECT * FROM agent_ownership ORDER BY agent_id LIMIT %s", (limit,)
        )
        return tuple(_ownership(row) for row in rows)

    def remove_ownership(
        self,
        transaction: Transaction,
        *,
        agent_id: str,
        owner_email: str,
    ) -> bool:
        agent_id = _required_id(agent_id, "agent_id", maximum=512)
        owner_email = _bounded_text(owner_email, "owner_email", maximum=1024)
        _lock_unbound_agent_identity(transaction, agent_id)
        result = transaction.execute(
            "DELETE FROM agent_ownership WHERE agent_id = %s AND owner_email = %s "
            "AND NOT EXISTS (SELECT 1 FROM user_agent WHERE "
            "user_agent.agent_id=agent_ownership.agent_id AND agent_kind<>'executable')",
            (agent_id, owner_email),
        )
        if result.rowcount not in {0, 1}:
            raise RepositoryDataError("agent ownership deletion returned an invalid row count")
        return result.rowcount == 1

    def set_visibility(
        self,
        transaction: Transaction,
        *,
        agent_id: str,
        owner_email: str,
        is_public: bool,
        updated_at: int,
    ) -> AgentOwnershipRecord:
        agent_id = _required_id(agent_id, "agent_id", maximum=512)
        owner_email = _bounded_text(owner_email, "owner_email", maximum=1024)
        updated_at = _non_negative_int(updated_at, "updated_at")
        if not isinstance(is_public, bool):
            raise RepositoryValidationError("is_public must be boolean")
        _lock_unbound_agent_identity(transaction, agent_id)
        row = transaction.fetch_one(
            """
            UPDATE agent_ownership SET is_public = %s, updated_at = %s
            WHERE agent_id = %s AND owner_email = %s
              AND NOT EXISTS (SELECT 1 FROM user_agent
                              WHERE user_agent.agent_id=agent_ownership.agent_id
                              AND agent_kind<>'executable') RETURNING *
            """,
            (is_public, updated_at, agent_id, owner_email),
        )
        if row is None:
            raise RepositoryNotFoundError("owner-scoped agent ownership was not found")
        return _ownership(row)

    def get_trust(self, transaction: Transaction, *, agent_id: str) -> AgentTrustRecord | None:
        agent_id = _required_id(agent_id, "agent_id", maximum=512)
        row = transaction.fetch_one("SELECT * FROM agent_trust WHERE agent_id = %s", (agent_id,))
        return None if row is None else _trust(row)

    def set_trust(
        self,
        transaction: Transaction,
        *,
        agent_id: str,
        is_safe: bool,
        marked_by: str,
        reset_for_revision: bool = False,
    ) -> AgentTrustRecord:
        agent_id = _required_id(agent_id, "agent_id", maximum=512)
        marked_by = _required_id(marked_by, "marked_by")
        if not isinstance(is_safe, bool) or not isinstance(reset_for_revision, bool):
            raise RepositoryValidationError("trust flags must be boolean")
        _lock_unbound_agent_identity(transaction, agent_id)
        row = transaction.fetch_one(
            """
            INSERT INTO agent_trust (
                agent_id, is_safe, marked_by, marked_at, prior_state,
                revised_reset_at
            ) SELECT %s, %s, %s, clock_timestamp(), FALSE,
                CASE WHEN %s THEN clock_timestamp() ELSE NULL END
            WHERE NOT EXISTS (SELECT 1 FROM user_agent
                              WHERE agent_id=%s AND agent_kind<>'executable')
            ON CONFLICT (agent_id) DO UPDATE SET
                is_safe = EXCLUDED.is_safe,
                marked_by = EXCLUDED.marked_by,
                marked_at = EXCLUDED.marked_at,
                prior_state = agent_trust.is_safe,
                revised_reset_at = CASE WHEN %s THEN clock_timestamp()
                                        ELSE agent_trust.revised_reset_at END
            RETURNING *
            """,
            (agent_id, is_safe, marked_by, reset_for_revision, agent_id, reset_for_revision),
        )
        if row is None:
            raise RepositoryConflictError("declarative identity cannot inherit executable trust")
        return _trust(row)

    def create_agent(
        self,
        transaction: Transaction,
        *,
        agent_id: str,
        owner_id: str,
        display_name: str,
        observed_at: int,
        owner_email: str | None = None,
        draft_id: str | None = None,
        declared_tools: Iterable[str] = (),
        declared_scopes: Iterable[str] = (),
        declared_egress: Iterable[str] | None = None,
    ) -> UserAgentRecord:
        agent_id, owner_id = _agent_owner(agent_id, owner_id)
        display_name = _bounded_text(display_name, "display_name", maximum=1024)
        observed_at = _non_negative_int(observed_at, "observed_at")
        owner_email = _optional_text(owner_email, "owner_email", 1024)
        draft_id = _optional_text(draft_id, "draft_id", 512)
        tools = _string_tuple(declared_tools, "declared_tools")
        scopes = _string_tuple(declared_scopes, "declared_scopes")
        egress = (
            None if declared_egress is None else _string_tuple(declared_egress, "declared_egress")
        )
        self.lock_owner(transaction, owner_id=owner_id)
        _lock_agent_identity(transaction, agent_id)
        row = transaction.fetch_one(
            """
            INSERT INTO user_agent (
                agent_id, owner_user_id, owner_email, display_name, status,
                declared_tools, declared_scopes, declared_egress, draft_id,
                is_public, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, 'authoring', %s, %s, %s, %s, FALSE, %s, %s
            )
            ON CONFLICT (agent_id) DO NOTHING
            RETURNING *
            """,
            (
                agent_id,
                owner_id,
                owner_email,
                display_name,
                json.dumps(tools, separators=(",", ":")),
                json.dumps(scopes, separators=(",", ":")),
                None if egress is None else json.dumps(egress, separators=(",", ":")),
                draft_id,
                observed_at,
                observed_at,
            ),
        )
        if row is None:
            existing = transaction.fetch_one(
                "SELECT * FROM user_agent WHERE agent_id = %s FOR SHARE",
                (agent_id,),
            )
            if existing is None:
                raise RepositoryConflictError("user-agent create was not applied")
            if existing.get("deleted_at") is not None:
                raise RepositoryConflictError("user-agent tombstone cannot be overwritten")
            stored = _user_agent(existing)
            if stored.owner_id != owner_id:
                raise RepositoryConflictError("agent identity is bound to another owner")
            expected = (
                owner_email,
                display_name,
                "authoring",
                tools,
                scopes,
                egress,
                draft_id,
                False,
                observed_at,
                observed_at,
                0,
            )
            observed = (
                stored.owner_email,
                stored.display_name,
                stored.status,
                stored.declared_tools,
                stored.declared_scopes,
                stored.declared_egress,
                stored.draft_id,
                stored.is_public,
                stored.created_at,
                stored.updated_at,
                stored.state_revision,
            )
            if observed != expected:
                raise RepositoryConflictError("user-agent create replay changed initial semantics")
            return stored
        return _user_agent(row)

    def get_agent(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        for_update: bool = False,
    ) -> UserAgentRecord | None:
        agent_id, owner_id = _agent_owner(agent_id, owner_id)
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            "SELECT * FROM user_agent WHERE agent_id = %s AND owner_user_id = %s" + lock,
            (agent_id, owner_id),
        )
        return None if row is None else _user_agent(row)

    def get_agent_for_administration(
        self,
        transaction: Transaction,
        *,
        agent_id: str,
        for_update: bool = False,
    ) -> UserAgentRecord | None:
        agent_id = _required_id(agent_id, "agent_id", maximum=512)
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            "SELECT * FROM user_agent WHERE agent_id = %s" + lock,
            (agent_id,),
        )
        return None if row is None else _user_agent(row)

    def list_agents(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        include_deleted: bool = False,
        limit: int = 500,
    ) -> tuple[UserAgentRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        if not isinstance(include_deleted, bool):
            raise RepositoryValidationError("include_deleted must be boolean")
        limit = _bounded_limit(limit, maximum=2000)
        rows = transaction.fetch_all(
            """
            SELECT * FROM user_agent
            WHERE owner_user_id = %s AND (%s OR deleted_at IS NULL)
            ORDER BY updated_at DESC NULLS LAST, agent_id LIMIT %s
            """,
            (owner_id, include_deleted, limit),
        )
        return tuple(_user_agent(row) for row in rows)

    def compare_and_set_agent(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        expected_revision: int,
        updates: Mapping[str, object],
    ) -> UserAgentRecord:
        agent_id, owner_id = _agent_owner(agent_id, owner_id)
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        assignments, values = _agent_updates(updates)
        if not assignments:
            raise RepositoryValidationError("agent update must contain at least one field")
        row = transaction.fetch_one(
            f"""
            UPDATE user_agent SET {", ".join(assignments)},
                state_revision = state_revision + 1
            WHERE agent_id = %s AND owner_user_id = %s
              AND deleted_at IS NULL AND state_revision = %s AND agent_kind='executable'
            RETURNING *
            """,
            (*values, agent_id, owner_id, expected_revision),
        )
        if row is None:
            _raise_agent_miss(transaction, owner_id, agent_id)
        return _user_agent(row)

    def tombstone_agent(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        expected_revision: int,
        deleted_at: int,
    ) -> UserAgentRecord:
        deleted_at = _non_negative_int(deleted_at, "deleted_at")
        return self.compare_and_set_agent(
            transaction,
            owner_id=owner_id,
            agent_id=agent_id,
            expected_revision=expected_revision,
            updates={"status": "disabled", "deleted_at": deleted_at, "updated_at": deleted_at},
        )

    def lock_declarative_owner(self, transaction: Transaction, *, owner_id: str) -> None:
        owner_id = _required_id(owner_id, "owner_id")
        transaction.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner_id,))
        retired = transaction.fetch_one(
            "SELECT state FROM astralplane_blob_owner_state WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        if retired is not None and retired["state"] != "active":
            raise RepositoryConflictError("declarative owner is retired")
        from astralplane.repositories.assignments import AssignmentRepository

        AssignmentRepository()._lock_selected_agent_dependants(transaction, owner_id)
        self.lock_owner(transaction, owner_id=owner_id)

    def prepare_declarative_command(
        self,
        transaction: Transaction,
        *,
        command: DeclarativeAgentCommand,
    ) -> DeclarativeAgentPreparation:
        if type(command) is not DeclarativeAgentCommand:
            raise RepositoryValidationError("a typed declarative command is required")
        command = replace(command)
        digest = command.request_digest
        self.lock_declarative_owner(transaction, owner_id=command.owner_id)
        row = transaction.fetch_one(
            "SELECT * FROM user_agent_command_receipt WHERE owner_user_id=%s AND command_id=%s",
            (command.owner_id, command.command_id),
        )
        if row is not None:
            receipt = _declarative_receipt(row)
            if (receipt.agent_id, receipt.command, receipt.request_digest) != (
                command.agent_id,
                command.command,
                digest,
            ):
                raise RepositoryConflictError("declarative command identity was reused")
            agent = self.get_agent(
                transaction, owner_id=command.owner_id, agent_id=command.agent_id, for_update=True
            )
            if agent is None or agent.agent_kind != "declarative":
                raise RepositoryDataError("accepted declarative head is unavailable")
            return DeclarativeAgentPreparation(command, digest, agent, None, receipt, True)

        heads = {}
        for identity in sorted({command.agent_id, command.source_agent_id} - {None}):
            _lock_agent_identity(transaction, identity)
        for identity in sorted({command.agent_id, command.source_agent_id} - {None}):
            heads[identity] = self.get_agent(
                transaction,
                owner_id=command.owner_id,
                agent_id=identity,
                for_update=True,
            )
        agent = heads[command.agent_id]
        if command.command in {"create", "clone"}:
            if agent is not None:
                raise RepositoryConflictError("declarative target already exists")
            collision = transaction.fetch_one(
                "SELECT 1 AS present WHERE EXISTS (SELECT 1 FROM user_agent WHERE agent_id=%s) "
                "OR EXISTS (SELECT 1 FROM agent_ownership WHERE agent_id=%s) "
                "OR EXISTS (SELECT 1 FROM agent_trust WHERE agent_id=%s)",
                (command.agent_id, command.agent_id, command.agent_id),
            )
            if collision is not None:
                raise RepositoryConflictError("declarative target identity is already bound")
        elif agent is None or agent.agent_kind != "declarative" or agent.deleted_at is not None:
            raise RepositoryNotFoundError("owned declarative agent is unavailable")
        elif agent.state_revision != command.expected_revision:
            raise RepositoryConflictError("declarative head state fence is stale")

        count = transaction.fetch_one(
            "SELECT count(*) AS count FROM user_agent_command_receipt "
            "WHERE agent_id=%s AND owner_user_id=%s",
            (command.agent_id, command.owner_id),
        )["count"]
        _integer(count, "receipt count", maximum=4096)
        capacity = {"archive": 4095, "delete": 4096}.get(command.command, 4094)
        if count >= capacity:
            raise RepositoryConflictError("declarative command receipt capacity is exhausted")
        revision = None
        if command.command == "revise":
            latest = self.list_revisions(
                transaction, owner_id=command.owner_id, agent_id=command.agent_id, limit=1
            )
            if not latest or latest[0].revision_id != command.parent_revision_id:
                raise RepositoryConflictError("declarative parent revision is stale")
            revision = self._definition_revision(
                transaction,
                command.owner_id,
                command.agent_id,
                command.parent_revision_id,
            )
        elif command.command == "activate":
            revision = self._definition_revision(
                transaction,
                command.owner_id,
                command.agent_id,
                command.revision_id,
            )
            if (
                agent.status == "active"
                and agent.selected_definition_revision_id == revision.revision_id
            ):
                raise RepositoryConflictError("definition is already selected")
        elif command.command == "archive" and agent.status == "archived":
            raise RepositoryConflictError("definition is already archived")
        elif command.command == "clone":
            source = heads[command.source_agent_id]
            if (
                source is None
                or source.agent_kind != "declarative"
                or source.deleted_at is not None
            ):
                raise RepositoryNotFoundError("owned clone source is unavailable")
            revision = self._definition_revision(
                transaction,
                command.owner_id,
                command.source_agent_id,
                command.source_revision_id,
            )
        return DeclarativeAgentPreparation(command, digest, agent, revision, None, False)

    def _definition_revision(self, transaction, owner_id, agent_id, revision_id):
        revision = self.get_revision(
            transaction,
            owner_id=owner_id,
            agent_id=agent_id,
            revision_id=revision_id,
            for_update=True,
        )
        if revision is None or revision.revision_kind != "declarative":
            raise RepositoryNotFoundError("owned definition revision is unavailable")
        return revision

    def apply_declarative_command(
        self,
        transaction: Transaction,
        *,
        preparation: DeclarativeAgentPreparation,
    ) -> DeclarativeAgentResult:
        if type(preparation) is not DeclarativeAgentPreparation:
            raise RepositoryValidationError("a typed declarative preparation is required")
        with transaction.savepoint("agent_definition_" + uuid.uuid4().hex):
            current = self.prepare_declarative_command(transaction, command=preparation.command)
            if current != preparation:
                raise RepositoryConflictError("declarative preparation is no longer current")
            if current.replayed:
                return DeclarativeAgentResult(
                    current.agent, current.revision, current.receipt, True
                )
            command, agent, revision = current.command, current.agent, current.revision
            now = transaction.fetch_one(
                "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS now"
            )["now"]
            if command.command in {"create", "clone"}:
                row = transaction.fetch_one(
                    "INSERT INTO user_agent(agent_id,owner_user_id,display_name,status,agent_kind,"
                    "created_at,updated_at) VALUES (%s,%s,%s,'draft','declarative',%s,%s) "
                    "ON CONFLICT (agent_id) DO NOTHING RETURNING *",
                    (command.agent_id, command.owner_id, command.display_name, now, now),
                )
                if row is None:
                    raise RepositoryConflictError(
                        "declarative target identity was concurrently bound"
                    )
                agent = _user_agent(row)
            if command.command in {"create", "revise", "clone"}:
                definition = (
                    revision.definition_json if command.command == "clone" else command.definition
                )
                snapshot, digest = definition_snapshot(definition)
                number = revision.revision_number + 1 if command.command == "revise" else 1
                row = transaction.fetch_one(
                    "INSERT INTO user_agent_revision(revision_id,agent_id,owner_user_id,"
                    "revision_number,"
                    "parent_revision_id,revision_kind,compatibility_state,state,definition_version,"
                    "definition_json,definition_digest) VALUES (%s,%s,%s,%s,%s,'declarative',"
                    "'declarative','definition',1,%s::jsonb,%s) RETURNING *",
                    (
                        command.revision_id,
                        command.agent_id,
                        command.owner_id,
                        number,
                        command.parent_revision_id,
                        _canonical_json(snapshot, "definition"),
                        digest,
                    ),
                )
                revision = _revision(row)
            if command.command not in {"create", "clone"}:
                from astralplane.repositories.assignments import AssignmentRepository

                AssignmentRepository()._invalidate_selected_agent_dependants(
                    transaction, command.owner_id, command.agent_id
                )
                status = (
                    "active"
                    if command.command == "activate"
                    else ("draft" if command.command == "revise" else "archived")
                )
                selected = command.revision_id if command.command == "activate" else None
                row = transaction.fetch_one(
                    "UPDATE user_agent SET status=%s,selected_definition_revision_id=%s,"
                    "display_name=COALESCE(%s,display_name),deleted_at=%s,updated_at=%s,"
                    "state_revision=state_revision+1 WHERE agent_id=%s AND owner_user_id=%s "
                    "AND agent_kind='declarative' AND deleted_at IS NULL "
                    "AND state_revision=%s RETURNING *",
                    (
                        status,
                        selected,
                        command.display_name,
                        now if command.command == "delete" else None,
                        now,
                        command.agent_id,
                        command.owner_id,
                        command.expected_revision,
                    ),
                )
                if row is None:
                    raise RepositoryConflictError("declarative head mutation is stale")
                agent = _user_agent(row)
            result_revision_id = revision.revision_id if revision is not None else None
            row = transaction.fetch_one(
                "INSERT INTO user_agent_command_receipt(owner_user_id,agent_id,command_id,"
                "command_version,command,request_digest,result_state_revision,"
                "result_definition_revision_id) "
                "VALUES (%s,%s,%s,1,%s,%s,%s,%s) RETURNING *",
                (
                    command.owner_id,
                    command.agent_id,
                    command.command_id,
                    command.command,
                    current.request_digest,
                    agent.state_revision,
                    result_revision_id,
                ),
            )
            receipt = _declarative_receipt(row)
            return DeclarativeAgentResult(agent, revision, receipt, False)

    def create_revision(
        self,
        transaction: Transaction,
        *,
        revision_id: str,
        agent_id: str,
        owner_id: str,
        revision_number: int,
        compatibility_state: str,
        state: str,
        parent_revision_id: str | None = None,
        previous_good_revision_id: str | None = None,
        artifact_digest: str | None = None,
        manifest: Mapping[str, Any] | None = None,
        artifact_relative_path: str | None = None,
        runtime_contract_version: int | None = None,
        release_lock_digest: str | None = None,
        promotion_token: str | None = None,
    ) -> AgentRevisionRecord:
        revision_id = _uuid_text(revision_id, "revision_id")
        agent_id, owner_id = _agent_owner(agent_id, owner_id)
        revision_number = _non_negative_int(revision_number, "revision_number")
        compatibility_state = _bounded_text(compatibility_state, "compatibility_state", maximum=64)
        state = _bounded_text(state, "state", maximum=64)
        parent_revision_id = _optional_uuid(parent_revision_id, "parent_revision_id")
        previous_good_revision_id = _optional_uuid(
            previous_good_revision_id, "previous_good_revision_id"
        )
        artifact_digest = _optional_digest(artifact_digest, "artifact_digest")
        release_lock_digest = _optional_digest(release_lock_digest, "release_lock_digest")
        promotion_token = _optional_uuid(promotion_token, "promotion_token")
        artifact_relative_path = _relative_path(artifact_relative_path)
        if runtime_contract_version is not None:
            runtime_contract_version = _positive_int(
                runtime_contract_version, "runtime_contract_version"
            )
        manifest_json = None if manifest is None else _canonical_json(manifest, "manifest")
        row = transaction.fetch_one(
            """
            INSERT INTO user_agent_revision (
                revision_id, agent_id, owner_user_id, revision_number,
                parent_revision_id, previous_good_revision_id, artifact_digest,
                manifest_json, artifact_relative_path, runtime_contract_version,
                release_lock_digest, compatibility_state, state, promotion_token
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (revision_id) DO NOTHING RETURNING *
            """,
            (
                revision_id,
                agent_id,
                owner_id,
                revision_number,
                parent_revision_id,
                previous_good_revision_id,
                artifact_digest,
                manifest_json,
                artifact_relative_path,
                runtime_contract_version,
                release_lock_digest,
                compatibility_state,
                state,
                promotion_token,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                """
                SELECT * FROM user_agent_revision
                WHERE revision_id = %s AND agent_id = %s AND owner_user_id = %s
                """,
                (revision_id, agent_id, owner_id),
            )
        if row is None:
            raise RepositoryConflictError("agent revision identity has conflicting semantics")
        result = _revision(row)
        if (
            result.revision_number != revision_number
            or result.parent_revision_id != parent_revision_id
            or result.previous_good_revision_id != previous_good_revision_id
            or result.compatibility_state != compatibility_state
            or result.artifact_digest != artifact_digest
            or (
                None
                if result.manifest is None
                else _canonical_json(result.manifest, "persisted_manifest")
            )
            != manifest_json
            or result.artifact_relative_path != artifact_relative_path
            or result.runtime_contract_version != runtime_contract_version
            or result.release_lock_digest != release_lock_digest
            or result.promotion_token != promotion_token
        ):
            raise RepositoryConflictError("agent revision replay changed immutable fields")
        return result

    def get_revision(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        revision_id: str,
        for_update: bool = False,
    ) -> AgentRevisionRecord | None:
        agent_id, owner_id = _agent_owner(agent_id, owner_id)
        revision_id = _uuid_text(revision_id, "revision_id")
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            """
            SELECT * FROM user_agent_revision
            WHERE revision_id = %s AND agent_id = %s AND owner_user_id = %s
            """
            + lock,
            (revision_id, agent_id, owner_id),
        )
        return None if row is None else _revision(row)

    def list_revisions(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        limit: int = 200,
        before_revision_number: int | None = None,
    ) -> tuple[AgentRevisionRecord, ...]:
        agent_id, owner_id = _agent_owner(agent_id, owner_id)
        limit = _bounded_limit(limit, maximum=1000)
        if before_revision_number is not None:
            _integer(before_revision_number, "before_revision_number")
        rows = transaction.fetch_all(
            """
            SELECT * FROM user_agent_revision
            WHERE agent_id = %s AND owner_user_id = %s
              AND (%s::bigint IS NULL OR revision_number < %s)
            ORDER BY revision_number DESC LIMIT %s
            """,
            (agent_id, owner_id, before_revision_number, before_revision_number, limit),
        )
        return tuple(_revision(row) for row in rows)

    def transition_revision(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        revision_id: str,
        expected_revision: int,
        expected_state: str,
        updates: Mapping[str, object],
    ) -> AgentRevisionRecord:
        agent_id, owner_id = _agent_owner(agent_id, owner_id)
        revision_id = _uuid_text(revision_id, "revision_id")
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        expected_state = _bounded_text(expected_state, "expected_state", maximum=64)
        assignments, values = _revision_updates(updates)
        if not assignments:
            raise RepositoryValidationError("revision transition must update a field")
        row = transaction.fetch_one(
            f"""
            UPDATE user_agent_revision SET {", ".join(assignments)},
                state_revision = state_revision + 1
            WHERE revision_id = %s AND agent_id = %s AND owner_user_id = %s
              AND state_revision = %s AND state = %s AND revision_kind='executable'
            RETURNING *
            """,
            (*values, revision_id, agent_id, owner_id, expected_revision, expected_state),
        )
        if row is None:
            raise RepositoryConflictError("agent revision state fence is stale")
        return _revision(row)

    def create_host_session(
        self,
        transaction: Transaction,
        *,
        host_session_id: str,
        host_id: str,
        owner_id: str,
        connection_scope_id: str,
        platform: str,
        client_version: str,
        host_generation: int,
        supported_runtime_contract_versions: Iterable[int],
        runtime_contract_version: int,
        release_lock_digest: str,
        eligible_since: datetime,
        accepted_at: datetime,
        last_seen_at: datetime,
        state: str = "connected",
        inventory_state: str = "pending",
        supersedes_session_id: str | None = None,
    ) -> AgentHostSessionRecord:
        host_session_id = _uuid_text(host_session_id, "host_session_id")
        host_id = _uuid_text(host_id, "host_id")
        owner_id = _required_id(owner_id, "owner_id")
        connection_scope_id = _uuid_text(connection_scope_id, "connection_scope_id")
        platform = _bounded_text(platform, "platform", maximum=32)
        client_version = _bounded_text(client_version, "client_version", maximum=128)
        host_generation = _positive_int(host_generation, "host_generation")
        versions = _positive_versions(supported_runtime_contract_versions)
        runtime_contract_version = _positive_int(
            runtime_contract_version, "runtime_contract_version"
        )
        if runtime_contract_version not in versions:
            raise RepositoryValidationError(
                "runtime_contract_version must be supported by the host"
            )
        release_lock_digest = _digest(release_lock_digest, "release_lock_digest")
        supersedes_session_id = _optional_uuid(supersedes_session_id, "supersedes_session_id")
        eligible_since = _aware_datetime(eligible_since, "eligible_since")
        accepted_at = _aware_datetime(accepted_at, "accepted_at")
        last_seen_at = _aware_datetime(last_seen_at, "last_seen_at")
        row = transaction.fetch_one(
            """
            INSERT INTO agent_host_session (
                host_session_id, host_id, owner_user_id, connection_scope_id,
                platform, client_version, host_generation, supersedes_session_id,
                supported_runtime_contract_versions, runtime_contract_version,
                release_lock_digest, state, inventory_state, eligible_since,
                accepted_at, last_seen_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (host_session_id) DO NOTHING RETURNING *
            """,
            (
                host_session_id,
                host_id,
                owner_id,
                connection_scope_id,
                platform,
                client_version,
                host_generation,
                supersedes_session_id,
                list(versions),
                runtime_contract_version,
                release_lock_digest,
                state,
                inventory_state,
                eligible_since,
                accepted_at,
                last_seen_at,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM agent_host_session "
                "WHERE host_session_id = %s AND owner_user_id = %s",
                (host_session_id, owner_id),
            )
        if row is None:
            raise RepositoryConflictError("host session identity has conflicting semantics")
        result = _host_session(row)
        if (
            result.host_id != host_id
            or result.connection_scope_id != connection_scope_id
            or result.platform != platform
            or result.client_version != client_version
            or result.host_generation != host_generation
            or result.supersedes_session_id != supersedes_session_id
            or result.supported_runtime_contract_versions != versions
            or result.runtime_contract_version != runtime_contract_version
            or result.release_lock_digest != release_lock_digest
            or result.eligible_since != eligible_since
            or result.accepted_at != accepted_at
        ):
            raise RepositoryConflictError("host session replay changed immutable fields")
        return result

    def get_host_session(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        host_session_id: str,
        for_update: bool = False,
    ) -> AgentHostSessionRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        host_session_id = _uuid_text(host_session_id, "host_session_id")
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            "SELECT * FROM agent_host_session WHERE host_session_id = %s AND owner_user_id = %s"
            + lock,
            (host_session_id, owner_id),
        )
        return None if row is None else _host_session(row)

    def list_host_sessions(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        state: str | None = None,
        host_id: str | None = None,
        inventory_state: str | None = None,
        for_update: bool = False,
        limit: int = 200,
    ) -> tuple[AgentHostSessionRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        state = _optional_text(state, "state", 64)
        host_id = None if host_id is None else _uuid_text(host_id, "host_id")
        inventory_state = _optional_text(inventory_state, "inventory_state", 64)
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        limit = _bounded_limit(limit, maximum=1000)
        lock = " FOR UPDATE" if for_update else ""
        rows = transaction.fetch_all(
            """
            SELECT * FROM agent_host_session
            WHERE owner_user_id = %s
              AND (%s IS NULL OR state = %s)
              AND (%s::uuid IS NULL OR host_id = %s::uuid)
              AND (%s IS NULL OR inventory_state = %s)
            ORDER BY host_generation DESC, host_session_id
            LIMIT %s
            """
            + lock,
            (
                owner_id,
                state,
                state,
                host_id,
                host_id,
                inventory_state,
                inventory_state,
                limit,
            ),
        )
        return tuple(_host_session(row) for row in rows)

    def transition_host_session(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        host_session_id: str,
        expected_state: str,
        updates: Mapping[str, object],
    ) -> AgentHostSessionRecord:
        owner_id = _required_id(owner_id, "owner_id")
        host_session_id = _uuid_text(host_session_id, "host_session_id")
        expected_state = _bounded_text(expected_state, "expected_state", maximum=64)
        assignments, values = _host_updates(updates)
        if not assignments:
            raise RepositoryValidationError("host-session transition must update a field")
        row = transaction.fetch_one(
            f"""
            UPDATE agent_host_session SET {", ".join(assignments)}
            WHERE host_session_id = %s AND owner_user_id = %s AND state = %s
            RETURNING *
            """,
            (*values, host_session_id, owner_id, expected_state),
        )
        if row is None:
            raise RepositoryConflictError("host-session state fence is stale")
        return _host_session(row)

    def create_runtime_instance(
        self,
        transaction: Transaction,
        *,
        runtime_instance_id: str,
        agent_id: str,
        owner_id: str,
        host_id: str,
        host_session_id: str,
        delivery_id: str,
        revision_id: str,
        lifecycle_generation: int,
        runtime_contract_version: int,
        operation_execution_generation: int,
        operation_id: str | None = None,
        state: str = "delivering",
    ) -> AgentRuntimeInstanceRecord:
        runtime_instance_id = _uuid_text(runtime_instance_id, "runtime_instance_id")
        agent_id, owner_id = _agent_owner(agent_id, owner_id)
        host_id = _uuid_text(host_id, "host_id")
        host_session_id = _uuid_text(host_session_id, "host_session_id")
        delivery_id = _uuid_text(delivery_id, "delivery_id")
        revision_id = _uuid_text(revision_id, "revision_id")
        operation_id = _optional_uuid(operation_id, "operation_id")
        lifecycle_generation = _positive_int(lifecycle_generation, "lifecycle_generation")
        runtime_contract_version = _positive_int(
            runtime_contract_version, "runtime_contract_version"
        )
        operation_execution_generation = _positive_int(
            operation_execution_generation, "operation_execution_generation"
        )
        state = _bounded_text(state, "state", maximum=64)
        row = transaction.fetch_one(
            """
            INSERT INTO agent_runtime_instance (
                runtime_instance_id, agent_id, owner_user_id, host_id,
                host_session_id, delivery_id, revision_id, lifecycle_generation,
                runtime_contract_version, operation_id,
                operation_execution_generation, state, is_authoritative
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE
            ) ON CONFLICT (runtime_instance_id) DO NOTHING RETURNING *
            """,
            (
                runtime_instance_id,
                agent_id,
                owner_id,
                host_id,
                host_session_id,
                delivery_id,
                revision_id,
                lifecycle_generation,
                runtime_contract_version,
                operation_id,
                operation_execution_generation,
                state,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                """
                SELECT * FROM agent_runtime_instance
                WHERE runtime_instance_id = %s AND agent_id = %s AND owner_user_id = %s
                """,
                (runtime_instance_id, agent_id, owner_id),
            )
        if row is None:
            raise RepositoryConflictError("runtime instance identity has conflicting semantics")
        result = _runtime_instance(row)
        if (
            result.host_id != host_id
            or result.host_session_id != host_session_id
            or result.delivery_id != delivery_id
            or result.revision_id != revision_id
            or result.lifecycle_generation != lifecycle_generation
            or result.runtime_contract_version != runtime_contract_version
            or result.operation_id != operation_id
            or result.operation_execution_generation != operation_execution_generation
        ):
            raise RepositoryConflictError("runtime instance replay changed immutable fields")
        return result

    def get_runtime_instance(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        runtime_instance_id: str,
        for_update: bool = False,
    ) -> AgentRuntimeInstanceRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        runtime_instance_id = _uuid_text(runtime_instance_id, "runtime_instance_id")
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            """
            SELECT * FROM agent_runtime_instance
            WHERE runtime_instance_id = %s AND owner_user_id = %s
            """
            + lock,
            (runtime_instance_id, owner_id),
        )
        return None if row is None else _runtime_instance(row)

    def get_runtime_instance_for_administration(
        self,
        transaction: Transaction,
        *,
        runtime_instance_id: str,
        for_update: bool = False,
    ) -> AgentRuntimeInstanceRecord | None:
        runtime_instance_id = _uuid_text(runtime_instance_id, "runtime_instance_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            "SELECT * FROM agent_runtime_instance WHERE runtime_instance_id = %s" + lock,
            (runtime_instance_id,),
        )
        return None if row is None else _runtime_instance(row)

    def lock_runtime_if_startup_expired(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        runtime_instance_id: str,
        timeout_seconds: float,
    ) -> AgentRuntimeInstanceRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        runtime_instance_id = _uuid_text(runtime_instance_id, "runtime_instance_id")
        timeout = _bounded_timeout(timeout_seconds, maximum=300)
        row = transaction.fetch_one(
            """
            SELECT * FROM agent_runtime_instance
            WHERE runtime_instance_id = %s AND owner_user_id = %s
              AND state IN ('delivering', 'starting')
              AND registered_at IS NULL
              AND COALESCE(started_at, created_at)
                    + (%s * INTERVAL '1 second') <= clock_timestamp()
            FOR UPDATE
            """,
            (runtime_instance_id, owner_id, timeout),
        )
        return None if row is None else _runtime_instance(row)

    def lock_runtime_if_liveness_expired(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        runtime_instance_id: str,
        timeout_seconds: float,
    ) -> AgentRuntimeInstanceRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        runtime_instance_id = _uuid_text(runtime_instance_id, "runtime_instance_id")
        timeout = _bounded_timeout(timeout_seconds, maximum=60)
        row = transaction.fetch_one(
            """
            SELECT * FROM agent_runtime_instance
            WHERE runtime_instance_id = %s AND owner_user_id = %s
              AND state IN ('ready', 'online', 'updating')
              AND last_liveness_at IS NOT NULL
              AND last_liveness_at
                    + (%s * INTERVAL '1 second') <= clock_timestamp()
            FOR UPDATE
            """,
            (runtime_instance_id, owner_id, timeout),
        )
        return None if row is None else _runtime_instance(row)

    def list_expired_runtime_candidates_for_administration(
        self,
        query: QueryExecutor,
        *,
        startup_timeout_seconds: float,
        liveness_timeout_seconds: float,
        limit: int = 1000,
    ) -> tuple[AgentRuntimeExpiryCandidate, ...]:
        startup_timeout = _bounded_timeout(startup_timeout_seconds, maximum=300)
        liveness_timeout = _bounded_timeout(liveness_timeout_seconds, maximum=60)
        limit = _bounded_limit(limit, maximum=2000)
        rows = query.fetch_all(
            """
            SELECT runtime_instance_id, owner_user_id, state,
                   CASE
                       WHEN state IN ('delivering', 'starting') THEN 'startup'
                       ELSE 'liveness'
                   END AS expiry_reason
            FROM agent_runtime_instance
            WHERE (
                    state IN ('delivering', 'starting')
                AND registered_at IS NULL
                AND COALESCE(started_at, created_at)
                      + (%s * INTERVAL '1 second') <= clock_timestamp()
            ) OR (
                    state IN ('ready', 'online', 'updating')
                AND last_liveness_at IS NOT NULL
                AND last_liveness_at
                      + (%s * INTERVAL '1 second') <= clock_timestamp()
            )
            ORDER BY runtime_instance_id
            LIMIT %s
            """,
            (startup_timeout, liveness_timeout, limit),
        )
        candidates: list[AgentRuntimeExpiryCandidate] = []
        for row in rows:
            state = str(row.get("state") or "")
            reason = str(row.get("expiry_reason") or "")
            if (
                reason not in {"startup", "liveness"}
                or (reason == "startup" and state not in {"delivering", "starting"})
                or (reason == "liveness" and state not in {"ready", "online", "updating"})
            ):
                raise RepositoryDataError(
                    "runtime expiry candidate has invalid persisted semantics"
                )
            candidates.append(
                AgentRuntimeExpiryCandidate(
                    runtime_instance_id=str(row.get("runtime_instance_id") or ""),
                    owner_id=str(row.get("owner_user_id") or ""),
                    state=state,
                    reason=reason,
                )
            )
        if any(
            not candidate.runtime_instance_id or not candidate.owner_id for candidate in candidates
        ):
            raise RepositoryDataError("runtime expiry candidate identity is empty")
        return tuple(candidates)

    def list_runtime_instances(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str | None = None,
        host_session_id: str | None = None,
        states: Iterable[str] | None = None,
        for_update: bool = False,
        limit: int = 200,
    ) -> tuple[AgentRuntimeInstanceRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        agent_id = _optional_text(agent_id, "agent_id", 512)
        host_session_id = (
            None if host_session_id is None else _uuid_text(host_session_id, "host_session_id")
        )
        state_values = None if states is None else list(_string_tuple(states, "states", maximum=64))
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        limit = _bounded_limit(limit, maximum=1000)
        lock = " FOR UPDATE" if for_update else ""
        rows = transaction.fetch_all(
            """
            SELECT * FROM agent_runtime_instance
            WHERE owner_user_id = %s
              AND (%s IS NULL OR agent_id = %s)
              AND (%s::uuid IS NULL OR host_session_id = %s::uuid)
              AND (%s::text[] IS NULL OR state = ANY(%s::text[]))
            ORDER BY lifecycle_generation DESC, runtime_instance_id
            LIMIT %s
            """
            + lock,
            (
                owner_id,
                agent_id,
                agent_id,
                host_session_id,
                host_session_id,
                state_values,
                state_values,
                limit,
            ),
        )
        return tuple(_runtime_instance(row) for row in rows)

    def list_latest_runtime_instances(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        limit: int = 500,
    ) -> tuple[AgentRuntimeInstanceRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        limit = _bounded_limit(limit, maximum=2000)
        rows = transaction.fetch_all(
            """
            SELECT latest.*
            FROM (
                SELECT DISTINCT ON (agent_id) *
                FROM agent_runtime_instance
                WHERE owner_user_id = %s
                ORDER BY agent_id, lifecycle_generation DESC, runtime_instance_id DESC
            ) AS latest
            ORDER BY latest.agent_id
            LIMIT %s
            """
            if not dialect_of(transaction).is_sqlite
            else """
            SELECT * FROM (
                SELECT *,row_number() OVER (
                    PARTITION BY agent_id
                    ORDER BY lifecycle_generation DESC,runtime_instance_id DESC
                ) AS rank FROM agent_runtime_instance WHERE owner_user_id=%s
            ) AS latest WHERE rank=1 ORDER BY agent_id LIMIT %s
            """,
            (owner_id, limit),
        )
        return tuple(_runtime_instance(row) for row in rows)

    def get_authoritative_runtime(
        self, transaction: Transaction, *, owner_id: str, agent_id: str
    ) -> AgentRuntimeInstanceRecord | None:
        agent_id, owner_id = _agent_owner(agent_id, owner_id)
        row = transaction.fetch_one(
            """
            SELECT * FROM agent_runtime_instance
            WHERE owner_user_id = %s AND agent_id = %s AND is_authoritative
            """,
            (owner_id, agent_id),
        )
        return None if row is None else _runtime_instance(row)

    def transition_runtime_instance(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        runtime_instance_id: str,
        expected_revision: int,
        expected_states: Iterable[str],
        updates: Mapping[str, object],
    ) -> AgentRuntimeInstanceRecord:
        owner_id = _required_id(owner_id, "owner_id")
        runtime_instance_id = _uuid_text(runtime_instance_id, "runtime_instance_id")
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        states = _string_tuple(expected_states, "expected_states", maximum=64)
        if not states:
            raise RepositoryValidationError("expected_states must not be empty")
        assignments, values = _runtime_updates(updates)
        if not assignments:
            raise RepositoryValidationError("runtime transition must update a field")
        row = transaction.fetch_one(
            f"""
            UPDATE agent_runtime_instance SET {", ".join(assignments)},
                state_revision = state_revision + 1
            WHERE runtime_instance_id = %s AND owner_user_id = %s
              AND state_revision = %s AND state = ANY(%s::text[])
            RETURNING *
            """,
            (*values, runtime_instance_id, owner_id, expected_revision, list(states)),
        )
        if row is None:
            raise RepositoryConflictError("runtime instance state fence is stale")
        return _runtime_instance(row)

    def create_runtime_request(
        self,
        transaction: Transaction,
        *,
        request_id: str,
        request_generation: str,
        runtime_instance_id: str,
        agent_id: str,
        owner_id: str,
        operation_execution_generation: int,
        operation_id: str | None = None,
        state: str = "assigned",
    ) -> AgentRuntimeRequestRecord:
        request_id = _uuid_text(request_id, "request_id")
        request_generation = _uuid_text(request_generation, "request_generation")
        runtime_instance_id = _uuid_text(runtime_instance_id, "runtime_instance_id")
        agent_id, owner_id = _agent_owner(agent_id, owner_id)
        operation_id = _optional_uuid(operation_id, "operation_id")
        operation_execution_generation = _positive_int(
            operation_execution_generation, "operation_execution_generation"
        )
        state = _bounded_text(state, "state", maximum=64)
        row = transaction.fetch_one(
            """
            INSERT INTO agent_runtime_request (
                request_id, request_generation, operation_id,
                operation_execution_generation, runtime_instance_id,
                agent_id, owner_user_id, state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (request_id) DO NOTHING RETURNING *
            """,
            (
                request_id,
                request_generation,
                operation_id,
                operation_execution_generation,
                runtime_instance_id,
                agent_id,
                owner_id,
                state,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                """
                SELECT * FROM agent_runtime_request
                WHERE request_id = %s AND owner_user_id = %s
                """,
                (request_id, owner_id),
            )
        if row is None:
            raise RepositoryConflictError("runtime request identity has conflicting semantics")
        result = _runtime_request(row)
        if (
            result.request_generation != request_generation
            or result.runtime_instance_id != runtime_instance_id
            or result.agent_id != agent_id
            or result.operation_id != operation_id
            or result.operation_execution_generation != operation_execution_generation
        ):
            raise RepositoryConflictError("runtime request replay changed immutable fields")
        return result

    def get_runtime_request(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        request_id: str,
        for_update: bool = False,
    ) -> AgentRuntimeRequestRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        request_id = _uuid_text(request_id, "request_id")
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            "SELECT * FROM agent_runtime_request WHERE request_id = %s AND owner_user_id = %s"
            + lock,
            (request_id, owner_id),
        )
        return None if row is None else _runtime_request(row)

    def get_runtime_request_for_administration(
        self,
        transaction: Transaction,
        *,
        request_id: str,
        for_update: bool = False,
    ) -> AgentRuntimeRequestRecord | None:
        request_id = _uuid_text(request_id, "request_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            "SELECT * FROM agent_runtime_request WHERE request_id = %s" + lock,
            (request_id,),
        )
        return None if row is None else _runtime_request(row)

    def list_runtime_requests(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        runtime_instance_id: str | None = None,
        states: Iterable[str] | None = None,
        for_update: bool = False,
        limit: int = 500,
    ) -> tuple[AgentRuntimeRequestRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        runtime_instance_id = (
            None
            if runtime_instance_id is None
            else _uuid_text(runtime_instance_id, "runtime_instance_id")
        )
        state_values = None if states is None else list(_string_tuple(states, "states", maximum=64))
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be boolean")
        limit = _bounded_limit(limit, maximum=2000)
        lock = " FOR UPDATE" if for_update else ""
        rows = transaction.fetch_all(
            """
            SELECT * FROM agent_runtime_request
            WHERE owner_user_id = %s
              AND (%s::uuid IS NULL OR runtime_instance_id = %s::uuid)
              AND (%s::text[] IS NULL OR state = ANY(%s::text[]))
            ORDER BY assigned_at, request_id
            LIMIT %s
            """
            + lock,
            (
                owner_id,
                runtime_instance_id,
                runtime_instance_id,
                state_values,
                state_values,
                limit,
            ),
        )
        return tuple(_runtime_request(row) for row in rows)

    def transition_runtime_request(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        request_id: str,
        expected_revision: int,
        expected_states: Iterable[str],
        updates: Mapping[str, object],
    ) -> AgentRuntimeRequestRecord:
        owner_id = _required_id(owner_id, "owner_id")
        request_id = _uuid_text(request_id, "request_id")
        expected_revision = _non_negative_int(expected_revision, "expected_revision")
        states = _string_tuple(expected_states, "expected_states", maximum=64)
        if not states:
            raise RepositoryValidationError("expected_states must not be empty")
        assignments, values = _request_updates(updates)
        if not assignments:
            raise RepositoryValidationError("request transition must update a field")
        row = transaction.fetch_one(
            f"""
            UPDATE agent_runtime_request SET {", ".join(assignments)},
                state_revision = state_revision + 1
            WHERE request_id = %s AND owner_user_id = %s
              AND state_revision = %s AND state = ANY(%s::text[])
            RETURNING *
            """,
            (*values, request_id, owner_id, expected_revision, list(states)),
        )
        if row is None:
            raise RepositoryConflictError("runtime request state fence is stale")
        return _runtime_request(row)


_AGENT_COLUMNS = {
    "active_revision_id",
    "authoritative_instance_id",
    "constitution_version",
    "declared_egress",
    "declared_scopes",
    "declared_tools",
    "deleted_at",
    "display_name",
    "draft_id",
    "generation_counter",
    "host_client_id",
    "host_last_seen_at",
    "host_session_id",
    "last_known_good_revision_id",
    "lifecycle_generation",
    "owner_email",
    "revalidation_required",
    "selected_host_session_id",
    "status",
    "updated_at",
    "validated_at",
    "validated_policy_revision",
}
_REVISION_COLUMNS = {
    "confirmed_at",
    "failed_at",
    "failure_code",
    "previous_good_revision_id",
    "promoted_at",
    "state",
}
_HOST_COLUMNS = {
    "disconnected_at",
    "failure_code",
    "inventory_reconciled_at",
    "inventory_state",
    "last_seen_at",
    "state",
}
_RUNTIME_COLUMNS = {
    "failure_code",
    "is_authoritative",
    "last_heartbeat_sequence",
    "last_liveness_at",
    "process_id",
    "ready_at",
    "registered_at",
    "started_at",
    "state",
    "terminal_at",
}
_REQUEST_COLUMNS = {"result_digest", "state", "terminal_at", "terminal_code"}


def _validated_updates(
    updates: Mapping[str, object], allowed: set[str], *, operation: str
) -> tuple[list[str], list[object]]:
    if not isinstance(updates, Mapping):
        raise RepositoryValidationError(f"{operation} updates must be a mapping")
    unknown = set(updates) - allowed
    if unknown:
        raise RepositoryValidationError(
            f"{operation} contains unsupported fields",
            metadata={"fields": ",".join(sorted(unknown))},
        )
    assignments: list[str] = []
    values: list[object] = []
    for name in sorted(updates):
        value = updates[name]
        assignments.append(f"{name} = %s")
        values.append(value)
    return assignments, values


def _agent_updates(updates: Mapping[str, object]) -> tuple[list[str], list[object]]:
    normalized = dict(updates)
    for name in ("declared_tools", "declared_scopes", "declared_egress"):
        if name in normalized and normalized[name] is not None:
            normalized[name] = json.dumps(
                _string_tuple(normalized[name], name), separators=(",", ":")
            )
    for name in (
        "active_revision_id",
        "authoritative_instance_id",
        "last_known_good_revision_id",
        "selected_host_session_id",
    ):
        if name in normalized:
            normalized[name] = _optional_uuid(normalized[name], name)
    for name in (
        "lifecycle_generation",
        "generation_counter",
        "updated_at",
        "validated_at",
        "deleted_at",
    ):
        if name in normalized and normalized[name] is not None:
            normalized[name] = _non_negative_int(normalized[name], name)
    return _validated_updates(normalized, _AGENT_COLUMNS, operation="agent update")


def _revision_updates(updates: Mapping[str, object]) -> tuple[list[str], list[object]]:
    normalized = dict(updates)
    if "previous_good_revision_id" in normalized:
        normalized["previous_good_revision_id"] = _optional_uuid(
            normalized["previous_good_revision_id"], "previous_good_revision_id"
        )
    return _validated_updates(normalized, _REVISION_COLUMNS, operation="revision transition")


def _host_updates(updates: Mapping[str, object]) -> tuple[list[str], list[object]]:
    return _validated_updates(dict(updates), _HOST_COLUMNS, operation="host transition")


def _runtime_updates(updates: Mapping[str, object]) -> tuple[list[str], list[object]]:
    normalized = dict(updates)
    if "process_id" in normalized:
        normalized["process_id"] = _optional_uuid(normalized["process_id"], "process_id")
    if (
        "last_heartbeat_sequence" in normalized
        and normalized["last_heartbeat_sequence"] is not None
    ):
        normalized["last_heartbeat_sequence"] = _positive_int(
            normalized["last_heartbeat_sequence"], "last_heartbeat_sequence"
        )
    return _validated_updates(normalized, _RUNTIME_COLUMNS, operation="runtime transition")


def _request_updates(updates: Mapping[str, object]) -> tuple[list[str], list[object]]:
    normalized = dict(updates)
    if "result_digest" in normalized:
        normalized["result_digest"] = _optional_digest(normalized["result_digest"], "result_digest")
    return _validated_updates(normalized, _REQUEST_COLUMNS, operation="request transition")


def _raise_agent_miss(transaction: Transaction, owner_id: str, agent_id: str) -> None:
    row = transaction.fetch_one(
        "SELECT deleted_at, state_revision FROM user_agent "
        "WHERE agent_id = %s AND owner_user_id = %s",
        (agent_id, owner_id),
    )
    if row is None:
        raise RepositoryNotFoundError("owner-scoped user agent was not found")
    if row.get("deleted_at") is not None:
        raise RepositoryConflictError("user-agent tombstone cannot be changed")
    raise RepositoryConflictError("user-agent state revision is stale")


def _agent_owner(agent_id: str, owner_id: str) -> tuple[str, str]:
    return (
        _required_id(agent_id, "agent_id", maximum=512),
        _required_id(owner_id, "owner_id"),
    )


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum=maximum, allow_empty=True)


def _uuid_text(value: object, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RepositoryValidationError(f"{field} must be a UUID") from exc


def _optional_uuid(value: object, field: str) -> str | None:
    return None if value is None else _uuid_text(value, field)


def _bounded_timeout(value: object, *, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= maximum
    ):
        raise RepositoryValidationError(f"timeout_seconds must be in (0, {maximum:g}]")
    return float(value)


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RepositoryValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _optional_digest(value: object, field: str) -> str | None:
    return None if value is None else _digest(value, field)


def _relative_path(value: object) -> str | None:
    if value is None:
        return None
    path = _bounded_text(value, "artifact_relative_path", maximum=2048)
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or ".." in normalized.split("/"):
        raise RepositoryValidationError("artifact_relative_path must stay relative")
    return path


def _positive_versions(values: Iterable[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise RepositoryValidationError(
            "supported_runtime_contract_versions must be an integer iterable"
        )
    versions = tuple(sorted({_positive_int(value, "runtime_contract_version") for value in values}))
    if not versions:
        raise RepositoryValidationError("supported_runtime_contract_versions must not be empty")
    return versions


def _string_tuple(values: object, field: str, *, maximum: int = 512) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Iterable):
        raise RepositoryValidationError(f"{field} must be an iterable of strings")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _bounded_text(value, field, maximum=maximum)
        if text not in seen:
            result.append(text)
            seen.add(text)
    return tuple(result)


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryValidationError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _optional_datetime(value: object, field: str) -> datetime | None:
    return None if value is None else _aware_datetime(value, field)


def _json_strings(value: object, field: str, *, nullable: bool = False) -> tuple[str, ...] | None:
    if value is None and nullable:
        return None
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise RepositoryDataError(f"persisted {field} is not valid JSON") from exc
    if not isinstance(decoded, (list, tuple)) or any(not isinstance(item, str) for item in decoded):
        raise RepositoryDataError(f"persisted {field} must be a string array")
    return tuple(decoded)


def _json_mapping(value: object, field: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    decoded = _structured_json(value, field)
    if not isinstance(decoded, Mapping):
        raise RepositoryDataError(f"persisted {field} must be an object")
    return decoded


def _ownership(row: Mapping[str, Any]) -> AgentOwnershipRecord:
    return AgentOwnershipRecord(
        agent_id=str(row["agent_id"]),
        owner_email=str(row["owner_email"]),
        is_public=bool(row["is_public"]),
        created_at=None if row.get("created_at") is None else int(row["created_at"]),
        updated_at=None if row.get("updated_at") is None else int(row["updated_at"]),
    )


def _trust(row: Mapping[str, Any]) -> AgentTrustRecord:
    return AgentTrustRecord(
        agent_id=str(row["agent_id"]),
        is_safe=bool(row["is_safe"]),
        marked_by=None if row.get("marked_by") is None else str(row["marked_by"]),
        marked_at=_optional_datetime(row.get("marked_at"), "marked_at"),
        prior_state=(None if row.get("prior_state") is None else bool(row["prior_state"])),
        revised_reset_at=_optional_datetime(row.get("revised_reset_at"), "revised_reset_at"),
    )


def _lock_agent_identity(transaction: Transaction, agent_id: str) -> None:
    transaction.execute(
        "SELECT pg_advisory_xact_lock(%s,hashtext(%s))",
        (_AGENT_IDENTITY_LOCK_NAMESPACE, agent_id),
    )


def _lock_unbound_agent_identity(transaction: Transaction, agent_id: str) -> None:
    transaction.execute(
        "SELECT pg_advisory_xact_lock(%s,hashtext(%s)) WHERE NOT EXISTS "
        "(SELECT 1 FROM user_agent WHERE agent_id=%s)",
        (_AGENT_IDENTITY_LOCK_NAMESPACE, agent_id, agent_id),
    )


def _agent_kind(value: object) -> str:
    if type(value) is not str or value not in {"executable", "declarative"}:
        raise RepositoryDataError("persisted agent kind is unsupported")
    return value


def _validate_declarative_head(record: UserAgentRecord) -> None:
    if (
        record.status not in {"draft", "active", "archived"}
        or (record.status == "active") != (record.selected_definition_revision_id is not None)
        or (record.deleted_at is not None and record.status != "archived")
        or any(
            value is not None
            for value in (
                record.active_revision_id,
                record.last_known_good_revision_id,
                record.host_client_id,
                record.host_session_id,
                record.host_last_seen_at,
                record.selected_host_session_id,
                record.authoritative_instance_id,
                record.draft_id,
                record.constitution_version,
                record.validated_at,
                record.validated_policy_revision,
                record.declared_egress,
            )
        )
        or record.lifecycle_generation
        or record.generation_counter
        or record.is_public
        or record.revalidation_required
        or record.declared_tools
        or record.declared_scopes
    ):
        raise RepositoryDataError("declarative agent has an invalid lifecycle shape")


def _validate_definition_revision(record: AgentRevisionRecord) -> None:
    try:
        _, digest = definition_snapshot(record.definition_json)
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted definition is invalid") from exc
    if (
        type(record.definition_version) is not int
        or record.definition_version != 1
        or record.definition_digest != digest
        or record.compatibility_state != "declarative"
        or record.state != "definition"
        or record.state_revision != 0
        or any(
            value is not None
            for value in (
                record.artifact_digest,
                record.manifest,
                record.artifact_relative_path,
                record.runtime_contract_version,
                record.release_lock_digest,
                record.promotion_token,
                record.previous_good_revision_id,
                record.confirmed_at,
                record.promoted_at,
                record.failed_at,
                record.failure_code,
            )
        )
    ):
        raise RepositoryDataError("definition revision has an invalid immutable shape")


def _declarative_receipt(row: Mapping[str, Any]) -> DeclarativeAgentReceipt:
    try:
        command = row["command"]
        if type(command) is not str or command not in {
            "create",
            "revise",
            "activate",
            "archive",
            "clone",
            "delete",
        }:
            raise RepositoryValidationError("unsupported receipt command")
        if type(row["command_version"]) is not int or row["command_version"] != 1:
            raise RepositoryValidationError("unsupported receipt version")
        if row["agent_kind"] != "declarative":
            raise RepositoryValidationError("invalid receipt kind")
        return DeclarativeAgentReceipt(
            _required_id(row["owner_user_id"], "owner_id"),
            _required_id(row["agent_id"], "agent_id", maximum=255),
            _uuid_text(str(row["command_id"]), "command_id"),
            command,
            _digest(row["request_digest"], "request_digest"),
            _integer(row["result_state_revision"], "result_state_revision"),
            _optional_uuid(
                None
                if row["result_definition_revision_id"] is None
                else str(row["result_definition_revision_id"]),
                "result_definition_revision_id",
            ),
            _aware_datetime(row["created_at"], "created_at"),
        )
    except (KeyError, RepositoryValidationError) as exc:
        raise RepositoryDataError("persisted declarative receipt is invalid") from exc


def _user_agent(row: Mapping[str, Any]) -> UserAgentRecord:
    tools = _json_strings(row.get("declared_tools"), "declared_tools")
    scopes = _json_strings(row.get("declared_scopes"), "declared_scopes")
    egress = _json_strings(row.get("declared_egress"), "declared_egress", nullable=True)
    result = UserAgentRecord(
        agent_id=str(row["agent_id"]),
        owner_id=str(row["owner_user_id"]),
        owner_email=None if row.get("owner_email") is None else str(row["owner_email"]),
        display_name=str(row["display_name"]),
        status=str(row["status"]),
        declared_tools=tools or (),
        declared_scopes=scopes or (),
        declared_egress=egress,
        constitution_version=(
            None if row.get("constitution_version") is None else str(row["constitution_version"])
        ),
        validated_at=None if row.get("validated_at") is None else int(row["validated_at"]),
        revalidation_required=bool(row.get("revalidation_required", False)),
        draft_id=None if row.get("draft_id") is None else str(row["draft_id"]),
        host_client_id=(None if row.get("host_client_id") is None else str(row["host_client_id"])),
        host_session_id=(
            None if row.get("host_session_id") is None else str(row["host_session_id"])
        ),
        host_last_seen_at=(
            None if row.get("host_last_seen_at") is None else int(row["host_last_seen_at"])
        ),
        is_public=bool(row.get("is_public", False)),
        deleted_at=None if row.get("deleted_at") is None else int(row["deleted_at"]),
        created_at=None if row.get("created_at") is None else int(row["created_at"]),
        updated_at=None if row.get("updated_at") is None else int(row["updated_at"]),
        active_revision_id=(
            None if row.get("active_revision_id") is None else str(row["active_revision_id"])
        ),
        last_known_good_revision_id=(
            None
            if row.get("last_known_good_revision_id") is None
            else str(row["last_known_good_revision_id"])
        ),
        selected_host_session_id=(
            None
            if row.get("selected_host_session_id") is None
            else str(row["selected_host_session_id"])
        ),
        authoritative_instance_id=(
            None
            if row.get("authoritative_instance_id") is None
            else str(row["authoritative_instance_id"])
        ),
        lifecycle_generation=int(row.get("lifecycle_generation", 0)),
        generation_counter=int(row.get("generation_counter", 0)),
        state_revision=int(row.get("state_revision", 0)),
        validated_policy_revision=(
            None
            if row.get("validated_policy_revision") is None
            else str(row["validated_policy_revision"])
        ),
        agent_kind=_agent_kind(row.get("agent_kind", "executable")),
        selected_definition_revision_id=(
            None
            if row.get("selected_definition_revision_id") is None
            else str(row["selected_definition_revision_id"])
        ),
    )
    if result.agent_kind == "declarative":
        _validate_declarative_head(result)
    elif result.selected_definition_revision_id is not None:
        raise RepositoryDataError("executable agent has a definition selection")
    return result


def _revision(row: Mapping[str, Any]) -> AgentRevisionRecord:
    result = AgentRevisionRecord(
        revision_id=str(row["revision_id"]),
        agent_id=str(row["agent_id"]),
        owner_id=str(row["owner_user_id"]),
        revision_number=int(row["revision_number"]),
        parent_revision_id=(
            None if row.get("parent_revision_id") is None else str(row["parent_revision_id"])
        ),
        previous_good_revision_id=(
            None
            if row.get("previous_good_revision_id") is None
            else str(row["previous_good_revision_id"])
        ),
        artifact_digest=(
            None if row.get("artifact_digest") is None else str(row["artifact_digest"])
        ),
        manifest=_json_mapping(row.get("manifest_json"), "manifest_json"),
        artifact_relative_path=(
            None
            if row.get("artifact_relative_path") is None
            else str(row["artifact_relative_path"])
        ),
        runtime_contract_version=(
            None
            if row.get("runtime_contract_version") is None
            else int(row["runtime_contract_version"])
        ),
        release_lock_digest=(
            None if row.get("release_lock_digest") is None else str(row["release_lock_digest"])
        ),
        compatibility_state=str(row["compatibility_state"]),
        state=str(row["state"]),
        promotion_token=(
            None if row.get("promotion_token") is None else str(row["promotion_token"])
        ),
        state_revision=int(row["state_revision"]),
        created_at=_aware_datetime(row["created_at"], "created_at"),
        confirmed_at=_optional_datetime(row.get("confirmed_at"), "confirmed_at"),
        promoted_at=_optional_datetime(row.get("promoted_at"), "promoted_at"),
        failed_at=_optional_datetime(row.get("failed_at"), "failed_at"),
        failure_code=(None if row.get("failure_code") is None else str(row["failure_code"])),
        revision_kind=_agent_kind(row.get("revision_kind", "executable")),
        definition_version=row.get("definition_version"),
        definition_json=_json_mapping(row.get("definition_json"), "definition_json"),
        definition_digest=(
            None if row.get("definition_digest") is None else str(row["definition_digest"])
        ),
    )
    if result.revision_kind == "declarative":
        _validate_definition_revision(result)
    elif any(
        value is not None
        for value in (result.definition_version, result.definition_json, result.definition_digest)
    ):
        raise RepositoryDataError("executable revision contains a definition")
    return result


def _host_session(row: Mapping[str, Any]) -> AgentHostSessionRecord:
    raw_versions = row["supported_runtime_contract_versions"]
    if not isinstance(raw_versions, (list, tuple)):
        raise RepositoryDataError("persisted host contract versions must be an array")
    return AgentHostSessionRecord(
        host_session_id=str(row["host_session_id"]),
        host_id=str(row["host_id"]),
        owner_id=str(row["owner_user_id"]),
        connection_scope_id=str(row["connection_scope_id"]),
        platform=str(row["platform"]),
        client_version=str(row["client_version"]),
        host_generation=int(row["host_generation"]),
        supersedes_session_id=(
            None if row.get("supersedes_session_id") is None else str(row["supersedes_session_id"])
        ),
        supported_runtime_contract_versions=tuple(int(value) for value in raw_versions),
        runtime_contract_version=int(row["runtime_contract_version"]),
        release_lock_digest=str(row["release_lock_digest"]),
        state=str(row["state"]),
        inventory_state=str(row["inventory_state"]),
        eligible_since=_aware_datetime(row["eligible_since"], "eligible_since"),
        accepted_at=_aware_datetime(row["accepted_at"], "accepted_at"),
        last_seen_at=_aware_datetime(row["last_seen_at"], "last_seen_at"),
        disconnected_at=_optional_datetime(row.get("disconnected_at"), "disconnected_at"),
        inventory_reconciled_at=_optional_datetime(
            row.get("inventory_reconciled_at"), "inventory_reconciled_at"
        ),
        failure_code=(None if row.get("failure_code") is None else str(row["failure_code"])),
    )


def _runtime_instance(row: Mapping[str, Any]) -> AgentRuntimeInstanceRecord:
    return AgentRuntimeInstanceRecord(
        runtime_instance_id=str(row["runtime_instance_id"]),
        agent_id=str(row["agent_id"]),
        owner_id=str(row["owner_user_id"]),
        host_id=str(row["host_id"]),
        host_session_id=str(row["host_session_id"]),
        delivery_id=str(row["delivery_id"]),
        revision_id=str(row["revision_id"]),
        process_id=None if row.get("process_id") is None else str(row["process_id"]),
        lifecycle_generation=int(row["lifecycle_generation"]),
        runtime_contract_version=int(row["runtime_contract_version"]),
        operation_id=None if row.get("operation_id") is None else str(row["operation_id"]),
        operation_execution_generation=int(row["operation_execution_generation"]),
        state=str(row["state"]),
        is_authoritative=bool(row["is_authoritative"]),
        state_revision=int(row["state_revision"]),
        created_at=_aware_datetime(row["created_at"], "created_at"),
        started_at=_optional_datetime(row.get("started_at"), "started_at"),
        registered_at=_optional_datetime(row.get("registered_at"), "registered_at"),
        last_heartbeat_sequence=(
            None
            if row.get("last_heartbeat_sequence") is None
            else int(row["last_heartbeat_sequence"])
        ),
        ready_at=_optional_datetime(row.get("ready_at"), "ready_at"),
        last_liveness_at=_optional_datetime(row.get("last_liveness_at"), "last_liveness_at"),
        terminal_at=_optional_datetime(row.get("terminal_at"), "terminal_at"),
        failure_code=(None if row.get("failure_code") is None else str(row["failure_code"])),
    )


def _runtime_request(row: Mapping[str, Any]) -> AgentRuntimeRequestRecord:
    return AgentRuntimeRequestRecord(
        request_id=str(row["request_id"]),
        request_generation=str(row["request_generation"]),
        operation_id=None if row.get("operation_id") is None else str(row["operation_id"]),
        operation_execution_generation=int(row["operation_execution_generation"]),
        runtime_instance_id=str(row["runtime_instance_id"]),
        agent_id=str(row["agent_id"]),
        owner_id=str(row["owner_user_id"]),
        state=str(row["state"]),
        state_revision=int(row["state_revision"]),
        assigned_at=_aware_datetime(row["assigned_at"], "assigned_at"),
        terminal_at=_optional_datetime(row.get("terminal_at"), "terminal_at"),
        terminal_code=(None if row.get("terminal_code") is None else str(row["terminal_code"])),
        result_digest=(None if row.get("result_digest") is None else str(row["result_digest"])),
    )


__all__ = (
    "AgentHostSessionRecord",
    "AgentOwnershipRecord",
    "AgentPolicyReconciliationResult",
    "AgentRepository",
    "AgentRevisionRecord",
    "AgentRuntimeExpiryCandidate",
    "AgentRuntimeInstanceRecord",
    "AgentRuntimeRequestRecord",
    "AgentTrustRecord",
    "UserAgentRecord",
)
