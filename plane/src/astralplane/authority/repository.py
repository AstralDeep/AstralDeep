"""Owner-isolated PostgreSQL repositories for durable external authority (claims,
effects, lifecycle, bindings); owns only persistence invariants like optimistic
fences and replay-safe receipts, never authorization policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, TypeVar

from astralplane.authority.claims import (
    ExternalAuthorityAnchorMetadata,
    ReceiptClaim,
    ReceiptSequenceWatermark,
)
from astralplane.authority.effects import (
    AstralToolScope,
    ProtectedEffectOperation,
    ProtectedEffectStatus,
)
from astralplane.authority.lifecycle import (
    AuthorityLifecycleKind,
    AuthorityLifecycleOperation,
    AuthorityLifecycleStatus,
)
from astralplane.authority.models import (
    AgentAuthorityBinding,
    AuthorityBindingState,
    AuthorityPopulation,
    pending_authority_identity,
)
from astralplane.contracts import OutboxEntry, OutboxStore, Record, Transaction
from astralplane.domain import require_identifier, require_utc
from astralplane.errors import DomainValidationError
from astralplane.outbox import PostgresOutboxStore
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)


class AuthorityIdempotencyConflictError(RepositoryConflictError):
    default_code = "authority_idempotency_conflict"


class AuthorityCompareAndSetConflictError(RepositoryConflictError):
    default_code = "authority_compare_and_set_conflict"


class ReceiptClaimConflictError(RepositoryConflictError):
    default_code = "authority_receipt_claim_conflict"


class ReceiptWatermarkConflictError(RepositoryConflictError):
    default_code = "authority_receipt_watermark_conflict"


_SAVEPOINT: Final = "astralplane_authority_claim"

_EFFECT_TRANSITIONS: Final = {
    ProtectedEffectStatus.CREATED: frozenset(
        {
            ProtectedEffectStatus.ASTRAL_AUTHORIZED,
            ProtectedEffectStatus.DENIED,
            ProtectedEffectStatus.FAILED_CLOSED,
        }
    ),
    ProtectedEffectStatus.ASTRAL_AUTHORIZED: frozenset(
        {
            ProtectedEffectStatus.LETS_PENDING,
            ProtectedEffectStatus.DENIED,
            ProtectedEffectStatus.FAILED_CLOSED,
        }
    ),
    ProtectedEffectStatus.LETS_PENDING: frozenset(
        {
            ProtectedEffectStatus.RECEIPT_RECEIVED,
            ProtectedEffectStatus.DENIED,
            ProtectedEffectStatus.FAILED_CLOSED,
        }
    ),
    ProtectedEffectStatus.RECEIPT_RECEIVED: frozenset(
        {
            ProtectedEffectStatus.RECEIPT_CLAIMED,
            ProtectedEffectStatus.DENIED,
            ProtectedEffectStatus.FAILED_CLOSED,
        }
    ),
    ProtectedEffectStatus.RECEIPT_CLAIMED: frozenset(
        {
            ProtectedEffectStatus.EXECUTING,
            ProtectedEffectStatus.DENIED,
            ProtectedEffectStatus.FAILED_CLOSED,
        }
    ),
    ProtectedEffectStatus.EXECUTING: frozenset(
        {
            ProtectedEffectStatus.SUCCEEDED,
            ProtectedEffectStatus.EFFECT_FAILED,
            ProtectedEffectStatus.OUTCOME_UNCERTAIN,
        }
    ),
    ProtectedEffectStatus.OUTCOME_UNCERTAIN: frozenset(
        {
            ProtectedEffectStatus.SUCCEEDED,
            ProtectedEffectStatus.EFFECT_FAILED,
        }
    ),
}

_INSERT_BINDING: Final = """
INSERT INTO astralplane_authority_binding (
    binding_id, owner_id, agent_id, runtime_id, runtime_generation, population,
    tenant_id, envelope_id, warden_id, lease_id, lineage_id, subject_id,
    policy_digest, machine_digest, config_epoch, capabilities, lease_sequence,
    lease_expires_at_ns, state, created_at, updated_at, version
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT DO NOTHING
RETURNING *
""".strip()

_READ_BINDING_CONFLICT: Final = """
SELECT * FROM astralplane_authority_binding
WHERE owner_id = %s
  AND (
      binding_id = %s
      OR (agent_id = %s AND population = %s AND state NOT IN ('closed', 'revoked', 'expired'))
      OR (agent_id = %s AND runtime_id = %s AND runtime_generation = %s)
  )
FOR UPDATE
""".strip()

_GET_BINDING: Final = """
SELECT * FROM astralplane_authority_binding
WHERE owner_id = %s AND binding_id = %s
""".strip()

_GET_ACTIVE_BINDING: Final = """
SELECT * FROM astralplane_authority_binding
WHERE owner_id = %s
  AND agent_id = %s
  AND runtime_id = %s
  AND runtime_generation = %s
  AND state = 'active'
""".strip()

_GET_LATEST_BINDING: Final = """
SELECT * FROM astralplane_authority_binding
WHERE owner_id = %s AND agent_id = %s AND population = %s
ORDER BY created_at DESC, binding_id DESC
LIMIT 1
""".strip()

_LOCK_BINDING: Final = f"{_GET_BINDING}\nFOR UPDATE"

_TRANSITION_BINDING: Final = """
UPDATE astralplane_authority_binding
SET lease_sequence = %s, lease_expires_at_ns = %s, state = %s,
    updated_at = %s, version = %s
WHERE owner_id = %s AND binding_id = %s
  AND agent_id = %s AND runtime_id = %s AND runtime_generation = %s
  AND population = %s AND tenant_id = %s AND envelope_id = %s
  AND warden_id = %s AND lease_id = %s AND lineage_id = %s AND subject_id = %s
  AND policy_digest = %s AND machine_digest = %s AND config_epoch = %s
  AND capabilities = %s AND state = %s AND version = %s
RETURNING *
""".strip()

_ACTIVATE_BINDING: Final = """
UPDATE astralplane_authority_binding
SET warden_id = %s, lease_id = %s, lineage_id = %s, subject_id = %s,
    lease_sequence = %s, lease_expires_at_ns = %s, state = %s,
    updated_at = %s, version = %s
WHERE owner_id = %s AND binding_id = %s
  AND agent_id = %s AND runtime_id = %s AND runtime_generation = %s
  AND population = %s AND tenant_id = %s AND envelope_id = %s
  AND policy_digest = %s AND machine_digest = %s AND config_epoch = %s
  AND capabilities = %s AND created_at = %s
  AND warden_id = %s AND lease_id = %s AND lineage_id = %s AND subject_id = %s
  AND lease_sequence = 0 AND lease_expires_at_ns = 0
  AND state = 'provisioning' AND version = %s
RETURNING *
""".strip()

_INSERT_LIFECYCLE: Final = """
INSERT INTO astralplane_authority_lifecycle_operation (
    operation_id, owner_id, binding_id, kind, expected_binding_version,
    expected_lease_sequence, request_fingerprint, status, remote_request_id,
    result_digest, error_code, attempt_count, next_attempt_at, last_attempt_at,
    reconciled_at, reconciliation_digest, created_at, updated_at, version
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT DO NOTHING
RETURNING *
""".strip()

_GET_LIFECYCLE: Final = """
SELECT * FROM astralplane_authority_lifecycle_operation
WHERE owner_id = %s AND operation_id = %s
""".strip()

_LOCK_LIFECYCLE: Final = f"{_GET_LIFECYCLE}\nFOR UPDATE"

_LIST_RECOVERABLE_LIFECYCLE: Final = """
SELECT * FROM astralplane_authority_lifecycle_operation
WHERE owner_id = %s
  AND status IN ('pending', 'in_flight', 'uncertain')
  AND next_attempt_at IS NOT NULL
  AND next_attempt_at <= %s
ORDER BY next_attempt_at, operation_id
FOR UPDATE SKIP LOCKED
LIMIT %s
""".strip()

_TRANSITION_LIFECYCLE: Final = """
UPDATE astralplane_authority_lifecycle_operation
SET status = %s, result_digest = %s, error_code = %s, attempt_count = %s,
    next_attempt_at = %s, last_attempt_at = %s, reconciled_at = %s,
    reconciliation_digest = %s, updated_at = %s, version = %s
WHERE owner_id = %s AND operation_id = %s AND binding_id = %s
  AND kind = %s AND expected_binding_version = %s
  AND expected_lease_sequence IS NOT DISTINCT FROM %s
  AND request_fingerprint = %s AND remote_request_id = %s
  AND status = %s AND version = %s
RETURNING *
""".strip()

_INSERT_EFFECT: Final = """
INSERT INTO astralplane_protected_effect_operation (
    operation_id, owner_id, agent_id, binding_id, tool_id, astral_scope,
    lets_capability, lets_transition, executor_audience, nonce, effect_digest,
    expected_sequence, audit_correlation_id, status, receipt_id, receipt_digest,
    effect_result_digest, error_code, created_at, updated_at, version
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT DO NOTHING
RETURNING *
""".strip()

_READ_EFFECT_CONFLICT: Final = """
SELECT * FROM astralplane_protected_effect_operation
WHERE owner_id = %s
  AND (
      operation_id = %s
      OR (binding_id = %s AND executor_audience = %s AND nonce = %s)
  )
FOR UPDATE
""".strip()

_GET_EFFECT: Final = """
SELECT * FROM astralplane_protected_effect_operation
WHERE owner_id = %s AND operation_id = %s
""".strip()

_LOCK_EFFECT: Final = f"{_GET_EFFECT}\nFOR UPDATE"

_RECOVERABLE_EFFECT_STATUSES: Final = frozenset(
    {
        ProtectedEffectStatus.CREATED,
        ProtectedEffectStatus.ASTRAL_AUTHORIZED,
        ProtectedEffectStatus.LETS_PENDING,
        ProtectedEffectStatus.RECEIPT_RECEIVED,
        ProtectedEffectStatus.RECEIPT_CLAIMED,
        ProtectedEffectStatus.EXECUTING,
        ProtectedEffectStatus.OUTCOME_UNCERTAIN,
    }
)

_LIST_RECOVERABLE_EFFECTS: Final = """
SELECT * FROM astralplane_protected_effect_operation
WHERE owner_id = %s
  AND status IN (
      'created', 'astral_authorized', 'lets_pending', 'receipt_received',
      'receipt_claimed', 'executing', 'outcome_uncertain'
  )
  AND updated_at < %s
ORDER BY updated_at, operation_id
FOR UPDATE SKIP LOCKED
LIMIT %s
""".strip()

_LIST_RECOVERY_OWNERS: Final = """
SELECT owner_id, MIN(recovery_at) AS recovery_at
FROM (
    SELECT owner_id, next_attempt_at AS recovery_at
    FROM astralplane_authority_lifecycle_operation
    WHERE status IN ('pending', 'in_flight', 'uncertain')
      AND next_attempt_at IS NOT NULL
      AND next_attempt_at <= %s
    UNION ALL
    SELECT owner_id, updated_at AS recovery_at
    FROM astralplane_protected_effect_operation
    WHERE status IN (
        'created', 'astral_authorized', 'lets_pending', 'receipt_received',
        'receipt_claimed', 'executing', 'outcome_uncertain'
    )
      AND updated_at < %s
) AS recoverable
GROUP BY owner_id
ORDER BY MIN(recovery_at), owner_id
LIMIT %s
""".strip()

_TRANSITION_EFFECT: Final = """
UPDATE astralplane_protected_effect_operation
SET status = %s, receipt_id = %s, receipt_digest = %s,
    effect_result_digest = %s, error_code = %s, updated_at = %s, version = %s
WHERE owner_id = %s AND operation_id = %s AND agent_id = %s AND binding_id = %s
  AND tool_id = %s AND astral_scope = %s AND lets_capability = %s
  AND lets_transition = %s AND executor_audience = %s AND nonce = %s
  AND effect_digest = %s AND expected_sequence = %s AND audit_correlation_id = %s
  AND status = %s AND version = %s
RETURNING *
""".strip()

_INSERT_CLAIM: Final = """
INSERT INTO astralplane_receipt_claim (
    receipt_id, operation_id, owner_id, binding_id, tenant_id, envelope_id,
    warden_id, lease_id, subject_id, lineage_id, policy_digest, machine_digest,
    config_epoch, audience, transition, nonce, resulting_sequence,
    evidence_digest, issued_at_ns, expires_at_ns, claimed_at, canonical_digest,
    anchor_format, anchor_executor_policy_sha256, anchor_trust_registry_sha256,
    anchor_schema_version, anchor_database_instance_id, anchor_claim_sequence,
    anchor_claim_digest, anchor_clock_floor_ns, anchor_confirmed_at
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT DO NOTHING
RETURNING *
""".strip()

_READ_CLAIM_CONFLICT: Final = """
SELECT * FROM astralplane_receipt_claim
WHERE owner_id = %s
  AND (
      receipt_id = %s OR operation_id = %s OR canonical_digest = %s
      OR (tenant_id = %s AND envelope_id = %s AND audience = %s AND nonce = %s)
      OR (warden_id = %s AND lease_id = %s AND audience = %s AND resulting_sequence = %s)
  )
FOR UPDATE
""".strip()

_ADVANCE_WATERMARK: Final = """
INSERT INTO astralplane_receipt_sequence_watermark (
    warden_id, lease_id, audience, last_sequence, updated_at, expires_at_ns, version
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (warden_id, lease_id, audience) DO UPDATE
SET last_sequence = EXCLUDED.last_sequence,
    updated_at = EXCLUDED.updated_at,
    expires_at_ns = EXCLUDED.expires_at_ns,
    version = EXCLUDED.version
WHERE astralplane_receipt_sequence_watermark.last_sequence < EXCLUDED.last_sequence
  AND astralplane_receipt_sequence_watermark.version + 1 = EXCLUDED.version
  AND astralplane_receipt_sequence_watermark.updated_at <= EXCLUDED.updated_at
RETURNING *
""".strip()


_T = TypeVar("_T")


def _require_model(value: object, expected: type[_T], name: str) -> _T:
    if not isinstance(value, expected):
        raise RepositoryValidationError(f"{name} must be a validated {expected.__name__}")
    return value


def _row_value(row: Mapping[str, Any], field: str) -> Any:
    try:
        return row[field]
    except KeyError as exc:
        raise RepositoryDataError(
            "authority row is missing a required field",
            metadata={"field": field},
        ) from exc


def _decode(row: Record, factory: type[_T], **values: object) -> _T:
    try:
        return factory(**values)  # type: ignore[arg-type]
    except (DomainValidationError, TypeError, ValueError) as exc:
        raise RepositoryDataError("persisted authority row is invalid") from exc


def _enum_member(expected: type[_T], value: object) -> _T:
    try:
        return expected(value)  # type: ignore[call-arg]
    except (TypeError, ValueError) as exc:
        raise RepositoryDataError("persisted authority enum is invalid") from exc


def _capability_tuple(value: object) -> tuple[str, ...]:
    if type(value) not in {list, tuple} or any(type(item) is not str for item in value):
        raise RepositoryDataError("persisted capabilities are invalid")
    return tuple(value)


def _binding_from_row(row: Record) -> AgentAuthorityBinding:
    return _decode(
        row,
        AgentAuthorityBinding,
        binding_id=_row_value(row, "binding_id"),
        owner_id=_row_value(row, "owner_id"),
        agent_id=_row_value(row, "agent_id"),
        runtime_id=_row_value(row, "runtime_id"),
        runtime_generation=_row_value(row, "runtime_generation"),
        population=_enum_member(AuthorityPopulation, _row_value(row, "population")),
        tenant_id=_row_value(row, "tenant_id"),
        envelope_id=_row_value(row, "envelope_id"),
        warden_id=_row_value(row, "warden_id"),
        lease_id=_row_value(row, "lease_id"),
        lineage_id=_row_value(row, "lineage_id"),
        subject_id=_row_value(row, "subject_id"),
        policy_digest=_row_value(row, "policy_digest"),
        machine_digest=_row_value(row, "machine_digest"),
        config_epoch=_row_value(row, "config_epoch"),
        capabilities=_capability_tuple(_row_value(row, "capabilities")),
        lease_sequence=_row_value(row, "lease_sequence"),
        lease_expires_at_ns=_row_value(row, "lease_expires_at_ns"),
        state=_enum_member(AuthorityBindingState, _row_value(row, "state")),
        created_at=_row_value(row, "created_at"),
        updated_at=_row_value(row, "updated_at"),
        version=_row_value(row, "version"),
    )


def _lifecycle_from_row(row: Record) -> AuthorityLifecycleOperation:
    return _decode(
        row,
        AuthorityLifecycleOperation,
        operation_id=_row_value(row, "operation_id"),
        owner_id=_row_value(row, "owner_id"),
        binding_id=_row_value(row, "binding_id"),
        kind=_enum_member(AuthorityLifecycleKind, _row_value(row, "kind")),
        expected_binding_version=_row_value(row, "expected_binding_version"),
        expected_lease_sequence=_row_value(row, "expected_lease_sequence"),
        request_fingerprint=_row_value(row, "request_fingerprint"),
        status=_enum_member(AuthorityLifecycleStatus, _row_value(row, "status")),
        remote_request_id=_row_value(row, "remote_request_id"),
        result_digest=_row_value(row, "result_digest"),
        error_code=_row_value(row, "error_code"),
        attempt_count=_row_value(row, "attempt_count"),
        next_attempt_at=_row_value(row, "next_attempt_at"),
        last_attempt_at=_row_value(row, "last_attempt_at"),
        reconciled_at=_row_value(row, "reconciled_at"),
        reconciliation_digest=_row_value(row, "reconciliation_digest"),
        created_at=_row_value(row, "created_at"),
        updated_at=_row_value(row, "updated_at"),
        version=_row_value(row, "version"),
    )


def _effect_from_row(row: Record) -> ProtectedEffectOperation:
    return _decode(
        row,
        ProtectedEffectOperation,
        operation_id=_row_value(row, "operation_id"),
        owner_id=_row_value(row, "owner_id"),
        agent_id=_row_value(row, "agent_id"),
        binding_id=_row_value(row, "binding_id"),
        tool_id=_row_value(row, "tool_id"),
        astral_scope=_enum_member(AstralToolScope, _row_value(row, "astral_scope")),
        lets_capability=_row_value(row, "lets_capability"),
        lets_transition=_row_value(row, "lets_transition"),
        executor_audience=_row_value(row, "executor_audience"),
        nonce=_row_value(row, "nonce"),
        effect_digest=_row_value(row, "effect_digest"),
        expected_sequence=_row_value(row, "expected_sequence"),
        audit_correlation_id=_row_value(row, "audit_correlation_id"),
        status=_enum_member(ProtectedEffectStatus, _row_value(row, "status")),
        receipt_id=_row_value(row, "receipt_id"),
        receipt_digest=_row_value(row, "receipt_digest"),
        effect_result_digest=_row_value(row, "effect_result_digest"),
        error_code=_row_value(row, "error_code"),
        created_at=_row_value(row, "created_at"),
        updated_at=_row_value(row, "updated_at"),
        version=_row_value(row, "version"),
    )


def _watermark_from_row(row: Record) -> ReceiptSequenceWatermark:
    return _decode(
        row,
        ReceiptSequenceWatermark,
        warden_id=_row_value(row, "warden_id"),
        lease_id=_row_value(row, "lease_id"),
        audience=_row_value(row, "audience"),
        last_sequence=_row_value(row, "last_sequence"),
        updated_at=_row_value(row, "updated_at"),
        expires_at_ns=_row_value(row, "expires_at_ns"),
        version=_row_value(row, "version"),
    )


def _claim_from_row(row: Record) -> ReceiptClaim:
    anchor = _decode(
        row,
        ExternalAuthorityAnchorMetadata,
        anchor_format=_row_value(row, "anchor_format"),
        audience=_row_value(row, "audience"),
        tenant_id=_row_value(row, "tenant_id"),
        envelope_id=_row_value(row, "envelope_id"),
        config_epoch=_row_value(row, "config_epoch"),
        executor_policy_sha256=_row_value(row, "anchor_executor_policy_sha256"),
        trust_registry_sha256=_row_value(row, "anchor_trust_registry_sha256"),
        schema_version=_row_value(row, "anchor_schema_version"),
        database_instance_id=_row_value(row, "anchor_database_instance_id"),
        claim_sequence=_row_value(row, "anchor_claim_sequence"),
        claim_digest=_row_value(row, "anchor_claim_digest"),
        clock_floor_ns=_row_value(row, "anchor_clock_floor_ns"),
        confirmed_at=_row_value(row, "anchor_confirmed_at"),
    )
    return _decode(
        row,
        ReceiptClaim,
        receipt_id=_row_value(row, "receipt_id"),
        operation_id=_row_value(row, "operation_id"),
        owner_id=_row_value(row, "owner_id"),
        binding_id=_row_value(row, "binding_id"),
        tenant_id=_row_value(row, "tenant_id"),
        envelope_id=_row_value(row, "envelope_id"),
        warden_id=_row_value(row, "warden_id"),
        lease_id=_row_value(row, "lease_id"),
        subject_id=_row_value(row, "subject_id"),
        lineage_id=_row_value(row, "lineage_id"),
        policy_digest=_row_value(row, "policy_digest"),
        machine_digest=_row_value(row, "machine_digest"),
        config_epoch=_row_value(row, "config_epoch"),
        audience=_row_value(row, "audience"),
        transition=_row_value(row, "transition"),
        nonce=_row_value(row, "nonce"),
        resulting_sequence=_row_value(row, "resulting_sequence"),
        evidence_digest=_row_value(row, "evidence_digest"),
        issued_at_ns=_row_value(row, "issued_at_ns"),
        expires_at_ns=_row_value(row, "expires_at_ns"),
        claimed_at=_row_value(row, "claimed_at"),
        canonical_digest=_row_value(row, "canonical_digest"),
        authority_anchor=anchor,
    )


def _binding_values(value: AgentAuthorityBinding) -> tuple[object, ...]:
    return (
        value.binding_id,
        value.owner_id,
        value.agent_id,
        value.runtime_id,
        value.runtime_generation,
        value.population.value,
        value.tenant_id,
        value.envelope_id,
        value.warden_id,
        value.lease_id,
        value.lineage_id,
        value.subject_id,
        value.policy_digest,
        value.machine_digest,
        value.config_epoch,
        list(value.capabilities),
        value.lease_sequence,
        value.lease_expires_at_ns,
        value.state.value,
        value.created_at,
        value.updated_at,
        value.version,
    )


def _lifecycle_values(value: AuthorityLifecycleOperation) -> tuple[object, ...]:
    return (
        value.operation_id,
        value.owner_id,
        value.binding_id,
        value.kind.value,
        value.expected_binding_version,
        value.expected_lease_sequence,
        value.request_fingerprint,
        value.status.value,
        value.remote_request_id,
        value.result_digest,
        value.error_code,
        value.attempt_count,
        value.next_attempt_at,
        value.last_attempt_at,
        value.reconciled_at,
        value.reconciliation_digest,
        value.created_at,
        value.updated_at,
        value.version,
    )


def _effect_values(value: ProtectedEffectOperation) -> tuple[object, ...]:
    return (
        value.operation_id,
        value.owner_id,
        value.agent_id,
        value.binding_id,
        value.tool_id,
        value.astral_scope.value,
        value.lets_capability,
        value.lets_transition,
        value.executor_audience,
        value.nonce,
        value.effect_digest,
        value.expected_sequence,
        value.audit_correlation_id,
        value.status.value,
        value.receipt_id,
        value.receipt_digest,
        value.effect_result_digest,
        value.error_code,
        value.created_at,
        value.updated_at,
        value.version,
    )


def _claim_values(value: ReceiptClaim) -> tuple[object, ...]:
    anchor = value.authority_anchor
    return (
        value.receipt_id,
        value.operation_id,
        value.owner_id,
        value.binding_id,
        value.tenant_id,
        value.envelope_id,
        value.warden_id,
        value.lease_id,
        value.subject_id,
        value.lineage_id,
        value.policy_digest,
        value.machine_digest,
        value.config_epoch,
        value.audience,
        value.transition,
        value.nonce,
        value.resulting_sequence,
        value.evidence_digest,
        value.issued_at_ns,
        value.expires_at_ns,
        value.claimed_at,
        value.canonical_digest,
        anchor.anchor_format,
        anchor.executor_policy_sha256,
        anchor.trust_registry_sha256,
        anchor.schema_version,
        anchor.database_instance_id,
        anchor.claim_sequence,
        anchor.claim_digest,
        anchor.clock_floor_ns,
        anchor.confirmed_at,
    )


def _same_lifecycle_request(
    left: AuthorityLifecycleOperation,
    right: AuthorityLifecycleOperation,
) -> bool:
    return (
        left.owner_id,
        left.operation_id,
        left.binding_id,
        left.kind,
        left.expected_binding_version,
        left.expected_lease_sequence,
        left.request_fingerprint,
        left.remote_request_id,
    ) == (
        right.owner_id,
        right.operation_id,
        right.binding_id,
        right.kind,
        right.expected_binding_version,
        right.expected_lease_sequence,
        right.request_fingerprint,
        right.remote_request_id,
    )


def _same_effect_intent(
    left: ProtectedEffectOperation,
    right: ProtectedEffectOperation,
) -> bool:
    return _effect_values(left)[:13] == _effect_values(right)[:13] and (
        left.created_at == right.created_at
    )


def _require_next_version(replacement_version: int, expected_version: int) -> None:
    if (
        type(expected_version) is not int
        or expected_version < 0
        or replacement_version != expected_version + 1
    ):
        raise RepositoryValidationError(
            "replacement version must advance the expected version exactly once"
        )


def _require_query_identifier(value: object, *, field: str) -> str:
    try:
        return require_identifier(value, field=field)  # type: ignore[arg-type]
    except DomainValidationError as exc:
        raise RepositoryValidationError(f"{field} is invalid") from exc


class AuthorityRepository:
    def __init__(self, outbox: OutboxStore | None = None) -> None:
        self._outbox = PostgresOutboxStore() if outbox is None else outbox

    def create_binding(
        self,
        transaction: Transaction,
        binding: AgentAuthorityBinding,
    ) -> AgentAuthorityBinding:
        exact = _require_model(binding, AgentAuthorityBinding, "binding")
        row = transaction.fetch_one(_INSERT_BINDING, _binding_values(exact))
        if row is not None:
            persisted = _binding_from_row(row)
            if persisted != exact:
                raise RepositoryDataError("binding insert returned different data")
            return persisted
        row = transaction.fetch_one(
            _READ_BINDING_CONFLICT,
            (
                exact.owner_id,
                exact.binding_id,
                exact.agent_id,
                exact.population.value,
                exact.agent_id,
                exact.runtime_id,
                exact.runtime_generation,
            ),
        )
        if row is not None and _binding_from_row(row) == exact:
            return exact
        raise AuthorityIdempotencyConflictError("binding identity represents different work")

    def get_binding(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        binding_id: str,
    ) -> AgentAuthorityBinding | None:
        row = transaction.fetch_one(_GET_BINDING, (owner_id, binding_id))
        return None if row is None else _binding_from_row(row)

    def get_active_binding(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        runtime_id: str,
        runtime_generation: int,
    ) -> AgentAuthorityBinding | None:
        owner = _require_query_identifier(owner_id, field="owner id")
        agent = _require_query_identifier(agent_id, field="agent id")
        runtime = _require_query_identifier(runtime_id, field="runtime id")
        if type(runtime_generation) is not int or runtime_generation <= 0:
            raise RepositoryValidationError("runtime generation must be a positive integer")
        row = transaction.fetch_one(
            _GET_ACTIVE_BINDING,
            (owner, agent, runtime, runtime_generation),
        )
        if row is None:
            return None
        binding = _binding_from_row(row)
        if (
            binding.owner_id,
            binding.agent_id,
            binding.runtime_id,
            binding.runtime_generation,
            binding.state,
        ) != (
            owner,
            agent,
            runtime,
            runtime_generation,
            AuthorityBindingState.ACTIVE,
        ):
            raise RepositoryDataError("active binding query returned a row outside its exact scope")
        return binding

    def get_latest_binding(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        agent_id: str,
        population: AuthorityPopulation,
    ) -> AgentAuthorityBinding | None:
        owner = _require_query_identifier(owner_id, field="owner id")
        agent = _require_query_identifier(agent_id, field="agent id")
        if not isinstance(population, AuthorityPopulation):
            raise RepositoryValidationError("population must be an authority population")
        row = transaction.fetch_one(
            _GET_LATEST_BINDING,
            (owner, agent, population.value),
        )
        if row is None:
            return None
        binding = _binding_from_row(row)
        if (binding.owner_id, binding.agent_id, binding.population) != (
            owner,
            agent,
            population,
        ):
            raise RepositoryDataError(
                "latest binding query returned a row outside its population scope"
            )
        return binding

    def transition_binding(
        self,
        transaction: Transaction,
        replacement: AgentAuthorityBinding,
        *,
        expected_state: AuthorityBindingState,
        expected_version: int,
    ) -> AgentAuthorityBinding:
        exact = _require_model(replacement, AgentAuthorityBinding, "binding")
        if not isinstance(expected_state, AuthorityBindingState):
            raise RepositoryValidationError("expected state must be an authority binding state")
        _require_next_version(exact.version, expected_version)
        if expected_state.terminal:
            raise AuthorityCompareAndSetConflictError("terminal binding cannot reopen")
        parameters = (
            exact.lease_sequence,
            exact.lease_expires_at_ns,
            exact.state.value,
            exact.updated_at,
            exact.version,
            exact.owner_id,
            exact.binding_id,
            exact.agent_id,
            exact.runtime_id,
            exact.runtime_generation,
            exact.population.value,
            exact.tenant_id,
            exact.envelope_id,
            exact.warden_id,
            exact.lease_id,
            exact.lineage_id,
            exact.subject_id,
            exact.policy_digest,
            exact.machine_digest,
            exact.config_epoch,
            list(exact.capabilities),
            expected_state.value,
            expected_version,
        )
        row = transaction.fetch_one(_TRANSITION_BINDING, parameters)
        if row is None:
            raise AuthorityCompareAndSetConflictError("binding compare-and-set fence is stale")
        persisted = _binding_from_row(row)
        if persisted != exact:
            raise RepositoryDataError("binding transition returned different data")
        return persisted

    def activate_binding(
        self,
        transaction: Transaction,
        replacement: AgentAuthorityBinding,
        *,
        expected_version: int,
    ) -> AgentAuthorityBinding:
        exact = _require_model(replacement, AgentAuthorityBinding, "binding")
        _require_next_version(exact.version, expected_version)
        if exact.state is not AuthorityBindingState.ACTIVE:
            raise RepositoryValidationError("activated binding must enter the active state")
        pending = (
            pending_authority_identity(exact.binding_id, field="warden"),
            pending_authority_identity(exact.binding_id, field="lease"),
            pending_authority_identity(exact.binding_id, field="lineage"),
            pending_authority_identity(exact.binding_id, field="subject"),
        )
        row = transaction.fetch_one(
            _ACTIVATE_BINDING,
            (
                exact.warden_id,
                exact.lease_id,
                exact.lineage_id,
                exact.subject_id,
                exact.lease_sequence,
                exact.lease_expires_at_ns,
                exact.state.value,
                exact.updated_at,
                exact.version,
                exact.owner_id,
                exact.binding_id,
                exact.agent_id,
                exact.runtime_id,
                exact.runtime_generation,
                exact.population.value,
                exact.tenant_id,
                exact.envelope_id,
                exact.policy_digest,
                exact.machine_digest,
                exact.config_epoch,
                list(exact.capabilities),
                exact.created_at,
                *pending,
                expected_version,
            ),
        )
        if row is None:
            raise AuthorityCompareAndSetConflictError(
                "binding activation compare-and-set fence is stale"
            )
        persisted = _binding_from_row(row)
        if persisted != exact:
            raise RepositoryDataError("binding activation returned different data")
        return persisted

    def abandon_provisioning_binding(
        self,
        transaction: Transaction,
        replacement: AgentAuthorityBinding,
        *,
        expected_version: int,
    ) -> AgentAuthorityBinding:
        exact = _require_model(replacement, AgentAuthorityBinding, "binding")
        pending = (
            pending_authority_identity(exact.binding_id, field="warden"),
            pending_authority_identity(exact.binding_id, field="lease"),
            pending_authority_identity(exact.binding_id, field="lineage"),
            pending_authority_identity(exact.binding_id, field="subject"),
        )
        if exact.state is not AuthorityBindingState.CLOSED:
            raise RepositoryValidationError(
                "abandoned provisioning binding must enter the closed state"
            )
        if (
            (
                exact.warden_id,
                exact.lease_id,
                exact.lineage_id,
                exact.subject_id,
            )
            != pending
            or exact.lease_sequence != 0
            or exact.lease_expires_at_ns != 0
        ):
            raise RepositoryValidationError(
                "abandoned provisioning binding must retain its pending authority fence"
            )
        return self.transition_binding(
            transaction,
            exact,
            expected_state=AuthorityBindingState.PROVISIONING,
            expected_version=expected_version,
        )

    def create_lifecycle_operation(
        self,
        transaction: Transaction,
        operation: AuthorityLifecycleOperation,
    ) -> AuthorityLifecycleOperation:
        exact = _require_model(operation, AuthorityLifecycleOperation, "lifecycle operation")
        row = transaction.fetch_one(_INSERT_LIFECYCLE, _lifecycle_values(exact))
        if row is not None:
            persisted = _lifecycle_from_row(row)
            if persisted != exact:
                raise RepositoryDataError("lifecycle insert returned different data")
            return persisted
        row = transaction.fetch_one(
            _LOCK_LIFECYCLE,
            (exact.owner_id, exact.operation_id),
        )
        if row is not None:
            persisted = _lifecycle_from_row(row)
            if _same_lifecycle_request(persisted, exact):
                return persisted
        raise AuthorityIdempotencyConflictError(
            "lifecycle operation fingerprint represents different work"
        )

    def get_lifecycle_operation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        operation_id: str,
    ) -> AuthorityLifecycleOperation | None:
        row = transaction.fetch_one(_GET_LIFECYCLE, (owner_id, operation_id))
        return None if row is None else _lifecycle_from_row(row)

    def list_recoverable_lifecycle_operations(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        due_at: datetime,
        limit: int = 50,
    ) -> tuple[AuthorityLifecycleOperation, ...]:
        try:
            owner = require_identifier(owner_id, field="owner id")
            due = require_utc(due_at, field="due at")
        except DomainValidationError as exc:
            raise RepositoryValidationError(
                "lifecycle recovery scope is invalid"
            ) from exc
        if type(limit) is not int or not 1 <= limit <= 200:
            raise RepositoryValidationError(
                "lifecycle recovery limit must be between 1 and 200"
            )
        rows = transaction.fetch_all(
            _LIST_RECOVERABLE_LIFECYCLE,
            (owner, due, limit),
        )
        return tuple(_lifecycle_from_row(row) for row in rows)

    def transition_lifecycle_operation(
        self,
        transaction: Transaction,
        replacement: AuthorityLifecycleOperation,
        *,
        expected_status: AuthorityLifecycleStatus,
        expected_version: int,
    ) -> AuthorityLifecycleOperation:
        exact = _require_model(
            replacement,
            AuthorityLifecycleOperation,
            "lifecycle operation",
        )
        if not isinstance(expected_status, AuthorityLifecycleStatus):
            raise RepositoryValidationError("expected status must be a lifecycle status")
        _require_next_version(exact.version, expected_version)
        if expected_status.terminal:
            raise AuthorityCompareAndSetConflictError("terminal lifecycle operation cannot reopen")
        row = transaction.fetch_one(
            _TRANSITION_LIFECYCLE,
            (
                exact.status.value,
                exact.result_digest,
                exact.error_code,
                exact.attempt_count,
                exact.next_attempt_at,
                exact.last_attempt_at,
                exact.reconciled_at,
                exact.reconciliation_digest,
                exact.updated_at,
                exact.version,
                exact.owner_id,
                exact.operation_id,
                exact.binding_id,
                exact.kind.value,
                exact.expected_binding_version,
                exact.expected_lease_sequence,
                exact.request_fingerprint,
                exact.remote_request_id,
                expected_status.value,
                expected_version,
            ),
        )
        if row is None:
            raise AuthorityCompareAndSetConflictError("lifecycle compare-and-set fence is stale")
        persisted = _lifecycle_from_row(row)
        if persisted != exact:
            raise RepositoryDataError("lifecycle transition returned different data")
        return persisted

    def create_protected_effect(
        self,
        transaction: Transaction,
        operation: ProtectedEffectOperation,
    ) -> ProtectedEffectOperation:
        exact = _require_model(operation, ProtectedEffectOperation, "protected effect")
        row = transaction.fetch_one(_INSERT_EFFECT, _effect_values(exact))
        if row is not None:
            persisted = _effect_from_row(row)
            if persisted != exact:
                raise RepositoryDataError("protected effect insert returned different data")
            return persisted
        row = transaction.fetch_one(
            _READ_EFFECT_CONFLICT,
            (
                exact.owner_id,
                exact.operation_id,
                exact.binding_id,
                exact.executor_audience,
                exact.nonce,
            ),
        )
        if row is not None:
            persisted = _effect_from_row(row)
            if _same_effect_intent(persisted, exact):
                return persisted
        raise AuthorityIdempotencyConflictError("effect intent represents different work")

    def get_protected_effect(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        operation_id: str,
    ) -> ProtectedEffectOperation | None:
        row = transaction.fetch_one(_GET_EFFECT, (owner_id, operation_id))
        return None if row is None else _effect_from_row(row)

    # Must transition rows here; commit alone just releases locks
    def list_recoverable_protected_effects(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        updated_before: datetime,
        limit: int = 50,
    ) -> tuple[ProtectedEffectOperation, ...]:
        owner = _require_query_identifier(owner_id, field="owner id")
        try:
            cutoff = require_utc(updated_before, field="updated before")
        except DomainValidationError as exc:
            raise RepositoryValidationError("effect recovery cutoff must be UTC-aware") from exc
        if type(limit) is not int or not 1 <= limit <= 200:
            raise RepositoryValidationError("effect recovery limit must be between 1 and 200")
        rows = transaction.fetch_all(
            _LIST_RECOVERABLE_EFFECTS,
            (owner, cutoff, limit),
        )
        effects = tuple(_effect_from_row(row) for row in rows)
        if len(effects) > limit:
            raise RepositoryDataError("effect recovery query exceeded its limit")
        keys: list[tuple[datetime, str]] = []
        for effect in effects:
            if (
                effect.owner_id != owner
                or effect.status not in _RECOVERABLE_EFFECT_STATUSES
                or effect.updated_at >= cutoff
            ):
                raise RepositoryDataError(
                    "effect recovery query returned a row outside its stale owner scope"
                )
            keys.append((effect.updated_at, effect.operation_id))
        if keys != sorted(keys):
            raise RepositoryDataError(
                "effect recovery query returned nondeterministically ordered rows"
            )
        return effects

    def list_recovery_owners(
        self,
        transaction: Transaction,
        *,
        lifecycle_due_at: datetime,
        effect_updated_before: datetime,
        limit: int = 200,
    ) -> tuple[str, ...]:
        try:
            due = require_utc(lifecycle_due_at, field="lifecycle due at")
            cutoff = require_utc(
                effect_updated_before,
                field="effect updated before",
            )
        except DomainValidationError as exc:
            raise RepositoryValidationError("recovery owner cutoff must be UTC-aware") from exc
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RepositoryValidationError(
                "recovery owner limit must be between 1 and 1000"
            )
        rows = transaction.fetch_all(
            _LIST_RECOVERY_OWNERS,
            (due, cutoff, limit),
        )
        if len(rows) > limit:
            raise RepositoryDataError("recovery owner query exceeded its limit")
        owners: list[str] = []
        keys: list[tuple[datetime, str]] = []
        for row in rows:
            try:
                owner = require_identifier(
                    _row_value(row, "owner_id"),
                    field="owner id",
                )
                recovery_at = require_utc(
                    _row_value(row, "recovery_at"),
                    field="recovery at",
                )
            except DomainValidationError as exc:
                raise RepositoryDataError(
                    "recovery owner query returned invalid data"
                ) from exc
            owners.append(owner)
            keys.append((recovery_at, owner))
        if len(owners) != len(set(owners)) or keys != sorted(keys):
            raise RepositoryDataError(
                "recovery owner query returned nondeterministic partitions"
            )
        return tuple(owners)

    def transition_protected_effect(
        self,
        transaction: Transaction,
        replacement: ProtectedEffectOperation,
        *,
        expected_status: ProtectedEffectStatus,
        expected_version: int,
    ) -> ProtectedEffectOperation:
        exact = _require_model(replacement, ProtectedEffectOperation, "protected effect")
        if not isinstance(expected_status, ProtectedEffectStatus):
            raise RepositoryValidationError("expected status must be a protected effect status")
        _require_next_version(exact.version, expected_version)
        if exact.status not in _EFFECT_TRANSITIONS.get(expected_status, frozenset()):
            raise AuthorityCompareAndSetConflictError("protected effect transition is not allowed")
        row = transaction.fetch_one(
            _TRANSITION_EFFECT,
            (
                exact.status.value,
                exact.receipt_id,
                exact.receipt_digest,
                exact.effect_result_digest,
                exact.error_code,
                exact.updated_at,
                exact.version,
                exact.owner_id,
                exact.operation_id,
                exact.agent_id,
                exact.binding_id,
                exact.tool_id,
                exact.astral_scope.value,
                exact.lets_capability,
                exact.lets_transition,
                exact.executor_audience,
                exact.nonce,
                exact.effect_digest,
                exact.expected_sequence,
                exact.audit_correlation_id,
                expected_status.value,
                expected_version,
            ),
        )
        if row is None:
            raise AuthorityCompareAndSetConflictError("effect compare-and-set fence is stale")
        persisted = _effect_from_row(row)
        if persisted != exact:
            raise RepositoryDataError("effect transition returned different data")
        return persisted

    def claim_receipt(
        self,
        transaction: Transaction,
        *,
        claim: ReceiptClaim,
        watermark: ReceiptSequenceWatermark,
        claimed_effect: ProtectedEffectOperation,
        outbox_entry: OutboxEntry,
    ) -> ReceiptClaim:
        exact_claim = _require_model(claim, ReceiptClaim, "receipt claim")
        exact_watermark = _require_model(
            watermark,
            ReceiptSequenceWatermark,
            "receipt watermark",
        )
        exact_effect = _require_model(
            claimed_effect,
            ProtectedEffectOperation,
            "claimed effect",
        )
        _require_model(outbox_entry, OutboxEntry, "outbox entry")
        with transaction.savepoint(_SAVEPOINT) as atomic:
            binding_row = atomic.fetch_one(
                _LOCK_BINDING,
                (exact_claim.owner_id, exact_claim.binding_id),
            )
            effect_row = atomic.fetch_one(
                _LOCK_EFFECT,
                (exact_claim.owner_id, exact_claim.operation_id),
            )
            if binding_row is None or effect_row is None:
                raise ReceiptClaimConflictError("receipt owner binding or effect is unavailable")
            binding = _binding_from_row(binding_row)
            current_effect = _effect_from_row(effect_row)
            self._validate_claim_relationships(exact_claim, binding, current_effect)

            claim_row = atomic.fetch_one(_INSERT_CLAIM, _claim_values(exact_claim))
            if claim_row is None:
                existing_row = atomic.fetch_one(
                    _READ_CLAIM_CONFLICT,
                    (
                        exact_claim.owner_id,
                        exact_claim.receipt_id,
                        exact_claim.operation_id,
                        exact_claim.canonical_digest,
                        exact_claim.tenant_id,
                        exact_claim.envelope_id,
                        exact_claim.audience,
                        exact_claim.nonce,
                        exact_claim.warden_id,
                        exact_claim.lease_id,
                        exact_claim.audience,
                        exact_claim.resulting_sequence,
                    ),
                )
                if existing_row is not None:
                    existing = _claim_from_row(existing_row)
                    if existing == exact_claim:
                        self._require_existing_claim_effect(current_effect, exact_claim)
                        return existing
                raise ReceiptClaimConflictError("receipt uniqueness fence rejected the claim")

            persisted_claim = _claim_from_row(claim_row)
            if persisted_claim != exact_claim:
                raise RepositoryDataError("receipt insert returned different data")
            self._validate_new_claim_transition(current_effect, exact_effect, exact_claim)
            self._advance_watermark(atomic, exact_watermark, exact_claim)
            self.transition_protected_effect(
                atomic,
                exact_effect,
                expected_status=current_effect.status,
                expected_version=current_effect.version,
            )
            self._outbox.enqueue(atomic, outbox_entry)
            return persisted_claim

    @staticmethod
    def _validate_claim_relationships(
        claim: ReceiptClaim,
        binding: AgentAuthorityBinding,
        effect: ProtectedEffectOperation,
    ) -> None:
        binding_values = (
            binding.owner_id,
            binding.binding_id,
            binding.tenant_id,
            binding.envelope_id,
            binding.warden_id,
            binding.lease_id,
            binding.subject_id,
            binding.lineage_id,
            binding.policy_digest,
            binding.machine_digest,
            binding.config_epoch,
        )
        claim_binding_values = (
            claim.owner_id,
            claim.binding_id,
            claim.tenant_id,
            claim.envelope_id,
            claim.warden_id,
            claim.lease_id,
            claim.subject_id,
            claim.lineage_id,
            claim.policy_digest,
            claim.machine_digest,
            claim.config_epoch,
        )
        if binding_values != claim_binding_values:
            raise ReceiptClaimConflictError("receipt does not match the owner binding")
        if binding.state is not AuthorityBindingState.ACTIVE:
            raise ReceiptClaimConflictError("receipt binding is not active")
        if effect.agent_id != binding.agent_id:
            raise ReceiptClaimConflictError("receipt effect agent does not match the binding")
        if effect.lets_capability not in binding.capabilities:
            raise ReceiptClaimConflictError("receipt effect capability is not bound")
        if effect.expected_sequence != binding.lease_sequence:
            raise ReceiptClaimConflictError("receipt effect sequence is stale")
        if claim.resulting_sequence != effect.expected_sequence + 1:
            raise ReceiptClaimConflictError("receipt sequence does not advance exactly once")
        if claim.expires_at_ns > binding.lease_expires_at_ns:
            raise ReceiptClaimConflictError("receipt expiry exceeds the bound lease")
        if (
            effect.owner_id,
            effect.operation_id,
            effect.binding_id,
            effect.executor_audience,
            effect.lets_transition,
            effect.nonce,
        ) != (
            claim.owner_id,
            claim.operation_id,
            claim.binding_id,
            claim.audience,
            claim.transition,
            claim.nonce,
        ):
            raise ReceiptClaimConflictError("receipt does not match the protected effect")
        if claim.evidence_digest is not None and (
            claim.evidence_digest.removeprefix("sha256:") != effect.effect_digest
        ):
            raise ReceiptClaimConflictError("receipt evidence does not match the effect")

    @staticmethod
    def _validate_new_claim_transition(
        current: ProtectedEffectOperation,
        replacement: ProtectedEffectOperation,
        claim: ReceiptClaim,
    ) -> None:
        if not _same_effect_intent(current, replacement):
            raise ReceiptClaimConflictError("claimed effect changes immutable intent")
        if (
            current.status is not ProtectedEffectStatus.RECEIPT_RECEIVED
            or replacement.status is not ProtectedEffectStatus.RECEIPT_CLAIMED
            or replacement.version != current.version + 1
            or replacement.receipt_id != claim.receipt_id
            or replacement.receipt_digest != claim.canonical_digest
        ):
            raise ReceiptClaimConflictError("claimed effect does not advance the receipt fence")

    @staticmethod
    def _require_existing_claim_effect(
        effect: ProtectedEffectOperation,
        claim: ReceiptClaim,
    ) -> None:
        if (
            effect.status
            not in {
                ProtectedEffectStatus.RECEIPT_CLAIMED,
                ProtectedEffectStatus.EXECUTING,
                ProtectedEffectStatus.SUCCEEDED,
                ProtectedEffectStatus.EFFECT_FAILED,
                ProtectedEffectStatus.OUTCOME_UNCERTAIN,
            }
            or effect.receipt_id != claim.receipt_id
            or effect.receipt_digest != claim.canonical_digest
        ):
            raise ReceiptClaimConflictError("existing claim lacks matching effect evidence")

    @staticmethod
    def _advance_watermark(
        transaction: Transaction,
        watermark: ReceiptSequenceWatermark,
        claim: ReceiptClaim,
    ) -> None:
        if (
            watermark.key != claim.sequence_watermark_key
            or watermark.last_sequence != claim.resulting_sequence
            or watermark.expires_at_ns != claim.expires_at_ns
        ):
            raise RepositoryValidationError("watermark does not represent the receipt claim")
        row = transaction.fetch_one(
            _ADVANCE_WATERMARK,
            (
                watermark.warden_id,
                watermark.lease_id,
                watermark.audience,
                watermark.last_sequence,
                watermark.updated_at,
                watermark.expires_at_ns,
                watermark.version,
            ),
        )
        if row is None:
            raise ReceiptWatermarkConflictError("receipt sequence did not strictly advance")
        if _watermark_from_row(row) != watermark:
            raise RepositoryDataError("watermark advancement returned different data")


__all__ = (
    "AuthorityCompareAndSetConflictError",
    "AuthorityIdempotencyConflictError",
    "AuthorityRepository",
    "ReceiptClaimConflictError",
    "ReceiptWatermarkConflictError",
)
