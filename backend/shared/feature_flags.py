"""
Feature flag definitions for AstralDeep backend.
"""

from enum import Enum
from typing import Any


class FeatureFlag:
    """
    Simple feature flag container.
    """

    def __init__(self, name: str, default: bool = False):
        self.name = name
        self.default = default

    def is_enabled(self, overrides: dict[str, Any] | None = None) -> bool:
        """
        Return the effective state of the flag, optionally applying a dict of overrides.
        """
        if overrides and self.name in overrides:
            return bool(overrides[self.name])
        return self.default


class FeatureFlags(Enum):
    """
    Central enumeration of all feature flags used by the system.
    """

    # Existing flags ---------------------------------------------------------
    A2A_SERVER_OPT_IN = FeatureFlag("a2a_server_opt_in", default=True)
    FF_A2A_SERVER = FeatureFlag("ff_a2a_server", default=True)

    # -----------------------------------------------------------------------
    # New flag: enforce owner‑managed A2A publication and withdrawal
    # -----------------------------------------------------------------------
    OWNER_MANAGED_A2A_PUBLICATION = FeatureFlag(
        "owner_managed_a2a_publication", default=False
    )
