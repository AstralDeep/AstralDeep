"""Bounded startup/background recovery for durable LETS lifecycle intents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from astralplane.authority import (
    AgentAuthorityBinding,
    AuthorityLifecycleKind,
    AuthorityLifecycleOperation,
    AuthorityLifecycleStatus,
    AuthorityRepository,
    ProtectedEffectOperation,
    ProtectedEffectStatus,
)

from orchestrator.lets_lifecycle import (
    LifecycleRecoveryContext,
    LetsLifecycleError,
    LetsLifecycleService,
    PlaneAuthorityRuntime,
)


_RECOVERY_DELAY = timedelta(seconds=15)
_CONTEXT_KINDS = frozenset(
    {AuthorityLifecycleKind.SPAWN, AuthorityLifecycleKind.REVOKE}
)
_RECOVERY_ERROR_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRE_EXECUTION_EFFECT_STATES = frozenset(
    {
        ProtectedEffectStatus.CREATED,
        ProtectedEffectStatus.ASTRAL_AUTHORIZED,
        ProtectedEffectStatus.LETS_PENDING,
        ProtectedEffectStatus.RECEIPT_RECEIVED,
        ProtectedEffectStatus.RECEIPT_CLAIMED,
    }
)


class LifecycleRecoveryResolver(Protocol):
    """Resolve host-owned context from Deep's durable agent/runtime graph."""

    def __call__(
        self,
        operation: AuthorityLifecycleOperation,
        binding: AgentAuthorityBinding,
    ) -> LifecycleRecoveryContext | None: ...


@dataclass(frozen=True, slots=True)
class EffectRecoveryResolution:
    """Known domain outcome supplied by a Deep-owned idempotency resolver."""

    outcome: Literal["succeeded", "effect_failed"]
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"succeeded", "effect_failed"}:
            raise LetsLifecycleError("invalid_effect_recovery_resolution")
        if (self.outcome == "succeeded") != (self.error_code is None):
            raise LetsLifecycleError("invalid_effect_recovery_resolution")
        if self.error_code is not None and _RECOVERY_ERROR_CODE.fullmatch(
            self.error_code
        ) is None:
            raise LetsLifecycleError("invalid_effect_recovery_resolution")


class EffectRecoveryResolver(Protocol):
    """Resolve an executing/uncertain effect from domain idempotency evidence."""

    def __call__(
        self,
        operation: ProtectedEffectOperation,
    ) -> EffectRecoveryResolution | None: ...


@dataclass(frozen=True, slots=True)
class LifecycleRecoveryBatch:
    """Redacted result of one bounded owner-scoped recovery pass."""

    owner_id: str
    selected: int
    claimed: int
    converged: int
    deferred: int
    error_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EffectRecoveryBatch:
    """Redacted result of one owner-scoped stale-effect recovery pass."""

    owner_id: str
    selected: int
    transitioned: int
    deferred: int
    failed_closed: int
    uncertain: int
    resolved: int
    error_codes: tuple[str, ...]


