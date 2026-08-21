"""Plane-backed checkpoints for LETS-governed physical effects.

The public LETS verifier owns signature, freshness, replay, clock, and
rollback-authority checks. AstralPlane owns neutral owner-scoped records. This
module joins those two public contracts without moving product policy into the
data plane or storing tool arguments, credentials, receipts, or result bodies.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from astralplane.authority import (
    AgentAuthorityBinding,
    AstralToolScope,
    AuthorityBindingState,
    AuthorityRepository,
    ExternalAuthorityAnchorMetadata,
    ProtectedEffectOperation,
    ProtectedEffectStatus,
    ReceiptClaim,
    ReceiptSequenceWatermark,
)
from astralplane.contracts import OutboxEntry
from lets.canonical import canonical_digest
from lets.models import Receipt

from orchestrator.protected_dispatch import ProtectedDispatchContext


_ERROR_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRE_EXECUTION = frozenset(
    {
        ProtectedEffectStatus.CREATED,
        ProtectedEffectStatus.ASTRAL_AUTHORIZED,
        ProtectedEffectStatus.LETS_PENDING,
        ProtectedEffectStatus.RECEIPT_RECEIVED,
        ProtectedEffectStatus.RECEIPT_CLAIMED,
    }
)


class PlaneEffectRuntime(Protocol):
    """The explicit transaction seam used by the neutral Plane repository."""

    def transaction(self, **options: object): ...


class ProtectedPermit(Protocol):
    """Structural permit view, avoiding a component-private gateway dependency."""

    binding_id: str
    owner_id: str
    runtime_generation: int
    context: object
    expected_sequence: int
    nonce: str
    wire_arguments_sha256: str
    receipt: Receipt


class ExecutorReplayStatusView(Protocol):
    """Public LETS status fields required after a local replay claim."""

    rollback_protected: bool
    authority_healthy: bool
    identity: object
    claim_sequence: int
    clock_floor_ns: int | None
    authority_checkpoint: object


class LetsEffectError(RuntimeError):
    """Stable content-free persistence/gateway refusal."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _outcome_digest(operation_id: str, outcome: str) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "type": "astral.lets-effect-outcome/v1",
                "operation_id": operation_id,
                "outcome": outcome,
            }
        )
    ).hexdigest()


def _error_code(value: str) -> str:
    if not isinstance(value, str) or _ERROR_CODE.fullmatch(value) is None:
        return "protected_effect_failure"
    return value


def _same_intent(
    operation: ProtectedEffectOperation,
    binding: AgentAuthorityBinding,
    context: ProtectedDispatchContext,
) -> bool:
    return (
        operation.owner_id,
        operation.agent_id,
        operation.binding_id,
        operation.tool_id,
        operation.astral_scope.value,
        operation.lets_capability,
        operation.lets_transition,
        operation.executor_audience,
        operation.nonce,
        operation.effect_digest,
        operation.expected_sequence,
        operation.audit_correlation_id,
    ) == (
        binding.owner_id,
        context.agent_id,
        binding.binding_id,
        context.tool_id,
        context.scope,
        context.capability,
        context.transition,
        context.executor_audience,
        context.nonce,
        canonical_digest(dict(context.lets_evidence())).removeprefix("sha256:"),
        context.expected_sequence,
        context.audit_correlation_id,
    )


