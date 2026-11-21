"""Runs required post-migration reconciliation hooks to completion under a durable
cross-process coordinator, keyed by name and version. Coordinated via
reconciliation_store.py's locking and invoked from database/bootstrap.py during
startup.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from astralplane.contracts import (
    ProductReconciler,
    ReconciliationCoordinator,
    ReconciliationHookIdentity,
    ReconciliationMarker,
    ReconciliationMarkerState,
    ReconciliationSession,
)
from astralplane.database.revision import validate_revision
from astralplane.errors import ReconciliationError

RECONCILIATION_ADVISORY_LOCK: Final = (1095980114, 60002)

_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    schema_revision: str
    plan_digest: str
    hooks: tuple[ReconciliationHookIdentity, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationHookReport:
    hook: ReconciliationHookIdentity
    attempt: int
    already_complete: bool
    result_digest: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    schema_revision: str
    plan_digest: str
    advisory_lock: tuple[int, int]
    hooks: tuple[ReconciliationHookReport, ...]
    durably_complete: bool


def _canonical_plan_digest(
    schema_revision: str, hooks: tuple[ReconciliationHookIdentity, ...]
) -> str:
    document = {
        "hooks": [{"name": hook.name, "version": hook.version} for hook in hooks],
        "schemaRevision": schema_revision,
    }
    canonical = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _result_digest(result: Mapping[str, object] | None) -> str:
    if result is None:
        result = {}
    if not isinstance(result, Mapping):
        raise ReconciliationError("reconciliation hook result must be a mapping")
    if any(not isinstance(key, str) or not key for key in result):
        raise ReconciliationError("reconciliation hook result keys must be non-empty strings")
    try:
        canonical = json.dumps(
            dict(result),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReconciliationError(
            "reconciliation hook result must be canonical JSON metadata"
        ) from exc
    return hashlib.sha256(canonical).hexdigest()


def _validate_marker(
    marker: ReconciliationMarker,
    *,
    plan: ReconciliationPlan,
    hook: ReconciliationHookIdentity,
    expected_state: ReconciliationMarkerState | None = None,
) -> ReconciliationMarker:
    if not isinstance(marker, ReconciliationMarker):
        raise ReconciliationError("reconciliation store returned an invalid marker")
    if (
        marker.schema_revision != plan.schema_revision
        or marker.plan_digest != plan.plan_digest
        or marker.hook != hook
    ):
        raise ReconciliationError(
            "reconciliation marker does not match the required plan",
            metadata={"hook": hook.name, "version": hook.version},
        )
    if marker.attempt < 1:
        raise ReconciliationError("reconciliation marker attempt must be positive")
    if expected_state is not None and marker.state is not expected_state:
        raise ReconciliationError(
            "reconciliation marker has an unexpected state",
            metadata={"hook": hook.name, "state": marker.state},
        )
    if marker.state is ReconciliationMarkerState.COMPLETED:
        if marker.result_digest is None or _SHA256.fullmatch(marker.result_digest) is None:
            raise ReconciliationError(
                "completed reconciliation marker lacks a canonical result digest"
            )
        if marker.error_type is not None:
            raise ReconciliationError("completed reconciliation marker must not carry a failure")
    elif marker.state is ReconciliationMarkerState.FAILED:
        if (
            not marker.error_type
            or _SAFE_IDENTITY.fullmatch(marker.error_type) is None
            or marker.result_digest is not None
        ):
            raise ReconciliationError("failed reconciliation marker is malformed")
    elif marker.state is ReconciliationMarkerState.STARTED:
        if marker.error_type is not None or marker.result_digest is not None:
            raise ReconciliationError("started reconciliation marker is malformed")
    else:
        raise ReconciliationError("reconciliation marker state is invalid")
    return marker


class ReconciliationRunner:
    def __init__(
        self,
        coordinator: ReconciliationCoordinator,
        hooks: Iterable[ProductReconciler],
    ) -> None:
        materialized_hooks = tuple(hooks)
        if not materialized_hooks:
            raise ReconciliationError("at least one required reconciliation hook is needed")
        identities: list[ReconciliationHookIdentity] = []
        names: set[str] = set()
        for hook in materialized_hooks:
            name = hook.name
            version = hook.version
            if (
                not isinstance(name, str)
                or _SAFE_IDENTITY.fullmatch(name) is None
                or not isinstance(version, str)
                or _SAFE_IDENTITY.fullmatch(version) is None
            ):
                raise ReconciliationError(
                    "reconciliation hook name and version must be bounded identifiers"
                )
            if name in names:
                raise ReconciliationError("reconciliation hook names must be unique")
            names.add(name)
            identities.append(ReconciliationHookIdentity(name=name, version=version))
        self._coordinator = coordinator
        self._hooks = materialized_hooks
        self._identities = tuple(identities)

    def plan(self, *, schema_revision: str) -> ReconciliationPlan:
        revision = validate_revision(schema_revision, field="reconciliation schema revision")
        return ReconciliationPlan(
            schema_revision=revision,
            plan_digest=_canonical_plan_digest(revision, self._identities),
            hooks=self._identities,
        )

    def plan_digest(self, *, schema_revision: str) -> str:
        return self.plan(schema_revision=schema_revision).plan_digest

    @staticmethod
    def _durable_failure(
        session: ReconciliationSession,
        *,
        plan: ReconciliationPlan,
        hook: ReconciliationHookIdentity,
        error_type: str,
    ) -> ReconciliationMarker:
        marker = session.mark_failed(hook, error_type=error_type)
        return _validate_marker(
            marker,
            plan=plan,
            hook=hook,
            expected_state=ReconciliationMarkerState.FAILED,
        )

    def run(
        self,
        *,
        schema_revision: str,
        context: Mapping[str, object],
    ) -> ReconciliationReport:
        plan = self.plan(schema_revision=schema_revision)
        immutable_context = MappingProxyType(dict(context))
        reports: list[ReconciliationHookReport] = []

        with self._coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=plan.schema_revision,
            plan_digest=plan.plan_digest,
        ) as session:
            for hook, identity in zip(self._hooks, plan.hooks, strict=True):
                existing = session.get_marker(identity)
                if existing is not None:
                    existing = _validate_marker(existing, plan=plan, hook=identity)
                    if existing.state is ReconciliationMarkerState.COMPLETED:
                        assert existing.result_digest is not None
                        reports.append(
                            ReconciliationHookReport(
                                hook=identity,
                                attempt=existing.attempt,
                                already_complete=True,
                                result_digest=existing.result_digest,
                            )
                        )
                        continue

                started = _validate_marker(
                    session.mark_started(identity),
                    plan=plan,
                    hook=identity,
                    expected_state=ReconciliationMarkerState.STARTED,
                )
                expected_attempt = 1 if existing is None else existing.attempt + 1
                completed = False
                ordinary_failure = False
                try:
                    if started.attempt != expected_attempt:
                        raise ReconciliationError("reconciliation marker attempt is not monotonic")
                    digest = _result_digest(hook.reconcile(immutable_context))
                    completed_marker = _validate_marker(
                        session.mark_completed(identity, result_digest=digest),
                        plan=plan,
                        hook=identity,
                        expected_state=ReconciliationMarkerState.COMPLETED,
                    )
                    if completed_marker.attempt != started.attempt:
                        raise ReconciliationError(
                            "reconciliation completion changed the attempt identity"
                        )
                    if completed_marker.result_digest != digest:
                        raise ReconciliationError(
                            "reconciliation completion changed the result digest"
                        )
                    completed = True
                except Exception as exc:
                    ordinary_failure = True
                    try:
                        failed = self._durable_failure(
                            session,
                            plan=plan,
                            hook=identity,
                            error_type=type(exc).__name__,
                        )
                        if failed.attempt != started.attempt:
                            raise ReconciliationError(
                                "reconciliation failure changed the attempt identity"
                            )
                    except Exception as marker_exc:
                        raise ReconciliationError(
                            "reconciliation failure marker could not be persisted",
                            metadata={"hook": identity.name, "version": identity.version},
                        ) from marker_exc
                    raise ReconciliationError(
                        "required reconciliation hook failed",
                        metadata={
                            "hook": identity.name,
                            "version": identity.version,
                            "attempt": failed.attempt,
                        },
                    ) from exc
                finally:
                    if not completed and not ordinary_failure:
                        with suppress(Exception):
                            self._durable_failure(
                                session,
                                plan=plan,
                                hook=identity,
                                error_type="Interrupted",
                            )

                assert completed_marker.result_digest is not None
                reports.append(
                    ReconciliationHookReport(
                        hook=identity,
                        attempt=started.attempt,
                        already_complete=False,
                        result_digest=completed_marker.result_digest,
                    )
                )

            for identity in plan.hooks:
                marker = session.get_marker(identity)
                try:
                    if marker is None:
                        raise ReconciliationError(
                            "required reconciliation completion marker is missing"
                        )
                    _validate_marker(
                        marker,
                        plan=plan,
                        hook=identity,
                        expected_state=ReconciliationMarkerState.COMPLETED,
                    )
                except Exception as exc:
                    try:
                        self._durable_failure(
                            session,
                            plan=plan,
                            hook=identity,
                            error_type="CompletionNotDurable",
                        )
                    except Exception as marker_exc:
                        raise ReconciliationError(
                            "reconciliation completion and failure markers are not durable",
                            metadata={"hook": identity.name, "version": identity.version},
                        ) from marker_exc
                    raise ReconciliationError(
                        "required reconciliation completion is not durable",
                        metadata={"hook": identity.name, "version": identity.version},
                    ) from exc

        return ReconciliationReport(
            schema_revision=plan.schema_revision,
            plan_digest=plan.plan_digest,
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            hooks=tuple(reports),
            durably_complete=True,
        )


__all__ = (
    "RECONCILIATION_ADVISORY_LOCK",
    "ReconciliationHookReport",
    "ReconciliationPlan",
    "ReconciliationReport",
    "ReconciliationRunner",
)
