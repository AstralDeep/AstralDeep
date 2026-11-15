"""Opt-in real-PostgreSQL conformance tests for the authority schema; set
ASTRALPLANE_TEST_POSTGRES_DSN to run, each test in a fresh dropped schema. Covers
binding, lifecycle, effect, and claim repositories end to end.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any
from uuid import uuid4

import pytest

import astralplane
from astralplane import api
from astralplane.authority import (
    EXECUTOR_ANCHOR_FORMAT,
    AgentAuthorityBinding,
    AstralToolScope,
    AuthorityBindingState,
    AuthorityIdempotencyConflictError,
    AuthorityLifecycleKind,
    AuthorityLifecycleOperation,
    AuthorityLifecycleStatus,
    AuthorityPopulation,
    ExternalAuthorityAnchorMetadata,
    ProtectedEffectOperation,
    ProtectedEffectStatus,
    ReceiptClaim,
    ReceiptClaimConflictError,
    ReceiptSequenceWatermark,
)
from astralplane.contracts import OutboxEntry
from astralplane.database.migrations import (
    MIGRATION_REGISTRY,
    PLANE_SCHEMA_067_STATEMENTS,
    PLANE_SCHEMA_074_STATEMENTS,
)
from astralplane.errors import PlaneError
from tests.fixtures.database_errors import check_violation

psycopg2 = pytest.importorskip("psycopg2")
sql = importlib.import_module("psycopg2.sql")
RealDictCursor = importlib.import_module("psycopg2.extras").RealDictCursor

_TEST_DSN_ENV = "ASTRALPLANE_TEST_POSTGRES_DSN"
_SCHEMA_PATTERN = re.compile(r"^astralplane_074_test_[0-9a-f]{32}$")
_NOW = datetime(2026, 8, 14, 18, tzinfo=UTC)
_POLICY_DIGEST = "sha256:" + "1" * 64
_MACHINE_DIGEST = "sha256:" + "2" * 64
_LEASE_EXPIRY_NS = 2_000_000_060_000_000_000

_INSERT_BINDING_SQL = """
INSERT INTO astralplane_authority_binding (
    binding_id, owner_id, agent_id, runtime_id, runtime_generation, population,
    tenant_id, envelope_id, warden_id, lease_id, lineage_id, subject_id,
    policy_digest, machine_digest, config_epoch, capabilities, lease_sequence,
    lease_expires_at_ns, state, created_at, updated_at, version
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
""".strip()


@dataclass(frozen=True, slots=True)
class CommandResult:
    rowcount: int
    status_message: str | None
    returned_records: tuple[Mapping[str, Any], ...]


class PostgresTransaction:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @property
    def dialect(self):
        from astralplane.database.dialect import POSTGRES

        return getattr(self.connection, "dialect", POSTGRES)

    def execute(self, statement: str, parameters: object = ()) -> CommandResult:
        with self.connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            rows = (
                ()
                if cursor.description is None
                else tuple(dict(row) for row in cursor.fetchall())
            )
            return CommandResult(
                rowcount=cursor.rowcount,
                status_message=cursor.statusmessage,
                returned_records=rows,
            )

    def fetch_one(
        self,
        statement: str,
        parameters: object = (),
    ) -> Mapping[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            row = cursor.fetchone()
            return None if row is None else dict(row)

    def fetch_all(
        self,
        statement: str,
        parameters: object = (),
    ) -> tuple[Mapping[str, Any], ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return tuple(dict(row) for row in cursor.fetchall())

    @contextmanager
    def savepoint(self, name: str) -> Iterator[PostgresTransaction]:
        if self.dialect.is_sqlite:
            assert re.fullmatch(r"[a-z_]+", name) is not None
            with self.connection.cursor() as cursor:
                cursor.execute(f'SAVEPOINT "{name}"')
            try:
                yield self
            except BaseException:
                with self.connection.cursor() as cursor:
                    cursor.execute(f'ROLLBACK TO SAVEPOINT "{name}"')
                    cursor.execute(f'RELEASE SAVEPOINT "{name}"')
                raise
            else:
                with self.connection.cursor() as cursor:
                    cursor.execute(f'RELEASE SAVEPOINT "{name}"')
            return
        identifier = sql.Identifier(name)
        with self.connection.cursor() as cursor:
            cursor.execute(sql.SQL("SAVEPOINT {}").format(identifier))
        try:
            yield self
        except BaseException:
            with self.connection.cursor() as cursor:
                cursor.execute(sql.SQL("ROLLBACK TO SAVEPOINT {}").format(identifier))
                cursor.execute(sql.SQL("RELEASE SAVEPOINT {}").format(identifier))
            raise
        else:
            with self.connection.cursor() as cursor:
                cursor.execute(sql.SQL("RELEASE SAVEPOINT {}").format(identifier))


@dataclass(frozen=True, slots=True)
class PostgresHarness:
    dsn: str
    schema: str

    @contextmanager
    def connection(self) -> Iterator[Any]:
        if self.dsn.startswith("sqlite:"):
            from astralplane.database.sqlite import create_sqlite_driver_pool

            driver = create_sqlite_driver_pool(self.dsn)
            connection = driver.getconn()
            connection.raw.row_factory = sqlite3.Row
            try:
                yield connection
            finally:
                driver.putconn(connection)
                driver.closeall()
            return
        connection = psycopg2.connect(self.dsn, cursor_factory=RealDictCursor)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}, pg_catalog").format(
                        sql.Identifier(self.schema)
                    )
                )
            connection.commit()
            yield connection
        finally:
            connection.close()


def _outbox_foundation_statement() -> str:
    return next(
        statement
        for statement in PLANE_SCHEMA_067_STATEMENTS
        if statement.startswith("CREATE TABLE IF NOT EXISTS astralplane_outbox")
    )


def _apply_authority_schema(connection: Any) -> None:
    with connection.cursor() as cursor:
        for statement in PLANE_SCHEMA_074_STATEMENTS:
            cursor.execute(statement)


@pytest.fixture
def postgres_074(migrated_template) -> Iterator[PostgresHarness]:
    dsn = os.environ.get(_TEST_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(f"set {_TEST_DSN_ENV} to an isolated PostgreSQL test database")
    if dsn.startswith("sqlite:"):
        from tests.fixtures.migrated_template import bound_clone

        with bound_clone(migrated_template) as clone:
            yield PostgresHarness(dsn=clone.dsn, schema=clone.schema)
        return

    schema = f"astralplane_074_test_{uuid4().hex}"
    assert _SCHEMA_PATTERN.fullmatch(schema) is not None
    administrator = psycopg2.connect(dsn)
    administrator.autocommit = True
    try:
        with administrator.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        harness = PostgresHarness(dsn=dsn, schema=schema)
        with harness.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(_outbox_foundation_statement())
            _apply_authority_schema(connection)
            connection.commit()
        yield harness
    finally:
        with administrator.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        administrator.close()


def _binding(
    suffix: str = "1",
    *,
    owner_id: str = "owner-1",
    agent_id: str = "agent-1",
    state: AuthorityBindingState = AuthorityBindingState.ACTIVE,
) -> AgentAuthorityBinding:
    return AgentAuthorityBinding(
        binding_id=f"binding-{suffix}",
        owner_id=owner_id,
        agent_id=agent_id,
        runtime_id=f"runtime-{suffix}",
        runtime_generation=1,
        population=AuthorityPopulation.SERVER_DYNAMIC,
        tenant_id="tenant-1",
        envelope_id="envelope-1",
        warden_id="warden-1",
        lease_id=f"lease-{suffix}",
        lineage_id=f"lineage-{suffix}",
        subject_id=agent_id,
        policy_digest=_POLICY_DIGEST,
        machine_digest=_MACHINE_DIGEST,
        config_epoch=7,
        capabilities=("astral.tools.read",),
        lease_sequence=10,
        lease_expires_at_ns=_LEASE_EXPIRY_NS,
        state=state,
        created_at=_NOW,
        updated_at=_NOW,
        version=0,
    )


def _provisioning_binding(suffix: str = "pending") -> AgentAuthorityBinding:
    return AgentAuthorityBinding.provisioning_intent(
        binding_id=f"binding-{suffix}",
        owner_id="owner-1",
        agent_id="agent-1",
        runtime_id=f"runtime-{suffix}",
        runtime_generation=1,
        population=AuthorityPopulation.SERVER_DYNAMIC,
        tenant_id="tenant-1",
        envelope_id="envelope-1",
        policy_digest=_POLICY_DIGEST,
        machine_digest=_MACHINE_DIGEST,
        config_epoch=7,
        capabilities=("astral.tools.read",),
        created_at=_NOW,
    )


def _lifecycle(
    binding: AgentAuthorityBinding,
    *,
    fingerprint: str = "5" * 64,
) -> AuthorityLifecycleOperation:
    return AuthorityLifecycleOperation(
        operation_id="lifecycle-1",
        owner_id=binding.owner_id,
        binding_id=binding.binding_id,
        kind=AuthorityLifecycleKind.RENEW,
        expected_binding_version=binding.version,
        expected_lease_sequence=binding.lease_sequence,
        request_fingerprint=fingerprint,
        status=AuthorityLifecycleStatus.PENDING,
        remote_request_id="lifecycle-1",
        result_digest=None,
        error_code=None,
        attempt_count=0,
        next_attempt_at=_NOW + timedelta(seconds=10),
        last_attempt_at=None,
        reconciled_at=None,
        reconciliation_digest=None,
        created_at=_NOW,
        updated_at=_NOW,
        version=0,
    )


def _provision_lifecycle(binding: AgentAuthorityBinding) -> AuthorityLifecycleOperation:
    return AuthorityLifecycleOperation(
        operation_id="provision-1",
        owner_id=binding.owner_id,
        binding_id=binding.binding_id,
        kind=AuthorityLifecycleKind.PROVISION,
        expected_binding_version=binding.version,
        expected_lease_sequence=None,
        request_fingerprint="b" * 64,
        status=AuthorityLifecycleStatus.PENDING,
        remote_request_id="provision-1",
        result_digest=None,
        error_code=None,
        attempt_count=0,
        next_attempt_at=_NOW,
        last_attempt_at=None,
        reconciled_at=None,
        reconciliation_digest=None,
        created_at=_NOW,
        updated_at=_NOW,
        version=0,
    )


def _effect(
    binding: AgentAuthorityBinding,
    suffix: str = "1",
) -> ProtectedEffectOperation:
    digest_character = str((int(suffix) + 2) % 10)
    operation_id = f"operation-{suffix}"
    return ProtectedEffectOperation(
        operation_id=operation_id,
        owner_id=binding.owner_id,
        agent_id=binding.agent_id,
        binding_id=binding.binding_id,
        tool_id="search-records",
        astral_scope=AstralToolScope.READ,
        lets_capability="astral.tools.read",
        lets_transition="tool-read",
        executor_audience="executor-a",
        nonce=f"nonce-{suffix}-0123456789abcdef",
        effect_digest=digest_character * 64,
        expected_sequence=binding.lease_sequence,
        audit_correlation_id=f"audit-{suffix}",
        status=ProtectedEffectStatus.RECEIPT_RECEIVED,
        receipt_id=f"receipt-{suffix}",
        receipt_digest=hashlib.sha256(operation_id.encode("ascii")).hexdigest(),
        effect_result_digest=None,
        error_code=None,
        created_at=_NOW,
        updated_at=_NOW,
        version=3,
    )


def _claim(effect: ProtectedEffectOperation, suffix: str = "1") -> ReceiptClaim:
    anchor = ExternalAuthorityAnchorMetadata(
        anchor_format=EXECUTOR_ANCHOR_FORMAT,
        audience=effect.executor_audience,
        tenant_id="tenant-1",
        envelope_id="envelope-1",
        config_epoch=7,
        executor_policy_sha256="6" * 64,
        trust_registry_sha256="7" * 64,
        schema_version=5,
        database_instance_id="8" * 64,
        claim_sequence=12,
        claim_digest="9" * 64,
        clock_floor_ns=2_000_000_000_000_000_000,
        confirmed_at=_NOW + timedelta(seconds=1),
    )
    return ReceiptClaim(
        receipt_id=effect.receipt_id or "",
        operation_id=effect.operation_id,
        owner_id=effect.owner_id,
        binding_id=effect.binding_id,
        tenant_id="tenant-1",
        envelope_id="envelope-1",
        warden_id="warden-1",
        lease_id=f"lease-{suffix}",
        subject_id=effect.agent_id,
        lineage_id=f"lineage-{suffix}",
        policy_digest=_POLICY_DIGEST,
        machine_digest=_MACHINE_DIGEST,
        config_epoch=7,
        audience=effect.executor_audience,
        transition=effect.lets_transition,
        nonce=effect.nonce,
        resulting_sequence=effect.expected_sequence + 1,
        evidence_digest="sha256:" + effect.effect_digest,
        issued_at_ns=2_000_000_000_000_000_000,
        expires_at_ns=_LEASE_EXPIRY_NS,
        claimed_at=_NOW,
        canonical_digest=hashlib.sha256(effect.operation_id.encode("ascii")).hexdigest(),
        authority_anchor=anchor,
    )


def _watermark(claim: ReceiptClaim, *, version: int = 0) -> ReceiptSequenceWatermark:
    return ReceiptSequenceWatermark(
        warden_id=claim.warden_id,
        lease_id=claim.lease_id,
        audience=claim.audience,
        last_sequence=claim.resulting_sequence,
        updated_at=_NOW,
        expires_at_ns=claim.expires_at_ns,
        version=version,
    )


def _outbox(
    suffix: str,
    *,
    idempotency_key: str | None = None,
    payload: bytes | None = None,
) -> OutboxEntry:
    canonical_payload = payload or f'{{"operation_id":"operation-{suffix}"}}'.encode()
    return OutboxEntry(
        entry_id=f"authority-outbox-{suffix}",
        topic="authority.receipt_claimed",
        canonical_payload=canonical_payload,
        payload_sha256=hashlib.sha256(canonical_payload).hexdigest(),
        idempotency_key=idempotency_key or f"authority-outbox-{suffix}",
        available_at=_NOW,
    )


def _raw_binding_values(
    suffix: str,
    *,
    capabilities: list[str] | None = None,
) -> tuple[object, ...]:
    binding = _binding(suffix)
    return (
        binding.binding_id,
        binding.owner_id,
        binding.agent_id,
        binding.runtime_id,
        binding.runtime_generation,
        binding.population.value,
        binding.tenant_id,
        binding.envelope_id,
        binding.warden_id,
        binding.lease_id,
        binding.lineage_id,
        binding.subject_id,
        binding.policy_digest,
        binding.machine_digest,
        binding.config_epoch,
        capabilities if capabilities is not None else list(binding.capabilities),
        binding.lease_sequence,
        binding.lease_expires_at_ns,
        binding.state.value,
        binding.created_at,
        binding.updated_at,
        binding.version,
    )


@pytest.mark.postgresql_schema_catalog
def test_074_001_authority_ddl_is_repeat_safe_on_real_postgresql(
    postgres_074: PostgresHarness,
) -> None:
    with postgres_074.connection() as connection:
        _apply_authority_schema(connection)
        _apply_authority_schema(connection)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT relname
                FROM pg_class
                WHERE relnamespace = current_schema()::regnamespace
                  AND relname = ANY(%s)
                ORDER BY relname
                """,
                (
                    [
                        "astralplane_authority_binding",
                        "astralplane_authority_lifecycle_operation",
                        "astralplane_protected_effect_operation",
                        "astralplane_receipt_claim",
                        "astralplane_receipt_sequence_watermark",
                    ],
                ),
            )
            assert len(cursor.fetchall()) == 5
            cursor.execute(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND indexname = 'uq_astralplane_authority_binding_nonterminal'
                """
            )
            index = cursor.fetchone()
            assert index is not None
            assert "UNIQUE INDEX" in index["indexdef"]
            assert "WHERE" in index["indexdef"]

    assert MIGRATION_REGISTRY.migrations[-1].target_revision == astralplane.SCHEMA_REVISION
    assert astralplane.CURRENT_DATA_PLANE_REVISION.schema_revision == astralplane.SCHEMA_REVISION


def test_partial_binding_uniqueness_and_owner_isolation_use_public_repository(
    postgres_074: PostgresHarness,
) -> None:
    repository = api.create_authority_repository()
    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        first = _binding("1")
        assert repository.create_binding(transaction, first) == first

        competing = _binding("2")
        with pytest.raises(AuthorityIdempotencyConflictError):
            repository.create_binding(transaction, competing)

        closed = replace(
            first,
            state=AuthorityBindingState.CLOSED,
            updated_at=_NOW + timedelta(seconds=1),
            version=1,
        )
        assert repository.transition_binding(
            transaction,
            closed,
            expected_state=AuthorityBindingState.ACTIVE,
            expected_version=0,
        ) == closed
        assert repository.create_binding(transaction, competing) == competing

        child = _binding("child", agent_id="agent-child")
        assert repository.create_binding(transaction, child) == child

        other_owner = _binding("3", owner_id="owner-2")
        assert repository.create_binding(transaction, other_owner) == other_owner
        assert repository.get_binding(
            transaction,
            owner_id="owner-2",
            binding_id=competing.binding_id,
        ) is None
        connection.commit()


def test_runtime_and_population_binding_queries_conform_on_postgresql(
    postgres_074: PostgresHarness,
) -> None:
    repository = api.create_authority_repository()
    historical = _binding("1", state=AuthorityBindingState.CLOSED)
    current = _binding("2")

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        repository.create_binding(transaction, historical)
        repository.create_binding(transaction, current)

        assert (
            repository.get_active_binding(
                transaction,
                owner_id=current.owner_id,
                agent_id=current.agent_id,
                runtime_id=current.runtime_id,
                runtime_generation=current.runtime_generation,
            )
            == current
        )
        assert (
            repository.get_active_binding(
                transaction,
                owner_id="owner-2",
                agent_id=current.agent_id,
                runtime_id=current.runtime_id,
                runtime_generation=current.runtime_generation,
            )
            is None
        )
        assert (
            repository.get_latest_binding(
                transaction,
                owner_id=current.owner_id,
                agent_id=current.agent_id,
                population=current.population,
            )
            == current
        )
        connection.rollback()


def test_provision_intent_precedes_external_identity_and_activation_is_atomic(
    postgres_074: PostgresHarness,
) -> None:
    repository = api.create_authority_repository()
    intent = _provisioning_binding()
    operation = _provision_lifecycle(intent)
    activated = replace(
        intent,
        warden_id="warden-1",
        lease_id="lease-1",
        lineage_id="lineage-1",
        subject_id="agent-1",
        lease_expires_at_ns=_LEASE_EXPIRY_NS,
        state=AuthorityBindingState.ACTIVE,
        updated_at=_NOW + timedelta(seconds=1),
        version=1,
    )

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        assert repository.create_binding(transaction, intent) == intent
        assert repository.create_lifecycle_operation(transaction, operation) == operation
        connection.commit()

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        assert repository.activate_binding(
            transaction,
            activated,
            expected_version=0,
        ) == activated
        connection.rollback()

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        assert repository.get_binding(
            transaction,
            owner_id=intent.owner_id,
            binding_id=intent.binding_id,
        ) == intent
        succeeded = replace(
            operation,
            status=AuthorityLifecycleStatus.SUCCEEDED,
            result_digest="c" * 64,
            attempt_count=1,
            next_attempt_at=None,
            last_attempt_at=_NOW + timedelta(seconds=1),
            updated_at=_NOW + timedelta(seconds=1),
            version=1,
        )
        assert repository.transition_lifecycle_operation(
            transaction,
            succeeded,
            expected_status=AuthorityLifecycleStatus.PENDING,
            expected_version=0,
        ) == succeeded
        assert repository.activate_binding(
            transaction,
            activated,
            expected_version=0,
        ) == activated
        connection.commit()

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        assert repository.get_binding(
            transaction,
            owner_id=activated.owner_id,
            binding_id=activated.binding_id,
        ) == activated


def test_failed_provision_closes_pending_intent_without_losing_lifecycle_evidence(
    postgres_074: PostgresHarness,
) -> None:
    repository = api.create_authority_repository()
    intent = _provisioning_binding()
    operation = _provision_lifecycle(intent)
    failed = replace(
        operation,
        status=AuthorityLifecycleStatus.FAILED,
        error_code="lets-provision-failed",
        attempt_count=1,
        next_attempt_at=None,
        last_attempt_at=_NOW + timedelta(seconds=1),
        updated_at=_NOW + timedelta(seconds=1),
        version=1,
    )
    abandoned = replace(
        intent,
        state=AuthorityBindingState.CLOSED,
        updated_at=_NOW + timedelta(seconds=1),
        version=1,
    )

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        repository.create_binding(transaction, intent)
        repository.create_lifecycle_operation(transaction, operation)
        assert repository.transition_lifecycle_operation(
            transaction,
            failed,
            expected_status=AuthorityLifecycleStatus.PENDING,
            expected_version=0,
        ) == failed
        assert repository.abandon_provisioning_binding(
            transaction,
            abandoned,
            expected_version=0,
        ) == abandoned
        connection.commit()

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        assert repository.get_binding(
            transaction,
            owner_id=abandoned.owner_id,
            binding_id=abandoned.binding_id,
        ) == abandoned
        assert repository.get_lifecycle_operation(
            transaction,
            owner_id=failed.owner_id,
            operation_id=failed.operation_id,
        ) == failed
        replacement = _provisioning_binding("replacement")
        assert repository.create_binding(transaction, replacement) == replacement
        connection.commit()


def test_lifecycle_replay_requires_the_same_request_fingerprint(
    postgres_074: PostgresHarness,
) -> None:
    repository = api.create_authority_repository()
    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        binding = repository.create_binding(transaction, _binding())
        operation = _lifecycle(binding)
        assert repository.create_lifecycle_operation(transaction, operation) == operation
        assert repository.create_lifecycle_operation(transaction, operation) == operation

        with pytest.raises(AuthorityIdempotencyConflictError, match="fingerprint"):
            repository.create_lifecycle_operation(
                transaction,
                replace(operation, request_fingerprint="a" * 64),
            )
        connection.commit()


@pytest.mark.postgresql_lock_catalog
def test_recoverable_lifecycle_work_is_owner_partitioned_and_skip_locked(
    postgres_074: PostgresHarness,
) -> None:
    repository = api.create_authority_repository()
    first_binding = _binding("recoverable-1")
    second_binding = _binding(
        "recoverable-2",
        owner_id="owner-2",
        agent_id="agent-2",
    )
    first_operation = _lifecycle(first_binding)
    second_operation = replace(
        _lifecycle(second_binding),
        operation_id="lifecycle-2",
        remote_request_id="lifecycle-2",
    )

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        repository.create_binding(transaction, first_binding)
        repository.create_binding(transaction, second_binding)
        repository.create_lifecycle_operation(transaction, first_operation)
        repository.create_lifecycle_operation(transaction, second_operation)
        connection.commit()

    with postgres_074.connection() as locking_connection:
        locking_transaction = PostgresTransaction(locking_connection)
        assert repository.list_recoverable_lifecycle_operations(
            locking_transaction,
            owner_id="owner-1",
            due_at=_NOW + timedelta(minutes=1),
            limit=1,
        ) == (first_operation,)

        with postgres_074.connection() as competing_connection:
            competing_transaction = PostgresTransaction(competing_connection)
            assert repository.list_recoverable_lifecycle_operations(
                competing_transaction,
                owner_id="owner-1",
                due_at=_NOW + timedelta(minutes=1),
                limit=1,
            ) == ()
            assert repository.list_recoverable_lifecycle_operations(
                competing_transaction,
                owner_id="owner-2",
                due_at=_NOW + timedelta(minutes=1),
                limit=1,
            ) == (second_operation,)
            competing_connection.rollback()
        locking_connection.rollback()


@pytest.mark.postgresql_lock_catalog
def test_recoverable_effect_work_is_ordered_owner_partitioned_and_skip_locked(
    postgres_074: PostgresHarness,
) -> None:
    repository = api.create_authority_repository()
    owner_binding = _binding("effect-owner-1")
    other_binding = _binding(
        "effect-owner-2",
        owner_id="owner-2",
        agent_id="agent-2",
    )
    first = _effect(owner_binding, "1")
    second = _effect(owner_binding, "2")
    other = _effect(other_binding, "3")
    cutoff = _NOW + timedelta(seconds=1)

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        repository.create_binding(transaction, owner_binding)
        repository.create_binding(transaction, other_binding)
        for effect in (first, second, other):
            repository.create_protected_effect(transaction, effect)
        connection.commit()

    with postgres_074.connection() as locking_connection:
        locking_transaction = PostgresTransaction(locking_connection)
        assert repository.list_recoverable_protected_effects(
            locking_transaction,
            owner_id="owner-1",
            updated_before=cutoff,
            limit=1,
        ) == (first,)

        with postgres_074.connection() as competing_connection:
            competing_transaction = PostgresTransaction(competing_connection)
            assert repository.list_recoverable_protected_effects(
                competing_transaction,
                owner_id="owner-1",
                updated_before=cutoff,
                limit=5,
            ) == (second,)
            assert repository.list_recoverable_protected_effects(
                competing_transaction,
                owner_id="owner-2",
                updated_before=cutoff,
                limit=5,
            ) == (other,)
            competing_connection.rollback()

        claimed = replace(
            first,
            status=ProtectedEffectStatus.RECEIPT_CLAIMED,
            updated_at=cutoff,
            version=first.version + 1,
        )
        assert (
            repository.transition_protected_effect(
                locking_transaction,
                claimed,
                expected_status=ProtectedEffectStatus.RECEIPT_RECEIVED,
                expected_version=first.version,
            )
            == claimed
        )
        locking_connection.commit()


def test_receipt_claim_replay_is_idempotent_and_equal_sequence_is_rejected(
    postgres_074: PostgresHarness,
) -> None:
    repository = api.create_authority_repository()
    binding = _binding()
    effect = _effect(binding)
    claim = _claim(effect)
    claimed_effect = replace(
        effect,
        status=ProtectedEffectStatus.RECEIPT_CLAIMED,
        updated_at=_NOW + timedelta(seconds=1),
        version=4,
    )
    event = _outbox("1")

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        repository.create_binding(transaction, binding)
        repository.create_protected_effect(transaction, effect)
        assert repository.claim_receipt(
            transaction,
            claim=claim,
            watermark=_watermark(claim),
            claimed_effect=claimed_effect,
            outbox_entry=event,
        ) == claim
        connection.commit()

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        assert repository.claim_receipt(
            transaction,
            claim=claim,
            watermark=_watermark(claim),
            claimed_effect=claimed_effect,
            outbox_entry=event,
        ) == claim

        second_effect = _effect(binding, "2")
        second_claim = _claim(second_effect, "1")
        repository.create_protected_effect(transaction, second_effect)
        with pytest.raises(ReceiptClaimConflictError, match="uniqueness"):
            repository.claim_receipt(
                transaction,
                claim=second_claim,
                watermark=_watermark(second_claim),
                claimed_effect=replace(
                    second_effect,
                    status=ProtectedEffectStatus.RECEIPT_CLAIMED,
                    updated_at=_NOW + timedelta(seconds=1),
                    version=4,
                ),
                outbox_entry=_outbox("2"),
            )
        connection.commit()


def test_authority_constraints_reject_noncanonical_capabilities(
    postgres_074: PostgresHarness,
) -> None:
    with postgres_074.connection() as connection:
        with connection.cursor() as cursor, check_violation():
            cursor.execute(
                _INSERT_BINDING_SQL,
                _raw_binding_values(
                    "constraint",
                    capabilities=["astral.tools.write", "astral.tools.read"],
                ),
            )
        connection.rollback()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM astralplane_authority_binding"
            )
            assert cursor.fetchone()["count"] == 0


def test_concurrent_nonterminal_binding_insert_has_one_typed_winner(
    postgres_074: PostgresHarness,
) -> None:
    repository = api.create_authority_repository()
    first = _binding("concurrent-1")
    second = _binding("concurrent-2")
    started = Event()

    def contend() -> BaseException | None:
        with postgres_074.connection() as connection:
            transaction = PostgresTransaction(connection)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '5s'")
            started.set()
            try:
                repository.create_binding(transaction, second)
            except BaseException as exc:
                connection.rollback()
                return exc
            connection.commit()
            return None

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        repository.create_binding(transaction, first)
        with ThreadPoolExecutor(max_workers=1) as executor:
            contender = executor.submit(contend)
            assert started.wait(timeout=5)
            connection.commit()
            outcome = contender.result(timeout=10)

    assert isinstance(outcome, AuthorityIdempotencyConflictError)
    with postgres_074.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
                SELECT COUNT(*) AS count
                FROM astralplane_authority_binding
                WHERE owner_id = %s AND agent_id = %s
                  AND state NOT IN ('closed', 'revoked', 'expired')
                """,
            (first.owner_id, first.agent_id),
        )
        assert cursor.fetchone()["count"] == 1