class PlaneProtectedEffectCoordinator:
    """Persist exact effect intent, claim, sequence, and redacted outcome fences."""

    def __init__(
        self,
        *,
        plane: PlaneEffectRuntime,
        repository: AuthorityRepository,
    ) -> None:
        self._plane = plane
        self._repository = repository

    def prepare_authorization(
        self,
        *,
        binding: AgentAuthorityBinding,
        context: ProtectedDispatchContext,
    ) -> ProtectedEffectOperation:
        """Commit an exact Astral-authorized intent before contacting LETS."""

        if not isinstance(binding, AgentAuthorityBinding):
            raise LetsEffectError("invalid_authority_binding")
        if not isinstance(context, ProtectedDispatchContext):
            raise LetsEffectError("invalid_protected_context")
        if (
            binding.state is not AuthorityBindingState.ACTIVE
            or binding.owner_id == ""
            or binding.agent_id != context.agent_id
            or binding.runtime_id != context.runtime_id
            or binding.lease_sequence != context.expected_sequence
            or context.capability not in binding.capabilities
        ):
            raise LetsEffectError("binding_mismatch")
        now = _now()
        try:
            with self._plane.transaction() as transaction:
                operation = self._repository.get_protected_effect(
                    transaction,
                    owner_id=binding.owner_id,
                    operation_id=context.operation_id,
                )
                if operation is not None:
                    if not _same_intent(operation, binding, context):
                        raise LetsEffectError("effect_request_conflict")
                    if operation.status not in {
                        ProtectedEffectStatus.LETS_PENDING,
                        ProtectedEffectStatus.RECEIPT_RECEIVED,
                    }:
                        raise LetsEffectError("effect_request_not_retryable")
                    return operation
                operation = ProtectedEffectOperation(
                    operation_id=context.operation_id,
                    owner_id=binding.owner_id,
                    agent_id=context.agent_id,
                    binding_id=binding.binding_id,
                    tool_id=context.tool_id,
                    astral_scope=AstralToolScope(context.scope),
                    lets_capability=context.capability,
                    lets_transition=context.transition,
                    executor_audience=context.executor_audience,
                    nonce=context.nonce,
                    effect_digest=canonical_digest(
                        dict(context.lets_evidence())
                    ).removeprefix("sha256:"),
                    expected_sequence=context.expected_sequence,
                    audit_correlation_id=context.audit_correlation_id,
                    status=ProtectedEffectStatus.CREATED,
                    receipt_id=None,
                    receipt_digest=None,
                    effect_result_digest=None,
                    error_code=None,
                    created_at=now,
                    updated_at=now,
                    version=0,
                )
                operation = self._repository.create_protected_effect(
                    transaction, operation
                )
                operation = self._transition(
                    transaction,
                    operation,
                    ProtectedEffectStatus.ASTRAL_AUTHORIZED,
                    now=now,
                )
                return self._transition(
                    transaction,
                    operation,
                    ProtectedEffectStatus.LETS_PENDING,
                    now=now,
                )
        except LetsEffectError:
            raise
        except Exception:
            raise LetsEffectError("effect_persistence_unavailable", retryable=True) from None

    def record_receipt(
        self,
        *,
        binding: AgentAuthorityBinding,
        context: ProtectedDispatchContext,
        envelope: ProtectedPermit,
    ) -> ProtectedEffectOperation:
        """Persist validated receipt metadata without storing signed receipt bytes."""

        receipt = envelope.receipt
        if (
            envelope.owner_id != binding.owner_id
            or envelope.binding_id != binding.binding_id
            or envelope.runtime_generation != binding.runtime_generation
            or envelope.expected_sequence != context.expected_sequence
            or receipt.request_id != context.operation_id
            or receipt.subject_id != binding.subject_id
            or receipt.lease_id != binding.lease_id
        ):
            raise LetsEffectError("receipt_binding_mismatch")
        digest = canonical_digest(receipt.to_dict()).removeprefix("sha256:")
        try:
            with self._plane.transaction() as transaction:
                operation = self._require_effect(
                    transaction,
                    owner_id=binding.owner_id,
                    operation_id=context.operation_id,
                )
                if not _same_intent(operation, binding, context):
                    raise LetsEffectError("effect_request_conflict")
                if operation.status is ProtectedEffectStatus.RECEIPT_RECEIVED:
                    if (
                        operation.receipt_id == receipt.receipt_id
                        and operation.receipt_digest == digest
                    ):
                        return operation
                    raise LetsEffectError("receipt_request_conflict")
                if operation.status is not ProtectedEffectStatus.LETS_PENDING:
                    raise LetsEffectError("receipt_checkpoint_not_pending")
                replacement = replace(
                    operation,
                    status=ProtectedEffectStatus.RECEIPT_RECEIVED,
                    receipt_id=receipt.receipt_id,
                    receipt_digest=digest,
                    updated_at=_now(),
                    version=operation.version + 1,
                )
                return self._repository.transition_protected_effect(
                    transaction,
                    replacement,
                    expected_status=operation.status,
                    expected_version=operation.version,
                )
        except LetsEffectError:
            raise
        except Exception:
            raise LetsEffectError("effect_persistence_unavailable", retryable=True) from None

    def claim_for_execution(
        self,
        *,
        envelope: ProtectedPermit,
        replay_status: ExecutorReplayStatusView,
    ) -> ProtectedEffectOperation:
        """Commit Plane claim + sequence + executing fences after LETS claims locally.

        This method must be called while the binding-scoped executor lock is
        still held. If it fails, the caller omits the physical effect; it must
        never replay a locally consumed receipt merely to repair Plane evidence.
        """

        if not isinstance(getattr(envelope, "receipt", None), Receipt):
            raise LetsEffectError("invalid_protected_permit")
        try:
            checkpoint = replay_status.authority_checkpoint
            identity = replay_status.identity
        except AttributeError:
            raise LetsEffectError("executor_authority_unavailable", retryable=True)
        if (
            not replay_status.rollback_protected
            or not replay_status.authority_healthy
            or checkpoint is None
            or identity is None
        ):
            raise LetsEffectError("executor_authority_unavailable", retryable=True)
        try:
            checkpoint_valid = (
                checkpoint.identity == identity
                and checkpoint.claim_sequence == replay_status.claim_sequence
                and checkpoint.clock_floor_ns == replay_status.clock_floor_ns
                and checkpoint.claim_sequence >= 1
                and checkpoint.clock_floor_ns is not None
                and isinstance(identity.executor_policy_sha256, bytes)
                and len(identity.executor_policy_sha256) == 32
                and isinstance(identity.trust_registry_sha256, bytes)
                and len(identity.trust_registry_sha256) == 32
                and isinstance(checkpoint.database_instance_id, bytes)
                and len(checkpoint.database_instance_id) == 32
                and isinstance(checkpoint.claim_digest, bytes)
                and len(checkpoint.claim_digest) == 32
            )
        except AttributeError:
            checkpoint_valid = False
        if not checkpoint_valid:
            raise LetsEffectError("executor_authority_unavailable", retryable=True)
        receipt = envelope.receipt
        now = _now()
        receipt_digest = canonical_digest(receipt.to_dict()).removeprefix("sha256:")
        anchor = ExternalAuthorityAnchorMetadata(
            anchor_format="LETS-EXECUTOR-AUTHORITY-ANCHOR/1",
            audience=identity.audience,
            tenant_id=identity.tenant_id,
            envelope_id=identity.envelope_id,
            config_epoch=identity.config_epoch,
            executor_policy_sha256=identity.executor_policy_sha256.hex(),
            trust_registry_sha256=identity.trust_registry_sha256.hex(),
            schema_version=checkpoint.schema_version,
            database_instance_id=checkpoint.database_instance_id.hex(),
            claim_sequence=checkpoint.claim_sequence,
            claim_digest=checkpoint.claim_digest.hex(),
            clock_floor_ns=checkpoint.clock_floor_ns,
            confirmed_at=now,
        )
        claim = ReceiptClaim(
            receipt_id=receipt.receipt_id,
            operation_id=receipt.request_id,
            owner_id=envelope.owner_id,
            binding_id=envelope.binding_id,
            tenant_id=receipt.tenant_id,
            envelope_id=receipt.envelope_id,
            warden_id=receipt.warden_id,
            lease_id=receipt.lease_id,
            subject_id=receipt.subject_id,
            lineage_id=receipt.lineage_id,
            policy_digest=receipt.policy_digest,
            machine_digest=receipt.machine_digest,
            config_epoch=receipt.config_epoch,
            audience=receipt.executor_audience,
            transition=receipt.transition,
            nonce=receipt.nonce,
            resulting_sequence=receipt.resulting_sequence,
            evidence_digest=receipt.evidence_digest,
            issued_at_ns=receipt.issued_at_ns,
            expires_at_ns=receipt.expires_at_ns,
            claimed_at=now,
            canonical_digest=receipt_digest,
            authority_anchor=anchor,
        )
        watermark = ReceiptSequenceWatermark(
            warden_id=receipt.warden_id,
            lease_id=receipt.lease_id,
            audience=receipt.executor_audience,
            last_sequence=receipt.resulting_sequence,
            updated_at=now,
            expires_at_ns=receipt.expires_at_ns,
            version=envelope.expected_sequence,
        )
        payload = _canonical_bytes(
            {
                "type": "astralplane.authority-receipt-claimed/v1",
                "owner_id": envelope.owner_id,
                "operation_id": receipt.request_id,
                "binding_id": envelope.binding_id,
                "receipt_id": receipt.receipt_id,
                "receipt_sha256": receipt_digest,
                "resulting_sequence": receipt.resulting_sequence,
            }
        )
        claim_entry_id = (
            "authority-claim-"
            + hashlib.sha256(receipt.receipt_id.encode("utf-8")).hexdigest()
        )
        outbox = OutboxEntry(
            entry_id=claim_entry_id,
            topic="authority.receipt_claimed",
            canonical_payload=payload,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            idempotency_key=claim_entry_id,
            available_at=now,
        )
        try:
            with self._plane.transaction() as transaction:
                binding = self._repository.get_binding(
                    transaction,
                    owner_id=envelope.owner_id,
                    binding_id=envelope.binding_id,
                )
                operation = self._require_effect(
                    transaction,
                    owner_id=envelope.owner_id,
                    operation_id=receipt.request_id,
                )
                if binding is None or (
                    binding.state is not AuthorityBindingState.ACTIVE
                    or binding.lease_sequence != envelope.expected_sequence
                    or binding.runtime_generation != envelope.runtime_generation
                ):
                    raise LetsEffectError("binding_claim_fence_mismatch")
                if (
                    operation.status is not ProtectedEffectStatus.RECEIPT_RECEIVED
                    or operation.receipt_id != receipt.receipt_id
                    or operation.receipt_digest != receipt_digest
                ):
                    raise LetsEffectError("receipt_checkpoint_mismatch")
                claimed = replace(
                    operation,
                    status=ProtectedEffectStatus.RECEIPT_CLAIMED,
                    updated_at=now,
                    version=operation.version + 1,
                )
                self._repository.claim_receipt(
                    transaction,
                    claim=claim,
                    watermark=watermark,
                    claimed_effect=claimed,
                    outbox_entry=outbox,
                )
                advanced = replace(
                    binding,
                    lease_sequence=receipt.resulting_sequence,
                    updated_at=now,
                    version=binding.version + 1,
                )
                self._repository.transition_binding(
                    transaction,
                    advanced,
                    expected_state=binding.state,
                    expected_version=binding.version,
                )
                executing = replace(
                    claimed,
                    status=ProtectedEffectStatus.EXECUTING,
                    updated_at=now,
                    version=claimed.version + 1,
                )
                return self._repository.transition_protected_effect(
                    transaction,
                    executing,
                    expected_status=claimed.status,
                    expected_version=claimed.version,
                )
        except LetsEffectError:
            raise
        except Exception:
            raise LetsEffectError("plane_claim_unavailable", retryable=True) from None

    def fail_before_execution(
        self,
        *,
        owner_id: str,
        operation_id: str,
        error_code: str,
        denied: bool = False,
    ) -> ProtectedEffectOperation:
        """Terminally refuse a known pre-execution intent without false success."""

        try:
            with self._plane.transaction() as transaction:
                operation = self._require_effect(
                    transaction,
                    owner_id=owner_id,
                    operation_id=operation_id,
                )
                if operation.status.terminal:
                    return operation
                if operation.status not in _PRE_EXECUTION:
                    raise LetsEffectError("effect_already_executing")
                replacement = replace(
                    operation,
                    status=(
                        ProtectedEffectStatus.DENIED
                        if denied
                        else ProtectedEffectStatus.FAILED_CLOSED
                    ),
                    error_code=_error_code(error_code),
                    updated_at=_now(),
                    version=operation.version + 1,
                )
                return self._repository.transition_protected_effect(
                    transaction,
                    replacement,
                    expected_status=operation.status,
                    expected_version=operation.version,
                )
        except LetsEffectError:
            raise
        except Exception:
            raise LetsEffectError("effect_persistence_unavailable", retryable=True) from None

    def record_outcome(
        self,
        *,
        owner_id: str,
        operation_id: str,
        outcome: str,
        error_code: str | None = None,
    ) -> ProtectedEffectOperation:
        """Persist a redacted known/uncertain outcome after an execution claim."""

        targets = {
            "succeeded": ProtectedEffectStatus.SUCCEEDED,
            "effect_failed": ProtectedEffectStatus.EFFECT_FAILED,
            "outcome_uncertain": ProtectedEffectStatus.OUTCOME_UNCERTAIN,
        }
        try:
            target = targets[outcome]
        except (KeyError, TypeError):
            raise LetsEffectError("invalid_effect_outcome") from None
        if target is ProtectedEffectStatus.SUCCEEDED and error_code is not None:
            raise LetsEffectError("invalid_effect_outcome")
        if target is not ProtectedEffectStatus.SUCCEEDED and error_code is None:
            raise LetsEffectError("invalid_effect_outcome")
        try:
            with self._plane.transaction() as transaction:
                operation = self._require_effect(
                    transaction,
                    owner_id=owner_id,
                    operation_id=operation_id,
                )
                if operation.status is target:
                    expected_error = (
                        None
                        if target is ProtectedEffectStatus.SUCCEEDED
                        else _error_code(error_code or "protected_effect_failure")
                    )
                    if operation.error_code == expected_error:
                        return operation
                    raise LetsEffectError("effect_outcome_conflict")
                if operation.status not in {
                    ProtectedEffectStatus.EXECUTING,
                    ProtectedEffectStatus.OUTCOME_UNCERTAIN,
                }:
                    raise LetsEffectError("effect_not_executing")
                if (
                    operation.status is ProtectedEffectStatus.OUTCOME_UNCERTAIN
                    and target is ProtectedEffectStatus.OUTCOME_UNCERTAIN
                ):
                    raise LetsEffectError("effect_outcome_conflict")
                replacement = replace(
                    operation,
                    status=target,
                    effect_result_digest=(
                        None
                        if target is ProtectedEffectStatus.OUTCOME_UNCERTAIN
                        else _outcome_digest(operation.operation_id, outcome)
                    ),
                    error_code=(
                        None
                        if target is ProtectedEffectStatus.SUCCEEDED
                        else _error_code(error_code or "protected_effect_failure")
                    ),
                    updated_at=_now(),
                    version=operation.version + 1,
                )
                return self._repository.transition_protected_effect(
                    transaction,
                    replacement,
                    expected_status=operation.status,
                    expected_version=operation.version,
                )
        except LetsEffectError:
            raise
        except Exception:
            raise LetsEffectError("effect_persistence_unavailable", retryable=True) from None

    def _transition(
        self,
        transaction: object,
        operation: ProtectedEffectOperation,
        status: ProtectedEffectStatus,
        *,
        now: datetime,
    ) -> ProtectedEffectOperation:
        replacement = replace(
            operation,
            status=status,
            updated_at=now,
            version=operation.version + 1,
        )
        return self._repository.transition_protected_effect(
            transaction,  # type: ignore[arg-type]
            replacement,
            expected_status=operation.status,
            expected_version=operation.version,
        )

    def _require_effect(
        self,
        transaction: object,
        *,
        owner_id: str,
        operation_id: str,
    ) -> ProtectedEffectOperation:
        operation = self._repository.get_protected_effect(
            transaction,  # type: ignore[arg-type]
            owner_id=owner_id,
            operation_id=operation_id,
        )
        if operation is None:
            raise LetsEffectError("protected_effect_unavailable")
        return operation


__all__ = (
    "LetsEffectError",
    "PlaneEffectRuntime",
    "PlaneProtectedEffectCoordinator",
)
