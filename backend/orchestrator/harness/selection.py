"""Fail-closed harness deployment catalog and profile selection: registration
validates the full seam set against the pinned contract version, and profile
selection stays unavailable until the rollout flag is enabled AND the catalog
carries a qualified rollout record — ordinary dispatch keeps using the existing
runners unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Tuple

from shared.feature_flags import flags as global_flags

from .contract import (
    HARNESS_CONTRACT_VERSION,
    HarnessContractError,
    HarnessDeployment,
)
from .standard_llm_adapter import standard_deployment
from .typesafe_adapter import typesafe_deployment

logger = logging.getLogger("Orchestrator.Harness.Selection")

ROLLOUT_FLAG = "harness_profile_selection"

ROLLOUT_ENV_VAR = "FF_HARNESS_PROFILE_SELECTION"


class HarnessSelectionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HarnessCatalog:
    deployments: Tuple[HarnessDeployment, ...]
    qualified_rollout: bool = False

    def __post_init__(self) -> None:
        seen: set[str] = set()
        defaults = 0
        for deployment in self.deployments:
            validate_catalog_deployment(deployment)
            if deployment.identity.harness_id in seen:
                raise HarnessContractError(
                    f"duplicate harness deployment: "
                    f"{deployment.identity.harness_id!r}"
                )
            seen.add(deployment.identity.harness_id)
            defaults += 1 if deployment.default else 0
        if defaults != 1:
            raise HarnessContractError(
                f"a catalog requires exactly one default deployment, got {defaults}"
            )

    def deployment(self, harness_id: str) -> HarnessDeployment:
        for deployment in self.deployments:
            if deployment.identity.harness_id == harness_id:
                return deployment
        raise HarnessContractError(f"no harness deployment named {harness_id!r}")

    def select(self, harness_id: str, *, flags=None) -> HarnessDeployment:
        deployment = self.deployment(harness_id)
        flag_source = flags if flags is not None else global_flags
        try:
            rollout_enabled = bool(flag_source.is_enabled(ROLLOUT_FLAG))
        except Exception:
            rollout_enabled = False
        if not rollout_enabled:
            logger.info(
                "harness profile selection for %s is unavailable: the %s rollout "
                "flag is off",
                harness_id,
                ROLLOUT_ENV_VAR,
            )
            raise HarnessSelectionUnavailable(
                f"harness profile selection is unavailable: {ROLLOUT_ENV_VAR} is off"
            )
        if not self.qualified_rollout:
            logger.info(
                "harness profile selection for %s is unavailable: no qualified "
                "rollout record exists for the harness catalog",
                harness_id,
            )
            raise HarnessSelectionUnavailable(
                "harness profile selection is unavailable: no deployment is "
                "qualified for rollout yet"
            )
        return deployment


def validate_catalog_deployment(deployment: HarnessDeployment) -> None:
    if deployment.identity.contract_version != HARNESS_CONTRACT_VERSION:
        raise HarnessContractError(
            f"deployment {deployment.identity.harness_id!r} targets contract "
            f"{deployment.identity.contract_version!r}, not "
            f"{HARNESS_CONTRACT_VERSION!r}"
        )


def default_catalog() -> HarnessCatalog:
    return HarnessCatalog(
        deployments=(
            standard_deployment(default=True),
            typesafe_deployment(),
        )
    )


__all__ = (
    "ROLLOUT_ENV_VAR",
    "ROLLOUT_FLAG",
    "HarnessCatalog",
    "HarnessSelectionUnavailable",
    "default_catalog",
    "validate_catalog_deployment",
)