def test_claim_and_outbox_failure_roll_back_to_the_savepoint(
    postgres_074: PostgresHarness,
) -> None:
    repository = api.create_authority_repository()
    outbox = api.create_outbox_store()
    binding = _binding()
    effect = _effect(binding)
    claim = _claim(effect)
    conflict_key = "authority-claim-conflict"
    existing_event = _outbox(
        "existing",
        idempotency_key=conflict_key,
        payload=b'{"different":true}',
    )
    requested_event = _outbox("1", idempotency_key=conflict_key)

    with postgres_074.connection() as connection:
        transaction = PostgresTransaction(connection)
        repository.create_binding(transaction, binding)
        repository.create_protected_effect(transaction, effect)
        outbox.enqueue(transaction, existing_event)

        with pytest.raises(PlaneError, match="idempotency key"):
            repository.claim_receipt(
                transaction,
                claim=claim,
                watermark=_watermark(claim),
                claimed_effect=replace(
                    effect,
                    status=ProtectedEffectStatus.RECEIPT_CLAIMED,
                    updated_at=_NOW + timedelta(seconds=1),
                    version=4,
                ),
                outbox_entry=requested_event,
            )

        assert transaction.fetch_one(
            "SELECT receipt_id FROM astralplane_receipt_claim WHERE receipt_id = %s",
            (claim.receipt_id,),
        ) is None
        assert transaction.fetch_one(
            """
            SELECT last_sequence FROM astralplane_receipt_sequence_watermark
            WHERE warden_id = %s AND lease_id = %s AND audience = %s
            """,
            claim.sequence_watermark_key,
        ) is None
        durable_effect = repository.get_protected_effect(
            transaction,
            owner_id=effect.owner_id,
            operation_id=effect.operation_id,
        )
        assert durable_effect == effect
        assert transaction.fetch_one(
            "SELECT entry_id FROM astralplane_outbox WHERE idempotency_key = %s",
            (conflict_key,),
        ) == {"entry_id": existing_event.entry_id}
        connection.commit()
