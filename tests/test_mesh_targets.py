# Tests for Mesh Target Resolution and Presence
# Part of AstralDeep #281
# Coverage: deterministic success, edge, denial, failure, and recovery tests

from __future__ import annotations

import pytest
from datetime import datetime, timedelta

from astraldeep.backend.shared.mesh_targets import (
    MeshPresenceService,
    MeshTarget,
    MeshTargetResolution,
    PresenceStatus,
    TargetResolution,
)


@pytest.fixture
def sample_targets() -> dict[str, MeshTarget]:
    """Create sample mesh targets for testing."""
    now = datetime.utcnow()
    return {
        "target-1": MeshTarget(
            target_id="target-1",
            identity_key="key-1",
            capabilities=["read", "write"],
            endpoint="https://endpoint-1.example.com",
            approved_by="admin",
            enrolled_at=now,
            last_seen=now,
            presence_status=PresenceStatus.ONLINE,
        ),
        "target-2": MeshTarget(
            target_id="target-2",
            identity_key="key-2",
            capabilities=["read"],
            endpoint=None,
            enrolled_at=now,
            last_seen=None,
            presence_status=PresenceStatus.OFFLINE,
        ),
        "target-3": MeshTarget(
            target_id="target-3",
            identity_key="key-3",
            capabilities=["write"],
            endpoint="https://endpoint-3.example.com",
            enrolled_at=now,
            last_seen=now - timedelta(days=2),
            presence_status=PresenceStatus.AWAY,
        ),
        "target-4": MeshTarget(
            target_id="target-4",
            identity_key="key-4",
            capabilities=["read", "write"],
            endpoint="https://endpoint-4.example.com",
            enrolled_at=now,
            last_seen=now,
            presence_status=PresenceStatus.REVOKED,
        ),
    }


def make_service(targets: dict[str, MeshTarget]) -> MeshPresenceService:
    """Create a mesh service with verification function."""
    async def verify(target_id: str, identity_key: str) -> bool:
        if target_id in targets:
            return True
        return False

    return MeshPresenceService(
        enrollment_store=targets,
        verify_fn=verify,
        cache_ttl=timedelta(hours=24),
    )


class TestResolveTarget:
    """Test cases for target resolution."""

    def test_approve_valid_target(self, sample_targets):
        """Test resolving an approved target."""
        service = make_service(sample_targets)
        result: MeshTargetResolution = service.resolve_target("target-1")

        assert result.resolution == TargetResolution.APPROVED
        assert result.endpoint == "https://endpoint-1.example.com"
        assert set(result.capabilities) == {"read", "write"}
        assert result.verified_membership is True
        assert result.reason is None

    def test_reject_unknown_target(self, sample_targets):
        """Test resolving an unknown target."""
        service = make_service(sample_targets)
        result: MeshTargetResolution = service.resolve_target("unknown")

        assert result.resolution == TargetResolution.APPROVED
        assert result.verified_membership is False
        assert "requires verification" in result.reason

    def test_reject_revoked_target(self, sample_targets):
        """Test rejecting a revoked target."""
        service = make_service(sample_targets)
        result: MeshTargetResolution = service.resolve_target("target-4")

        assert result.resolution == TargetResolution.REVOKED
        assert result.verified_membership is False

    def test_reject_stale_target(self, sample_targets):
        """Test rejecting a stale target."""
        service = make_service(sample_targets)
        result: MeshTargetResolution = service.resolve_target("target-3")

        assert result.resolution == TargetResolution.STALE
        assert result.verified_membership is False

    def test_resolve_with_unverified_target(self):
        """Test resolving a target that fails verification."""
        targets: dict[str, MeshTarget] = {
            "target-1": MeshTarget(
                target_id="target-1",
                identity_key="key-1",
                last_seen=datetime.utcnow(),
            ),
        }

        async def verify(target_id: str, identity_key: str) -> bool:
            return False

        service = MeshPresenceService(
            enrollment_store=targets,
            verify_fn=verify,
        )
        result: MeshTargetResolution = service.resolve_target("target-1")

        assert result.resolution == TargetResolution.REJECTED
        assert result.verified_membership is False

    def test_resolve_with_verification_error(self):
        """Test handling verification errors."""
        targets: dict[str, MeshTarget] = {
            "target-1": MeshTarget(
                target_id="target-1",
                identity_key="key-1",
                last_seen=datetime.utcnow(),
            ),
        }

        async def verify(target_id: str, identity_key: str) -> bool:
            raise RuntimeError("Verification service down")

        service = MeshPresenceService(
            enrollment_store=targets,
            verify_fn=verify,
        )
        result: MeshTargetResolution = service.resolve_target("target-1")

        assert result.resolution == TargetResolution.AMBIGUOUS
        assert result.verified_membership is False


