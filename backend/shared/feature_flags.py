"""
Feature flags for AstralDeep.

All flags default to False unless explicitly enabled.
Service keys remain operator-local; no mesh admission is granted.
"""

from __future__ import annotations

# ── Cresco bridge (Spec 050 — bounded) ──────────────────────────────────────
# T1: GaiaKeep archival path — supported
CRESO_ARCHIVAL_ENABLED: bool = True

# T2: Bounded wsapi read from approved store — supported
CRESO_WSAPI_BOUNDED: bool = True

# T3: Generic Cresco bridge topology/discovery/dispatch — UNSUPPORTED
CRESO_BRIDGE_GENERIC: bool = False

# T4: Mesh admission / service-key external admission — UNSUPPORTED
CRESO_MESH_ADMISSION: bool = False

# ── Existing stable flags (preserve) ────────────────────────────────────────
KEYCLOAK_ENABLED: bool = True          # RFC 8693 authentication
OWNER_TOOL_BOUNDARY: bool = True       # owner/tool/PHI/egress/confirmation
AUDIT_LOG_ENABLED: bool = True         # immutable audit trail
PHI_GATING_ENABLED: bool = True        # PHI restricted path
EGRESS_CONFIRMATION: bool = True       # outbound confirmation required

# ── Runtime helpers ─────────────────────────────────────────────────────────

_CRESO_RISK_TIERS: dict[str, bool] = {
    "archival":          CRESO_ARCHIVAL_ENABLED,    # T1
    "wsapi_bounded":     CRESO_WSAPI_BOUNDED,       # T2
    "generic_bridge":    CRESO_BRIDGE_GENERIC,      # T3 — unsupported
    "mesh_admission":    CRESO_MESH_ADMISSION,      # T4 — unsupported
}


def cresco_risk_tier_supported(tier: str) -> bool:
    """Return True only for bounded tiers (T1/T2)."""
    return _CRESO_RISK_TIERS.get(tier, False)


def cresco_unsupported_states() -> list[str]:
    """Explicit unsupported states per Spec 050."""
    return [k for k, v in _CRESO_RISK_TIERS.items() if not v]


__all__ = [
    "CRESO_ARCHIVAL_ENABLED",
    "CRESO_WSAPI_BOUNDED",
    "CRESO_BRIDGE_GENERIC",
    "CRESO_MESH_ADMISSION",
    "KEYCLOAK_ENABLED",
    "OWNER_TOOL_BOUNDARY",
    "AUDIT_LOG_ENABLED",
    "PHI_GATING_ENABLED",
    "EGRESS_CONFIRMATION",
    "cresco_risk_tier_supported",
    "cresco_unsupported_states",
]
