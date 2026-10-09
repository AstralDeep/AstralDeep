"""
Tests for the bounded Cresco bridge contract (Spec 050).

Acceptance checks from issue #279:
  1. Reconcile spec 050, GaiaKeep and current constitution.
  2. Retire stale dependency assumptions.
  3. Document explicit support/unsupported behaviour.
  4. Complete reviewable contract with source traceability.
"""

from __future__ import annotations

import pytest

from backend.cresco import (
    CrescoBridge,
    CrescoContractError,
    CrescoTier,
    TIER_SUPPORT,
    bridge,
)
from backend.shared.feature_flags import (
    cresco_risk_tier_supported,
    cresco_unsupported_states,
)


# ── Tier support constants ──────────────────────────────────────────────────

class TestTierSupport:
    def test_t1_archival_supported(self):
        assert TIER_SUPPORT[CrescoTier.ARCHIVAL] is True

    def test_t2_wsapi_bounded_supported(self):
        assert TIER_SUPPORT[CrescoTier.WSAPI_BOUNDED] is True

    def test_t3_generic_bridge_unsupported(self):
        assert TIER_SUPPORT[CrescoTier.GENERIC_BRIDGE] is False

    def test_t4_mesh_admission_unsupported(self):
        assert TIER_SUPPORT[CrescoTier.MESH_ADMISSION] is False


# ── Contract boundary ───────────────────────────────────────────────────────

class TestContractBoundary:
    def test_reconcile_t1(self):
        result = bridge.reconcile(CrescoTier.ARCHIVAL)
        assert "T1" in result
        assert "supported" in result

    def test_reconcile_t2(self):
        result = bridge.reconcile(CrescoTier.WSAPI_BOUNDED)
        assert "T2" in result
        assert "supported" in result

    def test_reconcile_t3_raises(self):
        with pytest.raises(CrescoContractError, match="outside the bounded contract"):
            bridge.reconcile(CrescoTier.GENERIC_BRIDGE)

    def test_reconcile_t4_raises(self):
        with pytest.raises(CrescoContractError, match="outside the bounded contract"):
            bridge.reconcile(CrescoTier.MESH_ADMISSION)

    def test_unsupported_states(self):
        states = bridge.unsupported_states()
        assert CrescoTier.GENERIC_BRIDGE.name in states
        assert CrescoTier.MESH_ADMISSION.name in states
        assert CrescoTier.ARCHIVAL.name not in states
        assert CrescoTier.WSAPI_BOUNDED.name not in states


# ── Feature-flag integration ────────────────────────────────────────────────

class TestFeatureFlags:
    def test_risk_tier_supported_t1(self):
        assert cresco_risk_tier_supported("archival") is True

    def test_risk_tier_supported_t2(self):
        assert cresco_risk_tier_supported("wsapi_bounded") is True

    def test_risk_tier_supported_t3(self):
        assert cresco_risk_tier_supported("generic_bridge") is False

    def test_risk_tier_supported_unknown(self):
        assert cresco_risk_tier_supported("nonexistent") is False

    def test_unsupported_states_flag(self):
        states = cresco_unsupported_states()
        assert "generic_bridge" in states
        assert "mesh_admission" in states


# ── Deterministic conformance ──────────────────────────────────────────────

class TestConformance:
    def test_no_runtime_activation_for_unsupported(self):
        """T3/T4 must never become supported at runtime."""
        assert not cresco_risk_tier_supported("generic_bridge")
        assert not cresco_risk_tier_supported("mesh_admission")

    def test_isolated_service_keys(self):
        """Service keys remain operator-local; no mesh admission."""
        assert not bridge.is_tier_supported(CrescoTier.MESH_ADMISSION)

    def test_gaiakeep_archival_path(self):
        """T1 archival path remains operational."""
        assert bridge.is_tier_supported(CrescoTier.ARCHIVAL)