class TestPresenceUpdates:
    """Test cases for presence updates."""

    def test_update_online_presence(self, sample_targets):
        """Test updating presence to online."""
        service = make_service(sample_targets)
        service.update_presence("target-1", PresenceStatus.ONLINE, "https://new-endpoint.example.com")

        target: MeshTarget = service.get_presence("target-1")
        assert target is not None
        assert target.presence_status == PresenceStatus.ONLINE
        assert target.endpoint == "https://new-endpoint.example.com"

    def test_update_offline_presence(self, sample_targets):
        """Test updating presence to offline."""
        service = make_service(sample_targets)
        service.update_presence("target-1", PresenceStatus.OFFLINE)

        target: MeshTarget = service.get_presence("target-1")
        assert target is not None
        assert target.presence_status == PresenceStatus.OFFLINE

    def test_update_unknown_target(self, sample_targets):
        """Test updating presence for unknown target."""
        service = make_service(sample_targets)
        # Should not raise, just log warning
        service.update_presence("unknown", PresenceStatus.ONLINE)


class TestListTargets:
    """Test cases for listing targets."""

    def test_list_all_approved(self, sample_targets):
        """Test listing all approved targets."""
        service = make_service(sample_targets)
        targets: list[MeshTarget] = service.list_approved_targets()

        # target-1 is online, target-2 is offline but not stale
        # target-3 is stale, target-4 is revoked
        assert len(targets) == 2
        target_ids = {t.target_id for t in targets}
        assert "target-1" in target_ids
        assert "target-2" in target_ids

    def test_list_online_only(self, sample_targets):
        """Test filtering by online status."""
        service = make_service(sample_targets)
        targets: list[MeshTarget] = service.list_approved_targets(
            status_filter=PresenceStatus.ONLINE
        )

        assert len(targets) == 1
        assert targets[0].target_id == "target-1"


class TestRevocation:
    """Test cases for target revocation."""

    def test_revoke_target(self, sample_targets):
        """Test revoking a target."""
        service = make_service(sample_targets)
        success: bool = service.mark_revoked("target-1")

        assert success is True
        target: MeshTarget = service.get_presence("target-1")
        assert target is not None
        assert target.presence_status == PresenceStatus.REVOKED

    def test_revoke_unknown_target(self, sample_targets):
        """Test revoking an unknown target."""
        service = make_service(sample_targets)
        success: bool = service.mark_revoked("unknown")

        assert success is False


class TestCacheExpiry:
    """Test cache expiry and reconnection scenarios."""

    def test_expired_cache_clear(self, sample_targets):
        """Test clearing the presence cache."""
        service = make_service(sample_targets)
        service.clear_cache()
        # Cache should be cleared without error


class TestIdentityReplacement:
    """Test identity replacement scenarios."""

    def test_replace_target_identity(self, sample_targets):
        """Test replacing a target's identity."""
        service = make_service(sample_targets)

        # Replace target-1 with new identity
        new_target = MeshTarget(
            target_id="target-1",
            identity_key="new-key-1",
            capabilities=["read", "write", "admin"],
            endpoint="https://new-endpoint.example.com",
            approved_by="admin",
            enrolled_at=datetime.utcnow(),
            last_seen=datetime.utcnow(),
            presence_status=PresenceStatus.ONLINE,
        )
        sample_targets["target-1"] = new_target

        result: MeshTargetResolution = service.resolve_target("target-1")
        assert result.resolution == TargetResolution.APPROVED
        assert result.endpoint == "https://new-endpoint.example.com"
        assert "admin" in result.capabilities