class LetsLifecycleReconciler:
    """Claim due operations under row locks, then replay exact safe requests."""

    def __init__(
        self,
        *,
        plane: PlaneAuthorityRuntime,
        repository: AuthorityRepository,
        lifecycle: LetsLifecycleService,
        resolver: LifecycleRecoveryResolver | None = None,
    ) -> None:
        self._plane = plane
        self._repository = repository
        self._lifecycle = lifecycle
        self._resolver = resolver

    async def recover_owner(
        self,
        owner_id: str,
        *,
        due_at: datetime | None = None,
        limit: int = 50,
    ) -> LifecycleRecoveryBatch:
        """Recover at most ``limit`` due operations for exactly one owner."""

        selected_at = datetime.now(UTC) if due_at is None else due_at
        if selected_at.tzinfo is None or selected_at.utcoffset() != timedelta(0):
            raise LetsLifecycleError("invalid_recovery_due_at")
        claimed: list[
            tuple[
                AgentAuthorityBinding,
                AuthorityLifecycleOperation,
                LifecycleRecoveryContext,
            ]
        ] = []
        deferred = 0
        errors: list[str] = []
        selected_count = 0

        # list_recoverable_lifecycle_operations uses FOR UPDATE SKIP LOCKED.
        # Every selected row is transitioned before this transaction releases
        # its lock, so multiple workers cannot perform the same recovery call.
        with self._plane.transaction() as transaction:
            operations = self._repository.list_recoverable_lifecycle_operations(
                transaction,
                owner_id=owner_id,
                due_at=selected_at,
                limit=limit,
            )
            selected_count = len(operations)
            for operation in operations:
                binding = self._repository.get_binding(
                    transaction,
                    owner_id=owner_id,
                    binding_id=operation.binding_id,
                )
                if binding is None:
                    self._defer(
                        transaction,
                        operation,
                        selected_at,
                        "recovery_binding_unavailable",
                    )
                    deferred += 1
                    errors.append("recovery_binding_unavailable")
                    continue
                try:
                    context = self._resolve(operation, binding)
                except Exception:
                    context = None
                if operation.kind in _CONTEXT_KINDS and context is None:
                    code = "recovery_context_unavailable"
                    self._defer(transaction, operation, selected_at, code)
                    deferred += 1
                    errors.append(code)
                    continue
                exact_context = LifecycleRecoveryContext() if context is None else context
                started = replace(
                    operation,
                    status=AuthorityLifecycleStatus.IN_FLIGHT,
                    result_digest=None,
                    error_code=None,
                    attempt_count=operation.attempt_count + 1,
                    next_attempt_at=selected_at + _RECOVERY_DELAY,
                    last_attempt_at=selected_at,
                    updated_at=selected_at,
                    version=operation.version + 1,
                )
                started = self._repository.transition_lifecycle_operation(
                    transaction,
                    started,
                    expected_status=operation.status,
                    expected_version=operation.version,
                )
                claimed.append((binding, started, exact_context))

        converged = 0
        for binding, operation, context in claimed:
            try:
                result = await self._lifecycle.resume_claimed(
                    binding=binding,
                    operation=operation,
                    context=context,
                )
            except LetsLifecycleError as exc:
                errors.append(exc.code)
                continue
            if result.error_code is None:
                converged += 1
            else:
                errors.append(result.error_code)
        return LifecycleRecoveryBatch(
            owner_id=owner_id,
            selected=selected_count,
            claimed=len(claimed),
            converged=converged,
            deferred=deferred,
            error_codes=tuple(errors),
        )

    async def run_forever(
        self,
        owner_ids: Callable[[], Iterable[str]],
        *,
        stop: asyncio.Event,
        interval_seconds: float = 15.0,
        limit_per_owner: int = 50,
    ) -> None:
        """Run serialized bounded passes until the host signals shutdown."""

        if interval_seconds < 1.0 or interval_seconds > 300.0:
            raise LetsLifecycleError("invalid_recovery_interval")
        while not stop.is_set():
            for owner_id in tuple(owner_ids()):
                if stop.is_set():
                    break
                await self.recover_owner(owner_id, limit=limit_per_owner)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass

    def _resolve(
        self,
        operation: AuthorityLifecycleOperation,
        binding: AgentAuthorityBinding,
    ) -> LifecycleRecoveryContext | None:
        if self._resolver is None:
            return None
        value = self._resolver(operation, binding)
        if value is not None and not isinstance(value, LifecycleRecoveryContext):
            raise LetsLifecycleError("invalid_recovery_context")
        return value

    def _defer(
        self,
        transaction: object,
        operation: AuthorityLifecycleOperation,
        now: datetime,
        code: str,
    ) -> None:
        replacement = replace(
            operation,
            status=AuthorityLifecycleStatus.UNCERTAIN,
            result_digest=None,
            error_code=code,
            attempt_count=operation.attempt_count + 1,
            next_attempt_at=now + _RECOVERY_DELAY,
            last_attempt_at=now,
            updated_at=now,
            version=operation.version + 1,
        )
        self._repository.transition_lifecycle_operation(
            transaction,  # type: ignore[arg-type]
            replacement,
            expected_status=operation.status,
            expected_version=operation.version,
        )


