"""
Cresco bridge contract — Spec 050 bounded reconciliation.

This module declares the legal operational boundary for the Cresco bridge.
The generic bridge (T3) and mesh admission (T4) paths are explicitly
unsupported; only the GaiaKeep archival path (T1) and bounded wsapi (T2)
are in scope.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Final

# ── Risk tiers (per spec 050) ───────────────────────────────────────────────

class CrescoTier(Enum):
    ARCHIVAL = auto()           # T1 — GaiaKeep write-only
    WSAPI_BOUNDED = auto()      # T2 — read from approved store
    GENERIC_BRIDGE = auto()     # T3 — UNSUPPORTED
    MESH_ADMISSION = auto()     # T4 — UNSUPPORTED


TIER_SUPPORT: Final[dict[CrescoTier, bool]] = {
    CrescoTier.ARCHIVAL:          True,
    CrescoTier.WSAPI_BOUNDED:     True,
    CrescoTier.GENERIC_BRIDGE:    False,
    CrescoTier.MESH_ADMISSION:    False,
}

# ── Contract boundary ────────────────────────────────────────────────────────

class CrescoContractError(Exception):
    """Raised when an operation falls outside the bounded contract."""


class CrescoBridge:
    """
    Bounded Cresco bridge contract.

    Only T1 (archival) and T2 (bounded wsapi) paths are operational.
    T3 (generic bridge) and T4 (mesh admission) are explicitly unsupported.
    """

    def __init__(self) -> None:
        self._tiers: dict[CrescoTier, bool] = dict(TIER_SUPPORT)

    def is_tier_supported(self, tier: CrescoTier) -> bool:
        return self._tiers.get(tier, False)

    def reconcile(self, tier: CrescoTier) -> str:
        """
        Return deterministic conformance outcome for *tier*.

        Raises CrescoContractError for unsupported tiers.
        """
        if not self.is_tier_supported(tier):
            raise CrescoContractError(
                f"{tier.name} (T{tier.value}) is outside the bounded contract. "
                "See spec 050 — Cresco Integration Decision."
            )
        if tier is CrescoTier.ARCHIVAL:
            return "T1: GaiaKeep archival write to approved store — supported"
        if tier is CrescoTier.WSAPI_BOUNDED:
            return "T2: Bounded wsapi read from approved store — supported"
        return "unknown"

    def unsupported_states(self) -> list[str]:
        return [
            t.name for t, supported in self._tiers.items() if not supported
        ]


# ── Module-level convenience ────────────────────────────────────────────────

bridge: Final[CrescoBridge] = CrescoBridge()


__all__ = [
    "CrescoTier",
    "CrescoContractError",
    "CrescoBridge",
    "TIER_SUPPORT",
    "bridge",
]
