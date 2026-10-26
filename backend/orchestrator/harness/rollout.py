"""Owner-scoped harness profile selection with conformance-bound qualification,
stable in-flight bindings, and controlled new-work rollout and rollback; the
catalog default stays reachable as the shipped baseline while every other profile
must qualify through a passing conformance report before new work can roll onto it.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Optional, Tuple

from .conformance import ConformanceReport
from .contract import (
    MAX_USER_ID_CHARS,
    HarnessContractError,
    HarnessDeployment,
    HarnessIdentity,
)
from .selection import (
    ROLLOUT_ENV_VAR,
    ROLLOUT_FLAG,
    HarnessCatalog,
    HarnessSelectionUnavailable,
    default_catalog,
)
from shared.feature_flags import flags as global_flags

logger = logging.getLogger("Orchestrator.Harness.Rollout")


class HarnessProfileUnavailable(HarnessSelectionUnavailable):
    def __init__(self, harness_id: str, reason: str) -> None:
        self.harness_id = harness_id
        self.reason = reason
        super().__init__(f"harness profile {harness_id!r} is unavailable: {reason}")


@dataclass(frozen=True, slots=True)
class HarnessBinding:
    identity: HarnessIdentity
    deployment: HarnessDeployment
    owner_id: str

    @property
    def harness_id(self) -> str:
        return self.identity.harness_id


@dataclass(frozen=True, slots=True)
class HarnessRolloutTransition:
    action: str
    changed: bool
    previous_harness_id: str
    target_harness_id: str


def _validate_owner(owner_id: str) -> str:
    if not owner_id or len(owner_id) > MAX_USER_ID_CHARS:
        raise HarnessContractError(
            f"owner_id must be 1..{MAX_USER_ID_CHARS} chars: {owner_id!r}"
        )
    return owner_id


class HarnessRollout:
    def __init__(self, catalog: Optional[HarnessCatalog] = None, *, flags=None) -> None:
        self._catalog = catalog if catalog is not None else default_catalog()
        self._flags = flags
        self._lock = threading.RLock()
        self._qualified: dict[str, HarnessIdentity] = {}
        self._target = self._default_harness_id()

    @property
    def target(self) -> str:
        with self._lock:
            return self._target

    @property
    def qualified_ids(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._qualified))

    @property
    def catalog(self) -> HarnessCatalog:
        return self._catalog

    def qualify(
        self, deployment: HarnessDeployment, report: ConformanceReport
    ) -> HarnessIdentity:
        if not isinstance(deployment, HarnessDeployment):
            raise HarnessContractError("qualification requires a harness deployment")
        if not isinstance(report, ConformanceReport):
            raise HarnessContractError("qualification requires a conformance report")
        if not report.passed:
            raise HarnessContractError(
                f"profile {deployment.identity.harness_id!r} cannot qualify on a "
                "failing conformance report"
            )
        if not report.results:
            raise HarnessContractError(
                "qualification requires a conformance report with at least one "
                "scenario result"
            )
        if report.identity != deployment.identity:
            raise HarnessContractError(
                "the conformance report identity does not match the deployment "
                f"identity: {report.identity!r} vs {deployment.identity!r}"
            )
        if not self._catalog_member(deployment):
            raise HarnessContractError(
                f"deployment {deployment.identity.harness_id!r} is not a member of "
                "this rollout's catalog"
            )
        with self._lock:
            self._qualified[deployment.identity.harness_id] = deployment.identity
        logger.info(
            "harness profile %s (%s, adapter %s) qualified for rollout",
            deployment.identity.harness_id,
            deployment.identity.profile,
            deployment.identity.adapter_version,
        )
        return deployment.identity

    def select(self, harness_id: str, *, owner_id: str) -> HarnessBinding:
        _validate_owner(owner_id)
        if not self._rollout_enabled():
            logger.info(
                "harness profile selection for %s is unavailable: the %s rollout "
                "flag is off",
                harness_id,
                ROLLOUT_ENV_VAR,
            )
            raise HarnessProfileUnavailable(harness_id, "rollout_flag_disabled")
        try:
            deployment = self._catalog.deployment(harness_id)
        except HarnessContractError:
            logger.info(
                "harness profile selection denied for owner %s: unknown profile %s",
                owner_id,
                harness_id,
            )
            raise HarnessProfileUnavailable(harness_id, "unknown_profile") from None
        if not self._selectable(deployment):
            logger.info(
                "harness profile selection for %s is unavailable: no passing "
                "conformance report qualifies this profile",
                harness_id,
            )
            raise HarnessProfileUnavailable(harness_id, "not_qualified")
        logger.info(
            "owner %s selected harness profile %s (%s)",
            owner_id,
            harness_id,
            deployment.identity.profile,
        )
        return HarnessBinding(
            identity=deployment.identity,
            deployment=deployment,
            owner_id=owner_id,
        )

    def select_new_work(self, *, owner_id: str) -> HarnessBinding:
        with self._lock:
            target = self._target
        return self.select(target, owner_id=owner_id)

    def enable(self, harness_id: str) -> HarnessRolloutTransition:
        try:
            deployment = self._catalog.deployment(harness_id)
        except HarnessContractError:
            raise HarnessProfileUnavailable(harness_id, "unknown_profile") from None
        if not self._selectable(deployment):
            raise HarnessProfileUnavailable(harness_id, "not_qualified")
        with self._lock:
            previous = self._target
            self._target = harness_id
        logger.info(
            "harness new-work rollout moved %s -> %s", previous, harness_id
        )
        return HarnessRolloutTransition(
            action="enable",
            changed=previous != harness_id,
            previous_harness_id=previous,
            target_harness_id=harness_id,
        )

    def rollback(self) -> HarnessRolloutTransition:
        default_id = self._default_harness_id()
        with self._lock:
            previous = self._target
            self._target = default_id
        logger.info(
            "harness new-work rollout rolled back %s -> %s", previous, default_id
        )
        return HarnessRolloutTransition(
            action="rollback",
            changed=previous != default_id,
            previous_harness_id=previous,
            target_harness_id=default_id,
        )

    def _default_harness_id(self) -> str:
        for deployment in self._catalog.deployments:
            if deployment.default:
                return deployment.identity.harness_id
        raise HarnessContractError("the harness catalog carries no default deployment")

    def _rollout_enabled(self) -> bool:
        source = self._flags if self._flags is not None else global_flags
        try:
            return bool(source.is_enabled(ROLLOUT_FLAG))
        except Exception:
            return False

    def _catalog_member(self, deployment: HarnessDeployment) -> bool:
        try:
            cataloged = self._catalog.deployment(deployment.identity.harness_id)
        except HarnessContractError:
            return False
        return cataloged.identity == deployment.identity

    def _selectable(self, deployment: HarnessDeployment) -> bool:
        if deployment.default:
            return True
        with self._lock:
            qualified = self._qualified.get(deployment.identity.harness_id)
        return qualified is not None and qualified == deployment.identity


def default_rollout() -> HarnessRollout:
    return HarnessRollout(default_catalog())


__all__ = (
    "HarnessBinding",
    "HarnessProfileUnavailable",
    "HarnessRollout",
    "HarnessRolloutTransition",
    "default_rollout",
)