class LetsEffectReconciler:
    """Fail closed or resolve stale authorization/effect checkpoints."""

    def __init__(
        self,
        *,
        plane: PlaneAuthorityRuntime,
        repository: AuthorityRepository,
        resolver: EffectRecoveryResolver | None = None,
    ) -> None:
        self._plane = plane
        self._repository = repository
        self._resolver = resolver

    def recover_owner(
        self,
        owner_id: str,
        *,
        updated_before: datetime | None = None,
        stale_after: timedelta = timedelta(minutes=1),
        limit: int = 50,
    ) -> EffectRecoveryBatch:
        """Recover one bounded owner partition under Plane row locks.

        Pre-execution rows are safe to fail closed because no actuator began.
        An abandoned ``executing`` row becomes ``outcome_uncertain`` unless a
        Deep-owned domain resolver proves a known result. Existing uncertainty
        is never rewritten as failure merely because no resolver is available.
        """

        if not isinstance(stale_after, timedelta) or stale_after <= timedelta(0):
            raise LetsLifecycleError("invalid_effect_recovery_staleness")
        selected_at = _now_utc()
        cutoff = selected_at - stale_after if updated_before is None else updated_before
        if cutoff.tzinfo is None or cutoff.utcoffset() != timedelta(0):
            raise LetsLifecycleError("invalid_effect_recovery_cutoff")

        transitioned = 0
        deferred = 0
        failed_closed = 0
        uncertain = 0
        resolved = 0
        errors: list[str] = []
        with self._plane.transaction() as transaction:
            operations = self._repository.list_recoverable_protected_effects(
                transaction,
                owner_id=owner_id,
                updated_before=cutoff,
                limit=limit,
            )
            for operation in operations:
                replacement: ProtectedEffectOperation | None
                if operation.status in _PRE_EXECUTION_EFFECT_STATES:
                    code = f"recovery_abandoned_{operation.status.value}"
                    replacement = replace(
                        operation,
                        status=ProtectedEffectStatus.FAILED_CLOSED,
                        effect_result_digest=None,
                        error_code=code,
                        updated_at=selected_at,
                        version=operation.version + 1,
                    )
                    failed_closed += 1
                    errors.append(code)
                elif operation.status is ProtectedEffectStatus.EXECUTING:
                    resolution = self._resolve(operation)
                    if resolution is None:
                        code = "effect_result_unavailable"
                        replacement = replace(
                            operation,
                            status=ProtectedEffectStatus.OUTCOME_UNCERTAIN,
                            effect_result_digest=None,
                            error_code=code,
                            updated_at=selected_at,
                            version=operation.version + 1,
                        )
                        uncertain += 1
                        errors.append(code)
                    else:
                        replacement = self._resolved(
                            operation,
                            resolution,
                            now=selected_at,
                        )
                        resolved += 1
                elif operation.status is ProtectedEffectStatus.OUTCOME_UNCERTAIN:
                    resolution = self._resolve(operation)
                    if resolution is None:
                        deferred += 1
                        errors.append("effect_resolution_deferred")
                        continue
                    replacement = self._resolved(
                        operation,
                        resolution,
                        now=selected_at,
                    )
                    resolved += 1
                else:  # pragma: no cover - Plane query validates exhaustiveness.
                    deferred += 1
                    errors.append("effect_recovery_state_unsupported")
                    continue
                self._repository.transition_protected_effect(
                    transaction,
                    replacement,
                    expected_status=operation.status,
                    expected_version=operation.version,
                )
                transitioned += 1
        return EffectRecoveryBatch(
            owner_id=owner_id,
            selected=len(operations),
            transitioned=transitioned,
            deferred=deferred,
            failed_closed=failed_closed,
            uncertain=uncertain,
            resolved=resolved,
            error_codes=tuple(errors),
        )

    async def run_forever(
        self,
        owner_ids: Callable[[], Iterable[str]],
        *,
        stop: asyncio.Event,
        interval_seconds: float = 15.0,
        stale_after: timedelta = timedelta(minutes=1),
        limit_per_owner: int = 50,
    ) -> None:
        """Run bounded synchronous Plane recovery passes off the event loop."""

        if interval_seconds < 1.0 or interval_seconds > 300.0:
            raise LetsLifecycleError("invalid_recovery_interval")
        while not stop.is_set():
            for owner_id in tuple(owner_ids()):
                if stop.is_set():
                    break
                await asyncio.to_thread(
                    self.recover_owner,
                    owner_id,
                    stale_after=stale_after,
                    limit=limit_per_owner,
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass

    def _resolve(
        self,
        operation: ProtectedEffectOperation,
    ) -> EffectRecoveryResolution | None:
        if self._resolver is None:
            return None
        try:
            resolution = self._resolver(operation)
        except Exception:
            return None
        if resolution is not None and not isinstance(
            resolution, EffectRecoveryResolution
        ):
            return None
        return resolution

    @staticmethod
    def _resolved(
        operation: ProtectedEffectOperation,
        resolution: EffectRecoveryResolution,
        *,
        now: datetime,
    ) -> ProtectedEffectOperation:
        status = (
            ProtectedEffectStatus.SUCCEEDED
            if resolution.outcome == "succeeded"
            else ProtectedEffectStatus.EFFECT_FAILED
        )
        document = {
            "type": "astral.lets-effect-recovery/v1",
            "operation_id": operation.operation_id,
            "outcome": resolution.outcome,
        }
        encoded = json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        return replace(
            operation,
            status=status,
            effect_result_digest=hashlib.sha256(encoded).hexdigest(),
            error_code=resolution.error_code,
            updated_at=now,
            version=operation.version + 1,
        )


def _now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = (
    "EffectRecoveryBatch",
    "EffectRecoveryResolution",
    "EffectRecoveryResolver",
    "LifecycleRecoveryBatch",
    "LifecycleRecoveryResolver",
    "LetsEffectReconciler",
    "LetsLifecycleReconciler",
)
